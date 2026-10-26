"""Diff-only test-tamper detection (issue #1937) -- the layer that guards the suite that ALREADY
existed.

Every other test-trust layer guards the NEW test: red-before-green records it, the mutation gate
scores it, the author-blind breaker writes it. None of them looks at the tests that were already
passing. That is the open door: an agent that cannot make the implementation satisfy the old test
can simply edit the old test, and every red-first record and mutation score for the NEW code stays
green while the regression protection that was there before is quietly gone. The signal is the
ABSENCE of something that used to be there, which is exactly the shape of defect this repo has been
bitten by before (a squash template dropping `(#N)` makes every landed commit unaccountable and
nothing announces it).

NO TEST EXECUTION, NO GIT, NO FILESYSTEM. `scan()` takes unified-diff TEXT and returns counts. That
is a deliberate decomposition, not an accident of implementation: it means the caller supplies the
one input every context already has (a gate, a hook, a CI step, a human with `git diff` in a
terminal), and it means every control in tests/test_test_trust.py is a string fixture rather than a
scratch repo.

WHY TEXT RATHER THAN AN AST OF BOTH REVISIONS. Parsing the old and new blobs with `ast` would be
more precise about "weakened", but it needs both revisions checked out and cannot run on a diff
alone. The precision that trades away is named below rather than hidden.

WHAT THIS DOES NOT CATCH, stated plainly because a guard that oversells itself is worse than none:
  - a tolerance widened inside a helper the test calls, rather than in the test body;
  - an assertion moved into a branch that never executes (an `if False:`, an unreachable `else`);
  - a test whose FIXTURE is weakened -- a narrower input, a stubbed collaborator -- while its
    assertions are untouched. This is probably the largest hole and it is not addressable by any
    line-level pass;
  - a `-` line inside a docstring or a string literal that merely looks like an `assert`;
  - a parametrised case removed from a `@pytest.mark.parametrize` list, which deletes real coverage
    without touching a single `assert` line.
It is a cheap, high-signal pass over the commonest deliberate weakenings, not a proof of
non-tampering.
"""
import re

#: A path counts as a test file if it looks like one to pytest's own default discovery. Anchored on
#: `^` or `/` so `contests/foo.py` and `src/mytest_helper.py` do NOT match -- a loose regex here
#: silently widens the gate across the whole repo, which is regression-covered.
_TEST_PATH = re.compile(r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.py$)")
_ASSERT = re.compile(r"^\s*assert\b")
_SKIP = re.compile(
    r"@pytest\.mark\.(skip|skipif|xfail)\b"
    r"|^\s*pytest\.skip\("
    r"|^\s*self\.skipTest\("
    r"|@unittest\.(skip|expectedFailure)\b"
)
#: A "weak" assertion asserts that something merely EXISTS, or asserts nothing at all. This is the
#: cheap pre-filter the mutation gate (#1935) can run behind -- catching `assert x is not None`
#: costs microseconds, where proving the same test kills no mutants costs minutes.
_WEAK = re.compile(
    r"^\s*assert\s+.*\bis\s+not\s+None\s*$"
    r"|^\s*assert\s+True\s*$"
    r"|^\s*assert\s+not\s+None\s*$"
    r"|^\s*except[^:]*:\s*pass\s*$"
)


def _files(diff_text):
    """[(path, is_wholesale, [+/- lines])] -- one entry per file section of a unified diff.

    `kind` is "new" for a file the diff CREATES, "deleted" for one it REMOVES outright, and None
    for an ordinary modification. scan() treats "new" and "deleted" identically -- neither has
    pre-existing assertions being quietly weakened -- but changed_test_files() must tell them
    apart, because a deleted file has no tests left to re-run.

    THE MARKER, NOT A HEURISTIC. An earlier version of changed_test_files() inferred deletion from
    "every hunk line is a removal", which is ALSO true of an ordinary edit whose only change is a
    deleted line -- exactly the DELETED_ASSERT case this module exists to catch, so it silently
    dropped the commonest tamper from the flake check's input. Caught by its own test.
    """
    out, path, kind, lines = [], None, None, []
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if path is not None:
                out.append((path, kind, lines))
            path, kind, lines = line.split(" b/", 1)[-1], None, []
        elif line.startswith("new file mode"):
            kind = "new"
        elif line.startswith("deleted file mode"):
            kind = "deleted"
        elif path is not None and line[:1] in ("+", "-") \
                and not line.startswith(("+++", "---")):
            lines.append(line)
    if path is not None:
        out.append((path, kind, lines))
    return out


