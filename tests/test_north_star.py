"""issue #1778: EXECUTED tests for skills/sigma-loop/scripts/north_star.py.

The defect these pin is a check that was never seen to fail. `sigma-research` §4 and
`sigma-plan-review` §4 both gated their strategy/architecture pass on `.sdlc/context/north-star.md`
existing in the CURRENT directory; the loop runs both phases from inside the goal's worktree, and
`.gitignore` excludes `.sdlc/*`, so that file is never there. Worse, `sigma-plan-review` read the
absence as "no north-star = drop-in project: skip this check; it's a no-op" — reporting NOT
APPLICABLE where the truth was I COULD NOT LOOK.

So the tests that matter are the ones that could not pass before: a REAL `git worktree` is built here
(not simulated with a stub, which is what would have let the original bug through), the project's
north-star is left in the main checkout only, and the check is run FROM the worktree. And the
drop-in case is exercised in both of its real forms — a git repo with no `.sdlc/context/`, and a
plain directory that is not a repo at all — because a fix that turned every drop-in project into a
nagging `unreachable` would be its own regression.

Lives in the root `tests/`: the module under test is plugin surface under `skills/`, which may
never import the private side (tests/test_import_boundary.py).
"""
import importlib.util
import os
import pathlib
import re
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "sigma-loop" / "scripts" / "north_star.py"


def _load():
    spec = importlib.util.spec_from_file_location("north_star", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ns = _load()

NORTH_STAR_BODY = "## Architecture\n1. skills/ never imports vendor/.\n"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True).stdout


def _repo(path, north_star=True):
    """A real git repo with one commit, and (optionally) a GITIGNORED `.sdlc/context/north-star.md`
    — the same shape this kit ships: the north-star lives only in the main checkout's working tree,
    never in the index, so no worktree cut from a commit can ever contain it."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path.parent, "init", "-q", "-b", "main", str(path))
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    (path / ".gitignore").write_text(".sdlc/\n", encoding="utf-8")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(path, "add", ".gitignore", "README.md")
    _git(path, "commit", "-q", "-m", "init")
    if north_star:
        ctx = path / ".sdlc" / "context"
        ctx.mkdir(parents=True)
        (ctx / "north-star.md").write_text(NORTH_STAR_BODY, encoding="utf-8")
    return path


def _worktree(repo, at):
    _git(repo, "worktree", "add", "-q", "-b", "sdlc/1778", str(at), "HEAD")
    return at


# --- the defect itself -----------------------------------------------------------------------


def test_a_goal_worktree_resolves_the_main_checkouts_north_star(tmp_path):
    """THE bug. From inside a worktree the file is genuinely not there, and the check must still
    find the project's real one."""
    repo = _repo(tmp_path / "proj")
    wt = _worktree(repo, tmp_path / "proj" / ".sdlc" / "work" / "1778")

    # the premise, asserted rather than assumed: the worktree really has no north-star of its own
    assert not (wt / ".sdlc" / "context" / "north-star.md").exists()

    state, detail = ns.check(wt)
    assert state == ns.PRESENT, (state, detail)
    assert pathlib.Path(detail) == repo / ".sdlc" / "context" / "north-star.md"
    assert pathlib.Path(detail).read_text(encoding="utf-8") == NORTH_STAR_BODY


def test_a_worktree_placed_outside_the_main_checkout_resolves_too(tmp_path):
    """`work.worktree_dir` is configurable, so the worktree is not always under the main checkout.
    Resolution must come from git, not from assuming the `.sdlc/work/<goal>` layout."""
    repo = _repo(tmp_path / "proj")
    wt = _worktree(repo, tmp_path / "elsewhere" / "1778")
    state, detail = ns.check(wt)
    assert state == ns.PRESENT
    assert pathlib.Path(detail) == repo / ".sdlc" / "context" / "north-star.md"


def test_the_main_checkout_still_resolves_to_itself(tmp_path):
    """The behaviour that already worked must not move."""
    repo = _repo(tmp_path / "proj")
    state, detail = ns.check(repo)
    assert state == ns.PRESENT
    assert pathlib.Path(detail) == repo / ".sdlc" / "context" / "north-star.md"


