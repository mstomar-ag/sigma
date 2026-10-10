"""Pre-work backlog cross-check (0.9.21) — the LLM-free stage-1 retrieval that, given a just-picked
goal, surfaces likely DUPLICATES / BLOCKERS / OBSOLETE-BY-completed-work against the rest of the
backlog + in-flight team work, so the loop doesn't spend a full Research/Plan cycle on redundant work.

Zero LLM tokens: a stdlib TF-IDF cosine over a candidate set of issues that share ≥1 term with the goal
(rarity then drives the SCORE via idf, not membership) — exact at a few-hundred-issue scale, no
MinHash/LSH needed — plus an explicit `#N`-reference graph and the team
ledger. It EMITS EVIDENCE, renders no verdict and takes no action — a later slice's loop hook decides
whether to park-with-proof or annotate, and only THAT stage may (optionally) hand the top-K flagged
pairs to an LLM. The lexical index is cheap enough to rebuild each run, so nothing is persisted here;
the incremental cache belongs to the embedding layer (0.9.23), and its entries are keyed by embedder
identity as well as content (#542) — see `_dense_channel`.

Corpus: github mode → the gitignored board mirror (mirror.read_mirror); local mode → the goal files.
Completed work → closed mirror issues (velocity-scaled window) or local `status: done` goals.

SECRET-SAFE: findings carry issue refs + shared index TERMS (scrubbed, capped) — never a title/body/
secret. FAIL-OPEN: no corpus / no git / any error → an empty pack with a machine-readable `degraded[]`,
never a raise (a later slice calls this on the pick hot-path).

A human who reviews a parked, confident finding and rules it a false positive can dismiss that EXACT
(kind, ref) finding so a retry doesn't re-park on it identically — post `dismiss_comment(kind, ref)`
as a comment (via `loop.py note`); see `DISMISS_MARKER` / `_dismissed_findings` (#830). GitHub mode
ONLY: local goal files have no comment thread to read a dismissal back from, so local-mode discovery
surfaces an explicit `degraded` flag instead of silently no-op'ing — see `_local_dismissal_flag`.

    python3 pipeline.py crosscheck <sdlc_dir> <goal>      # prints a backlog-check/v1 JSON pack
    python3 backlog_check.py dismiss-text <kind> <ref> [reason...]  # prints the dismissal marker text
"""
import hashlib, importlib.util, json, math, pathlib, re, subprocess, sys
from collections import Counter

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


shell_policy = _load("shell_policy")


def _load_velocity():
    vp = _HERE.parent.parent / "sigma-velocity" / "scripts" / "velocity.py"
    spec = importlib.util.spec_from_file_location("velocity", vp)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


scrub = _load("scrub").scrub
mirror = _load("mirror")
legacy = _load("legacy")   # #239: dismissals and decomposition markers under the previous name
sources = _load("sources")
blocker_scan = _load("blocker_scan")   # #1487: the blocker vocabulary + unpark-QA strip, shared
                                       # with mirror.py so a full-body scan at fetch time and an
                                       # excerpt scan at check time can never drift apart
goal_size = _load("goal_size")     # #521: shared decomposition-marker constants -- single source of
                                   # truth with loop.py's decompose_check guard

SCHEMA = "backlog-check/v1"
_DEFAULTS = {"dup_threshold": 0.72, "obsolete_threshold": 0.72, "park_threshold": 0.80, "top_k": 8}
_AUTO_TARGET_MERGES = 50            # "auto" window ≈ the span of the last ~50 merges
_AUTO_WINDOW_FALLBACK = 90         # days, when velocity has no history (fresh / non-git repo)
_TITLE_WEIGHT = 3                  # a title term counts 3× a body term (bug-dedup convention)

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "a an the of to in on for and or is are be this that it with as at by from we you i he she they "
    "them our your their its it's not no do does did done can will would should could may might must "
    "if then else when while how what which who whom whose why has have had was were been being get "
    "add fix use make set new via per into out up off over under about only also all any each".split())
# The blocker vocabulary — blocking phrasing immediately followed by a #N reference, an EXPLICIT,
# high-precision blocker edge. DEFINED IN `blocker_scan.py` (#1487, with its full #1393 rationale
# and boundary table) so that `mirror.py` can run the SAME scan over an issue's full body at fetch
# time; `mirror` is the lower module and cannot import this one.
#
# Re-exported here as a module ATTRIBUTE under its historical name, deliberately: `doctor.py`,
# `triage.py` and `sources.py` all read `backlog_check._BLOCK_RE` live off this module by documented
# contract (`sources.py`'s `_get_backlog_check()` comment; `test_triage.py`'s sentinel-patch test),
# and that keeps working against the exact same compiled object.
_BLOCK_RE = blocker_scan._BLOCK_RE


def _tokens(text):
    return [t for t in _TOKEN_RE.findall((text or "").lower()) if len(t) >= 2 and t not in _STOP]


# #1533: the fixed scaffolding `handoff.issue_body()` / `handoff._tracked_issue_body()` write into
# EVERY issue the kit ever opens on its own behalf (a hand-off, a same-area follow-up, a queued
# finding), plus the default title `create_tracked_issue()` / `hand_off()` fall back to when no
# caller supplies one. Two such issues describing genuinely UNRELATED work were measured at 0.825+
# cosine on that shared prose ALONE -- enough by itself to clear `park_threshold` (0.80, the shipped
# default) and auto-park a goal against a "duplicate" that shares nothing but the kit's own wording;
# the more disciplined the template, the worse this gets, since every line it adds is one MORE term
# every tracked issue now shares with every other one.
#
# The fix is to strip it, not down-weight it with a stopword list: it is *exactly* known, because
# this codebase generates it. Each pattern below mirrors one fixed span of `handoff.issue_body()` /
# `handoff._tracked_issue_body()` / their default title format, verbatim. `**What is needed:** ` (and
# no other line) strips only the LABEL, never the free text after it -- that text is the one part of
# either template holding actual, discriminating content (the `why` a caller supplied), and it has to
# survive so a genuine duplicate -- two issues that really do describe the same "why" -- still scores
# well above the parking line once the shared boilerplate around it is gone (pinned in both
# directions by `tests/test_backlog_check.py`).
#
# Deliberately NOT imported from `handoff.py` (unlike `_strip_offboard_prefixes`'s reuse of
# `sources.OFFBOARD_COMMENT_PREFIXES` above): those fixed spans live inline inside f-string list
# builders there, not as standalone constants, and lifting them out is a bigger touch to that
# module's own template functions than this fix calls for. What keeps this from silently drifting
# out of step is the test suite calling the REAL `handoff.issue_body()` / `_tracked_issue_body()`
# functions rather than hand-typed copies of their prose -- a future wording change there that this
# list stops covering shows up as the stripped score creeping back up, not as a silent miss.
_TEMPLATE_STRIP_PATTERNS = (
    # "Blocking another area's work. Raised automatically by the SDLC loop from goal `<goal>`."
    # (issue_body) / "Raised automatically by the SDLC loop from goal `<goal>`." (_tracked_issue_body)
    # -- the goal reference is provenance (which goal filed this), not content, and is exactly the
    # value a whole overnight batch of follow-ups shares.
    re.compile(r"(?:Blocking another area's work\. )?"
               r"Raised automatically by the SDLC loop from goal `[^`]*`\."),
    # "**Area:** `<area>`" -- stripped whole (label and value): the value is a routing label, not a
    # description of the work, and it repeats verbatim across every follow-up filed to the same area.
    re.compile(r"\*\*Area:\*\* `[^`]*`"),
    # "**Blocked goal:** <url>" (issue_body only, when a blocked-goal URL is available)
    re.compile(r"\*\*Blocked goal:\*\* \S*"),
    # "**Done when:** the dependency above exists and the blocked goal can proceed." (issue_body) /
    # "...the work above exists..." (_tracked_issue_body) -- fully fixed, no variable in either.
    re.compile(r"\*\*Done when:\*\* the (?:dependency|work) above exists and the blocked goal can "
               r"proceed\."),
    # "Reply on this issue if this should be re-scoped, re-assigned, or declined — the blocked goal
    # is parked until then." -- fully fixed, identical text in both templates.
    re.compile(r"Reply on this issue if this should be re-scoped, re-assigned, or declined — the "
               r"blocked goal is parked until then\."),
    # "**What is needed:** " -- LABEL only; the `why` text that follows is real content and stays.
    re.compile(r"\*\*What is needed:\*\* "),
    # The default title `create_tracked_issue()` / `hand_off()` render when no caller supplies one:
    # "[<area>] dependency from <goal>" / "[<area>] finding from <goal>" / "[<area>] dependency for
    # <goal>" -- title terms count 3x (`_TITLE_WEIGHT`), so this one is the heaviest single contributor.
    re.compile(r"\[[^\]]*\]\s+(?:dependency|finding)\s+(?:from|for)\s+\S+"),
)


