#!/usr/bin/env python3
"""Assemble the context an INDEPENDENT reviewer needs — the project, never the author.

THE PROBLEM. A maker that reviews its own work rationalizes it; on a lower-tier model that confirmation
bias amplifies hallucination exactly where a review is supposed to catch it. So every review gate
(plan-review, code review, the post-PR review) must run as a FRESH reviewer that never saw the maker's
reasoning. But a fresh reviewer handed only the diff has the opposite failure: it cannot see BLAST
RADIUS — the callers a small change breaks two files away, the invariant it quietly violates — because
the change is on screen and the impact surface is not. The reviewer needs the PROJECT (what it is for,
its rules, the whole codebase to read), not the AUTHOR (their justifications).

WHERE THE BOUNDARY IS. A python script cannot spawn a subagent — only the agent can (the same boundary
`slices.py` states). So this module COMPUTES the reviewer's context PACK and the SKILL prose DISPATCHES
one fresh subagent per review, fed only this pack. The value here is that the payload is assembled by
code: the maker's transcript is excluded BY CONSTRUCTION (there is no field for it), and what the
reviewer is told — "trace blast radius across the whole repo" — is consistent
across every gate and testable, instead of prose each phase is trusted to reproduce.

WHAT GOES IN. The project's own words, north-star first (same ordering `sigma-context` uses), but here
UN-GATED by the knowledge graph and reviewer-framed: the north-star (vision / strategy / non-goals /
numbered architecture rules the plan is judged against), project.md + every governing CLAUDE.md
(root AND directory-scoped), the
contracts dir, the goal, and a pointer to the artifact under review. Never the maker's context.

Fail-open: a missing file drops its line, never raises — a reviewer with a partial brief still reviews;
one that crashed assembling the brief reviews nothing. ASCII-only output (a non-utf8 locale must not
break it). Zero deps.
"""
import collections
import datetime
import fnmatch
import importlib.util
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile


