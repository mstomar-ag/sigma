"""The public `.gitignore` is pinned in BOTH directions (#2739, Q1-G11).

The snapshot builder ships `public-overrides/.gitignore` as the public tree's root `.gitignore`.
That file excludes `.sdlc/*` and then re-includes the tracked design, changelog-fragment, plan and
research directories -- and re-excludes the runtime subtrees under plans. Git cannot re-include
under an excluded parent, so a single dropped or reordered line silently flips a whole tracked
directory to ignored (or a runtime tree to tracked). These tests ask git itself, with
`check-ignore`, rather than grepping for lines: the file's meaning, not its text, is what ships.

FILE UNDER TEST, by tree (the `public_surface` convention -- `tools/public-manifest.txt` present
means the monorepo): monorepo -> `public-overrides/.gitignore`, NEVER the private root
`.gitignore`, which would silently test the wrong file; public tree -> the root `.gitignore`, which
is where the override lands. Absence in either is a defect (the override is tracked and the builder
refuses a snapshot without it), so there is no skip case, and the node ids and outcomes are
identical in both trees, as the snapshot's collection-equality check requires.

HARNESS: a fresh `git init` under `tmp_path` holding the text as its `.gitignore`, with every
other ignore source neutralised -- `.git/info/exclude` blanked, `-c core.excludesFile=` passed
(measured: `GIT_CONFIG_GLOBAL=/dev/null` alone does NOT disable the default
`$XDG_CONFIG_HOME/git/ignore`), and HOME/XDG pointed at scratch. A global `*.md` ignore can only
fake a RED on the not-ignored direction, never a green, but the test must not depend on the host.

Both directions occur, so neither assertion can be vacuous, and an in-test control (drop
`!.sdlc/plans/`) shows the harness going red. Stdlib and pytest only; module-level tests only.
S2-G3 (#2589) deletes monorepo mode; the manifest branch of `_override_path` dies with it.
"""
import os
import pathlib
import subprocess

import pytest

import public_surface

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: The lines the whole scheme turns on, exactly as they must appear (stripped).
REQUIRED_LINES = (
    ".sdlc/*",
    "!.sdlc/design/",
    "!.sdlc/changelog-fragments/",
    "!.sdlc/plans/",
    "!.sdlc/research/",
    ".sdlc/plans/reconcile/",
    ".sdlc/plans/triage/",
    ".sdlc/plans/scope/",
)

_TRACKED = {
    "design": ".sdlc/design/x.md",
    "changelog-fragments": ".sdlc/changelog-fragments/x.md",
    "plans": ".sdlc/plans/x.md",
    "research": ".sdlc/research/x.md",
}

_RUNTIME = {
    "plans-reconcile": ".sdlc/plans/reconcile/x",
    "plans-triage": ".sdlc/plans/triage/x",
    "plans-scope": ".sdlc/plans/scope/x",
    "state": ".sdlc/state/x",
    "events": ".sdlc/events/x",
}


def _override_path():
    """The shipped `.gitignore` for this tree; fails loudly, naming the path, when it is missing."""
    if ROOT.joinpath(*public_surface.MANIFEST).is_file():
        tree, path = "monorepo (manifest present)", ROOT / "public-overrides" / ".gitignore"
    else:
        tree, path = "public tree (manifest absent)", ROOT / ".gitignore"
    if not path.is_file():
        pytest.fail("%s: the public .gitignore is missing at %s" % (tree, path))
    return path


def _override_text():
    return _override_path().read_text(encoding="utf-8")


def _env(tmp):
    """A git environment that can read only the scratch repo's own `.gitignore`."""
    env = {k: v for k, v in os.environ.items() if k not in public_surface._GIT_REDIRECTS}
    env.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(tmp / "home"),
        "XDG_CONFIG_HOME": str(tmp / "xdg"),
    })
    return env


