"""Sigma must never autonomously delete a `feature/<name>` branch (issue #1819).

`docs/branching-model.md` (S13/S13a) already treats `feature/<name>` as long-lived: finishing a
unit is a REGISTRY EDIT (`open: false`), never a branch deletion. A prior audit found no code path
in the kit deletes a `feature/<name>` branch today -- but that was true only by absence, never
checked or stated as an invariant. This file makes it checked (S13b in the doc is the stated half).

WHAT THIS PROVES, in two parts:

1. STRUCTURAL INVENTORY (`_delete_call_sites` + `test_every_delete_shaped_call_site_...`). Every
   `.py` file `git` tracks under `skills/` or `hooks/` is scanned, with comments and docstrings
   blanked out first (a tokenize pass, not a raw substring scan -- see `_python_scannable_lines`),
   for seven kinds:
   - `branch_dD`    -- `git branch -d/-D <name>` (a local delete)
   - `push_delete`  -- `git push ... --delete <name>` (GitHub's explicit remote-delete flag), or
     the short `push ... -d` flag on the same line
   - `rest_delete_ref` -- a REST DELETE in any spelling (`-X DELETE`, `--method DELETE`,
     `--method=DELETE`, `-XDELETE`) on a line that also names a `.../git/refs/...` endpoint
   - `rest_delete` -- the same REST DELETE verb with no `git/refs/` on its line. The endpoint may
     sit on another physical line (`gh_api.remove_label` builds its argument list that way), so the
     guard cannot tell a ref delete from a label delete and flags both for review
   - `update_ref_delete` -- `git update-ref -d/--delete <ref>`, optionally after `--no-deref`. Only
     the delete forms: a plain `update-ref <ref> <sha>` is not a deletion and stays quiet
   - `gh_api_delete` -- a `gh_api.delete...(` helper call, on a `gh_api` name or on a
     `_load("gh_api")` style loader receiver
   - `colon_refspec` -- a two-sided push refspec `<src>:refs/...` (heads, or any other namespace)
     -- git's THIRD, easy to miss delete syntax: when `<src>` is empty, `git push` DELETES
     `<dst>`. This is not itself a deletion, but a BUILD SITE for one, and it is exactly the shape
     a naive "just check for -d/-D and --delete" scanner would miss entirely.
   The inventory is then pinned to the EXACT, already-reviewed set of sites this scan currently
   finds -- a SEVENTH site appearing anywhere (a new delete, or a new unaudited colon-refspec build)
   fails the pin immediately, by file, function and kind, forcing a conscious decision rather than
   a silent regression.

2. BEHAVIORAL PROOF for the two shapes that are structurally live. `work.py`'s `finish()` and
   `merge()` really do delete a branch (the GOAL's own throwaway `sdlc/<goal>` branch, once its PR
   is confirmed merged) -- so the inventory pin alone is not enough; this proves that even when the
   SAME goal's `base` is a `feature/<name>` unit branch, no delete-shaped call ever names it.
   `feature_rebase.py`'s colon-refspec site is proven safe differently (its source side is the
   fixed literal `"HEAD"`, never a variable) and `define.py`'s is proven safe via `resolved_base()`
   never returning a falsy value.

WHY TOKENIZE, NOT A RAW SUBSTRING SCAN. `_delete_remote_branch`'s own real docstring in
`work.py` contains the literal prose `"gh pr merge --delete-branch"` -- a raw scan for the
substring `"--delete"` would flag its own explanatory comment. `_python_scannable_lines` (the same
shape `tests/test_no_unsupervised_process_pause.py` already established in this repo, reimplemented
here rather than imported -- this repo has no shared test-helper module) blanks every `COMMENT`
token and every triple-quoted, non-f-string `STRING` token via the stdlib `tokenize` module, leaving
ordinary string literals (`"branch"`, `"-D"`, `"DELETE"`) -- exactly where the real git/gh
arguments live -- fully scannable.

WHY TRACKED FILES, NOT `Path.rglob()`. This repo's own `tests/test_self_contained.py`
(`_tracked_files()`) already fixed this exact defect class (#1434, #1461, landed as #1821 the same
day as this file): a raw filesystem walk picks up untracked clutter -- a stray local scratch file,
a `.claude/worktrees/` checkout -- that never ships and is not this project's code. `_owned_py_files`
below asks `git ls-files` the same way, so a stray untracked `.py` file on a developer's disk (or a
concurrent goal's own worktree) can never spuriously trip the inventory pin at `verify` time.

NAMED, ACCEPTED GAPS. Like the SIGSTOP guard, this is a TEXT/line scan, not a full data-flow
analysis: a delete-shaped call built with the flag as a separate list element assembled across
several statements (rather than one contiguous literal call) would not be caught. Each pattern is
also matched PER PHYSICAL LINE, not per logical statement, so a call deliberately wrapped across
multiple lines can split a pattern's two halves onto different lines and slip through (`push` and
`-d`, `update-ref` and `-d`). Three more gaps are known: a colon-empty refspec whose colon is built
dynamically (a `":%s"` format, an f-string) has no literal `:refs/` on its line; a `gh_api`
receiver bound to a variable first, or a bare `from gh_api import ...` name, is not recognised;
and files under `tools/` and shell scripts are outside the scanned tree. Every one of them is
pinned as a KNOWN LIMIT by `test_known_limits_of_the_per_line_scan_are_pinned` in
`tests/test_never_delete_guard_backup_refs.py`, rather than papered over with a multi-line join --
closing a gap is a conscious edit of that test. The write-surface scanner
(`tools/readiness/write_surface.py`, AST based) covers `tools/`, shell scripts and the dynamic
colon builds. This closes the LIKELY reintroduction shape -- a literal `git branch -d/-D`,
`--delete`, REST DELETE, `update-ref -d` or colon-refspec construction, written the way every real
call site in this repo already is -- not every conceivable way to construct one dynamically.

THE BACKUP-NAMESPACE RULE. What this guard enforces about `refs/sigma/backup/` is stated once, in
`NEVER_DELETE_BACKUP_RULE` below, and pinned verbatim by
`tests/test_never_delete_guard_backup_refs.py`; `_files_carrying_the_rule_wording` finds any other
tracked file that states it, so a copy cannot drift unlisted.
"""
import ast
import io
import json
import pathlib
import re
import subprocess
import sys
import tokenize
import types
import importlib.util