def _load(name):
    """Import a sibling script by path — the kit's standard zero-install module loader."""
    spec = importlib.util.spec_from_file_location(
        name, pathlib.Path(__file__).resolve().parent / (name + ".py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def _fetch_issue(sdlc_dir, number, source=None):
    """A GitHub-mode goal's title+body -> {"title": str, "body": str}; {} on ANY failure.

    In github mode `$goal` is a bare NUMBER, so a brief that only POINTS at `gh issue view` has its
    grounding contingent on the reviewer choosing to run a command — while local mode inlines the
    goal body outright. That asymmetry is the bug: a reviewer judging fitness needs the acceptance
    criteria IN the pack, in both modes.

    Read-only, and routed through `sources`' own `_run` chokepoint rather than a bare shell-out, so
    the recording-fake tests that cover every other gh call cover this one too. Fail-open (this
    module's rule): no config, no gh, no network, or a renamed field all degrade to {} and the caller
    falls back to the pointer text that shipped before."""
    try:
        src = source
        if src is None:
            src = _load("sources").get_source(sdlc_dir, _load("state").load_config(sdlc_dir))
        data = src.fetch_title_body(number) or {}
        return {"title": (data.get("title") or "").strip(),
                "body": (data.get("body") or "").strip()}
    except Exception:               # noqa: BLE001 - fail-open by design; see docstring
        return {}


def _issue_text(issue):
    """A fetched issue rendered for the brief, or "" if it carried nothing worth printing."""
    title, body = issue.get("title", ""), issue.get("body", "")
    return ("# %s\n\n%s" % (title, body)).strip() if (title or body) else ""


def _research_note_posted(sdlc_dir, number, source=None):
    """True iff a Research phase note is already on this github-mode issue's comment thread -- the
    `loop.py note` / sigma-research SKILL.md convention. False on ANY failure, including a source with
    no comment seam at all: this only ever SOFTENS the missing-dossier gap in `_missing()` below, it
    never suppresses the review, so an unreadable timeline must default to the original, stronger
    wording rather than silently claim coverage that was never confirmed.

    Closes a false negative confirmed live on issue #1762: `_missing()` used to assert "blast radius
    was never measured" whenever no LOCAL FILE dossier existed, but github-discovery mode posts
    Research findings as an issue COMMENT instead (sigma-research/SKILL.md), never a file. The regex
    accepts both SKILL.md's prescribed lowercase example ("research: <summary>") and the live-observed
    uppercase convention ("RESEARCH (lane: medium): ...") -- verified against the real comment text.

    Mirrors `_fetch_issue`'s own source-injection exactly (same fallback to a real `get_source`).
    `fetch_comments_strict` is the established comment-read method this codebase already reads off
    `source` in loop.py/feature_owner.py/feature_propagate.py -- there duck-typed via `getattr`
    because those callers need a different message for "no such method" vs. "method raised"; this
    function needs neither, so it calls the method directly and lets the outer `except` below catch
    both identically, deliberately relying on `fetch_comments_strict` being the RAISING sibling of
    `fetch_title_body` (never silently degrades a malformed payload to "no comments")."""
    try:
        src = source
        if src is None:
            src = _load("sources").get_source(sdlc_dir, _load("state").load_config(sdlc_dir))
        comments = src.fetch_comments_strict(number).get("comments") or []
        return any(re.match(r"(?i)^research\s*(\(.*?\))?\s*:", (c.get("body") or "").strip())
                   for c in comments)
    except Exception:               # noqa: BLE001 - fail-open by design; see docstring
        return False


def _parent(sdlc_dir, goal_body, source=None):
    """The parent goal a decomposition child was split out of, rendered for the brief, or "".

    A child goal's FIRST LINE carries `goal_size.DECOMPOSED_FROM_MARKER` (`loop.py:754`,
    `backlog_check.py:465`). Without the parent, a reviewer judging child 3-of-5 can only ask "is
    this correct?", never "is this the right piece of the whole?" — which is the alignment question
    a decomposed goal most needs asked.

    ONE level only: a child's parent, never a recursive climb. A grandparent adds context the
    reviewer cannot act on and an unbounded number of gh calls, and a marker cycle would not
    terminate.

    The first-line read is deliberately IDENTICAL to `loop.py`'s (`body.splitlines()[:1]`, no strip)
    so the two checks cannot disagree about whether a goal is a child. A local goal file never
    matches — `decompose_check` only ever files a GitHub issue, and a local goal's first line is its
    `---` frontmatter fence — so this correctly no-ops in local mode. Do not "fix" that by stripping
    frontmatter: it would make this check diverge from the one that defines the marker.

    #688: the number is the LEADING digit run right after the marker (an optional `#`, then
    digits), matched with `re.match` rather than collected by scanning every character in the
    remainder for `.isdigit()`. Today's only real marker shape has no digit anywhere past the
    number, so the two approaches agree here — but a scan-every-char join would silently
    concatenate a future marker that ever appended a trailing digit (e.g. `...=#7 (part 2)`) into
    `"72"`, fetching the wrong parent issue with no error. Anchoring at the start of the remainder
    and stopping at the first non-digit closes that before it can happen."""
    lines = (goal_body or "").splitlines()[:1]
    if not lines:
        return ""
    try:
        marker = _load("goal_size").DECOMPOSED_FROM_MARKER
    except Exception:               # noqa: BLE001 - fail-open; no marker constant, no parent block
        return ""
    at, spelled = _load("legacy").find_marker(lines[0], marker)   # #239: either spelling
    if at == -1:
        return ""
    match = re.match(r"\s*#?(\d+)", lines[0][at + len(spelled):])
    number = match.group(1) if match else ""
    return _issue_text(_fetch_issue(sdlc_dir, number, source)) if number else ""


def _artifact_issue(sdlc_dir, number, source=None):
    """#1826: `goal-review`'s own artifact fetch — mirrors `_parent()` immediately above it. The
    thing under review at this phase IS an issue (the story/epic ticket `goal-design` produced its
    write-up against), never a diff or a plan file already sitting on the local filesystem/branch —
    so (the same reasoning `_parent()` and the `goal` fetch a few lines down in `brief()` already
    apply) it is fetched and INLINED here rather than left as a live-diff-style pointer the
    reviewer has to think to run a command for. "" on any failure — fail-open, this module's rule;
    the caller degrades to a pointer-only line, exactly like `goal_text`'s own fetch does."""
    return _issue_text(_fetch_issue(sdlc_dir, number, source)) if number else ""

#: The gates this brief serves and what each reviews. Kept explicit so an unknown `--for` is a loud
#: error, not a silently-empty brief that reads as "nothing to review".
PHASES = {
    "plan-review": "the implementation PLAN (before any code)",
    "code-review": "the branch DIFF (before the PR)",
    "pr-review": "the opened PR's real, mergeable diff (after commit + CI)",
    "retro": "the shipped change vs its stated intent",
    "goal-review": "the story/epic issue and its design write-up (before any branch/code exists)",
}

#: Project docs pulled into the pack, in relevance order — north-star first (the highest grounding: a
#: plan/diff is judged against its strategy, non-goals, and architecture rules). Each is optional; a
#: repo without one just drops that line.
_PROJECT_DOCS = [
    ("context/north-star.md", "north-star (vision / strategy / non-goals / architecture rules)"),
    ("project.md", "project (stack, conventions, verify command)"),
]

#: `_missing` asks about the north-star BY NAME (#2147), so its key is named once here rather than
#: spelled again at the gap check, where a typo would silently mean "never missing".
_NORTH_STAR_REL = _PROJECT_DOCS[0][0]

#: Phases whose brief needs a Research dossier to judge blast radius. Shared between `brief()`'s
#: research_note_seen gate and `_missing()`'s gap check so the two can never desync about which
#: phases this covers.
_DOSSIER_PHASES = ("plan-review", "code-review", "pr-review")


#: #993: `context.inline_max_bytes` default; a configured document larger than this is a pointer.
_INLINE_MAX_DEFAULT = 16384


class DocRef(collections.namedtuple("DocRef", "path state detail")):
    """One configured reference document: repo-relative `path`, `state` in present / absent /
    unreachable, and `detail` (the reason when unreachable)."""
    __slots__ = ()


def _context_block(config):
    block = config.get("context") if isinstance(config, dict) else None
    return block if isinstance(block, dict) else {}


def documents(config, repo_root):
    """`list[DocRef]` for `context.documents`, or None when the key is absent or not a list (closed:
    the brief is then byte-identical to before this key existed). `[]` is a real answer."""
    named = _context_block(config).get("documents")
    if not isinstance(named, list):
        return None
    ns = _load("north_star")
    refs = []
    for rel in named:
        state, detail = ns.resolve_doc(repo_root, rel)
        refs.append(DocRef(rel if isinstance(rel, str) else repr(rel), state, detail))
    return refs


def inline_max_bytes(config):
    value = _context_block(config).get("inline_max_bytes")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return _INLINE_MAX_DEFAULT
    return value


def render_doc(ref, max_bytes, repo_root="."):
    """`("inline", text)` for a present document within the cap, else `("pointer", text)`."""
    path = pathlib.Path(repo_root) / ref.path
    try:
        size = path.stat().st_size
    except OSError:
        size = max_bytes + 1
    if size <= max_bytes:
        body = _read(path)
        if body:
            return "inline", "## reference document: %s\n%s" % (ref.path, body)
    return "pointer", ("## reference document: %s\nLarge (over %d bytes), so not inlined - read "
                       "`%s` for it." % (ref.path, max_bytes, ref.path))


def _read(path):
    """Fail-open read: an unreadable or absent file contributes nothing, never an exception — a partial
    brief is a working reviewer, a crash is a skipped review."""
    try:
        return pathlib.Path(path).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return ""


#: Every `CLAUDE.md` that governs the repo, root first. Discovery is git's INDEX, not a filesystem
#: walk: `rglob` walked 469,372 paths in 1.4-4.5s on this repo against `git ls-files`' 0.012s, and git
#: already knows to skip the ignored trees (vendored `node_modules`, `.sdlc/work`, linked worktrees)
#: that a hand-written denylist would have to re-derive for every adopter. TWO EXACT pathspecs, never
#: `*CLAUDE.md` - that glob also matches `NOTCLAUDE.md` and `web/MYCLAUDE.md` (measured, not assumed).
#: At 100 nested files this adds ~100 short path lines (~3KB) to a brief already tens of KB, so the
#: list is stated rather than capped. Fail-open, this module's rule: no git, not a repo, or a wedged
#: call degrades to the root file alone - exactly what shipped before.
def _convention_files(repo_root):
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "--cached", "--others", "--exclude-standard",
             "--", "CLAUDE.md", "*/CLAUDE.md"],
            capture_output=True, text=True, timeout=10)
    except Exception:               # noqa: BLE001 - fail-open by design; see comment above
        return []
    if proc.returncode != 0:
        return []
    return sorted({ln.strip() for ln in proc.stdout.splitlines() if ln.strip()})


def _project_sdlc_dir(sdlc_dir):
    """The `.sdlc` that actually holds the project docs, when `sdlc_dir` is a worktree's empty one.

    #1778: `sigma-plan-review/SKILL.md` prescribes `review_context.py brief .sdlc "<goal>"`, and the
    loop runs that phase from inside the goal's WORKTREE — where `.gitignore`'s `.sdlc/*` means
    `.sdlc/context/` does not exist at all. Measured on this repo: the same command produced a
    14,686-byte brief from the main checkout and 1,816 bytes from the worktree, silently dropping
    BOTH project docs. Returns None when nothing better than `sdlc_dir` can be established, so the
    caller keeps exactly the behaviour that shipped before."""
    try:
        root, _ = _load("north_star").project_root(pathlib.Path(sdlc_dir).parent)
    except Exception:               # noqa: BLE001 - fail-open, this module's rule
        return None
    if root is None:
        return None
    resolved = pathlib.Path(root) / ".sdlc"
    try:
        if resolved.resolve() == pathlib.Path(sdlc_dir).resolve():
            return None             # already the project's own .sdlc; nothing to add
    except OSError:
        return None
    return resolved