def _strip_known_templates(text):
    """`text` with every fixed span in `_TEMPLATE_STRIP_PATTERNS` removed (#1533). A no-op on any
    text that carries none of them -- i.e. every hand-written issue on a real board -- so this only
    ever narrows the token set a kit-filed issue contributes; it never touches anything else."""
    for pat in _TEMPLATE_STRIP_PATTERNS:
        text = pat.sub("", text)
    return text


def _doc_tokens(title, body):
    c = Counter()
    for t in _tokens(_strip_known_templates(title or "")):
        c[t] += _TITLE_WEIGHT
    for t in _tokens(_strip_known_templates(body or "")):
        c[t] += 1
    return c


def _idf(docs):
    df = Counter()
    for d in docs:
        for term in d["tokens"]:
            df[term] += 1
    n = len(docs)
    return {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}      # smoothed idf


def _vector(tokens, idf):
    return {t: c * idf.get(t, 0.0) for t, c in tokens.items()}


def _lex_weight(tokens, idf, mode="tfidf", avgdl=0.0, k1=1.2, b=0.75):
    """The lexical term-weight vector. `tfidf` (default) is IDENTICAL to _vector — so the default path
    is byte-identical to pre-0.9.23. `bm25` uses BM25 saturation + length normalization (the stronger
    bug-dedup baseline, Q1's opt-in), as symmetric BM25-weighted cosine."""
    if mode != "bm25":
        return {t: c * idf.get(t, 0.0) for t, c in tokens.items()}
    dl = sum(tokens.values()) or 1
    norm = (1 - b + b * dl / avgdl) if avgdl else 1.0
    return {t: idf.get(t, 0.0) * (c * (k1 + 1)) / (c + k1 * norm) for t, c in tokens.items()}


def _list_cosine(a, b):
    """Cosine over two dense (embedding) vectors — plain lists of equal length."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    if dot <= 0:
        return 0.0
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _cosine(v1, v2):
    if not v1 or not v2:
        return 0.0
    small, big = (v1, v2) if len(v1) <= len(v2) else (v2, v1)
    dot = sum(w * big.get(t, 0.0) for t, w in small.items())
    if dot <= 0:
        return 0.0
    n1 = math.sqrt(sum(w * w for w in v1.values()))
    n2 = math.sqrt(sum(w * w for w in v2.values()))
    return dot / (n1 * n2) if n1 and n2 else 0.0


def _shared_terms(a, b, idf, k=6):
    shared = set(a["tokens"]) & set(b["tokens"])
    return [scrub(t) for t in sorted(shared, key=lambda t: (-idf.get(t, 0.0), t))[:k]]


def _finding(kind, ref, score, source, evidence, confident):
    return {"kind": kind, "ref": str(ref), "score": round(float(score), 4),
            "source": source, "evidence": list(evidence)[:8], "confident": bool(confident)}


def _load_config(sdlc_dir):
    try:
        return json.loads((pathlib.Path(sdlc_dir) / "config.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _num(cfg, key, default):
    """Coerce a config value to the default's numeric type, falling back to the default on anything
    bad (a hand-edited `top_k: "all"` or `dup_threshold: "high"` must degrade to the default — not
    zero out the whole check via the outer catch). `bool` (an int subclass) is treated as unset."""
    v = cfg.get(key, default)
    if isinstance(v, bool):
        return default
    try:
        return type(default)(v)
    except (TypeError, ValueError):
        return default


def _docs_from_records(recs):
    """Normalize board-mirror-SHAPED records (mirror.build_records's own output -- number/
    title/body_excerpt/state/closed_at/...) into the doc shape the TF-IDF engine consumes. Factored
    out of `_build_corpus`'s github branch (#1204) so a caller that already has its OWN record set
    -- `mirror.fetch_dependency_records`'s assignee-unrestricted, multi-label corpus, built for
    handoff.py's file-time duplicate search, rather than the cached, narrower `board-mirror.ndjson`
    this function's other caller (`_build_corpus`, below) reads -- gets the identical normalization
    (same scrub-before-tokenize discipline, same field shape) without re-deriving it."""
    docs = []
    for r in recs:
        if not isinstance(r, dict):
            continue                                # a garbage record degrades ONE row, not all
        # the mirror already scrubs, but re-scrub the RAW (case-preserving) text here BEFORE
        # tokenizing: tokens are lowercased, and scrub's shape patterns are case-sensitive, so a
        # secret must be redacted while its case survives — else a lowercased secret token could
        # reach `evidence`. Idempotent on an already-scrubbed mirror; robust to a hand-built one.
        title, body = scrub(r.get("title") or ""), scrub(r.get("body_excerpt") or "")
        state = (r.get("state") or "open").lower()
        # #1487: `blocker_refs` rides ALONGSIDE `raw`, never inside it. `raw` feeds the TF-IDF and
        # embedding channels for every doc in the corpus, so folding the refs back into it as
        # synthetic text would silently change dedup/obsolescence scoring — the same reason
        # `_explicit_blockers` keeps its comment `extra_text` a separate parameter.
        docs.append({"ref": str(r.get("number")), "title": title, "raw": title + "\n" + body,
                     "body": body, "tokens": _doc_tokens(title, body), "open": state == "open",
                     "completed": state == "closed", "closed_at": r.get("closed_at"),
                     "blocker_refs": r.get("blocker_refs") or [],
                     "source": "mirror"})
    return docs


def _build_corpus(sdlc_dir, config):
    """Returns (docs, degraded). doc = {ref,title,raw,body,tokens,open,completed,closed_at,source}.
    `open` = a live dedup candidate; `completed` = finished work that can obsolete the goal. `body` is
    the scrubbed body ALONE (unlike `raw`, which is `title + "\\n" + body`) -- #521's decomposition-
    marker exemption needs the body's own first line, and `raw.splitlines()[0]` is always the TITLE,
    never the marker."""
    degraded = []
    if mirror.is_github_mode(config):
        recs = mirror.read_mirror(sdlc_dir)
        if not recs:
            degraded.append("no_mirror")
        return _docs_from_records(recs), degraded
    # local-files mode: the goal files ARE the corpus
    disc, fm = _load("discovery"), _load("frontmatter")
    docs = []
    for p in sorted((pathlib.Path(sdlc_dir) / "goals").glob("*.md")):
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        meta = fm.parse(text)
        if not meta:
            continue                                    # a README etc. — not a goal
        status = meta.get("status")
        title = meta.get("title") or ""
        # #544: NOT text.split("---", 2) -- a bare '---' substring inside a frontmatter VALUE (e.g. a
        # title like "Fix the A---B connector bug") counts as a split point ahead of the real closing
        # fence, under-stripping the body (a title fragment + the real fence get prepended to it).
        # fm.strip() reuses the same line-anchored _FENCE regex fm.parse() already applies above.
        body = scrub(fm.strip(text))                    # local bodies aren't pre-scrubbed like the mirror
        docs.append({"ref": str(p), "title": scrub(title), "raw": scrub(title) + "\n" + body,
                     "body": body, "tokens": _doc_tokens(scrub(title), body),
                     "open": status not in disc._SKIP, "completed": status == "done",
                     "closed_at": None, "source": "goals"})
    if not docs:
        degraded.append("no_goals")
    return docs, degraded


def _closed_window_days(config, run=None, velocity_measure=None):
    """Velocity-scaled 'recently closed' window (config `backlog_check.closed_window_days`, default
    'auto'): a window that captures ~50 recent merges, so obsolescence tracks the repo's real pace.
    A pinned integer/string wins; 'auto' inverts velocity; a rate-0 (fresh/non-git) repo → fallback."""
    win = (config.get("backlog_check") or {}).get("closed_window_days", "auto")
    if isinstance(win, bool):                           # guard: bool is an int subclass
        win = "auto"
    if isinstance(win, int):
        return max(1, win)
    if isinstance(win, str) and win.isdigit():
        return max(1, int(win))
    try:
        measure = velocity_measure or _load_velocity().measure
        m = measure(days=30, run=run)
        rate = m.get("prs_per_day") or m.get("commits_per_day") or 0
        if rate and rate > 0:
            return max(7, min(180, math.ceil(_AUTO_TARGET_MERGES / rate)))
    except Exception:
        pass
    return _AUTO_WINDOW_FALLBACK


def _within_window(doc, window_days, now):
    """A completed issue counts as obsolescence evidence only if closed within the window. A local
    `done` goal has no closed date → we can't window-filter it, so it always counts (fail-open)."""
    ca = doc.get("closed_at")
    if not ca:
        return True
    try:
        import calendar, time
        t = calendar.timegm(time.strptime(ca.replace("Z", "GMT"), "%Y-%m-%dT%H:%M:%S%Z"))
        ref_now = now if now is not None else time.time()
        return (ref_now - t) <= window_days * 86400
    except Exception:
        return True                                     # unparseable date → don't drop the candidate


