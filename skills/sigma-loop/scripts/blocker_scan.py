"""Dependency-marker vocabulary — the ONE definition of what "this issue is blocked by #N"
looks like in text, plus the fixed spans that must never be scanned for it (#1487).

Extracted from `backlog_check.py` so that BOTH ends of the pipeline can apply the identical scan:

  - `backlog_check._explicit_blockers` / `_referenced_blocker_refs` scan a goal's corpus text at
    CHECK time (plus its comment text, which never rides the mirror at all);
  - `mirror.normalize_issue` scans an issue's FULL body once at FETCH time and stores the refs it
    finds on the record, because the mirror keeps only a 500-character body EXCERPT and
    `compile_plan` writes its `**Blocked by:** #N` marker at the END of a body — so any issue
    longer than the cap had its dependency silently truncated away and unenforced.

The two scans share the character class, the proximity window and the capture groups, but NOT the
trigger set: the fetch-time one is restricted to `EXPLICIT_TRIGGERS` because it reads ~10x the text.
See `TRIGGERS` below — that asymmetry is the point of this module, not an accident in it.

`mirror.py` is the LOWER module (`backlog_check` imports it, never the reverse), so the vocabulary
could not live in either of them without inverting that edge or duplicating the regex. It lives
here, and both import it. `backlog_check` re-exports `_BLOCK_RE`, `UNPARK_QA_START`,
`UNPARK_QA_END` and `_strip_unpark_qa` as its own module attributes, so every existing reader —
`doctor.py` (`_load_loop_script("backlog_check")._BLOCK_RE`), `triage.py` and `sources.py` (which
both read it LIVE off the module attribute by contract), `unpark.py` (`backlog_check.
UNPARK_QA_START`) — keeps working verbatim against an equivalent pattern. (Usually the IDENTICAL
object, but only because `re.compile` caches: `_load()` never registers in `sys.modules`, so each
call re-executes this file, and `re.purge()` between two loads yields two distinct compiled objects
with equal patterns. Nothing asserts identity, and nothing should.)

ONE consequence of that, worth knowing before writing a test: monkeypatching
`backlog_check._BLOCK_RE` does NOT reach `extract_refs`, which resolves the pattern through this
module's own globals. `sources.py` and `triage.py` document "read it live so a patch propagates" as
a contract; after #1487 that holds for the check-time scanners only. The source-level pin
(`test_the_block_vocabulary_is_declared_only_in_blocker_scan`) is what guards the fetch-time side.

Pure, with ONE sibling dependency: `legacy.py` (#239), loaded once at import so an unpark block
written under the plugin's previous name strips too. `legacy.py` is itself stdlib-only and does no
I/O at import, so nothing here does I/O either. It sits under `mirror.normalize_issue`, which runs
once per issue on a board-sized fetch; the per-call cost is still a bounded number of `find`s.
"""
import importlib.util
import pathlib
import re