import prfake          # #895 4a-1: REST bodies for the migrated PR reads

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
DEFINE_SCRIPTS = ROOT / "skills" / "sigma-define" / "scripts"


def _load(name, directory=SCRIPTS):
    spec = importlib.util.spec_from_file_location(name, directory / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


work = _load("work")
define = _load("define", DEFINE_SCRIPTS)

ON = {"work": {"enabled": True}}
ALWAYS_GITHUB = {"work": {"enabled": True, "auto_merge": "always"},
                  "discovery": {"source": "github"}}
NOSLEEP = lambda _: None                                          # noqa: E731 - one-liner test stub
HEAD_SHA = "0" * 40
REMOTE_URL = "git@github.com:acme/app.git"

NEVER_DELETE_BACKUP_RULE = "The never-delete guard pins every delete-shaped call it can see under skills/ and hooks/; under refs/sigma/backup/ the only sanctioned automatic deletion is a prune by literal prefix."
_RULE_HOME = "tests/test_no_autonomous_feature_branch_deletion.py"    # the one defining module
_RULE_PIN_HOME = "tests/test_never_delete_guard_backup_refs.py"       # holds the independent pin literal
_FILES_STATING_THE_RULE = frozenset({
    "docs/branching-model.md",          # section 13b states the rule verbatim
})
_RULE_VARIANT_MIN_SHARED = 8            # shared word-4-grams that make a file a "variant" (of 28)
_RULE_FILE_CAP = 2000000                # bytes; a larger tracked file is skipped


def _words(text):
    return re.findall(r"[a-z0-9]+", text.casefold())


def _files_carrying_the_rule_wording(root):
    """{repo-relative path: "exact" | "variant"} over tracked text files, or None when `root` is not a
    git checkout. Skips `.sdlc/`, the two home paths, files over the cap and unreadable files."""
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                             capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    sentence = _words(NEVER_DELETE_BACKUP_RULE)
    whole = " " + " ".join(sentence) + " "
    grams = [" " + " ".join(sentence[i:i + 4]) + " " for i in range(len(sentence) - 3)]
    found = {}
    for rel in sorted(f for f in out.stdout.split("\0") if f):
        if rel.startswith(".sdlc/") or rel in (_RULE_HOME, _RULE_PIN_HOME):
            continue
        try:
            path = pathlib.Path(root) / rel
            if path.stat().st_size > _RULE_FILE_CAP:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        flat = " " + " ".join(_words(text)) + " "
        if whole in flat:
            found[rel] = "exact"
        elif sum(1 for g in grams if g in flat) >= _RULE_VARIANT_MIN_SHARED:
            found[rel] = "variant"
    return found


# =====================================================================================================
# Part 1 -- the structural inventory: scanner + comment/docstring stripper + pin
# =====================================================================================================

_STRING_PREFIX_RE = re.compile(r"^[A-Za-z]*")


def _is_triple_quoted(token_text):
    """Whether a `tokenize` STRING token's own source text is an exemptable, real docstring-shaped
    triple-quoted, non-f-string literal -- same rule `test_no_unsupervised_process_pause.py`
    established: f-strings are never exempt, on any Python version, because an f-string can never
    actually be a compiled `__doc__` in the first place."""
    prefix_end = _STRING_PREFIX_RE.match(token_text).end()
    prefix, body = token_text[:prefix_end], token_text[prefix_end:]
    if "f" in prefix.lower():
        return False
    return body.startswith('"""') or body.startswith("'''")


def _blank_span(line_chars, start, end):
    """Overwrite `line_chars` (one mutable per-line list of characters) with spaces across the
    half-open `tokenize` span `[start, end)` -- spaces, never deletion, so no two separated code
    fragments can ever be accidentally concatenated into a new false match."""
    (start_row, start_col), (end_row, end_col) = start, end
    if start_row == end_row:
        row = start_row - 1
        if 0 <= row < len(line_chars):
            width = len(line_chars[row])
            for col in range(start_col, min(end_col, width)):
                line_chars[row][col] = " "
        return
    row = start_row - 1
    if 0 <= row < len(line_chars):
        for col in range(start_col, len(line_chars[row])):
            line_chars[row][col] = " "
    for row in range(start_row, end_row - 1):
        if 0 <= row < len(line_chars):
            for col in range(len(line_chars[row])):
                line_chars[row][col] = " "
    row = end_row - 1
    if 0 <= row < len(line_chars):
        width = len(line_chars[row])
        for col in range(0, min(end_col, width)):
            line_chars[row][col] = " "


def _python_scannable_lines(text):
    """The code-only view of a `.py` file's lines: every COMMENT token and every real triple-quoted,
    non-f-string STRING token (a docstring) is blanked; an ordinary string literal is left fully
    scannable. Fails SAFE on an unparsable file: falls back to the raw lines (comments/docstrings
    not stripped there), which can only ever cause a false positive, never a false negative."""
    lines = text.split("\n")
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError):
        return lines
    line_chars = [list(line) for line in lines]
    for tok in tokens:
        if tok.type == tokenize.COMMENT:
            _blank_span(line_chars, tok.start, tok.end)
        elif tok.type == tokenize.STRING and _is_triple_quoted(tok.string):
            _blank_span(line_chars, tok.start, tok.end)
    return ["".join(chars) for chars in line_chars]