#: #1392's fenced `/sigma-unpark` Q&A span, and the strip that keeps its ordinary English out of
#: every `_BLOCK_RE` scan. DEFINED IN `blocker_scan.py` (#1487) so `mirror.py`, which now runs the
#: same scan over an issue's FULL body at fetch time, applies the identical strip without importing
#: this module (it is the LOWER module -- see `blocker_scan`'s own docstring). Re-exported here
#: under their historical names: `unpark.py` reads `backlog_check.UNPARK_QA_START`, and
#: `_blocker_haystack` below resolves `_strip_unpark_qa` through this module's globals, so both
#: remain patchable exactly as before.
UNPARK_QA_START = blocker_scan.UNPARK_QA_START
UNPARK_QA_END = blocker_scan.UNPARK_QA_END
_strip_unpark_qa = blocker_scan.strip_unpark_qa


def _blocker_haystack(goal_doc, extra_text=""):
    """The ONE text `_BLOCK_RE` is ever run over, built the same way for both scanners below.

    Deliberately a shared chokepoint rather than two identical inline expressions (#1392): the
    scrub above only works if EVERY scan path applies it, and `_explicit_blockers` /
    `_referenced_blocker_refs` are called from different modules for different purposes. One place
    to strip means a future third scanner cannot silently reintroduce the re-park bug by composing
    its own haystack."""
    return _strip_unpark_qa(goal_doc.get("raw", "")
                            + ("\n" + extra_text if extra_text else ""))


def _record_blocker_refs(goal_doc):
    """`[(ref, phrase), ...]` from the goal doc's own `blocker_refs` field — the refs `mirror.py`
    scanned off the issue's FULL body at fetch time (#1487), which the 500-character `body_excerpt`
    that becomes `raw` may have truncated away entirely.

    A thin alias for `blocker_scan.read_refs` (#1499), which is where the field's read rules now
    live — beside `extract_refs`, the function that WRITES it, so the two cannot drift. See that
    function for the full degradation table (an absent field, a bare `str`, a non-digit ref, a
    self-reference). Kept under this name because `_explicit_blockers` and
    `_referenced_blocker_refs` below both call it, and this module's own tests pin it.

    (`mirror.is_fresh` refuses a mirror whose meta records an older schema, so a pre-#1487 record's
    empty result lasts one pick, not one TTL.)"""
    return blocker_scan.read_refs(goal_doc, self_ref=goal_doc.get("ref"))


def _explicit_blockers(goal_doc, docs, extra_text=""):
    """Regex the goal's own text for explicit `blocked by/depends on … #N` edges. High precision: a
    blocker is asserted ONLY when N is a real OPEN issue in the corpus (a closed ref isn't a blocker).

    `extra_text` is scrubbed comment text from `_goal_comment_text()` below — kept as a SEPARATE
    parameter rather than mutated into `goal_doc["raw"]` itself, because `raw` also feeds the
    embedding channel (`_dense_channel`) for EVERY doc in the corpus, not just the goal being
    checked; folding comment text into it would silently change dedup/embedding scoring too, which
    is out of scope here (#389 is about the explicit-blocker fallback only).

    CONFIDENCE, not just presence, now depends on the matched PHRASE (#1497). This scan keeps the
    full `TRIGGERS` vocabulary (see `blocker_scan.TRIGGERS`'s own docstring for why it must, unlike
    the fetch-time scan) — a weak trigger ("needs", "after", "requires", "waiting on") is prose-
    shaped, not a declaration: the label vocabulary itself (including the legacy confirmation label) and
    Sigma's own quoted park-comment boilerplate ("needs human review") both trip it on real
    board text, and `_resolve_blockers_for_park` hands a `confident` finding straight to
    `blockers.resolve`, which WRITES TO the referenced issue. So the finding still stays PRESENT —
    evidence, never silently discarded — but only a canonical marker (`blocker_scan.
    EXPLICIT_TRIGGERS`: "blocked by" / "depends on" / "depends upon") sets `confident=True`; a weak
    trigger downgrades to advisory, exactly like a human-dismissed finding (`_apply_dismissal`
    below) or an exempted decomposition child. `decide()` never parks or resolves on an advisory
    finding alone, but still surfaces it in the proceed note, so a human still sees it.

    A ref matched by BOTH a weak and a canonical trigger (e.g. mentioned twice in one body) is
    confident — a genuine canonical marker elsewhere in the text is not weakened by an unrelated
    prose mention of the same issue."""
    open_refs = {d["ref"] for d in docs if d["open"]}
    haystack = _blocker_haystack(goal_doc, extra_text)
    matched = {}                                    # ref -> (confident, phrase) -- OR across matches
    for m in _BLOCK_RE.finditer(haystack):
        n = m.group(2)
        if blocker_scan.is_cross_repo(haystack, m):     # #709: owner/repo#N is not local #N
            continue
        if n == goal_doc["ref"] or n not in open_refs:
            continue
        phrase = m.group(1).lower()
        confident = phrase in blocker_scan.EXPLICIT_TRIGGERS
        cur = matched.get(n)
        if cur is None or (confident and not cur[0]):
            matched[n] = (confident, phrase)
    out = [_finding("blocked-by", ref, 1.0, "explicit", [scrub(phrase)], confident)
           for ref, (confident, phrase) in matched.items()]
    seen = set(matched)
    # #1487: refs the mirror scanned off the FULL body, for the case the excerpt truncated the marker
    # away. Appended ONLY for refs the haystack scan did not already find, so a marker that DOES fit
    # inside the excerpt produces byte-identical output to before — this path can add an edge, never
    # duplicate or re-score one. Same open-only precision rule: a closed ref is not a blocker.
    # Always confident=True: `mirror.normalize_issue` already restricts ITS scan to
    # `EXPLICIT_TRIGGERS` only (#1493), so every ref this loop can add is canonical-shaped by
    # construction — there is no weak-trigger case here to downgrade.
    for ref, phrase in _record_blocker_refs(goal_doc):
        if ref not in seen and ref in open_refs:
            out.append(_finding("blocked-by", ref, 1.0, "explicit",
                                [scrub(phrase)] if phrase else [], True))
            seen.add(ref)
    return out


def _referenced_blocker_refs(goal_doc, extra_text=""):
    """{#N, ...} -- every ref `_BLOCK_RE` matches in `goal_doc`'s own raw text + `extra_text`,
    REGARDLESS of whether N is still open. `_explicit_blockers` above is the narrower, OPEN-only
    view of this exact same scan; this is its superset (#1129).

    Exists so a caller can tell two cases apart that `_explicit_blockers` alone cannot: "this goal
    was never marked blocked-by anything" (this returns empty) vs. "it WAS, and every one of those
    refs has since closed" (this returns non-empty, while `_explicit_blockers` over the identical
    haystack now returns nothing, because it already discards a closed ref before a caller ever
    learns the ref existed). The auto-unpark sweep (`auto_unpark.py`) needs exactly that
    distinction: a parked goal with no recorded blocker reference at all was parked for some OTHER
    reason (duplicate / obsoleted-by / needs_decision / a human's own manual park) and must be left
    alone; one whose every referenced blocker has closed is the one case this sweep exists to fix.

    Same self-reference guard as `_explicit_blockers` (`n != goal_doc["ref"]`) — a goal cannot be
    its own blocker."""
    haystack = _blocker_haystack(goal_doc, extra_text)
    refs = {m.group(2) for m in _BLOCK_RE.finditer(haystack)
            if m.group(2) != goal_doc.get("ref") and not blocker_scan.is_cross_repo(haystack, m)}
    # #1487, and the superset relationship is preserved BY CONSTRUCTION: `_explicit_blockers` draws
    # its extra refs from the same `_record_blocker_refs` list and then narrows them to open ones,
    # so this stays a superset of that function's output over the identical goal doc.
    return refs | {ref for ref, _ in _record_blocker_refs(goal_doc)}


