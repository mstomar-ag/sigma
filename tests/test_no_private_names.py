"""Strict guard for private names in the core's surface (S1-G1; tests/ since #2580; strict since
#2584).

Scans the core's surface for the private-name patterns in `_PATTERNS` below (PRD Appendix B). There
is NO baseline file and, since #2706, no exemption list: the scan must find nothing. No command
writes an exemption, so a new finding cannot be accepted by regeneration -- only by rewording it
(#2584 owner rulings 1 and 3). Every failure message ends with
`Recheck: python3 tests/test_no_private_names.py`.

WHAT IS SCANNED: the public surface, `tests/public_surface.py` -- in this repository, the files
`tools/public-manifest.txt` selects (tests/ included since #2580, owner decision D3); in a public
tree, every tracked file (#2584). `_scanned_paths()` is the one generator of scanned files, and
the cross-boundary and network guards read the same surface.

PATTERNS: `_PATTERNS` below (PRD Appendix B). They are not restated here, because a restatement
is a second copy that drifts -- and, since tests/ is scanned, one this guard would flag. They match
case-insensitively, except where a pattern says otherwise with `(?-i:...)` (the env-var pattern,
as Appendix B specifies, since #2584). NOT HERE: Appendix B's server hostnames (those in the
private deploy docs and configs) -- a hostname list is an internal-address scan, which S2-G1
(#2586) owns; this guard does not claim it.

RETIRED NAMES (#2729, PRD Q1-G0, D11): the brand and skill prefix this core shipped under before
`sigma`/`sigma-*` are private names now -- the old brand token, its `_`-suffixed env-var prefix,
the 42 old skill names, the old skill-namespace glob (the old prefix followed by `*`: always the
namespace, never a branch -- a branch glob is `sdlc/*`), the old `<...-dir>` usage placeholder, the
old repository name and (D12, post-PR review) the old personal-account owner slug in front of the new
repository name, which the mechanical rebrand minted and which names a repository that does not
exist -- each spelled in `_PATTERNS` from adjacent string fragments, which the parser folds into one literal, so
no shipped line (this file's included) carries one whole. The ONLY exemption: the ops-branch names
`sdlc-ledger` and `sdlc-knowledge`, and only on a line that names the branch (`branch`, `origin/`,
`refs/heads/`; `_sdlc_ledger_disposition`) -- after `name:`, after a bare `/`, under `skills/` or
in a heading the same token is the retired skill and is flagged.

EXEMPTIONS, two kinds and no others:
  1. EXACT PHRASES (`_EXEMPTIONS`): a phrase exempts itself and nothing else on its line (#2584;
     until then it exempted the whole line, hiding anything written beside it). The structured
     scan exempts a match only when it lies wholly inside a phrase span; the raw-word comparator
     reads the line with each phrase masked (`_mask_exemptions`).
  2. PATTERN-LIST SPANS (`_PATTERN_LIST_SPANS`): a guard must spell a private name to do its job,
     so per guard file the NAMES of the top-level assignments holding its pattern lists are listed,
     and only those assignments' own code lines are exempt. `_pattern_list_lines` finds them with
     `ast`, so a span follows edits and no line number is pinned. A listed name that is missing,
     assigned twice, or sharing a line with any other top-level statement raises `ExemptionDrift`
     -- loud, never a silent widening. Everything else in a guard file is scanned like any other
     test file, so a guard's plants are DERIVED from its listed constants, never spelled.
(A third kind, the one-release alias lines #2584 owner ruling 2 kept as named survivors, went with
the aliases themselves in #2706, which superseded that ruling.)
The raw-word comparator (`_RAW_ROOTS`, `RAW_RESIDUAL_SHIPPED`, `RAW_RESIDUAL_TESTS`) reads the same
files and spans, with each phrase masked (the structured scan uses span containment instead); its
pins are reviewed in-code counts, not a baseline.

DOCSTRING NOTES: This guard uses text scanning, not AST, for the scan itself. This is correct
because:
  1. We need to catch private names in docstrings, config files, templates, and docs
  2. Exemptions by exact phrase only (not by file) prevent false negatives from bypass attempts
  3. The exemptions are narrow and easy to audit (three exact strings per the PRD)
(`ast` is used for one thing only: locating a guard file's pattern-list spans.)

THE SCRIPT GESTURE: `python3 tests/test_no_private_names.py` takes NO arguments (any argument,
`--write-baseline` included, exits 2 and writes nothing). It runs EVERY zero-argument test in this
file, printing `ran: <name>` before each -- a strict test that gained a fixture parameter would
silently stop running, and the missing line shows it -- and ends with one line:
`test_no_private_names: OK|FAIL (<k> finding(s) over <n> scanned file(s))`, exit 0 or 1.
"""
import ast
import collections
import contextlib
import importlib.util
import inspect
import io
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import tokenize

import public_surface

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: The module's own root, captured once: `_scan_files()` memoises only when `ROOT` is still this,
#: so a planted-tree test (which swaps `ROOT` through `_planted_root`) always scans fresh.
_REAL_ROOT = ROOT

# Exact phrase exemptions only, per PRD Appendix B and AGENTS.md
_EXEMPTIONS = frozenset({
    "critical insight",
    "critical insights",
    "CRITICAL_INSIGHT",
})

# Private-name patterns from PRD Appendix B
_PATTERNS = [
    # Package names and imports
    (r"\binsight/", "insight/ path reference"),
    (r"-m insight\b", "-m insight"),
    (r"\bimport insight\b", "import insight"),
    (r"\bfrom insight\b", "from insight"),
    (r"\bimport pi\b", "import pi"),
    (r"\bfrom pi\b", "from pi"),
    (r"\bimport collector\b", "import collector"),
    (r"\bfrom collector\b", "from collector"),
    # The private git-hook directory (#2584 review rounds 1-2): a private root with no raw word,
    # as a path or bare (a `core.hooksPath` value)
    (r"(?<![\w.-])\.githooks\b", ".githooks reference"),
    # Named private components
    (r"\binsight (emit|ingest|control-plane|dash|backup|up|doctor|web)\b", "insight component"),
    # Environment variables (case-sensitive)
    (r"(?-i:INSIGHT_[A-Z_]+)", "INSIGHT_ env var"),
    # Config keys and filenames
    (r"insight-emit-token", "insight-emit-token"),
    (r"insight\.duckdb", "insight.duckdb"),
    (r"insight-control-plane", "insight-control-plane"),
    (r"insight-accounts", "insight-accounts"),
    (r"insight-web", "insight-web"),
    (r'"insight"\s*:', '"insight" config key'),
    # Function names
    (r"_ensure_emit_scheduler", "_ensure_emit_scheduler"),
    (r"_ensure_ingest_watcher", "_ensure_ingest_watcher"),
    (r"_ensure_config_puller", "_ensure_config_puller"),
    (r"emit_scheduler", "emit_scheduler"),
    (r"ingest_watcher", "ingest_watcher"),
    (r"config_puller", "config_puller"),
    (r"gated-check", "gated-check"),
    (r"control_plane", "control_plane"),
    (r"table-counts", "table-counts"),
    # API endpoints
    (r"/v1/ingest", "/v1/ingest"),
    (r"/v2/records", "/v2/records"),
    (r"/v1/table-counts", "/v1/table-counts"),
    # Enrolment
    (r"loop" "smith-collector", "loop" "smith-collector"),
    (r"collector enroll", "collector enroll"),
    # Product names
    (r"Loop" "Smith Insight", "Loop" "Smith Insight"),
    (r"Agrim Pi", "Agrim Pi"),
    (r"\btelemetry\b", "telemetry"),
    # Retired names (#2729, D11): the brand and skill prefix before `sigma`/`sigma-*`, from
    # fragments (the module docstring says why). The ledger skill's stem is carried by the ops-token
    # entry, whose branch-context matches `_scan_file` exempts through `_sdlc_ledger_disposition`.
    (r"\b" "loop" "smith" r"\b", "retired brand token"),
    (r"(?-i:" "LOOP" "SMITH" r"_[A-Z_]*)", "retired env-var prefix"),
    (r"sdlc-(align|audit|brainstorm|chain|context|contract-check|dag|debug|decide|define|doctor|dossier"
     r"|goal-design|goal-review|goal|implement|init|kg|log|loop|migration-check|model|plan-review"
     r"|plan|prd-intake|promote|radar|rebase|release-check|research|retro|review|scope|security-review|setup"
     r"|slack|status|time|triage|unpark|velocity|verify|vision|wizard)\b", "retired skill name"),
    (r"sdlc-(?:ledger|knowledge)\b", "retired ops token outside branch context"),
    (r"sdlc-" r"\*", "retired skill namespace glob"),
    (r"sdlc-" "dir", "retired dir placeholder"),
    (r"sdlc-" "kit", "retired repository name"),
    # #2729 post-PR review, D12: the mechanical rebrand put the OLD personal-account owner in front
    # of the NEW repository name -- a repository that does not exist. The public one is
    # `Agrim-Intelligence/sigmaloop` (the one org-slug string D12 allows); this exact old-owner slug is
    # a private name, whatever follows it (`.../sigma-x` is no more real).
    (r"swapnil-" "agrim/" "sigma", "retired owner slug on the public repo"),
]

