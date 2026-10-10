"""goal_decompose: the deterministic `goal_size` classifier + `loop.py`'s `decompose-check` verb.
`log`/`park` (#519) classify + (optionally) park a flagged goal; `file` (#522) additionally files
one idempotency-guarded "Decompose #N" meta-issue before parking -- see the "#522: file mode (real
filing)" section below for that behavior specifically. Opt-in (`goal_decompose.enabled`), zero LLM,
fail-open, off by default. Hermetic, $0.

No conftest.py in this repo (tests/test_import_boundary.py's own docstring records why) — every
helper below is copied in, not imported, mirroring test_backlog_precheck.py / test_sources.py."""
import json
import pathlib
import importlib.util
import tempfile

import pytest
from skill_corpus import skill_corpus
from journal_events import journal_events

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"
ROOT = pathlib.Path(__file__).resolve().parent.parent


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def _recording_runner(by_subcommand=None):
    """Fake `gh` runner: records every call, returns canned stdout keyed by the gh verb (args[1]).
    Copied from tests/test_sources.py:14-23."""
    calls = []
    by_subcommand = by_subcommand or {}
    def run(args):
        calls.append(list(args))
        return by_subcommand.get(args[1] if len(args) > 1 else args[0], "")
    run.calls = calls
    return run


_UNSET = object()   # sentinel: "no override given" -- None is itself one of the values #522 fix 2's
                     # tests need fetch_comments_strict() to be able to return, so it can't double
                     # as the "not overridden" marker too.


class _FakeSource:
    """A source stub carrying a canned title/body plus full call-recording — enough surface for
    `decompose_check` to drive (fetch_title_body/park/note/complete/fail), mirroring
    tests/test_backlog_precheck.py's own `_FakeSource` extended with the new read method.

    #522: further extended with create_dependency/last_assignee_applied/issue_url (modeled on
    tests/test_handoff.py's own FakeSource, ~line 117) plus fetch_comments_strict, so `file` mode's
    ordering/idempotency tests can drive the WHOLE sequence at this fake-source layer without ever
    touching a real GitHubSource (whose own park()/_offboard() issue further gh calls of its own —
    see the #522 review's test-infra note). `note_error_on_call` lets a test fail only the Nth
    `note()` call (1-based) -- `create_tracked_issue`'s own narrative note is always call 1, so a
    test isolating decompose_check's OWN marker-note failure (call 2) never also breaks the
    narrative one. `fetch_comments_strict_return` (#522 review fix 2) overrides the WHOLE return
    value of fetch_comments_strict -- not just its comments/labels contents -- so a test can drive
    a malformed-shape return (None, a list, {}, a dict missing "comments") a real GitHubSource
    could never produce (it always returns a well-shaped dict) but decompose_check must still
    defend against, since `fetch_strict` is resolved via a bare `getattr` off WHATEVER source it is
    given, not guaranteed to be a real GitHubSource."""
    def __init__(self, title="", body="", issue_number="90", comments=None, labels=None,
                 last_assignee_applied=True, create_dependency_error=None,
                 fetch_comments_strict_error=None, fetch_comments_strict_return=_UNSET,
                 note_error=None, note_error_on_call=None):
        self.calls = []
        self._title = title
        self._body = body
        self.issue_number = issue_number
        self._comments = comments if comments is not None else []
        self._labels = labels if labels is not None else []
        self.last_assignee_applied = last_assignee_applied
        self._create_dependency_error = create_dependency_error
        self._fetch_comments_strict_error = fetch_comments_strict_error
        self._fetch_comments_strict_return = fetch_comments_strict_return
        self._note_error = note_error
        self._note_error_on_call = note_error_on_call
        self._note_call_count = 0
        self.created = None

    def fetch_title_body(self, goal):
        self.calls.append(("fetch_title_body", goal))
        return {"title": self._title, "body": self._body}

    def fetch_comments_strict(self, goal):
        self.calls.append(("fetch_comments_strict", goal))
        if self._fetch_comments_strict_error:
            raise self._fetch_comments_strict_error
        if self._fetch_comments_strict_return is not _UNSET:
            return self._fetch_comments_strict_return
        return {"comments": self._comments, "labels": self._labels}

    def create_dependency(self, title, body, assignee, labels=(), goal_label=True):
        self.calls.append(("create_dependency", title, body, assignee, tuple(labels), goal_label))
        if self._create_dependency_error:
            raise self._create_dependency_error
        self.created = {"title": title, "body": body, "assignee": assignee, "labels": list(labels)}
        return self.issue_number

    def issue_url(self, goal):
        return f"https://example.invalid/issues/{goal}"

    def park(self, goal, reason): self.calls.append(("park", goal, reason))

    def note(self, goal, text):
        self._note_call_count += 1
        self.calls.append(("note", goal, text))
        if self._note_error and (self._note_error_on_call is None
                                  or self._note_call_count == self._note_error_on_call):
            raise self._note_error

    def complete(self, goal): self.calls.append(("complete", goal))
    def fail(self, goal, reason): self.calls.append(("fail", goal, reason))


def _sdlc(tmp_path, config):
    """A bare .sdlc — config.json + a scaffolded run cursor + review queue — for tests that drive
    `decompose_check`/`_record` directly instead of through `run_loop`. Mirrors
    tests/test_backlog_precheck.py's `_sdlc()` / tests/test_loop.py's `_telemetry_base()`."""
    base = tmp_path / ".sdlc"
    (base / "state").mkdir(parents=True)
    (base / "config.json").write_text(json.dumps(config))
    (base / "state" / "STATE.md").write_text("iteration: 0\nrun_iteration: 0\nlast_run: none\n")
    (base / "state" / "review-queue.md").write_text("# Q\n")
    return str(base)


# An unambiguous epic-shaped body: 4 "##" sections (>= SECTION_THRESHOLD) *and* explicit
# Phase-1/2/3 markers — flagged regardless of exactly which signal fires first, so tests that only
# care about "is this thing flagged" (not "by which signal") don't couple to threshold tuning.
_EPIC_BODY = (
    "## Phase 1: backend\nBuild the backend pieces.\n\n"
    "## Phase 2: frontend\nBuild the UI pieces.\n\n"
    "## Phase 3: migration\nMigrate the old data.\n\n"
    "## Verification\nRun the whole suite.\n"
)

_SMALL_BODY = (
    "Add a retry to fetch_comments so a transient 502 doesn't fail the whole precheck.\n"
    "Tests: simulate a 502 then success; simulate an exhausted retry.\n"
    "Verification: pytest tests/test_sources.py -q.\n"
)


# --------------------------------------------------------------------- goal_size.classify (unit)


def test_classify_small_body_is_not_flagged():
    gs = _mod("goal_size")
    flagged, reason = gs.classify(_SMALL_BODY)
    assert flagged is False and reason == ""


def test_classify_flags_a_body_over_the_word_threshold():
    gs = _mod("goal_size")
    body = "word " * (gs.WORD_THRESHOLD + 50)          # one line, far over on words alone
    flagged, reason = gs.classify(body)
    assert flagged is True and "words" in reason


def test_classify_flags_a_body_over_the_line_threshold_but_under_the_word_threshold():
    gs = _mod("goal_size")
    body = "\n".join(f"item {i}" for i in range(gs.LINE_THRESHOLD + 10))   # 2 words/line
    assert len(body.split()) <= gs.WORD_THRESHOLD          # isolates the LINE signal specifically
    flagged, reason = gs.classify(body)
    assert flagged is True and "lines" in reason


