"""Follow-ups to the delete-resolution proofs (#1741): visible truncation of the dropped list, short lines never
accounted for elsewhere, a symlinked parent refused, and an accepted-deleted path touched again at a later stop.
Controls (each broken once and seen red, then restored): the record marker, the message marker, the short-line floor,
the parent-link walk, the later-stop refusal. Gesture: `python -m pytest tests/test_upkeep_delete_1741.py -q`."""
import collections
import os

import test_feature_rebase as base
import test_upkeep_level1 as l1
from test_upkeep_agent_1516 import agent_cfg, wired  # noqa: F401
from test_upkeep_delete_1716 import drig  # noqa: F401
from test_upkeep_level2 import APP, FEATURE, mk, rig  # noqa: F401
from test_upkeep_session_1573 import _delete_in_base, run_session

level2 = base._load("feature_upkeep_level2")
res = base._load("feature_upkeep_resolution")
HEX = "a" * 40


def _record(dropped):
    return res.make_record("u", 2, "ab" * 6, HEX, HEX, "ref", {"f": "b" * 64}, {}, dropped_by_delete=dropped)


def test_record_marks_where_the_list_was_cut_by_lines_and_by_chars():
    rows = ["line %04d" % i for i in range(205)] + ["w" * 300]
    got = _record({"p": rows})["dropped_by_delete"]["p"]
    assert len(got) == 201 and got[-1] == "... (6 more lines)"
    assert _record({"p": ["w" * 300]})["dropped_by_delete"]["p"] == ["w" * 200 + "..."]
    assert _record({"p": ["short"]})["dropped_by_delete"] == {"p": ["short"]}      # nothing cut -> unchanged


def test_commit_message_marks_a_clipped_line():
    text = level2.dropped_section({"p": ["w" * 300, "ok"]})
    assert "w" * 200 + "..." in text and "w" * 201 not in text and "    ok" in text


def test_short_lines_are_never_accounted_for_elsewhere():
    added = collections.Counter({"}": 1, "": 1, "  ;": 1, "abc": 1, "a longer line": 1})
    elsewhere = collections.Counter({"}": 5, "abc": 5, "a longer line": 1, ";": 3})
    assert level2.unaccounted_lines(added, elsewhere) == [l for l in sorted(added) if l != "a longer line"]


def test_a_symlinked_parent_directory_is_refused_even_for_a_delete(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "f.txt").write_text("x\n")
    os.symlink(str(tmp_path / "real"), str(tmp_path / "link"))
    assert "symbolic link" in level2.session_problem(tmp_path, "link/f.txt", deletable=True)
    assert "symbolic link" in level2.session_problem(tmp_path, "link/gone.txt", deletable=True)
    assert level2.session_problem(tmp_path, "real/f.txt") is None


def test_a_path_accepted_as_a_delete_and_touched_again_at_a_later_stop_parks(drig, tmp_path):  # noqa: F811
    lines = "line1\nfeature side\nline3\n"
    w = l1.world(mk(tmp_path / "w"), feat={APP: lines, "src/extra.txt": "unrelated\n"}, integ={"other.txt": "x\n"},
                 base_files={"CHANGELOG.md": l1.LOG % "", APP: "line1\nshared\nline3\n"})
    base._git(w.local, "checkout", "-q", FEATURE)
    base._write(w.local / APP, lines + "second\n")
    base._write(w.local / "src/extra2.txt", "more\n")
    base._git(w.local, "add", APP, "src/extra2.txt")
    l1._commit(w.local, "feat: second step (#12)")
    base._git(w.local, "push", "-q", "origin", FEATURE)
    _delete_in_base(w, APP)
    drig.mode({"rm": [APP], "declare": {APP: ["feature side", "second"]}})
    before = w.tip(FEATURE)
    m, w, report, spy = run_session(drig, tmp_path, world=w)
    assert report["outcome"] != m.REBASED and not spy.pushes() and w.tip(FEATURE) == before
    assert "deleted-path-touched-again" in report["why"] and APP in report["why"], report["why"]