def _fetch_scrubbed_comments(sdlc_dir, config, goal_doc, run=None):
    """Scrubbed, RAW (uncapped) comment bodies for ONLY `goal_doc["ref"]` — the one issue actually
    being considered, right before `precheck()` would spend a real token (never corpus-wide;
    `mirror.py`'s own bulk fetch stays title+body only, unchanged — see its module docstring). A
    human commenting a dependency directly via the GitHub UI, bypassing `handoff.hand_off()`
    entirely, only ever leaves that marker on ONE issue at a time, so a bounded, single-issue fetch
    is enough to catch it.

    The SOLE `gh`-touching call this module makes for comments: both `_goal_comment_text` (below,
    the capped/joined text `_explicit_blockers` scans) and `cross_check`'s dismissal-marker scan
    (#830 finding 4, which needs the RAW text — see there) derive from this ONE fetch, so the
    feature adds zero new `gh` calls to precheck's hot path either way.

    Scrubbed identically to how title/body already are (`scrub.scrub()`, the same call
    `_build_corpus` already makes). Deliberately NOT capped here, unlike the single function this
    replaces — capping is a concern of each CONSUMER, not the fetch: `_explicit_blockers` wants the
    `mirror._EXCERPT_CHARS` budget (an issue body's own excerpt budget, reused for comments), but
    the dismissal-marker scan must NOT apply it — a marker sitting past that cap in a long dismissal
    narrative would be silently dropped (#830 finding 4).

    A no-op outside github mode: comments aren't a concept for local goal files at all, so this never
    attempts a network call it has no way to satisfy. FAIL-OPEN, independently of `cross_check()`'s
    own outer try/except: a comment-fetch failure degrades to "no extra blocker evidence from
    comments this run", never to an empty pack (`sources.fetch_comments` already fails open to `[]`
    on its own; this second try/except is defense in depth around the scrub step too)."""
    if not mirror.is_github_mode(config):
        return []
    try:
        # #650: a marker from a commenter outside the trust set is defused here, once, so neither the
        # dismissed-finding scan nor auto_unpark's keep-parked check can see it; the prose still counts.
        # #709: only a TRUSTED author's prose reaches the blocker scan at all. Defusing markers (the
        # #650 rule) left a stranger's "Blocked by #50" prose driving the default-on auto-unpark
        # sweep past the legacy confirmation gate; the whole text is dropped, with one stderr note.
        kept = []
        for c in sources.fetch_comments(config, goal_doc["ref"], run=run):
            if c.get("association") not in sources.TRUSTED_ASSOCIATIONS:
                print(f"sigma: ignoring a comment on {goal_doc['ref']} from "
                      f"{c.get('author') or 'an unknown author'} (association "
                      f"{c.get('association') or 'none'}): only OWNER/MEMBER/COLLABORATOR comments "
                      f"feed blocker detection (#709)", file=sys.stderr)
                continue
            kept.append(scrub(c["body"]))
        return kept
    except Exception:
        return []


def _filter_dismissal_comments(texts):
    """Drop any text that itself carries `DISMISS_MARKER` before a blocker-PHRASE scan (#830 finding
    3). A sigma-authored dismissal/bookkeeping comment is not fresh user-authored dependency
    information — but its free-text `reason` can innocently contain ordinary blocker vocabulary:
    dismissing #7 with "not a real dependency — this actually needs #9 to land first, unrelated to
    the contract" would otherwise make `_BLOCK_RE` match "needs #9" INSIDE the dismissal comment
    itself, spawning a brand-new confident `blocked-by` finding against #9 — `decide()` parks again,
    the exact re-park-forever bug #830 exists to fix, just relocated via a different field than the
    one that got a regression test.

    The marker itself is still scanned for separately and exactly — `cross_check` passes the
    UNFILTERED raw comments to `_dismissed_findings` — this only ever narrows the broader
    trigger-phrase corpus that produces NEW findings, never the marker read-back."""
    return [t for t in texts if not legacy.has_marker(t, DISMISS_MARKER)]


def _strip_offboard_prefixes(texts):
    """Strip Sigma's own fixed park/fail comment-template PREFIX (`sources.
    OFFBOARD_COMMENT_PREFIXES`) off the front of any text that carries it, before a blocker-PHRASE
    scan (#1186). The prefix text itself contains the word "needs" ("Parked by Sigma — needs
    human review: "; "Failed in the Sigma loop — needs a fix (not a decision): ") — itself a
    `_BLOCK_RE` TRIGGER WORD — so left in the haystack, any `#N` the CALLER's OWN `reason` happens
    to mention within the ~25 characters of the 40-char match window still open after the prefix
    reads as a phantom blocker, purely an artifact of Sigma's own fixed prose and nothing to do
    with a real dependency (worst case: a needs_decision park recording "PR #1234 is not approved
    yet" — closing #1234, one of the things a human might do in response, silently unparks the
    goal).

    Strips ONLY the known fixed prefix SPAN, at most once per text (`sources.park()`/`fail()` each
    write exactly one) — the REASON text that follows is left fully in the haystack, so a REAL
    blocker reference embedded in that reason (#1129's own mainstream case: backlog_check's
    automated park folds a genuine "blocked by #N" finding straight into the park reason) is still
    caught. A text carrying no known prefix is returned unchanged — this only ever narrows the
    corpus a fixed, code-known span already covers, never a general prose rewrite."""
    out = []
    for t in texts or []:
        for prefix in sources.OFFBOARD_COMMENT_PREFIXES:
            if t.startswith(prefix):
                t = t[len(prefix):]
                break
        out.append(t)
    return out


def _cap_join_excerpts(texts):
    """Cap each text at `mirror._EXCERPT_CHARS` (the same excerpt budget the mirror already gives an
    issue body) then join with a newline — the lexical-scan budget `_explicit_blockers` gets for its
    `extra_text`. NEVER reuse this for the dismissal-MARKER scan: a marker sitting past this cap in a
    long dismissal narrative would be silently dropped (#830 finding 4) — that scan reads the raw,
    uncapped texts directly instead (see `cross_check`)."""
    return "\n".join((t or "")[:mirror._EXCERPT_CHARS] for t in texts)


def _goal_comment_text(sdlc_dir, config, goal_doc, run=None):
    """The capped, filtered, joined comment text `_explicit_blockers` scans as its `extra_text` —
    kept as a standalone, directly-callable function (tests exercise it in isolation, and it is the
    smallest unit that answers "one issue's worth of blocker-scan-safe comment text"). `cross_check`
    itself does NOT call this: it also needs the RAW, unfiltered comment list for the dismissal-
    marker scan (#830 finding 4), and calling this as well would fetch comments a second time — so
    it calls `_fetch_scrubbed_comments` once and derives both this text (via
    `_filter_dismissal_comments` + `_strip_offboard_prefixes` + `_cap_join_excerpts`, the same three
    steps applied here) and the raw dismissal-scan text from that one result."""
    raw = _fetch_scrubbed_comments(sdlc_dir, config, goal_doc, run=run)
    return _cap_join_excerpts(_strip_offboard_prefixes(_filter_dismissal_comments(raw)))