def _project_context(sdlc_dir, repo_root):
    """`(text, docs_found)` — the project's own words the reviewer is grounded in, and whether the
    two PROJECT DOCS specifically were among them. north-star + project.md from .sdlc, every
    governing CLAUDE.md (the root one AND any directory-scoped ones) and any contracts dir from the
    repo root. Pointers where a file is large
    (CLAUDE.md, contracts) so the reviewer pulls detail on demand rather than drowning in it.

    `docs_found` is returned separately because the two are NOT interchangeable: `_missing()` used to
    infer "no north-star or project.md" from the whole string being empty, which the conventions
    block below makes truthy on its own — so on a worktree brief both docs vanished AND the gap line
    that should have said so was suppressed (#1778, confirmed by running the shipped command).

    It is a SET of the rel paths found, not a bool, because #1778's fix left the same defect one
    level down: a single bool set by EITHER doc meant a present `project.md` suppressed the notice
    that the north-star was gone (#2147, again confirmed by running the shipped command). Two docs,
    two answers."""
    base = pathlib.Path(sdlc_dir)
    # A worktree's `.sdlc` is not the project's; fall back to the real one PER DOC, and only for the
    # two project docs. Purely additive — it can restore a doc that was missing, never redirect the
    # goal, dossier or plan reads, whose worktree-local copies are deliberate.
    fallback = _project_sdlc_dir(sdlc_dir)
    lines, docs_found = [], set()
    for rel, label in _PROJECT_DOCS:
        body = _read(base / rel)
        if not body and fallback is not None:
            body = _read(fallback / rel)
        if body:
            docs_found.add(rel)
            lines.append("## %s\n%s" % (label, body))

    root = pathlib.Path(repo_root)
    try:
        cfg = _load("state").load_config(sdlc_dir)
    except Exception:                       # noqa: BLE001 - fail open: no config, no extra documents
        cfg = {}
    refs = documents(cfg, repo_root) if isinstance(cfg, dict) else None
    for ref in refs or []:
        if ref.state == "present":
            lines.append(render_doc(ref, inline_max_bytes(cfg), repo_root)[1])
        elif ref.state == "unreachable":
            lines.append("## reference document: %s\nNOT READ - %s." % (ref.path, ref.detail))
        else:
            lines.append("## reference document: %s\nNOT FOUND - it is named in context.documents but "
                         "absent from the repository." % ref.path)
    if refs == []:
        lines.append("## reference documents\nNo reference documents named in context.documents.")
    # The root check stays a plain stat, not a git query: a drop-in repo with no git still gets its
    # conventions. Discovery only ADDS the directory-scoped ones on top.
    conventions = []
    if (root / "CLAUDE.md").is_file():
        conventions.append("Read `CLAUDE.md` at the repo root - its rules bind this change.")
    nested = [f for f in _convention_files(repo_root) if f != "CLAUDE.md"]
    if nested:
        conventions.append(
            "These directory-scoped `CLAUDE.md` files bind this change too - each governs the subtree "
            "it sits in, so judge anything under that path against it as well: %s"
            % ", ".join("`%s`" % f for f in nested))
    if conventions:
        lines.append("## conventions\n%s" % "\n".join(conventions))
    contracts = root / "docs" / "CONTRACTS"
    if contracts.is_dir():
        frozen = sorted(p.name for p in contracts.glob("*") if p.is_file())
        if frozen:
            lines.append("## contracts\nHonor `docs/CONTRACTS/` (a FROZEN contract is not yours to "
                         "change): %s" % ", ".join(frozen))
    return "\n\n".join(lines), docs_found


def _artifact_names(goal):
    """(`<name>.md` candidates in preference order, glob-or-None) for this goal's phase artifact.

    Split out of `phase_doc_file` for #2647, which added a SECOND resolver — the same question asked
    of a git ref rather than the filesystem. The naming rule is the thing that must not fork (see
    `phase_doc_file`'s docstring: two resolvers would let a goal be judged plan-less by the guard and
    plan-ful by the reviewer), so it lives here once and both callers read it. What deliberately does
    NOT live here is the #1808 tie-break, because the two resolvers cannot share one: a filesystem
    breaks the tie by mtime ("the freshest measurement"), and a git tree has no mtime at all."""
    # Path(goal).stem extracts only the filename component (no directories or extension), preventing traversal (#486).
    stem = pathlib.Path(goal).stem
    # Goals are `NNNN-slug.md`; an artifact may be filed under either the full stem or the bare slug.
    slug = stem.split("-", 1)[1] if "-" in stem and stem.split("-", 1)[0].isdigit() else stem
    exact = [n + ".md" for n in dict.fromkeys([stem, slug]) if n]
    # `stem.isdigit()` also means the glob can never carry a traversal or shell-metacharacter
    # payload — it is only digits.
    return exact, (stem + "-*.md" if stem.isdigit() else None)