def _owned_py_files(root):
    """Every `.py` file `git` tracks under `root` inside `skills/` or `hooks/` -- TRACKED files,
    never a raw walk (module docstring's "WHY TRACKED FILES"). Returns `None` when `root` is not a
    git checkout (or `git` is unavailable), so callers can skip honestly rather than silently
    scanning nothing."""
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                             capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    files = [f for f in out.stdout.split("\0") if f]
    owned = [f for f in files
             if f.endswith(".py") and (f.startswith("skills/") or f.startswith("hooks/"))]
    return sorted(root / f for f in owned)


#: `"branch", "-D"` / `"branch", "-d"` -- a local delete, e.g. `["git", "branch", "-D", branch]`.
_BRANCH_DD_RE = re.compile(r'"branch"\s*,\s*"-[dD]"')

#: `"push" ... "-d"` -- the short remote-delete flag, on the SAME physical line as `push` (a bare `"-d"`
#: alone would also match `curl -d`, so the two halves are paired).
_PUSH_D_RE = re.compile(r'"push"[^\n]*"-d"')

#: `"push" ... "--mirror"` -- a mirror push deletes every remote ref the local side lacks; the write-surface
#: scanner classes it destructive, so the guard pins it too (#958).
_PUSH_MIRROR_RE = re.compile(r'"push"[^\n]*"--mirror"')