def test_classify_flags_six_or_more_independent_h2_sections():
    """Retuned for #520's corpus-calibrated SECTION_THRESHOLD (3 -> 6, see goal_size.py's module
    docstring): the old value of 3 flagged every conventionally-shaped Context/Scope/AC/Verification
    goal in this repo's own issue history with zero true positives. Body has exactly 6 H2 sections —
    the new threshold's own boundary."""
    gs = _mod("goal_size")
    body = ("## Context\nshort\n\n## Scope\nshort\n\n## Approach\nshort\n\n"
            "## Risks\nshort\n\n## Testing\nshort\n\n## Verification\nshort\n")
    flagged, reason = gs.classify(body)
    assert flagged is True and "sections" in reason


def test_classify_does_not_flag_five_h2_sections():
    """Boundary pin one below the new threshold. 5 is the fence-stripped max across the FULL
    269-issue measured corpus (see goal_size.py's module docstring) — not literally represented in
    any of the 29 fixtures checked into tests/fixtures/goal_size/, which top out at 4 (goal-519.md,
    goal-464.md). This synthetic body is what actually pins the real-world ceiling; the checked-in
    corpus alone would leave 5 untested."""
    gs = _mod("goal_size")
    body = ("## A\nshort\n\n## B\nshort\n\n## C\nshort\n\n"
            "## D\nshort\n\n## E\nshort\n")
    flagged, reason = gs.classify(body)
    assert flagged is False


def test_classify_does_not_flag_only_two_h2_sections():
    gs = _mod("goal_size")
    body = "## Context\nshort\n\n## Scope\nshort\n"
    flagged, reason = gs.classify(body)
    assert flagged is False


def test_classify_h3_subsections_never_count_toward_the_h2_section_signal():
    """Re-armed for #520's SECTION_THRESHOLD=6 (was 3): with only 3 H3 subsections, even a
    regression that started counting H3 as a section would total just 1+3=4, still under the new
    threshold, so that body could no longer catch anything — silently vacuous at the new
    threshold, not just weak. Widened to 1 H2 + 6 H3 (total 7 if H3 wrongly counted) so the
    threshold itself is what keeps this test load-bearing; proof: a scratch copy of goal_size.py
    with `_SECTION_RE` widened to `^###?[ \\t]+\\S.*$` (H2 or H3 both match) classifies this exact
    body as flagged=True, reason='7 independent ## sections (>= 6)' — confirming this body WOULD
    fail against that regression, while the real (H2-only) regex classifies it as flagged=False."""
    gs = _mod("goal_size")
    body = "## Context\n### a\nx\n### b\nx\n### c\nx\n### d\nx\n### e\nx\n### f\nx\n"   # 1 H2 + 6 H3
    flagged, reason = gs.classify(body)
    assert flagged is False


def test_classify_flags_four_or_more_top_level_checkboxes():
    gs = _mod("goal_size")
    body = "- [ ] build the classifier\n- [ ] wire the verb\n- [x] write tests\n- [ ] update docs\n"
    flagged, reason = gs.classify(body)
    assert flagged is True and "checkboxes" in reason


def test_classify_does_not_flag_only_three_top_level_checkboxes():
    gs = _mod("goal_size")
    body = "- [ ] one\n- [ ] two\n- [x] three\n"
    flagged, reason = gs.classify(body)
    assert flagged is False


def test_classify_indented_checkboxes_are_not_top_level():
    gs = _mod("goal_size")
    body = "top task\n  - [ ] sub 1\n  - [ ] sub 2\n  - [ ] sub 3\n  - [ ] sub 4\n"
    flagged, reason = gs.classify(body)
    assert flagged is False


def test_classify_flags_explicit_multi_phase_structure():
    gs = _mod("goal_size")
    body = "## Phase 1: backend\ndo it\n\n## Phase 2: frontend\ndo it\n"   # only 2 sections, isolates phase
    flagged, reason = gs.classify(body)
    assert flagged is True and "phase" in reason.lower()


def test_classify_does_not_flag_a_single_phase_mention():
    gs = _mod("goal_size")
    body = "## Phase 1: backend\ndo it\n\nsome other prose with no second phase.\n"
    flagged, reason = gs.classify(body)
    assert flagged is False


def test_classify_reason_is_always_single_line():
    """`reason` lands verbatim in a park detail / action-log field — both reject raw newlines
    (state.py's `_offboard`/actionlog's `reject_newline`) — so a multi-line reason would corrupt
    either channel, not just look ugly. The sections body below is bumped to 6 (#520's new
    SECTION_THRESHOLD) — at the old 3-section body this arm no longer flags at all, so `reason`
    was always "" (trivially single-line) and this stopped actually exercising the sections-signal
    reason string; 6 makes it produce a real, non-empty reason again."""
    gs = _mod("goal_size")
    bodies = ["word " * (gs.WORD_THRESHOLD + 5),
              "\n".join(f"x {i}" for i in range(gs.LINE_THRESHOLD + 5)),
              "## a\nx\n\n## b\nx\n\n## c\nx\n\n## d\nx\n\n## e\nx\n\n## f\nx\n",
              "- [ ] a\n- [ ] b\n- [ ] c\n- [ ] d\n",
              "## Phase 1\nx\n\n## Phase 2\nx\n"]
    for body in bodies:
        _, reason = gs.classify(body)
        assert "\n" not in reason


# --------------------------------------------------------------------- #520: negative-precision synthetics
#
# Synthetic (not corpus) cases for _strip_fences / the anchored _PHASE_RE / the widened checkbox
# dialect — pinned here beside the other classifier units rather than as tests/fixtures/goal_size/
# corpus fixtures (per #520's adopted spec: these are constructed edge cases, not real issue bodies).


def test_classify_fenced_code_hash_lines_do_not_count_as_sections():
    """Without _strip_fences, the 5 fenced '## fake N' lines below would join the 2 real sections
    for a raw total of 7 (>= SECTION_THRESHOLD) — this proves they're excluded, not just
    coincidentally under threshold."""
    gs = _mod("goal_size")
    body = ("## Context\nshort\n\n"
            "```\n## fake 1\n## fake 2\n## fake 3\n## fake 4\n## fake 5\n```\n\n"
            "## Scope\nshort\n")
    flagged, reason = gs.classify(body)
    assert flagged is False, reason


def test_classify_shell_style_hash_comments_inside_fences_do_not_count():
    """A fenced shell snippet's `## Banner` comment lines are visually identical to a markdown H2
    to a naive regex; only fencing (not comment-syntax detection) keeps them out. Same 2-real+5-fake
    shape as the fenced-code case above, so it would also raw-total 7 without stripping."""
    gs = _mod("goal_size")
    body = ("## Context\nshort\n\n"
            "```bash\n## Section one\n## Section two\n## Section three\n## Section four\n"
            "## Section five\necho hi\n```\n\n"
            "## Scope\nshort\n")
    flagged, reason = gs.classify(body)
    assert flagged is False, reason


def test_classify_prose_phase_accident_does_not_flag():
    """The old unanchored `_PHASE_RE` flagged this exact sentence shape as a genuine two-phase body
    (reproduced live against this repo's own #519, whose prose mentions 'Phase-1/Phase-2' without
    describing an actual multi-phase goal). The new line-start-anchored regex requires 'phase' to
    open the line (optionally after a heading/bullet/number marker), never mid-sentence."""
    gs = _mod("goal_size")
    body = "This is the phase-2 follow-up to the phase-1 work in #500.\n"
    flagged, reason = gs.classify(body)
    assert flagged is False, reason


