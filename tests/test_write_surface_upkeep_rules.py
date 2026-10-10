"""Planned tests for #918 (upkeep part A, slice 2): the write-surface scanner must class push-scoped
deletes, colon-empty and forced refspecs as destructive, see `update-ref` deletes and the REST verb
spellings, and see the `gh_api` write helpers by name.

How this file is built (it is bound by the red/green gate, so read this before touching it):

- It loads `tools/readiness/write_surface.py` by path, the way `tests/test_write_surface.py` does.
- A name that the change defines (`_GH_API_WRITES`) is read with `getattr(..., ())` inside the test
  body and then asserted, so collection never fails and a missing name is an assertion failure.
- Every ratchet case holds the function's existing `git-push` row (`held`), so a red is never red for
  the wrong reason: a brand-new push line would already be a new `git-push` site on the old scanner.
- The planned tests are unparametrised: a table is looped inside one function and every failing id goes
  into one assert message. The parametrised tests are green-by-design controls (stay-quiet, known
  limit, lookalike) and are not planned nodes.
- Tests print nothing, never skip, and capture the output of every child process, so the gate's
  traceback attribution stays switched on.
- After the red verify this file is READ-ONLY: the gate binds it byte for byte, and a changed byte voids
  the red that was recorded for it.
"""
import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "readiness" / "write_surface.py"
GH_API = ROOT / "skills" / "sigma-loop" / "scripts" / "gh_api.py"

PRUNE_DEF = "def prune(run, cwd, remote, ref, name, src, dst):"
LAND_DEF = "def land(run, cwd):"


def _module():
    spec = importlib.util.spec_from_file_location("write_surface", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def NEW(function, rule):
    """The literal ratchet message for a new site."""
    return ("new write site a.py:%s %s -- add it to docs/launch/write-surface.json with its gate"
            % (function, rule))


def _findings(module, tmp_path, source, held=()):
    (tmp_path / "a.py").write_text(source)
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps({"entries": [
        {"path": "a.py", "function": f, "rule": r, "count": 1, "gate": "test gate", "risk": "high"}
        for f, r in held]}))
    return module.ratchet(tmp_path, inventory)


def _prune(*lines):
    return PRUNE_DEF + "\n" + "".join("    %s\n" % l for l in lines)


def _land(*lines):
    return LAND_DEF + "\n" + "".join("    %s\n" % l for l in lines)


def _ids(rows):
    return [row[0] for row in rows]


def _rest(rows):
    return [row[1:] for row in rows]


def _second(rows):
    return [row[1] for row in rows]


def _shell_rules_of(module, tmp_path, line):
    path = tmp_path / "w.sh"
    path.write_text(line + "\n")
    return {row["rule"] for row in module.scan_paths(tmp_path, [path])}


# =====================================================================================================
# Registry tables
# =====================================================================================================

#: id, the one push call; every id must give exactly the git-destructive finding (the push row is held).
PUSH_SCOPED = [
    ("delete_flag", 'run(cwd, ["git", "push", remote, "--delete", name])'),
    ("delete_equals", 'run(cwd, ["git", "push", remote, "--delete=" + name])'),
    ("colon_empty_literal", 'run(cwd, ["git", "push", remote, ":refs/sigma/backup/x"])'),
    ("colon_empty_percent", 'run(cwd, ["git", "push", remote, ":%s" % ref])'),
    ("colon_empty_fstring", 'run(cwd, ["git", "push", remote, f":{ref}"])'),
    ("colon_empty_concat", 'run(cwd, ["git", "push", remote, ":" + ref])'),
    ("plus_refspec", 'run(cwd, ["git", "push", remote, "+HEAD:refs/heads/x"])'),
    ("short_force", 'run(cwd, ["git", "push", "-f", remote, name])'),
    ("force_if_includes", 'run(cwd, ["git", "push", "--force-if-includes", remote, name])'),
    ("mirror", 'run(cwd, ["git", "push", "--mirror", remote])'),
    ("prune_flag", 'run(cwd, ["git", "push", "--prune", remote])'),
    # Controls that already pass on the unwidened scanner.
    ("short_delete_flag_unchanged", 'run(cwd, ["git", "push", "-d", remote, name])'),
    ("long_force_unchanged", 'run(cwd, ["git", "push", "--force", remote, name])'),
]

