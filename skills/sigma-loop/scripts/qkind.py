#!/usr/bin/env python3
"""Question-kind declaration for a park (rulebook, slice 4 of story 988).

Today the kind of a parked goal is GUESSED from its free text (`loop._reason_class`). This module
lets the parking agent DECLARE the kind instead: `loop.py record <dir> <goal> parked "<text>"
--qkind <k>` appends one machine line to the park comment, and the unpark brief prefers it over the
guess. Omit the flag and nothing changes.

Pure and stdlib only: no I/O, no ledger import (the mapping is checked against the ledger's class
list by a test, so the two cannot drift silently)."""
import datetime
import re
import sys

#: The closed list of declarable kinds. The first nine are the park-reachable reason classes;
#: `scope_hold` and `owner_hold` are the gate-hold kinds (written by the two pick gates through `gate_hold`);
#: `reviewer_down` is a park, not a label: the independent reviewer failed (docs/label-model.md §2c);
#: `review_post_ambiguous`: a review post's outcome is unknown, so no new generation is published (§2b-iv);
#: `label_set` (#1578) is the up-front route question for a whole run label, never declared on a park.
QKINDS = ("irreversible", "needs_decision", "merge_conflict", "failing_check", "no_evidence",
          "dependency", "review_cap", "quota", "unknown", "scope_hold", "owner_hold", "reviewer_down",
          "review_post_ambiguous", "label_set")

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


HARDSTOP_PREFIX = "sigma-hardstop: "
_HARDSTOP = re.compile(r"^" + re.escape(HARDSTOP_PREFIX) + r"([^\n]+)$", re.MULTILINE)
DECLARATION_PREFIX = "Question kind set: "
_TRUSTED = ("OWNER", "MEMBER", "COLLABORATOR")     # mirrors sources.TRUSTED_ASSOCIATIONS (tests pin it)
_SIGMA_OPENERS = ("Parked by Sigma", "Failed in the Sigma loop", "<!-- sigma:migrate-confirmation -->",
                  "Parked at filing")


def render_hardstop(cls):
    """The machine line naming the hard-stop class a park matched (one line, no newline)."""
    return HARDSTOP_PREFIX + " ".join(str(cls).split())


def parse_hardstop(comment):
    """The hard-stop class named by the LAST `sigma-hardstop:` line in `comment`, or None."""
    found = _HARDSTOP.findall(comment or "")
    return found[-1].strip() if found else None


def _honoured(c, goal, note=True):
    """True when comment `c` may declare a kind: a bare string (Sigma's own text, already trusted by
    the caller) or any comment by an OWNER/MEMBER/COLLABORATOR. Otherwise
    False, with one stderr note when `note`."""
    if isinstance(c, str):
        return True
    assoc = c.get("authorAssociation")
    if assoc in _TRUSTED:
        return True
    if note:
        login = (c.get("author") or {}).get("login") if isinstance(c.get("author"), dict) else None
        sys.stderr.write("sigma: ignoring a sigma-qkind line in a comment on #%s from %s (%s): only "
                         "OWNER, MEMBER or COLLABORATOR declarations are honoured\n"
                         % (goal, login or "an unknown commenter", assoc or "no association"))
    return False


def _body(c):
    return c if isinstance(c, str) else ((c or {}).get("body") or "")


def resolve(comments, goal=None):
    """The declared kind on an issue: the newest kind line among Sigma's own park/migration comments and
    declaration comments by an OWNER/MEMBER/COLLABORATOR; None when there is none. A comment is a dict
    (`body`, `authorAssociation`, `author.login`) or a bare string (Sigma's own text). A kind line from
    anyone else is ignored with one stderr note; an absent association is untrusted (fail closed)."""
    found = None
    for c in comments or []:
        kind = parse_line(_body(c))
        if kind is not None and _honoured(c, goal):
            found = kind
    return found


def resolve_hardstop(comments, goal=None):
    """The newest hard-stop class line from a comment `_honoured` accepts (no note), else None."""
    found = None
    for c in comments or []:
        cls = parse_hardstop(_body(c))
        if cls is not None and _honoured(c, goal, note=False):
            found = cls
    return found


# ---- #1563: a quota park that names its reset time ------------------------------------------------

RESET_PREFIX = "sigma-reset-at: "
_RESET_LINE = re.compile(r"^" + re.escape(RESET_PREFIX) + r"([^\n]+?)[ \t]*$", re.MULTILINE)
MAX_RESET_DAYS = 7
_ISO = r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"
_RETRY_AFTER = re.compile(r"retry[- ]after\W{0,3}(\d{1,9})\b", re.IGNORECASE)
_IN = re.compile(r"resets?\s+in\s+(\d{1,9})\s*(second|sec|minute|min|hour|hr|h|m|s)s?\b", re.IGNORECASE)
_AT = re.compile(r"(?:resets?\s+at|reset\W{0,3})\s*[:=]?\s*(" + _ISO + r"|\d{10})\b", re.IGNORECASE)
_UNIT = {"s": 1, "sec": 1, "second": 1, "m": 60, "min": 60, "minute": 60, "h": 3600, "hr": 3600, "hour": 3600}


def _aware(t):
    return t if t.tzinfo else t.replace(tzinfo=datetime.timezone.utc)


def parse_reset(text, now):
    """When the quota named in `text` resets, as an aware datetime; None when no time is named, the text is
    unreadable, the time is not after `now`, or it is more than `MAX_RESET_DAYS` away. Reads `Retry-After: N`
    (seconds), `resets in N minutes|hours|seconds`, and `resets at` / `reset:` followed by an ISO time or a
    10-digit epoch. Pure; never raises."""
    try:
        if not isinstance(text, str) or not isinstance(now, datetime.datetime):
            return None
        now = _aware(now)
        found = None
        m = _RETRY_AFTER.search(text)
        if m:
            found = now + datetime.timedelta(seconds=int(m.group(1)))
        if found is None:
            m = _IN.search(text)
            if m:
                found = now + datetime.timedelta(seconds=int(m.group(1)) * _UNIT[m.group(2).lower()])
        if found is None:
            m = _AT.search(text)
            if m:
                v = m.group(1)
                if v.isdigit():
                    found = datetime.datetime.fromtimestamp(int(v), datetime.timezone.utc)
                else:
                    found = _aware(datetime.datetime.fromisoformat(v.replace("Z", "+00:00").replace("z", "+00:00")))
        if found is None or found <= now or found - now > datetime.timedelta(days=MAX_RESET_DAYS):
            return None
        return found
    except (ValueError, OverflowError, OSError, TypeError):
        return None


def render_reset_line(when):
    """The machine line carrying a reset time; ValueError for anything but a datetime."""
    if not isinstance(when, datetime.datetime):
        raise ValueError("reset time must be a datetime")
    return RESET_PREFIX + _aware(when).isoformat()


def parse_reset_line(comment):
    """The reset time in `comment`'s LAST machine line, or None (absent or unreadable)."""
    try:
        found = _RESET_LINE.findall(comment) if isinstance(comment, str) else []
        return _aware(datetime.datetime.fromisoformat(found[-1])) if found else None
    except ValueError:
        return None
