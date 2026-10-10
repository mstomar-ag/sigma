"""Shared reader for the changelog pins (#1074).

A release commit moves every entry under `## Unreleased` beneath a new dated heading, so a pin that
looks for "its" entry in Unreleased, or in a fixed number of sections from the top, goes red the
moment the next release lands. Pins read the whole file through `entries_naming`, and
`tests/test_changelog_release_layout.py` replays a release with `simulate_release`.
"""
import re


def entries_naming(text, marker):
    """Every top-level bullet entry, in any section, whose text contains `marker`."""
    return [e for e in re.split(r"\n(?=- \*\*)", text) if e.startswith("- **") and marker in e]


def simulate_release(text, version="9.9.9", date="2099-01-01"):
    """The layout a release commit produces: Unreleased emptied, its entries under a new dated heading."""
    head, sep, rest = text.partition("\n## Unreleased\n")
    assert sep, "no Unreleased heading to release"
    return "%s\n## Unreleased\n\n## %s — %s — simulated release\n%s" % (head, version, date, rest)
