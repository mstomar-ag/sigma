#!/usr/bin/env python3
"""sigma-rebase, slice 1 (#2304, epic #2303, design #2288 §2-§4): the
decision-context brief and the clean-path rebase for a manually-triggered, human-attended
companion to `feature_rebase.py`'s automatic upkeep pass.

WHAT THIS IS FOR. `feature_rebase.py`'s `upkeep()` already detects a conflict correctly and files
it as tracked work -- but it never explains WHY a rebase is needed, and its only action on a
conflict is to abandon the throwaway worktree and hand a human a raw `git` error. This module is
the resolution-and-explanation half: before touching anything, it assembles a brief of what the
base branch did while this branch was away and WHY (from CHANGELOG.md, PR descriptions, and linked
design docs), flags the files most likely to conflict, then attempts the rebase. On a genuine
conflict it shows that file's own decision context and stops -- strictly better than a bare error,
even before the interactive conflict-options walker (slice 2, #2305) lands.

REUSED, NEVER REINVENTED:
  * `feature_rebase._settings`/`_remote` -- `work.base`/`work.remote` resolution, unconditional,
    the same fallback `feature_rebase.py`'s own `_upkeep()` uses, so a repo with no
    `.sdlc/features/`, or a branch that is not `feature/<name>`, still resolves cleanly (design
    D-4 -- this skill works generically, "across the repos this org runs").
  * `feature_rebase.landed_commits` -- the exact first-parent delta walk, reused in the OTHER
    direction here: what the BASE did while this branch was away, rather than what landed ON a
    feature branch.
  * `feature_rebase.rebase_stopped` -- the structural (never exception-text) test for "did a
    rebase actually stop here", reused directly to tell a genuine conflict apart from a git call
    that simply failed to run.
  * `changelog_coverage.extract_issue_ids` -- pulls every `#N` out of a text as a SET, loaded by
    path exactly as `feature_rebase._load` already loads its own siblings and exactly as
    the release-CI script's own test already loads this module -- when it is present. It is
    sigma's OWN release-CI script under `.github/scripts/`, not part of what a plugin install
    ships to an adopting repo (`skills/sigma-release-check/SKILL.md` already runs it by a bare
    repo-root path for the same reason), so a plugin install elsewhere genuinely will not have it.
    `_changelog_coverage()` degrades to a one-line local port of the identical technique in that
    case, so behaviour is the same whichever branch is taken.

DELIBERATELY NOT REUSED: `feature_rebase.arrived_through_a_pull_request`. Its two regexes are
`\\Z`-anchored to the TRAILING number in this repo's own dominant squash shape (`<title> (#issue)
(#PR)`) -- correct for "did a pull request land this", wrong for "which issue does the CHANGELOG
key this to" (the heading is keyed to the issue, never the trailing PR). `_pr_number` below still
borrows those same two regexes' own anchoring -- to find WHICH match is the PR reference, never an
earlier issue reference in the same subject -- then reuses `_ISSUE_RE`'s own capturing group (the
identical regex `changelog_coverage.extract_issue_ids` already uses) to pull the number out of
that one match, for the one place a PR number specifically is needed: the PR-description fallback.

RUNS AGAINST THE HUMAN'S OWN LIVE CHECKOUT, not a throwaway worktree (design D-2) -- unlike
`feature_rebase.py`'s ephemeral detached replay, this skill's whole premise is a person sitting at
their own terminal on their own branch.

CLI: `rebase_brief.py brief <sdlc_dir> [branch]` (assemble and print the brief, touch nothing) |
`rebase_brief.py rebase <sdlc_dir> [branch]` (assemble, print, then -- only when `branch` is the
CURRENTLY checked-out one -- attempt the rebase; on a conflict, print that file's own decision
context and stop, leaving the tree exactly as `git rebase` itself leaves a stopped one)."""
import importlib.util
import json
import os
import pathlib
import re
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent
_LOOP_SCRIPTS = _HERE.parent.parent / "sigma-loop" / "scripts"
_REPO_ROOT = _HERE.parent.parent.parent