#: id, the one call; every id must give exactly the git-destructive finding (nothing is held).
UPDATE_REF = [
    ("plain_run_short", 'run(cwd, ["update-ref", "-d", ref])'),
    ("plain_run_long", 'run(cwd, ["update-ref", "--delete", ref])'),
    ("no_deref", 'run(cwd, ["update-ref", "--no-deref", "-d", ref])'),
    ("git_token_long", 'run(cwd, ["git", "update-ref", "--delete", ref])'),
    ("runner_long", 'gitc(cwd, ["update-ref", "--delete", ref])'),
    # Controls that already pass on the unwidened scanner.
    ("git_token_short", 'run(cwd, ["git", "update-ref", "-d", ref])'),
    ("runner_short", 'gitc(cwd, ["update-ref", "-d", ref])'),
]

#: id, the one call; every id must give exactly the gh-api-write finding (nothing is held).
REST_SPELLINGS = [
    ("method_equals_delete", 'run(cwd, ["gh", "api", "--method=DELETE", "repos/o/r/git/refs/x"])'),
    ("attached_xdelete", 'run(cwd, ["gh", "api", "-XDELETE", "repos/o/r/git/refs/x"])'),
    # Controls that already pass on the unwidened scanner.
    ("x_delete", 'run(cwd, ["gh", "api", "-X", "DELETE", "x"])'),
    ("method_delete", 'run(cwd, ["gh", "api", "--method", "DELETE", "x"])'),
]

#: id, the one call inside `land`; every id must give exactly the gh-api-write finding.
GH_API_HELPERS = [
    ("comment_issue", 'gh_api.comment_issue(run, 1, "x")'),
    ("add_labels", 'gh_api.add_labels(run, 1, ["x"])'),
    ("remove_label", 'gh_api.remove_label(run, 1, "x")'),
    ("create_issue", 'gh_api.create_issue(run, "t", "b")'),
    ("close_issue", 'gh_api.close_issue(run, 1)'),
    ("create_pr", 'gh_api.create_pr(run, "t", "b", "h", "b")'),
    ("merge_pr", 'gh_api.merge_pr(run, 1)'),
    ("merge_pr_via_loader", '_load("gh_api").merge_pr(run, 1)'),
    ("comment_issue_via_loop_script_loader", '_load_loop_script("gh_api").comment_issue(run, 1, "x")'),
    ("create_pr_via_self_attribute", 'self.gh_api.create_pr(run, "t", "b", "h", "b")'),
]

#: id, one shell line, the exact rule set.
SHELL_PARITY = [
    ("sh_push_delete", "git push origin --delete topic", {"git-push", "git-destructive"}),
    ("sh_push_colon_empty_backup", "git push origin :refs/sigma/backup/x", {"git-push", "git-destructive"}),
    ("sh_push_plus_refspec", "git push origin +HEAD:main", {"git-push", "git-destructive"}),
    ("sh_push_short_d", "git push -d origin x", {"git-push", "git-destructive"}),
    ("sh_push_force_if_includes", "git push origin --force-if-includes topic",
     {"git-push", "git-destructive"}),
    ("sh_update_ref_short_d", "git update-ref -d refs/x", {"git-destructive"}),
    ("sh_update_ref_long_delete", "git update-ref --delete refs/x", {"git-destructive"}),
    ("sh_api_method_equals_delete", "gh api --method=DELETE x", {"gh-api-write"}),
    ("sh_api_attached_xdelete", "gh api -XDELETE x", {"gh-api-write"}),
    # Control that already passes on the unwidened scanner.
    ("sh_api_x_delete_separate", "gh api -X DELETE x", {"gh-api-write"}),
]

