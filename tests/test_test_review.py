"""#1964: independent test review blocks the Review phase.

Every test first asserts the piece it exercises exists, so a missing module or name reads as an
AssertionError (a red `red_green.py` can attribute), never an ImportError or TypeError.

The blocking clauses of `review_tests.py check` are driven through the documented gesture -- the
CLI, in a subprocess -- so a control that breaks one clause is seen red through that gesture.
"""
import importlib.util
import inspect
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

from test_work import (ON, _comment_reply, _posted, _review_chain, _sdlc, _started,
                       prfake, work)

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
GOAL = "0001-x.md"
A, M = "tests/test_x.py::test_a", "tests/test_x.py::TestK::test_m"
GOOD = ("TEST-MAP AC-1: %s\nTEST-MAP AC-2: %s\nTEST-MAP AC-3: UNTESTABLE: a README sentence\n\n"
        "VERDICT: approve\n" % (A, M))
BAD = "TEST-MAP AC-1: %s\nTEST-MAP AC-3: NONE\n\nVERDICT: approve\n" % A
MEASURED = {"test_first": {"passed": True, "nodes": {A: "h"}, "error": ""},
            "flake": {"verdict": "verified"},
            "diff_revert": {"verdict": "verified", "kills": [A], "survivors": [M],
                            "changed_survivors": [], "production_changed": True}}


def _load(name):
    path = SCRIPTS / (name + ".py")
    assert path.is_file(), "%s is not built yet" % path.name
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rt():
    return _load("review_tests")


def _repo(tmp_path, gate=None, criteria=("first", "second", "third"), evidence=None):
    """A drop-in project: `.sdlc` with config, an acceptance record and (optionally) verify evidence,
    plus one real test file the map can name."""
    d = tmp_path / ".sdlc"
    (d / "state").mkdir(parents=True)
    (d / "config.json").write_text(json.dumps({} if gate is None else {"review": {"test_review": gate}}))
    (d / "acceptance").mkdir()
    (d / "acceptance" / "0001-x.md").write_text(
        '---\nkind: "acceptance"\ngoal: "0001-x"\nverify_command: null\n---\n## Done when\n'
        + "".join("- [ ] %s\n" % c for c in criteria))
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(
        "def test_a():\n    assert True\n\n\nclass TestK:\n    def test_m(self):\n        assert True\n")
    if evidence is not None:
        _evidence(str(d), evidence)
    return str(d)


def _evidence(d, data):
    path = _load("state").evidence_path(d, GOAL)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data))


def _check(tmp_path, d, text):
    _rt()
    review = tmp_path / "review.txt"
    review.write_text(text)
    return subprocess.run([sys.executable, str(SCRIPTS / "review_tests.py"), "check", d, GOAL,
                           "--review", str(review)], capture_output=True, text=True)


def _last(proc):
    return proc.stdout.strip().splitlines()[-1]


# ---------------------------------------------------------------- criterion ids (#535's legacy rule)

def test_ids_are_positional_unless_explicit(tmp_path):
    d = _repo(tmp_path, criteria=("AC-7: explicit one", "plain two", "plain three"))
    assert _rt().criteria(d, GOAL) == [("AC-7", "explicit one"), ("AC-2", "plain two"),
                                        ("AC-3", "plain three")]


def test_a_duplicate_id_blocks(tmp_path):
    d = _repo(tmp_path, criteria=("x", "AC-1: y", "z"), evidence=MEASURED)
    p = _check(tmp_path, d, GOOD)
    assert p.returncode == 1 and "duplicate criterion id AC-1" in p.stdout, p.stdout + p.stderr


# ---------------------------------------------------------------- the criterion -> test clause

def test_a_complete_map_passes(tmp_path):
    p = _check(tmp_path, _repo(tmp_path, evidence=MEASURED), GOOD)
    assert p.returncode == 0 and _last(p) == "TEST-REVIEW: pass", p.stdout + p.stderr


def test_a_missing_line_blocks(tmp_path):
    p = _check(tmp_path, _repo(tmp_path, evidence=MEASURED), GOOD.replace("TEST-MAP AC-2: %s\n" % M, ""))
    assert p.returncode == 1 and "AC-2" in p.stdout and _last(p) == "TEST-REVIEW: block", p.stdout