def _load(name, directory=None):
    """Import a sibling script by path -- the kit's standard zero-install module loader
    (`feature_rebase._load`, `review_context._load`)."""
    directory = pathlib.Path(directory) if directory else _LOOP_SCRIPTS
    spec = importlib.util.spec_from_file_location(name, directory / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


feature_rebase = _load("feature_rebase")
state = _load("state")

_UNSET = object()
_CHANGELOG_COVERAGE = _UNSET

#: The same digit-capturing regex `changelog_coverage.extract_issue_ids` uses -- ported here as the
#: fallback when that module is not on disk (see module docstring), and reused directly by
#: `_pr_number` either way.
_ISSUE_RE = re.compile(r"#(\d+)")
_HEADING_RE = re.compile(r"^(### .*)$", re.MULTILINE)
_DESIGN_NAMES = ("{id}.md", "{id}-in-brief.md")

#: What this brief says when no source explains a change -- an honest terminal state, never a
#: fabricated reason and never a silently empty brief (design §3 step 3.4).
NO_EXPLANATION = "no written explanation exists for this change"

#: Outcomes `attempt_rebase` reports. Mirrors `feature_rebase.py`'s own vocabulary where the
#: concept is identical (`CURRENT`, `CONFLICT`, `REBASED`, `FAILED`) rather than inventing a
#: parallel one for the same four ideas.
CURRENT = "current"
CONFLICT = "conflict"
REBASED = "rebased"
FAILED = "failed"
#: #144: the replay succeeded but would lose content the branch has (`feature_rebase.dropped_paths`)
#: -- nothing is pushed. The same name as `feature_rebase.WOULD_DROP`.
WOULD_DROP = feature_rebase.WOULD_DROP


def _changelog_coverage():
    """`.github/scripts/changelog_coverage.py`, loaded by path -- when it exists. See the module
    docstring for why its absence is an ordinary, expected outcome on most installs rather than an
    error. Cached after the first call; `None` means "fall back to the local port"."""
    global _CHANGELOG_COVERAGE
    if _CHANGELOG_COVERAGE is _UNSET:
        try:
            _CHANGELOG_COVERAGE = _load("changelog_coverage", _REPO_ROOT / ".github" / "scripts")
        except Exception:                      # noqa: BLE001 - a missing/broken module is not fatal
            _CHANGELOG_COVERAGE = None
    return _CHANGELOG_COVERAGE


def extract_issue_ids(text):
    """Every `#NNNN` reference in `text`, as a set of digit strings. Delegates to the real
    `changelog_coverage.extract_issue_ids` when that module is loadable (the exact technique
    design §3 step 2 calls for); otherwise a local port of the identical regex/set-comprehension,
    so the result is the same either way."""
    cc = _changelog_coverage()
    if cc is not None:
        return cc.extract_issue_ids(text or "")
    return {m.group(1) for m in _ISSUE_RE.finditer(text or "")}


def _pr_number(subject):
    """The one PR number this commit's subject accounts for, or `None`. See the module docstring
    for why this borrows `feature_rebase`'s two anchored regexes rather than
    `arrived_through_a_pull_request` itself (which proves the number exists but throws it away)."""
    if not isinstance(subject, str):
        return None
    text = subject.strip()
    m = feature_rebase._SQUASH_PR_RE.search(text) or feature_rebase._MERGE_PR_RE.match(text)
    if not m:
        return None
    found = _ISSUE_RE.search(m.group())
    return found.group(1) if found else None


def _flat(exc):
    return " ".join(str(exc).split())


# --------------------------------------------------------------------------- branch/base (§2)


def resolve_remote(config):
    """`work.remote` -- reused directly from `feature_rebase`, never re-derived."""
    return feature_rebase._remote(config)


def resolve_base(config):
    """`work.base`, unconditionally -- the identical fallback `feature_rebase.py`'s own
    `_upkeep()` uses (`_settings(config).get("base") or ""`), so a repo with no
    `.sdlc/features/`, or a branch that is not `feature/<name>`, still resolves cleanly (design
    §2, D-4)."""
    return state.safe_ref("work.base", feature_rebase._settings(config).get("base")) or ""     # #710


def current_branch(run, cwd):
    return str(run(cwd, ["git", "rev-parse", "--abbrev-ref", "HEAD"]) or "").strip()


def resolve_branch(run, cwd, explicit=None):
    """The current checked-out branch by default, or `explicit` when given (design §2 — "a human
    who wants to check on a branch they are not currently on")."""
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    return current_branch(run, cwd)


# --------------------------------------------------------------------------- the delta (§3.1-2)


def delta_commits(run, cwd, branch, base_ref):
    """What `base_ref` did while `branch` was away -> `[(sha, subject)]`, newest first.

    `feature_rebase.landed_commits(run, cwd, integration_ref, feature_ref)` walks
    `integration_ref..feature_ref` -- calling it with the two refs SWAPPED gives exactly the delta
    this brief needs: everything on `base_ref` that `branch` does not have. Same function, same
    `--first-parent` shape, reused rather than reimplemented (design §3 step 1)."""
    return feature_rebase.landed_commits(run, cwd, branch, base_ref)


# --------------------------------------------------------------------------- CHANGELOG (§3.3.1)


def changelog_text(run, cwd, ref):
    """CHANGELOG.md as of `ref` -- fail-open: no file, no repo, any git error -> `""`."""
    try:
        return str(run(cwd, ["git", "show", "%s:CHANGELOG.md" % ref]) or "")
    except Exception:                          # noqa: BLE001 - a missing CHANGELOG is not fatal
        return ""


def changelog_entries(text):
    """Every `### ` heading in `text`, paired with its OWN referenced issue ids ->
    `[(heading, ids)]`. Matches only the heading's own line -- never the whole entry body, and
    never the whole unreleased-window text `changelog_coverage.unaccounted` matches against --
    because a body paragraph mentioning an unrelated issue must not misattribute a commit to the
    wrong entry (design §3 step 3.1, the round-1 REJECT-fix this design itself documents)."""
    return [(h.strip(), extract_issue_ids(h)) for h in _HEADING_RE.findall(text or "")]


def changelog_match(ids, entries):
    """The first CHANGELOG heading whose own `#N` set intersects `ids`, or `None`."""
    for heading, heading_ids in entries:
        if heading_ids & ids:
            return heading
    return None


# --------------------------------------------------------------------------- PR / design (§3.3.2-3)


_GH = "gh"          # one binding, so the adapter below builds no `["gh", *args]` ratchet site


def pr_description(run, cwd, number):
    """PR `number`'s `{"title", "body"}`, or `None` on any failure (no gh, no auth, no such PR,
    offline, gh_api unloadable) -- fail-open, matching `review_context.py`'s own precedent for a
    decision-context source (design BR-15).

    #895 4a-1: REST first through `gh_api.view_pr_gh` (`gh api repos/{owner}/{repo}/pulls/<n>`, ONE
    `gh pr view` fallback built inside gh_api on a rate limit / 5xx / transport failure). gh_api is
    loaded lazily, by path, from the sibling sigma-loop skill that ships in the same plugin, so an
    import of this module never fails on it. No sdlc_dir: this read never writes the REST breaker or
    the fallback log. The adapter turns `run(cwd, argv)` (argv WITH "gh") into gh_api's `run(args)`,
    putting only the detail after the argv prefix on `.hint` so classification never reads argv."""
    if not number:
        return None

    def api_run(args):
        argv = [_GH, *args]
        try:
            return run(cwd, argv)
        except RuntimeError as exc:
            msg = str(exc)
            prefix = " ".join(str(a) for a in argv) + ": "
            err = RuntimeError(msg)
            err.hint = getattr(exc, "hint", None) or (msg[len(prefix):] if msg.startswith(prefix) else msg)
            raise err from exc

    try:
        data = _load("gh_api").view_pr_gh(api_run, number, ["title", "body"])
    except Exception:                          # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    return {"title": str(data.get("title") or ""), "body": str(data.get("body") or "")}


def design_doc_reference(run, cwd, ids, base_ref):
    """The first `.sdlc/design/<id>.md` (or `<id>-in-brief.md`) that actually exists on
    `base_ref`, for any id in `ids` -- this pipeline's own convention (`docs/dossier-pipeline.md`),
    design §3 step 3.3. Checked with `git cat-file -e`, fail-open, against the BASE's own tree
    (never the working tree, which may be mid-rebase or simply on a different branch)."""
    for issue_id in sorted(ids, key=lambda s: int(s) if s.isdigit() else 0):
        for template in _DESIGN_NAMES:
            path = ".sdlc/design/%s" % template.format(id=issue_id)
            try:
                run(cwd, ["git", "cat-file", "-e", "%s:%s" % (base_ref, path)])
            except Exception:                  # noqa: BLE001 - "does not exist" is not an error
                continue
            return path
    return None


def commit_context(run, cwd, sha, subject, entries, base_ref):
    """This commit's decision context, in priority order, fail-open at each step (design §3 step
    3): a CHANGELOG heading sharing an issue id with this subject; failing that, the landing PR's
    own description; failing that, a linked design write-up; failing all three, an honest
    admission rather than a fabricated reason or a silently empty brief."""
    ids = extract_issue_ids(subject)
    heading = changelog_match(ids, entries)
    if heading:
        return {"source": "changelog", "detail": heading}
    number = _pr_number(subject)
    pr = pr_description(run, cwd, number) if number else None
    if pr and (pr["title"] or pr["body"]):
        return {"source": "pr", "detail": "PR #%s -- %s" % (number, pr["title"] or "(no title)"),
                "body": pr["body"]}
    doc = design_doc_reference(run, cwd, ids, base_ref)
    if doc:
        return {"source": "design", "detail": doc}
    return {"source": "none", "detail": NO_EXPLANATION}


def _describe(context):
    source, detail = context["source"], context["detail"]
    if source == "changelog":
        return "CHANGELOG: %s" % detail
    if source == "design":
        return "design write-up: `%s`" % detail
    return detail                              # "pr" and "none" are already full sentences


# --------------------------------------------------------------------------- files (§3.4-5)


def touched_files(run, cwd, ref_a, ref_b):
    out = run(cwd, ["git", "diff", "--name-only", "%s..%s" % (ref_a, ref_b)])
    return sorted({line.strip() for line in str(out or "").splitlines() if line.strip()})


def overlapping_files(run, cwd, merge_base, branch, base_ref):
    """Files BOTH sides touched since the merge-base -- where a conflict is possible, flagged
    BEFORE the rebase runs rather than discovered by it (design §3 step 4)."""
    ours = set(touched_files(run, cwd, merge_base, branch))
    theirs = set(touched_files(run, cwd, merge_base, base_ref))
    return sorted(ours & theirs)


def deleting_commit(run, cwd, merge_base, base_ref, path):
    """The commit that deleted `path` on the base's side since the merge-base, newest first ->
    `(sha, subject)` or `None`. `--follow` so a rename-then-delete is still found (design §3
    step 5)."""
    out = run(cwd, ["git", "log", "--follow", "--diff-filter=D", "--format=%H %s",
                    "%s..%s" % (merge_base, base_ref), "--", path])
    for line in str(out or "").splitlines():
        sha, _, subject = line.strip().partition(" ")
        if sha:
            return sha, subject
    return None


def file_context(run, cwd, merge_base, base_ref, path, entries):
    """Per-file decision context, used both in the brief's overlap section and in a conflict
    explanation: if the base side DELETED this file, explain that deletion specifically (design §3
    step 5); otherwise, every base-side commit that touched it, each with its own context."""
    deleted = deleting_commit(run, cwd, merge_base, base_ref, path)
    if deleted:
        sha, subject = deleted
        return {"path": path, "deleted_on_base": True,
                "commits": [{"sha": sha, "subject": subject,
                            "context": commit_context(run, cwd, sha, subject, entries, base_ref)}]}
    out = run(cwd, ["git", "log", "--first-parent", "--format=%H %s",
                    "%s..%s" % (merge_base, base_ref), "--", path])
    commits = []
    for line in str(out or "").splitlines():
        sha, _, subject = line.strip().partition(" ")
        if sha:
            commits.append({"sha": sha, "subject": subject,
                            "context": commit_context(run, cwd, sha, subject, entries, base_ref)})
    return {"path": path, "deleted_on_base": False, "commits": commits}


# --------------------------------------------------------------------------- decision-context
# snapshot (#2321)
#
# WHAT THIS IS FOR. `file_context` above re-derives its answer from whatever `merge_base`/`base_ref`
# it is handed -- correct for the brief's own one-shot print, wrong once a conflict's resolution
# spans two separate process invocations (a human runs `rebase_brief.py rebase`, hits a conflict,
# and only later runs `conflict_walk.py walk` to resolve it -- design #2288 §5's
# own "a later slice"). If the base advances in between, re-deriving against the NOW-moved
# `base_ref` can describe an entirely different commit -- reproduced live (#2321): a conflict
# correctly classified as a content clash, whose context text cited an unrelated file-deletion
# commit that only became "the" deleting commit once the base moved further.
#
# THE FIX: capture `file_context`'s own answer ONCE, at first detection, and persist it where a
# later, separate process can read it back -- `.sdlc/state/rebase-context/<branch-stem>.json`,
# following this codebase's OWN convention for a per-branch/per-goal scratch file keyed by an id
# that may contain `/` (`diff_revert.scratch_dir`, `witness.path`: `str(id).replace("/",
# "_").replace("..", "_")`), under `.sdlc/state/` (gitignored, per-machine runtime --
# `setup.RUNTIME_IGNORES`), written atomically (`mkstemp` + `os.replace`, `state.py`'s and
# `triage._atomic_write_text`'s own standing publish tail) so a reader never observes a
# half-written store.
#
# NOT A ONE-SHOT CACHE FOR THE WHOLE REBASE. Keyed by `path` inside one per-branch store so a
# SECOND, later conflict on a different file gets its own independent entry; `discard_context_
# snapshot` drops a path's entry the moment it is actually resolved, so if that SAME path conflicts
# again later in this same rebase (a different commit touching it a second time), the next ask
# finds nothing cached and captures fresh -- never answers from the first conflict's now-stale
# entry. `clear_context_snapshots` wipes the whole per-branch store once a rebase concludes cleanly
# (`conflict_walk.walk_conflicts`'s own DONE/ABORTED outcomes) -- SCALABILITY: nothing here is left
# to grow without bound, and a stale leftover from an abandoned attempt never has old data.
#
# `sdlc_dir` FALSY DEGRADES TO NO PERSISTENCE, NEVER A CRASH: any caller that does not have (or does
# not care about) a `.sdlc` directory -- most of this suite's own existing tests -- gets exactly
# `file_context`'s own fresh-computed answer, matching this module's pre-#2321 behaviour.

_UNSAFE_STEM_CHARS = ("/", "\\")


def _context_snapshot_path(sdlc_dir, branch):
    """`.sdlc/state/rebase-context/<branch-stem>.json` -- the one store a branch's own conflict
    decision-context snapshots live under. `branch` routinely contains `/` (`feature/x`,
    `sdlc/2321-fix`) which would otherwise re-parse into extra path segments once joined with `/`
    (the same class of danger `state.evidence_path`'s own `unsafe_goal_reason` guards against for a
    goal id) -- neutralized here exactly as `diff_revert.scratch_dir`/`witness.path` already do for
    the identical shape of id."""
    stem = str(branch)
    for ch in _UNSAFE_STEM_CHARS:
        stem = stem.replace(ch, "_")
    stem = stem.replace("..", "_")
    return pathlib.Path(sdlc_dir) / "state" / "rebase-context" / ("%s.json" % stem)


def _read_context_store(sdlc_dir, branch):
    """The whole per-branch snapshot store -> `{path: context}`. Fail-open to `{}`: an absent,
    corrupt, or unreadable store means "nothing snapshotted yet", never a crash -- this is
    gitignored, per-machine `.sdlc/state/`, exactly like every other reader of that tree
    (`state.py`'s own module docstring)."""
    try:
        data = json.loads(_context_snapshot_path(sdlc_dir, branch).read_text(encoding="utf-8"))
    except Exception:                          # noqa: BLE001 - absent/corrupt is "nothing yet"
        return {}
    return data if isinstance(data, dict) else {}


def _write_context_store(sdlc_dir, branch, store):
    """Publish the whole per-branch store via a temp file in the SAME directory, then `os.replace`
    -- `triage._atomic_write_text`'s and `state.py`'s own standing publish tail, so a reader never
    observes a half-written file. Fail-open: a store this session cannot persist (a read-only
    `.sdlc/`, a full disk) costs future re-derivation, never a crash of the rebase itself."""
    path = _context_snapshot_path(sdlc_dir, branch)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(store, fh)
        os.replace(tmp, str(path))
    except OSError:
        pass


def snapshot_conflict_context(sdlc_dir, branch, run, cwd, merge_base, base_ref, path, entries):
    """`file_context`, computed fresh RIGHT NOW and persisted for a later, separate process to read
    back (#2321) -- `rebase_brief.py rebase`'s own CONFLICT branch is BY CONSTRUCTION the moment of
    first detection (git has just stopped the rebase for the first time this invocation), so this
    always OVERWRITES whatever a store may already hold for `path` rather than trusting a leftover
    entry: the one guarantee this function exists to provide is that what gets persisted here is
    accurate at THIS moment. A falsy `sdlc_dir` skips persistence entirely -- the caller still gets
    `file_context`'s own honest answer, just not remembered for later."""
    context = file_context(run, cwd, merge_base, base_ref, path, entries)
    if not sdlc_dir:
        return context
    store = _read_context_store(sdlc_dir, branch)
    store[path] = context
    _write_context_store(sdlc_dir, branch, store)
    return context


def reuse_or_snapshot_context(sdlc_dir, branch, run, cwd, merge_base, base_ref, path, entries):
    """`path`'s decision-context, read back from a PRIOR `snapshot_conflict_context` (or an earlier
    call to this same function) when one exists for this exact `(branch, path)` -- #2321's actual
    fix: `conflict_walk.py walk`, a later and separate process from whichever one first detected
    this conflict, must describe the conflict as it looked THEN, never a fresh re-derivation
    against wherever the base has since moved on to. Computes and persists fresh, matching
    `snapshot_conflict_context`'s own shape, only the FIRST time this exact path is asked about --
    e.g. a rebase resumed straight from a raw `git rebase` that never went through `rebase_brief.py
    rebase` at all, which had no chance to snapshot anything yet. A falsy `sdlc_dir` skips the store
    entirely and always re-derives, matching this module's pre-#2321 behaviour."""
    if not sdlc_dir:
        return file_context(run, cwd, merge_base, base_ref, path, entries)
    store = _read_context_store(sdlc_dir, branch)
    if path in store:
        return store[path]
    context = file_context(run, cwd, merge_base, base_ref, path, entries)
    store[path] = context
    _write_context_store(sdlc_dir, branch, store)
    return context


def discard_context_snapshot(sdlc_dir, branch, path):
    """Drop `path`'s own snapshot once it is actually resolved (#2321): the SAME path can conflict
    again later in this same rebase, from a different commit, and that is a genuinely NEW conflict
    that must be captured fresh at ITS OWN detection time -- never answered from the first
    conflict's now-stale entry. A falsy `sdlc_dir`, or a path with no entry to begin with, is a
    no-op, never an error."""
    if not sdlc_dir:
        return
    store = _read_context_store(sdlc_dir, branch)
    if path in store:
        del store[path]
        _write_context_store(sdlc_dir, branch, store)


def clear_context_snapshots(sdlc_dir, branch):
    """Wipe this branch's whole snapshot store once its rebase concludes cleanly -- DONE (landed
    and pushed) or ABORTED (tree restored to exactly what it was) both mean nothing is left
    pending, and leaving old entries around risks a stale hit on some FUTURE, unrelated rebase
    attempt against this same branch (SCALABILITY: bounded, not left to accumulate). A falsy
    `sdlc_dir`, or a store that was never created, is a no-op."""
    if not sdlc_dir:
        return
    try:
        _context_snapshot_path(sdlc_dir, branch).unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------- the brief itself


def assemble_brief(run, cwd, remote, branch, base):
    """Everything the human sees before anything is touched: the delta, each commit's decision
    context, and the files where a conflict is possible. Fetches `base`/`branch` first so every
    later measurement is against a fresh remote-tracking ref, never a stale local one."""
    run(cwd, ["git", "fetch", remote, base, branch])
    base_ref = "%s/%s" % (remote, base)
    merge_base = str(run(cwd, ["git", "merge-base", branch, base_ref]) or "").strip()
    entries = changelog_entries(changelog_text(run, cwd, base_ref))
    delta = [{"sha": sha, "subject": subject,
             "context": commit_context(run, cwd, sha, subject, entries, base_ref)}
            for sha, subject in delta_commits(run, cwd, branch, base_ref)]
    overlap = overlapping_files(run, cwd, merge_base, branch, base_ref)
    return {"branch": branch, "base": base, "base_ref": base_ref, "merge_base": merge_base,
            "delta": delta, "overlap_files": overlap, "changelog_entries": entries}


def format_brief(brief):
    if not brief["delta"]:
        return "`%s` already carries everything on `%s` -- nothing to explain." % (
            brief["branch"], brief["base_ref"])
    lines = ["What `%s` did while `%s` was away (%d commit(s)):" % (
        brief["base_ref"], brief["branch"], len(brief["delta"]))]
    for c in brief["delta"]:
        lines.append("- `%s` %s" % (c["sha"][:12], c["subject"]))
        lines.append("    %s" % _describe(c["context"]))
    lines.append("")
    if brief["overlap_files"]:
        lines.append("Files both sides touched since the merge-base -- where a conflict is "
                     "possible:")
        for f in brief["overlap_files"]:
            lines.append("- `%s`" % f)
    else:
        lines.append("No file was touched by both sides -- the rebase should apply cleanly.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- the rebase (§4)


def conflicted_files(run, cwd):
    """Every path with an unmerged (conflict) entry in the current tree."""
    out = run(cwd, ["git", "diff", "--name-only", "--diff-filter=U"])
    return sorted({line.strip() for line in str(out or "").splitlines() if line.strip()})


def push_branch(run, cwd, remote, branch, accepted=(), pre_head=None, base_ref=None, lease_sha=None):
    """`git push --force-with-lease <remote> HEAD:<branch>` -- the ONE force-with-lease push this
    skill performs, so there is exactly one call site to reason about rather than several that
    could quietly drift apart (#2319: `conflict_walk.walk_conflicts` reuses this directly for its
    own post-resolution push instead of hand-rolling a second copy).

    Never raises: a stale lease means somebody else pushed to this branch between our fetch and
    this push -- their commits are intact, ours are still local and nothing was overwritten. That
    is the mechanism working, not a failure to hide from the human; report it plainly so they can
    re-run (fetch again, brief/walk again) rather than silently losing the local work.

    #144, review block #2: THE TREE GUARD LIVES HERE, at the single chokepoint, so every caller is
    behind it -- `attempt_rebase`, `conflict_walk.walk_conflicts`'s DONE push, its
    `_manual_recovery_push`, and Slack's `--rebase` (which calls `attempt_rebase`). Before pushing,
    HEAD is compared against `<remote>/<branch>` -- exactly the commit the lease will overwrite --
    with `feature_rebase.dropped_paths`; a HEAD that would remove or roll back content that commit
    has is REFUSED and nothing is pushed. No remote-tracking ref means nothing is overwritten (the
    lease then only creates the branch), so there is nothing to lose. A comparison that cannot be
    made (or times out) is a refusal too, never a pass. The LOCAL branch is left as it is: the
    refusal says how to put it back. `accepted` names paths a human explicitly resolved in this
    session (the walker's own `resolved` list): their loss is the human's decision -- ABANDON on a
    deleted-by-us file IS a deletion -- so only OTHER paths can refuse the push. The manual-recovery
    push has no such record and passes none, so everything it would lose refuses it.

    #278: `pre_head` is the head the branch had BEFORE the rebase being pushed; what the rebase
    itself loses (`pre_head` -> HEAD) is always measured. A loss already present between the remote
    tip and `pre_head` is exempt ONLY when it is the branch's own deliberate local DELETION --
    `feature_rebase.own_losses` against `base_ref` (a local, unpushed `git rm` commit of a path the
    base left alone: exempt; a ROLLBACK or any modification in that range, and losses attributable
    to a fetched upstream after an earlier LOCAL rebase: refused -- review blocks #1 and #2,
    subject to the current-ref provenance limits in branching-model §15). No `base_ref`, or a shallow
    clone, exempts nothing. All the guard's reads for one push share one wall-clock budget
    (`feature_rebase.guard_deadline`, `SIGMA_WATCH_CALL_TIMEOUT`); running out refuses.
    `None` derives `pre_head` from git's own record: when the branch reflog's newest entry is a
    rebase's own `(finish)` line for this branch, the pre-rebase head is `<branch>@{1}`
    (`pre_rebase_head`). Unknown (no such entry, reflogs off) falls back to the remote-tip
    comparison alone. Either way the fallback exempts nothing: it can refuse a healthy local
    deletion, and it never exempts a loss the attribution rule would refuse (a loss of content the
    remote never had, with no pre-rebase head to measure from, is not seen -- `_would_lose` and the
    refused-push marker cover the paths this skill itself rebases). The undo advice names
    `pre_head` when known, never the remote tip, because resetting to the remote tip would throw
    the unpushed local commits away. That reset only undoes the latest rebase. The refusal also
    names the retained remote commit as a content-recovery source for losses predating it.

    A branch a refused replay could not put back (`feature_rebase.push_refused`'s marker) is refused
    before anything is measured.

    Returns `{"ok": True, "why": ""}` or `{"ok": False, "why": <flat text>, "dropped": [...]}`."""
    marked = feature_rebase.push_refused(cwd, branch)
    if marked:
        return {"ok": False, "dropped": [], "why": marked}
    try:
        overwritten = _remote_tip(run, cwd, remote, branch)
        if pre_head is None:
            pre_head = pre_rebase_head(run, cwd, branch)
        refusal = None
        if overwritten or pre_head:
            lost, deadline = set(), feature_rebase.guard_deadline()    # one budget per push
            if overwritten:
                lost |= set(feature_rebase.dropped_paths(cwd, overwritten, "HEAD", deadline))
                if pre_head and pre_head != overwritten and lost:
                    already = set(feature_rebase.dropped_paths(cwd, overwritten, pre_head,
                                                               deadline)) & lost
                    lost -= feature_rebase.own_losses(cwd, overwritten, pre_head, base_ref, already,
                                                      deadline)
            if pre_head:
                lost |= set(feature_rebase.dropped_paths(cwd, pre_head, "HEAD", deadline))
            refusal = feature_rebase.refuse_losing_push(cwd, overwritten, "HEAD", accepted,
                                                        dropped=lost)
    except Exception as exc:                    # noqa: BLE001 - unmeasured is never "nothing lost"
        return {"ok": False, "dropped": [],
                "why": "refused to push %s: the pre/post tree comparison against %s/%s could not "
                       "be made, so nothing was pushed: %s" % (branch, remote, branch, _flat(exc))}
    if refusal is not None:
        dropped, why = refusal
        if pre_head:
            undo = ("`git reset --keep %s` undoes only this rebase; it does not recover losses "
                    "already present in that pre-rebase head" % pre_head)
        else:
            undo = ("the head it had before the rebase is in `git reflog %s`; `git reset --keep "
                    "<that sha>` undoes that rebase but may not recover earlier losses "
                    "(not the remote tip %s -- that would also discard "
                    "any local commits not yet pushed)" % (branch, overwritten[:12]))
        recovery = (". The retained remote tip %s (%s/%s) is a recovery source for the "
                    "paths it still contains; inspect it with `git show %s:<path>` and restore chosen "
                    "content without discarding unpushed local commits"
                    % (overwritten, remote, branch, overwritten)) if overwritten else ""
        return {"ok": False, "dropped": dropped,
                "why": "%s/%s: %s. The local branch still holds the rewritten history -- %s%s; if "
                       "losing or rolling back those paths IS intended (only a plain local "
                       "deletion is ever let through without you), push it yourself with `git push "
                       "--force-with-lease %s HEAD:%s`" %(remote, branch, why, undo, recovery, remote, branch)}
    try:
        # `lease_sha` is given only under the upkeep opt-in: a lease on the exact tip read BEFORE the rebase, which no later
        # fetch in the same repository can re-arm (a bare lease compares with whatever was fetched last). None: the legacy argv.
        lease = "--force-with-lease" if lease_sha is None else "--force-with-lease=%s:%s" % (branch, lease_sha)
        run(cwd, ["git", "push", lease, remote, "HEAD:%s" % branch])
    except Exception as exc:                    # noqa: BLE001 - a refused lease is an outcome, not a crash
        return {"ok": False, "why": _flat(exc)}
    return {"ok": True, "why": ""}


def _rebase_finish_re(branch):
    """The branch-reflog subject a rebase writes when it lands on `branch` (#278). Measured against
    real git 2.55: `rebase (finish): refs/heads/<b> onto <sha>` for a plain, `--apply`, `-i`, or
    conflicted-then-`--continue`/`--skip` rebase and for a `pull --rebase` that stopped and was
    continued; `pull <its own argv> (finish): ...` for a `pull --rebase` that landed in one go; and
    `rebase (continue) (finish): ...` where the reflog action carries the continue (a caller-set
    `GIT_REFLOG_ACTION`; measured with that set). Older gits' apply backend wrote `rebase finished:
    ...` -- accepted, not measured here (no such git on hand). The
    subject must name THIS branch and an `onto` sha, so nothing but a rebase's own landing matches;
    a pull argv with a `:` in it (a refspec) does not, and reads as unknown -- conservative."""
    name = re.escape("refs/heads/%s" % branch)
    return re.compile(r"^(?:(?:rebase|pull)(?: [^:]*)? \(finish\)|rebase finished): %s onto "
                      r"[0-9a-f]{7,}$" % name)


def pre_rebase_head(run, cwd, branch):
    """The head `branch` had before the rebase that JUST finished on it, or "" when that cannot be
    read from git itself (#278). A rebase moves `refs/heads/<branch>` exactly once, when its last
    step lands, and records that move in the branch's own reflog (`_rebase_finish_re`: `rebase
    (finish): ...`, `pull ... (finish): ...`, and the other shapes git writes) -- so when that is
    the NEWEST entry, `<branch>@{1}` is the pre-rebase head. Anything else newest (a reset, a
    commit since) means the answer is not known; never raises."""
    ref = "refs/heads/%s" % branch
    try:
        subject = str(run(cwd, ["git", "reflog", "show", "-1", "--format=%gs", ref]) or "").strip()
        if not _rebase_finish_re(branch).match(subject):
            return ""
        return str(run(cwd, ["git", "rev-parse", "--verify", "-q", "%s@{1}" % ref]) or "").strip()
    except Exception:                           # noqa: BLE001 - unknown, never a guess
        return ""


def _remote_tip(run, cwd, remote, branch):
    """The sha `refs/remotes/<remote>/<branch>` holds -- what `--force-with-lease` with no explicit
    expectation compares against, so what a push would overwrite -- or "" when there is no such
    ref. `for-each-ref`, not `rev-parse --verify`, because a missing ref must read as "" while a
    failed read RAISES (the caller refuses the push); the refname is matched exactly because a
    for-each-ref pattern also matches from the start up to a slash."""
    ref = "refs/remotes/%s/%s" % (remote, branch)
    out = str(run(cwd, ["git", "for-each-ref", "--format=%(objectname) %(refname)", ref]) or "")
    for line in out.splitlines():
        sha, _, name = line.strip().partition(" ")
        if name == ref:
            return sha
    return ""


def attempt_rebase(run, cwd, remote, branch, base, lease=False):
    """Rebase `branch` onto `<remote>/<base>` in the CURRENT checkout (autostash always passed,
    matching `work.rebase()`'s own convention -- a no-op on a clean tree), then a lease-guarded
    push. Never raises, never calls `rebase --abort`: on a genuine conflict the tree is left
    exactly as `git rebase` itself leaves a stopped one, for the human to resolve by hand (design
    §4/§5's framing -- the interactive walker is a later slice)."""
    base_ref = "%s/%s" % (remote, base)
    behind = str(run(cwd, ["git", "rev-list", "--count", "%s..%s" % (branch, base_ref)]) or "").strip()
    if behind == "0":
        return {"outcome": CURRENT, "why": ""}
    pre_head = str(run(cwd, ["git", "rev-parse", "HEAD"]) or "").strip()
    lease_sha = None
    if lease:
        try:
            lease_sha = _remote_tip(run, cwd, remote, branch)
        except Exception as exc:                # noqa: BLE001 - #1017: an unreadable tip is an outcome, never a raise
            return {"outcome": FAILED, "why": "could not read %s/%s for the push lease: %s"
                                              % (remote, branch, _flat(exc)), "files": []}
    try:
        # `--rebase-merges` (#2756): a plain rebase flattens a merge-commit landing onto the
        # first-parent line, where upkeep's no-direct-commits check then refuses it forever.
        run(cwd, ["git", "rebase", "--autostash", "--rebase-merges", base_ref])
    except Exception as exc:                    # noqa: BLE001 - a conflict is an outcome, not a crash
        if feature_rebase.rebase_stopped(run, cwd):
            return {"outcome": CONFLICT, "why": _flat(exc), "files": conflicted_files(run, cwd)}
        return {"outcome": FAILED, "why": _flat(exc), "files": []}
    if run(cwd, ["git", "diff", "--name-only", "--diff-filter=U"]):
        # `work.rebase()`'s own guard, reused: the rebase COMMAND can report success while its own
        # final autostash-pop step conflicts silently underneath it ("Successfully rebased"
        # describes the replayed commits, not the stash). Recover to exactly the state before this
        # call rather than force-push a tree carrying literal conflict markers.
        run(cwd, ["git", "reset", "--hard", pre_head])
        if run(cwd, ["git", "stash", "list"]):
            run(cwd, ["git", "stash", "pop"])
        return {"outcome": FAILED, "files": [],
                "why": "the worktree's own uncommitted changes conflict with what's now on %s "
                       "(autostash pop conflict)" % base_ref}
    # #144: THE SAME TREE GUARD upkeep's own force-push sits behind, before this one. A replay onto
    # a base holding a revert of the branch's own work succeeds cleanly and would publish the loss.
    refused = _would_lose(run, cwd, pre_head, base_ref, branch)
    if refused is not None:
        return refused
    push = push_branch(run, cwd, remote, branch, pre_head=pre_head, base_ref=base_ref, lease_sha=lease_sha)
    if not push["ok"]:
        if push.get("dropped"):
            return {"outcome": WOULD_DROP, "files": push["dropped"], "why": push["why"]}
        return {"outcome": FAILED, "files": [], "why": push["why"]}
    return {"outcome": REBASED, "why": ""}


def attended_rebase(config, sdlc_dir, run, cwd, remote, branch, base, lock_timeout=None):
    """The attended door's rebase. Gate closed: exactly `attempt_rebase(run, cwd, remote, branch, base)`, nothing else.

    Under the upkeep opt-in the door takes the unit's rebase lock HERE, in the caller, and not inside `attempt_rebase`
    (the chat door calls that while it already holds the same lock), and pushes with a lease on the exact remote tip read
    before the rebase. Another holder of the lock is a refusal, never a wait without end. A branch that is not a unit branch
    has no unit lock; it still gets the explicit lease."""
    upkeep = _load("feature_upkeep")
    if not upkeep.enabled(config):
        return attempt_rebase(run, cwd, remote, branch, base)
    prefix = feature_rebase.features.BRANCH_PREFIX
    unit = branch[len(prefix):] if branch.startswith(prefix) else ""
    fd = None
    if unit:
        try:
            lock = feature_rebase.lock_path(sdlc_dir, unit)
            lock.parent.mkdir(parents=True, exist_ok=True)
            fd = feature_rebase._acquire(lock, timeout=feature_rebase.LOCK_TIMEOUT if lock_timeout is None else lock_timeout)
        except (OSError, ValueError) as exc:
            return {"outcome": FAILED, "files": [], "why": "the unit lock could not be taken: %s" % _flat(exc)}
        if fd is None and feature_rebase.sync.fcntl is None:
            return {"outcome": FAILED, "files": [],
                    "why": "locking is not supported on this platform, so the opt-in attended rebase is refused; nothing was changed"}
        if fd is None:
            return {"outcome": FAILED, "files": [],
                    "why": "another rebase of %s holds the unit lock (or its lock directory is not writable); nothing was changed" % branch}
    try:
        return attempt_rebase(run, cwd, remote, branch, base, lease=True)
    finally:
        if fd is not None:
            feature_rebase._release(fd)


def _would_lose(run, cwd, pre_head, base_ref, branch=""):
    """None when the rebased HEAD keeps everything `pre_head` had, else the refusal report.

    On a refusal the LOCAL branch is put back with `git reset --keep <pre_head>` -- `--keep`, not
    `--hard`, because an autostash may just have re-applied the human's uncommitted edits, and
    `--keep` refuses rather than discard them. If it refuses, the report says how to undo by hand,
    and (#278) `feature_rebase.mark_push_refused` records it: the branch still holds the lossy
    replay, a later run would find it current and a recovery push would publish it, so
    `push_branch` refuses `branch` until HEAD is back at `pre_head`. Fails closed: a comparison
    that cannot be made is a `FAILED` with nothing pushed."""
    try:
        head = str(run(cwd, ["git", "rev-parse", "HEAD"]) or "").strip()
        dropped = feature_rebase.dropped_paths(cwd, pre_head, head)
        why = ("bringing it forward onto %s would remove or roll back %d tracked path(s) it has "
               "(%s) -- the base most likely holds a revert of the branch's own commits; see "
               "docs/branching-model.md §3b" % (base_ref, len(dropped), ", ".join(dropped[:3]) +
                                               (" and %d more" % (len(dropped) - 3)
                                                if len(dropped) > 3 else "")))
        outcome = WOULD_DROP
    except Exception as exc:                    # noqa: BLE001 - unmeasured is never "nothing lost"
        dropped, outcome = [], FAILED
        why = "the pre/post tree comparison could not be made: %s" % _flat(exc)
    if outcome == WOULD_DROP and not dropped:
        return None
    try:
        run(cwd, ["git", "reset", "--keep", pre_head])
        why += "; nothing was pushed and the local branch was put back at %s" % pre_head[:12]
    except Exception as exc:                    # noqa: BLE001
        marker = feature_rebase.mark_push_refused(cwd, branch, pre_head, why) if branch else ""
        why += ("; nothing was pushed, but putting the branch back failed (%s) and it is still "
                "rebased -- undo it with `git reset --keep %s`; %s"
                % (_flat(exc), pre_head,
                   "pushes of %s are refused until it is (marker %s)" % (branch, marker) if marker
                   else "the refusal could NOT be recorded, so do this before anything pushes it"))
    return {"outcome": outcome, "files": dropped, "why": why}


def format_conflict(brief, report, run, cwd, sdlc_dir=None):
    """`sdlc_dir` (#2321): this call is BY CONSTRUCTION the moment of first conflict detection
    (`rebase_brief.py rebase`'s own CLI never resumes a stopped rebase -- `attempt_rebase` just ran
    and returned CONFLICT for the first time this invocation), so each file's context is captured
    HERE via `snapshot_conflict_context` -- persisted for a later, separate `conflict_walk.py walk`
    invocation to read back rather than re-derive against whatever the base has since moved to. A
    falsy `sdlc_dir` (no `.sdlc` in scope) degrades to `file_context`'s own fresh, unpersisted
    answer -- this module's pre-#2321 behaviour."""
    lines = ["`%s` conflicts with `%s` -- the rebase is stopped here for you to resolve by hand:"
             % (brief["branch"], brief["base_ref"])]
    for path in report["files"]:
        ctx = snapshot_conflict_context(sdlc_dir, brief["branch"], run, cwd, brief["merge_base"],
                                        brief["base_ref"], path, brief["changelog_entries"])
        lines.append("")
        lines.append("`%s`%s:" % (path, " -- DELETED on %s" % brief["base"]
                                  if ctx["deleted_on_base"] else ""))
        if not ctx["commits"]:
            lines.append("  - %s" % NO_EXPLANATION)
        for c in ctx["commits"]:
            lines.append("  - `%s` %s -- %s" % (c["sha"][:12], c["subject"],
                                                _describe(c["context"])))
    lines.append("")
    lines.append("Resolve by hand, `git add` each file, then `git rebase --continue` -- or "
                 "`git rebase --abort` to back out.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- CLI


USAGE = ("usage: rebase_brief.py brief <sdlc_dir> [branch] | "
         "rebase_brief.py rebase <sdlc_dir> [branch]")


def main(argv):
    """`rebase_brief.py brief <sdlc_dir> [branch]` | `rebase_brief.py rebase <sdlc_dir> [branch]`.

    `brief` never mutates anything, on any branch. `rebase` also attempts the rebase, but ONLY
    when the resolved branch is the currently checked-out one -- checking on a branch you are not
    on gets the brief, never a mutation of a tree you are not sitting in front of."""
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    if len(argv) < 3 or argv[1] not in ("brief", "rebase"):
        print(USAGE, file=sys.stderr)
        return 2
    sdlc_dir = argv[2]
    explicit = argv[3] if len(argv) >= 4 else None
    config = state.load_config(sdlc_dir)
    cwd = str(pathlib.Path(sdlc_dir).parent)
    run = feature_rebase._run
    remote = resolve_remote(config)
    base = resolve_base(config)
    branch = resolve_branch(run, cwd, explicit)
    if not base or base == branch:
        print("no integration branch to compare against (work.base is %r)" % base, file=sys.stderr)
        return 1
    brief = assemble_brief(run, cwd, remote, branch, base)
    print(format_brief(brief))
    if argv[1] == "brief":
        return 0
    if current_branch(run, cwd) != branch:
        print("\n`%s` is not the currently checked-out branch -- brief only, no rebase attempted."
              % branch, file=sys.stderr)
        return 0
    print("")
    report = attended_rebase(config, sdlc_dir, run, cwd, remote, branch, base)
    if report["outcome"] == CURRENT:
        print("`%s` already carries everything on `%s` -- nothing to do." % (branch, base))
        return 0
    if report["outcome"] == REBASED:
        print("`%s` rebased onto `%s` and pushed." % (branch, base))
        return 0
    if report["outcome"] == CONFLICT:
        print(format_conflict(brief, report, run, cwd, sdlc_dir))
        return 1
    print("`%s` was NOT rebased: %s" % (branch, report["why"]))
    return 1


def _state_guard(argv):
    """#708: refuse a committed symlink under .sdlc/state or .sdlc/journey before any write."""
    import importlib.util as _u
    import pathlib as _p
    spec = _u.spec_from_file_location("_guard_state", _p.Path(__file__).resolve().parent.parent.parent / "sigma-loop" / "scripts" / "state.py")
    mod = _u.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.guard_argv(argv, _p.Path(__file__).name)


if __name__ == "__main__":
    raise SystemExit(_state_guard(sys.argv) or main(sys.argv))
