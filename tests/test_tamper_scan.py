"""Tests for skills/sigma-loop/scripts/test_trust.py (issue #1937).

Every fixture here is a STRING, not a repo, because `scan()` deliberately takes unified-diff text
rather than shelling out to git. That is what makes the issue's three named controls executable in
milliseconds instead of needing a scratch repo each.
"""
import pathlib
import importlib.util

S = pathlib.Path(__file__).parent.parent / "skills" / "sigma-loop" / "scripts"


def _tt():
    spec = importlib.util.spec_from_file_location("tamper_scan", S / "tamper_scan.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


DELETED_ASSERT = """\
diff --git a/tests/test_thing.py b/tests/test_thing.py
index 1111111..2222222 100644
--- a/tests/test_thing.py
+++ b/tests/test_thing.py
@@ -3,7 +3,6 @@ def test_answer():
     result = compute()
     assert result.status == "ok"
-    assert result.value == 42
     assert result.name
"""

ADDED_SKIP = """\
diff --git a/tests/test_thing.py b/tests/test_thing.py
index 1111111..2222222 100644
--- a/tests/test_thing.py
+++ b/tests/test_thing.py
@@ -1,3 +1,4 @@
+@pytest.mark.skip(reason="flaky")
 def test_answer():
     assert compute().value == 42
"""

INNOCENT_RENAME = """\
diff --git a/tests/test_thing.py b/tests/test_thing.py
index 1111111..2222222 100644
--- a/tests/test_thing.py
+++ b/tests/test_thing.py
@@ -1,3 +1,3 @@
-def test_answer():
+def test_computed_answer():
     assert compute().value == 42
"""

NEW_TEST_FILE = """\
diff --git a/tests/test_new.py b/tests/test_new.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/tests/test_new.py
@@ -0,0 +1,3 @@
+def test_thing():
+    assert compute() is not None
"""


def test_a_deleted_assertion_in_a_preexisting_test_is_flagged():
    """CONTROL 1 of the issue's three judged_when scenarios."""
    r = _tt().scan(DELETED_ASSERT)
    assert r["assertions_removed"] == 1
    assert r["tests"] == ["tests/test_thing.py"]
    assert r["clean"] is False


def test_an_added_skip_on_a_preexisting_test_is_flagged():
    """CONTROL 2."""
    r = _tt().scan(ADDED_SKIP)
    assert r["skips_added"] == 1
    assert r["clean"] is False


def test_an_innocent_rename_is_NOT_flagged():
    """CONTROL 3. The assertion is untouched; only the def line moved. A gate that flags this
    is a gate that gets switched off in a week."""
    r = _tt().scan(INNOCENT_RENAME)
    assert r["assertions_removed"] == 0
    assert r["skips_added"] == 0
    assert r["clean"] is True


def test_a_genuinely_new_test_file_does_not_trip_the_preexisting_path():
    """judged_when 2. A new file has no PRE-EXISTING assertions to remove -- but its weak
    assertion IS caught by the new-test lint, which is the other half of this goal."""
    r = _tt().scan(NEW_TEST_FILE)
    assert r["assertions_removed"] == 0
    assert r["skips_added"] == 0
    assert r["weak_new_assertions"] == 1


def test_a_moved_assertion_is_not_a_removal():
    """A line removed and re-added in the same file is a MOVE. Counting it as a removal is the
    obvious false positive -- re-indenting a test into a class would flag every assertion in it."""
    moved = DELETED_ASSERT + "+    assert result.value == 42\n"
    assert _tt().scan(moved)["assertions_removed"] == 0


def test_a_weakened_assertion_is_flagged_as_both_a_removal_and_a_weak_new_one():
    """`== 42` -> `is not None` is the exact weakening the issue names. It is one edit but two
    signals, and reporting both is what makes the record legible."""
    weakened = """\
diff --git a/tests/test_thing.py b/tests/test_thing.py
index 1111111..2222222 100644
--- a/tests/test_thing.py
+++ b/tests/test_thing.py
@@ -1,2 +1,2 @@
 def test_answer():
-    assert compute().value == 42
+    assert compute().value is not None
"""
    r = _tt().scan(weakened)
    assert r["assertions_removed"] == 1
    assert r["weak_new_assertions"] == 1


def test_deleting_a_whole_test_file_is_not_counted_as_removing_its_assertions():
    """PLAN-REVIEW F2. Every line of a deleted file appears as `-`, so without the deleted-file
    guard a legitimate "remove the obsolete suite" commit reports a dozen removals. Deleting a
    file outright is visible and reviewable; this gate is for SILENT erosion."""
    deleted = """\
diff --git a/tests/test_old.py b/tests/test_old.py
deleted file mode 100644
index 1111111..0000000
--- a/tests/test_old.py
+++ /dev/null
@@ -1,3 +0,0 @@
-def test_answer():
-    assert compute().value == 42
-    assert compute().name == "x"
"""
    assert _tt().scan(deleted)["assertions_removed"] == 0


def test_a_non_test_file_is_ignored_entirely():
    """Implementation files are not this gate's business -- an `assert` removed from production
    code is an ordinary code change, not test erosion."""
    impl = DELETED_ASSERT.replace("tests/test_thing.py", "src/thing.py")
    assert _tt().scan(impl)["clean"] is True


def test_a_path_that_merely_contains_the_word_test_is_not_a_test_file():
    """`contests/` and `mytest_helper.py` must not match -- both need `^` or `/` immediately
    before the token. Checked because a loose regex here silently widens the gate over the repo."""
    tt = _tt()
    for path in ("contests/test_thing.py".replace("test_thing", "thing"), "src/mytest_helper.py"):
        assert tt.scan(DELETED_ASSERT.replace("tests/test_thing.py", path))["clean"] is True


def test_malformed_input_yields_zeros_rather_than_raising():
    """A crashing gate is a gate someone removes."""
    for junk in ("", None, "not a diff at all", "diff --git\n@@ bad"):
        assert _tt().scan(junk)["clean"] is True


def test_no_source_module_under_skills_is_named_like_a_test_file():
    """THE ROOT-CAUSE GUARD, and it was written because this module used to violate it.

    A source module named `test_*.py` is read as a TEST by two independent things: pytest's own
    default discovery, and this module's `_TEST_PATH`. When it was called `test_trust.py`, the
    consequences were both real and both silent:

      * `changed_test_files()` listed it as a changed TEST, so #1933's flake check tried to re-run a
        source module three times;
      * `pytest --collect-only -q` over a set including it emitted malformed, path-less node ids
        (`::test_x`), which pytest then rejects as a usage error -- exit 4 on every run, and because
        all three runs agreed on that exit, the flake check reported `verified`. A guard reporting
        success while measuring nothing.

    Neither showed up in any unit test. Both appeared the moment the cost was actually measured on a
    real goal, which is why AGENTS.md requires the measurement rather than an estimate."""
    import pathlib
    scripts = pathlib.Path(__file__).parent.parent / "skills" / "sigma-loop" / "scripts"
    offenders = sorted(p.name for p in scripts.glob("test_*.py"))
    assert offenders == [], (
        "source modules named like test files: %s -- pytest will try to collect them and "
        "tamper_scan._TEST_PATH will classify them as tests" % offenders)

# ---------------------------------------------------------- #1933: which tests CHANGED (not: which look wrong)

DELETED_FILE = """\
diff --git a/tests/test_gone.py b/tests/test_gone.py
deleted file mode 100644
index 1111111..0000000
--- a/tests/test_gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def test_a():
-    assert compute() == 42
"""


def test_changed_test_files_lists_added_and_modified_test_files():
    tt = _tt()
    assert tt.changed_test_files(DELETED_ASSERT) == ["tests/test_thing.py"]
    assert tt.changed_test_files(NEW_TEST_FILE) == ["tests/test_new.py"]


def test_changed_test_files_ignores_implementation_files():
    assert _tt().changed_test_files(DELETED_ASSERT.replace("tests/test_thing.py", "src/x.py")) == []


def test_changed_test_files_reports_a_clean_test_edit_that_scan_does_not_flag():
    """THE DISTINCTION THAT MATTERS, and the reason this is a separate function rather than a reuse
    of scan()["tests"]. That list holds files where something was FLAGGED; this one holds files that
    CHANGED. An innocently renamed test is not suspicious, but it is still new code that has to
    prove it is deterministic before #1933 credits it. Conflating the two would silently skip the
    flake check on every clean test edit -- which is most of them."""
    tt = _tt()
    assert tt.scan(INNOCENT_RENAME)["tests"] == []
    assert tt.changed_test_files(INNOCENT_RENAME) == ["tests/test_thing.py"]


def test_changed_test_files_omits_a_deleted_test_file():
    """A deleted file has no tests left to run three times."""
    assert _tt().changed_test_files(DELETED_FILE) == []


def test_changed_test_files_dedupes_and_preserves_diff_order():
    tt = _tt()
    two = NEW_TEST_FILE + DELETED_ASSERT + NEW_TEST_FILE
    assert tt.changed_test_files(two) == ["tests/test_new.py", "tests/test_thing.py"]


# ------------------------------------------------------------------ #1999: monitor removals
def _diff(path, body, header=""):
    return ("diff --git a/%s b/%s\n%sindex 1111111..2222222 100644\n--- a/%s\n+++ b/%s\n%s"
            % (path, path, header, path, path, body))


STALENESS_BLOCK_DELETED = _diff("hooks/session_start.sh", (
    "@@ -10,6 +10,2 @@ main\n"
    " echo start\n"
    "-if watcher_is_stale; then\n"
    "-  echo 'watcher stale'\n"
    "-fi\n"
    " echo done\n"))
CI_STEP_DROPPED = _diff(".github/workflows/ci.yml", (
    "@@ -5,5 +5,3 @@ jobs\n"
    "     steps:\n"
    "-      - name: ratchet\n"
    "-        run: pytest tests/test_no_direct_gh.py\n"
    "       - run: pytest\n"))
ADDITIONS_ONLY = _diff("hooks/session_start.sh", (
    "@@ -1,1 +1,3 @@\n echo start\n+echo more\n+echo evenmore\n"))
NON_MONITOR_REMOVAL = _diff("skills/other/scripts/thing.py", (
    "@@ -1,3 +1,1 @@\n x = 1\n-y = 2\n-z = 3\n"))
MONITOR_FILE_DELETED = (
    "diff --git a/skills/sigma-loop/scripts/run_with_timeout.py "
    "b/skills/sigma-loop/scripts/run_with_timeout.py\ndeleted file mode 100644\n"
    "index 1111111..0000000\n--- a/skills/sigma-loop/scripts/run_with_timeout.py\n+++ /dev/null\n"
    "@@ -1,2 +0,0 @@\n-import os\n-import sys\n")


def test_monitor_a_deleted_staleness_block_in_the_session_hook_is_flagged():
    got = _tt().monitor_changes(STALENESS_BLOCK_DELETED)
    assert [f["path"] for f in got] == ["hooks/session_start.sh"]
    assert got[0]["removed"] == 3


def test_monitor_a_dropped_ci_workflow_step_is_flagged():
    got = _tt().monitor_changes(CI_STEP_DROPPED)
    assert [f["path"] for f in got] == [".github/workflows/ci.yml"]


def test_monitor_an_additions_only_diff_is_not_flagged():
    assert _tt().monitor_changes(ADDITIONS_ONLY) == []


def test_monitor_a_non_monitor_path_removal_is_not_flagged():
    assert _tt().monitor_changes(NON_MONITOR_REMOVAL) == []


def test_monitor_a_deleted_monitor_file_is_flagged_as_deleted():
    got = _tt().monitor_changes(MONITOR_FILE_DELETED)
    assert len(got) == 1 and got[0]["deleted"] is True


def test_monitor_an_emptied_file_deleted_with_no_hunk_lines_is_still_flagged():
    """The deleted-file clause on its own: no `-` lines exist, so only the marker can flag it."""
    empty = ("diff --git a/hooks/guard.sh b/hooks/guard.sh\ndeleted file mode 100644\n"
             "index 1111111..0000000\n")
    got = _tt().monitor_changes(empty)
    assert len(got) == 1 and got[0]["deleted"] is True and got[0]["removed"] == 0


def test_monitor_a_pure_move_within_a_monitor_file_is_not_a_removal():
    moved = _diff("hooks/session_start.sh",
                  "@@ -1,2 +1,2 @@\n-echo a\n-echo b\n+echo b\n+echo a\n")
    assert _tt().monitor_changes(moved) == []


def test_monitor_malformed_input_yields_nothing_rather_than_raising():
    m = _tt()
    assert m.monitor_changes("") == [] and m.monitor_changes(None) == []


def test_monitor_every_named_path_is_in_the_set_and_flagged_alone():
    m = _tt()
    for p in ("hooks/x.sh", ".github/workflows/ci.yml",
              "skills/sigma-loop/scripts/watch_daemon.py",
              "skills/sigma-loop/scripts/run_with_timeout.py",
              "skills/sigma-doctor/scripts/doctor.py",
              "tests/test_no_direct_gh.py", "tests/test_no_direct_gh_prose.py"):
        d = _diff(p, "@@ -1,2 +1,1 @@\n keep\n-gone\n")
        assert [f["path"] for f in m.monitor_changes(d)] == [p], p