#: id, function, held rows, the one call; the ratchet must report nothing.
SCANNER_STAY_QUIET = [
    ("plain_heads_refspec", "prune", (("prune", "git-push"),),
     'run(cwd, ["git", "push", remote, "HEAD:refs/heads/x"])'),
    ("percent_format_heads_refspec", "prune", (("prune", "git-push"),),
     'run(cwd, ["git", "push", remote, "%s:refs/heads/%s" % (src, dst)])'),
    ("fstring_heads_refspec_trap", "prune", (("prune", "git-push"),),
     'run(cwd, ["git", "push", remote, f"{src}:refs/heads/{dst}"])'),
    ("set_upstream", "prune", (("prune", "git-push"),),
     'run(cwd, ["git", "push", "-u", "origin", name])'),
    ("plain_push", "prune", (("prune", "git-push"),),
     'run(cwd, ["git", "push", remote, name])'),
    ("fetch_plus_refspec_is_not_a_push", "prune", (),
     'run(cwd, ["git", "fetch", remote, "+refs/heads/*:refs/remotes/origin/*"])'),
    ("rest_method_equals_get", "prune", (),
     'run(cwd, ["gh", "api", "--method=GET", "x"])'),
    ("rest_attached_xget", "prune", (),
     'run(cwd, ["gh", "api", "-XGET", "x"])'),
    ("rest_x_get_separate", "prune", (),
     'run(cwd, ["gh", "api", "-X", "GET", "x"])'),
    ("gh_api_view_pr", "land", (),
     'gh_api.view_pr(run, 1)'),
    ("gh_api_graphql_available", "land", (),
     '_load_loop_script("gh_api").graphql_available(env={})'),
    ("bare_merge_pr_unrelated", "land", (),
     'merge_pr(run, cwd, 1)'),
    ("other_receiver_merge_pr", "land", (),
     'other.merge_pr(run, 1)'),
    ("gh_api_name_in_a_string", "land", (),
     'print("gh_api.merge_pr(run, 1)")'),
]

#: id, body lines inside `land`; the ratchet must report nothing.
GH_API_KNOWN_LIMITS = [
    ("gh_api_bound_to_a_variable_merge_pr", ['api = _load("gh_api")', 'api.merge_pr(run, 1)']),
    ("gh_api_bare_import_merge_pr", ['from gh_api import merge_pr', 'merge_pr(run, 1)']),
]

#: id, one shell line, the exact rule set.
SHELL_STAY_QUIET = [
    ("sh_q_push_plain", "git push origin topic", {"git-push"}),
    ("sh_q_push_heads", "git push origin HEAD:refs/heads/x", {"git-push"}),
    ("sh_q_update_ref_read_only_verify", "git rev-parse --verify refs/x", set()),
    ("sh_q_api_method_equals_get", "gh api --method=GET x", set()),
    ("sh_q_api_x_get", "gh api -X GET x", set()),
    ("sh_q_comment_line", "# git push origin --delete x", set()),
    ("sh_q_quoted_example", "printf '%s\\n' 'git push origin --delete x'", set()),
]

#: id, rule that must be absent, the call text that is only printed as a string.
LOOKALIKES = [
    ("lk_push_delete", "git-destructive", 'subprocess.run(["git", "push", "--delete", "x"])'),
    ("lk_update_ref_delete", "git-destructive", 'subprocess.run(["git", "update-ref", "--delete", "x"])'),
    ("lk_api_method_equals_delete", "gh-api-write", 'subprocess.run(["gh", "api", "--method=DELETE", "x"])'),
    ("lk_gh_api_merge_pr", "gh-api-write", 'gh_api.merge_pr(run, 1)'),
]


# =====================================================================================================
# Task 2 -- push-scoped deletes and forces, update-ref deletes, REST spellings, shell parity
# =====================================================================================================

def test_push_scoped_delete_and_force_shapes_are_destructive(tmp_path):
    module = _module()
    misses = []
    for case_id, call in PUSH_SCOPED:
        got = _findings(module, tmp_path, _prune(call), held=(("prune", "git-push"),))
        if got != [NEW("prune", "git-destructive")]:
            misses.append((case_id, got))
    assert not misses, misses