def test_classify_plus_bullet_checkboxes_count_now():
    """Dialect pin: `+` is a valid GFM bullet marker the old regex (`[-*]` only) missed."""
    gs = _mod("goal_size")
    body = "+ [ ] one\n+ [ ] two\n+ [ ] three\n+ [ ] four\n"
    flagged, reason = gs.classify(body)
    assert flagged is True and "checkboxes" in reason


def test_classify_two_space_gap_checkboxes_count_now():
    """Dialect pin: a two-space gap between the bullet and `[` is valid GFM the old regex (exactly
    one `[ \\t]`) missed."""
    gs = _mod("goal_size")
    body = "-  [ ] one\n-  [ ] two\n-  [ ] three\n-  [ ] four\n"
    flagged, reason = gs.classify(body)
    assert flagged is True and "checkboxes" in reason


def test_classify_unterminated_fence_blanks_to_eof():
    """An opened-but-never-closed fence blanks EVERYTHING after it, to EOF — not just some
    heuristic extent — conservative, failing TOWARD not-flagging a malformed body. Without that,
    the 2 real sections before the fence plus the 4 '## ' lines after the (never-closed) fence would
    raw-total 6 (>= SECTION_THRESHOLD); properly blanked, only the 2 real sections before the fence
    count."""
    gs = _mod("goal_size")
    body = ("## Context\nshort\n\n## Scope\nshort\n\n"
            "```\nsome code\n"
            "## D\n## E\n## F\n## G\n")
    flagged, reason = gs.classify(body)
    assert flagged is False, reason


def test_classify_mismatched_fence_delimiter_does_not_close_the_fence():
    """CommonMark: a fence closes only on a matching delimiter CHARACTER — a ~~~ line inside an
    open ``` fence is ordinary fenced content, not a close. Without that rule, the ~~~ below would
    have closed the ``` fence early, exposing the 5 '## ' lines that follow it as real sections (1
    real section before the fence + those 5 = 6, >= SECTION_THRESHOLD) — this bug failed TOWARD
    flagging, never away from it."""
    gs = _mod("goal_size")
    body = ("## Context\nshort\n\n"
            "```\nsome code\n~~~\n## D\n## E\n## F\n## G\n## H\n```\n")
    flagged, reason = gs.classify(body)
    assert flagged is False, reason


# --------------------------------------------------------------------- config gate (OFF / fail-open)


def test_decompose_check_off_when_disabled_touches_nothing(tmp_path, capsys):
    lp = _mod("loop")
    base = _sdlc(tmp_path, {"goal_decompose": {"enabled": False}})
    src = _FakeSource(body=_EPIC_BODY)
    assert lp.decompose_check(base, "1", json.loads((pathlib.Path(base) / "config.json").read_text()), src) == "OFF"
    assert src.calls == []
    assert capsys.readouterr().err == ""           # OFF is silent — no spurious warning


def test_decompose_check_off_when_key_absent_touches_nothing(tmp_path, capsys):
    lp = _mod("loop")
    base = _sdlc(tmp_path, {})                       # no goal_decompose key at all
    cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
    src = _FakeSource(body=_EPIC_BODY)
    assert lp.decompose_check(base, "1", cfg, src) == "OFF"
    assert src.calls == []
    assert capsys.readouterr().err == ""


def test_decompose_check_off_when_enabled_key_absent_touches_nothing(tmp_path, capsys):
    lp = _mod("loop")
    base = _sdlc(tmp_path, {"goal_decompose": {}})    # present, but no "enabled" key
    cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
    src = _FakeSource(body=_EPIC_BODY)
    assert lp.decompose_check(base, "1", cfg, src) == "OFF"
    assert src.calls == []
    assert capsys.readouterr().err == ""


def test_decompose_check_off_on_falsy_malformed_config(tmp_path, capsys):
    """Falsy-malformed (`false`/`0`/`""`/`[]`) degrades the same as absent — OFF, no crash, no
    warning (plan-review change 4)."""
    lp = _mod("loop")
    for i, bogus in enumerate((False, 0, "", [])):
        base = _sdlc(tmp_path / str(i), {"goal_decompose": bogus})   # fresh dir per case
        cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
        src = _FakeSource(body=_EPIC_BODY)
        assert lp.decompose_check(base, "1", cfg, src) == "OFF", bogus
        assert src.calls == [], bogus
        assert capsys.readouterr().err == "", bogus


def test_decompose_check_fails_open_on_truthy_malformed_config(tmp_path, capsys):
    """Truthy-malformed (`"on"`/`true`/`5` — where `.get` would raise AttributeError) fails OPEN to
    PROCEED via the outer catch, with exactly one stderr line — never a crash, never a silent OFF
    (plan-review change 4: this must not be confused with the falsy-malformed OFF case above)."""
    lp = _mod("loop")
    for i, bogus in enumerate(("on", True, 5)):
        base = _sdlc(tmp_path / str(i), {"goal_decompose": bogus})   # fresh dir per case
        cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
        src = _FakeSource(body=_EPIC_BODY)
        assert lp.decompose_check(base, "1", cfg, src) == "PROCEED", bogus
        assert src.calls == [], bogus                  # the gate itself raised — never reached the read
        err = capsys.readouterr().err
        assert err.strip() != "" and err.count("\n") == 1, bogus


def test_decompose_check_proceeds_for_an_unflagged_goal(tmp_path):
    lp = _mod("loop")
    base = _sdlc(tmp_path, {"goal_decompose": {"enabled": True, "mode": "park"}})
    cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
    src = _FakeSource(body=_SMALL_BODY)
    assert lp.decompose_check(base, "1", cfg, src) == "PROCEED"
    assert not any(c[0] == "park" for c in src.calls)   # not flagged -> never parked, even in park mode


# --------------------------------------------------------------------- first-line anchoring


def test_decompose_check_proceeds_for_a_decomposition_child_marked_first_line(tmp_path):
    lp = _mod("loop")
    base = _sdlc(tmp_path, {"goal_decompose": {"enabled": True, "mode": "park"}})
    cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
    body = "sigma:decomposed-from=100\n\n" + _EPIC_BODY     # would otherwise clearly flag
    src = _FakeSource(body=body)
    assert lp.decompose_check(base, "1", cfg, src) == "PROCEED"
    assert src.calls == [("fetch_title_body", "1")]              # read, but never classified/parked


def test_decompose_check_proceeds_for_a_meta_goal_marked_first_line(tmp_path):
    lp = _mod("loop")
    base = _sdlc(tmp_path, {"goal_decompose": {"enabled": True, "mode": "park"}})
    cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
    body = "sigma:decompose-of=100\n\n" + _EPIC_BODY
    src = _FakeSource(body=body)
    assert lp.decompose_check(base, "1", cfg, src) == "PROCEED"
    assert src.calls == [("fetch_title_body", "1")]


def test_decompose_check_a_marker_in_the_title_only_does_not_refuse(tmp_path):
    """Negative anchoring test (plan-review change 7): the guard reads the BODY only. A marker
    sitting in the title must never exempt an otherwise-epic-shaped body."""
    lp = _mod("loop")
    base = _sdlc(tmp_path, {"goal_decompose": {"enabled": True, "mode": "log"}, "action_log": {"enabled": False}})
    cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
    src = _FakeSource(title="sigma:decomposed-from=100 — my epic", body=_EPIC_BODY)
    result = lp.decompose_check(base, "1", cfg, src)
    assert result.startswith("PROCEED (flagged:")           # classification actually ran


