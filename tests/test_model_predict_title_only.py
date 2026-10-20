"""#2827: a haiku-tier signal counts only in the goal's TITLE. #390: a body-only opus/fable
signal needs 2+ hits.

The router used to scan title+body in one pass, and a real issue body almost always quotes a code
comment, names a docstring or cites a lint -- so a non-trivial goal with no opus/fable stem was
routed DOWN to haiku by one body word (a P1 multi-module fix ran at haiku on `comment` alone). Haiku is the
one tier where under-powering costs more than over-powering, so it is the one tier whose stems are
read from the title only. The title is the text's FIRST LINE -- the shape
`loop._predict_model_choice_at_pick` builds (`"<title>\\n\\n<body>"`) and the one documented
fallback gesture prints -- or, for a goal FILE, the title `sources.LocalSource.fetch_title_body`
reads (frontmatter `title:`, else the file stem).

Upward resolution has the mirror failure one tier UP (#390): a body-only opus/fable match used to
win on ONE occurrence; the predecessor's census of its own two boards (its measurement, not
Sigma's) found 58 of 61 opus routings came from a body word, 16 of a 21-issue hand-read a false positive -- a doc-path citation, a test path, a
branch literal, "authorization" meaning spend approval, a JSON key, a shipped-migration
retrospective mention -- each mentioned exactly once. So a body-only opus/fable match now needs 2+
occurrences (`_BODY_MULTI_MIN_HITS`), or a title hit (which still wins on one, like haiku's own
title rule); a match lying entirely inside a backtick code span or a path-shaped token never
counts at all. `predict_with_location` now returns a 4-tuple, `(tier, signal, where, hits)`.
"""
import importlib.util
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
P = ROOT / "skills" / "sigma-model" / "scripts" / "predict.py"


def _mod():
    spec = importlib.util.spec_from_file_location("predict", P)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _sdlc(tmp_path, cfg='{"model_selection": "auto"}'):
    base = tmp_path / ".sdlc"
    base.mkdir()
    (base / "config.json").write_text(cfg)
    return str(base)


@pytest.mark.parametrize("word", ["comment", "comments", "commented", "docstring", "lint",
                                  "renamed", "changelog"])
def test_a_haiku_stem_only_in_the_body_resolves_to_sonnet(word):
    text = (f"Fence the shared write sites across three modules\n\n"
            f"The {word} in the S2 module is wrong and must change with the fence.")
    assert _mod().predict_with_reason(text) == ("sonnet", None)


def test_a_haiku_stem_in_the_title_still_routes_to_haiku():
    m = _mod()
    assert m.predict("fix typo in README") == "haiku"
    assert m.predict_with_location("fix typo in README\n\nThe word is misspelled.") == (
        "haiku", "typo", "title", 1)


def test_opus_and_fable_stems_still_count_in_the_body_with_two_hits():
    """#390: a body-only opus/fable signal now needs 2+ occurrences -- one migration mention
    twice (same stem, real repetition, not a distinct-literal-text requirement) and one goal with
    two DIFFERENT fable stems, each once."""
    m = _mod()
    assert m.predict_with_location(
        "Tidy the module\n\nthis needs a data migration first, and the migration also "
        "touches the billing tables") == ("opus", "migrat", "body", 2)
    assert m.predict_with_location(
        "Tidy the page\n\nrewrite the blog intro, it reads like marketing copy",
        max_tier="fable") == ("fable", "blog", "body", 2)


def test_a_single_body_opus_hit_no_longer_reaches_opus():
    """#390's own routing change, isolated from any title/exclusion interaction: one mention,
    nothing else in the text, falls through every tier to the sonnet default."""
    assert _mod().predict_with_location("Tidy the module\n\nthis needs a data migration") == (
        "sonnet", None, None, 0)


def test_a_body_opus_signal_needs_two_hits_but_a_title_hit_still_wins_on_one():
    """#390 only narrows the BODY-alone path -- a title hit is trusted exactly as before, on one
    occurrence, mirroring #2827's title rule for haiku. A single body "payment" no longer outranks
    the title's own haiku; the title's `typo` wins as haiku, which is the intended new behavior."""
    assert _mod().predict_with_location("Fix the typo\n\nin the payment reconciliation job") == (
        "haiku", "typo", "title", 1)
    # an opus stem IN the title still wins outright, on one occurrence, same as always.
    assert _mod().predict_with_location("Migrate the schema\n\nsome unrelated typo fix") == (
        "opus", "migrat", "title", 1)