# --- #830: a human's dismissal of one SPECIFIC finding, so a retry doesn't re-park identically -----
# `precheck` parks a CONFIDENT finding for a human to review; today the only undo is restoring
# `sdlc:goal` (drop `sdlc:parked` / re-add `sdlc:goal`) -- a pure label edit with no machine-readable
# trace -- so the NEXT cross_check recomputes the identical (kind, ref) finding from the identical
# corpus text and re-parks it, forever, until the underlying text happens to change.
#
# Convention chosen to match the CLOSEST existing "a human/loop already handled this" precedent in
# this codebase, `decompose_goal.DECOMPOSE_FILED_MARKER` (loop.py's `file`-mode idempotency check):
# an HTML-comment marker POSTED AS A COMMENT (never the body -- same reasoning `append_to_body`'s own
# docstring gives for why a body rewrite is a far more invasive ask than "leave a comment"), read back
# as a bare substring/regex scan over the goal's own comment text -- comments have no "first line is a
# declaration" convention to anchor to, exactly like DECOMPOSE_FILED_MARKER's own docstring reasons.
# NOT `blocker_promotion`'s explanatory-comment convention (sources.py `_write_blocker_promotion`'s
# plain-prose audit trail): that exists for a one-way-ratchet write with nothing to re-derive on a
# later run, so it has no machine-checked marker at all -- the opposite of what #830 needs, which is
# specifically to change what a LATER run computes. NOT sigma-scope's dedup-hit-as-a-question
# convention either: that is a synchronous, conversational brainstorm-time flow with a human on the
# other end of the turn; `precheck` runs unattended inside the autonomous loop with no chat turn to
# ask a question in.
#
# Reused, not fetched again: `raw_comments` (cross_check, below) is the SAME list already fetched for
# `_explicit_blockers`'s `comment_text` a few lines up (via `_fetch_scrubbed_comments`) -- one
# bounded, single-issue fetch serves both the explicit-blocker regex and the dismissal scan, so this
# feature adds zero new `gh` calls to precheck's hot path. The dismissal scan reads the RAW list
# directly (unfiltered, uncapped), not the derived `comment_text`: `comment_text` is capped at
# `mirror._EXCERPT_CHARS` and has any dismissal-marker-bearing comment filtered OUT (see
# `_filter_dismissal_comments`) -- exactly the text `_explicit_blockers` should scan for NEW blocker
# phrases, and exactly the text the marker scan must NOT use, since either transformation could hide
# the very marker this scan exists to find (#830 findings 3 and 4).
#
# GitHub mode ONLY (#830 finding 1): the read-back above is entirely comment-shaped, and local goal
# files have no comment thread at all -- `_fetch_scrubbed_comments` already no-ops there by
# construction (github-only, like `_goal_comment_text` before it). A parallel local-mode mechanism
# (e.g. reading LocalSource's own `.sdlc/journey/<stem>.md` journal, which `loop.py note` already
# writes to correctly in local mode -- only the READ side was ever missing) was considered and
# rejected for THIS fix: every non-explicit finding kind's `ref` in local mode is a full goal-file
# PATH (`_build_corpus`'s local branch), never the bare issue number `_DISMISS_RE`'s `ref=(\d+)`
# marker format requires, so safely matching a local marker back to the exact finding it names is a
# materially bigger change than "add a read" -- scoped out. `_local_dismissal_flag` below instead
# turns the restriction LOUD rather than silent: a journal entry carrying the marker is detected
# (cheap -- noticing it exists needs none of the ref-matching machinery above) and surfaced as an
# explicit `degraded` flag + park/advisory note (`decide`, further down), so a local-mode operator
# who dismisses a finding sees "not applied" on the very next park instead of silence. This mirrors
# the established precedent for this exact category of problem elsewhere in this codebase (`loop.py`
# `decompose_check`, `mode == "file"`: a local source lacks a comment timeline to read an idempotency
# marker back from, so it degrades to a visible, explained park rather than a silent skip).
#
# Keyed on (kind, ref) -- the SAME pair `_dedup_sort` already treats as a finding's own identity a
# few screens down -- deliberately NOT a hash of the full finding (score/evidence drift run to run as
# the corpus's idf recomputes, e.g. merely because an unrelated issue closed elsewhere; a hash keyed
# on those would silently stop matching the very next run and defeat the whole feature) and
# deliberately NOT scoped to any one finding SOURCE (explicit-blocker regex vs. TF-IDF similarity vs.
# a ledger hand-off): a human saying "the blocked-by-821 finding on this goal is wrong" means exactly
# that, whichever channel re-derives it.
#
# Downgrades `confident` only -- never removes the finding -- mirroring the #521 decomposition-
# `exempt` treatment above exactly: the module's own contract is "evidence, never a verdict" (module
# docstring, top of file), so a dismissed finding stays visible (advisory) rather than vanishing; if
# the human's dismissal was itself wrong, the evidence is still there to be reconsidered, just no
# longer park-confident.
DISMISS_MARKER = "sigma:dismissed-finding"
_DISMISS_RE = re.compile("(?:%s)" % "|".join(re.escape(s) for s in legacy.spellings(DISMISS_MARKER))
                         + r"\s+kind=([a-z-]+)\s+ref=(\d+)")


def dismiss_comment(kind, ref, reason=""):
    """The exact narrative + machine marker a human (or a script/agent acting on their behalf) posts
    as a COMMENT on a goal issue to dismiss ONE specific (kind, ref) finding -- mirrors
    `decompose_goal.filed_marker_comment`'s own narrative-then-marker shape, so the two "already
    handled" conventions in this repo read consistently. Posting it needs no new gh-mutating code
    here: the existing `loop.py note <sdlc_dir> <goal> "<this text>"` verb already does it.

    Deliberately never renders `kind` through `_KIND_PHRASE` (which spells "blocked-by" as the human
    phrase "blocked by #{ref}"): for kind="blocked-by" specifically, that literal phrase next to
    "#{ref}" is itself a `_BLOCK_RE` trigger -- rendering it here would make the DISMISSAL COMMENT
    look like a FRESH explicit blocker on the very finding it dismisses, so the next cross_check
    would re-derive a brand-new confident "blocked-by" finding from this comment and silently undo
    itself. The raw `kind` identifier (e.g. "blocked-by", hyphenated) is safe: `_BLOCK_RE`'s own
    trigger phrase requires a literal SPACE ("blocked by"), which a hyphen never supplies.

    `ref` tolerates a stray leading "#" (a human copying "#821" straight off the park comment,
    where `_KIND_PHRASE` renders it WITH the hash for display) -- every finding's own `ref` is
    stored bare (no "#") throughout this module; `#` is a display-only artifact, never part of the
    stored identity `_dismissed_findings` matches against."""
    ref = str(ref).lstrip("#")
    reason = (reason or "").strip()
    tail = f" — {reason}" if reason else ""
    return (f"Backlog cross-check: dismissed the {kind!r} finding against #{ref} — reviewed by a "
            f"human, not a real match{tail}. <!-- {DISMISS_MARKER} kind={kind} ref={ref} -->")


def _dismissed_findings(comment_text):
    """{(kind, ref)} pairs a human has already dismissed on THIS goal, per `DISMISS_MARKER` comments
    found in `comment_text` -- already scoped to one issue and scrubbed by `_fetch_scrubbed_comments`,
    so no separate secret-safety pass is needed here. `cross_check` passes the RAW (uncapped) joined
    comment text, never `_goal_comment_text`'s capped/filtered one (#830 finding 4): a marker sitting
    past `mirror._EXCERPT_CHARS` in a long dismissal narrative must not be silently dropped."""
    return {(m.group(1), m.group(2)) for m in _DISMISS_RE.finditer(comment_text or "")}


def _apply_dismissal(finding, dismissed):
    """`finding`, or a fresh copy downgraded to `confident: False` when a human already dismissed
    this EXACT (kind, ref) pair. Never mutates `finding` in place, matching every `_finding()` caller
    in this module handing back a fresh dict."""
    if (finding["kind"], finding["ref"]) not in dismissed:
        return finding
    return {**finding, "confident": False}


def _local_dismissal_flag(sdlc_dir, config, goal_doc):
    """True when local (non-GitHub) discovery mode's own journey log for THIS goal contains a
    `DISMISS_MARKER` -- i.e. a human/agent ran the documented dismissal remediation (`loop.py note`,
    which already dispatches correctly to `LocalSource.note()` and appends to
    `.sdlc/journey/<stem>.md` -- the WRITE side has never been broken) but local mode has no way to
    actually APPLY it (see the design-rationale comment above `DISMISS_MARKER`: local finding refs
    are full file paths, not the bare issue numbers the marker format requires). Detection needs none
    of that ref-matching machinery -- it only asks "does a marker exist at all" -- and lets
    `cross_check`/`decide` turn #830 finding 1's silent no-op into a LOUD, human-visible signal
    instead: a local-mode operator who dismisses a finding now sees an explicit "not applied" note on
    the very next park, rather than the finding just silently staying confident and re-parking
    forever with no error, no warning.

    GitHub mode never checks this (real dismissal already works there). Read-only, fail-open: a
    missing/unreadable journey file (nothing dismissed yet, or a goal ref that doesn't resolve to a
    real file) degrades to False, never an exception."""
    if mirror.is_github_mode(config):
        return False
    try:
        stem = pathlib.Path(goal_doc["ref"]).stem
        text = (pathlib.Path(sdlc_dir) / "journey" / (stem + ".md")).read_text(encoding="utf-8")
        return legacy.has_marker(text, DISMISS_MARKER)
    except Exception:
        return False