def test_update_ref_delete_forms_are_destructive(tmp_path):
    module = _module()
    misses = []
    for case_id, call in UPDATE_REF:
        got = _findings(module, tmp_path, _prune(call))
        if got != [NEW("prune", "git-destructive")]:
            misses.append((case_id, got))
    assert not misses, misses


def test_rest_delete_spellings_are_gh_api_writes(tmp_path):
    module = _module()
    misses = []
    for case_id, call in REST_SPELLINGS:
        got = _findings(module, tmp_path, _prune(call))
        if got != [NEW("prune", "gh-api-write")]:
            misses.append((case_id, got))
    assert not misses, misses


def test_shell_parity_for_push_scoped_deletes_update_ref_and_rest_spellings(tmp_path):
    module = _module()
    misses = []
    for case_id, line, expected in SHELL_PARITY:
        got = _shell_rules_of(module, tmp_path, line)
        if got != expected:
            misses.append((case_id, sorted(got), sorted(expected)))
    assert not misses, misses


def test_documented_check_gesture_fails_for_a_push_that_gained_a_delete(tmp_path):
    def git(*args):
        subprocess.run(["git", *args], cwd=str(tmp_path), check=True,
                       capture_output=True, text=True, timeout=120)

    def check():
        return subprocess.run([sys.executable, str(SCRIPT), "check", ".", "inventory.json"],
                              cwd=str(tmp_path), capture_output=True, text=True, timeout=120)

    git("init", "-q")
    source = tmp_path / "a.py"
    source.write_text(_prune('run(cwd, ["git", "push", remote, name])'))
    git("add", "a.py")
    (tmp_path / "inventory.json").write_text(json.dumps({"entries": [
        {"path": "a.py", "function": "prune", "rule": "git-push", "count": 1,
         "gate": "test gate", "risk": "high"}]}))

    first = check()
    assert first.returncode == 0, (first.returncode, first.stdout, first.stderr)
    assert first.stdout.strip() == "", first.stdout

    # The scanner reads tracked names from the index and contents from disk: no second `git add`.
    source.write_text(_prune('run(cwd, ["git", "push", "--delete", remote, name])'))
    second = check()
    assert second.returncode == 1, (second.returncode, second.stdout, second.stderr)
    assert "new write site a.py:prune git-destructive -- " in second.stdout, second.stdout


@pytest.mark.parametrize("function, held, call", _rest(SCANNER_STAY_QUIET), ids=_ids(SCANNER_STAY_QUIET))
def test_scanner_stay_quiet_cases(tmp_path, function, held, call):
    """Green by design (not a planned node): lookalikes of the widened rules that must not be sites."""
    source = _prune(call) if function == "prune" else _land(call)
    assert _findings(_module(), tmp_path, source, held=held) == []


@pytest.mark.parametrize("line, expected", _rest(SHELL_STAY_QUIET), ids=_ids(SHELL_STAY_QUIET))
def test_shell_shapes_that_must_stay_quiet(tmp_path, line, expected):
    """Green by design (not a planned node): shell lines the widened rules must leave alone."""
    assert _shell_rules_of(_module(), tmp_path, line) == expected


@pytest.mark.parametrize("rule, text", _rest(LOOKALIKES), ids=_ids(LOOKALIKES))
def test_new_rules_ignore_nonexecuting_lookalikes(tmp_path, rule, text):
    """Green by design (not a planned node): the call text is only printed as a string."""
    source = tmp_path / "lookalike.py"
    source.write_text("def explain():\n    print(" + repr(text) + ")\n")
    assert rule not in {row["rule"] for row in _module().scan_paths(tmp_path, [source])}


# =====================================================================================================
# Task 3 -- gh_api write helpers by name
# =====================================================================================================

def test_gh_api_write_helpers_are_seen_by_name(tmp_path):
    module = _module()
    misses = []
    for case_id, call in GH_API_HELPERS:
        got = _findings(module, tmp_path, _land(call))
        if got != [NEW("land", "gh-api-write")]:
            misses.append((case_id, got))
    assert not misses, misses