def test_decompose_check_a_marker_mid_body_only_does_not_refuse(tmp_path):
    """Negative anchoring test (plan-review change 7 + base AC): a marker anywhere other than the
    first line of the body must never exempt the goal."""
    lp = _mod("loop")
    base = _sdlc(tmp_path, {"goal_decompose": {"enabled": True, "mode": "log"}, "action_log": {"enabled": False}})
    cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
    body = "This is a normal-looking first line.\n" + _EPIC_BODY + "\nsigma:decomposed-from=100\n"
    src = _FakeSource(body=body)
    result = lp.decompose_check(base, "1", cfg, src)
    assert result.startswith("PROCEED (flagged:")


def test_decompose_check_anchoring_is_crlf_tolerant(tmp_path):
    lp = _mod("loop")
    base = _sdlc(tmp_path, {"goal_decompose": {"enabled": True, "mode": "park"}})
    cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
    body = "sigma:decomposed-from=100\r\n\r\n" + _EPIC_BODY.replace("\n", "\r\n")
    src = _FakeSource(body=body)
    assert lp.decompose_check(base, "1", cfg, src) == "PROCEED"
    assert not any(c[0] == "park" for c in src.calls)


def test_decompose_check_verb_reads_the_marker_constant_live_not_a_hardcoded_copy(tmp_path):
    """#521 review (SHOULD-FIX): the single-source-of-truth pin was one-sided.
    tests/test_backlog_check.py's test_backlog_check_exempt_reads_the_constant_live_not_a_hardcoded_copy
    proves backlog_check reads goal_size's constant live, by mutating the ACTUAL goal_size module
    object backlog_check holds. But loop.py's own `_load("goal_size")` call resolves a FRESH module
    instance on every invocation (never cached), so that same trick doesn't reach it -- nothing
    previously proved decompose_check's marker guard actually reads `gs.DECOMPOSED_FROM_MARKER`
    at runtime rather than a hand-typed literal that merely happens to match today.

    Monkeypatch loop.py's OWN `_load` (the name decompose_check's `gs = _load("goal_size")` resolves
    via its module globals at call time) so it hands back a stand-in with a RENAMED
    DECOMPOSED_FROM_MARKER but the REAL `classify`. A body whose first line carries the OLD/real
    marker text no longer matches the guard's (renamed) constant, so it falls through to real
    classification -- and, being unambiguously epic-shaped, gets flagged and parked instead of
    short-circuiting to a bare PROCEED. A hardcoded-literal guard would be blind to this swap and
    would still short-circuit to PROCEED regardless -- exactly the gap this test closes."""
    lp = _mod("loop")
    gs = _mod("goal_size")

    class _StubGoalSize:
        DECOMPOSED_FROM_MARKER = "totally-renamed-marker="
        DECOMPOSE_OF_MARKER = "totally-renamed-meta="
        classify = staticmethod(gs.classify)             # real classification, unmodified

    real_load = lp._load
    lp._load = lambda name: _StubGoalSize if name == "goal_size" else real_load(name)
    try:
        base = _sdlc(tmp_path, {"goal_decompose": {"enabled": True, "mode": "park"}})
        cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
        body = "sigma:decomposed-from=100\n\n" + _EPIC_BODY     # the OLD/real marker text
        src = _FakeSource(body=body)
        result = lp.decompose_check(base, "1", cfg, src)
        # renamed constant -> the OLD marker text no longer matches the guard -> falls through to
        # real classification -> epic body gets flagged+parked, NOT a bare "PROCEED".
        assert result.startswith("PARKED"), result
    finally:
        lp._load = real_load


# --------------------------------------------------------------------- log mode: zero mutation


def test_log_mode_is_zero_mutation_and_writes_one_actionlog_entry(tmp_path):
    """The load-bearing proof (plan-review change 1): a REAL GitHubSource, built the same way
    production code builds one, driven through decompose_check — the read must show up in the
    recording fake's own call log (proving the verb used `source.fetch_title_body`, not a bypassing
    module-level shell-out — a bypass would make this whole test vacuous), and not one mutating
    `issue edit|comment|close` call may ever appear.

    #1808: `fetch_title_body` reads via REST (`gh api repos/.../issues/<n>`) now, not the GraphQL
    `gh issue view` this used to key on — the fake is keyed on the REST endpoint accordingly."""
    lp = _mod("loop")
    sources = _mod("sources")
    actionlog = _mod("actionlog")
    base = _sdlc(tmp_path, {
        "goal_decompose": {"enabled": True, "mode": "log"},
        "action_log": {"enabled": True},
        "discovery": {"source": "github"},
    })
    cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
    run = _recording_runner({"repos/{owner}/{repo}/issues/42":
                              json.dumps({"title": "an epic issue", "body": _EPIC_BODY})})
    gh = sources.GitHubSource(cfg, run=run)

    result = lp.decompose_check(base, "42", cfg, gh)

    assert result.startswith("PROCEED (flagged:")
    assert any(len(c) > 1 and c[0] == "api" and c[1] == "repos/{owner}/{repo}/issues/42"
               for c in run.calls), \
        "the read never reached the recording fake — decompose_check likely bypassed the source"
    mutating = [c for c in run.calls if len(c) > 1 and c[0] == "issue" and c[1] in ("edit", "comment", "close")]
    assert mutating == []

    entries = [e for e in actionlog.read_goal(base, "42") if e["kind"] == "decompose_check"]
    assert len(entries) == 1
    assert entries[0]["verdict"] == "flagged"
    assert entries[0]["mode"] == "log"
    assert entries[0]["reason"]                 # non-empty, and (schema-enforced) newline-free


def test_log_mode_without_action_log_enabled_still_proceeds_without_mutating(tmp_path):
    """`action_log` is its own independent opt-in — decompose_check's `log` mode must not depend on
    it being on to stay non-mutating."""
    lp = _mod("loop")
    sources = _mod("sources")
    base = _sdlc(tmp_path, {
        "goal_decompose": {"enabled": True, "mode": "log"},
        "discovery": {"source": "github"},
    })
    cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
    run = _recording_runner({"repos/{owner}/{repo}/issues/42":
                              json.dumps({"title": "an epic issue", "body": _EPIC_BODY})})
    gh = sources.GitHubSource(cfg, run=run)
    result = lp.decompose_check(base, "42", cfg, gh)
    assert result.startswith("PROCEED (flagged:")
    mutating = [c for c in run.calls if len(c) > 1 and c[0] == "issue" and c[1] in ("edit", "comment", "close")]
    assert mutating == []


# --------------------------------------------------------------------- park mode: single _record


def test_park_mode_records_exactly_once_with_needs_decision_reason_class(tmp_path):
    """Precedent: tests/test_backlog_precheck.py:51-61 (cursor-advance proof) + plan-review change 6
    (the ledger park event's reason_class)."""
    lp = _mod("loop")
    state = _mod("state")
    ledger = _mod("ledger")
    base = _sdlc(tmp_path, {
        "goal_decompose": {"enabled": True, "mode": "park"},
        "ledger": {"enabled": True, "actor": "rae"},
        "journal": {"enabled": True},   # the EVENTS stream gate is ledger AND journal, both
    })
    cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
    gs = _mod("goal_size")
    _, expected_reason = gs.classify(_EPIC_BODY)
    src = _FakeSource(body=_EPIC_BODY)

    result = lp.decompose_check(base, "7", cfg, src)

    expected_detail = f"too large per goal_size ({expected_reason}) — needs manual decomposition"
    assert result == "PARKED " + expected_detail
    park_calls = [c for c in src.calls if c[0] == "park"]
    assert len(park_calls) == 1                                       # exactly one _record park
    assert park_calls[0] == ("park", "7", expected_detail)
    assert state.load_cursor(base)["run_iteration"] == 1              # the park counted as an iteration

    events = [e for e in journal_events(ledger, base) if e["kind"] == "park"]
    assert len(events) == 1
    assert events[0]["reason_class"] == "needs_decision"