def _repo(tmp, text):
    """A fresh repository under `tmp` whose only ignore source is `.gitignore` = `text`."""
    repo = tmp / "r"
    repo.mkdir(parents=True)
    proc = subprocess.run(["git", "init", "-q", str(repo)], env=_env(tmp),
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    (repo / ".gitignore").write_text(text, encoding="utf-8")
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text("", encoding="utf-8")
    return repo


def _check(repo, path):
    """Ask git whether `path` is ignored in `repo`: `(rc, source, pattern, shown_path)`.

    `check-ignore -v --non-matching` prints exactly one line, `source:line:pattern<TAB>path`, for
    every path -- `::<TAB>path` when nothing matched -- so a not-ignored verdict is positive
    evidence (the path named with empty source and pattern), not an empty stdout that an
    unrelated failure could also produce. Two traps the caller must not fall into: rc 128 (a
    fatal, e.g. not a repository) is asserted here so it can never read as "not ignored"; and a
    path whose LAST matching rule is a negation (`!pattern`) is reported with rc 0 and that
    `!pattern` on stdout, so an "ignored" verdict must also demand a pattern not starting with `!`
    (see `_assert_ignored`). Exit codes: 0 some path ignored, 1 none ignored, 128 fatal.
    """
    proc = subprocess.run(
        ["git", "-c", "core.excludesFile=", "-C", str(repo), "check-ignore", "-v", "--no-index",
         "--non-matching", "--", path],
        env=_env(repo.parent), capture_output=True, text=True)
    assert proc.returncode in (0, 1), "check-ignore %r: rc %d, stderr %r" % (
        path, proc.returncode, proc.stderr)
    line = proc.stdout.rstrip("\n")
    assert "\n" not in line and "\t" in line, "unexpected check-ignore output %r" % proc.stdout
    source, _lineno, rest = line.split(":", 2)
    pattern, _tab, shown = rest.partition("\t")
    return proc.returncode, source, pattern, shown


def _assert_not_ignored(repo, path):
    got = _check(repo, path)
    assert got == (1, "", "", path), "%s should NOT be ignored; check-ignore said %r" % (path, got)


def _assert_ignored(repo, path):
    rc, source, pattern, shown = _check(repo, path)
    assert rc == 0 and shown == path, "%s should be ignored; check-ignore said %r" % (
        path, (rc, source, pattern, shown))
    assert source == ".gitignore", "%s ignored by %r, not the rendered .gitignore" % (path, source)
    assert not pattern.startswith("!"), "%s matched a re-inclusion %r, so it is NOT ignored" % (
        path, pattern)


def test_override_carries_the_reinclusion_lines():
    text = _override_text()
    assert text.strip(), "%s is empty" % _override_path()
    present = {line.strip() for line in text.splitlines()}
    missing = [line for line in REQUIRED_LINES if line not in present]
    assert not missing, "%s lacks %r" % (_override_path(), missing)


@pytest.mark.parametrize("path", list(_TRACKED.values()), ids=list(_TRACKED))
def test_tracked_sdlc_docs_are_not_ignored(tmp_path, path):
    _assert_not_ignored(_repo(tmp_path, _override_text()), path)


@pytest.mark.parametrize("path", list(_RUNTIME.values()), ids=list(_RUNTIME))
def test_runtime_sdlc_state_is_ignored(tmp_path, path):
    _assert_ignored(_repo(tmp_path, _override_text()), path)


def test_dropping_a_reinclusion_line_flips_plans_to_ignored(tmp_path):
    """The control: the harness goes red on the defect it guards against, not just green."""
    text = _override_text()
    mutated = "".join(line for line in text.splitlines(keepends=True)
                      if line.rstrip("\r\n") != "!.sdlc/plans/")
    assert mutated != text, "the mutation removed nothing; the control is a no-op"
    rc, source, pattern, shown = _check(_repo(tmp_path / "mutated", mutated), ".sdlc/plans/x.md")
    assert (rc, source, pattern, shown) == (0, ".gitignore", ".sdlc/*", ".sdlc/plans/x.md")
    _assert_not_ignored(_repo(tmp_path / "shipped", text), ".sdlc/plans/x.md")


def test_decision_records_are_tracked_and_their_index_and_lock_are_not(tmp_path):
    repo = _repo(tmp_path, _override_text())
    _assert_not_ignored(repo, ".sdlc/decisions/20261010T120000-ab12cd34.json")
    _assert_not_ignored(repo, ".sdlc/decisions/archive-2025.json")
    _assert_ignored(repo, ".sdlc/decisions/_index.json")
    _assert_ignored(repo, ".sdlc/decisions/_index.lock")
