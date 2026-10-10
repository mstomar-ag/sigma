#!/usr/bin/env python3
"""Question-kind declaration for a park (decision rubric, slice 4 of story 988).

Today the kind of a parked goal is GUESSED from its free text (`loop._reason_class`). This module
lets the parking agent DECLARE the kind instead: `loop.py record <dir> <goal> parked "<text>"
--qkind <k>` appends one machine line to the park comment, and the unpark brief prefers it over the
guess. Omit the flag and nothing changes.

Pure and stdlib only: no I/O, no ledger import (the mapping is checked against the ledger's class
list by a test, so the two cannot drift silently)."""
import re

#: The closed list of declarable kinds. The first nine are the park-reachable reason classes;
#: `scope_hold` and `owner_hold` are the gate-hold kinds (written by the two pick gates through `gate_hold`).
QKINDS = ("irreversible", "needs_decision", "merge_conflict", "failing_check", "no_evidence",
          "dependency", "review_cap", "quota", "unknown", "scope_hold", "owner_hold")

LINE_PREFIX = "sigma-qkind: "
# The source may append a suffix on the same line (a decision-tier tag), so the kind token ends at
# whitespace or end of line, not strictly at end of line.
_LINE = re.compile(r"\n?^" + re.escape(LINE_PREFIX) + r"([a-z_]+)(?=[ \t]|$)", re.MULTILINE)


def from_reason_class(rc):
    """The kind for a ledger reason class. Total: a class that cannot reach a park (`budget`,
    `backlog-empty`, a run-stop class) or is not recognised maps to `unknown`."""
    return rc if rc in QKINDS else "unknown"


def render_line(qkind):
    """The machine line for `qkind`; ValueError for a kind outside `QKINDS`."""
    if qkind not in QKINDS:
        raise ValueError("unknown question kind %r" % (qkind,))
    return LINE_PREFIX + qkind


def parse_line(comment):
    """The declared kind in `comment` (the LAST machine line wins), or None when there is none or
    its kind is not in `QKINDS`."""
    found = _LINE.findall(comment or "")
    return found[-1] if found and found[-1] in QKINDS else None


def strip_line(comment):
    """`comment` without its machine line(s) and trailing blank space."""
    return _LINE.sub("", comment or "").strip()