def test_none_blocks(tmp_path):
    p = _check(tmp_path, _repo(tmp_path, evidence=MEASURED), GOOD.replace("AC-2: %s" % M, "AC-2: NONE"))
    assert p.returncode == 1 and "AC-2" in p.stdout, p.stdout


def test_a_missing_test_file_blocks(tmp_path):
    p = _check(tmp_path, _repo(tmp_path, evidence=MEASURED), GOOD.replace(A, "tests/test_nope.py::test_a"))
    assert p.returncode == 1 and "AC-1" in p.stdout, p.stdout


def test_a_missing_test_function_blocks(tmp_path):
    p = _check(tmp_path, _repo(tmp_path, evidence=MEASURED), GOOD.replace(A, "tests/test_x.py::test_zz"))
    assert p.returncode == 1 and "AC-1" in p.stdout, p.stdout


def test_a_method_is_scoped_to_its_class(tmp_path):
    d = _repo(tmp_path, evidence=MEASURED)
    for wrong in ("tests/test_x.py::test_m", "tests/test_x.py::TestK::test_a"):
        p = _check(tmp_path, d, GOOD.replace("AC-2: %s" % M, "AC-2: %s" % wrong))
        assert p.returncode == 1 and "AC-2" in p.stdout, (wrong, p.stdout)


def test_a_parametrized_id_exists(tmp_path):
    p = _check(tmp_path, _repo(tmp_path, evidence=MEASURED), GOOD.replace(A, A + "[case-1]"))
    assert p.returncode == 0, p.stdout


def test_one_real_test_among_missing_ones_is_enough(tmp_path):
    p = _check(tmp_path, _repo(tmp_path, evidence=MEASURED), GOOD.replace(A, A + ", tests/test_x.py::test_zz"))
    assert p.returncode == 0, p.stdout


def test_none_with_an_explanation_still_blocks(tmp_path):
    """P6 review B1: an explanation after NONE that names a real test must not read as a mapping."""
    text = GOOD.replace("AC-1: %s" % A, "AC-1: NONE - %s only exercises the happy path" % A)
    text = text.replace("AC-2: %s" % M, "AC-2: NONE, see tests/test_x.py")
    p = _check(tmp_path, _repo(tmp_path, evidence=MEASURED), text)
    assert p.returncode == 1 and "AC-1" in p.stdout and "AC-2" in p.stdout, p.stdout


def test_an_empty_line_does_not_swallow_the_next(tmp_path):
    """P6 review B2: `TEST-MAP AC-1:` with nothing after it is no mapping, not the next line's."""
    text = "TEST-MAP AC-1:\nTEST-MAP AC-2: %s\nTEST-MAP AC-3: UNTESTABLE: docs only\n" % M
    p = _check(tmp_path, _repo(tmp_path, evidence=MEASURED), text)
    assert p.returncode == 1 and "AC-1" in p.stdout, p.stdout


def test_a_file_id_must_be_a_test_file(tmp_path):
    """P6 review F2: an id with no `::` only needs its file, so that file must at least be a test."""
    d = _repo(tmp_path, evidence=MEASURED)
    (tmp_path / "README.md").write_text("docs\n")
    p = _check(tmp_path, d, GOOD.replace(A, "README.md"))
    assert p.returncode == 1 and "AC-1" in p.stdout, p.stdout
    assert _check(tmp_path, d, GOOD.replace(A, "tests/test_x.py")).returncode == 0


def test_untestable_needs_a_reason(tmp_path):
    p = _check(tmp_path, _repo(tmp_path, evidence=MEASURED),
               GOOD.replace("UNTESTABLE: a README sentence", "UNTESTABLE:"))
    assert p.returncode == 1 and "AC-3" in p.stdout, p.stdout


def test_conflicting_lines_block(tmp_path):
    p = _check(tmp_path, _repo(tmp_path, evidence=MEASURED), GOOD + "TEST-MAP AC-1: NONE\n")
    assert p.returncode == 1 and "conflicting" in p.stdout and "AC-1" in p.stdout, p.stdout


def test_markdown_decoration_is_tolerated(tmp_path):
    text = ("- `TEST-MAP AC-1: %s`\n* **TEST-MAP AC-2:** `%s`\n> TEST-MAP AC-3: UNTESTABLE: docs only\n"
            % (A, M))
    p = _check(tmp_path, _repo(tmp_path, evidence=MEASURED), text)
    assert p.returncode == 0, p.stdout


