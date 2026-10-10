#!/usr/bin/env python3
"""sigma-rebase, slice 2 (#2305, epic #2303, design #2288 §5): the interactive
conflict-options walker -- the layer #2304's own SKILL.md named as "a later slice" for what
happens once `rebase_brief.attempt_rebase` reports `CONFLICT` (or a prior conflict-walk session was
interrupted mid-resolution).

WHAT THIS IS FOR. `rebase_brief.py` (slice 1) explains a conflict's decision-context and stops --
strictly better than a bare `git` error, but the human still resolves every file by hand. This
module is the resolution layer: for each conflicted file, classify its shape, pull its own
decision-context (reused, never rebuilt), search for where the code may have moved, then offer
four named options and apply whichever is chosen.

REUSED, NEVER REINVENTED:
  * `rebase_brief.conflicted_files` -- the same `git diff --name-only --diff-filter=U` measurement,
    called again each time through the loop rather than cached, so a file resolved this iteration
    never reappears.
  * `rebase_brief.file_context` -- the SAME per-file decision-context assembler slice 1 already
    built (CHANGELOG heading -> PR description -> design doc -> honest "no explanation exists"),
    reused directly for step 2 of the walk. Nothing here re-derives it.
  * `rebase_brief.assemble_brief` / `resolve_remote` / `resolve_base` -- branch/base resolution and
    the brief this walker's own decision-context step needs (`merge_base`, `base_ref`,
    `changelog_entries`), unchanged.
  * `feature_rebase.rebase_stopped` -- the structural, git-native "is a rebase actually stopped
    here" measurement (never exception-text guessing), reused directly for BOTH the resumability
    requirement (design §5's own "on re-invocation... pick the conflict walk back up") and this
    walker's own continue/repeat loop.

THE OURS/THEIRS INVERSION, MADE EXPLICIT ONCE, RELIED ON EVERYWHERE BELOW. During `git rebase`,
HEAD is temporarily the commit being rebased ONTO (the base), and the commit being replayed is the
ORIGINAL branch -- so git's own "ours" (index stage 2) is the BASE's content, and "theirs" (stage
3) is the BRANCH's own content, for every conflict this walker will ever see. Verified against real
git, not assumed: a base-side deletion reports `git status` code `DU` ("deleted by us") with stage
2 absent and stage 3 (the branch's edit) present; a branch-side deletion reports `UD` ("deleted by
them") with stage 3 absent and stage 2 (the base's edit) present. This is why `apply_option` below
needs no per-shape branching for Recreate/Abandon: "the branch's own version" is ALWAYS stage 3 and
"the base's decision" is ALWAYS stage 2, whether the conflict is a content clash or a deletion on
either side.

WHY `git checkout --ours`/`--theirs` RATHER THAN `git show :N:path` + a write: `run`'s
`(cwd, argv) -> stdout` contract strips its output (matching `feature_sync._run` and every runner
in this codebase) -- round-tripping a blob through that would silently drop a trailing newline on
every file it touches. `git checkout --ours/--theirs` has git write its own stored blob straight to
the working tree, byte for byte, so nothing here corrupts a file's ending.

RESUMABILITY (design §5's own last paragraph): `git rev-parse --abbrev-ref HEAD` returns the
literal string "HEAD" while a rebase has HEAD detached -- verified against real git -- so this
module never uses it to resolve which branch a STOPPED rebase belongs to. `_rebase_original_branch`
reads git's own on-disk `<gitdir>/rebase-merge/head-name` (or `rebase-apply/head-name` for the `am`
backend) instead, the same directory `feature_rebase.rebase_stopped` already asks about --
structural, never guessed, exactly this codebase's own standing rule for telling a conflict apart
from a guess.

TESTABILITY (design's own framing: "a function that can be driven both by a real interactive CLI
loop AND by tests"): `walk_conflicts(run, cwd, brief, decide, remote)` is the one entry point every
caller uses. `decide` is an injected callable -- `(FileConflictState) -> {"option": ..., "candidate": ...}`
-- never a hardcoded `input()` scattered through the walking logic. `main()`'s own `walk` subcommand
builds a real interactive `decide` from `input()`/`print()` (this module IS the interactive layer,
unlike `rebase_brief.py`'s non-interactive `brief`/`rebase` subcommands); a test supplies a canned
or queue-driven one instead. Neither caller touches the walking loop itself.

CLI: `conflict_walk.py walk <sdlc_dir> [branch]` -- resumes or walks a stopped rebase's conflicts
to completion (or an explicit abort), interactively.
"""
import importlib.util
import pathlib
import re
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_LOOP_SCRIPTS = _HERE.parent.parent / "sigma-loop" / "scripts"