def _ledger_signals(sdlc_dir, goal_doc, idf, doc_by_ref, dup_th, now, exempt=False, config=None):
    """Team-wide signals from the shared ledger (read-only, no `enabled` needed): a SIMILAR goal a
    teammate is claiming right now (the race the exact-goal lease can't see), and an outstanding
    hand-off this goal FILED (a real, recorded blocker — it is waiting on the target it handed off,
    which is why the finding's `ref` is that target and not this goal).

    The similarity branch reads the claim lease through the SAME TTL every other consumer uses
    (#535): a claim the loop is already free to re-take cannot simultaneously be grounds for parking
    a paraphrase of it — that would be an internal contradiction, and an abandoned claim would park
    similar goals forever.

    `exempt` (#521): `goal_doc` is a decomposition child/meta-goal (see `cross_check`'s own
    precomputed `exempt`). Applies ONLY to the in-flight-elsewhere SIMILARITY branch below -- parallel
    sibling execution is exactly what decomposition creates, the same false-positive family as
    duplicate/obsoleted-by, just at this branch's lower (unconditional) bar. Never the recorded-
    hand-off branch further down: a hand-off is an explicit, human-recorded blocker, not a similarity
    guess, and applies to a marked child in full."""
    try:
        ledger = _load("ledger")
        entries = ledger.read_all(sdlc_dir)
    except Exception:
        return []
    if not entries:
        return []
    goal_ref = goal_doc["ref"]
    gvec = _vector(goal_doc["tokens"], idf)
    out = []
    try:
        # Inside the try on purpose: lease_ttl_seconds RAISES on a malformed `ttl_hours`, and that
        # must degrade THIS channel only — outside, cross_check's own catch-all would empty the pack.
        ttl = ledger.lease_ttl_seconds(config)
        for cg, _actor in ledger.open_claims(entries, now=now, ttl_seconds=ttl).items():
            cg = str(cg)
            d = doc_by_ref.get(cg)
            if cg == goal_ref or not d:
                continue
            s = _cosine(gvec, _vector(d["tokens"], idf))
            if s >= dup_th:                             # a paraphrase of this goal already in flight
                out.append(_finding("in-flight-elsewhere", cg, s, "ledger",
                                     _shared_terms(goal_doc, d, idf), not exempt))
    except Exception:
        pass
    try:
        for h in ledger.outstanding(entries):
            # The entry's `goal` is the side that RECORDED "I am blocked"; its `issue` is the target
            # it opened in the owner's area. Matching on `handoff_key` (issue-or-goal) parked the
            # TARGET against itself and let the genuinely-blocked filer walk straight into the work
            # it was waiting on (#532). `handoff_key` itself stays exactly as it is — pairing a
            # hand-off with its `ack` answers is a different question, and that conversation really
            # does live on the target issue.
            if str(h.get("goal")) != goal_ref:
                continue
            key = ledger.handoff_key(h)
            # A closed target is not a blocker — the same precision rule `_explicit_blockers()`
            # applies to its own `#N` refs. An `ack` is skippable (the recipient's normal path is
            # merge-and-close) and `outstanding()` settles only on ack resolved/declined, so without
            # this an unacked hand-off would outlive the issue it waited on and park the filer
            # forever. A target absent from the corpus still blocks: unknown is not finished.
            d = doc_by_ref.get(key)
            if d and d["completed"]:
                continue
            # `ref` is the TARGET that must land first — the same the-other-item meaning `ref`
            # carries in the duplicate/obsoleted-by findings, instead of the self-reference the
            # pre-fix branch emitted. In local mode `handoff_key` falls back to the goal itself:
            # there is no separate target artifact to point at, and the `[handoff]` term still says
            # what kind of block it is.
            out.append(_finding("blocked-by", key, 1.0, "ledger", ["handoff"], True))
    except Exception:
        pass
    return out


_EMBED_CACHE_REL = "state/embeddings.json"      # gitignored (.sdlc/state/), keyed by (embedder identity, content)
_EMBED_CACHE_MAX_ENTRIES = 2000                 # #624: ~14.5MB at the #584 review's own measured rate
                                                 # (~1.45MB / 200 docs, one identity) — evict-oldest past this


def _evict_oldest(cache, max_entries):
    """Trim `cache` (a plain dict, MUTATED in place) down to `max_entries` by dropping the OLDEST
    entries first — oldest by INSERTION order, since a cache hit re-reads an existing key without
    reassigning it, so a hit never moves that key later in iteration order. Deliberately the simpler
    FIFO reading of "evict-oldest", not LRU: a hit does not renew an entry's lifetime."""
    while len(cache) > max_entries:
        cache.pop(next(iter(cache)))


def _embed_enabled(config):
    # `embed.enabled` is the SOLE switch for the dense channel — `similarity` only picks the lexical
    # scorer (tfidf | bm25). One switch, one meaning; no second, undocumented path.
    return ((config.get("backlog_check") or {}).get("embed") or {}).get("enabled") is True


def _run_embedder(text, command, cwd=None):
    """Run the provider-agnostic embedder: `command` reads the text on stdin and prints a JSON array
    (the vector). Returns a list[float], or None on any failure (missing tool, bad JSON, non-zero exit,
    timeout) so the dense channel fails open to lexical-only."""
    if not shell_policy.repository_shell_commands_allowed(cwd or pathlib.Path.cwd()):
        return None
    try:
        proc = subprocess.run(command, shell=True, input=text or "", capture_output=True,
                              text=True, timeout=30, cwd=cwd)
        if proc.returncode != 0:
            return None
        v = json.loads(proc.stdout)
        return [float(x) for x in v] if isinstance(v, list) and v else None
    except Exception:
        return None


def _embedder_from_config(config, sdlc_dir=None):
    command = ((config.get("backlog_check") or {}).get("embed") or {}).get("command") or ""
    command = command.strip()
    root = pathlib.Path(sdlc_dir).resolve().parent if sdlc_dir else pathlib.Path.cwd()
    return (lambda text: _run_embedder(text, command, cwd=root)) if command else None