#: A one-string command (`"git push -d origin x"`): `git push` with `-d`, `--delete` or `--mirror`, or
#: `git update-ref` with `-d`/`--delete`, inside one quoted literal (#958).
_STRING_FORM_RE = re.compile(
    r"""["'][^"'\n]*\bgit\s+(?:push\b[^"'\n]*\s(?:-d|--delete|--mirror)\b"""
    r"""|update-ref\b[^"'\n]*\s(?:-d|--delete)\b)""")

#: A REST delete in any spelling: `"-X", "DELETE"`, `"--method", "DELETE"`, `"--method=DELETE"`,
#: `"-XDELETE"`. The endpoint is NOT required on the same line (`gh_api.remove_label` puts the verb and
#: the endpoint on different physical lines); the kind records whether a `git/refs/` endpoint is there.
_REST_DELETE_RE = re.compile(r'"(?:-X|--method)"\s*,\s*"(?i:delete)"|"(?:--method=|-X)(?i:delete)"')

#: `update-ref -d` / `--delete`, optionally after other flags such as `--no-deref` or `-m <msg>` (#958) -- the delete forms only; a plain
#: `update-ref <ref> <sha>` is not a deletion and stays quiet.
_UPDATE_REF_DELETE_RE = re.compile(r'"update-ref"\s*,(?:\s*"[^"]*"\s*,)*?\s*"(?:-d|--delete)"')

#: A `gh_api.delete...(` helper call, on a `gh_api` name or a `_load("gh_api")` style loader receiver.
_GH_API_DELETE_RE = re.compile(r"\bgh_api\b(?:[\"']\))?\.delete\w*\s*\(")


#: A Python HTTP-client DELETE: `method="DELETE"` keyword, `requests|httpx.delete(`, `.request("DELETE"` (#959).
_HTTP_DELETE_RE = re.compile(
    r"""\bmethod\s*=\s*["'](?i:delete)["']|\b(?:requests|httpx)\.delete\s*\(|\.request\(\s*["'](?i:delete)["']""")


def _delete_call_sites(root):
    """The guard, as a pure function: `(path, lineno, kind, line_text)` for every branch/ref
    delete-shaped (or delete-CAPABLE, for `colon_refspec`) line found under `root`'s tracked
    `skills/`+`hooks/` tree. `None` when `root` is not a git checkout -- see `_owned_py_files`."""
    files = _owned_py_files(root)
    if files is None:
        return None
    hits = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(_python_scannable_lines(text), start=1):
            if _BRANCH_DD_RE.search(line):
                hits.append((path, lineno, "branch_dD", line))
            if ("--delete" in line or _PUSH_D_RE.search(line) or _PUSH_MIRROR_RE.search(line)
                    or _STRING_FORM_RE.search(line)):
                hits.append((path, lineno, "push_delete", line))
            if _REST_DELETE_RE.search(line):
                hits.append((path, lineno,
                             "rest_delete_ref" if "git/refs/" in line else "rest_delete", line))
            if _UPDATE_REF_DELETE_RE.search(line):
                hits.append((path, lineno, "update_ref_delete", line))
            if _HTTP_DELETE_RE.search(line):
                hits.append((path, lineno, "http_delete", line))
            if _GH_API_DELETE_RE.search(line):
                hits.append((path, lineno, "gh_api_delete", line))
            if ":refs/" in line:
                hits.append((path, lineno, "colon_refspec", line))
    return sorted(hits, key=lambda h: (str(h[0]), h[1], h[2]))


