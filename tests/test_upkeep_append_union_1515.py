"""#1515: upkeep's mechanical level also resolves two append-only shapes (a dated `## YYYY-MM` log and a bullet-list
file) when both sides purely ADDED lines. Edits and removals fall through. Offline: pure functions, plus real git in
scratch repositories through the upkeep entry point, with no model call and no network call."""
import pytest

import test_feature_rebase as base
import test_upkeep_level1 as lv
import upkeep_support as support

proof = support.script("conflict_proof")
LOG_BASE = "# Decisions\n\n## 2026-01\n\n- a\n"
BULLETS = "- one\n- two\n"


def union(b, o, t):
    return proof.append_union(b, o, t)


# ------------------------------------------------------------------ pure shapes

def test_dated_log_both_sides_append_in_one_section_and_in_new_sections():
    b = LOG_BASE
    o = b + "- ours-a\n\n## 2026-03\n\n- ours-march\n"
    t = b + "- theirs-a\n\n## 2026-02\n\n- theirs-feb\n"
    got = union(b, o, t)
    assert got == ("# Decisions\n\n## 2026-01\n\n- a\n- ours-a\n- theirs-a\n\n## 2026-02\n\n- theirs-feb\n\n"
                   "## 2026-03\n\n- ours-march\n")


def test_dated_log_sections_follow_a_descending_log():
    b = "## 2026-02\n\n- b\n\n## 2026-01\n\n- a\n"
    got = union(b, "## 2026-03\n\n- o\n\n" + b, b + "- t\n")
    assert got.index("## 2026-03") < got.index("## 2026-02") < got.index("## 2026-01")


def test_bullet_list_keeps_base_order_then_the_new_items():
    got = union(BULLETS, "- one\n- two\n- ours\n", "- one\n- two\n- theirs\n- ours\n")
    assert got == "- one\n- two\n- ours\n- theirs\n"


@pytest.mark.parametrize("kind", ["theirs-edits", "ours-removes", "theirs-removes"])
def test_an_edit_or_removal_falls_through(kind):
    o, t = BULLETS + "- ours\n", BULLETS + "- theirs\n"
    if kind == "theirs-edits":
        t = "- one\n- TWO\n- theirs\n"
    elif kind == "ours-removes":
        o = "- one\n- ours\n"
    else:
        t = "- two\n- theirs\n"
    assert union(BULLETS, o, t) is None


@pytest.mark.parametrize("b,o,t", [
    ("plain\n", "plain\nx\n", "plain\ny\n"),                          # prose, no recognised shape
    ("- a\n", "- a\nprose\n", "- a\n- t\n"),                          # a non-bullet line in a bullet file
    ("", "- o\n", "- t\n"),                                           # no base content: add/add
    ("## 2026-01\n- a\n", "## 2026-01\n- a\n", "## 2026-01\n- a\n"),  # nothing added
])
def test_non_matching_shapes_are_not_unioned(b, o, t):
    assert union(b, o, t) is None


def test_the_would_drop_check_rejects_a_lossy_result():
    # a union that silently drops a unit line (the dedup swallowing a distinct line) must be refused, not returned
    assert proof.would_drop("- a\n", "- a\n", "- b\n") == ["- b"]
    assert proof.would_drop("- a\n- b\n", "- a\n", "- a\n- b\n") == []


# ------------------------------------------------------------------ real git, through the upkeep entry point

def _scenario(tmp_path, monkeypatch, name, b, feat, integ):
    m = lv.mod()
    w = lv.world(tmp_path, base_files={name: b}, feat={name: feat}, integ={name: integ})
    before = w.tip(lv.FEATURE)
    lv.verify_ok(m, monkeypatch)
    report, spy = lv.run_pass(m, w, lv.cfg())
    return m, w, before, report, spy


def test_both_sides_append_to_a_decisions_log_resolved_with_backup_and_trailer(tmp_path, monkeypatch):
    m, w, before, report, spy = _scenario(tmp_path, monkeypatch, "decisions.md", LOG_BASE,
                                          LOG_BASE + "- unit\n", LOG_BASE + "- upstream\n\n## 2026-02\n\n- feb\n")
    assert report["outcome"] == m.REBASED and report["resolved"]["level"] == 1, report.get("why")
    assert len(spy.pushes()) == 1 and report["backup"] in base._git(w.local, "ls-remote", "origin")
    text = base._git(w.local, "show", "origin/%s:decisions.md" % lv.FEATURE)
    assert text + "\n" == LOG_BASE + "- upstream\n- unit\n\n## 2026-02\n\n- feb\n"   # _git strips the last newline
    assert report["resolved"]["files"].get("decisions.md")
    assert lv.remote_log(w).rstrip().splitlines()[-1] == "sigma-resolution: 1 " + report["resolved"]["run_id"]


def test_a_bullet_list_both_sides_add_is_resolved_in_order(tmp_path, monkeypatch):
    m, w, before, report, spy = _scenario(tmp_path, monkeypatch, "items.md", BULLETS, BULLETS + "- unit\n",
                                          BULLETS + "- upstream\n")
    assert report["outcome"] == m.REBASED, report.get("why")
    assert base._git(w.local, "show", "origin/%s:items.md" % lv.FEATURE) + "\n" == BULLETS + "- upstream\n- unit\n"


def test_a_bullet_list_where_a_side_edited_a_line_parks_untouched(tmp_path, monkeypatch):
    m, w, before, report, spy = _scenario(tmp_path, monkeypatch, "items.md", BULLETS, "- one\n- TWO\n- unit\n",
                                          BULLETS + "- upstream\n")
    assert report["outcome"] == m.PARKED and w.tip(lv.FEATURE) == before and not spy.pushes()
    assert "level 1 refused" not in str(report.get("why")), "the stop must not enter Level 1 at all"
