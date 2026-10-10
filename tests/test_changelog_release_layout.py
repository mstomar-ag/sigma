"""#1074 -- every test that reads CHANGELOG.md survives a release commit.

THE CHANGELOG READERS (audited; "survives" = still green after `simulate_release`, once or twice):
  test_upkeep_pins.test_changelog_entry            survives (reads the whole file; was: Unreleased + one section, broke at the 2nd release)
  test_bk_gate (slice 5 entry)                     survives (reads the whole file; was: text above the 1.0.4 heading)
  test_readiness_decide._changelog_entry           survives (any section)
  test_known_false_claims.test_changelog_versions_are_dated   survives (headings only)
  test_release_consistency heading pin             survives (headings only; Unreleased skipped)
  test_release_notes.test_shipped_*                survives (extracts one named version)
  test_manifests first-release-heading pin         survives (skips `## Unreleased`)
Fixture-only readers (tmp_path changelogs in test_feature_rebase, test_union_headed, test_rebase_brief,
test_rename_tools, test_work, ...) never read the shipped file and are not affected.
Ignore-lists that merely skip the file (test_skill_structure, test_readme_first_run, ...) are not pins.
"""
import pathlib

import pytest

import test_bk_gate
import test_known_false_claims as kfc
import test_readiness_decide as rd
import test_release_consistency as rc
import test_upkeep_pins as up
from changelog_layout import entries_naming, simulate_release

ROOT = pathlib.Path(__file__).resolve().parent.parent
REAL = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def _layouts():
    once = simulate_release(REAL, "9.9.8", "2098-01-01")
    return {"as-committed": REAL, "one release": once, "two releases": simulate_release(once, "9.9.9", "2099-01-01")}


@pytest.mark.parametrize("name", ["as-committed", "one release", "two releases"])
def test_every_changelog_pin_passes_after_a_release_commit(name):
    text = _layouts()[name]
    assert up.changelog_entry_failures(text) == []
    assert test_bk_gate.changelog_names_slice_5(text)
    assert len(rd._changelog_entry(text)) > 100
    assert kfc.undated_changelog_headings(text) == []
    headings = [l for l in text.splitlines() if l.startswith("## ") and l != "## Unreleased"]
    assert not rc.heading_problems(headings), rc.heading_problems(headings)


def test_the_simulated_release_really_moves_the_entries():
    """Control on the control: Unreleased is empty and the entries sit under the new dated heading."""
    text = simulate_release(REAL, "9.9.9", "2099-01-01")
    unreleased = text.split("\n## Unreleased\n", 1)[1].split("\n## ", 1)[0]
    assert unreleased.strip() == ""
    assert len(entries_naming(text, "#919")) == len(entries_naming(REAL, "#919")) == 1


def test_the_old_window_pin_shape_goes_red_on_two_releases():
    """The pre-#1074 shape (Unreleased plus the next section only) loses an entry once a release pushes it down."""
    def old_window(text):
        window = "\n".join(text.split("\n## ", 3)[1:3])
        return [e for e in ("\n" + window).split("\n- **") if "#919" in e]
    layouts = _layouts()
    assert len(old_window(layouts["as-committed"])) == 1
    assert old_window(layouts["one release"]) == []