# --- the drop-in project, in both of its real forms ------------------------------------------


def test_a_repo_with_no_north_star_is_absent_not_unreachable(tmp_path):
    repo = _repo(tmp_path / "proj", north_star=False)
    state, detail = ns.check(repo)
    assert state == ns.ABSENT
    assert detail.endswith(os.path.join(".sdlc", "context", "north-star.md"))


def test_a_worktree_of_a_repo_with_no_north_star_is_absent_too(tmp_path):
    """Resolution succeeding and finding nothing is a SKIP, not a gap — otherwise adopting this fix
    would start nagging every drop-in project on every plan-review."""
    repo = _repo(tmp_path / "proj", north_star=False)
    wt = _worktree(repo, tmp_path / "proj" / ".sdlc" / "work" / "1778")
    state, _ = ns.check(wt)
    assert state == ns.ABSENT


def test_a_plain_directory_that_is_not_a_repo_is_absent(tmp_path):
    """No `.git` anywhere above means no linked worktree can exist, so the local answer IS the
    project's answer. Reporting `unreachable` here would nag a repo-less drop-in forever."""
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    state, _ = ns.check(plain)
    assert state == ns.ABSENT


# --- the third value: present-but-unreachable -------------------------------------------------


def test_a_dangling_worktree_pointer_is_unreachable_never_absent(tmp_path):
    """A `.git` FILE is a linked worktree or submodule. If git cannot say where its real checkout
    is, we do not know whether a north-star exists — and that is not the same as knowing there is
    none. This is the state the whole three-valued design exists for."""
    orphan = tmp_path / "orphan"
    orphan.mkdir()
    (orphan / ".git").write_text("gitdir: %s\n" % (tmp_path / "gone" / "worktrees" / "x"),
                                 encoding="utf-8")
    state, reason = ns.check(orphan)
    assert state == ns.UNREACHABLE
    assert "file" in reason and str(orphan) in reason