def _dense_channel(sdlc_dir, config, docs, goal_ref, embed_fn):
    """Build {ref: embedding} for the corpus, cached by (embedder identity, content) in gitignored .sdlc/state/ and
    computed INCREMENTALLY (only new/changed texts are embedded). Returns (vectors, weight) when the
    dense channel is usable, else (None, 0.0) — not enabled, no embedder configured, or the goal's own
    text couldn't be embedded (can't fuse without it). Fully fail-open; never raises.

    #624: the on-disk cache is BOUNDED two ways, both applied on write only (a read-only / fully-
    cached run never touches disk, so a same-identity cache HIT stays exactly as free as before —
    the #584 bar: repeated runs cost zero re-embeds).
    (1) Identity-scoped: the file stores ONE `identity` alongside its `vectors`; a write under a
        DIFFERENT identity (a provider/model swap) discards the previous generation WHOLESALE rather
        than appending alongside it. Those old entries would never be looked up again anyway — their
        hash key was computed under the old identity, so a same-text lookup under the new identity
        always misses — #542 already made a swap self-invalidating; this just stops the now-dead
        entries from sitting in the file forever. Trade-off, deliberate: the #584 review praised that
        swapping BACK to a previously-used identity came back cache-warm (no re-embed) purely as a
        side effect of the old flat, unbounded cache never dropping anything. That side effect is
        SACRIFICED here — swap-back now re-embeds like any other fresh identity — because letting the
        file grow without bound across every identity a project has ever tried is the worse default.
    (2) Capped: even ONE identity's own entries are evicted OLDEST-FIRST (by insertion order — a
        cache HIT never moves an existing key, so this is "oldest ever written", not
        least-recently-used) once they exceed `_EMBED_CACHE_MAX_ENTRIES`, so an actively-edited corpus
        (every edit mints a new hash key; the stale text's old entry is never looked up again either)
        cannot grow the file unboundedly within a single identity either. The cap must exceed the
        working corpus, though: once the corpus is bigger than `_EMBED_CACHE_MAX_ENTRIES`, the
        overflow (`corpus − cap` documents) re-embeds on every single `cross_check` instead of ever
        converging, so raise the cap rather than let an oversized corpus thrash forever."""
    if not _embed_enabled(config):
        return None, 0.0
    fn = embed_fn or _embedder_from_config(config, sdlc_dir)
    if fn is None:
        return None, 0.0                                     # enabled but no command -> lexical-only
    embed_cfg = (config.get("backlog_check") or {}).get("embed") or {}
    weight = _num(embed_cfg, "weight", 0.5)
    # #542: the cache key used to be sha256(text) ALONE -- nothing tied a cached vector to the
    # embedder that produced it, so swapping `embed.command` (a provider/model change) while the
    # corpus text stayed the same silently served the OLD embedder's vectors into the NEW
    # embedder's vector space (a meaningless cross-space cosine). Folding the command string into
    # the key makes a swap self-invalidating: every doc misses the cache once under the new key and
    # gets genuinely re-embedded, no separate "clear the cache" step required. `embed_fn` (test/CLI
    # injection) has no command string of its own to identify it BY DESIGN -- the config's own
    # `embed.command` is the caller-visible identity either way, exactly the value #542's fix sketch
    # names ("sha of embed.command"), so callers that inject a function still get real invalidation
    # as long as they set `command` to something meaningful for their embedder.
    identity = (embed_cfg.get("command") or "").strip()
    try:
        cache_path = pathlib.Path(sdlc_dir) / _EMBED_CACHE_REL
        try:
            raw_cache = json.loads(cache_path.read_text(encoding="utf-8"))
            if (isinstance(raw_cache, dict) and raw_cache.get("identity") == identity
                    and isinstance(raw_cache.get("vectors"), dict)):
                cache = raw_cache["vectors"]
            else:
                # #624: no file, a pre-#624 flat-shaped file, or (the common case) a DIFFERENT
                # identity than last write -- either way nothing in it is reachable under the
                # CURRENT identity's keys, so start clean rather than carry dead entries forward.
                cache = {}
        except Exception:
            cache = {}
        vecs, dirty = {}, False
        for d in docs:
            key = hashlib.sha256(
                (identity + "\x00" + (d.get("raw") or "")).encode("utf-8")).hexdigest()[:16]
            v = cache.get(key)
            if v is None:
                v = fn(d.get("raw") or "")
                if v is None:
                    continue                                 # skip a doc that won't embed; keep the rest
                cache[key], dirty = v, True
            vecs[d["ref"]] = v
        if dirty:
            _evict_oldest(cache, _EMBED_CACHE_MAX_ENTRIES)
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                # NOT sort_keys: alphabetizing would destroy the insertion order evict-oldest relies
                # on across runs (this process's dict order survives the round-trip only because
                # json.dump/load both preserve key order; sorting on write would reset it to
                # alphabetical on the very next read).
                cache_path.write_text(
                    json.dumps({"identity": identity, "vectors": cache}), encoding="utf-8")
            except Exception:
                pass                                         # a read-only .sdlc must not break the check
        return (vecs, weight) if goal_ref in vecs else (None, 0.0)
    except Exception:
        return None, 0.0


def cross_check(sdlc_dir, goal, config=None, run=None, now=None, velocity_measure=None, embed_fn=None):
    """The whole stage-1 pass. FAIL-OPEN: any error → an empty pack + degraded['error']."""
    goal_ref = str(goal)
    try:
        config = config if config is not None else _load_config(sdlc_dir)
        cfg = config.get("backlog_check") or {}
        dup_th = _num(cfg, "dup_threshold", _DEFAULTS["dup_threshold"])
        obs_th = _num(cfg, "obsolete_threshold", _DEFAULTS["obsolete_threshold"])
        park_th = _num(cfg, "park_threshold", _DEFAULTS["park_threshold"])
        top_k = _num(cfg, "top_k", _DEFAULTS["top_k"])

        docs, degraded = _build_corpus(sdlc_dir, config)
        goal_doc = next((d for d in docs if d["ref"] == goal_ref), None)
        if goal_doc is None:
            return _pack(goal_ref, [], degraded + ["goal_not_in_corpus"])

        idf = _idf(docs)
        sim_mode = (config.get("backlog_check") or {}).get("similarity", "tfidf")
        avgdl = (sum(sum(d["tokens"].values()) for d in docs) / len(docs)) if docs else 0.0
        gvec = _lex_weight(goal_doc["tokens"], idf, sim_mode, avgdl)
        gterms = set(goal_doc["tokens"])
        # #521: a first-line `sigma:decomposed-from=`/`sigma:decompose-of=` marker means this
        # goal IS a deliberately authored decomposition child/meta-goal (constants shared with loop.py's
        # decompose_check guard via goal_size.py, so the two checks can't drift apart). The duplicate
        # path's `_earlier()` rule always parks the NEWER of a similar pair, and a freshly created child
        # is always the newest -- so a child similar to its own parent/siblings would always be the one
        # parked; the obsoleted-by path has no `_earlier()` gate but compares against closed work a
        # fresh child can also spuriously resemble; the in-flight-elsewhere ledger-similarity branch
        # shares the same false-positive family at a lower bar (parallel sibling execution is exactly
        # what decomposition creates). Exemption only downgrades `confident` -- the finding is still
        # EMITTED (module contract: evidence, never a verdict) -- and NEVER reaches `_explicit_blockers`
        # or the recorded-hand-off branch: an explicit blocker or a recorded hand-off still fully
        # applies to a marked child.
        #
        # "First line" here means the first NON-BLANK line of the (already-scrubbed) body excerpt.
        # local-mode bodies do NOT always start with a blank line (#544: `_build_corpus` strips the
        # frontmatter fence itself via `frontmatter.strip()`, not an extra artifact newline) -- but a
        # goal file AUTHORED with a blank line right after the closing fence (a common, legitimate
        # markdown style) still produces one, since that blank line is real content the fence-stripping
        # never touches. `lstrip()` before `splitlines()` therefore stays mandatory for that still-live
        # case -- a deliberate divergence from loop.py's own guard, which reads the raw, unstripped body
        # straight from the source and has no such leading blank line to strip. CRLF-tolerant via
        # `splitlines()`, same as loop.py's guard. `.get("body")` because some callers (tests) hand-build
        # a partial doc.
        first_line = (goal_doc.get("body") or "").lstrip().splitlines()[:1]
        first_line = first_line[0] if first_line else ""
        exempt = (legacy.has_marker(first_line, goal_size.DECOMPOSED_FROM_MARKER)
                  or legacy.has_marker(first_line, goal_size.DECOMPOSE_OF_MARKER))
        window = _closed_window_days(config, run=run, velocity_measure=velocity_measure)

        # Opt-in dense/embedding channel (Q5): catches paraphrases that share NO lexical term. Fail-open
        # to lexical-only. When it's off (the default), `dense` is None and every line below reduces to
        # the exact pre-0.9.23 lexical path — byte-identical.
        dense, dweight = _dense_channel(sdlc_dir, config, docs, goal_ref, embed_fn)
        if dense is None and _embed_enabled(config):
            degraded.append("no_embedder")
        gden = dense.get(goal_ref) if dense else None

        scored = []
        for d in docs:
            if d["ref"] == goal_ref:
                continue
            shares = bool(gterms & set(d["tokens"]))
            if not shares and dense is None:
                continue                                # lexical-only: candidate-gen = docs sharing ≥1 term
            lex = _cosine(gvec, _lex_weight(d["tokens"], idf, sim_mode, avgdl)) if shares else 0.0
            den = dweight * _list_cosine(gden, dense.get(d["ref"])) if dense else 0.0
            s = max(lex, den)                           # fuse: the stronger of the two channels
            if s > 0:
                scored.append((s, d))
        scored.sort(key=lambda x: (-x[0], _ref_key(x[1]["ref"])))   # earliest-ref tiebreak, not lexical

        findings = []
        for s, d in scored[:top_k]:
            ev = _shared_terms(goal_doc, d, idf)
            if d["completed"] and s >= obs_th and _within_window(d, window, now):
                findings.append(_finding("obsoleted-by", d["ref"], s, d["source"], ev,
                                          s >= park_th and not exempt))
            elif d["open"] and s >= dup_th:
                # Of a duplicate PAIR, park only the LATER goal: a `duplicate` is park-confident only when
                # the matched open issue is EARLIER in pick order (lower number / earlier filename). Else
                # both #1↔#2 would park each other and neither would ever be worked — the first-filed
                # survives and is worked; later duplicates park against it. (Reported either way.)
                confident = s >= park_th and _earlier(d["ref"], goal_ref) and not exempt
                findings.append(_finding("duplicate", d["ref"], s, d["source"], ev, confident))

        # #830 findings 3/4 (PR #1109 review): fetch comments ONCE, then derive TWO different views --
        # _explicit_blockers wants the excerpt-budget-capped text with any dismissal/bookkeeping
        # comment excluded (a dismissal's own free-text `reason` can otherwise read as a fresh
        # blocker mention and spawn a false finding) and Sigma's own park/fail boilerplate
        # PREFIX stripped (#1186: that fixed prose contains "needs", itself a `_BLOCK_RE` trigger,
        # so a #N the goal's OWN prior park reason happens to mention nearby would otherwise spawn a
        # fresh, unrelated blocked-by finding on the very next cross_check), while the
        # dismissal-marker scan needs the RAW, unfiltered, UNCAPPED text (a marker past the excerpt
        # cap in a long narrative must not be silently dropped). See `_fetch_scrubbed_comments` /
        # `_filter_dismissal_comments` / `_strip_offboard_prefixes` / `_cap_join_excerpts` for the
        # full rationale.
        raw_comments = _fetch_scrubbed_comments(sdlc_dir, config, goal_doc, run=run)
        comment_text = _cap_join_excerpts(_strip_offboard_prefixes(_filter_dismissal_comments(raw_comments)))
        findings += _explicit_blockers(goal_doc, docs, comment_text)
        doc_by_ref = {d["ref"]: d for d in docs}
        findings += _ledger_signals(sdlc_dir, goal_doc, idf, doc_by_ref, dup_th, now, exempt,
                                     config=config)
        # #830: a human already reviewed and dismissed some of these EXACT findings -- don't re-park
        # on them again. Must run BEFORE _dedup_sort, whose own sort key reads `confident`.
        dismissed = _dismissed_findings("\n".join(raw_comments))
        findings = [_apply_dismissal(f, dismissed) for f in findings]
        # #830 finding 1: local mode can't apply a dismissal (see `_local_dismissal_flag`), but a
        # human/agent may still have posted one per SKILL.md's documented remediation -- surface that
        # LOUDLY rather than silently proceeding as if nothing had been attempted.
        if _local_dismissal_flag(sdlc_dir, config, goal_doc):
            degraded.append("dismissal_local_mode_unsupported")
        return _pack(goal_ref, _dedup_sort(findings), degraded)
    except Exception:
        return _pack(goal_ref, [], ["error"])