def _git_out(root, args):
    """`git -C <root> <args>` stdout, or None on ANY failure. Never raises, never `check=True`.

    Same fail-open posture and the same `timeout=10` as `_convention_files` above, for the same two
    reasons: a brief that lost one input is a working reviewer while a crash is a skipped review,
    and a wedged git (an `index.lock`, a network filesystem, a credential helper waiting on a
    terminal) must not freeze the phase that called it."""
    try:
        proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                              timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _branch_doc(sdlc_dir, goal, subdir):
    """(pointer, text) for this goal's phase artifact read from its own BRANCH, or (None, "").

    #2647. `.sdlc/plans/<goal>.md` and `.sdlc/research/<goal>.md` are TRACKED files on
    `sdlc/<goal>` (`.gitignore`'s `!.sdlc/plans/` / `!.sdlc/research/` exceptions), while every
    documented gesture runs the brief from the MAIN checkout with `<sdlc_dir>` = `.sdlc` — a tree
    where they may never have existed. Measured on #2575: a 1072-line plan and a 466-line dossier,
    both present on `origin/sdlc/2575`, both reported to the reviewer as absent, with the plan's
    absence phrased as an instruction to "judge against the goal alone".

    This is a FALLBACK, not a replacement — `brief()` asks the filesystem first. That order is
    deliberate and is pinned by a test: `references/landing.md` ("the reviewer BRIEF reads the main
    checkout's, the PR's reviewer reads the branch's") splits the two copies on purpose, and a plan
    CORRECTION (#2237) is written into the main checkout's copy before anything commits it — so
    reading the branch first would serve the stale committed text, which is the same silent
    degradation this function exists to remove.

    The repo is derived from `sdlc_dir`, never from `brief()`'s `repo_root`: that argument means
    "where the conventions and contracts live" and defaults to the CWD, so a caller pointing it at
    another tree would silently desync the two. Same rule as `work.project_root`.

    The local ref is tried before `<remote>/<branch>` — during a goal's own run the local branch is
    authoritative and the push lags — but the remote is not optional: `work.py finish` deletes the
    local branch, so on the main checkout that measured this bug `git show sdlc/2575:...` answers
    "invalid object name" while `origin/sdlc/2575` resolves.

    The pointer handed to the reviewer is the runnable `git show <ref>:<path>`, not a path: that
    path does not exist on disk here, and pointing at a file the reviewer cannot open would be a
    second silent degradation. It needs a shell, which `reviewer.py`'s `subagent` and `process`
    mechanisms have and an operator's `command` reviewer may not — better than today's silence,
    not parity with a local file.

    Historical plan/research COST (before acceptance), measured on this repo:
    2 git calls per artifact resolved off a branch, 1 per ref that does
    not resolve — so a `pr-review` brief adds 6 when both plan and dossier come off the branch, 4
    when no ref exists, and 0 when both are on disk. ~10ms each against a 0.76s brief. The `ls-tree`
    is linear in the ref's tracked artifacts for that subdir (50 plan files here), so 100x that tree
    is still one listing, not 100 calls. Deliberately UN-memoised: the two subdirs repeat the same
    two `ls-tree`s, and a cache would have to be invalidated across a long-lived process for ~20ms.
    The ceiling to know is the timeout: it is PER CALL, so a wedged git costs up to 40-80s per
    brief, not 10. Acceptance adds a size check before reading the blob: at most four calls
    across the local/remote refs (40s timeout ceiling), no calls when its local file exists.
    Its listing cost is linear in that ref's acceptance filenames; its body is capped at 32 KiB.
    Acceptance lookup wall time has not been measured here."""
    root = pathlib.Path(sdlc_dir).resolve().parent
    rel = pathlib.PurePosixPath(pathlib.Path(sdlc_dir).resolve().name) / subdir
    try:
        work = _load("work")
        try:
            config = _load("state").load_config(sdlc_dir)
        except Exception:                             # noqa: BLE001 - a drop-in repo with no
            config = {}                               # config.json still gets work.DEFAULTS
        settings = work.settings(config)
        branch = "%s%s" % (settings["branch_prefix"], work.stem(goal))
        exact, pattern = _artifact_names(goal)
        refs = (branch, "%s/%s" % (settings["remote"], branch))
    except Exception:                                 # noqa: BLE001 - config/IO; fail open like the rest
        return None, ""
    for ref in refs:
        # `work.stem` returns a non-`.md` goal VERBATIM and `branch_prefix` is operator-configurable,
        # so an empty prefix would let a goal like `--upload-pack=...` reach argv as an option token.
        # The pathspec is always behind `--`; the ref cannot be, so it is checked instead (#486's
        # traversal defence has a twin here).
        if not ref or ref.startswith("-"):
            continue
        listing = _git_out(root, ["ls-tree", "--name-only", ref, "--", rel.as_posix() + "/"])
        if not listing:
            continue
        names = {pathlib.PurePosixPath(line).name for line in listing.splitlines() if line.strip()}
        hit = next((n for n in exact if n in names), None)
        if hit is None and pattern:
            # No mtime in a tree: the last name in sorted order, deterministically. Reachable only
            # when the working tree holds NO copy at all, so the two resolvers can never disagree
            # about one brief.
            hit = next(iter(sorted((n for n in names if fnmatch.fnmatch(n, pattern)), reverse=True)),
                       None)
        if hit is None:
            continue
        path = (rel / hit).as_posix()
        if subdir == "acceptance":
            size = _git_out(root, ["cat-file", "-s", "%s:%s" % (ref, path)])
            if not size or not size.strip().isdigit() or int(size) > _load("acceptance").MAX_BYTES:
                return None, ""
        text = _git_out(root, ["show", "%s:%s" % (ref, path)])
        if text and text.strip():
            return "git -C %s show %s:%s" % (root, ref, path), (text if subdir == "acceptance" else text.strip())
    return None, ""