def test_gh_api_write_helper_names_cover_every_non_get_helper_in_the_module():
    tree = ast.parse(GH_API.read_text(encoding="utf-8"))
    helpers = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.List):
                continue
            for first, second in zip(sub.elts, sub.elts[1:]):
                if (isinstance(first, ast.Constant) and first.value == "--method"
                        and isinstance(second, ast.Constant) and isinstance(second.value, str)
                        and second.value != "GET"):
                    helpers.add(node.name)
    assert helpers, "found no non-GET helper in gh_api.py -- the drift check would be vacuous"
    declared = set(getattr(_module(), "_GH_API_WRITES", ()))
    assert helpers == declared, (
        "the scanner's gh_api write names differ from the non-GET helpers of gh_api.py: "
        "helpers=%s declared=%s" % (sorted(helpers), sorted(declared)))


@pytest.mark.parametrize("lines", _second(GH_API_KNOWN_LIMITS), ids=_ids(GH_API_KNOWN_LIMITS))
def test_gh_api_receiver_known_limits_are_pinned(tmp_path, lines):
    """KNOWN LIMIT (green by design, not a planned node): the scanner matches a `gh_api` write helper by
    its call receiver (a name or attribute ending in `gh_api`, or a loader call naming `"gh_api"`). A
    receiver bound to a variable first, and a helper imported by bare name, are NOT seen. Closing
    either gap is a conscious edit of this test, not a side effect: the case must be deleted or
    inverted here in the same change that closes it."""
    assert _findings(_module(), tmp_path, _land(*lines)) == []


# =====================================================================================================
# #960 -- a plain update-ref creates or moves a ref: it is a ref write, not a quiet lookalike
# =====================================================================================================

#: id, the one call; every id must give exactly the git-ref-write finding (nothing is held).
UPDATE_REF_WRITES = [
    ("plain_run", 'run(cwd, ["update-ref", "refs/heads/x", "abc"])'),
    ("git_token", 'run(cwd, ["git", "update-ref", "refs/sigma/backup/x", "abc"])'),
    ("runner", 'gitc(cwd, ["update-ref", "refs/sigma/backup/x", "abc"])'),
    ("with_message", 'run(cwd, ["update-ref", "-m", "why", "refs/x", "abc"])'),
    ("no_deref", 'run(cwd, ["update-ref", "--no-deref", "refs/x", "abc"])'),
    ("create_only", 'run(cwd, ["update-ref", "refs/x", "abc", ""])'),
]

#: id, the one call; delete forms keep git-destructive and must NOT also be a ref write.
UPDATE_REF_DELETES = [
    ("short_d", 'run(cwd, ["update-ref", "-d", ref])'),
    ("long_delete", 'gitc(cwd, ["git", "update-ref", "--delete", ref])'),
]


def test_plain_update_ref_is_a_ref_write(tmp_path):
    module = _module()
    misses = []
    for case_id, call in UPDATE_REF_WRITES:
        got = _findings(module, tmp_path, _prune(call))
        if got != [NEW("prune", "git-ref-write")]:
            misses.append((case_id, got))
    assert not misses, misses


def test_update_ref_deletes_are_not_also_ref_writes(tmp_path):
    module = _module()
    for case_id, call in UPDATE_REF_DELETES:
        got = _findings(module, tmp_path, _prune(call))
        assert got == [NEW("prune", "git-destructive")], (case_id, got)


def test_shell_plain_update_ref_is_a_ref_write(tmp_path):
    module = _module()
    assert _shell_rules_of(module, tmp_path, "git update-ref refs/x abc") == {"git-ref-write"}
    assert _shell_rules_of(module, tmp_path, "git update-ref -m why refs/x abc") == {"git-ref-write"}
    assert _shell_rules_of(module, tmp_path, "git update-ref -d refs/x") == {"git-destructive"}


def test_ref_write_has_a_risk_class():
    assert _module().RISK["git-ref-write"] == "high"