#: One sample line per `_PATTERNS` entry, by its description (#2584 review round 2, N3): the
#: pattern must find its own sample through the full scan pipeline, so a deleted or broken pattern
#: goes red even while the live surface happens to hold no leak for it. A listed span, because the
#: samples ARE private names.
_PATTERN_SAMPLES = {
    "insight/ path reference": "see insight/x",
    "-m insight": "python3 -m insight x",
    "import insight": "import insight",
    "from insight": "from insight import x",
    "import pi": "import pi",
    "from pi": "from pi import x",
    "import collector": "import collector",
    "from collector": "from collector import x",
    ".githooks reference": "git config core.hooksPath .githooks",
    "insight component": "run insight emit now",
    "INSIGHT_ env var": "INSIGHT_X=1",
    "insight-emit-token": "cat insight-emit-token",
    "insight.duckdb": "open insight.duckdb",
    "insight-control-plane": "the insight-control-plane service",
    "insight-accounts": "the insight-accounts store",
    "insight-web": "the insight-web tier",
    '"insight" config key': '{"insight": {}}',
    "_ensure_emit_scheduler": "_ensure_emit_scheduler()",
    "_ensure_ingest_watcher": "_ensure_ingest_watcher()",
    "_ensure_config_puller": "_ensure_config_puller()",
    "emit_scheduler": "the emit_scheduler",
    "ingest_watcher": "the ingest_watcher",
    "config_puller": "the config_puller",
    "gated-check": "a gated-check",
    "control_plane": "control_plane = 1",
    "table-counts": "the table-counts call",
    "/v1/ingest": "POST /v1/ingest",
    "/v2/records": "POST /v2/records",
    "/v1/table-counts": "GET /v1/table-counts",
    "loop" "smith-collector": "loop" "smith-collector enroll",
    "collector enroll": "collector enroll --token",
    "Loop" "Smith Insight": "Loop" "Smith Insight dashboard",
    "Agrim Pi": "Agrim Pi hardware",
    "telemetry": "the telemetry block",
    "retired brand token": "Loop" "Smith leaked here",
    "retired env-var prefix": "LOOP" "SMITH_TOKEN=x",
    "retired skill name": "run /sdlc-" "loop",
    "retired ops token outside branch context": "name: sdlc-" "ledger",
    "retired skill namespace glob": "read skills/sdlc-" "*/SKILL.md",
    "retired dir placeholder": "check <sdlc-" "dir>",
    "retired repository name": "clone sdlc-" "kit",
    "retired owner slug on the public repo": "gh run list --repo swapnil-" "agrim/" "sigma",
}

#: Where a guard must spell a private name to do its job: per guard file, the NAMES of the
#: module-level assignments holding its pattern lists. Only those assignments' own lines are
#: exempt, and of those only the CODE: a comment inside the literal, on its own line or trailing
#: code, stays scanned. What the exemption cannot see is the literals themselves -- a pattern's
#: description, a pin's key -- which is why the pins must be pure literals of the right shape
#: (`test_the_exemptions_are_exactly_the_pattern_lists`) and why a change to any listed constant
#: is a guard change that review of its diff must read. Every other line of these files is
#: scanned like any other test file.
#:
#: What counts as a pattern list (#2580 plan K19): a constant the guard needs to MATCH (patterns,
#: banned roots, samples), to NOT match (exemption phrases), or to PIN (the raw-word residue) --
#: whose lines ARE tokens, so unexempted they would have to pin themselves, with no fixed point.
#: A new spelling needs a listed constant AND an update to the
#: pin in `test_the_exemptions_are_exactly_the_pattern_lists`, both visible in the diff.
_PATTERN_LIST_SPANS = {
    "tests/test_no_private_names.py": ("_EXEMPTIONS", "_PATTERNS", "_PATTERN_SAMPLES",
                                       "_RAW_ROOTS", "RAW_RESIDUAL_SHIPPED", "RAW_RESIDUAL_TESTS",
                                       "_SDLC_LEDGER_SKILL_CONTEXT", "_SDLC_LEDGER_BRANCH_CONTEXT"),
    "tests/test_no_cross_boundary_paths.py": ("_PRIVATE_ROOTS",),
    "tests/test_import_boundary.py": ("_BANNED_PRIVATE",),
}

#: The ops-token rule (#2729, plan D-d/D-j2), per line and per token. A token in one of these
#: SKILL-context forms -- the text before it matches one -- is the retired skill and is flagged:
#: a `skills/` path segment, a `"skills" / "..."` pathlib join, a bare `/` gesture not glued to a
#: word (`origin/...` is a ref, not a gesture), a `name:` frontmatter key, a markdown heading.
_SDLC_LEDGER_SKILL_CONTEXT = (r"skills/$", r'"skills"\s*/\s*"$', r"(?:^|[^A-Za-z0-9_./-])/$",
                              r"name:\s*$", r"^\s*#+\s*$")
#: Any other token is the ops BRANCH -- exempt -- when the same line carries one of these (any
#: case); a line with neither is ambiguous and is flagged for a hand fix.
_SDLC_LEDGER_BRANCH_CONTEXT = ("branch", "origin/", "refs/heads/")

#: No LEFT boundary (the acceptance grep has none, and an f-string's `\n` escape glues its letter
#: to a token that follows it -- a `\b` rule shipped exactly that, #2729); the right edge is bounded
#: by `[-\w]`, so a SUFFIXED token (`...-ledger-evil`) is never the ops branch and gets no
#: disposition -- `(?![a-z0-9])` alone let the branch verdict exempt it (post-PR review cycle 3).
_OPS_TOKEN_RE = re.compile(r"sdlc-(?:ledger|knowledge)(?![-\w])")
_OPS_TOKEN_DESCRIPTION = "retired ops token outside branch context"


