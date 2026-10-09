"""Planned tests for #918 (upkeep part A, slice 2): the never-delete guard must see every shape that a
backup, restore or prune step will use, and one pinned sentence must state the guard's rule about the
backup-ref namespace.

How this file is built (it is bound by the red/green gate, so read this before touching it):

- It loads the guard module (`tests/test_no_autonomous_feature_branch_deletion.py`) by path and calls
  only that module's own helpers: `_delete_call_sites`, `_delete_shaped`, `_enclosing_function`, `ROOT`.
- Names that the change defines (`NEVER_DELETE_BACKUP_RULE`, `_files_carrying_the_rule_wording`,
  `_FILES_STATING_THE_RULE`) are read with `getattr(..., None)` inside each test body and then asserted,
  so collection never fails and a missing name is an assertion failure, never an exception.
- The planned tests are unparametrised: a table is looped inside one function and every failing id goes
  into one assert message. The parametrised tests are green-by-design controls (stay-quiet, known limit)
  and are not planned nodes.
- Tests print nothing, never skip, and capture the output of every child process, so the gate's
  traceback attribution stays switched on.
- After the red verify this file is READ-ONLY: the gate binds it byte for byte, and a changed byte voids
  the red that was recorded for it.
"""
import importlib.util
import itertools
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
GUARD_PATH = ROOT / "tests" / "test_no_autonomous_feature_branch_deletion.py"

_cache = []
_counter = itertools.count()


def _guard():
    """The guard module, loaded by path: lazy and cached, so collection stays cheap and side-effect free."""
    if not _cache:
        spec = importlib.util.spec_from_file_location("never_delete_guard_under_test", GUARD_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _cache.append(module)
    return _cache[0]


def _code(lines):
    """Lines go inside `def f(run, cwd, remote, name, sha, ref):`."""
    return "def f(run, cwd, remote, name, sha, ref):\n" + "".join("    %s\n" % l for l in lines)


def _tracked_tree(tmp_path, files):
    """{relative path: text}: `git init -q`, write, `git add -A`. No commit and no identity are needed
    because `git ls-files` reads the index. Every git call is captured and bounded."""
    def git(*args):
        subprocess.run(["git", *args], cwd=str(tmp_path), check=True,
                       capture_output=True, text=True, timeout=120)
    git("init", "-q")
    for rel, text in files.items():
        full = tmp_path / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text, encoding="utf-8")
    git("add", "-A")
    return tmp_path


def _planted_kinds(tmp_path, code, path="skills/x/scripts/y.py"):
    """The set of guard kinds found in one planted tracked file (a fresh sub-directory per case)."""
    sub = tmp_path / ("case%d" % next(_counter))
    sub.mkdir()
    _tracked_tree(sub, {path: code})
    hits = _guard()._delete_call_sites(sub)
    assert hits is not None, "not a git checkout -- cannot scan the planted tree"
    return {kind for _p, _n, kind, _l in hits}


def _docstring_code(line):
    return ("def f(run, cwd, remote, name, sha, ref):\n"
            '    """' + line + '"""\n'
            "    return 1\n")


def _comment_code(line):
    return ("def f(run, cwd, remote, name, sha, ref):\n"
            "    # " + line + "\n"
            "    return 1\n")


def _ids(rows):
    return [row[0] for row in rows]


def _rest(rows):
    return [row[1:] for row in rows]


def _second(rows):
    return [row[1] for row in rows]


# =====================================================================================================
# Registry tables (single source for the ids; each body is the lines of one planted function body)
# =====================================================================================================

