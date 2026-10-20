"""#1949: upkeep's mechanical level also unions two-sided additions under `## YYYY-MM-DD ...` headings (a decision-record
index) and bullet lists inside one such section. Edits and removals still fall through. Offline: pure functions."""
import upkeep_support as support

proof = support.script("conflict_proof")

BASE = "# Decisions\n\n## 2026-01-05 — alpha\n\n- a1\n\n## 2026-01-09 — beta\n\n- b1\n"


def union(b, o, t):
    return proof.append_union(b, o, t)


def test_both_sides_add_new_day_entries_in_date_order():
    o = BASE + "\n## 2026-03-01 — ours\n\n- o1\n"
    t = BASE + "\n## 2026-02-01 — theirs\n\n- t1\n"
    assert union(BASE, o, t) == BASE + "\n## 2026-02-01 — theirs\n\n- t1\n\n## 2026-03-01 — ours\n\n- o1\n"


def test_two_entries_on_one_date_both_survive():
    o = BASE + "\n## 2026-02-01 — ours\n\n- o1\n"
    t = BASE + "\n## 2026-02-01 — theirs\n\n- t1\n"
    got = union(BASE, o, t)
    assert got is not None and "— ours" in got and "— theirs" in got
    assert got.index("— ours") < got.index("— theirs")


def test_bullets_added_inside_one_day_section_by_both_sides():
    o = BASE.replace("- a1\n", "- a1\n- ours\n")
    t = BASE.replace("- a1\n", "- a1\n- theirs\n")
    got = union(BASE, o, t)
    assert got == BASE.replace("- a1\n", "- a1\n- ours\n- theirs\n")


def test_descending_index_keeps_descending_order():
    b = "## 2026-01-09 — beta\n\n- b\n\n## 2026-01-05 — alpha\n\n- a\n"
    o = "## 2026-03-01 — ours\n\n- o\n\n" + b
    t = "## 2026-02-01 — theirs\n\n- t\n\n" + b
    got = union(b, o, t)
    assert got is not None
    assert [ln for ln in got.split("\n") if ln.startswith("## ")] == [
        "## 2026-03-01 — ours", "## 2026-02-01 — theirs", "## 2026-01-09 — beta", "## 2026-01-05 — alpha"]


def test_edit_of_an_existing_line_parks():
    o = BASE.replace("- a1", "- a1 edited")
    t = BASE + "\n## 2026-02-01 — theirs\n\n- t1\n"
    assert union(BASE, o, t) is None


def test_removal_parks():
    o = BASE.replace("\n## 2026-01-09 — beta\n\n- b1\n", "")
    t = BASE + "\n## 2026-02-01 — theirs\n\n- t1\n"
    assert union(BASE, o, t) is None


def test_edited_heading_parks():
    o = BASE.replace("alpha", "alpha2") + "\n## 2026-02-01 — ours\n\n- o1\n"
    t = BASE + "\n## 2026-02-02 — theirs\n\n- t1\n"
    assert union(BASE, o, t) is None


def test_same_day_heading_added_twice_parks():
    o = BASE + "\n## 2026-02-01 — x\n\n- o1\n\n## 2026-02-01 — x\n\n- o2\n"
    t = BASE + "\n## 2026-02-02 — y\n\n- t1\n"
    assert union(BASE, o, t) is None


def test_one_side_adds_two_entries_on_one_date():
    o = BASE + "\n## 2026-02-01 — x\n\n- o1\n\n## 2026-02-01 — y\n\n- o2\n"
    t = BASE + "\n## 2026-02-02 — z\n\n- t1\n"
    got = union(BASE, o, t)
    assert got is not None
    assert [ln for ln in got.split("\n") if ln.startswith("## 2026-02")] == [
        "## 2026-02-01 — x", "## 2026-02-01 — y", "## 2026-02-02 — z"]


def test_no_existing_headings_new_ones_come_out_in_date_order():
    b = "# Decisions\n"
    o = b + "\n## 2026-03-01 — ours\n\n- o\n"
    t = b + "\n## 2026-02-01 — theirs\n\n- t\n"
    got = union(b, o, t)
    assert got is not None and got.index("theirs") < got.index("ours")