def test_missing_acceptance_blocks(tmp_path):
    d = _repo(tmp_path, evidence=MEASURED)
    (pathlib.Path(d) / "acceptance" / "0001-x.md").unlink()
    p = _check(tmp_path, d, GOOD)
    assert p.returncode == 1 and _last(p) == "TEST-REVIEW: block", p.stdout
    assert str(tmp_path) not in p.stdout, "a finding can reach a public PR comment: no absolute path"


def test_usage_exits_2():
    _rt()
    p = subprocess.run([sys.executable, str(SCRIPTS / "review_tests.py")], capture_output=True, text=True)
    assert p.returncode == 2, p.stdout + p.stderr


# ---------------------------------------------------------------- the changed-line survivor clause

def _with_changed(survivor):
    ev = json.loads(json.dumps(MEASURED))
    ev["diff_revert"]["changed_survivors"] = [survivor]
    return ev


def test_a_changed_line_survivor_blocks(tmp_path):
    p = _check(tmp_path, _repo(tmp_path, evidence=_with_changed(M)), GOOD)
    assert p.returncode == 1 and M in p.stdout and _last(p) == "TEST-REVIEW: block", p.stdout


def test_survivor_ok_with_a_reason_clears_it(tmp_path):
    text = GOOD + "SURVIVOR-OK %s: pins unchanged behaviour the goal relies on\n" % M
    p = _check(tmp_path, _repo(tmp_path, evidence=_with_changed(M)), text)
    assert p.returncode == 0, p.stdout


def test_survivor_ok_without_a_reason_does_not(tmp_path):
    p = _check(tmp_path, _repo(tmp_path, evidence=_with_changed(M)), GOOD + "SURVIVOR-OK %s:\n" % M)
    assert p.returncode == 1 and M in p.stdout, p.stdout


def test_no_evidence_is_partial_never_pass(tmp_path):
    p = _check(tmp_path, _repo(tmp_path), GOOD)
    assert p.returncode == 0 and _last(p).startswith("TEST-REVIEW: partial"), p.stdout


def test_an_absent_diff_revert_is_partial(tmp_path):
    ev = json.loads(json.dumps(MEASURED))
    ev["diff_revert"] = {"verdict": "absent", "kills": [], "survivors": [], "reason": "scope bound"}
    p = _check(tmp_path, _repo(tmp_path, evidence=ev), GOOD)
    assert p.returncode == 0 and _last(p).startswith("TEST-REVIEW: partial") and "scope bound" in p.stdout


def test_evidence_without_the_field_is_partial(tmp_path):
    ev = json.loads(json.dumps(MEASURED))
    del ev["diff_revert"]["changed_survivors"]
    p = _check(tmp_path, _repo(tmp_path, evidence=ev), GOOD)
    assert p.returncode == 0 and _last(p).startswith("TEST-REVIEW: partial"), p.stdout


# ---------------------------------------------------------------- the opt-in refusal

def test_refusal_is_off_unless_the_key_is_true(tmp_path):
    for i, gate in enumerate((None, False, "yes")):
        d = _repo(tmp_path / str(i), gate=gate, evidence=MEASURED)
        assert _rt().refusal(d, GOAL, BAD) is None, gate


def test_refusal_names_the_findings_when_on(tmp_path):
    out = _rt().refusal(_repo(tmp_path, gate=True, evidence=MEASURED), GOAL, BAD)
    assert out and "AC-2" in out


def test_refusal_on_an_unreadable_config(tmp_path):
    d = _repo(tmp_path, gate=True, evidence=MEASURED)
    (pathlib.Path(d) / "config.json").write_text("{not json")
    assert _rt().refusal(d, GOAL, GOOD)


def test_refusal_without_review_text_names_the_flag(tmp_path):
    out = _rt().refusal(_repo(tmp_path, gate=True, evidence=MEASURED), GOAL, None)
    assert out and "--review-output" in out


# ---------------------------------------------------------------- changed-line survivors at verify

def _git(cwd, *argv):
    return subprocess.run(["git", *argv], cwd=cwd, capture_output=True, text=True, check=True).stdout