def scan(diff_text):
    """Counts of test-weakening edits in `diff_text`.

    -> {"assertions_removed", "skips_added", "weak_new_assertions", "tests", "clean"}

    NEVER RAISES. Malformed or empty input yields zeros: a crashing gate is a gate someone
    removes, and this one is advisory by design (#1937 -- "Not a hard block on its own").
    """
    removed = skipped = weak = 0
    touched = []
    try:
        sections = _files(diff_text or "")
    except Exception:                       # noqa: BLE001 - see the NEVER RAISES contract above
        return {"assertions_removed": 0, "skips_added": 0, "weak_new_assertions": 0,
                "tests": [], "clean": True}
    for path, kind, lines in sections:
        if not _TEST_PATH.search(path):
            continue                        # implementation files are not this gate's business
        adds = [l[1:] for l in lines if l.startswith("+")]
        dels = [l[1:] for l in lines if l.startswith("-")]
        # A line removed and re-added in the same file is a MOVE, not a removal. Compared on
        # STRIPPED text, so a pure re-indent -- wrapping tests in a class, adding a `with` block --
        # is not read as deleting every assertion inside it. That is the commonest legitimate edit
        # and would otherwise be the commonest false positive.
        readded = {a.strip() for a in adds}
        file_removed = 0 if kind else sum(
            1 for d in dels if _ASSERT.search(d) and d.strip() not in readded)
        file_skipped = 0 if kind else sum(1 for a in adds if _SKIP.search(a))
        file_weak = sum(1 for a in adds if _WEAK.search(a))
        if file_removed or file_skipped or file_weak:
            touched.append(path)
        removed += file_removed
        skipped += file_skipped
        weak += file_weak
    return {"assertions_removed": removed, "skips_added": skipped,
            "weak_new_assertions": weak, "tests": touched,
            "clean": not (removed or skipped or weak)}


def changed_test_files(diff_text):
    """Every test file this diff ADDS or MODIFIES, in diff order, deduped (issue #1933).

    SEPARATE FROM `scan()["tests"]` ON PURPOSE, and the distinction is load-bearing. That list holds
    files where something was FLAGGED; this one holds files that CHANGED. An innocently renamed test
    is not suspicious, but it is still new code that has to prove it is deterministic before #1933
    credits it -- conflating the two would silently skip the flake check on every clean test edit,
    which is most of them.

    ONE PARSER, TWO QUESTIONS. This deliberately reuses `_files` and `_TEST_PATH` rather than
    introducing a second diff reader, so #1933, #1934 and #1935 all inherit the same answer to
    "which tests are in play" instead of three that are free to disagree.

    A DELETED file is excluded -- there is nothing left to run. Read off the `deleted file mode`
    MARKER, never inferred from "every hunk line is a removal": that inference is also true of an
    ordinary edit whose only change is a deleted line, which would have dropped the commonest
    tamper case out of the flake check's input entirely.
    """
    out = []
    for path, kind, _lines in _files(diff_text or ""):
        if not _TEST_PATH.search(path) or path in out:
            continue
        if kind == "deleted":
            continue                        # nothing left to run
        out.append(path)
    return out


#: #1999: the files that WATCH the system (hooks, CI, the watcher, the doctor, the ratchet baselines).
#: Entries ending `/` are directory prefixes, the rest are exact paths. A diff that deletes or
#: net-removes lines there can blind a monitor while every other check stays green. Advisory only.
MONITOR_PATHS = (
    "hooks/",
    ".github/workflows/",
    "skills/sigma-loop/scripts/watch_daemon.py",
    "skills/sigma-loop/scripts/run_with_timeout.py",
    "skills/sigma-doctor/scripts/doctor.py",
    "tests/test_no_direct_gh.py",
    "tests/test_no_direct_gh_prose.py",
)


def _is_monitor(path):
    return any(path.startswith(m) if m.endswith("/") else path == m for m in MONITOR_PATHS)


def monitor_changes(diff_text):
    """[{"path", "deleted", "removed"}] for monitor files a diff deletes or net-removes lines from.

    PURE: diff text in, findings out, no I/O. NEVER RAISES (bad input yields []). A line removed and
    re-added in the same file (stripped) is a move, not a removal, like scan(). Two clauses, each
    pinned alone: a DELETED file is flagged whatever its lines; otherwise net-removed lines > 0.
    Additions-only changes and non-monitor paths are never flagged."""
    try:
        sections = _files(diff_text or "")
    except Exception:                       # noqa: BLE001 - advisory; see NEVER RAISES
        return []
    out = []
    for path, kind, lines in sections:
        if not _is_monitor(path):
            continue
        readded = {l[1:].strip() for l in lines if l.startswith("+")}
        removed = sum(1 for l in lines if l.startswith("-") and l[1:].strip() not in readded)
        if kind == "deleted" or removed > 0:
            out.append({"path": path, "deleted": kind == "deleted", "removed": removed})
    return out