def _sdlc_ledger_disposition(line):
    """`[(start, end, verdict)]` for every ops-token occurrence in ONE line: `skill`, `branch` or
    `ambiguous`, by the rule the two constants above spell out. `tools/rebrand.py` carries its own
    copy (its tests keep the two in agreement); this one is deliberately self-contained, because
    the tool never ships and this guard does."""
    out = []
    branch = any(re.search(re.escape(word), line, re.I) for word in _SDLC_LEDGER_BRANCH_CONTEXT)
    for m in _OPS_TOKEN_RE.finditer(line):
        prefix = line[:m.start()]
        if m.group().endswith("ledger") and any(re.search(rx, prefix) for rx in _SDLC_LEDGER_SKILL_CONTEXT):
            verdict = "skill"
        elif branch:
            verdict = "branch"
        else:
            verdict = "ambiguous"
        out.append((m.start(), m.end(), verdict))
    return out


def _ops_token_is_branch(line, match):
    return any(s == match.start() and v == "branch" for s, _e, v in _sdlc_ledger_disposition(line))


class ExemptionDrift(AssertionError):
    """A listed pattern-list span no longer matches its guard file: a listed name is missing or
    assigned twice, or another statement shares a line with a span. Raised, never skipped -- a
    rename must not silently drop an exemption, and a shared line must not silently widen one."""