#: id, body lines, the kind that must be present.
SHAPES = [
    ("push_short_delete_flag",
     ['run(cwd, ["git", "push", "-d", remote, name])'],
     "push_delete"),
    ("colon_empty_backup_ref",
     ['run(cwd, ["git", "push", remote, ":refs/sigma/backup/%s" % name])'],
     "colon_refspec"),
    ("backup_create_refspec",
     ['run(cwd, ["git", "push", remote, "%s:refs/sigma/backup/%s" % (sha, name)])'],
     "colon_refspec"),
    ("two_line_push_with_backup_refspec",
     ['run(cwd, ["git", "push", "--force-with-lease=%s:%s" % (ref, sha),',
      '          remote, "%s:refs/sigma/backup/%s" % (sha, name)])'],
     "colon_refspec"),
    ("rest_x_delete_backup_ref",
     ['run(cwd, ["gh", "api", "-X", "DELETE", "repos/o/r/git/refs/sigma/backup/%s" % name])'],
     "rest_delete_ref"),
    ("rest_method_delete_backup_ref",
     ['run(cwd, ["gh", "api", "--method", "DELETE", "repos/o/r/git/refs/sigma/backup/%s" % name])'],
     "rest_delete_ref"),
    ("rest_method_equals_delete_backup_ref",
     ['run(cwd, ["gh", "api", "--method=DELETE", "repos/o/r/git/refs/sigma/backup/%s" % name])'],
     "rest_delete_ref"),
    ("rest_attached_xdelete_backup_ref",
     ['run(cwd, ["gh", "api", "-XDELETE", "repos/o/r/git/refs/sigma/backup/%s" % name])'],
     "rest_delete_ref"),
    ("rest_verb_with_endpoint_on_another_line",
     ['args = ["api", endpoint,',
      '        "--method", "DELETE"]'],
     "rest_delete"),
    ("update_ref_short_delete",
     ['run(cwd, ["update-ref", "-d", ref])'],
     "update_ref_delete"),
    ("update_ref_long_delete",
     ['gitc(cwd, ["update-ref", "--delete", ref])'],
     "update_ref_delete"),
    ("update_ref_no_deref_delete",
     ['run(cwd, ["update-ref", "--no-deref", "-d", ref])'],
     "update_ref_delete"),
    ("gh_api_delete_helper",
     ['gh_api.delete_backup_ref(run, ref)'],
     "gh_api_delete"),
    ("gh_api_delete_helper_via_loader",
     ['_load("gh_api").delete_backup_ref(run, ref)'],
     "gh_api_delete"),
]

_SHAPE_BY_ID = {row[0]: row for row in SHAPES}

#: The five shapes that are also planted inside a docstring and after a hash sign.
DOC_COMMENT_IDS = ["push_short_delete_flag", "colon_empty_backup_ref", "rest_x_delete_backup_ref",
                   "update_ref_short_delete", "gh_api_delete_helper"]

#: id, call string; each must be truthy for `_delete_shaped`.
DELETE_SHAPED_POSITIVES = [
    ("ds_push_short_d", "git push origin -d topic"),
    ("ds_colon_empty_backup", "git push origin :refs/sigma/backup/x"),
    ("ds_update_ref_short_d", "git update-ref -d refs/x"),
    ("ds_update_ref_long_delete", "git update-ref --delete refs/x"),
    ("ds_update_ref_no_deref_d", "git update-ref --no-deref -d refs/x"),
    ("ds_push_long_delete", "git push origin --delete x"),
    ("ds_branch_D", "git branch -D x"),
    ("ds_rest_method_equals_delete", "gh api --method=DELETE x"),
]

#: id, body lines; the planted kind set must be empty.
STAY_QUIET = [
    ("push_to_head_colon_feature", ['run(cwd, ["git", "push", remote, "HEAD:feature"])']),
    ("push_set_upstream", ['run(cwd, ["git", "push", "-u", "origin", name])']),
    ("update_ref_non_delete", ['run(cwd, ["update-ref", "refs/heads/x", sha])']),
    ("rest_get", ['run(cwd, ["gh", "api", "--method", "GET", "repos/o/r/git/refs/heads/x"])']),
    ("curl_dash_d_no_push", ['run(cwd, ["curl", "-d", "x"])']),
    ("gh_api_view_pr", ['gh_api.view_pr(run, 1)']),
    ("gh_api_merge_pr", ['gh_api.merge_pr(run, 1)']),
]

#: id, call string; each must be falsy for `_delete_shaped`.
DELETE_SHAPED_NEGATIVES = [
    ("ds_neg_push_head_to_heads", "git push origin HEAD:refs/heads/x"),
    ("ds_neg_push_set_upstream", "git push -u origin x"),
    ("ds_neg_update_ref_non_delete", "git update-ref refs/heads/x abc"),
    ("ds_neg_push_backup_create", "git push origin abc:refs/sigma/backup/x"),
    ("ds_neg_rev_parse", "git rev-parse HEAD"),
    ("ds_neg_push_branch_ending_in_d", "git push origin feature-d"),
]