def phase_doc_file(sdlc_dir, goal, subdir):
    """The path of the artifact filed for this goal under `.sdlc/<subdir>/`, or None.

    Split out from `_phase_doc` because the plan block needs the PATH as well as the text: it inlines
    only the outline and hands the reviewer the file to open when a heading is not enough.

    PUBLIC because it now has a second consumer outside this module: `work.py`'s pre-push guard
    (#1548) asks the same question — "which file is this goal's plan?" — before refusing to publish a
    branch that does not carry it. Deliberately the SAME resolver the reviewer brief uses, stem/slug
    fallback included: two resolvers would let a goal be judged plan-less by the guard and plan-ful
    by the reviewer it exists to serve.

    #1808: in github mode `$goal` is a BARE issue number with no slug of its own, but Research and
    Plan both routinely append one when they FILE the artifact — sigma-research/SKILL.md's own
    instruction is `.sdlc/research/<goal-slug>.md` — producing a real, on-disk name like
    `1756-repo-identity-declared-override.md`. The exact-name checks above never match that (the
    numeric stem has no dash to split on, so `slug == stem`), so a dossier that genuinely exists read
    as absent — confirmed live on goal #1756/PR #1806, and the second independent false-negative
    report for the identical goal. Scoped to a PURELY NUMERIC stem only: a local goal's stem already
    carries its own real slug and is matched exactly above, and loosening this for it too would let
    an unrelated same-prefixed file answer for the wrong goal. `stem.isdigit()` also means the glob
    pattern below can never carry a traversal or shell-metacharacter payload — it is only digits.
    Ties (a Research re-run refiled under a second slug) resolve to the most recently WRITTEN file:
    the freshest measurement is the one a reviewer should be pointed at."""
    base = pathlib.Path(sdlc_dir) / subdir
    if not (goal and base.is_dir()):
        return None
    exact, pattern = _artifact_names(goal)
    for name in exact:
        if (base / name).is_file():
            return base / name
    if pattern:                                       # #1808: <bare-issue-number>-<slug>.md
        candidates = sorted(base.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        if candidates:
            return candidates[0]
    return None


def plan_sha256(path):
    """sha256 hex of a plan file's RAW bytes: #258's one plan identity.

    Three sites must agree on it, so all three call this: the plan-review brief names it (`Plan
    sha256:`, `_plan_identity` below), `work.record_plan_review` refuses a verdict whose sha no copy
    of the plan still holds, and `work._plan_review_refusal` refuses `work.py pr` once the published
    plan no longer hashes to the recorded one. It takes a PATH and hashes bytes, never decoded or
    stripped text: `work._run` returns `stdout.strip()`, so a hash of `git show` output would lose the
    trailing newline and could never match a file's hash."""
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


_PLAN_IDENTITY_NOTE = ("This is the file to review. The dispatcher records your verdict against this "
                       "sha256; the PR gate (gates.plan_review) accepts it only for exactly these bytes.")


def _plan_identity(sdlc_dir, goal):
    """The plan-review brief's `Plan file:` / `Plan sha256:` section, or "" (#258).

    Names the REVIEWED copy (`main or branch`, `work.plan_copies`' rule): the main checkout's copy
    when it exists (no `work` load, no git call), else the copy committed on the goal's branch. The
    branch pointer is the runnable `git -C <root> show <branch>:<rel>` that `_branch_doc` already hands
    a reviewer, NEVER the worktree path: the documented gesture checks the brief with `reviewer.py
    check --scratch <worktree>`, which refuses any brief whose text names that path. The copy is
    committed and unmodified, so those `git show` bytes are the hashed bytes.

    Fail-open like the rest of the brief: any exception drops the section rather than the review. A
    brief with no such line found no plan, and `record-plan-review` then refuses its empty value."""
    try:
        plan = phase_doc_file(sdlc_dir, goal, "plans")
        if plan:
            pointer = plan.as_posix()
        else:
            work = _load("work")
            rec = work._record(sdlc_dir, goal)
            _, plan = work.plan_copies(sdlc_dir, goal, rec, work._run)
            if not plan:
                return ""
            rel = pathlib.PurePosixPath(*plan.relative_to(rec["worktree"]).parts).as_posix()
            pointer = "git -C %s show %s:%s" % (pathlib.Path(sdlc_dir).resolve().parent,
                                               rec["branch"], rel)
        return ("## The plan under review, by content\nPlan file: %s\nPlan sha256: %s\n%s"
                % (pointer, plan_sha256(plan), _PLAN_IDENTITY_NOTE))
    except Exception:                                 # noqa: BLE001 - the brief is fail-open
        return ""


def _outline(text):
    """A document's markdown heading lines only — its contract, without the prose under it.

    Fenced blocks are skipped, and that is not a nicety: measured on this repo, plan #100
    carries 32 code fences holding 29 lines that start with `#` — Python comments, not headings. A
    naive startswith would hand the reviewer those comments as if they were plan structure."""
    out, fenced = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced and line.startswith("#"):
            out.append(line)
    return "\n".join(out)


#: A plan heading names a correction/revision/clarification/supersession loosely, not by one fixed
#: wording -- this repo's own shipped plans already say it three different ways ("**[corrected]**",
#: "**[REVISED -- plan-review B1]**", "Correction to the spec -- dedup"), so matching a family of
#: words rather than one string is deliberate: a false positive over-includes a heading that merely
#: mentions "correct" (harmless -- the reviewer already gets that heading via the outline below,
#: this just adds its body too), while a false negative silently reproduces #2237.
_CORRECTION_HEADING_RE = re.compile(
    r"^#{1,6}\s.*\b(correct(ed|ion)?s?|revis(ed|ion)|clarif\w*|supersed\w*)\b", re.IGNORECASE)


def _plan_corrections(text):
    """Every heading-delimited section of a plan whose heading names a correction, revision,
    clarification or supersession -- FULL TEXT, unlike `_outline` just below, which strips every
    plan section down to its heading. #2237: a plan can correct something the research dossier
    embedded verbatim above got wrong, and a heading alone ("### 1.1 [corrected] Config location")
    tells the reviewer THAT something changed but never says what -- the stale research claim is
    the only version of the fact left in the brief. This is the part that carries the fact itself.

    A section runs until the next heading at the SAME OR SHALLOWER level (fewer or equal `#`s); a
    nested subheading stays part of the section it sits under, never a sibling boundary. Fenced
    code is skipped exactly like `_outline` does, for the identical reason: a code comment reading
    "# Correction: ..." is not a plan-level section, and must not be captured as one."""
    out, fenced, capturing, level = [], False, False, None
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            if capturing:
                out.append(line)
            continue
        if not fenced and line.startswith("#"):
            this_level = len(line) - len(line.lstrip("#"))
            if capturing and this_level <= level:
                capturing = False
            if _CORRECTION_HEADING_RE.match(line):
                capturing, level = True, this_level
        if capturing:
            out.append(line)
    return "\n".join(out).strip()


def _phase_doc(sdlc_dir, goal, subdir):
    """A phase artifact filed for this goal under `.sdlc/<subdir>/`, or "" if none.

    Goals are `NNNN-slug.md` (or a bare issue number), and BOTH `research/` and `plans/` file under
    either the full stem or the bare slug — so one resolver serves both rather than two that can
    drift apart.

    `research/` is the richest grounding available to an author-blind reviewer and the one thing that
    closes the gap the module docstring names: a fresh reviewer "cannot see BLAST RADIUS". Research
    already measured it — every affected site with `file:line`, the debt sitting in the radius, and
    the exact sweep commands, stored verbatim so they can be RE-RUN. Handing those over turns "trace
    the callers yourself" from an instruction into a checkable starting point, and lets the reviewer
    catch what landed *after* research by re-running the query rather than trusting the list.

    `plans/` is what the change AGREED to do, which is what makes unplanned scope and an unlanded
    planned step visible as findings rather than invisible.

    Both are project artifacts, not the maker's reasoning — they record what the code IS and what was
    AGREED, never why the author chose what they chose, so neither reintroduces the bias this module
    exists to remove."""
    path = phase_doc_file(sdlc_dir, goal, subdir)
    return _read(path) if path else ""


def _artifact_pointer(phase, artifact, artifact_text=None):
    """Where the reviewer finds the thing under review — a path, a PR number, or the phase default.
    NOT the artifact's content and NEVER the maker's reasoning about it: the reviewer opens it fresh.

    ONE deliberate exception, `goal-review` (#1826): `artifact_text` is that phase's own
    pre-fetched issue title+body (from `_artifact_issue`, "" when the fetch failed or was never
    attempted) — computed once by `brief()` and threaded through here so it is never fetched twice.
    Unlike a diff or a plan file, the goal-review artifact IS an issue, so (mirroring `_parent()`,
    not `code-review`'s diff-pointer or `pr-review`'s own PR-pointer) it is INLINED rather than left
    as a command the reviewer must think to run."""
    if artifact:
        if phase == "pr-review":
            pr = str(artifact).removeprefix("PR#")
            return "The PR under review: #%s. Read its real diff (`gh pr diff %s`)." % (pr, pr)
        if phase == "goal-review":
            # #1853: this safety sentence used to live ONLY in the no-artifact-at-all dict
            # fallback below -- a branch sigma-goal-review/SKILL.md never actually reaches, since
            # it always passes --artifact equal to --goal. Shared here so both real call shapes
            # (fetch succeeded, fetch failed) state it, not just the unreachable-in-practice one.
            no_code_yet = ("No branch or code exists yet - verify the codebase mapping, blast "
                           "radius and slice count against the real repo before confirming any "
                           "of it.\n\n")
            if artifact_text:
                return no_code_yet + "The story/epic issue under review (#%s):\n\n%s" % (artifact, artifact_text)
            return no_code_yet + ("The story/epic issue under review: #%s - its title/body could not be fetched "
                    "here; read it yourself (`gh issue view %s`) before judging fitness." % (artifact, artifact))
        return "The artifact under review: `%s`. Read it, then verify it against the code." % artifact
    return {
        "plan-review": "The active plan under `.sdlc/plans/`. Read it, then verify every claim against the code.",
        "code-review": "The branch's diff vs its base. Review the change, then read around it.",
        "pr-review": "The opened PR's diff. Read it fresh - this is the review AFTER the PR, not the pre-PR self-review.",
        "retro": "The shipped change and the goal it claimed to serve.",
        "goal-review": ("The story/epic issue this design maps, and its write-up at "
                        "`.sdlc/design/<n>.md` (goal-design's own output). No branch or code exists "
                        "yet - verify the codebase mapping, blast radius and slice count against "
                        "the real repo before confirming any of it."),
    }[phase]


def brief(sdlc_dir, goal, phase, artifact="", repo_root=".", source=None):
    """The independent reviewer's context pack: who they are, what the project is for, what to review,
    and the one rule that separates a real review from a rubber-stamp. Returns a string ready to hand a
    fresh subagent. Raises ValueError only on an unknown phase — everything else fails open."""
    if phase not in PHASES:
        raise ValueError("unknown --for %r; one of: %s" % (phase, ", ".join(sorted(PHASES))))

    goal_text = _read(goal) if goal and pathlib.Path(goal).exists() else (goal or "").strip()
    # In github mode `$goal` is a bare issue NUMBER, not the intent - a reviewer handed only "42" has
    # lost the acceptance criteria it is meant to judge against. FETCH the issue so the intent is in
    # the pack; only if that fails does it fall back to pointing at the issue.
    goal_body, goal_is_pointer, number = goal_text, False, None
    # #1826: goal-review's own artifact fetch is threaded through this same digit-branch fetch
    # when `artifact` names the IDENTICAL issue number as `goal` (exactly what `sigma-goal-review`'s
    # SKILL.md always passes -- there is no separate Epic issue yet at this stage, so the artifact
    # under review and the goal the change serves are the same ticket). Set here, reused below, so
    # the brief never issues two `gh issue view`/`fetch_title_body` calls for one issue.
    artifact_text = None
    artifact_fetched_via_goal = False
    if goal_text.isdigit():
        number = goal_text
        issue = _fetch_issue(sdlc_dir, number, source)
        goal_body = issue.get("body", "")
        fetched = _issue_text(issue)
        if phase == "goal-review" and artifact == number:
            artifact_text = fetched                 # "" (fetch failed) counts as attempted -- no retry
            artifact_fetched_via_goal = True
        if fetched:
            goal_text = "GitHub issue #%s\n\n%s" % (number, fetched)
        else:
            goal_is_pointer = True
            goal_text = ("GitHub issue #%s - read it (`gh issue view %s`) for the full intent and "
                         "acceptance criteria, and judge the change against those." % (number, number))
    project, project_docs = _project_context(sdlc_dir, repo_root)

    # #1826: goal-review's own artifact fetch, done ONCE here (never inside _artifact_pointer
    # itself) so the same fetch also feeds _missing's gap check below without a second `gh` call.
    # "" (never fetched, or the fetch failed) degrades _artifact_pointer to a pointer-only line —
    # mirrors goal_is_pointer's own shape one section up, for the identical reason. Skipped
    # entirely when the digit-branch above already fetched this exact number for `goal_text`.
    if not artifact_fetched_via_goal:
        artifact_text = (_artifact_issue(sdlc_dir, artifact, source)
                         if phase == "goal-review" and artifact else None)
    artifact_is_pointer = phase == "goal-review" and bool(artifact) and not artifact_text

    parts = [
        "# Independent review brief - %s" % phase,
        # The separation rule, stated to the reviewer itself. This is the whole point: author-blind by
        # construction (no maker field exists), and told to earn the review by tracing impact from code.
        "You are an INDEPENDENT reviewer if this brief reached you in a fresh context, which the "
        "dispatching skill resolved before spawning you (reviewer.py). Judge only what is here plus "
        "the code: you have not been given the author's reasoning, and there is no field in this "
        "brief through which it could arrive. You have full READ access to the whole "
        "repository: trace every caller and assess the blast radius from the code itself, not just the "
        "changed files. The diff is the change; the codebase is the impact surface. Try to break it; a "
        "review that only agreed did not review. State findings first, then end with exactly one "
        "standalone line: `VERDICT: approve` when there are no blocking findings, or `VERDICT: block` "
        "when there are blocking findings or a required input is absent. Do not use any other verdict "
        "spelling.",
        "## What you are reviewing\n%s" % _artifact_pointer(phase, artifact, artifact_text),
    ]
    if phase == "plan-review":
        # #258: the exact bytes under review, so the verdict can be recorded against them.
        identity = _plan_identity(sdlc_dir, goal)
        if identity:
            parts.append(identity)
    if project:
        parts.append("## What the project is for (judge the change against this)\n%s" % project)
    if goal_text:
        parts.append("## The goal this change serves\n%s" % goal_text)

    if phase in ("pr-review", "retro"):
        try:
            reader = _load("acceptance")
            try:
                acceptance = reader.read(sdlc_dir, goal)["text"]
            except FileNotFoundError:
                _, recorded = _branch_doc(sdlc_dir, goal, "acceptance")
                acceptance = reader.parse(recorded, reader.path(sdlc_dir, goal).stem)["text"]
        except (OSError, ValueError):
            acceptance = "MISSING OR INVALID: original acceptance was not recorded; do not claim achieved."
        parts.append("## Recorded acceptance (P1 intent)\nGrade each criterion against evidence "
                     "as achieved, partial, or diverged. Do not substitute the current issue text.\n\n"
                     + acceptance)

    parent = _parent(sdlc_dir, goal_body, source)
    if parent:
        parts.append("## The parent goal this one was split out of\nThis change is ONE piece of a "
                     "larger goal. Judge it as a piece: does it fit the whole, and does it leave "
                     "the seams the sibling pieces will need?\n%s" % parent)

    dossier = _phase_doc(sdlc_dir, goal, "research")
    # #2647: the dossier is a tracked file on `sdlc/<goal>` too, and hits this module's second face
    # of the same bug — `_missing` below then tells the reviewer blast radius "was never measured".
    if not dossier and phase in _DOSSIER_PHASES:
        _, dossier = _branch_doc(sdlc_dir, goal, "research")
    # Only worth a `gh` round-trip on the genuine gap path: a dossier FILE already answers the
    # question, and local-mode goals have no issue thread to check -- so this stays cheap and rare,
    # matching _missing()'s own "don't nag every run" rule. Deliberately NOT gated on goal_is_pointer:
    # that flag is also set when the issue fetched fine but just had a blank title/body, which is a
    # normal, valid issue state, not a sign the transport is broken -- and this call fails open on its
    # own on any real transport failure anyway, so gating on it only reintroduced the false negative
    # this function exists to close, for zero real safety benefit.
    research_note_seen = bool(
        not dossier and phase in _DOSSIER_PHASES
        and number and _research_note_posted(sdlc_dir, number, source))
    if dossier:
        parts.append("## Blast radius already measured (Research phase)\nThis is the project's own "
                     "survey, not the author's argument. Re-run its stored queries: a site that landed "
                     "AFTER research is exactly what a diff-only review misses.\n%s" % dossier)

    # The plan grounds a DIFF review, not plan-review — which already has the plan as its artifact and
    # would just be handed it twice. Only the OUTLINE is inlined: the headings are the contract (what
    # was agreed to land, at file granularity), while the prose under them describes work the diff
    # itself already shows. Measured on this repo, the body was 89% of the brief. The reviewer has full
    # repo read access, so it opens the plan itself when a heading is not enough.
    plan_file = phase_doc_file(sdlc_dir, goal, "plans") if phase in ("code-review", "pr-review") else None
    plan_text = _read(plan_file) if plan_file else ""
    if not plan_text and phase in ("code-review", "pr-review"):
        # #2647: not on disk here -> read it off the goal's own branch. `plan_file` carries a
        # runnable `git show ...` rather than a path in that case; it stays the same single value
        # the "Full plan:" line and `_missing`'s gap test both read, so neither can drift.
        # The prior `plan_file` is KEPT when the branch has nothing (code-review finding 3): a plan
        # that is on disk but unreadable or empty already had a path to hand the reviewer, and
        # overwriting it with None turned "I could not read this" into "this goal has no plan" --
        # the exact falsehood this issue exists to remove, reintroduced at a new seam.
        branch_file, plan_text = _branch_doc(sdlc_dir, goal, "plans")
        plan_file = branch_file or plan_file
    if plan_file:
        # #2237: surfaced BEFORE the outline block below, and right after the research dossier
        # above, so a correction reaches the reviewer NEXT TO the stale claim it overrules, not
        # buried in a heading the outline would otherwise reduce it to.
        corrections = _plan_corrections(plan_text)
        if corrections:
            parts.append(
                "## The plan corrects or clarifies something above\nThe plan below has a section "
                "the author marked as a correction, revision, clarification, or supersession. If it "
                "disagrees with the Research dossier above (or anything else in this brief), THIS is "
                "the current truth - a dossier is a point-in-time survey and is not re-run "
                "automatically when the plan moves past it.\n%s" % corrections)
        parts.append("## The plan this change was supposed to implement\nJudge the diff against it. "
                     "Work in the diff but NOT in the plan is unplanned scope; work in the plan but "
                     "NOT in the diff is incomplete. Both are findings.\n%s\n\nFull plan: `%s` - read "
                     "it when a heading is not enough."
                     % (_outline(plan_text), plan_file))

    gaps = _missing(project_docs, goal_text, dossier, phase, artifact, plan_file, goal_is_pointer,
                     research_note_seen,
                     artifact_is_pointer)
    if gaps:
        # Fail-open keeps the review RUNNING on a partial brief; this keeps it HONEST about it. An
        # under-briefed reviewer returning a confident "no issues" is worse than a biased one - the
        # verdict is indistinguishable from a real pass. Same rule pipeline.py states for stages:
        # no instrument reads ABSENT, never PASS.
        parts.append("## Inputs NOT available to you\n%s\n\nDo not imply coverage you did not have. "
                     "Read them yourself from the repo where they exist; where they do not, say so in "
                     "your verdict. A review missing a required input reads ABSENT, never PASS."
                     % "\n".join("- %s" % g for g in gaps))
    return "\n\n".join(parts)


def _missing(project_docs, goal_text, dossier, phase, artifact, plan=None, goal_is_pointer=False,
             research_note_seen=False,
             artifact_is_pointer=False):
    """What the reviewer was NOT given, stated as fact. Only genuine gaps - a drop-in repo with no
    north-star must not be nagged every run, or the notice stops being read.

    `project_docs` is the SET of project-doc rel paths `_project_context` actually found - not the
    assembled string, and not a bool. #1778: the string is also made truthy by the conventions and
    contracts blocks, so testing it here meant a brief that had lost both project docs still reported
    no gap. That is this repo's ABSENT-is-not-PASS rule broken by the very line meant to enforce it.
    #2147: a bool repeated the defect between the two docs, since either one set it - so a present
    `project.md` silently vouched for an absent north-star. The north-star is now asked about by
    name. Deliberately NOT symmetric: `project.md`'s own absence gains no new gap line here, because
    that would nag every repo that has a north-star and no project.md with a notice it never saw."""
    gaps = []
    if not goal_text:
        gaps.append("The goal / acceptance criteria - you cannot judge fitness without it.")
    if _NORTH_STAR_REL not in project_docs:
        gaps.append("No north-star: strategy, non-goals and architecture rules are "
                    "unavailable, so alignment cannot be judged - only correctness.")
    if not dossier and phase in _DOSSIER_PHASES:
        if research_note_seen:
            # A local file is missing, but the goal's own issue thread has a Research phase note --
            # asserting "never measured" here would be a plain falsehood (confirmed live on #1762).
            # The note's TEXT is still never inlined: an issue comment is far less curated than the
            # vetted research/ file above and can carry the maker's own rationale, which this
            # module's whole design keeps out of the reviewer's pack (see _phase_doc's docstring).
            gaps.append("No local Research dossier file, but a Research phase note is already on "
                        "the issue's comment thread. This brief deliberately excludes it (never the "
                        "maker's own reasoning) - read the thread yourself and verify blast radius "
                        "from the code before concluding the change is contained.")
        else:
            gaps.append("No Research dossier: blast radius was never measured for this goal. Trace "
                        "callers from the code yourself before concluding the change is contained.")
    if goal_is_pointer:
        gaps.append("The goal's body could not be fetched - you have its issue number, not its "
                    "acceptance criteria. Run the `gh issue view` above and read it before judging "
                    "fitness; if you cannot, say so rather than passing on correctness alone.")
    if not plan and phase in ("code-review", "pr-review"):
        gaps.append("No plan for this goal: you cannot tell unplanned scope from intended work, or "
                    "spot a planned step that never landed. Judge against the goal alone, and say so.")
    # #1826: goal-review's artifact is an ISSUE NUMBER, never a filesystem path (same reasoning
    # pr-review already established, pinned by test_pr_review_artifact_is_a_number_not_a_path) - a
    # bare `Path("1826").exists()` probe would spuriously report it "missing" on every real run.
    if artifact and phase not in ("pr-review", "goal-review") and not pathlib.Path(artifact).exists():
        gaps.append("The artifact `%s` does not exist - you have nothing to review. Stop and say so; "
                    "do not review from the goal text alone." % artifact)
    if artifact_is_pointer:
        gaps.append("The story/epic issue #%s could not be fetched - you have its number, not its "
                    "title/body. Run the `gh issue view` above and read it before confirming any of "
                    "the design against it; if you cannot, say so rather than confirming blind." % artifact)
    return gaps


def _atomic_bytes(path, data):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".review-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _manifest_output_path(sdlc_dir, output):
    """Resolve one requested manifest path and keep it inside the owned state directory."""
    directory = pathlib.Path(sdlc_dir).resolve() / "state" / "review-manifests"
    candidate = pathlib.Path(output).resolve(strict=False)
    try:
        candidate.relative_to(directory.resolve())
    except ValueError as exc:
        raise ValueError("review manifest output must stay under state/review-manifests") from exc
    if candidate == directory.resolve():
        raise ValueError("review manifest output must name a file")
    return candidate


def _pr_generation_binding(sdlc_dir, goal, artifact):
    """Return immutable PR/head facts for the documented ``--artifact PR#N`` gesture.

    These values must come from the live goal record and its checkout, never an option an author
    can accidentally bind to an older PR head.  Non-PR artifacts intentionally stay unbound: this
    publisher also serves plan/code/retro briefs.
    """
    match = re.fullmatch(r"PR#([1-9][0-9]*)", str(artifact))
    if not match:
        return {}
    stem = pathlib.Path(str(goal)).stem
    record_path = pathlib.Path(sdlc_dir) / "state" / "work" / (stem + ".json")
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("PR review generation needs a readable active work record") from exc
    pr = int(match.group(1))
    if not isinstance(record, dict) or str(record.get("pr")) != str(pr):
        raise ValueError("PR review artifact does not match the active work record")
    worktree = record.get("worktree")
    if not isinstance(worktree, str) or not pathlib.Path(worktree).is_dir():
        raise ValueError("PR review generation needs the active worktree")
    remote, base = record.get("remote"), record.get("base")
    if not (isinstance(remote, str) and remote and isinstance(base, str) and base):
        raise ValueError("PR review generation needs the goal's remote and base")
    base_ref = remote + "/" + base  # the same target `work.py pr` opens the PR against
    # Head A, the diff, head B: publish nothing unless one revision was stable across the capture.
    head_a = worktree_head(worktree)
    diff_sha256 = pr_diff_sha256(worktree, base_ref, head_a)
    head_b = worktree_head(worktree)
    if head_a != head_b:
        raise ValueError("the PR head moved while its diff was captured; nothing was published, retry")
    return {"pr": pr, "head_sha": head_a, "head_sha_b": head_b, "base_ref": base_ref,
            "diff_sha256": diff_sha256}


def worktree_head(worktree):
    """The worktree's current commit SHA, or a loud refusal."""
    try:
        proc = subprocess.run(["git", "-C", str(worktree), "rev-parse", "HEAD"], capture_output=True,
                              text=True, timeout=10)
    except OSError as exc:
        raise ValueError("PR review generation cannot read the worktree head") from exc
    head = proc.stdout.strip()
    if proc.returncode or not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", head):
        raise ValueError("PR review generation cannot read a Git head SHA")
    return head


def pr_diff_sha256(worktree, base_ref, head):
    """SHA-256 of the PR's own change: the diff from its merge-base with `base_ref` to `head`.

    Formatting flags are pinned so the digest depends on the change, not on the reader's git config.
    Publication and `work.py review-evidence` both call THIS function, so they agree by construction.
    """
    def git(*args):
        return subprocess.run(["git", "-C", str(worktree), *args], capture_output=True, timeout=120)
    try:
        merge_base = git("merge-base", base_ref, head)
        if merge_base.returncode:
            raise ValueError("PR review generation cannot find the merge-base with %s" % base_ref)
        diff = git("diff", "--binary", "--full-index", "--no-color", "--no-ext-diff",
                   merge_base.stdout.decode().strip(), head)
    except OSError as exc:
        raise ValueError("PR review generation cannot read the PR diff") from exc
    if diff.returncode:
        raise ValueError("PR review generation cannot read the PR diff")
    return hashlib.sha256(diff.stdout).hexdigest()


def publish_generation(sdlc_dir, goal, text, artifact, output, phase=""):
    """Publish immutable review input then atomically replace the sole manifest pointer.

    `phase` is optional and only sharpens one failure mode: for `pr-review`, a malformed
    `--artifact` (a bare number, a path, anything not `PR#<N>`) must not silently publish an
    UNBOUND manifest -- no `pr`/`head_sha`/`base_ref`/`diff_sha256` -- because that manifest still
    passes `work.py record-subagent-review`/`review-evidence` and only fails much later, opaquely,
    at `work.py post-review`, after a whole review cycle already ran on the wrong assumption."""
    root = pathlib.Path(sdlc_dir) / "state"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    binding = _pr_generation_binding(sdlc_dir, goal, artifact)
    if phase == "pr-review" and artifact and not binding:
        raise ValueError(
            'pr-review needs --artifact in the exact form "PR#<N>" (e.g. "PR#2819"), got %r -- a '
            "malformed --artifact would otherwise silently drop PR binding (head_sha/base_ref/"
            "diff_sha256) from the manifest instead of failing here" % str(artifact))
    # A generation is ONE publication, not a pure function of the brief and the revision.  When it
    # was, a re-review of the same head landed on the same generation, whose create-once result
    # already held the first verdict -- so a documented same-revision `unblock` could never succeed.
    # The plan's manifest already names a creation time; the nonce makes two publications in the same
    # clock tick distinct too.
    created_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="microseconds")
    nonce = os.urandom(16).hex()
    generation_material = (str(goal) + "\0" + str(artifact) + "\0" + digest + "\0"
                           + json.dumps(binding, sort_keys=True, separators=(",", ":"))
                           + "\0" + created_at + "\0" + nonce)
    generation = hashlib.sha256(generation_material.encode()).hexdigest()[:32]
    generation_dir = root / "review-generations" / generation
    brief_path = generation_dir / "brief.md"
    if brief_path.exists() and brief_path.read_bytes() != text.encode("utf-8"):
        raise ValueError("review generation collision")
    _atomic_bytes(brief_path, text.encode("utf-8"))
    manifest = {"generation_id": generation, "goal": str(goal), "artifact": str(artifact),
                "brief": str(brief_path.relative_to(root)), "brief_sha256": digest,
                "created_at": created_at}
    manifest.update(binding)
    _atomic_bytes(_manifest_output_path(sdlc_dir, output),
                  (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))
    return manifest


