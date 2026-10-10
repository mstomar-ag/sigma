"""Where the project's north-star actually IS — and "there is none" told apart from "I could not look".

THE DEFECT THIS EXISTS FOR (#1778). `sigma-research` §4 and `sigma-plan-review` §4 both gate their
strategy/architecture check on a bare `.sdlc/context/north-star.md` exists?` against the CURRENT
DIRECTORY. The loop runs both phases from inside the goal's WORKTREE (`sigma-loop/SKILL.md` 3a: "do
every edit for this goal inside that worktree"), and `.gitignore` excludes `.sdlc/*` — so that file
is never in a worktree. Measured on this repo the day the fix was written: main checkout
`.sdlc/context/north-star.md` = 11,523 bytes; `.sdlc/work/1778/.sdlc/` = `changelog-fragments/` and
`design/` only, no `context/` at all.

And it is worse than a no-op, which is the whole reason this module returns THREE values rather than
a boolean. `sigma-plan-review` read that absence as "no north-star = drop-in project: skip this check;
it's a no-op" — i.e. it reported NOT APPLICABLE when the truth was I COULD NOT LOOK. That is the
repo's ABSENT-is-not-PASS rule broken in its purest form, so:

    present      a north-star was found and read      -> run the check
    absent       we know where to look; nothing there -> skip, exactly as a drop-in project always did
    unreachable  we could not establish where to look -> NEITHER of the above; say so in the verdict

RESOLVE, DON'T REFUSE. A review gate that hard-refuses on a diagnostic failure converts one missing
binary into a fleet-wide stall on every plan-review forever, which AGENTS.md RESILIENCY forbids
("recovery must not itself be an outage"). The refusal that IS correct here is refusing to CLAIM the
check passed: `unreachable` is a stated gap the verdict has to carry, the same shape
`review_context._missing` already uses ("a review missing a required input reads ABSENT, never
PASS"). So this module never raises and never exits non-zero on a resolution failure — it types it.

HOW RESOLUTION WORKS. `git rev-parse --git-common-dir` is the primitive: a linked worktree's own
`.git` is a text FILE pointing at the main checkout's `.git`, and the common dir is that main `.git`
(absolute), while an ordinary clone answers with its own `.git`. Its parent is therefore the working
tree that owns `.sdlc`. Confirmed live from this goal's worktree: `--git-common-dir` ->
`/Users/.../sigma/.git`, `--show-toplevel` -> `/Users/.../sigma/.sdlc/work/1778`. A SUBMODULE's
common dir is `<super>/.git/modules/<name>`, whose parent is not a working tree at all — hence the
basename check, which falls back to `--show-toplevel` (the submodule's own checkout, which is the
right root for it).

WHEN GIT ITSELF CANNOT ANSWER, the `.git` entry's own TYPE decides, and this is why `unreachable` is
a state that can actually occur rather than dead code:
  * no `.git` anywhere above us      -> not a repo, so no worktree can exist -> the local tree IS the
                                        project; a drop-in with no north-star reads `absent`, not a nag
  * `.git` is a DIRECTORY            -> an ordinary checkout, never a linked worktree -> same
  * `.git` is a FILE                 -> a linked worktree or submodule, and the one thing that could
                                        say where the real tree is just failed -> `unreachable`

Host-agnostic (`git` + stdlib, no hooks), zero deps, ASCII output.

    python3 north_star.py [<dir>]      # prints "present <path>" | "absent <path>" | "unreachable <why>"
"""
import os
import pathlib
import subprocess
import sys

#: The one location the whole kit agrees on, relative to a project's `.sdlc` dir. Stated once here so
#: the skills' prose and `review_context._PROJECT_DOCS` cannot drift from what this module probes.
REL_PATH = os.path.join("context", "north-star.md")

PRESENT = "present"
ABSENT = "absent"
UNREACHABLE = "unreachable"


def _git_anchor(start):
    """(dir_holding_dot_git, is_file) for the nearest ancestor of `start` that has a `.git`, else
    (None, False). Pure filesystem — this is the fallback that runs precisely when `git` could not,
    so it must not need `git`."""
    try:
        here = pathlib.Path(start).resolve()
    except OSError:
        return None, False
    for d in (here, *here.parents):
        dot = d / ".git"
        try:
            if dot.is_file():
                return d, True
            if dot.is_dir():
                return d, False
        except OSError:                 # an unreadable ancestor: keep walking, never raise
            continue
    return None, False