def test_title_haiku_loses_to_body_opus_upward_resolution_intact():
    """Rewritten for #390 (was one body "payment"): upward resolution is intact only once the body
    signal clears its own threshold, so the opus signal is now TWO body hits -- a title haiku
    still loses to it."""
    assert _mod().predict_with_location(
        "Fix the typo\n\nin the payment reconciliation job, a payment edge case") == (
        "opus", "payment", "body", 2)


# ---- #390: issue's own false-positive shapes, scrubbed (no host-project names) -----------------
# Each is exactly ONE qualifying mention -- the shape the issue's evidence describes as the root
# cause of the false positives in the predecessor's hand-read. Each was seen RED against the pre-#390
# code (single-hit body match won opus outright): every one of these returned `("opus", ..., "body")`
# before this fix (control A/B transcripts are in the PR description). GREEN after: every one falls through to sonnet.

_FALSE_POSITIVE_SHAPES = {
    "doc_path_citation_backtick": (
        "x\n\nSee `docs/context/architecture/pipeline.md` for the full design."),
    "test_path_bare": (
        "x\n\nCovered by tests/architecture/test_thing.py already."),
    "branch_literal_bare": (
        "x\n\nCut from feature/auth-billing-security for this slice."),
    "authorization_meaning_spend_approval": (
        "x\n\nThis needs spend authorization from finance before it can merge."),
    "json_key_backtick": (
        "x\n\nThe config sets `\"complexity\": 3` for this module."),
    "migrat_in_a_shipped_retrospective": (
        "x\n\nFor context, the migration already shipped last quarter per the retro notes."),
}


@pytest.mark.parametrize("shape", sorted(_FALSE_POSITIVE_SHAPES))
def test_390_false_positive_shapes_no_longer_reach_opus(shape):
    tier, signal, where, hits = _mod().predict_with_location(_FALSE_POSITIVE_SHAPES[shape])
    assert (tier, signal, where, hits) == ("sonnet", None, None, 0), shape


def test_390_true_positives_still_reach_opus():
    """The issue's own counter-examples: a real migration and a real concurrency bug, each
    carrying its signal repeated (or as two distinct alternatives), not incidental."""
    m = _mod()
    assert m.predict_with_location(
        "x\n\nThis migrates the legacy schema. The migration touches three services.") == (
        "opus", "migrat", "body", 2)
    assert m.predict_with_location(
        "x\n\nThere is a real concurrency bug here: a race condition in the writer causes "
        "corruption.") == ("opus", "concurren", "body", 2)


def test_390_exactly_two_hits_is_the_threshold_not_only_three_or_more():
    assert _mod().predict_with_location("x\n\nmigrat migrat") == ("opus", "migrat", "body", 2)


def test_390_a_path_citation_does_not_swallow_adjacent_real_prose():
    """The exclusion span only covers the path-shaped token ITSELF -- a genuine second mention of
    the same stem sitting outside it still counts toward the threshold."""
    assert _mod().predict_with_location(
        "x\n\nsee docs/architecture/pipeline.md but this really is an architecture change, we "
        "need to architect it properly") == ("opus", "architect", "body", 2)


def test_390_a_fenced_code_block_is_excluded_too_not_only_inline_backticks():
    assert _mod().predict_with_location(
        "x\n\n```\nthis needs a data migration\nthe migration touches billing\n```") == (
        "sonnet", None, None, 0)


@pytest.mark.parametrize("title", [
    "Fix `migration` handling",
    "Rework feature/auth-billing",
    "Update docs/security/threat-model.md",
])
def test_390_a_match_inside_a_span_never_counts_in_the_TITLE_either(title):
    """Review send-back: every earlier exclusion test put the span in the body, so limiting the
    exclusion to the body passed them all. The issue says 'in title or body'."""
    assert _mod().predict_with_location(title + "\n\nplain body") == ("sonnet", None, None, 0)


def test_390_containment_not_overlap_a_match_straddling_a_span_edge_still_counts():
    """A match starting inside a path token but ending outside it (a backtick cannot sit inside a
    match, a space can) is not wholly a citation: it counts. Two of them reach the threshold. An
    overlap test would exclude both (review mutation)."""
    assert _mod().predict_with_location(
        "x\n\nfeature/race condition and other/race condition") == (
        "opus", "race condition", "body", 2)