def _file_cfg(tmp_path, extra=None, max_children=None):
    cfg = {"goal_decompose": {"enabled": True, "mode": "file"}, "ledger": {"actor": "rae"},
           # pre-triage filing path pinned here; decompose children are converted by a later slice (#1003)
           "ai_filed": {"triage": {"enabled": False}}}
    if max_children is not None:
        cfg["goal_decompose"]["max_children"] = max_children
    if extra:
        cfg.update(extra)
    return _sdlc(tmp_path, cfg)


# --------------------------------------------------------------------- #522: file mode (real filing)


def test_file_mode_idempotency_hit_parks_without_creating(tmp_path):
    lp = _mod("loop")
    base = _file_cfg(tmp_path)
    cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
    src = _FakeSource(body=_EPIC_BODY,
                       comments=[{"body": "just a note"},
                                 {"body": "already filed: sigma:decompose-filed=#901"}])

    result = lp.decompose_check(base, "7", cfg, src)

    assert result == "PARKED decomposition already filed — see comments"
    assert not any(c[0] == "create_dependency" for c in src.calls)
    assert len([c for c in src.calls if c[0] == "park"]) == 1
    assert any(c[0] == "fetch_comments_strict" for c in src.calls)


def test_file_mode_idempotency_read_raising_fails_closed_never_proceeds(tmp_path):
    """NEVER treat an unreadable timeline as "no marker" (that could double-file the meta-issue),
    and NEVER fall through to the outer catch's bare PROCEED."""
    lp = _mod("loop")
    base = _file_cfg(tmp_path)
    cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
    src = _FakeSource(body=_EPIC_BODY, fetch_comments_strict_error=RuntimeError("gh: rate limited"))

    result = lp.decompose_check(base, "7", cfg, src)

    assert result == ("PARKED could not confirm whether a decomposition was already filed — "
                       "check comments")
    assert not any(c[0] == "create_dependency" for c in src.calls)


# --------------------------------------------------------------------- #522 review fix 2: a
# malformed (not raised, just WRONGLY SHAPED) strict-read return must fail closed too -- treating
# None / a list / {} / a dict missing "comments" as "no marker found" would re-open the exact
# fail-open hole the strict read exists to close, one layer down from the "raises" case above.
# `fetch_strict` is resolved via a bare `getattr` off whatever source decompose_check is given, so
# this can't assume it always sees a real GitHubSource's own well-shaped dict.


def test_file_mode_strict_read_returning_none_fails_closed(tmp_path):
    lp = _mod("loop")
    base = _file_cfg(tmp_path)
    cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
    src = _FakeSource(body=_EPIC_BODY, fetch_comments_strict_return=None)

    result = lp.decompose_check(base, "7", cfg, src)

    assert result == ("PARKED could not confirm whether a decomposition was already filed — "
                       "check comments")
    assert not any(c[0] == "create_dependency" for c in src.calls)


def test_file_mode_strict_read_returning_a_list_fails_closed(tmp_path):
    lp = _mod("loop")
    base = _file_cfg(tmp_path)
    cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
    src = _FakeSource(body=_EPIC_BODY, fetch_comments_strict_return=[])

    result = lp.decompose_check(base, "7", cfg, src)

    assert result == ("PARKED could not confirm whether a decomposition was already filed — "
                       "check comments")
    assert not any(c[0] == "create_dependency" for c in src.calls)


def test_file_mode_strict_read_returning_empty_dict_fails_closed(tmp_path):
    lp = _mod("loop")
    base = _file_cfg(tmp_path)
    cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
    src = _FakeSource(body=_EPIC_BODY, fetch_comments_strict_return={})

    result = lp.decompose_check(base, "7", cfg, src)

    assert result == ("PARKED could not confirm whether a decomposition was already filed — "
                       "check comments")
    assert not any(c[0] == "create_dependency" for c in src.calls)


def test_file_mode_strict_read_missing_comments_key_fails_closed(tmp_path):
    """A dict that HAS "labels" but no "comments" key at all is just as untrustworthy as a raise --
    the marker check needs "comments" specifically."""
    lp = _mod("loop")
    base = _file_cfg(tmp_path)
    cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
    src = _FakeSource(body=_EPIC_BODY,
                       fetch_comments_strict_return={"labels": [{"name": "area:engine"}]})

    result = lp.decompose_check(base, "7", cfg, src)

    assert result == ("PARKED could not confirm whether a decomposition was already filed — "
                       "check comments")
    assert not any(c[0] == "create_dependency" for c in src.calls)


@pytest.mark.parametrize("comments", [
    "a string",             # the shape the #527 review named: truthy, iterable, yields CHARACTERS
    None,                   # present-but-null: `or []` used to launder this into "no comments"
    {},                     # falsy non-list: same laundering
    {"marker": "here"},     # truthy dict: iterating yields KEYS, so the marker scan sees nothing
    ("t",),                 # a tuple is iterable but is NOT a list -- the check is the type, not duck-typing
    5,                      # not iterable at all: parked before it can raise, with the RIGHT reason
])
def test_file_mode_strict_read_with_non_list_comments_fails_closed(tmp_path, comments):
    """The key being PRESENT is not enough -- its value has to be a list, or the marker scan below is
    meaningless and "no marker found" is a conclusion we never actually reached. Every shape here
    read as "no marker" and went on to FILE the meta-issue: the marker scan skips non-dict elements,
    so a string yields characters and a dict yields keys, each matching nothing. `5` was the one that
    happened to fail closed already -- but only by raising TypeError into the outer catch, i.e. with
    the wrong park reason and by accident. Unreachable through GitHubSource today (gh's schema always
    returns an array), so this is the same fail-closed principle the guard already implements,
    extended one level down from the key to its value."""
    lp = _mod("loop")
    base = _file_cfg(tmp_path)
    cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
    src = _FakeSource(body=_EPIC_BODY,
                       fetch_comments_strict_return={"comments": comments,
                                                     "labels": [{"name": "area:engine"}]})

    result = lp.decompose_check(base, "7", cfg, src)

    assert result == ("PARKED could not confirm whether a decomposition was already filed — "
                       "check comments")
    assert not any(c[0] == "create_dependency" for c in src.calls)


def test_file_mode_degrades_to_park_when_source_has_no_create_seam(capsys):
    """Mirrors test_decompose_check_cli_local_mode_parks_an_epic (mode park) with mode file: a
    LocalSource can now CREATE a goal (create_dependency) but has no per-item comment timeline, so
    it cannot read back its own idempotency marker and cannot guarantee it won't double-file. File
    mode must still degrade to the visible park action with a clause explaining why, never a false
    "failed to file" park with nothing behind it.

    This test previously asserted the guard via "LocalSource has no create_dependency at all". That
    premise died when local backlogs gained work-item creation; the guard now names both seams it
    needs, and this test pins the user-facing outcome rather than the proxy."""
    lp = _mod("loop")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"
        (base / "goals").mkdir(parents=True)
        (base / "state").mkdir()
        (base / "config.json").write_text(json.dumps(
            {"goal_decompose": {"enabled": True, "mode": "file"}, "budget": {"max_iterations": 100}}))
        (base / "state" / "STATE.md").write_text("iteration: 0\nrun_iteration: 0\nlast_run: none\n")
        (base / "state" / "review-queue.md").write_text("# Q\n")
        goal_path = base / "goals" / "0001.md"
        goal_path.write_text("---\nid: 0001\nstatus: pending\ntitle: a huge multi-phase goal\n---\n"
                              + _EPIC_BODY)
        rc = lp.main(["loop.py", "decompose-check", str(base), str(goal_path)])
        assert rc == 0
        out = capsys.readouterr().out
        assert out.startswith("PARKED")
        assert "needs manual decomposition" in out
        assert "file mode needs an issue tracker" in out