def _load_legacy():
    spec = importlib.util.spec_from_file_location(
        "legacy", pathlib.Path(__file__).resolve().parent / "legacy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: #239: an unpark Q&A block written under the plugin's previous name is stripped the same way.
legacy = _load_legacy()


# blocking phrasing immediately followed by a #N reference — an EXPLICIT, high-precision blocker edge
#: #1393: the character class excludes CLAUSE PUNCTUATION (`, ; . ! ?`), not just newlines and `#`.
#:
#: A trigger phrase and a `#N` on opposite sides of a clause boundary are almost never a dependency:
#: the ref is not the trigger's object, it is a separate remark. Measured against the marker and
#: prose forms this repo actually writes -- every real one survives, two whole classes of false
#: positive stop matching:
#:
#:     **Blocked by:** #123          match -> match     (the canonical marker every writer emits)
#:     Blocked by #99                match -> match
#:     this depends on #7            match -> match
#:     requires the new parser in #7 match -> match
#:     waiting on the sign-off, see #1234    match -> NO MATCH
#:     we need this after the release. see #77   match -> NO MATCH
#:
#: This REDUCES the false-positive rate; it does not eliminate it, and that is not a solvable
#: problem with a regex over prose. `after #40 lands` and `...this actually needs #9 to land first`
#: still match, because nothing about their SHAPE distinguishes them from a real dependency -- only
#: their meaning does. Two deliberate escapes exist for the residue and are documented for adopters:
#: a dismissal comment (`backlog_check.py dismiss-text`) retires one specific (kind, ref) match, and
#: `auto_unpark.KEEP_PARKED_MARKER` opts a goal out of the sweep entirely. See
#: `test_block_re_boundary_table` for the pinned behaviour of every case above.
#:
#: The cost is a real false NEGATIVE: `blocked by the parser, see #7` no longer registers. That is
#: the right trade -- a missed edge costs one out-of-order pick, while a phantom edge parks a goal
#: that nothing will unpark, and the reliable form (`Blocked by: #N`, nothing between) is what every
#: Sigma writer already emits.
#:
#: THE TWO SCANS ARE NOT THE SAME WIDTH, and that is deliberate (#1487 review). Everything above
#: describes a vocabulary CALIBRATED AGAINST A 500-CHARACTER EXCERPT -- that is the only text the
#: check-time scanners in `backlog_check.py` were ever pointed at, and the documented residue was
#: tolerable at that size. `mirror.normalize_issue` now scans a WHOLE BODY, and on the repo this was
#: measured against 395 of 400 issues are longer than the cap, so the same vocabulary over the same
#: board covers roughly ten times the text. Measured at that width the residue stops being
#: theoretical: 22 new confident edges, 21 genuine `blocked by` markers and one phantom off `after`
#: --
#:
#:     "...until a real user hit it after Phase 2 (#1396) went live."   -> blocked-by #1396, score 1.0
#:
#: -- narrative prose about a past event, emitted `confident`. A confident `blocked-by` finding is
#: not advisory: `loop.py`'s `_resolve_blockers_for_park` hands it to `blockers.resolve`, which
#: WRITES TO THE REFERENCED ISSUE (a `sdlc:proposed` -> `sdlc:goal` label swap, a board move and a
#: comment), and `auto_unpark.compute_blocking_actions` then stamps `sdlc:blocking` on it. One
#: sentence of English past character 500 would stall a real goal AND mutate a third, unrelated
#: issue, unattended.
#:
#: So the FETCH-TIME scan uses `EXPLICIT_TRIGGERS` -- the three phrases whose shape carries the
#: dependency and not just the vocabulary. On that same corpus the narrowing keeps 21 of 21 genuine
#: edges and drops the phantom, and it costs nothing this repo generates: `compile_plan`,
#: `handoff` and `triage` all write the same `**Blocked by:** #N` marker. The CHECK-TIME scan keeps
#: the full `TRIGGERS` set exactly as #1393 left it -- MATCHING moves for nobody, a weak trigger
#: inside the excerpt is found today and is still found -- but #1497 found the identical live-write
#: exposure one level up: `backlog_check._explicit_blockers` scans the excerpt with this same full
#: vocabulary and, before #1497, marked EVERY match `confident` regardless of phrase, so the same
#: legacy confirmation label text or a quoted "needs human review" park comment reaching
#: `_resolve_blockers_for_park` could mutate a third issue from inside the 500-character excerpt,
#: no truncation required. `_explicit_blockers` now sets `confident` only for an `EXPLICIT_TRIGGERS`
#: phrase, so presence is unchanged (this docstring's claim still holds) but a weak-trigger match is
#: advisory, never acted on.
TRIGGERS = ("blocked by", "depends on", "depends upon", "needs", "after", "requires", "waiting on")

#: The high-precision subset, for a scan over a whole body. A PREFIX of `TRIGGERS`, not a separate
#: list, so the narrow pattern can only ever match where the full one also matches.
EXPLICIT_TRIGGERS = TRIGGERS[:3]


def _compile(triggers):
    """Both patterns from ONE construction: same clause-punctuation class, same 40-character
    proximity window, same two capture groups (phrase, ref) -- only the trigger alternation
    differs. A second hand-typed literal would have let the window or the character class drift
    between the two scan sites, which is the exact failure `blocker_scan.py` exists to prevent."""
    return re.compile(r"(?i)\b(" + "|".join(triggers) + r")\b[^\n#,;.!?]{0,40}?#(\d+)")


def is_cross_repo(text, match):
    """#709: True when the `#N` this match captured is really `owner/repo#N` (or `repo#N`): another
    repository's issue, which must never collapse to the LOCAL #N and become a blocker edge. The
    shared pattern stays byte-identical (tools/leak_refs.py and mirror.py pin it); the consumers that
    ACT on a ref (`backlog_check`, `extract_refs`) skip a cross-repo hit with this."""
    i = match.start(2) - 2          # the character just before `#`
    return i >= 0 and (text[i].isalnum() or text[i] in "_/-.")


_BLOCK_RE = _compile(TRIGGERS)                    # check time: the excerpt + comment text
_EXPLICIT_BLOCK_RE = _compile(EXPLICIT_TRIGGERS)  # fetch time: a whole issue body


#: #1392: the fenced span `/sigma-unpark` writes into an issue BODY when a human answers the
#: questions that got it unparked. The answers are plain English, and `_BLOCK_RE`'s trigger set is
#: ordinary English -- "needs", "after", "requires", "waiting on", "depends on" -- within 40
#: characters of a `#N`. A completely reasonable answer like "waiting on the design sign-off, see
#: #1234" would therefore plant a PHANTOM blocker in the body of the very goal that was just
#: unblocked, and `precheck` would re-park it on the very next pick: the goal gets unparked, a human
#: spends real effort answering, work starts, and the recorded answers park it again.
#:
#: This is the same trap #1186 found in Sigma's OWN park boilerplate (`PARK_COMMENT_PREFIX`
#: contains "needs"), and the fix has the same shape: strip a FIXED, code-known SPAN before any
#: `_BLOCK_RE` scan sees the text. Only the marked span -- a genuine "Blocked by #99" written
#: anywhere else in the body is still caught exactly as before.
UNPARK_QA_START = "<!-- sigma:unpark-qa:start -->"
UNPARK_QA_END = "<!-- sigma:unpark-qa:end -->"


def strip_unpark_qa(text):
    """Remove every `/sigma-unpark` Q&A span from `text`. An UNTERMINATED start marker (a truncated
    write, an interrupted session) strips to the end of the text rather than being ignored: the
    whole point is that this content must never reach `_BLOCK_RE`, and a half-written block is
    exactly as full of ordinary English as a complete one.

    #1498: this used to be one `START.*?END` regex (DOTALL, non-greedy) run through `.sub`. Against
    a body containing many unterminated start markers -- START repeated with no END anywhere -- the
    backtracking engine retries the non-greedy scan from EVERY start position, each retry running to
    the end of the text before failing: O(k*n) for k starts over n characters, measured at 220ms on
    a 64KB adversarial body against under 5ms for every other shape of the same size. `text.find`
    pairs each START with the nearest END that follows it in a single left-to-right pass with no
    backtracking, and stops at the first START with no END after it -- so an unterminated start
    costs one linear scan to discover, not one per occurrence. Behaviour is unchanged: pairs are
    still resolved nearest-END-wins (matching the old non-greedy `.*?`), and hitting an unterminated
    START still truncates everything from there to the end of the text, discarding any further
    START/END pairs that happen to follow it -- exactly what the old sub-then-partition sequence did."""
    text = text or ""
    out, pos = [], 0
    while True:
        # #239: either spelling of each marker, nearest first -- `legacy.find_marker` is the same
        # single left-to-right `find`, so the #1498 linear bound above still holds (two finds per step).
        start, start_marker = legacy.find_marker(text, UNPARK_QA_START, pos)
        if start == -1:
            out.append(text[pos:])
            break
        end, end_marker = legacy.find_marker(text, UNPARK_QA_END, start + len(start_marker))
        if end == -1:
            out.append(text[pos:start])
            break
        out.append(text[pos:start])
        pos = end + len(end_marker)
    return "".join(out)


def read_refs(record, self_ref=None):
    """`[(ref, phrase), ...]` from a record's STORED `blocker_refs` field — the read half of
    `extract_refs` above, and the only sanctioned way to get the refs back off a mirror record.

    It lives HERE, beside the writer, for the same reason `_BLOCK_RE` does: two readers now consume
    this field at two different moments — `backlog_check._record_blocker_refs` at CHECK time and
    `loop._pick_dependency_hold` at PICK time (#1499) — and a second hand-written walk of it would
    be free to disagree about the shapes below without anything noticing. The vocabulary and the
    record shape are one contract; splitting them across modules is what #1487 factored this file
    out to stop.

    DEGRADES, never raises, on every shape a real `.sdlc/state/board-mirror.ndjson` can hold:

      - the field ABSENT — a mirror written by a pre-#1487 install, a local-mode goal doc, or a
        hand-built test doc: an empty list, i.e. exactly the pre-#1487 behaviour;
      - a bare string / int list rather than the `{"phrase","ref"}` records `extract_refs` writes —
        the ref survives, the evidence phrase is simply empty;
      - a non-list (INCLUDING a bare `str`, which would otherwise iterate per CHARACTER and read
        "blocked by #7" as ref "7"), a non-digit ref, or `record` itself not being a dict — dropped.

    `self_ref` drops a self-reference, the same guard `extract_refs` applies at write time. It is
    re-applied on the way out rather than trusted from the record, because a record can also arrive
    from a hand-built or older mirror that never applied it."""
    field = record.get("blocker_refs") if isinstance(record, dict) else None
    if not isinstance(field, (list, tuple)):
        return []
    out = []
    for item in field:
        if isinstance(item, dict):
            ref, phrase = str(item.get("ref") or ""), str(item.get("phrase") or "")
        elif isinstance(item, (str, int)):
            ref, phrase = str(item), ""
        else:
            continue
        if ref.isdigit() and not (self_ref is not None and ref == str(self_ref)):
            out.append((ref, phrase))
    return out


def extract_refs(text, self_ref=None):
    """Every EXPLICIT blocker reference in `text`, as an ORDERED, ref-deduplicated list of
    `{"phrase": <lowercased trigger phrase>, "ref": "<N>"}` — the record shape `mirror.py` stores
    and `backlog_check._record_blocker_refs` reads back.

    `_EXPLICIT_BLOCK_RE`, NOT `_BLOCK_RE`. This is the whole-body scan, and the weak half of the
    vocabulary ("needs", "after", "requires", "waiting on") was calibrated against a 500-character
    excerpt — see `TRIGGERS` above for the measurement and for what a phantom edge does to a third,
    unrelated issue. The check-time scanners keep the full set; only this one narrows.

    Applies `strip_unpark_qa` first, exactly as `backlog_check._blocker_haystack` does: an
    `/sigma-unpark` Q&A span is ordinary English and would otherwise plant the same phantom blocker
    (#1392) here that it plants there — and a scan of the FULL body reaches spans the truncated
    excerpt never even contained.

    SECRET-SAFE by construction, which is why the mirror can store the result of a full-body scan
    when it deliberately refuses to store the body itself: the only text that survives is a decimal
    issue number and one phrase from the closed trigger set. No surrounding body text is ever
    captured or persisted. (The number itself is now sourced from an unbounded region — a bare
    `#4815162342` past the cap is stored as that ref — but `scrub()` runs over the FULL body before
    this ever sees it, so every credential shape is already redacted.)

    `self_ref` (optional): drop a self-reference, the same guard both scanners in `backlog_check`
    apply — a goal can never be its own blocker.
    """
    out, seen = [], set()
    for m in _EXPLICIT_BLOCK_RE.finditer(strip_unpark_qa(text)):
        ref = m.group(2)
        if ref in seen or (self_ref is not None and ref == str(self_ref)):
            continue
        seen.add(ref)
        out.append({"phrase": m.group(1).lower(), "ref": ref})
    return out


def closed_state(state_str, state_reason):
    """Is a `Blocked by:` reference's live GitHub state actually RESOLVED (#2532)?

    MERGED resolves -- that is what actually resolves whatever a pull request was blocking.
    CLOSED-without-merge on a PR does NOT -- the referenced change never landed, so it must not
    read as resolved just because it shares the same bare "CLOSED" state a genuinely-closed ISSUE
    also reports. An ordinary closed issue still resolves, as it always has.

    `state_reason` is the free, single-call signal that tells the two "CLOSED" cases apart:
    populated ("COMPLETED"/"NOT_PLANNED") whenever an ISSUE is actually closed, but EMPTY for a PR
    in any state (open, closed, or merged) -- confirmed empirically against this repo's own real
    closed issues and closed/merged PRs (`auto_unpark.py`'s own `_ref_is_open`, the pattern this
    function extracts and both `promote.py` and `blockers.py` now share rather than each
    re-implementing their own copy of the same rule, #2532).

    A bare `state == "CLOSED"` (no `state_reason` awareness at all) gets BOTH failure modes wrong:
    it reads a MERGED pr as still open (never resolves) and a CLOSED-without-merge one as resolved
    (resolves immediately) -- both backwards."""
    if state_str == "MERGED":
        return True
    if state_str == "CLOSED":
        return bool(state_reason)
    return False