def _load(name, directory=None):
    """Import a sibling script by path -- the kit's standard zero-install module loader
    (`rebase_brief._load`, `feature_rebase._load`, `review_context._load`)."""
    directory = pathlib.Path(directory) if directory else _HERE
    spec = importlib.util.spec_from_file_location(name, directory / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


feature_rebase = _load("feature_rebase", _LOOP_SCRIPTS)
conflict_state = _load("conflict_state", _LOOP_SCRIPTS)
rebase_brief = _load("rebase_brief", _HERE)
state = _load("state", _LOOP_SCRIPTS)

# --------------------------------------------------------------------------- conflict shape (§5.1)

#: git's own porcelain v1 vocabulary (`git status --porcelain=v1`), read verbatim -- see the module
#: docstring for the ours/theirs inversion these labels are already correct under, and never
#: adjusted here into a "friendlier" but unverifiable third meaning.
DELETED_BY_US = "deleted-by-us"
DELETED_BY_THEM = "deleted-by-them"
CONTENT = "content"

_SHAPE_CODES = {"DU": DELETED_BY_US, "UD": DELETED_BY_THEM}

# --------------------------------------------------------------------------- the four options (§5.4)

RECREATE = "recreate"
FOLLOW = "follow"
ABANDON = "abandon"
MANUAL = "manual"
#: Not one of the four named options -- available at any point per design §5's own "a bail-out at
#: any point runs `git rebase --abort`" -- but it flows through the same `decide()` return shape.
ABORT = "abort"

# --------------------------------------------------------------------------- walk outcomes

DONE = "done"
NOTHING_TO_DO = "nothing-to-do"
ABORTED = "aborted"
FAILED = "failed"
#: #2318: every conflict was resolved, but committing right now would produce an EMPTY commit --
#: `git rebase --continue` silently DROPS one of these (git's own `--empty=drop` default, no
#: message on either stream, exit 0) rather than erroring, so it can never surface through
#: `FAILED`'s own except-and-report shape. Caught BEFORE that call, never after -- see
#: `_empty_commit_about_to_land`.
EMPTY_AFTER_RESOLVE = "empty-after-resolve"

#: D-1's own confidence bar, an implementation judgment the design leaves open (§5 step 4: "only
#: when the search found a plausible destination"): more hits than this means the symbol is too
#: generic to trust (it matched too many files to name a real destination), so the search
#: degrades to "not plausible" -- empty -- rather than presenting a noisy, unreliable list.
_MAX_PLAUSIBLE_CANDIDATES = 3

#: #2322: suffixes this repo's own move-search treats as NEVER a genuine "follow the move"
#: destination. A symbol's own DEFINITION never lives in one of these -- a `git log -S` hit here is
#: always a MENTION (a CHANGELOG.md entry naming the symbol in prose, a lockfile, a data/config
#: file) never the code that moved -- so a doc/data/config/lock file must not win the ranked
#: preview slot `_print_conflict_state` shows ahead of a real code destination. This repo's own
#: real extension mix is the source of truth (`git ls-files | sed 's/.*\.//' | sort -u`), not a
#: generic "is this a code file" classifier: a small DENYLIST, so an extension this list forgot
#: still wins candidacy on its own merits rather than being silently dropped by an allowlist that
#: never heard of it (`_MAX_PLAUSIBLE_CANDIDATES`'s own generic-symbol cap already guards against a
#: noisy result either way).
_NON_CODE_SUFFIXES = (".md", ".mdc", ".tmpl", ".txt", ".json", ".jsonl", ".csv", ".yml", ".yaml",
                     ".toml", ".lock", ".woff2")


def _looks_like_code(path):
    """Whether `path` is even ELIGIBLE to be a "follow the move" destination (#2322) -- see
    `_NON_CODE_SUFFIXES` for what disqualifies it and why. Case-insensitive (`README.MD` is still a
    doc); a path with no recognized non-code suffix at all (including no suffix) passes, since this
    is a denylist, never an allowlist."""
    return not str(path).lower().endswith(_NON_CODE_SUFFIXES)


_DEF_RE = re.compile(
    r"^[ \t]*(?:class|def|function|const|let|var|interface|type|struct|impl|fn)\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_STOPWORDS = {"self", "return", "import", "from", "None", "True", "False", "async", "await",
             "this", "export", "public", "private", "static", "void"}


def classify_conflict(run, cwd, path):
    """One of `CONTENT`/`DELETED_BY_US`/`DELETED_BY_THEM` for `path`, read from git's own porcelain
    status -- never guessed from the working tree's own content (design §5 step 1: "`git status
    --porcelain=v2` / `git ls-files -u`"). Any code outside the two delete shapes (`AA`, `UU`,
    `AU`, `UA`) is a genuine content conflict -- the issue's own three-way classification names no
    fourth shape."""
    out = run(cwd, ["git", "status", "--porcelain=v1", "--", path])
    for line in str(out or "").splitlines():
        if len(line) > 3 and line[3:] == path:
            return _SHAPE_CODES.get(line[:2], CONTENT)
    return CONTENT


def _stage_exists(run, cwd, path, stage):
    try:
        run(cwd, ["git", "cat-file", "-e", ":%d:%s" % (stage, path)])
        return True
    except Exception:                      # noqa: BLE001 - "no entry at this stage" is not an error
        return False


def _stage_blob(run, cwd, path, stage):
    if not _stage_exists(run, cwd, path, stage):
        return None
    try:
        return run(cwd, ["git", "show", ":%d:%s" % (stage, path)])
    except Exception:                      # noqa: BLE001 - fail-open, matching every other reader here
        return None


def conflict_source_text(run, cwd, path, shape):
    """The text `extract_symbol` searches for step 3's "meaningful symbol" (design §5 step 3): the
    literal conflict-marker region for a genuine content conflict (both hunks are right there in
    the working tree), or the surviving side's own content for a delete conflict -- there is no
    marker region when one side is a plain deletion, so the marker scan would find nothing to read.
    Fail-open to `""`, never raises: an unreadable source means no symbol, not a crash."""
    if shape == CONTENT:
        try:
            text = (pathlib.Path(cwd) / path).read_text(encoding="utf-8", errors="replace")
        except Exception:                  # noqa: BLE001
            return ""
        start = text.find("<<<<<<<")
        end = text.find(">>>>>>>")
        if start == -1 or end == -1:
            return text
        line_end = text.find("\n", end)
        return text[start:line_end if line_end != -1 else len(text)]
    # DELETED_BY_US -> stage 2 (base) absent, stage 3 (branch) is the surviving content;
    # DELETED_BY_THEM -> stage 3 absent, stage 2 is the surviving content (see module docstring).
    stage = 3 if shape == DELETED_BY_US else 2
    return _stage_blob(run, cwd, path, stage) or ""


def extract_symbol(text):
    """A meaningful identifier out of `text` for BR-16's `git log -S"<symbol>"` search to run
    against (design §5 step 3). Prefers a definition-shaped line (`def foo`, `class Foo`,
    `function bar`, ...) over a bare identifier -- a defined NAME is what a move actually preserves,
    where an arbitrary token in a diff might not. Falls back to the longest identifier-like token
    when no definition line is present; `None` when the text carries nothing identifier-shaped at
    all (e.g. pure data/config), which callers treat as "no search to run", never a fabricated one."""
    if not text:
        return None
    m = _DEF_RE.search(text)
    if m:
        return m.group(1)
    idents = [i for i in _IDENT_RE.findall(text) if i not in _STOPWORDS]
    return max(idents, key=len) if idents else None


def search_moved_symbol(run, cwd, symbol, exclude_path=None):
    """BR-16's own documented technique (`skills/sigma-research/SKILL.md:54` -- `git log
    -S"<symbol>" --all`), read for every file any such commit ever touched: the candidate
    destinations "follow the move" needs. History order (git log's own newest-first default),
    deduped, excluding the conflicting path itself -- it is not its own destination."""
    if not symbol:
        return []
    out = run(cwd, ["git", "log", "-S", symbol, "--all", "--name-only", "--pretty=format:"])
    seen, candidates = set(), []
    for line in str(out or "").splitlines():
        candidate = line.strip()
        if not candidate or candidate == exclude_path or candidate in seen:
            continue
        seen.add(candidate)
        candidates.append(candidate)
    return candidates


def plausible_move_candidates(run, cwd, symbol, exclude_path):
    """D-1's own confidence bar (design §5 step 4, "only when the search found a plausible
    destination"): a candidate only counts when it still EXISTS in the tree being rebased onto
    right now -- a file the symbol's history once touched but that has since been deleted is not a
    real destination to offer -- AND when it `_looks_like_code` (#2322): a CHANGELOG.md entry (or
    any other doc/data/config/lock file) merely mentioning the symbol in prose is a false-positive
    candidate, never a real move destination, and must not win the ranked preview slot
    `_print_conflict_state` shows ahead of the genuine one. More than `_MAX_PLAUSIBLE_CANDIDATES`
    hits surviving BOTH filters means the symbol is too generic to trust (see that constant) and
    this degrades to `[]` rather than a noisy list."""
    existing = [p for p in search_moved_symbol(run, cwd, symbol, exclude_path)
               if (pathlib.Path(cwd) / p).is_file() and _looks_like_code(p)]
    return existing if 0 < len(existing) <= _MAX_PLAUSIBLE_CANDIDATES else []


def file_conflict_state(run, cwd, brief, path, sdlc_dir=None):
    """Everything design §5 steps 1-4 need to present ONE conflicted file's options: its shape
    (step 1), its own decision-context (step 2) -- reused directly from `rebase_brief`'s snapshot
    machinery (#2321: `reuse_or_snapshot_context`, never `file_context` directly) so a resumed walk
    describes the SAME conflict a possibly-earlier, separate process already detected rather than
    re-deriving against wherever the base has since moved to -- the symbol the "follow the move"
    search ran against and what it found (step 3), and which of the four named options are
    actually on offer right now (step 4) -- `FOLLOW` only when step 3 found a plausible destination
    (D-1); the other three are always available. `sdlc_dir` falsy skips the snapshot store
    entirely (`reuse_or_snapshot_context`'s own degrade), matching this function's pre-#2321
    behaviour for a caller with no `.sdlc` in scope."""
    shape = classify_conflict(run, cwd, path)
    context = rebase_brief.reuse_or_snapshot_context(sdlc_dir, brief["branch"], run, cwd,
                                                      brief["merge_base"], brief["base_ref"], path,
                                                      brief["changelog_entries"])
    symbol = extract_symbol(conflict_source_text(run, cwd, path, shape))
    candidates = plausible_move_candidates(run, cwd, symbol, path) if symbol else []
    options = (RECREATE, ABANDON, MANUAL) + ((FOLLOW,) if candidates else ())
    return {"path": path, "shape": shape, "context": context, "symbol": symbol,
            "candidates": candidates, "options": options}


# --------------------------------------------------------------------------- applying a choice (§5.5)


def _resolve_to_stage(run, cwd, path, stage, why):
    """Resolve `path` to whichever side `stage` names (2 = the base's decision, 3 = the branch's
    own version -- see module docstring), byte-perfect via `git checkout --ours/--theirs` rather
    than a `run`-captured blob (which `.strip()`s, see module docstring). When that stage has no
    entry at all (the side that DELETED the file), the honest resolution is the deletion itself --
    `git rm -f` -- never a fabricated recreation of content that side chose not to keep."""
    if _stage_exists(run, cwd, path, stage):
        run(cwd, ["git", "checkout", "--ours" if stage == 2 else "--theirs", "--", path])
        run(cwd, ["git", "add", "--", path])
        return {"path": path, "action": "checked-out-stage-%d" % stage, "why": why}
    run(cwd, ["git", "rm", "-f", "--", path])
    return {"path": path, "action": "removed", "why": why}


def apply_option(run, cwd, path, option, candidate=None):
    """Apply whichever option `decide` chose for `path` (design §5 step 5), stage the resolved
    path, and report what happened.

    `RECREATE` and `ABANDON` need no per-shape branching -- see the module docstring's ours/theirs
    note -- "the branch's own version" is always stage 3, "the base's decision" is always stage 2,
    whether this conflict is a content clash or a deletion on either side.

    `FOLLOW` never writes the candidate destination itself (SAFETY: there is no verified 3-way
    merge target at an arbitrary file this walker did not itself track a conflict for -- silently
    splicing text into unrelated code is exactly the "weak guarantee" AGENTS.md refuses). It
    resolves the STALE original path by abandoning it there (the code is not supposed to live there
    any more) and hands back the branch's own version of the file for the human to apply at
    `candidate` by hand -- real, targeted help (the destination is already found and the old copy
    is already cleaned up) without a mechanism that could quietly corrupt a file it never
    understood.

    `MANUAL` touches nothing: design §5 step 4 hands control to the human's own editor, who is
    expected to `git add` the path themselves -- `walk_conflicts` re-asks `decide` for the same
    file if it is still conflicted afterwards, rather than assuming it is done."""
    if option == RECREATE:
        return _resolve_to_stage(run, cwd, path, 3, "kept this branch's own version")
    if option == ABANDON:
        return _resolve_to_stage(run, cwd, path, 2, "accepted the base's decision")
    if option == FOLLOW:
        if not isinstance(candidate, str) or not candidate.strip():
            raise ValueError("'follow' needs a candidate destination path")
        # Read the branch's own version FIRST: `_resolve_to_stage` below may `git rm` the path,
        # which clears EVERY stage entry for it (including stage 3) -- reading it after would find
        # nothing left to read. `action` is left exactly as `_resolve_to_stage` reports it
        # ("removed" or "checked-out-stage-2") -- what actually happened at the STALE path -- with
        # the move's own information added alongside, never overwriting it.
        branch_version = _stage_blob(run, cwd, path, 3)
        resolved = _resolve_to_stage(run, cwd, path, 2,
                                     "moved to %s -- the stale path was abandoned" % candidate)
        resolved.update({"candidate": candidate, "apply_by_hand_content": branch_version})
        return resolved
    if option == MANUAL:
        return {"path": path, "action": "manual", "why": "left for the human's own editor"}
    raise ValueError("unknown conflict option: %r" % (option,))


# --------------------------------------------------------------------------- the walk itself


def _empty_commit_about_to_land(run, cwd):
    """Whether `git rebase --continue` would silently DROP the commit being replayed (#2318). The body moved to
    sigma-loop's `conflict_state.empty_commit_about_to_land` (the engine needs it too, and importing it from this
    module would reverse the dependency); this name stays so every caller and test here is unchanged."""
    return conflict_state.empty_commit_about_to_land(run, cwd)


def _rebase_just_concluded_locally(run, cwd):
    """Whether the SINGLE most recent HEAD reflog entry records a rebase concluding right here
    (#2324) -- `rebase (finish): returning to refs/heads/<branch>`, the exact line git itself
    writes the instant a rebase's LAST replayed step lands, regardless of which of
    `_empty_commit_about_to_land`'s own three named recovery options (`--skip`, `--allow-empty`
    then a final `--continue`, or this module's own `--continue`) actually got there.

    VERIFIED AGAINST REAL GIT (2.49): reproduced all three recovery paths by hand outside this
    suite -- `--skip` and a plain `--continue` (the `--empty=drop` default silently completing the
    rebase) both write `rebase (finish): ...` the moment the LAST step lands; `git commit
    --allow-empty` alone does NOT conclude anything (the rebase stays stopped, needing its own
    final `--continue`, which is what actually writes `rebase (finish): ...`); `git rebase --abort`
    writes `rebase (abort): ...` instead -- deliberately excluded here, see `_manual_recovery_push`
    for why the ahead-of-remote check makes this belt-and-suspenders rather than load-bearing on
    its own.

    Read from git's OWN reflog -- never this tool's own cross-invocation memory, never guessed --
    so it answers correctly regardless of which process (a human's raw git, or a prior run of this
    same module) produced the state. Only the SINGLE most recent entry counts: if anything else
    happened to this branch since (an ordinary commit, say), that is what is "most recent" now, and
    pushing on the strength of an older, already-handled rebase buried under unrelated work would
    risk shipping something nobody asked this tool to send. Fail-open to `False` -- no reflog (a
    branch with no history yet) is not "just concluded"."""
    try:
        subject = str(run(cwd, ["git", "reflog", "show", "-1", "--format=%gs", "HEAD"]) or "")
    except Exception:                          # noqa: BLE001 - unreadable reflog is not "concluded"
        return False
    return subject.strip().startswith("rebase (finish):")


def _manual_recovery_push(run, cwd, remote, branch, base_ref=None):
    """#2324: `walk_conflicts`'s own `NOTHING_TO_DO` entry check (`not rebase_stopped`) fires both
    for the ORDINARY case (this branch was never mid-rebase -- nothing to push, nothing wrong) and
    for the state a human's raw-git escape hatch leaves behind after `_empty_commit_about_to_land`'s
    own refusal (#2318): `--skip`, `--allow-empty` + a final `--continue`, or `--abort`, all run
    OUTSIDE this module's own `--continue`-then-push loop (#2319), where nothing pushes. Re-running
    `conflict_walk.py walk` afterward used to just report "nothing to walk" and leave it there.

    TWO STRUCTURAL FACTS, BOTH REQUIRED -- never a guess, never this tool's own persisted memory:
      1. `_rebase_just_concluded_locally` -- git's OWN reflog says the most recent thing that
         happened to this branch actually was a rebase landing.
      2. The branch is genuinely AHEAD of `<remote>/<branch>` (fetched fresh here, never a stale
         local copy) -- a rebase that landed but already matches what is pushed needs nothing more:
         an ordinary `walk_conflicts` run that already pushed via DONE, or an `--abort` that
         restored the exact pre-rebase tip, both read as "not ahead" here and correctly do nothing.
         This is what keeps signal 1 from being load-bearing alone -- an abort's own "not ahead"
         answer means excluding `rebase (abort):` in signal 1 is belt-and-suspenders, not the only
         guard.

    Returns `None` when signal 1 is absent -- the ordinary case, where doing anything at all would
    be an unrequested, surprising push (AGENTS.md SAFETY: nothing sends data without the operator
    opting in). Otherwise `{"pushed": bool, "why": str}`: `why` names why nothing was pushed even
    though a rebase conclusion WAS seen (already matches remote, the fetch failed, no such
    remote-tracking ref, or the push itself was refused) -- read from the SAME
    `rebase_brief.push_branch` #2319's own DONE path already calls, one force-with-lease call site,
    never a second copy.

    `base_ref` (#278, review block #1) is what `push_branch` attributes a remote -> pre-rebase-head
    loss against (`feature_rebase.own_losses`); without it no such loss is exempt, so a bare call
    can refuse a healthy local deletion but never publish a loss an earlier lossy rebase left."""
    if not _rebase_just_concluded_locally(run, cwd):
        return None
    try:
        run(cwd, ["git", "fetch", remote, branch])
    except Exception as exc:                  # noqa: BLE001 - can't confirm ahead-ness; don't guess
        return {"pushed": False, "why": "could not fetch %s/%s to check: %s" % (
            remote, branch, rebase_brief._flat(exc))}
    try:
        ahead = str(run(cwd, ["git", "rev-list", "--count",
                              "%s/%s..HEAD" % (remote, branch)]) or "").strip()
    except Exception as exc:                  # noqa: BLE001 - no remote-tracking ref yet, etc.
        return {"pushed": False, "why": "could not compare against %s/%s: %s" % (
            remote, branch, rebase_brief._flat(exc))}
    if ahead in ("", "0"):
        return {"pushed": False,
               "why": "already matches %s/%s -- nothing to push" % (remote, branch)}
    push = rebase_brief.push_branch(run, cwd, remote, branch, base_ref=base_ref)
    if not push["ok"]:
        # #144: `refused` when `push_branch`'s own tree guard said no (it would lose content the
        # remote branch has, or could not tell) -- distinct from an ordinary stale lease, and
        # something `main` must SAY rather than fold into "nothing to walk".
        return {"pushed": False, "why": push["why"], "refused": "dropped" in push,
                "dropped": push.get("dropped") or []}
    return {"pushed": True, "why": ""}


def walk_conflicts(run, cwd, brief, decide, remote, sdlc_dir=None):
    """Drive the conflict-options walker to completion (design §5): for each conflicted file, in
    turn, classify it and assemble its state (steps 1-4), hand it to the injected `decide`
    callable, apply whichever option comes back (step 5), then move to the next file. Once every
    file in the current stop is resolved, `git rebase --continue`; if a later commit conflicts
    again, the whole loop repeats -- exactly design §5's own "repeating if a later commit hits
    another conflict". `decide(FileConflictState) -> {"option": ..., "candidate": ...?}` is the one
    seam a real interactive CLI and a test both drive (see module docstring) -- `ABORT` bails out
    at any point, matching git's own state exactly (`rebase --abort`), leaving the tree as it was.

    `sdlc_dir` (#2321), when given, threads through to `file_conflict_state` so each conflict's
    decision-context is captured once and reused rather than re-derived on every ask; a resolved
    path's own snapshot is discarded the moment it stops being conflicted (a later, different
    commit conflicting on the SAME path must get its own fresh capture, never this one's stale
    entry), and the whole per-branch store is cleared on a clean conclusion (DONE/ABORTED) so
    nothing here is left to grow without bound. A falsy `sdlc_dir` is a no-op throughout, matching
    this function's pre-#2321 behaviour.

    Before every `--continue`, `_empty_commit_about_to_land` checks whether it would silently drop
    the commit currently being replayed (#2318). When it would, this refuses: the rebase is left
    stopped EXACTLY where it is -- never aborted, never continued -- because the original commit is
    still fully intact there (a rebase does not touch `branch`'s own ref until the WHOLE rebase
    finishes, so nothing has been rewritten yet) and aborting would needlessly discard any conflicts
    already resolved earlier in this same walk. `resolved` up to that point (including any
    `apply_by_hand_content` already captured for this exact commit) is still returned, so the
    caller has everything found so far.

    Once every commit has actually landed (`DONE`), `remote` is pushed via `rebase_brief.push_branch`
    -- the identical force-with-lease mechanism `rebase_brief.attempt_rebase`'s own clean path uses
    (#2319) -- so `DONE` carries the same "fully done, including pushed" guarantee `REBASED` already
    does over there, rather than leaving an easy-to-forget manual step after a green walk.

    When NO rebase is stopped at all, `_manual_recovery_push` (#2324) checks whether that is because
    a human just concluded this exact rebase THEMSELVES via git's own escape hatch, outside this
    loop entirely, and pushes if so -- see that function's own docstring for the two structural
    facts this requires before it ever touches the remote. The ordinary "never mid-rebase" case
    returns the identical `{"outcome": NOTHING_TO_DO, "resolved": []}` this function has always
    returned; only the recovery-push case adds `"pushed"`/`"why"`.

    Returns `{"outcome": DONE|NOTHING_TO_DO|ABORTED|FAILED|EMPTY_AFTER_RESOLVE, "resolved": [...]}`
    (+ "why" on FAILED/EMPTY_AFTER_RESOLVE, + "pushed"/"why" on a NOTHING_TO_DO recovery push).
    """
    if not feature_rebase.rebase_stopped(run, cwd):
        recovery = _manual_recovery_push(run, cwd, remote, brief["branch"], brief.get("base_ref"))
        if recovery is None:
            return {"outcome": NOTHING_TO_DO, "resolved": []}
        found = {"outcome": NOTHING_TO_DO, "resolved": [], "pushed": recovery["pushed"],
                 "why": recovery["why"]}
        if recovery.get("refused"):
            found["dropped"] = recovery["dropped"]
        return found
    resolved = []
    # #278: read while the rebase is still stopped -- the state directory is gone once it lands.
    pre_head = _rebase_state_file(run, cwd, "orig-head") or None
    while True:
        pending = rebase_brief.conflicted_files(run, cwd)
        if not pending:
            at_risk = _empty_commit_about_to_land(run, cwd)
            if at_risk is not None:
                name = ("`%s` (%s)" % (at_risk["sha"][:12], at_risk["subject"]) if at_risk["sha"]
                        else "the commit currently being replayed")
                return {"outcome": EMPTY_AFTER_RESOLVE, "resolved": resolved,
                        "why": ("`git rebase --continue` would silently DROP %s -- every conflict "
                                "is resolved but the result is identical to the new parent, and "
                                "git's own default (`--empty=drop`) discards a commit like this "
                                "with no message on either stream and exit 0 (#2318). Left the "
                                "rebase stopped exactly here instead, nothing rewritten yet: `git "
                                "rebase --skip` explicitly drops it (same result, at least "
                                "announced), `git commit --allow-empty` keeps it as an empty "
                                "commit, or `git rebase --abort` abandons everything and restores "
                                "`%s` to its original state." % (name, brief["branch"]))}
            try:
                # `-c core.editor=true`: `rebase --continue` reuses the conflicting commit's own
                # message and would otherwise open an editor to let a human review/amend it. Every
                # OTHER `rebase --continue` call site in this kit already carries this same
                # protection (`sync.py`'s `GIT_EDITOR=true`, `work.py`'s identical `-c
                # core.editor=true`) for the identical reason: on a host with no controlling
                # terminal -- CI, or this kit's own unattended loop -- git refuses outright
                # ("Terminal is dumb, but EDITOR unset") instead of hanging, which without this
                # flag turned every conflict this function resolved into a hard FAILED outcome the
                # instant the LAST conflict was cleared (sigma's first Linux CI run, #2742/#2724).
                run(cwd, ["git", "-c", "core.editor=true", "rebase", "--continue"])
            except Exception as exc:        # noqa: BLE001 - a later conflict is an outcome, not a crash
                if rebase_brief.conflicted_files(run, cwd):
                    continue                # a later commit conflicted again -- next loop picks it up
                return {"outcome": FAILED, "resolved": resolved, "why": rebase_brief._flat(exc)}
            if not feature_rebase.rebase_stopped(run, cwd):
                push = rebase_brief.push_branch(
                    run, cwd, remote, brief["branch"], accepted=_accepted_losses(resolved),
                    pre_head=pre_head, base_ref=brief.get("base_ref"))
                if not push["ok"]:
                    # #144: `push_branch` itself refuses a HEAD that would lose content the remote
                    # branch has (`dropped`), and leaves the branch unpushed; FAILED either way.
                    return {"outcome": FAILED, "resolved": resolved, "why": push["why"],
                            "dropped": push.get("dropped") or []}
                rebase_brief.clear_context_snapshots(sdlc_dir, brief["branch"])
                return {"outcome": DONE, "resolved": resolved}
            continue
        path = pending[0]
        conflict_state = file_conflict_state(run, cwd, brief, path, sdlc_dir)
        decision = decide(conflict_state) or {}
        option = decision.get("option")
        if option == ABORT:
            run(cwd, ["git", "rebase", "--abort"])
            rebase_brief.clear_context_snapshots(sdlc_dir, brief["branch"])
            return {"outcome": ABORTED, "resolved": resolved}
        if option not in conflict_state["options"]:
            raise ValueError("decision provider chose %r for %s, which was not offered (offered: "
                             "%s)" % (option, path, conflict_state["options"]))
        outcome = apply_option(run, cwd, path, option, decision.get("candidate"))
        if path in rebase_brief.conflicted_files(run, cwd):
            # MANUAL (or a decide() that returned early without staging) -- still conflicted; loop
            # back and ask again rather than assume it is done (design §5: "waits for `git add`").
            continue
        rebase_brief.discard_context_snapshot(sdlc_dir, brief["branch"], path)
        resolved.append(outcome)


def _accepted_losses(resolved):
    """The paths whose LOSS a human decided in this walk, for `push_branch(accepted=...)` (#278).

    Only a resolution that IS a deletion -- `action == "removed"`: ABANDON (or FOLLOW) on a file the
    base deleted -- counts. A content resolution does not, whichever option produced it: resolving
    ONE conflicted hunk says nothing about the rest of the file, where git has already merged
    everything outside the markers -- including, when the base holds a revert, the silent removal
    of hundreds of the branch's lines. Exempting the whole path for a one-line decision let that
    through (#144's final review), so those paths stay behind the tree guard."""
    return [one.get("path") for one in resolved
            if isinstance(one, dict) and one.get("action") == "removed"]


# --------------------------------------------------------------------------- resumability


def _rebase_state_file(run, cwd, name):
    """One file of a stopped rebase's own on-disk state (`<gitdir>/rebase-merge/<name>`, or
    `rebase-apply/<name>` for the `am` backend), stripped, or "" when nothing is stopped or the
    file is absent/unreadable -- fails closed, like `rebase_stopped`."""
    for state in ("rebase-merge", "rebase-apply"):
        try:
            got = str(run(cwd, ["git", "rev-parse", "--git-path", state]) or "").strip()
        except Exception:                  # noqa: BLE001 - unmeasurable is not "found"
            continue
        if not got:
            continue
        here = pathlib.Path(got)
        if not here.is_absolute():
            here = pathlib.Path(cwd) / here
        try:
            text = (here / name).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            return text
    return ""


def _rebase_original_branch(run, cwd):
    """The branch a currently-stopped rebase is replaying commits FROM, read straight from git's
    own on-disk rebase state (`<gitdir>/rebase-merge/head-name`, or `rebase-apply/head-name` for
    the `am` backend) -- the same directory `feature_rebase.rebase_stopped` already asks about.
    NEVER `git rev-parse --abbrev-ref HEAD`: verified against real git, that returns the literal
    string "HEAD" while a rebase has HEAD detached, which is exactly when resumability needs this
    answer most. `None` when nothing is stopped or the on-disk shape is not what was expected --
    fails closed, matching `rebase_stopped`'s own "not knowing is never an answer"."""
    ref = _rebase_state_file(run, cwd, "head-name")
    if ref.startswith("refs/heads/"):
        return ref[len("refs/heads/"):]
    return None


def resolve_branch(run, cwd, explicit=None):
    """The branch to walk: `explicit` when given, else a stopped rebase's own original branch
    (resumability -- see `_rebase_original_branch`), else `rebase_brief.current_branch`'s ordinary
    checked-out-branch answer for the nothing-is-stopped-yet case."""
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    original = _rebase_original_branch(run, cwd)
    if original:
        return original
    return rebase_brief.current_branch(run, cwd)


# --------------------------------------------------------------------------- a real interactive CLI


def _print_conflict_state(conflict_state):
    print("")
    print("`%s` -- %s:" % (conflict_state["path"], conflict_state["shape"]))
    ctx = conflict_state["context"]
    if not ctx["commits"]:
        print("  %s" % rebase_brief.NO_EXPLANATION)
    for c in ctx["commits"]:
        print("  `%s` %s -- %s" % (c["sha"][:12], c["subject"], rebase_brief._describe(c["context"])))
    if conflict_state["symbol"]:
        print("  symbol searched: %s" % conflict_state["symbol"])
    if conflict_state["candidates"]:
        print("  possible move destination(s): %s" % ", ".join(conflict_state["candidates"]))


_MENU = {"1": RECREATE, "recreate": RECREATE, "2": FOLLOW, "follow": FOLLOW,
         "3": ABANDON, "abandon": ABANDON, "4": MANUAL, "manual": MANUAL, "abort": ABORT}


def _interactive_decide(conflict_state):
    """The real CLI's own decision provider -- built on `input()`/`print()`, entirely outside
    `walk_conflicts` (see module docstring: no `input()` scattered through the walking logic
    itself). This is the ONE place this module blocks on a human."""
    _print_conflict_state(conflict_state)
    # #278: `[3]` is `git checkout --ours` of the WHOLE file (or its deletion), never one hunk.
    print("Options: [1] Recreate here  [3] Take the base's version of the whole file (drops this "
          "branch's changes to it)  [4] Resolve by hand  [abort] Bail out")
    if FOLLOW in conflict_state["options"]:
        print("         [2] Follow the move -> %s" % conflict_state["candidates"][0])
    while True:
        choice = input("Choice: ").strip().lower()
        option = _MENU.get(choice)
        if option is None or (option != ABORT and option not in conflict_state["options"]):
            print("Not a valid/offered option for this file -- try again.")
            continue
        if option == FOLLOW:
            candidates = conflict_state["candidates"]
            candidate = candidates[0] if len(candidates) == 1 else \
                input("Which candidate? %s: " % candidates).strip()
            return {"option": FOLLOW, "candidate": candidate}
        if option == MANUAL:
            input("Resolve `%s` in your editor, `git add` it, then press enter: "
                 % conflict_state["path"])
        return {"option": option}


def _print_resolved_follow_ups(resolved):
    """Surface every resolution's own actionable, terminal-invisible-until-now data in the CLI's
    output (#2320): `apply_option`'s FOLLOW case has always returned `apply_by_hand_content` in its
    result dict (per its own docstring), but nothing printed it -- a real human running `walk` saw
    only the final outcome line, with zero indication that a file's content still needs manual
    re-application at its own candidate destination. This is `main()`'s own job, never
    `walk_conflicts`' (module docstring: the walking loop is the seam a test drives, never stdout
    itself) -- called for every `resolved` entry regardless of the walk's own final outcome, since
    it is now also the ONLY surviving trace of a commit `_empty_commit_about_to_land` (#2318) caught
    about to be silently dropped."""
    handoffs = [r for r in resolved if r.get("apply_by_hand_content")]
    if not handoffs:
        return
    print("\n%d file(s) resolved by \"follow the move\" need their content re-applied by hand:"
         % len(handoffs))
    for r in handoffs:
        print("\n`%s` -- moved to `%s` (%s). Its own content, to re-apply there:"
             % (r["path"], r.get("candidate", "?"), r.get("why", "")))
        print("-" * 60)
        print(r["apply_by_hand_content"])
        print("-" * 60)


USAGE = "usage: conflict_walk.py walk <sdlc_dir> [branch]"


def main(argv):
    """`conflict_walk.py walk <sdlc_dir> [branch]` -- resumes (design §5's resumability) or starts
    walking a stopped rebase's conflicts to completion, interactively, reports there is nothing to
    walk when no rebase is currently stopped, or (#2324) pushes and says so when a human just
    concluded this exact rebase themselves via git's own escape hatch, outside this tool entirely.

    `remote`/`branch` are resolved BEFORE the stopped-rebase check (moved up from this function's
    pre-#2324 order) because `_manual_recovery_push` needs both to even ask its question -- cheap
    either way (no fetch happens unless the reflog already says a rebase just concluded here), so
    the ordinary "never mid-rebase" case pays for nothing it did not already pay for."""
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    if len(argv) < 3 or argv[1] != "walk":
        print(USAGE, file=sys.stderr)
        return 2
    sdlc_dir = argv[2]
    explicit = argv[3] if len(argv) >= 4 else None
    config = state.load_config(sdlc_dir)
    cwd = str(pathlib.Path(sdlc_dir).parent)
    run = feature_rebase._run
    remote = rebase_brief.resolve_remote(config)
    branch = resolve_branch(run, cwd, explicit)
    base = rebase_brief.resolve_base(config)
    if not feature_rebase.rebase_stopped(run, cwd):
        base_ref = "%s/%s" % (remote, base) if base and base != branch else None
        recovery = _manual_recovery_push(run, cwd, remote, branch, base_ref)
        if recovery and recovery["pushed"]:
            print("`%s` was not mid-rebase -- but a rebase had concluded outside this tool and "
                 "left it ahead of `%s/%s`; pushed (#2324)." % (branch, remote, branch))
        elif recovery and recovery.get("refused"):
            print("`%s` was not mid-rebase -- a rebase had concluded outside this tool, but it was "
                  "NOT pushed: %s" % (branch, recovery["why"]))
            return 1
        else:
            print("no rebase is currently stopped in %r -- nothing to walk." % cwd)
        return 0
    if not base or base == branch:
        print("no integration branch to compare against (work.base is %r)" % base, file=sys.stderr)
        return 1
    brief = rebase_brief.assemble_brief(run, cwd, remote, branch, base)
    report = walk_conflicts(run, cwd, brief, _interactive_decide, remote, sdlc_dir)
    if report["outcome"] != ABORTED:
        # Suppressed on ABORTED: `git rebase --abort` reverts the WHOLE rebase, so anything
        # resolved earlier in this same walk is undone along with it -- printing its own
        # apply-by-hand content here would describe files that need no manual follow-up at all.
        _print_resolved_follow_ups(report.get("resolved") or [])
    if report["outcome"] == DONE:
        print("\n`%s` now sits cleanly on `%s` and was pushed -- every conflict resolved."
             % (branch, base))
        return 0
    if report["outcome"] == ABORTED:
        print("\nAborted -- the tree is back exactly as it was.")
        return 0
    if report["outcome"] == NOTHING_TO_DO:
        print("nothing to resolve.")
        return 0
    print("\nthe walk did not complete: %s" % report.get("why", report["outcome"]))
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