def _pattern_list_lines(rel, text):
    """The exempt line numbers of guard file `rel`: each listed TOP-LEVEL `Assign`/`AnnAssign`
    whose `Name` target is in `_PATTERN_LIST_SPANS[rel]`, `lineno..end_lineno` inclusive.

    Raises `ExemptionDrift` when a listed name is missing or assigned twice, or when any other
    top-level statement shares a line with a span (`X = [...]; import y` would otherwise hide the
    import). A syntax error propagates, which is loud too."""
    names = _PATTERN_LIST_SPANS[rel]
    lines = text.splitlines()
    tree = ast.parse(text, filename=rel)
    seen, spans, owners = {}, set(), []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            targets = []
        hit = [t.id for t in targets if isinstance(t, ast.Name) and t.id in names]
        for name in hit:
            if name in seen:
                raise ExemptionDrift("%s: listed pattern list %r is assigned twice (lines %d and %d)"
                                     % (rel, name, seen[name], node.lineno))
            seen[name] = node.lineno
        if hit:
            owners.append(node)
            spans |= set(range(node.lineno, node.end_lineno + 1))
    missing = [n for n in names if n not in seen]
    if missing:
        raise ExemptionDrift("%s: listed pattern list(s) %s not found as a top-level assignment -- "
                             "renamed or removed? Update _PATTERN_LIST_SPANS in the same edit."
                             % (rel, ", ".join(missing)))
    for node in tree.body:
        if any(node is o for o in owners):
            continue
        shared = spans & set(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        if shared:
            raise ExemptionDrift("%s: line %d holds a pattern-list span AND another statement; put "
                                 "the statement on its own line" % (rel, min(shared)))
    # A line that is ONLY a comment is prose, not a pattern: it stays scanned even inside a span
    # (#2580 review round 3). A comment TRAILING a span's code line is scanned too, by
    # `_scannable_lines` (round 4).
    return frozenset(n for n in spans if not lines[n - 1].lstrip().startswith("#"))


#: The phrases' MASKS for the raw-word comparator, longest first (so a plural is masked whole), each
#: with its case rule and its right edge (#2584 review round 2, N2). An all-lowercase phrase matches
#: any case and only where it ENDS: not followed by a word character, `/` or `-`, so
#: "<phrase>/web/app.py" or "<phrase>-web" is not masked into a private name. The other phrase
#: matches only as written, and only before a non-word character or `_TEMPLATE` -- the core's own
#: issue-template label -- so a private env var that merely starts with it is not masked either.
_UPPER_PHRASE_CONTINUATIONS = ("_TEMPLATE",)
_EXEMPTION_RES = tuple(
    re.compile(re.escape(x) + r"(?![\w/-])", re.I) if x.islower()
    else re.compile(re.escape(x) + r"(?=%s|(?!\w))"
                    % "|".join(map(re.escape, _UPPER_PHRASE_CONTINUATIONS)))
    for x in sorted(_EXEMPTIONS, key=len, reverse=True))


#: The phrase SPANS the structured scan exempts (#2584 review round 3, N1): each lowercase phrase in
#: any case, and the uppercase phrase with its optional `_TEMPLATE` continuation. A pattern match is
#: exempt only when it lies WHOLLY inside one span, so no character glued to a phrase -- `.`, `-`,
#: `/`, a space, a letter -- can carry a private name past the scan.
_PHRASE_SPAN_RE = re.compile("|".join(
    [r"(?i:%s)" % re.escape(x) for x in sorted((x for x in _EXEMPTIONS if x.islower()),
                                               key=len, reverse=True)]
    + [re.escape(x) + "(?:%s)?" % "|".join(map(re.escape, _UPPER_PHRASE_CONTINUATIONS))
       for x in sorted(x for x in _EXEMPTIONS if not x.islower())]))


def _phrase_spans(line):
    """`[(start, end)]` of every exemption phrase in `line`."""
    return [m.span() for m in _PHRASE_SPAN_RE.finditer(line)]


def _inside_a_phrase(match, spans):
    return any(start <= match.start() and match.end() <= end for start, end in spans)


def _mask_exemptions(line):
    """`line` with each exemption phrase's own span blanked to spaces of equal length -- the phrase
    exempts itself, never the rest of its line (#2584). Column positions are kept. The raw-word
    comparator reads lines through this; the structured scan uses span containment instead
    (`_phrase_spans`), which no glued character can defeat."""
    for rx in _EXEMPTION_RES:
        line = rx.sub(lambda m: " " * len(m.group()), line)
    return line


def _trailing_comments(text, lines):
    """{line number: comment text} for every `#` comment that starts on one of `lines`."""
    out = {}
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type == tokenize.COMMENT and tok.start[0] in lines:
            out[tok.start[0]] = tok.string
    return out


def _scannable_lines(path):
    """`(line_number, line)` for every line of `path` outside its pattern-list spans (if it is a
    listed guard file) -- and, for a span's code line that carries a trailing comment, that comment
    alone: a span exempts its PATTERNS, never prose written beside them (#2580 review round 4).
    Lines come out as written: the structured scan exempts by phrase-span containment, the
    raw-word comparator by `_mask_exemptions` (#2584). Both read through here, so they see the same
    files and the same pattern-list spans."""
    rel = path.relative_to(ROOT).as_posix()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return
    skip = _pattern_list_lines(rel, text) if rel in _PATTERN_LIST_SPANS else frozenset()
    comments = _trailing_comments(text, skip) if skip else {}
    for line_num, line in enumerate(text.splitlines(), 1):
        if line_num not in skip:
            yield line_num, line
        elif line_num in comments:
            yield line_num, comments[line_num]


def _scan_file(file_path):
    """Scan a single file and return violations found."""
    violations = []
    rel = file_path.relative_to(ROOT)

    for line_num, line in _scannable_lines(file_path):
        spans = _phrase_spans(line)
        for pattern, description in _PATTERNS:
            for match in re.finditer(pattern, line, re.IGNORECASE):
                if _inside_a_phrase(match, spans):
                    continue
                if description == _OPS_TOKEN_DESCRIPTION and _ops_token_is_branch(line, match):
                    continue
                violations.append({
                    "file": rel.as_posix(),
                    "line": line_num,
                    "pattern": description,
                    "text": match.group()[:80],  # Truncate long matches
                    "line_text": line.strip(),
                })

    return violations


def _scanned_paths():
    """Every file the guard scans, each once, sorted: the public surface of `ROOT`
    (`public_surface.public_files`), as paths."""
    return [ROOT / rel for rel in public_surface.public_files(ROOT)]


#: One scan per process for the real tree (SCALABILITY, #2580): with tests/ in scope a scan costs
#: about 5.3 s against 2.3 s without (measured), and five tests here read it. Cost is linear in
#: scanned lines, paid once per run. Only the module's real root is cached, so a planted-tree test
#: never sees a stale result, and nothing writes scanned files mid-process (controls run in their
#: own processes).
_SCAN_MEMO = {}


def _scan_files():
    """Scan the core's surface for private-name patterns."""
    cacheable = ROOT == _REAL_ROOT
    if cacheable and "rows" in _SCAN_MEMO:
        return list(_SCAN_MEMO["rows"])
    violations = [v for path in _scanned_paths() for v in _scan_file(path)]
    if cacheable:
        _SCAN_MEMO["rows"] = violations
    return list(violations)


def test_no_private_names():
    """STRICT (#2584; no exemption list since #2706): the scan finds nothing."""
    problems = _strict_problems(_scan_files())
    assert not problems, _strict_message(problems)


_RECHECK = "Recheck: python3 tests/test_no_private_names.py"


def _strict_problems(rows):
    """One `("new", (file, line, pattern, text))` per distinct `(file, line, pattern)` the scan
    found: since #2706 there is no exemption list, so every row is a problem."""
    lines = {(r["file"], r["line"], r["pattern"]): r["line_text"] for r in rows}
    return [("new", (f, n, pat, text)) for (f, n, pat), text in sorted(lines.items())]


def _strict_message(problems):
    out = ["the scanned surface holds private names:"]
    for _kind, (f, n, pat, text) in problems:
        out.append("  NEW %s:%d [%s] %s" % (f, n, pat, text[:160]))
    out.append("  -> reword it; there is no baseline and no exemption list to add it to")
    out.append(_RECHECK)
    return "\n".join(out)


def _load(name, rel):
    """A core module, cross-loaded by path under a private name (the doctor's parity-test idiom)."""
    spec = importlib.util.spec_from_file_location(name, _REAL_ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: The `_PATTERN_SAMPLES` lines that are JSON objects: a private block as a config holds it.
_JSON_SAMPLES = [v for v in _PATTERN_SAMPLES.values() if v.startswith("{")]

#: Every private name a stale or copied `.sdlc/config.json` could carry as a top-level key -- each
#: `_PATTERNS` description that is an identifier, plus the keys of the JSON-object sample. The two
#: config keys the core read for one release before #2706 are among them. DERIVED from the listed
#: constants, never spelled: a test body is scanned like any other line.
_PRIVATE_KEYS = (tuple(d for _rx, d in _PATTERNS if d.isidentifier())
                 + tuple(k for v in _JSON_SAMPLES for k in json.loads(v)))


def test_a_private_name_as_a_config_key_switches_nothing():
    """#2706: the core reads no private-side config key. The one-release aliases are gone, and a
    config that still holds one reads exactly as if it did not: nothing is migrated, and nothing is
    said about the old key, with ONE exception: #2738 (Q1-G9) says the journal's own pre-rename
    block out loud -- a suffix on the `/sigma-doctor features` journal row telling the operator to
    move the value -- while still migrating and aliasing nothing. Its source spells that key from
    fragments (`doctor._LEGACY_JOURNAL_BLOCK`), so no shipped line carries it whole.

    Each key alone carries a block that the old aliases would have honoured (`enabled: true` AND a
    `project_id`). The journal switch, an actual journal write through `ledger.append` (the path
    every emitter takes, so a read re-added at its own gate is caught too), adoption, every
    `/sigma-doctor features` row and the `managed settings` check row must read byte-for-byte as
    they do for `{}`, except that one key's journal row, which must read as the `{}` row plus the
    exact note and nothing else. Non-vacuous both ways: the key set is non-empty and every key is
    one the scan forbids, and the SAME block under the core's own keys does change what is seen --
    so the comparison can go red.

    Hermetic: every probe `run` answers "unavailable", every DI path points into the temporary
    directory, `cheap_only` skips the plugin CLI and the network, and the doctor's one real child
    process (`reviewer.py resolve`, 10 s timeout) is stubbed on this private module copy -- so a
    timeout under load cannot make one call differ from its baseline. The `append` call pins
    `ledger.actor`: without it the positive control resolves the writer through a live `gh api
    user` call, which pytest's `_no_live_gh` guard would block but the script gesture would make."""
    ledger = _load("_k_ledger", "skills/sigma-loop/scripts/ledger.py")
    managed = _load("_k_managed_settings", "skills/sigma-loop/scripts/managed_settings.py")
    doctor = _load("_k_doctor", "skills/sigma-doctor/scripts/doctor.py")
    doctor._resolved_mechanism = lambda sdlc_dir: ""
    block = {"enabled": True, "project_id": "p1"}

    assert len(_JSON_SAMPLES) == 1, _JSON_SAMPLES
    assert len(_PRIVATE_KEYS) >= 2, _PRIVATE_KEYS
    for key in _PRIVATE_KEYS:
        assert any(re.search(rx, json.dumps({key: {}}), re.I) for rx, _d in _PATTERNS), key
    legacy = doctor._LEGACY_JOURNAL_BLOCK
    assert legacy in _PRIVATE_KEYS, (legacy, _PRIVATE_KEYS)   # the exception names a real key
    journal_row = "journal (local event records)"
    note = ("; legacy `%s` block present; the journal reads `journal.enabled` only; "
            "move the value" % legacy)

    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp).resolve() / ".sdlc"
        base.mkdir()

        def seen(cfg):
            (base / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
            wrote = ledger.append(str(base), dict(cfg, ledger={"actor": "k"}), "gate", "g.md",
                                  stream="events", gate="merge", verdict="pass") is not None
            journal = (base / "events").exists()
            shutil.rmtree(base / "events", ignore_errors=True)
            check = doctor.check(str(base), run=lambda args: "", site_packages_dirs=[],
                                 scheduled_tasks_dir=str(base / "no-tasks"),
                                 installed_plugins_path=str(base / "no-plugins.json"),
                                 cheap_only=True)
            return (ledger.journal_enabled(cfg), ledger.journal_on(str(base), cfg), wrote, journal,
                    managed.declared_project_id(cfg), managed.is_adopted(str(base), cfg),
                    doctor.features(str(base), run=lambda args: "",
                                    scheduled_tasks_dir=str(base / "no-tasks")),
                    [c["name"] for c in check if c["name"].startswith("managed settings:")])

        baseline = seen({})
        assert baseline[:6] == (False, False, False, False, None, False), baseline[:6]
        assert len(baseline[7]) == 1, baseline[7]
        assert [r[0] for r in baseline[6]].count(journal_row) == 1, baseline[6]

        def expected(key):
            """The baseline, except the one journal row the rename note is sanctioned to change."""
            if key != legacy:
                return baseline
            rows = [(n, s + note, f) if n == journal_row else (n, s, f) for n, s, f in baseline[6]]
            return baseline[:6] + (rows,) + baseline[7:]

        observed = {k: seen({k: dict(block)}) for k in _PRIVATE_KEYS}
        aliased = [k for k in _PRIVATE_KEYS
                   if observed[k][:6] != baseline[:6] or observed[k][7] != baseline[7]]
        assert not aliased, "private-name config key(s) change what the core does: %s" % aliased
        switched = [k for k in _PRIVATE_KEYS if observed[k] != expected(k)]
        assert not switched, "private-name config key(s) change what the doctor says: %s" % switched
        assert expected(legacy) != baseline   # the one exception is a real, non-empty difference
        core = seen({"journal": dict(block), managed.CONFIG_KEY: dict(block)})
        assert core[:6] == (True, True, True, True, "p1", True), core[:6]   # the harness sees ON
        assert core != baseline


# --------------------------------------------------------------------------- #2580: tests/ in scope

#: A pattern description its own regex matches -- the planted private name, DERIVED from the
#: listed `_PATTERNS` rather than spelled here (a test body is scanned like any other line).
_D = next(d for rx, d in _PATTERNS if re.search(rx, d, re.I))

#: The cross-boundary guard, by path: the one listed guard file the planted-tree tests reproduce
#: (the segment guard's until #2584 folded it into this one).
_SEGMENT_GUARD = "tests/test_no_cross_boundary_paths.py"


def _segment_list():
    """That guard's one listed pattern-list name."""
    return _PATTERN_LIST_SPANS[_SEGMENT_GUARD][0]


def _baselines_state():
    """What a baseline write would change: the old census directory's existence and listing."""
    d = _REAL_ROOT / "tests" / "boundary_baselines"
    return (d.exists(), sorted(x.name for x in d.iterdir()) if d.exists() else [])


@contextlib.contextmanager
def _planted_root():
    """A temporary git tree as `ROOT`, restored afterwards -- so a planted-tree test needs no pytest
    fixture and the script gesture runs it too (#2584 review round 3, N3)."""
    module = sys.modules[__name__]
    saved = module.ROOT
    with tempfile.TemporaryDirectory() as tmp:
        module.ROOT = pathlib.Path(tmp).resolve()
        try:
            yield module.ROOT
        finally:
            module.ROOT = saved


def _plant(root, rel, text):
    """Plant one file in a real git tree: the surface is git's, so a planted tree must be one."""
    public_surface.plant(root, {rel: text})
    return root / rel


def test_a_comment_inside_a_pattern_list_is_still_scanned():
    """Only a span's code is exempt: a line that is only a comment, and a comment trailing a code
    line, are prose, scanned like any other. (The literals themselves are the span's patterns and
    pins; their SHAPE is pinned by `test_the_exemptions_are_exactly_the_pattern_lists`, and their
    content is a guard change for diff review.) The code on the trailing-comment line stays
    exempt."""
    with _planted_root() as root:
        _plant(root, _SEGMENT_GUARD,
               f'{_segment_list()} = {{\n    # {_D}\n    "{_D}",\n    "x",  # {_D}\n}}\n')
        rows = _scan_files()
    got = {(r["file"], r["line"]) for r in rows}
    assert got == {(_SEGMENT_GUARD, 2), (_SEGMENT_GUARD, 4)}, got
    assert all(r["line"] != 3 for r in rows), rows          # the pattern line itself stays exempt


def test_the_scan_covers_tests_and_exempts_only_pattern_lists():
    """The span exemption, both ways, on a planted tree: an ordinary test line is caught; a guard
    file's listed span is not, but its next line is. (Since #2584 there is no census file.)"""
    with _planted_root() as root:
        _plant(root, "tests/test_a.py", f"# {_D}\n")
        _plant(root, _SEGMENT_GUARD, f'{_segment_list()} = {{"{_D}"}}\n# {_D}\n')
        _plant(root, "skills/x.py", "x = 1\n")
        got = {(r["file"], r["line"]) for r in _scan_files()}
    assert got == {("tests/test_a.py", 1), (_SEGMENT_GUARD, 2)}, got


def test_a_drifted_pattern_list_fails_loudly():
    """A renamed listed constant, a doubly-assigned one, and a statement sharing a span's line each
    raise `ExemptionDrift` naming the file -- never a silent loss or widening of the exemption."""
    cases = {
        "renamed": f'{_segment_list()}_NAMES = {{"{_D}"}}\n',
        "twice": f'{_segment_list()} = {{"{_D}"}}\n{_segment_list()} = set()\n',
        "shared": f'{_segment_list()} = {{"{_D}"}}; import os\n',
    }
    for name, text in cases.items():
        with _planted_root() as root:                       # one planted tree per case
            _plant(root, _SEGMENT_GUARD, text)
            try:
                _scan_files()
            except ExemptionDrift as exc:
                assert _SEGMENT_GUARD in str(exc), (name, str(exc))
            else:
                raise AssertionError("%s: a drifted pattern list did not raise ExemptionDrift"
                                     % name)


def _literal_shape_errors(rel, name, check):
    """Errors if top-level `name` in guard file `rel` is not a pure literal of the shape `check`
    accepts -- no calls, no arithmetic, no names: what the exempt span holds is exactly data."""
    tree = ast.parse((_REAL_ROOT / rel).read_text(encoding="utf-8"))
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else [])
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return [] if check(node.value) else ["%s: %s is not a pure literal of its pinned shape"
                                                 % (rel, name)]
    return ["%s: %s not found" % (rel, name)]


def _is_str(n):
    return isinstance(n, ast.Constant) and isinstance(n.value, str)


def _is_pos_int(n):
    return (isinstance(n, ast.Constant) and type(n.value) is int and n.value > 0)


def _is_plain_data(v):
    """A pure literal: a str/int/bool constant, or a tuple/list/set/dict of pure literals, or
    `frozenset(<one such literal>)`."""
    if isinstance(v, ast.Constant):
        return isinstance(v.value, (str, int, bool))
    if isinstance(v, (ast.Tuple, ast.List, ast.Set)):
        return all(_is_plain_data(e) for e in v.elts)
    if isinstance(v, ast.Dict):
        return all(k is not None and _is_plain_data(k) and _is_plain_data(x)
                   for k, x in zip(v.keys, v.values))
    return (isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == "frozenset"
            and len(v.args) == 1 and not v.keywords and _is_plain_data(v.args[0]))


def _is_residual_pin(v):
    """{"file": {"token": <positive int>, ...}, ...} -- string keys, int values, nothing computed."""
    return isinstance(v, ast.Dict) and all(
        _is_str(k) and isinstance(d, ast.Dict) and d.keys
        and all(_is_str(t) and _is_pos_int(n) for t, n in zip(d.keys, d.values))
        for k, d in zip(v.keys, v.values))


def test_the_exemptions_are_exactly_the_pattern_lists():
    """The exemption set, pinned BY VALUE: widening it is a visible, deliberate edit here. The
    exact-phrase exemptions are pinned too (a new phrase would hide whole lines), and every listed
    constant, all of which sit inside exempt spans, must be plain data -- the ratchet pins of their
    exact shape, with keys that are whole raw words -- so no COMPUTED value can smuggle prose past
    both scans (#2580 review rounds 6-7). The literal strings themselves (a pattern's description,
    a sample line) are guard content: a change to them is a guard change that diff review reads."""
    # The PRD Appendix B phrases. Each line below holds one, so the scan exempts it by design.
    assert _EXEMPTIONS == frozenset({"critical insight", "critical insights", "CRITICAL_INSIGHT"})
    rel = "tests/test_no_private_names.py"
    errors = (_literal_shape_errors(rel, "RAW_RESIDUAL_SHIPPED", _is_residual_pin)
              + _literal_shape_errors(rel, "RAW_RESIDUAL_TESTS", _is_residual_pin))
    # Every OTHER listed constant, in every guard file, must be plain data too (#2580 review
    # round 7): strings, numbers and tuples/lists/sets/dicts of them, or `frozenset({...})` of
    # them -- never a comprehension, a call on prose, or arithmetic.
    for guard, names in _PATTERN_LIST_SPANS.items():
        for name in names:
            errors += _literal_shape_errors(guard, name, _is_plain_data)
    assert not errors, errors
    assert _PATTERN_LIST_SPANS == {
        "tests/test_no_private_names.py": ("_EXEMPTIONS", "_PATTERNS", "_PATTERN_SAMPLES",
                                           "_RAW_ROOTS", "RAW_RESIDUAL_SHIPPED",
                                           "RAW_RESIDUAL_TESTS", "_SDLC_LEDGER_SKILL_CONTEXT",
                                           "_SDLC_LEDGER_BRANCH_CONTEXT"),
        "tests/test_no_cross_boundary_paths.py": ("_PRIVATE_ROOTS",),
        "tests/test_import_boundary.py": ("_BANNED_PRIVATE",),
    }
    for rel in _PATTERN_LIST_SPANS:
        path = _REAL_ROOT / rel
        assert path.is_file(), rel
        assert _pattern_list_lines(rel, path.read_text(encoding="utf-8")), rel


def test_the_real_scan_reaches_tests():
    """Non-vacuity for the widened scope: the real walk is non-empty, holds `README.md`, and reaches
    tests/, including every guard file."""
    scanned = {p.relative_to(_REAL_ROOT).as_posix() for p in _scanned_paths()}
    assert scanned and "README.md" in scanned
    print("files scanned: %d" % len(scanned))
    assert "tests/test_state.py" in scanned
    for rel in _PATTERN_LIST_SPANS:
        assert rel in scanned, rel
    print("tests/ files scanned: %d" % sum(1 for s in scanned if s.startswith("tests/")))


# --------------------------------------------------------------------------- #2580: the raw-word comparator

#: The private side's root words, matched as ANY token containing one (a possessive, a plural, a
#: capitalised form), case-insensitively. A listed pattern-list span: the one place the words are
#: spelled.
_RAW_ROOTS = ("insight", "collector", r"\bpi\b")

#: A token containing a raw root, built from `_RAW_ROOTS` (so it is not itself a spelling).
_RAW_WORD = re.compile(r"[\w'’-]*(?:%s)[\w'’-]*" % "|".join(_RAW_ROOTS), re.I)


def _raw_word_counts(under_tests):
    """`Counter` of `(file, token-as-written)` over the scanned surface -- the same files and
    pattern-list spans as the structured scan, reading each line with its `_EXEMPTIONS` phrases
    masked (`_mask_exemptions`; the structured scan uses span containment) -- split by whether the
    file sits under tests/.

    WHY IT EXISTS. `_PATTERNS` matches NAMED private things (a package path, a product name, an
    env var). A bare possessive or a capitalised product word in prose ("the dashboard's view of
    it", written with the private package's name) matches none of them, and the structured guard
    stays green over it -- measured: `-k "not raw_word"` passes with such a line in place (plan
    controls C11 (ii), C13). The comparator sees every token, and a pin says which are allowed."""
    counts = collections.Counter()
    for path in _scanned_paths():
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith("tests/") is not bool(under_tests):
            continue
        for _n, line in _scannable_lines(path):
            for m in _RAW_WORD.finditer(_mask_exemptions(line)):
                counts[(rel, m.group())] += 1
    return counts


def _pinned_counts(pin):
    """The pin as a Counter -- refusing any entry that is not a real residue: a count that is not a
    positive int, or a token that is not one whole raw word. A pin is a code line inside an exempt
    pattern-list span, so without this an empty or zero entry could carry free prose that neither
    the STALE check nor the scans would ever see (#2580 review round 5)."""
    bad = [(f, t, n) for f, toks in pin.items() for t, n in (toks.items() if isinstance(toks, dict)
                                                             else [(toks, None)])
           if not (isinstance(n, int) and not isinstance(n, bool) and n > 0
                   and isinstance(t, str) and _RAW_WORD.fullmatch(t))]
    bad += [(f, None, None) for f, toks in pin.items() if not isinstance(toks, dict) or not toks]
    assert not bad, "raw-word pin entries that are not a real residue: %r" % (bad[:10],)
    return collections.Counter({(f, t): n for f, toks in pin.items() for t, n in toks.items()})


def _raw_residual_diff(pin, under_tests):
    """`(actual, added, stale)` of the raw-word scan against `pin`."""
    actual, pinned = _raw_word_counts(under_tests), _pinned_counts(pin)
    added = sorted((k, actual[k] - pinned.get(k, 0)) for k in actual if actual[k] > pinned.get(k, 0))
    stale = sorted((k, pinned[k] - actual.get(k, 0)) for k in pinned if pinned[k] > actual.get(k, 0))
    return actual, added, stale


def _assert_raw_residual_is_exactly(pin, under_tests):
    actual, added, stale = _raw_residual_diff(pin, under_tests)
    assert not added and not stale, (
        "the raw-word residual is not exactly the pin:\n"
        + "".join("  ADDED %r x%d\n" % (k, n) for k, n in added)
        + ("  -> reword it; pin only English or a core role noun (#2580 plan T17)\n" if added else "")
        + "".join("  STALE %r x%d\n" % (k, n) for k, n in stale)
        + ("  -> remove the pin entry; the ratchet only shrinks\n" if stale else "")
        + _RECHECK)
    return actual


#: The shipped surface's raw-word residue, by file and token as written: ENGLISH (the ordinary word),
#: a CORE ROLE NOUN (the core's own read-only evidence scripts, pytest's own node collection), or a
#: CORE LABEL (the critical/research issue labels) -- never a word naming the private side (T17).
#: The per-file comments inside say which. Written from the
#: comparator's own ADDED output; the ratchet only shrinks.
RAW_RESIDUAL_SHIPPED = {
    # core role noun: the `risk-detect` evidence script (the conditional-risk row)
    "docs/features.md": {"collector": 1},
    # core label
    "docs/backlog.md": {"critical-insight": 1},
    # core role noun: the read-only evidence scripts (`*-collect.sh`, risk-detect, discovery-scan)
    "hooks/completion_gate.sh": {"collector": 1},
    "hooks/research_capture.py": {"collectors": 1},
    "skills/sigma-align/SKILL.md": {"collector": 1},
    "skills/sigma-align/scripts/alignment-collect.sh": {"collector": 2, "collectors": 1},
    "skills/sigma-audit/SKILL.md": {"collector": 1},
    "skills/sigma-audit/scripts/audit-collect.sh": {"collector": 3, "collectors": 1},
    # core label: the critical / research issue labels
    "skills/sigma-init/github-templates/CRITICAL_INSIGHT_TEMPLATE.md.tmpl": {"research-insight": 1},
    "skills/sigma-init/scripts/sdlc_init.py": {"critical-insight": 1},
    "skills/sigma-loop/references/progress.md": {"research-insight": 1},
    # core role noun: pytest's own node collection (its `found no ... for` error)
    "skills/sigma-loop/scripts/diff_revert.py": {"_collector_failed_ids": 8, "collector_failed": 5,
                                                "collectors": 4},
    # core role noun: the read-only evidence scripts
    "skills/sigma-loop/scripts/discovery-scan.sh": {"collector": 1, "collectors": 2},
    # core role noun: #1933's pytest node collection; core label
    "skills/sigma-loop/scripts/loop.py": {"collector": 1, "critical-insight": 1},
    # core role noun: the discovery-scan evidence script
    "skills/sigma-loop/scripts/pipeline.py": {"collector": 1},
    "skills/sigma-loop/scripts/risk-detect.sh": {"collector": 1},
    "skills/sigma-loop/scripts/scrub.py": {"collectors": 1},
    # core label
    "skills/sigma-loop/scripts/sources.py": {"critical-insight": 1},
    # English: the ordinary word ("that ...")
    "skills/sigma-loop/scripts/triage.py": {"insight": 1},
    # core role noun: routing the discovery-scan evidence script
    "skills/sigma-loop/scripts/work.py": {"collector": 1},
    # core role noun: the read-only evidence scripts
    "skills/sigma-radar/SKILL.md": {"collector": 1},
    "skills/sigma-research/SKILL.md": {"collector": 1},
    # English: the ordinary word ("key ...")
    "skills/sigma-retro/SKILL.md": {"insight": 2},
}


def test_the_shipped_raw_word_residual_is_exactly_the_pinned_english():
    """Both directions: a new token fails as ADDED, a reworded one as STALE. Non-vacuous: the
    shipped surface does carry pinned English and core role nouns."""
    actual = _assert_raw_residual_is_exactly(RAW_RESIDUAL_SHIPPED, under_tests=False)
    assert actual, "the raw-word scan of the shipped surface found nothing -- the scan is broken"


#: tests/'s raw-word residue, the same shape and rule as `RAW_RESIDUAL_SHIPPED` (T17).
RAW_RESIDUAL_TESTS = {
    # core role noun: the read-only evidence scripts under test (`*-collect.sh`, discovery-scan,
    # risk-detect) and the helpers that name them
    "tests/test_alignment_collect.py": {"collector": 9, "test_sdlc_align_skill_wires_the_collector": 1},
    "tests/test_audit_collect.py": {"collector": 4, "collectors": 2},
    "tests/test_audit_skill.py": {"test_names_all_four_lenses_and_grounds_them_in_the_collector": 1},
    # core role noun: pytest's own node collection (its `found no ... for` error)
    "tests/test_diff_revert.py": {
        "_collector_failed_ids": 3, "collectors": 7,
        "test_collector_failed_ids_file_level_match_does_not_credit_an_unrelated_file": 1,
        "test_collector_failed_ids_matches_the_file_level_short_summary_line": 1,
    },
    # core role noun: the read-only evidence scripts
    "tests/test_discovery_scan.py": {"collector": 1},
    "tests/test_json_string_escaping.py": {"collector": 2},
    "tests/test_pipeline.py": {"collector": 2},
    "tests/test_risk_detect.py": {"collector": 2, "collectors": 1},
}


def test_the_tests_raw_word_residual_is_exactly_the_pinned_english():
    """The same comparator over tests/. No non-empty assertion: its non-vacuity is the planted test
    below and plan control C11 (ii) -- tests/ may legitimately end with nothing to pin."""
    _assert_raw_residual_is_exactly(RAW_RESIDUAL_TESTS, under_tests=True)


def test_the_raw_word_scan_sees_what_the_structured_patterns_miss():
    """The blind spot, planted: a possessive and a capitalised word are invisible to `_PATTERNS`
    and visible to the comparator; an `_EXEMPTIONS` phrase is invisible to both (since #2584 the
    phrase alone: `test_an_exemption_phrase_exempts_only_itself` plants text beside it)."""
    r = _RAW_ROOTS[0]
    e = next(x for x in sorted(_EXEMPTIONS) if x.islower())
    with _planted_root() as root:
        _plant(root, "skills/a.py", f"# {r}'s dashboard\n# see {r.capitalize()}\n# a {e}\n")
        assert _raw_word_counts(False) == collections.Counter(
            {("skills/a.py", f"{r}'s"): 1, ("skills/a.py", r.capitalize()): 1})
        assert _scan_files() == []


# --------------------------------------------------------------------------- #2584: strict


def test_an_exemption_phrase_exempts_only_itself():
    r = _RAW_ROOTS[0]
    e = next(x for x in sorted(_EXEMPTIONS) if x.islower())
    upper = next(x for x in _EXEMPTIONS if x.isupper())
    with _planted_root() as root:
        _plant(root, "skills/a.md", f"# {e}: see {r}/doctor.py\n")
        _plant(root, "skills/b.md", f"# {e}\n# {e.upper()}\n")
        _plant(root, "skills/c.md", upper + "_TEMPLATE.md.tmpl\n")
        rows = _scan_files()
        assert [(x["file"], x["line"]) for x in rows] == [("skills/a.md", 1)], rows
        assert _raw_word_counts(False) == collections.Counter({("skills/a.md", r): 1})


def test_the_env_var_pattern_is_case_sensitive():
    r = _RAW_ROOTS[0]
    label = next(d for rx, d in _PATTERNS if "[A-Z_]" in rx)
    with _planted_root() as root:
        _plant(root, "skills/a.md", r.upper() + "_EMIT\n" + r + "_emit\n" + r.capitalize() + "_X\n")
        rows = [(x["line"], x["pattern"]) for x in _scan_files()]
    assert (1, label) in rows and (2, label) not in rows and (3, label) not in rows, rows


def test_the_guard_takes_no_arguments_and_writes_nothing():
    before = _baselines_state()
    proc = subprocess.run([sys.executable, str(_REAL_ROOT / "tests" / "test_no_private_names.py"),
                           "--write-baseline"], capture_output=True, text=True, cwd=str(_REAL_ROOT))
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert "this guard takes no arguments; there is no baseline to write (#2584)" in proc.stderr
    assert _baselines_state() == before


def test_every_pattern_finds_its_own_sample():
    """Each `_PATTERNS` entry finds its `_PATTERN_SAMPLES` line through the full scan (#2584 review
    round 2, N3), and the two lists name the same patterns: deleting or breaking any one pattern goes
    red here, whatever the live surface holds -- on pytest and on the script gesture alike."""
    descriptions = [d for _rx, d in _PATTERNS]
    assert sorted(descriptions) == sorted(_PATTERN_SAMPLES), (
        sorted(set(descriptions) ^ set(_PATTERN_SAMPLES)))
    order = sorted(_PATTERN_SAMPLES)
    with _planted_root() as root:
        _plant(root, "skills/samples.md", "".join(_PATTERN_SAMPLES[d] + "\n" for d in order))
        found = {(r["line"], r["pattern"]) for r in _scan_files()}
    missed = [d for n, d in enumerate(order, 1) if (n, d) not in found]
    assert not missed, "pattern(s) that no longer find their own sample: %s" % missed


def test_an_exemption_phrase_cannot_swallow_the_start_of_a_private_name():
    """A phrase glued to a private name leaves the name visible, whatever the glue (#2584 review
    rounds 2-3); the phrase alone, and the core's own template label, stay exempt."""
    e = next(x for x in sorted(_EXEMPTIONS, key=len) if x.islower())
    upper = next(x for x in _EXEMPTIONS if x.isupper())
    glued = [e + "/web/app.py", e + "-web", upper + "_EMIT_TOKEN=1", e + ".duckdb",
             upper + "-web", upper + ".duckdb", upper + "-emit-token", "run " + e + " emit now",
             "the " + e + " doctor"]
    exempt = [e, e.capitalize() + "s for x", e + "'s view", upper + _UPPER_PHRASE_CONTINUATIONS[0]
              + ".md.tmpl"]
    with _planted_root() as root:
        _plant(root, "skills/a.md", "".join("# %s\n" % x for x in glued + exempt))
        lines = {row["line"] for row in _scan_files()}
    assert lines == set(range(1, len(glued) + 1)), (sorted(lines), glued)


def test_planted_retired_names_are_reported():
    """#2729 control (plan A2): every retired-name pattern finds a fragment-built plant through the
    full scan -- the old env-var prefix in a test file, the old slash gesture in a SKILL.md, the old
    brand in prose, the old dir placeholder, the old repository name, and the ops token out of
    branch context -- while the same token in branch context is exempt (D-d)."""
    plants = {
        "tests/test_x.py": "TOKEN = os.environ['" "LOOP" "SMITH_TOKEN']\n",
        "skills/x/SKILL.md": "run /sdlc-" "loop first\n",
        "README.md": "Loop" "Smith runs the loop\n",
        "docs/a.md": "check <sdlc-" "dir>\n",
        "docs/b.md": "clone sdlc-" "kit\n",
        "docs/c.md": "badge at github.com/swapnil-" "agrim/" "sigma/actions\n",   # D12 (post-PR review)
        "skills/y/SKILL.md": "name: sdlc-" "ledger\n",
        "skills/d.py": "print(f'\\nsdlc-" "doctor: {n} ready.')\n",   # glued to an escape (#2729 live)
        # the namespace GLOB the Cursor rule spells (review fix: no letter follows `sdlc-`, so the
        # skill-name pattern never saw it); on a line that also says `branch`, which exempts nothing here
        "skills/w.py": "text = 'the branching model, every skill in `skills/sdlc-" "*/SKILL.md`'\n",
    }
    expected = {"retired env-var prefix", "retired skill name", "retired brand token",
                "retired dir placeholder", "retired repository name",
                "retired ops token outside branch context", "retired skill namespace glob",
                "retired owner slug on the public repo"}
    with _planted_root() as root:
        for rel, text in plants.items():
            _plant(root, rel, text)
        _plant(root, "skills/z.md", "the sdlc-" "ledger branch and origin/sdlc-" "knowledge\n")
        rows = _scan_files()
    found = {(r["file"], r["pattern"]) for r in rows}
    assert {p for _f, p in found} >= expected, sorted(found)
    assert len({f for f, _p in found} & set(plants)) == len(plants), sorted(found)
    assert not any(f == "skills/z.md" for f, _p in found), sorted(found)
    assert ("skills/w.py", "retired skill namespace glob") in found, sorted(found)


def test_a_suffixed_ops_token_in_branch_context_is_not_exempt():
    """Post-PR review cycle 3 (#2729): the exemption's token regex stopped at `[a-z0-9]` only, so it
    matched the head of a SUFFIXED token and, on a line saying `branch`, exempted the whole thing.
    The token is now bounded by `[-\\w]` on the right: the suffixed token gets no disposition and
    is flagged, while the plain ops-branch name on the same line stays exempt (D-d)."""
    assert _sdlc_ledger_disposition("push the sdlc-" "ledger-evil branch") == []
    assert _sdlc_ledger_disposition("origin/sdlc-" "knowledge-2 and sdlc-" "ledger_x") == []
    with _planted_root() as root:
        _plant(root, "skills/e.py", "push the sdlc-" "ledger-evil branch, not the sdlc-" "ledger branch\n")
        rows = _scan_files()
    got = [(r["file"], r["line"], r["pattern"]) for r in rows]
    assert got == [("skills/e.py", 1, _OPS_TOKEN_DESCRIPTION)], got


def test_the_retired_skill_pattern_names_exactly_the_shipped_skills():
    """D11's `^sigma-[a-z-]+$` filter, defence in depth: every shipped skill directory carries the
    new prefix, and the retired-skill alternation lists exactly those stems (the ledger skill's stem
    rides the ops-token pattern instead, so branch context can exempt it there)."""
    rx = next(rx for rx, d in _PATTERNS if d == "retired skill name")
    stems = set(re.search(r"\((.*)\)", rx).group(1).split("|"))
    # a skill is a folder with a SKILL.md: a leftover untracked folder (only __pycache__) is not one
    dirs = sorted(p.name for p in (_REAL_ROOT / "skills").iterdir() if (p / "SKILL.md").is_file())
    assert dirs and all(re.fullmatch(r"sigma-[a-z-]+", d) for d in dirs), dirs
    assert stems | {"ledger"} == {d[len("sigma-"):] for d in dirs}, sorted(stems ^ {d[6:] for d in dirs})


def _tests_the_script_cannot_run():
    """`test_*` functions that take a parameter: the script gesture cannot run them, so a guard
    whose documented invocation silently skipped one could not fail on it (#2584 review round 5)."""
    return [name for name, fn in list(globals().items())
            if name.startswith("test_") and inspect.isfunction(fn)
            and inspect.signature(fn).parameters]


def _zero_argument_tests():
    """Every `test_*` in this module that takes no fixture, in definition order."""
    return [(name, fn) for name, fn in list(globals().items())
            if name.startswith("test_") and inspect.isfunction(fn)
            and not inspect.signature(fn).parameters]


# The script gesture sits at the END of the file on purpose (#2580): placed above a test, it ran
# before that test existed, so `python3 tests/test_no_private_names.py` could print a pass while an
# alias pin below it was red -- a documented invocation that cannot fail.
if __name__ == "__main__":
    if sys.argv[1:]:
        print("this guard takes no arguments; there is no baseline to write (#2584)", file=sys.stderr)
        sys.exit(2)
    failed = []
    for name, fn in _zero_argument_tests():
        print("ran: %s" % name)
        try:
            fn()
        except Exception as e:              # noqa: BLE001 - every failure is reported, then rc 1
            failed.append(name)
            print("FAILED %s: %s" % (name, e), file=sys.stderr)
    skipped = _tests_the_script_cannot_run()
    if skipped:
        failed.append("script coverage")
        print("FAILED script coverage: test(s) this script cannot run (they take a parameter): %s"
              % ", ".join(skipped), file=sys.stderr)
    # Findings: the strict comparison's problems plus both raw-word pins' ADDED and STALE entries.
    findings = len(_strict_problems(_scan_files())) + sum(
        len(d[1]) + len(d[2]) for d in (_raw_residual_diff(RAW_RESIDUAL_SHIPPED, False),
                                        _raw_residual_diff(RAW_RESIDUAL_TESTS, True)))
    print("test_no_private_names: %s (%d finding(s) over %d scanned file(s))"
          % ("FAIL" if failed else "OK", findings, len(_scanned_paths())))
    sys.exit(1 if failed else 0)