def _enclosing_function(path, lineno):
    """The innermost `def`/`async def` in `path` whose body spans `lineno`, by real AST ranges --
    immune to line-number drift, unlike pinning an exact line number. `None` if the file can't be
    parsed or no function contains the line (a module-level statement)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return None
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start, end = node.lineno, getattr(node, "end_lineno", node.lineno)
            if start <= lineno <= end:
                if best is None or (end - start) < (best[1] - best[0]):
                    best = (start, end, node.name)
    return best[2] if best else None


def _make_tracked_repo(tmp_path, rel_path, content):
    """A throwaway git repo with exactly one tracked `.py` file, staged (not necessarily committed
    -- `git ls-files` reports the index) -- enough for `_owned_py_files`/`_delete_call_sites` to see
    it as tracked."""
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(tmp_path), check=True)
    full = tmp_path / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
    return tmp_path


def test_a_planted_branch_dash_capital_d_call_is_caught(tmp_path):
    _make_tracked_repo(tmp_path, "skills/x/scripts/y.py",
                       'def f(run, cwd, name):\n'
                       '    run(cwd, ["git", "branch", "-D", name])\n')
    hits = _delete_call_sites(tmp_path)
    assert any(kind == "branch_dD" for _, _, kind, _ in hits), hits


def test_the_same_pattern_inside_a_docstring_is_not_flagged(tmp_path):
    """The exact shape `_delete_remote_branch`'s own real docstring has for `--delete-branch`, and
    the equivalent for `branch_dD`: prose describing the pattern, not code performing it."""
    _make_tracked_repo(tmp_path, "skills/x/scripts/y.py",
                       'def f():\n'
                       '    """Never do ["git", "branch", "-D", x] -- this is only documentation."""\n'
                       '    return 1\n')
    hits = _delete_call_sites(tmp_path)
    assert not any(kind == "branch_dD" for _, _, kind, _ in hits), hits


def test_the_same_pattern_inside_a_hash_comment_is_not_flagged(tmp_path):
    _make_tracked_repo(tmp_path, "skills/x/scripts/y.py",
                       'def f():\n'
                       '    # ["git", "branch", "-D", x] -- do not actually do this\n'
                       '    return 1\n')
    hits = _delete_call_sites(tmp_path)
    assert not any(kind == "branch_dD" for _, _, kind, _ in hits), hits


def test_a_planted_push_delete_flag_is_caught(tmp_path):
    _make_tracked_repo(tmp_path, "skills/x/scripts/y.py",
                       'def f(run, cwd, remote, name):\n'
                       '    run(cwd, ["git", "push", remote, "--delete", name])\n')
    hits = _delete_call_sites(tmp_path)
    assert any(kind == "push_delete" for _, _, kind, _ in hits), hits


def test_a_planted_rest_delete_ref_call_is_caught(tmp_path):
    """A single physical line, matching the real call's own shape (`work.py`'s
    `_delete_remote_branch`) -- the scanner is a per-line scan (module docstring's "NAMED,
    ACCEPTED GAP"), so a call deliberately split across lines is a different, accepted gap."""
    _make_tracked_repo(tmp_path, "skills/x/scripts/y.py",
                       'def f(run, cwd, branch):\n'
                       '    run(cwd, ["gh", "api", "-X", "DELETE", "repos/o/r/git/refs/heads/%s" % branch])\n')
    hits = _delete_call_sites(tmp_path)
    assert any(kind == "rest_delete_ref" for _, _, kind, _ in hits), hits


def test_a_planted_colon_refspec_is_caught(tmp_path):
    """`git push <src>:refs/heads/<dst>` deletes `<dst>` when `<src>` is empty -- git's third,
    easy-to-miss delete syntax, not covered by any of the other three patterns."""
    _make_tracked_repo(tmp_path, "skills/x/scripts/y.py",
                       'def f(run, cwd, remote, base, name):\n'
                       '    run(cwd, ["git", "push", remote, "%s:refs/heads/%s" % (base, name)])\n')
    hits = _delete_call_sites(tmp_path)
    assert any(kind == "colon_refspec" for _, _, kind, _ in hits), hits


def test_owned_py_files_scans_a_meaningful_number_of_real_files():
    """Non-vacuous by construction: a scan scoped to nothing would pass every test below for the
    wrong reason. Floor of 50 is well under this repo's real tracked count under skills/+hooks/."""
    files = _owned_py_files(ROOT)
    assert files is not None, "not a git checkout -- cannot verify the real tree"
    assert len(files) >= 50, len(files)


def test_every_delete_shaped_call_site_in_the_kit_is_one_of_the_known_reviewed_ones():
    """THE INVENTORY PIN. Every branch/ref-delete-shaped (or delete-capable) line in this repo's own
    tracked `skills/`+`hooks/` tree, today, is one of exactly these nine -- two real deletes (both
    scoped structurally to the GOAL's own throwaway `sdlc/<goal>` branch, never `feature/<name>`,
    proven behaviorally in Part 2 below), one label delete (`gh_api.remove_label`, a REST DELETE
    that touches an issue label and no ref), three colon-refspec BUILD sites, each independently
    proven safe (Part 2), and the two sites of `feature_backup.py`: the atomic push that creates a
    backup ref (a colon refspec whose sources are validated, never empty) and the prune, the one
    sanctioned automatic deletion, which uses the `--delete` flag form and names only refs that parse
    as exactly `<prefix><unit>/<stamp>`. A tenth site appearing anywhere -- including one that
    could delete a `feature/<name>` branch -- fails this test immediately, by file, function and
    kind, which is the whole point: it forces a conscious decision (update this pin AND
    docs/branching-model.md S13b) instead of a silent regression."""
    hits = _delete_call_sites(ROOT)
    assert hits is not None, "not a git checkout -- cannot verify the real tree"
    found = {(str(path.relative_to(ROOT)), _enclosing_function(path, lineno), kind)
             for path, lineno, kind, _line in hits}
    assert found == {
        ("skills/sigma-loop/scripts/work.py", "finish", "branch_dD"),
        ("skills/sigma-loop/scripts/work.py", "_delete_remote_branch", "rest_delete_ref"),
        # a label delete, not a ref delete: pinned because the guard cannot see the endpoint
        ("skills/sigma-loop/scripts/gh_api.py", "remove_label", "rest_delete"),
        ("skills/sigma-define/scripts/define.py", "_step_branch", "colon_refspec"),
        ("skills/sigma-loop/scripts/feature_rebase.py", "_pushed", "colon_refspec"),
        # read-only: a `git fetch` refspec that downloads a remote ref into a local one, deletes nothing
        ("skills/sigma-loop/scripts/feature_land.py", "_fetch", "colon_refspec"),
        ("skills/sigma-loop/scripts/release_manifest.py", "publish_to_ledger_branch", "colon_refspec"),
        ("skills/sigma-loop/scripts/feature_backup.py", "_atomic_leased_push", "colon_refspec"),
        ("skills/sigma-loop/scripts/feature_backup.py", "_delete_chunk", "push_delete"),
        # #1085: a lease-guarded force push of the replayed tip; the source side is checked against a hex-sha regex
        # (never empty) so the refspec can only update `refs/heads/<branch>`, never delete it
        ("skills/sigma-loop/scripts/feature_upkeep_pass.py", "push", "colon_refspec"),
    }, found


def test_the_feature_rebase_colon_refspec_source_side_is_the_fixed_literal_HEAD():
    """The `feature_rebase.py` colon-refspec site is safe for a DIFFERENT reason than the
    `define.py` one (proven behaviorally below): its refspec's source side is the fixed literal
    `"HEAD"`, never a runtime variable, so it can never go empty regardless of any other state.
    Inspects the exact scannable line the inventory pin above already located -- a more direct
    proof than exercising the whole rebase pass end-to-end and observing the branch survived once."""
    hits = _delete_call_sites(ROOT)
    assert hits is not None
    (line,) = [line for path, _lineno, kind, line in hits
               if kind == "colon_refspec" and path.name == "feature_rebase.py"]
    assert '"HEAD:refs/heads/' in line, line


def test_the_release_manifest_colon_refspec_source_side_is_the_fixed_literal_HEAD():
    """`publish_to_ledger_branch` is safe for the same reason `feature_rebase.py`'s site is: the
    refspec's source side is the fixed literal `"HEAD"`, never a runtime variable, so it can never
    go empty and become a delete. Its destination is the `sdlc-ledger` ops branch rather than any
    `feature/<name>` ref, so it cannot reach a unit branch at all -- but the source-side proof is
    what makes it safe independent of that."""
    hits = _delete_call_sites(ROOT)
    assert hits is not None
    (line,) = [line for path, _lineno, kind, line in hits
               if kind == "colon_refspec" and path.name == "release_manifest.py"]
    assert '"HEAD:refs/heads/' in line, line
    assert "refs/heads/sdlc-ledger" in line, line


# =====================================================================================================
# Part 2 -- behavioral proof for the two structurally-live call sites, plus the two build sites
# =====================================================================================================

def _runner(handlers):
    """First substring match wins -- same contract as `tests/test_work.py`'s own `_runner`,
    duplicated here (no shared test-helper module in this repo)."""
    calls = []

    def run(cwd, argv):
        line = " ".join(str(a) for a in argv)
        calls.append(line)
        for token, resp in handlers:
            if token in line:
                if isinstance(resp, Exception):
                    raise resp
                return resp(line) if callable(resp) else resp
        if "rev-parse HEAD" in line:
            return HEAD_SHA
        if "remote get-url" in line:
            return REMOTE_URL
        return ""

    run.calls = calls
    return run


def _sdlc(tmp_path, config=None):
    state = _load("state")
    d = tmp_path / ".sdlc"
    (d / "state").mkdir(parents=True)
    (d / "config.json").write_text(json.dumps(config or ON))
    state.start_run(str(d))
    return str(d)


def _started(sdlc_dir, goal="0001-x.md", pr="7", base="main"):
    wt = pathlib.Path(sdlc_dir).parent / ".sdlc" / "work" / "0001-x"
    wt.mkdir(parents=True, exist_ok=True)
    work._save(sdlc_dir, goal, {"worktree": str(wt), "branch": "sdlc/0001-x", "base": base,
                                "remote": "origin", "pr": pr})
    return goal


def _pr_state(state="OPEN", auto_merge=False):
    """#895 4a-1: the REST `pulls/7` body `_open_pr_refusal` / `_pr_merged` now read (via gh_api)."""
    return prfake.rest_pull(state=state, autoMergeRequest=({"merge_method": "squash"} if auto_merge else None))


def _view(mergeable="MERGEABLE", status="CLEAN", checks=(("ci", "SUCCESS"),), head=HEAD_SHA):
    """#895 4a-2: the gate's REST reads (pull, check-runs, status) as one handler list; its pull body also
    answers merge_rights' fork read, so it goes FIRST and stands beside `_perm()`, never `_rights()`."""
    return prfake.gate_handlers(prfake.rest_pull(mergeable=mergeable, mergeStateStatus=status, headRefOid=head),
                                checks, (), head)


def _perm(perm="ADMIN"):
    return [("viewerPermission", perm), ("nameWithOwner", "acme/app")]


def _default_branch(name="main"):
    return [("default_branch", name)]


def _evidence(sdlc_dir, goal="0001-x.md", exit_code=0, age=0):
    state = _load("state")
    ev = state.evidence_path(sdlc_dir, goal)
    ev.parent.mkdir(parents=True, exist_ok=True)
    at = state.load_cursor(sdlc_dir)["run_started_at"] - age
    ev.write_text(json.dumps({"command": "pytest", "exit": exit_code, "at": at, "tail": []}))


_UNIT_BRANCH = "feature/guard-test-unit"


def _delete_shaped(call):
    return ("--delete" in call or "DELETE" in call or re.search(r"\bbranch\s+-[dD]\b", call)
            or re.search(r"\bpush\b.*\s-d\b", call) or re.search(r"(?:^|\s):refs/", call)
            or re.search(r"\bupdate-ref\s+(?:--no-deref\s+)?(?:-d|--delete)\b", call))


def test_finish_never_names_the_feature_branch_when_the_goal_declared_a_unit(tmp_path):
    """The goal this whole file exists to prove for `finish()`: even with a unit-based goal (`base`
    is a `feature/<name>` branch), only the goal's OWN throwaway `sdlc/<goal>` branch is ever
    deleted -- `finish()` never even reads or names `base` anywhere."""
    d = _sdlc(tmp_path)
    goal = _started(d, base=_UNIT_BRANCH)
    run = _runner([(prfake.pull_get(7), _pr_state("MERGED"))])
    out = work.finish(d, ON, goal, run=run)
    # existing behavior untouched: the goal's own branch really is deleted, both sides --
    # otherwise the assertion below would pass vacuously (nothing delete-shaped ran at all).
    assert "deleted branch sdlc/0001-x" in out, out
    assert any(c == "git branch -D sdlc/0001-x" for c in run.calls), run.calls
    assert any(c == "gh api -X DELETE repos/{owner}/{repo}/git/refs/heads/sdlc/0001-x"
              for c in run.calls), run.calls
    # the actual invariant: the feature branch is never even named, delete-shaped call or not.
    assert not any(_UNIT_BRANCH in c for c in run.calls), run.calls


def test_merge_never_deletes_the_feature_branch_base_after_a_direct_landing(tmp_path):
    """`merge()` DOES legitimately mention `base` for an unrelated, non-delete reason (closing the
    goal's issue itself when the base can't -- docs/branching-model.md S13a), so the correct
    assertion is narrower than "base is never mentioned": no DELETE-SHAPED call ever names it."""
    d = _sdlc(tmp_path)
    goal = _started(d, base=_UNIT_BRANCH)
    _evidence(d, goal)
    run = _runner(_view() + _perm() + _default_branch("main"))
    out = work.merge(d, ALWAYS_GITHUB, goal, run=run, sleep=NOSLEEP)
    assert out.startswith("PR #7 merged"), out
    # existing behavior untouched: the goal's own branch really is deleted.
    assert any(c == "gh api -X DELETE repos/{owner}/{repo}/git/refs/heads/sdlc/0001-x"
              for c in run.calls), run.calls
    # the actual invariant.
    assert not any(_UNIT_BRANCH in c for c in run.calls if _delete_shaped(c)), run.calls


def test_resolved_base_never_returns_a_falsy_value(tmp_path):
    """The ONE colon-refspec build site whose source side is a runtime variable is `define.py`'s
    `_step_branch`, via `ctx.resolved_base()`. This is what makes that pinned site safe: it either
    returns a truthy, non-empty base, or refuses loudly (`_Refused`) -- it can never silently hand
    back `""`/`None`, the one value that would turn `"%s:refs/heads/%s" % (base, ctx.branch)` into
    the exact delete shape `define.py`'s own docstring names."""
    def ctx(config, base, run):
        return define._Ctx(str(tmp_path / ".sdlc"), "widget", "feature", config, run,
                           str(tmp_path), None, base)

    run_home = _runner([("rev-parse --abbrev-ref HEAD", "main\n")])
    explicit = ctx({}, "feature/explicit", run_home)
    assert explicit.resolved_base() == "feature/explicit"

    configured = ctx({"work": {"base": "feature/configured"}}, None, run_home)
    assert configured.resolved_base() == "feature/configured"

    from_head = ctx({}, None, run_home)
    assert from_head.resolved_base() == "main"

    detached_run = _runner([("rev-parse --abbrev-ref HEAD", "HEAD\n")])
    detached = ctx({}, None, detached_run)
    try:
        result = detached.resolved_base()
    except define._Refused:
        pass
    else:
        assert result, "resolved_base() returned a falsy value instead of refusing: %r" % (result,)

    empty_head_run = _runner([("rev-parse --abbrev-ref HEAD", "")])
    empty_head = ctx({}, None, empty_head_run)
    try:
        result = empty_head.resolved_base()
    except define._Refused:
        pass
    else:
        assert result, "resolved_base() returned a falsy value instead of refusing: %r" % (result,)