def project_root(start="."):
    """(root, None) — the working tree that owns `.sdlc` — or (None, reason) when it cannot be told.

    `root` is a `pathlib.Path`. Never raises: every failure is a reason string the caller types."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--git-common-dir", "--show-toplevel"],
            capture_output=True, text=True, timeout=10)
        ok = proc.returncode == 0
        lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()] if ok else []
    except Exception as exc:            # noqa: BLE001 - git missing, wedged, or killed; all typed below
        ok, lines, why = False, [], "%s: %s" % (type(exc).__name__, exc)
    else:
        why = (proc.stderr or "").strip().splitlines()[-1] if not ok and proc.stderr.strip() else \
            ("git rev-parse exited %d" % proc.returncode if not ok else "")

    if ok and len(lines) >= 2:
        # `--git-common-dir` is RELATIVE in an ordinary checkout (".git") and absolute in a worktree,
        # so it is resolved against `start` — the same normalisation a downstream profile reader
        # already performs on this exact primitive.
        common = pathlib.Path(os.path.abspath(os.path.join(str(start), lines[0])))
        if common.name == ".git":
            return common.parent, None
        return pathlib.Path(lines[1]), None     # submodule: its own checkout is its root

    # git could not answer. The `.git` entry's TYPE is what decides whether that matters.
    anchor, is_file = _git_anchor(start)
    if anchor is None:
        try:
            return pathlib.Path(start).resolve(), None      # not a repo: nothing can be linked here
        except OSError as exc:
            return None, "cannot resolve %r: %s" % (str(start), exc)
    if not is_file:
        return anchor, None                                 # a real checkout, never a linked worktree
    return None, ("`.git` at %s is a file (a linked worktree or submodule) and git could not say "
                  "where its real checkout is%s" % (anchor, (": " + why) if why else ""))


def check(start="."):
    """(state, detail) where state is PRESENT / ABSENT / UNREACHABLE.

    `detail` is the north-star's path for PRESENT, the path that was looked at for ABSENT, and the
    reason for UNREACHABLE. Reading (not just stat-ing) is deliberate: a north-star that exists and
    cannot be OPENED is the "present but I could not look" case this module was written for, and a
    stat alone would report it PRESENT and then hand the checker a file it cannot use."""
    root, reason = project_root(start)
    if root is None:
        return UNREACHABLE, reason
    path = root / ".sdlc" / REL_PATH
    try:
        if not path.is_file():
            return ABSENT, str(path)
        path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return UNREACHABLE, "%s exists but could not be read: %s" % (path, exc)
    return PRESENT, str(path)


def resolve_doc(root, rel):
    """(state, detail) for ONE repo-relative reference document named in `context.documents` (#993).

    Same three states as `check`. A path that is not a plain relative string, or that resolves
    (parent steps, absolute, or a link) outside `root`, is UNREACHABLE with the reason and is never
    read: containment is checked on the resolved path, not on the spelling."""
    if not isinstance(rel, str) or not rel.strip() or "\x00" in rel:
        return UNREACHABLE, "refused: not a usable path"
    if os.path.isabs(rel) or rel.startswith(("~", "\\")):
        return UNREACHABLE, "refused: %s is not repo-relative" % rel
    try:
        top = pathlib.Path(root).resolve()
        path = (top / rel).resolve()
        path.relative_to(top)
    except ValueError:
        return UNREACHABLE, "refused: %s leaves the repository" % rel
    except OSError as exc:
        return UNREACHABLE, "%s could not be resolved: %s" % (rel, exc)
    try:
        if not path.is_file():
            return ABSENT, str(rel)
        path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return UNREACHABLE, "%s exists but could not be read: %s" % (rel, exc)
    return PRESENT, str(rel)


USAGE = "usage: north_star.py [start_dir]"


def main(argv):
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    start = argv[1] if len(argv) > 1 else "."
    state, detail = check(start)
    # Always exit 0. A resolver that exits non-zero becomes a gate every caller has to defend against
    # with `|| true`, and the first `set -e` shell to forget turns a diagnostic into a dead phase. The
    # verdict is the first token on stdout; that is the contract.
    print("%s %s" % (state, detail) if detail else state)
    return 0


if __name__ == "__main__":
    # The script-time floor: this run's wall time, recorded to the session resolved at exit. This
    # module has no sibling loader of its own, so the store is loaded here, by path, CLI-only.
    import importlib.util as _ilu, pathlib as _pl
    _spec = _ilu.spec_from_file_location(
        "timing_store", _pl.Path(__file__).resolve().parent / "timing_store.py")
    _ts = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_ts)
    sys.exit(_ts.timed_main(main, sys.argv, "north_star"))