def test_changed_survivors_scopes_to_changed_functions(tmp_path):
    dr = _load("diff_revert")
    assert hasattr(dr, "changed_survivors"), "diff_revert.changed_survivors is not built yet"
    root = tmp_path
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "tests").mkdir()
    src = ("import pytest\n\n\n"
           "def test_keep():\n    assert 1\n\n\n"
           "def test_edit():\n    assert 1\n\n\n"
           "@pytest.mark.slow\ndef test_deco():\n    assert 1\n\n\n"
           "def test_del():\n    assert 1\n    assert 2\n\n\n"
           "class TestC:\n    def test_meth(self):\n        assert 1\n")
    (root / "tests" / "test_s.py").write_text(src)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    fork = _git(root, "rev-parse", "HEAD").strip()
    (root / "tests" / "test_s.py").write_text(
        src.replace("def test_edit():\n    assert 1", "def test_edit():\n    assert 1 == 1")
           .replace("@pytest.mark.slow", "@pytest.mark.fast")
           .replace("    assert 1\n    assert 2\n", "    assert 1\n")
           .replace("        assert 1\n", "        assert 1 == 1\n"))
    (root / "tests" / "test_new.py").write_text("def test_fresh():\n    assert True\n")
    ids = ["tests/test_s.py::" + n for n in ("test_keep", "test_edit", "test_deco", "test_del",
                                            "TestC::test_meth")] + ["tests/test_new.py::test_fresh"]
    got = dr.changed_survivors(str(root), fork, ids)
    assert set(got) == set(ids) - {"tests/test_s.py::test_keep"}, got


def _fork_repo(root, files):
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return _git(root, "rev-parse", "HEAD").strip()


def test_a_deletion_is_judged_on_the_fork_side(tmp_path):
    """P6 re-review bug 1: deleting a whole test must not mark its untouched neighbour; deleting a
    decorator must still mark the test it decorated."""
    dr = _load("diff_revert")
    assert hasattr(dr, "changed_survivors"), "diff_revert.changed_survivors is not built yet"
    src = ("import pytest\n\n\n"
           "@pytest.mark.slow\ndef test_deco():\n    assert 1\n\n\n"
           "def test_a():\n    assert 1\n\n\ndef test_old():\n    assert 1\n\n\ndef test_b():\n    assert 1\n")
    fork = _fork_repo(tmp_path, {"tests/test_s.py": src})
    (tmp_path / "tests" / "test_s.py").write_text(
        src.replace("def test_old():\n    assert 1\n\n\n", "").replace("@pytest.mark.slow\n", ""))
    ids = ["tests/test_s.py::" + n for n in ("test_a", "test_deco", "test_b")]
    assert dr.changed_survivors(str(tmp_path), fork, ids) == ["tests/test_s.py::test_deco"]


def test_changed_paths_ignore_git_warnings_and_quoting(tmp_path):
    """P6 re-review bug 2: git's stderr (an autocrlf warning) and its C-quoting of non-ASCII names
    must not reach the path list `production_changed` reads."""
    dr = _load("diff_revert")
    assert hasattr(dr, "changed_paths"), "diff_revert.changed_paths is not built yet"
    fork = _fork_repo(tmp_path, {"tests/test_s.py": "def test_a():\n    assert 1\n"})
    _git(tmp_path, "config", "core.autocrlf", "true")
    (tmp_path / "tests" / "test_s.py").write_text("def test_a():\n    assert 1 == 1\n")
    (tmp_path / "tests" / "test_\u00e9t\u00e9.py").write_text("def test_b():\n    assert 1\n")
    paths = dr.changed_paths(str(tmp_path), fork)
    assert sorted(paths) == ["tests/test_s.py", "tests/test_\u00e9t\u00e9.py"], paths
    assert dr.production_changed(paths, ".sdlc") is False


def test_production_changed_ignores_tests_and_sdlc_records():
    dr = _load("diff_revert")
    assert hasattr(dr, "production_changed"), "diff_revert.production_changed is not built yet"
    assert dr.production_changed(["tests/test_a.py"], ".sdlc") is False
    assert dr.production_changed(["tests/test_a.py", ".sdlc/research/1.md"], ".sdlc") is False
    assert dr.production_changed(["tests/test_a.py", "src/x.py"], ".sdlc") is True
    assert dr.production_changed(["README.md"], ".sdlc") is True