#: id, tracked path, body lines, the kind that must be ABSENT.
KNOWN_LIMITS = [
    ("push_and_short_flag_on_different_lines", "skills/x/scripts/y.py",
     ['run(cwd, ["git", "push",',
      '          "-d", remote, name])'],
     "push_delete"),
    ("update_ref_and_flag_on_different_lines", "skills/x/scripts/y.py",
     ['run(cwd, ["update-ref",',
      '          "-d", ref])'],
     "update_ref_delete"),
    ("dynamic_colon_empty_percent_format", "skills/x/scripts/y.py",
     ['run(cwd, ["git", "push", remote, ":%s" % ref])'],
     "colon_refspec"),
    ("dynamic_colon_empty_fstring", "skills/x/scripts/y.py",
     ['run(cwd, ["git", "push", remote, f":{ref}"])'],
     "colon_refspec"),
    ("gh_api_bound_to_a_variable", "skills/x/scripts/y.py",
     ['api = _load("gh_api")',
      'api.delete_backup_ref(run, ref)'],
     "gh_api_delete"),
    ("gh_api_bare_import_delete_helper", "skills/x/scripts/y.py",
     ['from gh_api import delete_backup_ref',
      'delete_backup_ref(run, ref)'],
     "gh_api_delete"),
    ("outside_skills_and_hooks", "tools/x.py",
     ['run(cwd, ["git", "push", "--delete", remote, name])'],
     "push_delete"),
]


# =====================================================================================================
# Task 1 -- the guard sees every deleting shape
# =====================================================================================================

def test_every_widened_delete_shape_is_caught_by_the_guard_scanner(tmp_path):
    misses = []
    for case_id, lines, kind in SHAPES:
        found = _planted_kinds(tmp_path, _code(lines))
        if kind not in found:
            misses.append((case_id, kind, sorted(found)))
    assert not misses, misses


def test_a_widened_shape_inside_a_docstring_or_comment_is_not_flagged(tmp_path):
    problems = []
    for case_id in DOC_COMMENT_IDS:
        _id, lines, kind = _SHAPE_BY_ID[case_id]
        (line,) = lines
        # As code the kind MUST be present: this is what makes the test red on an unwidened guard and
        # impossible to pass vacuously.
        present = kind in _planted_kinds(tmp_path, _code(lines))
        if not present:
            problems.append((case_id, "code", present))
        for form, code in (("docstring", _docstring_code(line)), ("comment", _comment_code(line))):
            present = kind in _planted_kinds(tmp_path, code)
            if present:
                problems.append((case_id, form, present))
    assert not problems, problems


def test_delete_shaped_helper_knows_every_widened_shape():
    helper = getattr(_guard(), "_delete_shaped", None)
    assert helper is not None, "_delete_shaped is not defined in the guard module"
    missed = [case_id for case_id, call in DELETE_SHAPED_POSITIVES if not helper(call)]
    assert not missed, missed


def test_the_real_tree_label_delete_helper_is_seen_as_a_rest_delete():
    guard = _guard()
    hits = guard._delete_call_sites(guard.ROOT)
    assert hits is not None, "not a git checkout"
    found = {(str(path.relative_to(guard.ROOT)), guard._enclosing_function(path, lineno), kind)
             for path, lineno, kind, _line in hits}
    wanted = ("skills/sigma-loop/scripts/gh_api.py", "remove_label", "rest_delete")
    assert wanted in found, (
        "the guard does not see the label delete in gh_api.py remove_label as a rest_delete site "
        "(the REST verb and its endpoint sit on different physical lines); found: %s"
        % sorted(found, key=str))


@pytest.mark.parametrize("lines", _second(STAY_QUIET), ids=_ids(STAY_QUIET))
def test_shapes_that_are_not_deletes_stay_quiet(tmp_path, lines):
    """Green by design (not a planned node): shapes next to the widened ones that delete nothing."""
    found = _planted_kinds(tmp_path, _code(lines))
    assert found == set(), sorted(found)


@pytest.mark.parametrize("call", _second(DELETE_SHAPED_NEGATIVES), ids=_ids(DELETE_SHAPED_NEGATIVES))
def test_delete_shaped_helper_stays_quiet_on_non_deletes(call):
    """Green by design (not a planned node): the behavioural helper must not call a non-delete a delete."""
    helper = getattr(_guard(), "_delete_shaped", None)
    assert helper is not None, "_delete_shaped is not defined in the guard module"
    assert not helper(call), call


