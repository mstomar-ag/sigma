"""#1103 -- level 1 (`work._union_headed`) across a release heading.

The base moved the old Unreleased entries under a new dated version heading and added one entry under
Unreleased; the unit added an entry under Unreleased. Real git: the conflict text is whatever git emits.
"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_feature_rebase as tfr  # noqa: E402
from test_feature_rebase import FEATURE, World, _git, _load, _write  # noqa: E402
from test_union_headed import CUT, OPEN, _nonblank, _remote_changelog, _under  # noqa: E402

SEED = "# Changelog\n\n## Unreleased\n\n- a1\n- a2\n\n## 1.0.3 %s 2026-01-01\n\n- old\n" % CUT


def _unit(entries):
    return SEED.replace("- a1\n", "".join("- %s\n" % e for e in entries) + "- a1\n")


def _base(extra=("b1",)):
    new = "".join("- %s\n" % e for e in extra)
    return SEED.replace(
        "## Unreleased\n\n- a1\n- a2\n",
        "## Unreleased\n\n%s\n## 1.0.4 %s 2026-02-02\n\n- a1\n- a2\n" % (new, CUT))


def _world(tmp_path, unit_text, base_text):
    world = World(tmp_path).build(feature_commits=(("CHANGELOG.md", SEED.rstrip("\n"), "seed changelog"),))
    world.goal_branch(11, files=(("CHANGELOG.md", unit_text.rstrip("\n")),))
    _git(world.local, "checkout", "-q", FEATURE)
    _write(world.local / "CHANGELOG.md", base_text)
    _git(world.local, "commit", "-qam", "cut 1.0.4")
    _git(world.local, "push", "-q", "origin", FEATURE)
    _git(world.local, "checkout", "-q", tfr.INTEGRATION)
    tfr._register_agent(world.sdlc, 11, 999999)
    return world


def _run(world):
    cfg = tfr._cfg()
    cfg.update(OPEN)
    return _load("work").rebase(str(world.sdlc), cfg, "11")


def test_release_heading_case_files_unit_entries_under_unreleased(tmp_path):
    world = _world(tmp_path, _unit(["u1", "u2"]), _base())
    out = _run(world)
    assert out.startswith("rebased"), out
    text = _remote_changelog(world)
    assert _under(text, "- u1") == _under(text, "- u2") == _under(text, "- b1") == "## Unreleased", text
    assert text.index("- b1") < text.index("- u1") < text.index("- u2") < text.index("## 1.0.4")
    assert _under(text, "- a1").startswith("## 1.0.4") and _under(text, "- a2").startswith("## 1.0.4")
    assert _nonblank(text) == _nonblank(
        _base().replace("- b1\n", "- b1\n- u1\n- u2\n"))                    # nothing lost, nothing duplicated
    assert text.count("- u1") == text.count("- a1") == 1


def test_out_of_grammar_changelog_edit_still_parks(tmp_path):
    unit = _unit(["u1"]).replace("- old", "- old\n\n[1.0.3]: https://example.invalid/x")
    world = _world(tmp_path, unit, _base().replace("- old", "- old\n\n[1.0.3]: https://example.invalid/y"))
    before = world.tip("sdlc/11")
    assert _run(world).startswith("rebase deferred")
    assert world.tip("sdlc/11") == before


def test_unit_edit_of_an_existing_base_line_still_parks(tmp_path):
    unit = SEED.replace("- a1", "- a1 edited")
    world = _world(tmp_path, unit, _base())
    before = world.tip("sdlc/11")
    assert _run(world).startswith("rebase deferred")
    assert world.tip("sdlc/11") == before


def test_conflict_in_another_file_still_parks(tmp_path):
    world = World(tmp_path).build(feature_commits=(("CHANGELOG.md", SEED.rstrip("\n"), "seed"),
                                                   ("f.txt", "one", "f")))
    world.goal_branch(11, files=(("CHANGELOG.md", _unit(["u1"]).rstrip("\n")), ("f.txt", "unit")))
    _git(world.local, "checkout", "-q", FEATURE)
    _write(world.local / "CHANGELOG.md", _base())
    _write(world.local / "f.txt", "base")
    _git(world.local, "commit", "-qam", "cut")
    _git(world.local, "push", "-q", "origin", FEATURE)
    _git(world.local, "checkout", "-q", tfr.INTEGRATION)
    tfr._register_agent(world.sdlc, 11, 999999)
    before = world.tip("sdlc/11")
    assert _run(world).startswith("rebase deferred")
    assert world.tip("sdlc/11") == before


# --- the function itself: the diff3 shape with a NON-empty base (the old entries moved under the release) ---


def _hunk(ours, base, theirs):
    return ("# Changelog\n\n## Unreleased\n\n<<<<<<< HEAD\n%s||||||| base\n%s=======\n%s>>>>>>> sdlc/1-x\n"
            "\n## 1.0.3 %s 2026-01-01\n\n- old\n" % (
                "".join(x + "\n" for x in ours), "".join(x + "\n" for x in base),
                "".join(x + "\n" for x in theirs), CUT))


REL = ["- b1", "", "## 1.0.4 %s 2026-02-02" % CUT, "", "- a1", "- a2"]
BASE = ["- a1", "- a2"]


def test_moved_entries_unit_entries_go_under_unreleased_base_sections_untouched():
    work = _load("work")
    text, ok = work._union_headed(_hunk(REL, BASE, ["- a1", "- a2", "- u1", "- u2"]))
    assert ok
    assert text.index("- b1") < text.index("- u1") < text.index("- u2") < text.index("## 1.0.4")
    assert _under(text, "- u1") == _under(text, "- u2") == "## Unreleased"
    assert "## 1.0.4 %s 2026-02-02\n\n- a1\n- a2\n\n## 1.0.3" % CUT in text
    assert text.count("- a1") == text.count("- a2") == 1 and "<<<<" not in text and "||||" not in text
    assert _nonblank(text) == sorted(["# Changelog", "## Unreleased", "- b1", "## 1.0.4 %s 2026-02-02" % CUT,
                                      "- a1", "- a2", "- u1", "- u2", "## 1.0.3 %s 2026-01-01" % CUT, "- old"])


def test_moved_entries_unit_entry_before_the_old_ones_keeps_order():
    work = _load("work")
    text, ok = work._union_headed(_hunk(REL, BASE, ["- u1", "- a1", "- a2"]))
    assert ok and _under(text, "- u1") == "## Unreleased" and text.count("- u1") == 1


def test_moved_entries_entry_already_on_base_is_dropped_by_text():
    work = _load("work")
    text, ok = work._union_headed(_hunk(REL, BASE, ["- a1", "- a2", "- b1", "- u2"]))
    assert ok and text.count("- b1") == 1 and text.count("- u2") == 1
    assert _under(text, "- u2") == "## Unreleased"


def test_moved_entries_nothing_new_returns_the_base_side_exactly():
    work = _load("work")
    text, ok = work._union_headed(_hunk(REL, BASE, ["- a1", "- a2", "- b1"]))
    assert ok
    assert text.count("- b1") == 1 and "- a1\n- a2\n\n## 1.0.3" in text


@pytest.mark.parametrize("ours,base,theirs", [
    (REL, BASE, ["- a1", "- a2 edited", "- u1"]),                 # the unit edited an existing line
    (REL, BASE, ["- a1", "- u1"]),                                # the unit deleted an existing line
    (REL, BASE, ["- a1", "- a2", "### Added", "- u1"]),           # not an entry
    (REL, BASE, ["- a1", "- a2", "[1.0.4]: https://example.invalid/x"]),   # link footer
    (REL, BASE, ["- a1", "- a2", "## 1.0.5 %s d" % CUT]),        # a heading on the unit side
    (["- b1", "", "- a1", "- a2"], BASE, ["- a1", "- a2", "- u1"]),    # no release heading in ours
    (["- b1", "", "## [1.0.4] - d", "", "- a1", "- a2"], BASE, ["- a1", "- a2", "- u1"]),  # off-grammar heading
    (["- b1", "", "## 1.0.4 %s d" % CUT, "", "- a1"], BASE, ["- a1", "- a2", "- u1"]),  # ours lost a2
    (REL, [""], ["- u1"]),                                        # blank-only base is not this shape
])
def test_moved_entries_everything_else_still_parks(ours, base, theirs):
    assert _load("work")._union_headed(_hunk(ours, base, theirs)) == (None, False)


def test_moved_entries_only_under_unreleased_and_legacy_union_unchanged():
    work = _load("work")
    text = _hunk(REL, BASE, ["- a1", "- a2", "- u1"]).replace("## Unreleased", "## 1.0.2 %s d" % CUT, 1)
    assert work._union_headed(text) == (None, False)
    assert work._union_diff3(_hunk(REL, BASE, ["- a1", "- a2", "- u1"])) == (None, False)