def _dedup_sort(findings):
    best = {}
    for f in findings:
        key = (f["kind"], f["ref"])
        if key not in best or f["score"] > best[key]["score"]:
            best[key] = f
    # confident first, then by score desc, then a stable (kind, ref) tiebreak — fully deterministic
    return sorted(best.values(), key=lambda f: (not f["confident"], -f["score"], f["kind"], f["ref"]))


def _pack(goal_ref, findings, degraded):
    return {"schema": SCHEMA, "goal": str(goal_ref), "findings": findings,
            "degraded": sorted(set(degraded))}


_KIND_PHRASE = {"duplicate": "duplicate of #{ref}", "obsoleted-by": "obsoleted by #{ref}",
                "blocked-by": "blocked by #{ref}", "in-flight-elsewhere": "in flight elsewhere: #{ref}"}


def _stem(ref):
    return pathlib.Path(str(ref)).name          # github ref '42' -> '42'; local path -> filename (win-safe)


def _ref_key(ref):
    """Deterministic ref ordering matching next_pending (numeric issue numbers, THEN filename). Used as
    the candidate-truncation tiebreak so the top_k window keeps the EARLIEST issues, not the lexically
    smallest — else in a duplicate cluster larger than top_k, #10..#17 could crowd #2 out of #3's window
    and #3 would find no earlier match and wrongly survive (a second survivor)."""
    try:
        return (0, int(ref), "")
    except (TypeError, ValueError):
        return (1, 0, _stem(ref))


def _earlier(a, b):
    """Pick order (mirrors next_pending's oldest-first): lower issue number, else earlier filename. Used
    so only the LATER goal of a duplicate pair parks — the first-filed survives and gets worked."""
    return _ref_key(a) < _ref_key(b)


def _phrase(f):
    base = _KIND_PHRASE.get(f["kind"], f["kind"] + " #{ref}").format(ref=_stem(f["ref"]))
    ev = ", ".join(f.get("evidence") or [])
    return f"{base} ({f['score']}" + (f"; shared: {ev}" if ev else "") + ")"


def summary(findings, limit=3):
    """A compact, secret-safe one-liner over the top findings (refs + scores + already-scrubbed shared
    terms — never a title/body). Used verbatim in the park comment / advisory note."""
    return "; ".join(_phrase(f) for f in findings[:limit])


def decide(pack, config):
    """Pure: turn a pack into the loop hook's action. `backlog_check.action` (default 'park'): a
    CONFIDENT finding parks-with-proof; 'flag' (or only weak findings) annotates and proceeds. Returns
    {action: 'park'|'proceed', reason: <park-comment text>, note: <advisory text>}. Deterministic —
    findings are already sorted confident-first, score desc."""
    pack = pack or {}
    findings = pack.get("findings") or []
    degraded = pack.get("degraded") or []
    if "goal_not_in_corpus" in degraded:
        # The goal itself was absent from the corpus, so NO finding could be computed for it. That
        # is not the same answer as "checked, found nothing", and a bare proceed reported it as if
        # it were. Still PROCEED — there is no evidence to park on, in either action mode — but say
        # so, in the advisory channel the loop already surfaces.
        return {"action": "proceed", "reason": "",
                "note": f"backlog cross-check: skipped — #{pack.get('goal')} is not in the backlog "
                        "mirror, so nothing was compared; refresh the mirror if this repeats (advisory)"}
    if not findings:
        return {"action": "proceed", "reason": "", "note": ""}
    action = (config.get("backlog_check") or {}).get("action", "park")
    line = "backlog cross-check: " + summary(findings)
    if "dismissal_local_mode_unsupported" in degraded:
        # #830 finding 1: make the GitHub-only scope LOUD, not silent -- a local-mode operator who
        # posted a dismissal note sees it called out right here, on the very park/advisory line
        # they're already reading, instead of the finding just silently staying confident forever.
        line += (" [a dismissal note exists in this goal's journey log, but dismissal is GitHub-mode"
                 " only and was NOT applied — see SKILL.md]")
    if action == "park" and any(f.get("confident") for f in findings):
        return {"action": "park", "reason": line, "note": ""}
    return {"action": "proceed", "reason": "", "note": line + " (advisory)"}


USAGE = ("usage: backlog_check.py <sdlc_dir> <goal> | "
         "backlog_check.py dismiss-text <kind> <ref> [reason...]")


def main(argv):
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    if len(argv) >= 2 and argv[1] == "dismiss-text":
        # #830: renders the exact marker comment text for a human/agent to post via the existing
        # `loop.py note <sdlc_dir> <goal> "<text>"` verb -- a pure, read-only helper; this file never
        # shells out to `gh` on its own, and that stays true here (the actual gh mutation is `note`'s
        # job, already tested there).
        #
        # #830 finding 5 (PR #1109 review): checked on the KEYWORD alone now, not `len(argv) >= 4` --
        # the old combined condition let a call with too few args (e.g. missing `ref`) fall straight
        # through to the generic `<sdlc_dir> <goal>` branch below and get silently misinterpreted as
        # a normal cross_check() call instead of failing with a usage error.
        if len(argv) < 4:
            print("usage: backlog_check.py dismiss-text <kind> <ref> [reason...]",
                  file=__import__("sys").stderr)
            return 2
        print(dismiss_comment(argv[2], argv[3], " ".join(argv[4:])))
        return 0
    if len(argv) < 3:
        print(USAGE, file=__import__("sys").stderr)
        return 2
    print(json.dumps(cross_check(argv[1], argv[2]), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv))