def test_verify_records_changed_survivors_end_to_end():
    """loop.py's own glue: a goal that edits an existing test whose file stops collecting at the fork
    (it is not a survivor: it never stayed green) and adds a new test that passes with or without
    the change (a survivor on changed lines)."""
    loop = _load("loop")
    d = tempfile.mkdtemp()
    try:
        def run(*argv):
            return subprocess.run(argv, cwd=d, capture_output=True, text=True)
        run("git", "init", "-q")
        run("git", "checkout", "-q", "-b", "main")
        run("git", "config", "user.email", "t@example.com")
        run("git", "config", "user.name", "t")
        root = pathlib.Path(d)
        (root / "pkg").mkdir()
        (root / "pkg" / "__init__.py").write_text("")
        (root / "pkg" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (root / "tests").mkdir()
        (root / "tests" / "test_calc.py").write_text(
            "from pkg.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n")
        run("git", "add", "-A")
        run("git", "commit", "-q", "-m", "base")
        run("git", "update-ref", "refs/remotes/origin/main", "HEAD")
        run("git", "checkout", "-q", "-b", "sdlc/77")
        (root / "pkg" / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef multiply(a, b):\n    return a * b\n")
        (root / "tests" / "test_calc.py").write_text(
            "from pkg.calc import add, multiply\n\n\n"
            "def test_add():\n    assert add(2, 3) == 5 and add(0, 0) == 0\n\n\n"
            "def test_multiply():\n    assert multiply(2, 3) == 6\n")
        (root / "tests" / "test_other.py").write_text("def test_noop():\n    assert 1 == 1\n")
        base = str(root / ".sdlc")
        loop.work._save(base, "77", {"worktree": d, "branch": "sdlc/77", "base": "main",
                                      "base_resolved": "main", "remote": "origin", "pr": ""})
        result = loop._diff_revert_verdict(base, "77", d, True)
        assert result.get("production_changed") is True, result
        assert result.get("changed_survivors") == ["tests/test_other.py::test_noop"], result
        assert "tests/test_calc.py::test_add" in result.get("uncollected", []), result
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_a_tests_only_goal_records_no_changed_survivors():
    """P6 review F1: nothing production changed, so nothing was reverted and no survivor is judged."""
    loop = _load("loop")
    d = tempfile.mkdtemp()
    try:
        def run(*argv):
            return subprocess.run(argv, cwd=d, capture_output=True, text=True)
        run("git", "init", "-q")
        run("git", "checkout", "-q", "-b", "main")
        run("git", "config", "user.email", "t@example.com")
        run("git", "config", "user.name", "t")
        root = pathlib.Path(d)
        (root / "pkg").mkdir()
        (root / "pkg" / "__init__.py").write_text("")
        (root / "pkg" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (root / "tests").mkdir()
        (root / "tests" / "test_calc.py").write_text(
            "from pkg.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n")
        run("git", "add", "-A")
        run("git", "commit", "-q", "-m", "base")
        run("git", "update-ref", "refs/remotes/origin/main", "HEAD")
        run("git", "checkout", "-q", "-b", "sdlc/78")
        (root / "tests" / "test_more.py").write_text(
            "from pkg.calc import add\n\n\ndef test_zero():\n    assert add(0, 0) == 0\n")
        base = str(root / ".sdlc")
        loop.work._save(base, "78", {"worktree": d, "branch": "sdlc/78", "base": "main",
                                      "base_resolved": "main", "remote": "origin", "pr": ""})
        result = loop._diff_revert_verdict(base, "78", d, True)
        assert result.get("production_changed") is False, result
        assert result.get("changed_survivors") == [], result
        assert "tests/test_more.py::test_zero" in result["survivors"], result
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- the brief's test-trust facts

def _brief(d, phase, root):
    return _load("review_context").brief(d, GOAL, phase, repo_root=str(root))


def _section(text):
    assert "## Test-trust facts" in text, "the brief has no test-trust section"
    return text.split("## Test-trust facts", 1)[1].split("\n## ", 1)[0]


def test_brief_carries_present_facts(tmp_path):
    _rt()
    d = _repo(tmp_path, evidence=_with_changed(M))
    s = _section(_brief(d, "code-review", tmp_path))
    for want in ("AC-1", "first", "AC-3", "third", "Red-green proof: present", "Flake check: present",
                 "Diff-revert: present", "kills: " + A, "survivors: " + M,
                 "survivors on changed lines: " + M):
        assert want in s, (want, s)


def test_brief_says_absent_without_evidence(tmp_path):
    _rt()
    s = _section(_brief(_repo(tmp_path), "pr-review", tmp_path))
    for want in ("Red-green proof: absent", "Flake check: absent", "Diff-revert: absent"):
        assert want in s, (want, s)


def test_brief_says_unreachable_without_leaking_a_path(tmp_path):
    _rt()
    d = _repo(tmp_path, evidence="{not json")
    s = _section(_brief(d, "code-review", tmp_path))
    assert "unreachable" in s and str(tmp_path) not in s, s


def test_plan_review_brief_has_no_facts(tmp_path):
    _rt()
    assert "## Test-trust facts" not in _brief(_repo(tmp_path, evidence=MEASURED), "plan-review", tmp_path)


def test_the_map_instruction_follows_the_key(tmp_path):
    _rt()
    on = _section(_brief(_repo(tmp_path / "on", gate=True, evidence=MEASURED), "code-review", tmp_path / "on"))
    off = _brief(_repo(tmp_path / "off", evidence=MEASURED), "code-review", tmp_path / "off")
    assert "TEST-MAP AC-" in on and "TEST-MAP" not in off


# ---------------------------------------------------------------- the approval seams

def test_record_review_refuses_a_blocking_approve(tmp_path):
    assert "review_text" in inspect.signature(work.record_review).parameters
    d = _repo(tmp_path, gate=True, evidence=MEASURED)
    try:
        work.record_review(d, {"review": {"test_review": True}}, GOAL, "APPROVE", review_text=BAD)
    except ValueError as exc:
        assert "AC-2" in str(exc) and "SEND-BACK" in str(exc), exc
    else:
        raise AssertionError("a blocking test review was recorded as an approval")


def test_record_review_without_text_names_the_flag(tmp_path):
    assert "review_text" in inspect.signature(work.record_review).parameters
    d = _repo(tmp_path, gate=True, evidence=MEASURED)
    try:
        work.record_review(d, {"review": {"test_review": True}}, GOAL, "APPROVE")
    except ValueError as exc:
        assert "--review-output" in str(exc), exc
    else:
        raise AssertionError("an approval with no reviewer text was recorded")


def _not_a_test_review_refusal(call):
    try:
        call()
    except ValueError as exc:
        assert "test review" not in str(exc).lower(), exc


def test_send_back_and_key_off_are_never_checked(tmp_path):
    assert "review_text" in inspect.signature(work.record_review).parameters
    on = _repo(tmp_path / "on", gate=True, evidence=MEASURED)
    off = _repo(tmp_path / "off", evidence=MEASURED)
    _not_a_test_review_refusal(lambda: work.record_review(on, {"review": {"test_review": True}}, GOAL,
                                                          "SEND-BACK", review_text=BAD))
    _not_a_test_review_refusal(lambda: work.record_review(off, {}, GOAL, "APPROVE", review_text=BAD))


def test_record_review_cli_reads_the_review_output_file(tmp_path):
    _rt()
    d = _repo(tmp_path, gate=True, evidence=MEASURED)
    p = subprocess.run([sys.executable, str(SCRIPTS / "work.py"), "record-review", d, GOAL, "--verdict",
                        "APPROVE", "--review-output", str(tmp_path / "missing.txt")],
                       capture_output=True, text=True)
    assert p.returncode == 2 and "missing.txt" in p.stderr, p.stderr
    review = tmp_path / "review.txt"
    review.write_text(BAD)
    p = subprocess.run([sys.executable, str(SCRIPTS / "work.py"), "record-review", d, GOAL, "--verdict",
                        "APPROVE", "--review-output", str(review)], capture_output=True, text=True)
    assert p.returncode == 2 and "AC-2" in p.stderr, p.stderr


def _write(d, verdict, reason, tmp_path):
    resolution = tmp_path / ("resolution-%s.json" % verdict)
    resolution.write_text("{}")
    manifest = {"generation_id": "g-" + verdict, "brief_sha256": "0" * 64, "goal": GOAL}
    return work._write_review_result(d, manifest, str(resolution), verdict, reason)


def test_a_blocking_approve_or_unblock_is_written_as_block(tmp_path):
    _rt()
    d = _repo(tmp_path, gate=True, evidence=MEASURED)
    for verdict in ("approve", "unblock"):
        result = _write(d, verdict, BAD, tmp_path)
        assert result["verdict"] == "block" and "AC-2" in result["reason"].split("VERDICT")[0], result
        assert result["reason"].rstrip().endswith("VERDICT: approve"), result
    assert _write(d, "block", "real findings", tmp_path)["reason"] == "real findings"


def test_a_partial_review_is_stored_in_the_pr_result(tmp_path):
    """P6 review M1: an unjudged survivor clause is written where the owner reads the result."""
    _rt()
    d = _repo(tmp_path, gate=True)
    result = _write(d, "approve", GOOD, tmp_path)
    assert result["verdict"] == "approve" and "TEST-REVIEW: partial" in result["reason"], result


def test_the_writer_is_unchanged_with_the_key_off(tmp_path):
    _rt()
    d = _repo(tmp_path, evidence=MEASURED)
    result = _write(d, "approve", BAD, tmp_path)
    assert result["verdict"] == "approve" and result["reason"] == BAD


def _post(d, goal, verdict, evidence, head):
    calls = []

    def run(cwd, argv):
        line = " ".join(map(str, argv))
        calls.append(line)
        if prfake.is_pr_get(argv, 7):
            return prfake.rest_pull(number=7, headRefOid=head)
        if _posted(line):
            return _comment_reply(99)
        return ""
    return work.post_review(d, ON, goal, run=run, verdict=verdict, evidence=str(evidence)), calls


def test_post_review_posts_the_stored_block_when_asked_to_approve(tmp_path):
    _rt()
    d = _sdlc(tmp_path)
    goal = _started(d)
    evidence, head = _review_chain(d, goal, "block", "AC-2 has no test")
    out, calls = _post(d, goal, "approve", evidence, head)
    assert out.startswith("posted sigma:block"), out
    assert any(_posted(c) and "AC-2 has no test" in c for c in calls), calls


def test_a_posted_block_reads_as_block(tmp_path):
    """P6 re-review bug 3: a stored reason quoting a marker line must not turn the posted block into
    an unblock for the merge gate, which reads the LAST marker line."""
    _rt()
    d = _sdlc(tmp_path)
    goal = _started(d)
    evidence, head = _review_chain(d, goal, "block", "AC-2 has no test\nsigma:unblock is what a fix posts")
    out, calls = _post(d, goal, "approve", evidence, head)
    body = next(c for c in calls if _posted(c))
    assert out.startswith("posted sigma:block"), out
    assert work._line_directive(body.split("body=", 1)[1]) == "block", body


def test_post_review_still_refuses_block_against_an_approval(tmp_path):
    _rt()
    d = _sdlc(tmp_path)
    goal = _started(d)
    evidence, head = _review_chain(d, goal, "approve")
    out, calls = _post(d, goal, "block", evidence, head)
    assert out == "review evidence does not match this goal and verdict" and not any(map(_posted, calls))


def test_bytecode_and_runner_caches_are_never_production():
    """The tests-only goal above wrote `__pycache__` files by running its own tests; they are not a production change.
    Written explicitly here, so this holds whether or not the host writes bytecode."""
    dr = _load("diff_revert")
    assert dr.production_changed(["tests/test_more.py", "pkg/__pycache__/calc.cpython-313.pyc",
                                  "tests/__pycache__/test_more.cpython-313-pytest-8.4.2.pyc",
                                  ".pytest_cache/v/cache/nodeids", "pkg/legacy.pyc"]) is False
    assert dr.production_changed(["tests/test_more.py", "pkg/calc.py"]) is True


def test_facts_name_a_touched_monitor_file_and_stay_silent_otherwise(tmp_path, monkeypatch):
    """#1999: the brief carries the monitor finding; no finding adds no line; a diff fault adds none."""
    rt = _rt()
    d = _repo(tmp_path)
    removal = ("diff --git a/hooks/x.sh b/hooks/x.sh\n--- a/hooks/x.sh\n+++ b/hooks/x.sh\n"
               "@@ -1,2 +1,1 @@\n keep\n-gone\n")
    monkeypatch.setattr(rt, "_goal_diff", lambda *_a: removal)
    assert "hooks/x.sh (-1 lines)" in rt.facts(d, GOAL)
    monkeypatch.setattr(rt, "_goal_diff", lambda *_a: "")
    assert "Monitor files" not in rt.facts(d, GOAL)

    def boom(*_a):
        raise OSError("no git")
    monkeypatch.setattr(rt, "_goal_diff", boom)
    assert "Monitor files" not in rt.facts(d, GOAL)