def test_file_mode_create_fail_parks_with_warnings_and_never_posts_a_marker(tmp_path):
    lp = _mod("loop")
    base = _file_cfg(tmp_path)
    cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
    src = _FakeSource(body=_EPIC_BODY, create_dependency_error=RuntimeError("gh: not authenticated"))

    result = lp.decompose_check(base, "7", cfg, src)

    assert result.startswith("PARKED too large — failed to file decomposition goal")
    assert "not authenticated" in result
    assert result.endswith("— needs a human")
    assert not any(c[0] == "note" for c in src.calls)          # never attempted -- report["issue"] is None
    assert len([c for c in src.calls if c[0] == "park"]) == 1


def test_file_mode_marker_post_failure_still_parks_with_the_warning_folded_in(tmp_path):
    lp = _mod("loop")
    base = _file_cfg(tmp_path)
    cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
    src = _FakeSource(body=_EPIC_BODY, issue_number="901",
                       note_error=RuntimeError("gh: comment failed"), note_error_on_call=2)

    result = lp.decompose_check(base, "7", cfg, src)

    assert result.startswith("PARKED too large per goal_size")
    assert "decomposition filed as #901" in result
    assert "comment failed" in result                          # the marker-post warning folds in
    notes = [c for c in src.calls if c[0] == "note"]
    assert len(notes) == 2
    assert "decompose-filed" in notes[1][2]                     # the SECOND note is our own marker
    assert len([c for c in src.calls if c[0] == "park"]) == 1


def test_file_mode_happy_path_ordering_one_create_two_notes_marker_last_one_park(tmp_path):
    lp = _mod("loop")
    base = _file_cfg(tmp_path, max_children=6)
    cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
    src = _FakeSource(title="a huge multi-phase goal", body=_EPIC_BODY, issue_number="901",
                       labels=[{"name": "area:engine"}, {"name": "priority:P0"}])

    result = lp.decompose_check(base, "7", cfg, src)

    assert result.startswith("PARKED too large per goal_size")
    assert "decomposition filed as #901" in result

    kinds = [c[0] for c in src.calls if c[0] in ("create_dependency", "note", "park")]
    assert kinds == ["create_dependency", "note", "note", "park"]

    notes = [c for c in src.calls if c[0] == "note"]
    assert "decompose-filed" not in notes[0][2]                 # narrative note (create_tracked_issue)
    assert "decompose-filed" in notes[1][2]                     # OUR marker note is LAST

    assert len([c for c in src.calls if c[0] == "fetch_title_body"]) == 1     # not re-fetched
    assert any(c[0] == "fetch_comments_strict" for c in src.calls)

    create_call = next(c for c in src.calls if c[0] == "create_dependency")
    _, title, body, assignee, labels, goal_label = create_call
    assert title.startswith("Decompose #7:") and "a huge multi-phase goal" in title
    assert assignee == "rae"                                    # same_area=True -> ledger.actor
    assert "area:engine" in labels and "priority:P0" in labels
    assert "sdlc:decompose" in labels
    assert not [l for l in labels if l.startswith("model:")], (
        "#1602: the meta-issue must not drag an undefined model:* value onto the adopter's board")
    assert goal_label is True                                   # immediately_actionable=True
    assert body.splitlines()[0] == "<!-- sigma:decompose-of=#7 -->"
    assert "2..6 children" in body


def test_file_mode_max_children_is_floored_at_two(tmp_path):
    """#522 review fix 6: a hand-edited (or config-typo'd) max_children of 0/1/negative must never
    render a nonsensical "Plan 2..0 children" (or worse) into the filed meta-goal's own
    instructions -- 2 is the minimum a "split" can mean at all."""
    lp = _mod("loop")
    for bogus in (0, 1, -3):
        base = _file_cfg(tmp_path / str(bogus), max_children=bogus)
        cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
        src = _FakeSource(body=_EPIC_BODY, issue_number="901")

        lp.decompose_check(base, "7", cfg, src)

        create_call = next(c for c in src.calls if c[0] == "create_dependency")
        body = create_call[2]
        assert "2..2 children" in body, (bogus, body)
        assert f"2..{bogus} children" not in body, (bogus, body)


def test_file_mode_unassigned_surfaces_in_the_park_detail(tmp_path):
    lp = _mod("loop")
    base = _file_cfg(tmp_path)
    cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
    src = _FakeSource(body=_EPIC_BODY, issue_number="901", last_assignee_applied=False)

    result = lp.decompose_check(base, "7", cfg, src)

    assert result.startswith("PARKED")
    assert "filed as #901 but unassigned — a human must assign it before any loop can see it" in result


def test_file_mode_area_and_priority_default_when_the_parent_has_no_such_labels(tmp_path):
    lp = _mod("loop")
    base = _file_cfg(tmp_path)
    cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
    src = _FakeSource(body=_EPIC_BODY, issue_number="901")     # no labels configured

    lp.decompose_check(base, "7", cfg, src)

    create_call = next(c for c in src.calls if c[0] == "create_dependency")
    _, _title, _body, _assignee, labels, _goal_label = create_call
    assert "area:unknown" in labels
    assert "priority:P1" in labels                              # handoff.DEFAULT_PRIORITY


def test_file_mode_inner_wrapper_survives_a_raise_after_create_dependency_returns(tmp_path, monkeypatch):
    """R8 (#522 review): a flagged file-mode park path must never report PROCEED once
    create_dependency has already landed a real issue -- monkeypatch something that runs strictly
    AFTER create_dependency returns inside create_tracked_issue itself (its own ledger.safe_append,
    called from handoff.py's OWN loaded `ledger` reference -- precedent
    test_park_survives_a_failure_after_source_park_has_already_landed, one level up: the file-mode
    sequence needs the identical guarantee for its own longer inner sequence)."""
    lp = _mod("loop")
    real_load = lp._load
    hf = real_load("handoff")            # a fresh handoff module instance, independent of anything
                                          # loop.py itself has already loaded

    def _boom(*a, **kw):
        raise RuntimeError("ledger boom after create")
    monkeypatch.setattr(hf.ledger, "safe_append", _boom)
    lp._load = lambda name: hf if name == "handoff" else real_load(name)
    try:
        base = _file_cfg(tmp_path)
        cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
        src = _FakeSource(body=_EPIC_BODY, issue_number="901")

        result = lp.decompose_check(base, "7", cfg, src)

        assert result.split()[0] == "PARKED", \
            ("a flagged file-mode park path must never downgrade to PROCEED once "
             f"create_dependency already landed a real issue, got: {result!r}")
        assert any(c[0] == "create_dependency" for c in src.calls)   # the mutation itself DID happen
        assert len([c for c in src.calls if c[0] == "park"]) == 1
    finally:
        lp._load = real_load