def test_390_an_unmatched_backtick_does_not_swallow_the_rest_of_the_line():
    assert _mod().predict_with_location(
        "x\n\nan unmatched ` backtick, then migrat and migrat") == ("opus", "migrat", "body", 2)


def test_390_path_token_scan_is_linear_on_a_long_slash_free_blob():
    """The `\\S+/\\S+` regex was quadratic here (57 s at 200 KB when this guard was run red against it)."""
    import time
    blob = "x" * 200_000
    t0 = time.monotonic()
    assert _mod().predict_with_location("x\n\n" + blob) == ("sonnet", None, None, 0)
    assert time.monotonic() - t0 < 5.0


def test_390_why_reports_the_hit_count(capsys):
    m = _mod()
    text = "x\n\nThe migration touches billing. A second migration note follows too."
    assert m.main(["predict.py", "why", text]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "model=opus in=body hits=2 signal=migrat"


def test_a_body_haiku_is_not_rescued_when_the_title_haiku_is_excluded():
    text = "Fix the lint config\n\nthe comment above it is stale"
    assert _mod().predict_with_reason(text, ("lint",)) == ("sonnet", None)


def test_the_price_ceiling_still_binds_the_default():
    text = "Fence the write sites\n\nthe comment is stale"
    assert _mod().predict_with_reason(text, max_tier="haiku") == ("haiku", None)


def test_an_empty_first_line_is_an_empty_title_never_lstripped():
    """A leading blank line means "no title"; stripping it would promote the body's first line."""
    assert _mod().predict("\nfix typo\n\nbody") == "sonnet"
    assert _mod().predict("\n\n---\ntitle: fix typo\n---\n") == "sonnet"


def test_a_crlf_title_line_is_still_a_title():
    assert _mod().predict_with_location("fix typo in README\r\n\r\nbody") == (
        "haiku", "typo", "title", 1)


def test_one_line_json_is_all_title_which_is_why_the_documented_gesture_is_not_gh_json():
    """`gh issue view --json title,body` prints one-line JSON: no newline, so the body is read as
    title. Pinned so nobody re-documents that gesture as the fallback."""
    blob = '{"body":"the comment is stale","title":"Fence the write sites"}'
    assert _mod().predict(blob) == "haiku"


def test_resolve_step_keeps_whole_text_haiku_semantics(tmp_path):
    """Out of scope (#2827): a mechanical STEP's downgrade is resolve-step's purpose."""
    base = _sdlc(tmp_path)
    assert _mod().resolve_step("Restructure the loader\nthen fix the comment", base) == {
        "model": "haiku", "effort": "low"}


# ---- CLI: the gestures the docs give ------------------------------------------------------------

def _documented_fallback_shape(title, body):
    # what the documented `gh issue view N --json title,body --jq '.title + "\n\n" + (.body // "")'`
    # prints (run live against a real issue during #2827's implement phase)
    return f"{title}\n\n{body}"


def test_why_reports_where_the_signal_was_found(capsys):
    m = _mod()
    assert m.main(["predict.py", "why", _documented_fallback_shape("Fix typo", "x")]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "model=haiku in=title hits=1 signal=typo"
    # #390: a single body-only "race condition" mention no longer clears the 2+-hit threshold.
    assert m.main(["predict.py", "why",
                   _documented_fallback_shape("Tidy", "the race condition")]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "model=sonnet signal="
    # ... but two distinct opus alternatives, one occurrence each, do.
    assert m.main(["predict.py", "why",
                   _documented_fallback_shape(
                       "Tidy", "the race condition and general concurrency issues")]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "model=opus in=body hits=2 signal=race condition"
    assert m.main(["predict.py", "why", _documented_fallback_shape("Tidy", "the comment")]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "model=sonnet signal="


def test_resolve_on_the_documented_fallback_shape_ignores_a_body_haiku(tmp_path, capsys):
    base = _sdlc(tmp_path)
    text = _documented_fallback_shape("Fence the shared write sites", "The S2 comment is wrong")
    assert _mod().main(["predict.py", "resolve", text, base]) == 0
    assert capsys.readouterr().out.strip() == "sonnet"


def _goal_file(tmp_path, name, content, newline="\n"):
    f = tmp_path / name
    f.write_bytes(content.replace("\n", newline).encode("utf-8"))
    return str(f)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_a_goal_file_title_is_its_frontmatter_title(tmp_path, capsys, newline):
    base = _sdlc(tmp_path)
    trivial = _goal_file(tmp_path, "0001-a.md",
                         "---\nid: 0001\ntitle: Fix typo in README\nstatus: pending\n---\n\n"
                         "The word is misspelled.\n", newline)
    real = _goal_file(tmp_path, "0002-b.md",
                      '---\nid: 0002\ntitle: "Fence the shared write sites"\n---\n\n'
                      "The S2 comment is wrong.\n", newline)
    m = _mod()
    assert m.main(["predict.py", "resolve", trivial, base]) == 0
    assert capsys.readouterr().out.strip() == "haiku"
    assert m.main(["predict.py", "resolve", real, base]) == 0
    assert capsys.readouterr().out.strip() == "sonnet"
    assert m.main(["predict.py", "why", trivial, base]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "model=haiku in=title hits=1 signal=typo"


def test_a_subtitle_key_is_not_the_title(tmp_path, capsys):
    base = _sdlc(tmp_path)
    f = _goal_file(tmp_path, "0004-plain.md",
                   "---\nsubtitle: fix typo\ntitle: Fence the write sites\n---\nbody\n")
    assert _mod().main(["predict.py", "resolve", f, base]) == 0
    assert capsys.readouterr().out.strip() == "sonnet"


def test_a_goal_file_without_a_title_key_falls_back_to_its_stem(tmp_path, capsys):
    """Same fallback as `sources.LocalSource.fetch_title_body`, so the documented local fallback
    (`resolve "$goal"`) and the tier the pick recorded cannot disagree."""
    base = _sdlc(tmp_path)
    no_key = _goal_file(tmp_path, "0003-fix-typo.md", "---\nid: 0003\n---\nthe comment\n")
    no_fence = _goal_file(tmp_path, "0005-wire-flag.md", "Wire the flag.\nthe comment is stale\n")
    bom = _goal_file(tmp_path, "0006-wire-thing.md", "﻿---\ntitle: fix typo\n---\nbody\n")
    m = _mod()
    assert m.main(["predict.py", "resolve", no_key, base]) == 0
    assert capsys.readouterr().out.strip() == "haiku"
    assert m.main(["predict.py", "resolve", no_fence, base]) == 0
    assert capsys.readouterr().out.strip() == "sonnet"
    assert m.main(["predict.py", "resolve", bom, base]) == 0      # fence unrecognised -> stem
    assert capsys.readouterr().out.strip() == "sonnet"


def test_a_goal_file_body_still_reaches_opus(tmp_path, capsys):
    """#390: needs 2+ body occurrences now -- the frontmatter `done_when:` and the prose body
    each mention the migration once, matching how `_read_goal` keeps the whole raw file (fence
    included) as the body (see its own docstring)."""
    base = _sdlc(tmp_path)
    f = _goal_file(tmp_path, "0007-x.md",
                   "---\ntitle: Tidy things\ndone_when: \"the migration passes\"\n---\n"
                   "the migration also needs a data backfill\n")
    assert _mod().main(["predict.py", "resolve", f, base]) == 0
    assert capsys.readouterr().out.strip() == "opus"


def test_an_empty_title_value_falls_back_to_the_stem_and_the_last_title_key_wins(tmp_path, capsys):
    """Parity with `frontmatter.get(text, "title") or path.stem`: an empty value is no title, and
    `frontmatter.parse` keeps the LAST `title:` line."""
    base = _sdlc(tmp_path)
    empty = _goal_file(tmp_path, "0008-fix-typo.md", '---\ntitle: ""\n---\nthe body\n')
    last = _goal_file(tmp_path, "0009-x.md", "---\ntitle: fix typo\ntitle: Fence writes\n---\nb\n")
    m = _mod()
    assert m.main(["predict.py", "resolve", empty, base]) == 0
    assert capsys.readouterr().out.strip() == "haiku"
    assert m.main(["predict.py", "resolve", last, base]) == 0
    assert capsys.readouterr().out.strip() == "sonnet"