def test_a_north_star_that_cannot_be_read_is_unreachable_not_present(tmp_path):
    """"It is there and I cannot open it" is the literal case this module is named for. A stat-only
    check would call this PRESENT and hand the reviewer a file it cannot use."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root ignores the permission bits this asserts on")
    repo = _repo(tmp_path / "proj")
    target = repo / ".sdlc" / "context" / "north-star.md"
    target.chmod(0o000)
    try:
        state, reason = ns.check(repo)
    finally:
        target.chmod(0o644)
    assert state == ns.UNREACHABLE
    assert "could not be read" in reason


# --- resolution details --------------------------------------------------------------------


def test_project_root_falls_back_to_the_toplevel_when_the_common_dir_is_not_dot_git(monkeypatch,
                                                                                    tmp_path):
    """A SUBMODULE's common dir is `<super>/.git/modules/<name>` — whose parent is not a working
    tree at all. The basename check must send that case to `--show-toplevel` instead."""
    top = tmp_path / "super" / "sub"
    top.mkdir(parents=True)

    class _Proc:
        returncode = 0
        stdout = "%s\n%s\n" % (tmp_path / "super" / ".git" / "modules" / "sub", top)
        stderr = ""

    monkeypatch.setattr(ns.subprocess, "run", lambda *a, **k: _Proc())
    root, reason = ns.project_root(top)
    assert reason is None
    assert root == top


def test_project_root_keeps_a_real_checkout_when_git_is_unavailable(tmp_path):
    """`.git` as a DIRECTORY proves this is not a linked worktree, so the local tree is authoritative
    even with no usable git. Without this, a missing git binary would turn every plan-review on
    every ordinary checkout into `unreachable`."""
    repo = _repo(tmp_path / "proj")
    boom = tmp_path / "empty-path"
    boom.mkdir()
    env_path = os.environ.get("PATH", "")
    os.environ["PATH"] = str(boom)
    try:
        root, reason = ns.project_root(repo / "does-not-need-to-exist" / "..")
    finally:
        os.environ["PATH"] = env_path
    assert reason is None
    assert pathlib.Path(root).resolve() == repo.resolve()


# --- the shipped gesture, exactly as the SKILL.md files print it -----------------------------


def test_cli_from_a_worktree_prints_present_and_exits_zero(tmp_path):
    repo = _repo(tmp_path / "proj")
    wt = _worktree(repo, tmp_path / "proj" / ".sdlc" / "work" / "1778")
    proc = subprocess.run([sys.executable, str(SCRIPT)], cwd=str(wt),
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    token, _, path = proc.stdout.strip().partition(" ")
    assert token == "present"
    assert pathlib.Path(path) == repo / ".sdlc" / "context" / "north-star.md"


def test_cli_exits_zero_on_every_verdict_including_unreachable(tmp_path):
    """A resolver that exits non-zero becomes a gate every caller defends against with `|| true`,
    and the first `set -e` shell to forget kills the phase. The verdict is the stdout token."""
    orphan = tmp_path / "orphan"
    orphan.mkdir()
    (orphan / ".git").write_text("gitdir: %s\n" % (tmp_path / "gone"), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(SCRIPT), str(orphan)],
                          capture_output=True, text=True)
    assert proc.returncode == 0
    assert proc.stdout.split(" ", 1)[0] == "unreachable"


# --- #2147: the OTHER skills that ground themselves in the north-star -------------------------
#
# #1778 fixed two. Nine more read the same file, and `.gitignore`'s `.sdlc/*` hides it from every
# one of them that the loop runs after `sigma-loop/SKILL.md` 3a cuts the worktree. The split below
# is the whole point: a skill that emits a VERDICT a human acts on has to tell `absent` from
# `unreachable`, and a skill that only wants the path does not.

#: Emit a verdict grounded in the north-star, so ABSENT-is-not-PASS applies: they MUST name all
#: three states. (`sigma-retro`'s grade feeds `loop.py record`; `sigma-security-review`'s severities
#: feed `sigma-review`'s Blocking rule; `sigma-contract-check` has an explicit FROZEN-contract gate.)
_GATES = ("sigma-research", "sigma-plan-review", "sigma-review", "sigma-retro", "sigma-context",
          "sigma-security-review", "sigma-contract-check")

#: Grounding only - they get no reviewer brief and assert nothing about the north-star, so they need
#: the PATH and nothing more. Three-state prose here would be ceremony.
_POINTERS = ("sigma-migration-check", "sigma-debug", "sigma-brainstorm")

_GROUNDING = _GATES + _POINTERS

#: Where naming `.sdlc/context/north-star.md` in prose is a DEFECT - it is the path that does not
#: exist in a worktree, and a model that sees it will open it instead of resolving. Excluded, with
#: reasons: `sigma-research`/`sigma-plan-review` cite it while EXPLAINING the defect; `sigma-retro:69`
#: proposes an edit *to* the file; `sigma-implement` only ever cites it (see the pin below).
_MUST_NOT_NAME_THE_BARE_PATH = ("sigma-review", "sigma-context", "sigma-security-review",
                                "sigma-contract-check", "sigma-migration-check", "sigma-debug")

BARE_PATH = ".sdlc/context/north-star.md"

#: The literal gesture, as a SKILL.md prints it. Extracted rather than hardcoded so this file cannot
#: drift from the docs - AGENTS.md: "copy the gesture out of the docs, run THAT, and see it red".
_GESTURE = re.compile(r'python3 "\$\{CLAUDE_SKILL_DIR\}/([A-Za-z0-9_./-]*north_star\.py)"')


def _skill_text(skill):
    return (ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("skill", _GROUNDING)
def test_every_grounding_skill_prints_the_resolver_gesture(skill):
    assert "north_star.py" in _skill_text(skill), (
        "%s grounds itself in the north-star but never says how to LOCATE it" % skill)


@pytest.mark.parametrize("skill", _MUST_NOT_NAME_THE_BARE_PATH)
def test_the_bare_path_is_gone_from_every_skill_that_only_wanted_to_read_it(skill):
    """The control rev 1 of the plan lacked. Banning the PHRASE ``If `...` exists`` was vacuous -
    it occurs in exactly one file. The bare PATH is what a model actually follows, and every skill
    here carried it exactly once before this change, so this assertion is red before the fix."""
    assert BARE_PATH not in _skill_text(skill), (
        "%s still names the bare path; from a goal worktree that resolves to nothing" % skill)


def test_sdlc_retro_keeps_only_its_one_legitimate_bare_path():
    """Not a blanket ban: `sigma-retro` PROPOSES an edit to the north-star, and naming the file it
    would edit is correct. Exactly one such mention may survive - the grounding read must not."""
    assert _skill_text("sigma-retro").count(BARE_PATH) == 1


@pytest.mark.parametrize("skill", _GATES)
def test_every_gate_branches_on_all_three_verdicts(skill):
    """Supersedes the two-skill version this file shipped for #1778. Prose naming only two outcomes
    puts the `no-op` reading straight back, which is the #1778 defect itself. Host-agnostic: this is
    the same text Cursor and Codex read, since neither has hooks. Asserted one-directionally - a
    pointer that happens to mention a state is not a bug, a gate that stops naming one is."""
    text = _skill_text(skill)
    for verdict in ("present", "absent", "unreachable"):
        assert verdict in text, (skill, verdict)


@pytest.mark.parametrize("skill", _GROUNDING)
def test_the_gesture_each_skill_prints_actually_runs_from_a_worktree(skill, tmp_path):
    """THE control. Not "is the string there" but "does the command the doc gives you WORK from the
    place the loop runs it". Catches a wrong `../` depth, a typo'd filename, a moved script - none of
    which any grep-shaped assertion above can see."""
    matches = _GESTURE.findall(_skill_text(skill))
    # An empty match set makes the loop below a green body that ran zero times.
    assert len(matches) >= 1, "%s prints no runnable resolver gesture" % skill

    repo = _repo(tmp_path / "proj")
    wt = _worktree(repo, tmp_path / "proj" / ".sdlc" / "work" / "2147")
    for rel in matches:
        # `${CLAUDE_SKILL_DIR}` is the skill's own directory; resolve the literal against it.
        target = (ROOT / "skills" / skill / rel).resolve()
        assert target.is_file(), "%s points at %s, which does not exist" % (skill, rel)
        proc = subprocess.run([sys.executable, str(target)], cwd=str(wt),
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        token, _, path = proc.stdout.strip().partition(" ")
        assert token == "present", (skill, proc.stdout)
        assert pathlib.Path(path) == repo / ".sdlc" / "context" / "north-star.md"


#: `sigma-implement` is deliberately NOT in `_GROUNDING`: all three of its north-star mentions QUOTE
#: the rule inline, so nothing is read and nothing can silently fail. Pinned so a later consistency
#: sweep cannot convert a citation into a phantom read of a path that is not there.
_IMPLEMENT_CITATIONS = (
    "A claim in prose is not evidence. Execute it",
    "2026-08-08 entry records a mutation test",
    "untrusted by default",
)


def test_sdlc_implement_still_cites_the_north_star_inline_rather_than_reading_it():
    text = _skill_text("sigma-implement")
    for quoted in _IMPLEMENT_CITATIONS:
        assert quoted in text, (
            "sigma-implement no longer quotes %r inline - if it now sends the reader to the file "
            "instead, it needs the resolver like every skill in _GROUNDING" % quoted)
    assert "north_star.py" not in text, (
        "sigma-implement gained a resolver call; if it now READS the north-star it belongs in "
        "_GROUNDING, and this pin is the wrong guard for it")


# --- #993 (decision rubric slice 3): per-document resolution -----------------------------------

def test_resolve_doc_types_each_state_and_refuses_escape(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("alpha\n", encoding="utf-8")
    assert ns.resolve_doc(tmp_path, "docs/a.md")[0] == ns.PRESENT
    assert ns.resolve_doc(tmp_path, "docs/none.md")[0] == ns.ABSENT
    for bad in ("../outside.md", "/etc/hosts", "docs/../../x.md", "", "a\x00b"):
        state, why = ns.resolve_doc(tmp_path, bad)
        assert state == ns.UNREACHABLE and why, bad