def test_park_survives_a_failure_after_source_park_has_already_landed(tmp_path, monkeypatch):
    """A flagged park path must NEVER report PROCEED once the actual park mutation
    (`source.park`) has already gone out — a caller reading PROCEED would go implement the very
    epic-shaped goal this feature exists to catch, on top of it now ALSO being parked on GitHub.
    `_record` calls `source.park` first, then cursor/ledger/actionlog bookkeeping; this monkeypatches
    `ledger.safe_append` (loop.py's own module-level reference, called from inside `_record` strictly
    AFTER `source.park`) to raise, simulating exactly that "mutation landed, bookkeeping blew up"
    ordering — a bare `_record(...)` call with no try/except of its own lets that exception fall
    through to decompose_check's outer catch, which used to answer PROCEED."""
    lp = _mod("loop")
    base = _sdlc(tmp_path, {"goal_decompose": {"enabled": True, "mode": "park"}})
    cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
    src = _FakeSource(body=_EPIC_BODY)

    def _boom(*a, **kw):
        raise RuntimeError("ledger boom")
    monkeypatch.setattr(lp.ledger, "safe_append", _boom)

    result = lp.decompose_check(base, "7", cfg, src)

    assert result.split()[0] == "PARKED", \
        f"a flagged park path must never downgrade to PROCEED after the mutation landed, got: {result!r}"
    park_calls = [c for c in src.calls if c[0] == "park"]
    assert len(park_calls) == 1, "the park mutation itself must still be attempted exactly once"


def test_file_mode_never_reuses_a_different_parents_decompose_meta_issue(tmp_path, monkeypatch):
    """PR #1204 review finding: `create_tracked_issue`'s new file-time duplicate search (wired in
    unconditionally by this same PR) also ran for THIS caller -- and decompose meta-issue bodies are
    ~99% fixed boilerplate (`decompose_goal._META_BODY`; only the parent id/title differ), so two
    DIFFERENT parents' decompose issues scored as TF-IDF "duplicates" of each other purely from
    shared template text (measured 0.55+ against a real, non-degenerate fixture -- comfortably past
    the 0.45 duplicate line; see the sibling regression in test_handoff.py). Unfixed, decompose_check
    would silently reuse goal #101's already-filed meta-issue as if it were goal #303's, post the
    "decomposition filed" marker onto #303's own issue naming the WRONG child, and #303's real
    decomposition would never happen -- with no error surfaced anywhere (`report["warnings"]` reads
    as an ordinary, correct-looking "duplicate found" line).

    Reproduced against the REAL `dedup`/`mirror` engine end to end -- only the `gh` round-trip
    (`fetch_dependency_records`) is stubbed, never the collision itself."""
    lp = _mod("loop")
    real_load = lp._load
    hf = real_load("handoff")            # a fresh handoff module instance, independent of anything
                                          # loop.py itself has already loaded (same precedent as
                                          # test_file_mode_inner_wrapper_survives_a_raise... above)
    dg = real_load("decompose_goal")

    # a decompose meta-issue ALREADY filed for a completely different, unrelated parent (#101) --
    # the exact near-duplicate-template shape the finding describes.
    prior_body = dg.render_meta_body(101, 4)
    prior_raw = {"number": 501, "title": "Decompose #101: payment gateway integration",
                "body": prior_body, "labels": [{"name": "sdlc:decompose"}], "state": "OPEN",
                "closedAt": None, "updatedAt": "2026-01-01T00:00:00Z"}
    monkeypatch.setattr(hf.mirror, "fetch_dependency_records",
                        lambda *a, **k: hf.mirror.build_records([prior_raw], []))

    lp._load = lambda name: {"handoff": hf, "decompose_goal": dg}.get(name) or real_load(name)
    try:
        base = _file_cfg(tmp_path, extra={"discovery": {"source": "github"}})
        cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
        # #303 is unrelated to #101 in every distinguishing word EXCEPT the shared decompose-
        # template boilerplate.
        src = _FakeSource(title="dark mode theming for the settings page", body=_EPIC_BODY,
                          issue_number="900")

        result = lp.decompose_check(base, "303", cfg, src)

        assert result.startswith("PARKED"), result
        assert "decomposition filed as #900" in result, \
            f"expected #303's OWN new meta-issue (#900), got: {result!r}"
        create_calls = [c for c in src.calls if c[0] == "create_dependency"]
        assert len(create_calls) == 1, (
            "decompose_check silently reused #501 (goal #101's own meta-issue) instead of filing "
            f"a new one for goal #303 -- calls: {src.calls!r}")
        assert "#303" in create_calls[0][1]
    finally:
        lp._load = real_load


# --------------------------------------------------------------------- unrecognized mode


def test_unrecognized_mode_falls_back_to_log_behavior_with_a_stderr_warning(tmp_path, capsys):
    lp = _mod("loop")
    base = _sdlc(tmp_path, {"goal_decompose": {"enabled": True, "mode": "bogus-mode"}})
    cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
    src = _FakeSource(body=_EPIC_BODY)

    result = lp.decompose_check(base, "1", cfg, src)

    assert result is not None
    assert isinstance(result, str)
    assert result.startswith("PROCEED (flagged:")        # log behavior, never PARKED
    assert not any(c[0] == "park" for c in src.calls)
    err = capsys.readouterr().err
    assert "bogus-mode" in err


def test_absent_mode_key_behaves_as_log_silently(tmp_path, capsys):
    """Plan-review change 3: an ABSENT `mode` key (not just an unrecognized string) must default to
    `log` behavior with NO stderr warning at all — the unrecognized-mode fallback above is for a
    genuinely unrecognized non-empty string only, never for the common "I didn't set mode" case."""
    lp = _mod("loop")
    base = _sdlc(tmp_path, {"goal_decompose": {"enabled": True}})   # enabled, no "mode" key
    cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
    src = _FakeSource(body=_EPIC_BODY)

    result = lp.decompose_check(base, "1", cfg, src)

    assert result.startswith("PROCEED (flagged:")
    assert capsys.readouterr().err == ""


# --------------------------------------------------------------------- CLI dispatch (local mode)


def test_decompose_check_cli_local_mode_parks_an_epic(capsys):
    lp = _mod("loop")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"
        (base / "goals").mkdir(parents=True)
        (base / "state").mkdir()
        (base / "config.json").write_text(json.dumps(
            {"goal_decompose": {"enabled": True, "mode": "park"}, "budget": {"max_iterations": 100}}))
        (base / "state" / "STATE.md").write_text("iteration: 0\nrun_iteration: 0\nlast_run: none\n")
        (base / "state" / "review-queue.md").write_text("# Q\n")
        goal_path = base / "goals" / "0001.md"
        goal_path.write_text("---\nid: 0001\nstatus: pending\ntitle: a huge multi-phase goal\n---\n"
                              + _EPIC_BODY)
        rc = lp.main(["loop.py", "decompose-check", str(base), str(goal_path)])
        assert rc == 0
        assert capsys.readouterr().out.startswith("PARKED")


def test_decompose_check_cli_prints_off_when_disabled(capsys):
    lp = _mod("loop")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"
        (base / "goals").mkdir(parents=True)
        (base / "state").mkdir()
        (base / "config.json").write_text(json.dumps({"budget": {"max_iterations": 100}}))
        (base / "state" / "STATE.md").write_text("iteration: 0\nrun_iteration: 0\nlast_run: none\n")
        goal_path = base / "goals" / "0001.md"
        goal_path.write_text("---\nid: 0001\nstatus: pending\ntitle: t\n---\nx\n")
        rc = lp.main(["loop.py", "decompose-check", str(base), str(goal_path)])
        assert rc == 0
        assert capsys.readouterr().out.strip() == "OFF"


def test_decompose_check_is_in_the_usage_string(capsys):
    lp = _mod("loop")
    assert lp.main(["loop.py"]) == 2
    assert "decompose-check <dir> <goal>" in capsys.readouterr().err


