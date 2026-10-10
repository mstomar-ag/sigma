"""Read-only questions about a rebase that has STOPPED, answered from git, for the unit-conflict work.

A library, loaded by path; no CLI. Every function takes the kit's injected runner `run(cwd, argv) -> stdout`
(raises on a non-zero exit) and a directory, runs only read-only git, and keeps no state. Nothing here reads
configuration, so this module is not a reader of the `upkeep` block and carries no gate: the callers that act on
the answers sit behind the gate.

WHY THIS IS NOT `rebase_stopped`. That predicate answers "is a rebase in progress here", and several other
call sites rely on exactly that. The questions below are different ones: WHICH paths are unmerged, at which
stages, and whether continuing right now would silently drop the commit being replayed.

PATHS ARE NUL-SEPARATED. `ls-files -u -z` prints each path unquoted; the line format C-quotes a non-ASCII
path and the quoted text then matches no pathspec. The reader splits on NUL and on the first tab only, so a
path holding a space, a newline or a quote round-trips.
"""


def stage_map(run, cwd):
    """The unmerged index as `{path: [stage, ...]}` with stages as sorted ints (1 base, 2 ours, 3 theirs).

    Empty when nothing is unmerged. Read from `ls-files -u -z`, never from porcelain, whose rename entries carry a
    second path chunk that reads as a bogus status code. RAISES whatever the runner raises: a measurement that
    could not be made is not "nothing is unmerged"."""
    out = str(run(cwd, ["git", "ls-files", "-u", "-z"]) or "")
    stages = {}
    for entry in out.split("\0"):
        meta, tab, name = entry.partition("\t")
        if not tab or not name:
            continue
        fields = meta.split()
        if len(fields) != 3 or not fields[2].isdigit():
            continue
        stages.setdefault(name, set()).add(int(fields[2]))
    return {name: sorted(found) for name, found in stages.items()}


def conflicted_paths(run, cwd):
    """The unmerged paths, sorted, NUL-safe. Empty means a stop with no conflicting file."""
    return sorted(stage_map(run, cwd))


def stage0_listing(run, cwd):
    """The index at the stop as `{path: (mode, blob)}` for stage 0 only, NUL-safe.

    The baseline for "only the conflicted files changed": taken before anything is resolved, a later listing may
    differ from it only at paths in `stage_map`. Unmerged paths have no stage 0 entry and are absent."""
    out = str(run(cwd, ["git", "ls-files", "-s", "-z"]) or "")
    listing = {}
    for entry in out.split("\0"):
        meta, tab, name = entry.partition("\t")
        fields = meta.split()
        if not tab or not name or len(fields) != 3 or fields[2] != "0":
            continue
        listing[name] = (fields[0], fields[1])
    return listing


def empty_commit_about_to_land(run, cwd):
    """Whether `git rebase --continue`, called right now, would silently DROP the commit being replayed: every
    conflict is resolved (nothing pending) but the staged result is byte-identical to HEAD, so there is nothing
    left for git to commit. This is the test git's own sequencer uses (`is_index_unchanged`): `git diff --cached
    HEAD --name-only` with no path in its output.

    A commit that becomes empty this way is dropped under git's default `--empty=drop` with no message on either
    stream and exit 0, so it has to be caught BEFORE `--continue`; there is nothing to catch afterwards.

    Returns `None` when continuing right now is safe. Otherwise `{"sha", "subject"}` naming the at-risk commit,
    read from `REBASE_HEAD` (valid while the rebase stays stopped). Best-effort naming: an unreadable
    `REBASE_HEAD` still returns `{"sha": "", "subject": ""}` rather than `None`, because "would be empty" is the
    finding that matters and "couldn't name it" is not "safe to proceed".

    UNVERIFIED AT A MERGE STOP (a `--rebase-merges` replay stopping on a merge): git probably still commits a
    merge while `MERGE_HEAD` is present, so this probably over-refuses there. Over-refusing parks; it never drops."""
    staged = run(cwd, ["git", "diff", "--cached", "HEAD", "--name-only"])
    if str(staged or "").strip():
        return None
    try:
        out = run(cwd, ["git", "log", "-1", "--format=%H%x1f%s", "REBASE_HEAD"])
    except Exception:                          # noqa: BLE001 - unnamed is still "would be empty"
        out = ""
    sha, _, subject = str(out or "").partition("\x1f")
    return {"sha": sha.strip(), "subject": subject.strip()}
