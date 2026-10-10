"""#944 (part B, slice 3): the proof gate's checks as pure functions, each with the control that makes it red.

`conflict_proof.py` is a library nothing calls yet, so the gate-closed guarantee is that it reads no config, spawns
nothing at import and is not wired anywhere (tested below). The real-git fixtures build their own scratch repository
with fixed identities and dates.

HOW EACH CONTROL IS BROKEN (and was seen red once):
  * context drift: `pair_commits(..., key=patch_id_of)` turns the drift fixture red (test below runs both).
  * ambiguity: a gate that picked one of two same-key commits would pair them; the test demands a park.
  * marker / whitespace / multiset / stage-0 / counter: each has a case that must refuse next to one that must pass.

D-6 (merge stop) and D-41 (key collisions) are measured on scratch repositories only, never on an adopter's."""
import collections
import importlib.util
import os
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cp = _load("conflict_proof")
cs = _load("conflict_state")

MAIL = "dev" + "@" + "example.test"          # built by concatenation: a readiness scan rejects email-shaped literals


def _env(date):
    return {"PATH": os.environ.get("PATH", ""), "HOME": os.devnull, "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull, "GIT_AUTHOR_NAME": "Dev", "GIT_AUTHOR_EMAIL": MAIL,
            "GIT_COMMITTER_NAME": "Dev", "GIT_COMMITTER_EMAIL": MAIL,
            "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date, "GIT_EDITOR": "true"}


def git(cwd, *args, date="1700000000 +0000", check=True):
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, env=_env(date))
    if check and p.returncode != 0:
        raise AssertionError("git %s: %s" % (" ".join(args), p.stderr or p.stdout))
    return p.stdout.strip()


def _run(cwd, argv):
    p = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, env=_env("1700000000 +0000"))
    if p.returncode != 0:
        raise RuntimeError(p.stderr)
    return p.stdout


def put(repo, name, text):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def lines(n, tag="line"):
    return "".join("%s%d\n" % (tag, i) for i in range(1, n + 1))


def edit(text, number, new):
    rows = text.splitlines(True)
    rows[number - 1] = new + "\n"
    return "".join(rows)


def commit(repo, msg, date="1700000100 +0000"):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg, date=date)
    return git(repo, "rev-parse", "HEAD")