def main(argv):
    if any(arg in ("--help", "-h") for arg in argv[1:]):
        print("usage: review_context.py brief <sdlc_dir> <goal> --for %s [--artifact <path|\"PR#<N>\" for pr-review>] [--repo-root <dir>] [--output <manifest>]" % "|".join(sorted(PHASES)))
        return 0
    if len(argv) >= 4 and argv[1] == "brief":
        sdlc_dir, goal = argv[2], argv[3]
        phase, artifact, repo_root, output = "", "", ".", ""
        rest = argv[4:]
        i = 0
        while i < len(rest):
            if rest[i] == "--for" and i + 1 < len(rest):
                phase = rest[i + 1]; i += 2
            elif rest[i] == "--artifact" and i + 1 < len(rest):
                artifact = rest[i + 1]; i += 2
            elif rest[i] == "--repo-root" and i + 1 < len(rest):
                repo_root = rest[i + 1]; i += 2
            elif rest[i] == "--output" and i + 1 < len(rest):
                output = rest[i + 1]; i += 2
            else:
                i += 1
        if not phase:
            print("usage: review_context.py brief <sdlc_dir> <goal> --for %s "
                  "[--artifact <path|\"PR#<N>\" for pr-review>] "
                  "[--repo-root <dir>]" % "|".join(sorted(PHASES)), file=sys.stderr)
            return 2
        try:
            result = brief(sdlc_dir, goal, phase, artifact, repo_root)
            if output:
                publish_generation(sdlc_dir, goal, result, artifact, output, phase)
            print(result)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return 0
    print("usage: review_context.py brief <sdlc_dir> <goal> --for %s "
          "[--artifact <path|\"PR#<N>\" for pr-review>] "
          "[--repo-root <dir>]" % "|".join(sorted(PHASES)), file=sys.stderr)
    return 2


if __name__ == "__main__":
    # The script-time floor: this run's wall time, recorded to the session resolved at exit.
    sys.exit(_load("timing_store").timed_main(main, sys.argv, "review_context"))