# --------------------------------------------------------------------- #522: decompose_goal.py (the
# meta-goal template + filed-marker helper `file` mode drives) -- content pins on the module itself,
# independent of decompose_check's own orchestration (tested in the `file` mode section below).


def test_decompose_meta_body_first_line_is_the_marker():
    dg = _mod("decompose_goal")
    body = dg.render_meta_body("522", 8)
    assert body.splitlines()[0] == "<!-- sigma:decompose-of=#522 -->"


def test_decompose_meta_body_reads_the_marker_constants_live_not_a_hardcoded_copy():
    """Mirrors test_decompose_check_verb_reads_the_marker_constant_live_not_a_hardcoded_copy above:
    the template's own worked examples must read goal_size's marker constants at render time, not a
    hand-typed copy that merely happens to match today."""
    dg = _mod("decompose_goal")
    real_marker = dg.goal_size.DECOMPOSE_OF_MARKER
    dg.goal_size.DECOMPOSE_OF_MARKER = "totally-renamed-marker="
    try:
        body = dg.render_meta_body("522", 8)
        assert body.splitlines()[0] == "<!-- totally-renamed-marker=#522 -->"
    finally:
        dg.goal_size.DECOMPOSE_OF_MARKER = real_marker


def test_decompose_meta_body_interpolates_max_children():
    dg = _mod("decompose_goal")
    body = dg.render_meta_body("522", 5)
    assert "2..5 children" in body


def test_decompose_meta_body_documents_lower_number_wins_tie_break():
    dg = _mod("decompose_goal")
    body = dg.render_meta_body("522", 8)
    assert "lower-number-wins" in body.lower() or "lowest-numbered" in body.lower()
    assert "mutual-abort deadlock" in body


def test_decompose_meta_body_documents_the_track_call_shape():
    dg = _mod("decompose_goal")
    body = dg.render_meta_body("522", 8)
    assert "handoff.py track" in body
    assert "--queue actionable" in body and "--assignee same-area" in body and "--blocks no" in body
    assert "--body-file" in body
    assert "gh issue create" in body and "never" in body   # never gh issue create directly
    assert "<!-- sigma:decomposed-from=#522 -->" in body
    assert "Blocked by" in body


def test_decompose_meta_body_track_call_targets_this_issue_not_the_parent():
    """#522 review fix 1 (THE BLOCKER): create_tracked_issue posts each child's narrative on the
    goal= timeline, and step 0's own reconciliation reads THIS goal's (the meta-goal's OWN)
    timeline as the child ledger. Interpolating the PARENT's number into the track call's goal
    argument would file every child FROM THE PARENT instead -- the meta-goal's own timeline would
    then always be empty, and step 0 could never see its own already-created children. The goal
    argument must be the literal placeholder "<THIS issue>" -- the meta-goal's own number, known
    only once IT is created, never {parent}. Every OTHER {parent} interpolation (area, priority,
    the decomposed-from marker, the parent-closed check) stays exactly as-is -- only the goal
    argument of the track call itself was wrong."""
    dg = _mod("decompose_goal")
    body = dg.render_meta_body("522", 8)
    assert "track <sdlc_dir> <THIS issue>" in body
    assert "track <sdlc_dir> 522" not in body
    # the OTHER {parent} interpolations around the same command must be untouched
    assert "<#522's area>" in body and "<#522's priority>" in body


def test_decompose_meta_body_documents_outcome_check():
    dg = _mod("decompose_goal")
    body = dg.render_meta_body("522", 8)
    assert "gh issue list --label sdlc:goal" in body
    assert "record parked" in body
    assert "loop.py verify" in body and "record done" in body


def test_decompose_meta_body_documents_skip_work_py():
    dg = _mod("decompose_goal")
    body = dg.render_meta_body("522", 8)
    assert "skip" in body.lower() and "work.py" in body


def test_decompose_meta_body_step_0_reconciliation_is_direct_reads_never_search():
    dg = _mod("decompose_goal")
    body = dg.render_meta_body("522", 8)
    assert "CLOSED" in body and "already done" in body
    assert "sdlc:decompose" in body
    assert "never a search" in body.lower() or "never search" in body.lower()


def test_filed_marker_comment_shape():
    dg = _mod("decompose_goal")
    text = dg.filed_marker_comment("531")
    assert text == ("Too large to implement as one goal — decomposition filed as #531. "
                     "<!-- sigma:decompose-filed=#531 -->")


def test_decompose_filed_marker_is_a_bare_substring_of_its_own_comment():
    """The idempotency check in decompose_check's file mode looks for DECOMPOSE_FILED_MARKER as a
    bare substring of a comment body -- prove the constant actually matches what
    filed_marker_comment() posts, so the two can never silently drift apart."""
    dg = _mod("decompose_goal")
    assert dg.DECOMPOSE_FILED_MARKER in dg.filed_marker_comment("531")


# --------------------------------------------------------------------- config-template discoverability


def test_goal_decompose_key_is_discoverable_in_the_scaffolded_config():
    """A feature nobody can find is a feature that doesn't ship (tests/test_config_discoverability.py's
    own framing) — but that file's `_GATE_READ` regex is scoped to `gates.*` sub-keys and will not
    catch a top-level key like `goal_decompose`, so this is a focused, standalone pin instead of an
    extension of that file."""
    tmpl_path = ROOT / "skills" / "sigma-init" / "templates" / "config.json.tmpl"
    tmpl = tmpl_path.read_text(encoding="utf-8")
    cfg = json.loads(tmpl)          # also proves the template is still valid JSON with the new key
    assert cfg.get("goal_decompose") == {"enabled": False, "mode": "log", "max_children": 8}
    assert "_goal_decompose" in cfg
    explainer = cfg["_goal_decompose"]
    # #522: file mode + max_children are now real (positive pin, replacing the old "NOT YET WIRED"
    # placeholder text that was true when this key was first scaffolded, #519).
    assert "NOT YET WIRED" not in explainer
    assert "idempotency-guarded" in explainer and "sigma:decompose-filed" in explainer
    assert "park" in explainer and "log" in explainer


def test_scaffolded_default_config_is_off_end_to_end(tmp_path):
    """Feature invisible unless enabled; zero behavior change with default config — proven against
    the REAL shipped template, not a hand-written stand-in."""
    lp = _mod("loop")
    tmpl_path = ROOT / "skills" / "sigma-init" / "templates" / "config.json.tmpl"
    tmpl_cfg = json.loads(tmpl_path.read_text(encoding="utf-8"))
    base = _sdlc(tmp_path, {"goal_decompose": tmpl_cfg["goal_decompose"]})
    cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
    src = _FakeSource(body=_EPIC_BODY)
    assert lp.decompose_check(base, "1", cfg, src) == "OFF"
    assert src.calls == []


def test_readme_goal_decompose_row_documents_file_mode_and_drops_the_stale_provisional_claim():
    """#522: the row's old "thresholds are PROVISIONAL... prefer log until a follow-up slice
    retunes them" claim was already falsified by #520's corpus calibration slice; the row must also
    now mention `file` mode's real behavior and `max_children`."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    row = next(line for line in readme.splitlines() if line.startswith("| `goal_decompose:"))
    assert "file" in row and "max_children" in row
    assert "PROVISIONAL" not in row


def test_skill_documents_file_mode_files_a_meta_issue():
    skill = skill_corpus("sigma-loop")   # #1611: SKILL.md + references/*.md
    assert "Decompose #" in skill or "meta-issue" in skill.lower()