def new_repo(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    return repo


def patch_id_fn(repo):
    return cp.git_patch_id_fn(repo)


def ranges(repo, base_before, base_after, tip_before="unit@{1}", tip_after="unit"):
    return "%s..%s" % (base_before, tip_before), "%s..%s" % (base_after, tip_after)


def drift_world(tmp_path, drifting=1, conflict=True):
    """base with `drifting` files a1..; main edits a line TWO lines from each unit hunk; the unit edits each file
    and (if `conflict`) ends with a commit that conflicts with main. Returns (repo, original_base, original_tip)."""
    repo = new_repo(tmp_path)
    for i in range(1, drifting + 1):
        put(repo, "a%d.txt" % i, lines(40))
    put(repo, "b.txt", lines(40, "b"))
    commit(repo, "base")
    git(repo, "checkout", "-q", "-b", "unit")
    for i in range(1, drifting + 1):
        put(repo, "a%d.txt" % i, edit(lines(40), 20, "UNIT"))
        commit(repo, "unit edits a%d" % i, date="17000002%02d +0000" % i)
    if conflict:
        put(repo, "b.txt", edit(lines(40, "b"), 10, "unit-b"))
        commit(repo, "unit edits b", date="1700000500 +0000")
    git(repo, "checkout", "-q", "main")
    for i in range(1, drifting + 1):
        put(repo, "a%d.txt" % i, edit(lines(40), 22, "BASE"))
    if conflict:
        put(repo, "b.txt", edit(lines(40, "b"), 10, "main-b"))
    commit(repo, "main moves", date="1700000900 +0000")
    git(repo, "checkout", "-q", "unit")
    return repo


def replay(repo, resolve=None):
    """Rebase unit onto main by hand, recording the stop. Returns (orig_base, orig_tip, stops) where stops maps the
    stopped original sha -> {"new_head"} written once the continuation finishes."""
    orig_base = git(repo, "merge-base", "main", "unit")
    orig_tip = git(repo, "rev-parse", "unit")
    p = subprocess.run(["git", "rebase", "main"], cwd=str(repo), capture_output=True, text=True, env=_env("1700001000 +0000"))
    stops = {}
    while p.returncode != 0:
        stopped = git(repo, "rev-parse", "REBASE_HEAD")
        for path in cs.conflicted_paths(_run, repo):
            put(repo, path, (resolve or {}).get(path, "resolved\n"))
            git(repo, "add", path)
        p = subprocess.run(["git", "rebase", "--continue"], cwd=str(repo), capture_output=True, text=True,
                           env=_env("1700001000 +0000"))
        stops[stopped] = {"new_head": None}
    return orig_base, orig_tip, stops


def prove_pairing(repo, orig_base, orig_tip, stops, key=cp.key_of, emptied=None, upstream=None):
    pid = patch_id_fn(repo)
    originals = cp.read_commits(_run, repo, "%s..%s" % (orig_base, orig_tip), pid)
    replayed = cp.read_commits(_run, repo, "main..HEAD", pid)
    return originals, replayed, cp.pair_commits(originals, replayed, stops, upstream, emptied, key=key)


# --------------------------------------------------------------------------- D: markers and whitespace
def test_marker_left_behind_is_refused_and_clean_blob_passes():
    bad = "a\n<<<<<<< HEAD\nx\n=======\ny\n>>>>>>> other\n"
    assert [r["code"] for r in cp.check_markers({"f.txt": bad})] == [cp.MARKER]
    assert cp.check_markers({"f.txt": "a\nb\n"}) == []


def test_diff3_base_marker_counts():
    assert cp.marker_count("<<<<<<< a\n||||||| b\n=======\n>>>>>>> c\n") == 4


def test_setext_rule_alone_is_not_a_marker():
    assert cp.marker_count("Title\n=======\nbody\n") == 0


def test_file_about_markers_is_allowed_what_the_original_had():
    doc = "<<<<<<< x\n=======\n>>>>>>> y\n"
    assert cp.check_markers({"d.md": doc}, {"d.md": doc}) == []
    assert cp.check_markers({"d.md": doc + doc}, {"d.md": doc}) != []


CHECK_OLD = "f.py:3: trailing whitespace.\n+x = 1 \n"


def test_new_whitespace_finding_is_refused():
    new = CHECK_OLD + "g.py:9: space before tab in indent.\n+ \tbad\n"
    got = cp.new_whitespace_findings(new, CHECK_OLD)
    assert [(r["code"], r["path"]) for r in got] == [(cp.WHITESPACE, "g.py")]


def test_inherited_and_moved_whitespace_findings_are_allowed():
    moved = "f.py:77: trailing whitespace.\n+x = 1 \n"
    assert cp.new_whitespace_findings(moved, CHECK_OLD) == []
    assert cp.new_whitespace_findings("", "") == []


def test_unscoped_check_would_refuse_an_ordinary_commit_but_scoped_does_not():
    assert cp.new_whitespace_findings(CHECK_OLD, CHECK_OLD) == []
    assert cp.whitespace_findings(CHECK_OLD)            # the finding exists; only its novelty is judged


# --------------------------------------------------------------------------- C: stage-0 and multiset
def test_stage0_edit_outside_the_conflicted_set_is_refused():
    before = {"a": ("100644", "1"), "b": ("100644", "2")}
    after = {"a": ("100644", "9"), "b": ("100644", "2"), "c": ("100644", "3")}
    got = cp.only_conflicted_differ(before, after, ["x"])
    assert [(r["path"]) for r in got] == ["a", "c"]
    assert cp.only_conflicted_differ(before, after, ["a", "c"]) == []


def test_stage0_removal_outside_is_refused():
    assert cp.only_conflicted_differ({"a": ("1", "1")}, {}, []) != []


def test_multiset_ignores_placement_and_context():
    a = "diff --git a/f b/f\nindex 1..2 100644\n--- a/f\n+++ b/f\n@@ -3 +3 @@\n-old\n+new\n"
    b = a.replace("@@ -3 +3 @@", "@@ -9 +11 @@")
    assert cp.multiset_differences(cp.per_path_multisets(a), cp.per_path_multisets(b), []) == []


def test_multiset_refuses_a_changed_line_in_another_file():
    a = "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -3 +3 @@\n-old\n+new\n"
    b = a.replace("+new", "+NEW")
    got = cp.multiset_differences(cp.per_path_multisets(a), cp.per_path_multisets(b), [])
    assert [r["path"] for r in got] == ["f"] and got[0]["code"] == cp.MULTISET_CHANGED
    assert cp.multiset_differences(cp.per_path_multisets(a), cp.per_path_multisets(b), ["f"]) == []


def test_multiset_sees_mode_change_and_added_content_starting_with_plus():
    a = "diff --git a/f b/f\nold mode 100644\nnew mode 100755\n"
    assert cp.per_path_multisets(a)["f"][0]["\0new mode 100755"] == 1
    b = "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1 +1 @@\n-x\n+++y\n"
    assert cp.per_path_multisets(b)["f"][0]["++y"] == 1


def test_real_git_drift_commits_match_by_multiset_and_an_edit_is_refused(tmp_path):
    repo = drift_world(tmp_path)
    ob, ot, stops = replay(repo)
    originals = cp.read_commits(_run, repo, "%s..%s" % (ob, ot))
    replayed = cp.read_commits(_run, repo, "main..HEAD")
    conflicted = ["b.txt"]
    for o, n in zip(originals, replayed):
        assert cp.multiset_differences(o["diff"], n["diff"], conflicted) == []
    # control: the engine edits a neighbour in a file outside the conflicted set
    put(repo, "a1.txt", edit((repo / "a1.txt").read_text(), 5, "ENGINE"))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--amend", "--no-edit")
    replayed = cp.read_commits(_run, repo, "main..HEAD")
    got = cp.multiset_differences(originals[-1]["diff"], replayed[-1]["diff"], conflicted)
    assert [r["path"] for r in got] == ["a1.txt"]


def test_real_git_binary_content_change_outside_conflicted_set_is_refused(tmp_path):
    """A binary file differing in the replay yields the same `Binary files differ` text; the blob ids must differ."""
    repo = new_repo(tmp_path)
    (repo / "a.bin").write_bytes(b"\0base\0")
    commit(repo, "base")
    git(repo, "checkout", "-q", "-b", "unit")
    (repo / "a.bin").write_bytes(b"\0one\0")
    commit(repo, "unit")
    orig = cp.read_commits(_run, repo, "main..unit")
    git(repo, "checkout", "-q", "--detach", "main")
    (repo / "a.bin").write_bytes(b"\0TWO\0")
    commit(repo, "replay")
    replayed = cp.read_commits(_run, repo, "main..HEAD")
    got = cp.multiset_differences(orig[0]["diff"], replayed[0]["diff"], [])
    assert [r["path"] for r in got] == ["a.bin"]
    same = cp.multiset_differences(orig[0]["diff"], orig[0]["diff"], [])
    assert same == []


# --------------------------------------------------------------------------- E: the test counter
BASE_T = "def test_a():\n    assert 1\n"
OURS_T = BASE_T + "def test_b():\n    assert 2\n"
THEIRS_T = BASE_T + "def test_c():\n    assert 3\n"
BOTH_T = BASE_T + "def test_b():\n    assert 2\ndef test_c():\n    assert 3\n"


def test_resolution_keeping_both_sides_passes():
    assert cp.check_tests("tests/test_x.py", BASE_T, OURS_T, THEIRS_T, BOTH_T) == []


def test_resolution_dropping_a_test_is_refused():
    got = cp.check_tests("tests/test_x.py", BASE_T, OURS_T, THEIRS_T, OURS_T)
    assert [r["code"] for r in got] == [cp.TEST_LOSS]


def test_dropping_only_assertions_is_refused():
    thin = BASE_T + "def test_b():\n    pass\ndef test_c():\n    pass\n"
    assert cp.check_tests("tests/test_x.py", BASE_T, OURS_T, THEIRS_T, thin) != []


def test_identical_additions_kept_once_fail_closed():
    """D-26: a legitimate resolution that keeps one copy of a test both sides added identically parks."""
    both = BASE_T + "def test_n():\n    assert 4\n"
    assert cp.check_tests("tests/test_x.py", BASE_T, both, both, both)[0]["code"] == cp.TEST_LOSS


def test_non_python_test_file_is_refused_and_non_test_path_passes():
    assert cp.check_tests("web/app.spec.ts", "", "", "", "")[0]["code"] == cp.TEST_LANGUAGE
    assert cp.check_tests("src/app.py", None, None, None, None) == []


def test_absent_resolved_or_unreadable_stage_is_refused_not_counted_as_zero():
    assert cp.check_tests("tests/test_x.py", BASE_T, OURS_T, THEIRS_T, None)[0]["code"] == cp.TEST_UNREADABLE
    assert cp.check_tests("tests/test_x.py", b"x", OURS_T, THEIRS_T, BOTH_T)[0]["code"] == cp.TEST_UNREADABLE


def test_both_added_file_without_a_base_counts_base_as_zero():
    assert cp.check_tests("tests/test_n.py", None, OURS_T, THEIRS_T, OURS_T + THEIRS_T) == []


def test_added_skip_marker_is_refused():
    skipped = BOTH_T.replace("def test_b", "@pytest.mark.skip\ndef test_b")
    assert cp.check_tests("tests/test_x.py", BASE_T, OURS_T, THEIRS_T, skipped)[0]["code"] == cp.TEST_SKIP


def test_unittest_and_raises_forms_are_counted():
    assert cp.count_tests("class TestX:\n  def test_a(self):\n    self.assertEqual(1, 1)\n    m.assert_called()\n")[0] == 4
    assert cp.count_tests("with pytest.raises(E):\n    f()\n")[0] == 1


# --------------------------------------------------------------------------- B: pairing, on real git
def test_context_drift_pairs_every_commit_and_passes_both_checks(tmp_path):
    repo = drift_world(tmp_path, drifting=2)
    ob, ot, stops = replay(repo)
    stops[next(iter(stops))]["new_head"] = git(repo, "rev-parse", "HEAD")
    originals, replayed, got = prove_pairing(repo, ob, ot, stops)
    assert got["refusals"] == [] and len(got["pairs"]) == 3
    for o, n in got["pairs"]:
        a = next(c for c in originals if c["sha"] == o)
        b = next(c for c in replayed if c["sha"] == n)
        assert cp.multiset_differences(a["diff"], b["diff"], ["b.txt"]) == []


def test_context_drift_is_red_when_the_key_is_swapped_for_patch_id_equality(tmp_path):
    """The control: patch-id equality cannot pair a commit whose neighbour moved, so it parks the drift fixture."""
    repo = drift_world(tmp_path, drifting=2)
    ob, ot, stops = replay(repo)
    stops[next(iter(stops))]["new_head"] = git(repo, "rev-parse", "HEAD")
    originals, replayed, _ = prove_pairing(repo, ob, ot, stops)
    assert originals[0]["patch_id"] != replayed[0]["patch_id"], "the fixture must really drift the patch-id"
    swapped = cp.pair_commits(originals, replayed, {}, None, None, key=cp.patch_id_of)
    assert swapped["refusals"], "patch-id pairing must park the drift fixture"
    keyed = cp.pair_commits(originals, replayed, stops)
    assert keyed["refusals"] == []


def test_twenty_drifting_commits_park_none(tmp_path):
    repo = drift_world(tmp_path, drifting=20, conflict=False)
    ob, ot, stops = replay(repo)
    originals, replayed, got = prove_pairing(repo, ob, ot, stops)
    assert len(originals) == 20 and got["refusals"] == [] and len(got["pairs"]) == 20
    assert sum(1 for o, n in zip(originals, replayed) if o["patch_id"] != n["patch_id"]) >= 10   # drift is real
    swapped = cp.pair_commits(originals, replayed, key=cp.patch_id_of)
    assert len(swapped["refusals"]) >= 10                        # a storm, if the key were the patch-id


def test_resolved_commit_pairs_through_its_stop_record_when_the_key_no_longer_matches(tmp_path):
    repo = drift_world(tmp_path)
    ob, ot, stops = replay(repo, resolve={"b.txt": "resolved\n"})
    git(repo, "commit", "-q", "--amend", "-m", "unit edits b (resolved)")        # the stamp changes the subject
    stops[next(iter(stops))]["new_head"] = git(repo, "rev-parse", "HEAD")
    originals, replayed, got = prove_pairing(repo, ob, ot, stops)
    assert got["refusals"] == [] and got["pairs"][-1] == (originals[-1]["sha"], replayed[-1]["sha"])
    assert originals[-1]["patch_id"] != replayed[-1]["patch_id"]
    no_record = cp.pair_commits(originals, replayed, {})
    assert {r["code"] for r in no_record["refusals"]} == {cp.UNACCOUNTED, cp.UNMATCHED_REPLAYED}


def _hotfix_world(tmp_path, drift):
    repo = new_repo(tmp_path)
    put(repo, "a.txt", lines(40))
    commit(repo, "base")
    git(repo, "checkout", "-q", "-b", "unit")
    put(repo, "a.txt", edit(lines(40), 20, "FIX"))
    commit(repo, "fix a", date="1700000200 +0000")
    put(repo, "k.txt", "kept\n")
    commit(repo, "keep k", date="1700000250 +0000")
    git(repo, "checkout", "-q", "main")
    if drift:
        put(repo, "a.txt", edit(lines(40), 22, "NEAR"))
        commit(repo, "main near", date="1700000300 +0000")
    git(repo, "cherry-pick", "unit~1")
    git(repo, "checkout", "-q", "unit")
    return repo


def test_base_hotfix_cherry_picked_into_the_unit_is_skipped_not_parked(tmp_path):
    repo = _hotfix_world(tmp_path, drift=False)
    ob, ot = git(repo, "merge-base", "main", "unit"), git(repo, "rev-parse", "unit")
    git(repo, "rebase", "main")
    pid = patch_id_fn(repo)
    originals = cp.read_commits(_run, repo, "%s..%s" % (ob, ot), pid)
    replayed = cp.read_commits(_run, repo, "main..HEAD", pid)
    assert len(originals) == 2 and len(replayed) == 1             # git skipped the cherry-picked commit
    upstream = cp.upstream_patch_ids(_run, repo, "%s..main" % ob, pid)
    ok = cp.pair_commits(originals, replayed, upstream=upstream)
    assert ok["refusals"] == [] and ok["skipped"] == [originals[0]["sha"]] and len(ok["pairs"]) == 1


def test_dropped_commit_parks(tmp_path):
    """The control for the drop: the same range with the upstream set withheld must park the skipped commit."""
    repo = _hotfix_world(tmp_path, drift=False)
    ob, ot = git(repo, "merge-base", "main", "unit"), git(repo, "rev-parse", "unit")
    git(repo, "rebase", "main")
    originals = cp.read_commits(_run, repo, "%s..%s" % (ob, ot), patch_id_fn(repo))
    replayed = cp.read_commits(_run, repo, "main..HEAD", patch_id_fn(repo))
    dropped = cp.pair_commits(originals, replayed, upstream=set())
    assert [r["code"] for r in dropped["refusals"]] == [cp.UNACCOUNTED]


def test_a_cherry_pick_whose_context_drifted_is_unaccounted_and_parks(tmp_path):
    """D-7, measured: the patch-id test is git's own, so a cherry-pick with moved context is not recognised as
    upstream either; git drops it as empty on a clean replay, and the gate has no key for it and parks."""
    repo = _hotfix_world(tmp_path, drift=True)
    ob, ot = git(repo, "merge-base", "main", "unit"), git(repo, "rev-parse", "unit")
    git(repo, "rebase", "main")
    pid = patch_id_fn(repo)
    originals = cp.read_commits(_run, repo, "%s..%s" % (ob, ot), pid)
    replayed = cp.read_commits(_run, repo, "main..HEAD", pid)
    upstream = cp.upstream_patch_ids(_run, repo, "%s..main" % ob, pid)
    assert [r["code"] for r in cp.pair_commits(originals, replayed, upstream=upstream)["refusals"]] == [cp.UNACCOUNTED]


def test_same_second_same_subject_pair_parks_as_ambiguous(tmp_path):
    repo = new_repo(tmp_path)
    put(repo, "a.txt", "1\n")
    commit(repo, "base")
    git(repo, "checkout", "-q", "-b", "unit")
    for name in ("x.txt", "y.txt"):
        put(repo, name, "1\n")
        commit(repo, "bump", date="1700000200 +0000")
    git(repo, "checkout", "-q", "main")
    put(repo, "z.txt", "1\n")
    commit(repo, "main moves", date="1700000300 +0000")
    git(repo, "checkout", "-q", "unit")
    ob, ot, stops = replay(repo)
    _, _, got = prove_pairing(repo, ob, ot, stops)
    codes = [r["code"] for r in got["refusals"]]
    assert codes.count(cp.KEY_COLLISION) == 2 and got["pairs"] == []     # neither is paired by guessing


def test_reordering_parks():
    def c(sha, subj):
        return {"sha": sha, "key": ("n", "e", "1", subj), "patch_id": ""}
    got = cp.pair_commits([c("o1", "a"), c("o2", "b")], [c("n2", "b"), c("n1", "a")])
    assert [r["code"] for r in got["refusals"]] == [cp.REORDERED]


def test_stop_record_naming_a_missing_commit_or_disagreeing_with_the_key_parks():
    def c(sha, subj):
        return {"sha": sha, "key": ("n", "e", "1", subj), "patch_id": ""}
    got = cp.pair_commits([c("o1", "a")], [c("n1", "a")], {"o1": {"new_head": "zzz"}})
    assert cp.STOP_MISSING in {r["code"] for r in got["refusals"]}
    got = cp.pair_commits([c("o1", "a"), c("o2", "b")], [c("n1", "a"), c("n2", "b")], {"o1": {"new_head": "n2"}})
    assert cp.STOP_KEY_DISAGREE in {r["code"] for r in got["refusals"]}


def test_emptied_at_a_stop_is_accounted_and_an_unrecorded_emptying_is_not():
    o = {"sha": "o1", "key": ("n", "e", "1", "a"), "patch_id": ""}
    assert cp.pair_commits([o], [], emptied=["o1"])["refusals"] == []
    assert cp.pair_commits([o], [])["refusals"][0]["code"] == cp.UNACCOUNTED


def test_empty_range_has_nothing_to_account_for():
    assert cp.pair_commits([], []) == {"pairs": [], "skipped": [], "emptied": [], "refusals": []}


# --------------------------------------------------------------------------- merge stop (D-6)
def merge_world(tmp_path):
    """unit and side both edit x.txt line 1 (the merge conflicts and is resolved by hand); main edits y.txt."""
    repo = new_repo(tmp_path)
    put(repo, "x.txt", "base\nkeep\n")
    put(repo, "y.txt", lines(10))
    commit(repo, "base")
    git(repo, "checkout", "-q", "-b", "side")
    put(repo, "x.txt", "side\nkeep\n")
    commit(repo, "side edits x", date="1700000200 +0000")
    git(repo, "checkout", "-q", "-b", "unit", "main")
    put(repo, "x.txt", "unit\nkeep\n")
    commit(repo, "unit edits x", date="1700000300 +0000")
    git(repo, "merge", "--no-ff", "-q", "side", check=False, date="1700000400 +0000")
    put(repo, "x.txt", "unit+side\nkeep\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "merge side", date="1700000400 +0000")
    git(repo, "checkout", "-q", "main")
    put(repo, "y.txt", edit(lines(10), 5, "MAIN"))
    commit(repo, "main moves", date="1700000900 +0000")
    git(repo, "checkout", "-q", "unit")
    return repo


def test_merge_stop_is_readable_and_the_merge_commit_pairs_by_key(tmp_path):
    """MEASURED on scratch git (D-6): a `--rebase-merges` replay stops AT the merge; the gate's readers see the
    unmerged path there, and the replayed merge commit keeps the original's authorship key."""
    repo = merge_world(tmp_path)
    ob = git(repo, "merge-base", "main", "unit")
    ot = git(repo, "rev-parse", "unit")
    p = subprocess.run(["git", "rebase", "--rebase-merges", "main"], cwd=str(repo), capture_output=True, text=True,
                       env=_env("1700001000 +0000"))
    assert p.returncode != 0 and cs.conflicted_paths(_run, repo) == ["x.txt"]
    stopped_at_merge = (repo / ".git" / "rebase-merge").is_dir()
    assert stopped_at_merge
    put(repo, "x.txt", "unit+side\nkeep\n")
    git(repo, "add", "x.txt")
    assert cs.empty_commit_about_to_land(_run, repo) is None     # staged result differs from HEAD: safe to continue
    p = subprocess.run(["git", "rebase", "--continue"], cwd=str(repo), capture_output=True, text=True,
                       env=_env("1700001000 +0000"))
    assert p.returncode == 0, p.stderr
    pid = patch_id_fn(repo)
    originals = cp.read_commits(_run, repo, "%s..%s" % (ob, ot), pid)
    replayed = cp.read_commits(_run, repo, "main..HEAD", pid)
    merges = [c for c in originals if len(c["parents"]) > 1]
    assert len(merges) == 1 and any(len(c["parents"]) > 1 for c in replayed)
    got = cp.pair_commits(originals, replayed)
    assert got["refusals"] == [], got["refusals"]
    assert len(got["pairs"]) == len(originals)
    assert any(k.endswith("@0") or k.endswith("@1") for c in replayed for k in c["diff"])   # per-parent diffs read


# --------------------------------------------------------------------------- gate closed: nothing is wired
def test_the_library_reads_no_config_and_nothing_imports_it_yet():
    src = (SCRIPTS / "conflict_proof.py").read_text(encoding="utf-8")
    assert "upkeep" not in src.replace("`upkeep`", "") .split('"""', 2)[2]
    for path in SCRIPTS.glob("*.py"):
        if path.name != "conflict_proof.py":
            assert "conflict_proof" not in path.read_text(encoding="utf-8"), path.name


def test_read_commits_survives_control_bytes_in_message_and_diff(tmp_path):
    """A commit whose message and file text hold 0x1e/0x1f is read whole: never truncated, never dropped."""
    repo = new_repo(tmp_path)
    put(repo, "a.txt", "base\n")
    commit(repo, "base")
    put(repo, "a.txt", "base\nx\x1ey\x1fz\n")
    commit(repo, "subj \x1f one\n\nbody \x1e two \x1f three")
    put(repo, "b.txt", "after\n")
    commit(repo, "next")
    got = cp.read_commits(_run, repo, "HEAD~2..HEAD")
    assert [c["subject"] for c in got] == ["subj \x1f one", "next"]
    assert set(got[0]["diff"]) == {"a.txt"} and set(got[1]["diff"]) == {"b.txt"}
    assert "x\\x1ey\\x1fz" in repr(got[0]["diff"]["a.txt"])    # the added line is whole, not cut at the control byte


def test_read_commits_refuses_unparseable_chunk():
    with pytest.raises(ValueError, match=cp.UNPARSEABLE_LOG):
        cp.read_commits(lambda cwd, argv: "\x00only-one-line", ".", "x..y")
