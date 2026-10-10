"""Parity between the never-delete guard and the write-surface scanner (#958).

The scanner (`tools/readiness/write_surface.py`) classes a call as `git-destructive` from its argv
tokens; the guard (`tests/test_no_autonomous_feature_branch_deletion.py`) reads one physical line.
Every delete shape the scanner flags in a planted tracked file must also be a guard hit, and the shapes
the guard still cannot see are pinned by name below. Closing one is a conscious edit of this file.
"""
import importlib.util
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
_cache = {}


def _load(key, path):
    if key not in _cache:
        spec = importlib.util.spec_from_file_location(key, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _cache[key] = module
    return _cache[key]


def _guard():
    return _load("parity_guard", ROOT / "tests" / "test_no_autonomous_feature_branch_deletion.py")


def _scanner():
    return _load("parity_scanner", ROOT / "tools" / "readiness" / "write_surface.py")


def _plant(tmp_path, line):
    code = "def f(run, cwd, remote, name, sha, ref):\n    %s\n" % line
    path = tmp_path / "skills" / "x" / "scripts" / "y.py"
    path.parent.mkdir(parents=True)
    path.write_text(code, encoding="utf-8")
    for args in (["init", "-q"], ["add", "-A"]):
        subprocess.run(["git", *args], cwd=str(tmp_path), check=True, capture_output=True, timeout=120)
    return path


def _scanner_rules(tmp_path, line):
    path = _plant(tmp_path, line)
    return {r["rule"] for r in _scanner().scan_paths(tmp_path, [path])}


def _guard_kinds(tmp_path):
    hits = _guard()._delete_call_sites(tmp_path)
    assert hits is not None
    return {kind for _p, _n, kind, _l in hits}


#: id, line. Each is flagged `git-destructive` by the scanner and must be a guard hit.
PARITY = [
    ("update_ref_message_before_delete", 'run(cwd, ["git", "update-ref", "-m", "why", "-d", ref])'),
    ("update_ref_bare_message_before_delete", 'run(cwd, ["update-ref", "-m", "why", "--delete", ref])'),
    ("push_mirror", 'run(cwd, ["git", "push", "--mirror", remote])'),
    ("push_mirror_after_remote", 'run(cwd, ["git", "push", remote, "--mirror"])'),
    ("push_short_delete", 'run(cwd, ["git", "push", "-d", remote, name])'),
    ("update_ref_delete", 'run(cwd, ["git", "update-ref", "-d", ref])'),
]

#: id, line. String-form commands: the scanner reads argv tokens and does NOT flag them (pinned below
#: as a scanner limit); the guard must flag them.
STRING_FORM = [
    ("string_push_short_d", 'run(cwd, "git push -d origin topic")'),
    ("string_push_long_delete", 'run(cwd, "git push origin --delete topic")'),
    ("string_push_mirror", 'run(cwd, "git push --mirror origin")'),
    ("string_update_ref_message_delete", 'run(cwd, "git update-ref -m why -d refs/x")'),
]

#: Shapes the guard does not see, pinned by name (id, line).
GUARD_KNOWN_LIMITS = [
    ("flag_built_by_concatenation", 'run(cwd, ["git", "push", "--" + "mirror"])'),
]


def test_every_scanner_flagged_delete_shape_is_a_guard_hit(tmp_path):
    missing = []
    for i, (case_id, line) in enumerate(PARITY):
        sub = tmp_path / ("p%d" % i)
        sub.mkdir()
        assert "git-destructive" in _scanner_rules(sub, line), case_id
        if not _guard_kinds(sub):
            missing.append(case_id)
    assert not missing, missing


def test_string_form_delete_commands_are_guard_hits(tmp_path):
    missing = []
    for i, (case_id, line) in enumerate(STRING_FORM):
        sub = tmp_path / ("s%d" % i)
        sub.mkdir()
        _plant(sub, line)
        if not _guard_kinds(sub):
            missing.append(case_id)
    assert not missing, missing


@pytest.mark.parametrize("case_id, line", STRING_FORM, ids=[c[0] for c in STRING_FORM])
def test_known_limit_scanner_does_not_classify_string_form_commands(tmp_path, case_id, line):
    """KNOWN LIMIT (green by design): the scanner sees argv tokens, not a one-string command. The guard
    covers the gap; closing the scanner side means inverting this case."""
    assert "git-destructive" not in _scanner_rules(tmp_path, line)


@pytest.mark.parametrize("case_id, line", GUARD_KNOWN_LIMITS, ids=[c[0] for c in GUARD_KNOWN_LIMITS])
def test_known_limit_guard_does_not_see_built_up_commands(tmp_path, case_id, line):
    """KNOWN LIMIT (green by design): a command assembled at run time is invisible to a line scan."""
    _plant(tmp_path, line)
    assert not _guard_kinds(tmp_path)


def test_guard_stays_quiet_on_nearby_non_deletes(tmp_path):
    _plant(tmp_path, 'run(cwd, ["git", "update-ref", "-m", "why", "refs/x", sha])')
    assert not _guard_kinds(tmp_path)
    sub = tmp_path / "q"
    sub.mkdir()
    _plant(sub, 'run(cwd, "git push origin topic")')
    assert not _guard_kinds(sub)