@pytest.mark.parametrize("path, lines, kind", _rest(KNOWN_LIMITS), ids=_ids(KNOWN_LIMITS))
def test_known_limits_of_the_per_line_scan_are_pinned(tmp_path, path, lines, kind):
    """KNOWN LIMIT (green by design, not a planned node): the guard reads one physical line of a tracked
    `.py` file under `skills/` or `hooks/`. Each case below is a delete-shaped call the scan does NOT
    see: the pieces are split over two lines, the colon-empty refspec is built dynamically, the
    `gh_api` receiver is bound to a variable or imported by bare name, or the file sits outside the two
    scanned trees. Closing any of these gaps is a conscious edit of this test, not a side effect: the
    case must be deleted or inverted here in the same change that closes it."""
    found = _planted_kinds(tmp_path, _code(lines), path=path)
    assert kind not in found, (kind, sorted(found))


# =====================================================================================================
# Task 4 -- one pinned sentence and its copy discovery
# =====================================================================================================

#: The independent literal. The guard module holds the constant; this holds the pin. Nothing else in
#: the repository states the sentence.
_PINNED_RULE = (
    "The never-delete guard pins every delete-shaped call it can see under skills/ and hooks/; "
    "under refs/sigma/backup/ the only sanctioned automatic deletion is a prune by literal prefix."
)


def test_the_pinned_rule_wording_is_verbatim_and_portable():
    rule = getattr(_guard(), "NEVER_DELETE_BACKUP_RULE", None)
    assert rule == _PINNED_RULE, (
        "changing the rule wording means changing NEVER_DELETE_BACKUP_RULE and this pin together")
    assert rule.isascii(), "the rule wording must be ASCII"
    assert "\n" not in rule and "\r" not in rule, "the rule wording must be one physical line"
    assert rule.endswith("."), "the rule wording must end with a full stop"
    assert len(rule) <= 200, "the rule wording must stay at most 200 characters (%d)" % len(rule)


def test_no_tracked_file_states_the_rule_wording_unlisted():
    guard = _guard()
    finder = getattr(guard, "_files_carrying_the_rule_wording", None)
    assert finder is not None, "_files_carrying_the_rule_wording is not defined in the guard module"
    found = finder(guard.ROOT)
    assert found is not None, "not a git checkout -- cannot verify the real tree"
    listed = getattr(guard, "_FILES_STATING_THE_RULE", frozenset())
    stray = sorted(p for p in found if p not in listed)
    stale = sorted(p for p in listed if found.get(p) != "exact")
    assert not stray and not stale, (
        "a tracked file states the rule wording without being listed, or a listed file no longer "
        "carries it verbatim -- list a verbatim copy in _FILES_STATING_THE_RULE, or reword a variant "
        "to the constant; stray=%s stale=%s" % (stray, stale))


def test_the_rule_wording_finder_sees_planted_copies_and_variants_and_skips_excluded_paths(tmp_path):
    guard = _guard()
    finder = getattr(guard, "_files_carrying_the_rule_wording", None)
    assert finder is not None, "_files_carrying_the_rule_wording is not defined in the guard module"
    words = _PINNED_RULE.split()
    third = len(words) // 3
    wrapped = "\n".join("# " + " ".join(chunk)
                        for chunk in (words[:third], words[third:2 * third], words[2 * third:])) + "\n"
    variant = _PINNED_RULE.replace("only", "sole")
    assert variant != _PINNED_RULE, "the planted variant must differ from the sentence"
    tree = _tracked_tree(tmp_path, {
        "docs/x.md": wrapped,
        "docs/v.md": variant + "\n",
        "docs/other.md": ("Old backups are pruned by a literal prefix after thirty days, and the sweep "
                          "logs each removed name.\n"),
        ".sdlc/research/r.md": _PINNED_RULE + "\n",
        "tests/test_no_autonomous_feature_branch_deletion.py": _PINNED_RULE + "\n",
        "tests/test_never_delete_guard_backup_refs.py": _PINNED_RULE + "\n",
    })
    found = finder(tree)
    assert found == {"docs/x.md": "exact", "docs/v.md": "variant"}, found
