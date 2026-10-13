#!/usr/bin/env python3
"""The managed block in `.sdlc/features/<name>.md` (#1470, epic #1464, story #1427).

`feature_registry.py` is the machine truth -- `units/<name>.json` and the `index.json` chart sheet.
This module owns the OTHER half of the same registry: the file a person opens. One file, two owners:

    <!-- sigma:begin managed sha256:... -->
    ...regenerated from the registry -- do not edit...
    <!-- sigma:end managed -->

    ## Notes
    Anything a human writes below is theirs and is never touched.

THE FOUR RULES, IN THE PRECEDENCE ORDER THE ISSUE STATES THEM, AND WHERE EACH ONE LIVES:

  1. THE REGISTRY IS AUTHORITATIVE FOR STRUCTURED STATE. The block is rendered FROM an entry
     (`render_block`), and prose is never parsed to recover state on the normal path. `parse_block`
     and `recover` exist only for rule 4, and `sync` reaches them only when the registry has nothing
     to say -- see `recover`'s own docstring for why this separation is the rule rather than a
     stylistic one.
  2. THE REGION OUTSIDE THE BLOCK IS HUMAN-OWNED AND NEVER REWRITTEN. This module therefore works on
     BYTES from end to end and never decodes the human region at all. Reading `<name>.md` as text
     would translate CRLF to LF and replace undecodable bytes; writing it back would then silently
     rewrite prose this module promised not to touch, and every result-shaped test would still pass.
     A splice of `data[:start] + block + data[stop:]` is the only form that makes "byte-identical"
     literally true rather than approximately true.
  3. A HAND-EDIT INSIDE THE BLOCK IS OVERWRITTEN, AND THE DIVERGENCE IS WRITTEN TO THE LEDGER.
     Silently discarding somebody's edit is the one outcome to avoid; overwriting it while saying so
     is fine, because the block is marked as not theirs.
  4. A `<name>.md` WITH NO ENTRY IN THE REGISTRY is rebuilt from what still parses, and if nothing
     does, it is LEFT UNTOUCHED AND FLAGGED -- never deleted, never truncated.

WHY THE BLOCK CARRIES A CHECKSUM OF ITS OWN BODY, which is this module's one real design decision.
Rule 3 needs to separate "a human typed in the block" from "the registry changed", and the two
obvious tests cannot:

  - comparing the block on disk with the block about to be written reports a divergence on every
    ordinary registry change, and a signal that fires every time is not a signal;
  - re-rendering what the block parses to and comparing that with the block on disk looks stricter
    and is in fact blind exactly where it matters: a hand-edit that changes a TITLE leaves a
    perfectly canonical block, so the re-render matches, the check passes, and the edit is discarded
    in silence -- the precise failure rule 3 exists to prevent.

So the begin marker carries `sha256:<16 hex>` over the block's body. A body its digest vouches for
was last written by this module; one it does not vouch for was not, and is reported. The digest is
TRUNCATED to 64 bits deliberately: the property needed is "an edit changes it", for which any
collision-resistant prefix suffices, and this line is the first thing a human sees in the file. It
is tamper-EVIDENT, not tamper-proof -- someone who recomputes a digest to hide an edit has decided
to hide it, and no marker in a text file can stop that.

WHAT A COLLISION COSTS, SAID PLAINLY, because 64 bits is a judgement and a judgement should carry
its downside: an edited body whose digest happens to match the recorded one is treated as vouched,
so it is discarded IN SILENCE and reported as `UPDATED` -- the exact outcome rule 3 exists to
prevent. This is a targeted match at 2^-64, not a birthday problem: there is one recorded digest and
one body, and the edit has to hit it. Widening the field buys nothing against someone who has
decided to recompute the digest anyway, and costs the first line of the file its readability.

A BLOCK WITH NO DIGEST AT ALL IS REPORTED TOO, and that is the deliberate direction. A block
somebody wrote by hand, or one whose attribute a person deleted, cannot vouch for itself -- and
"cannot prove nothing was discarded" has to fail towards saying so. It lands as `STAMPED` rather
than as a divergence, because "cannot prove" is not "somebody edited this"; the cost is one report
the first time such a file is synced, and the alternative cost is a lost edit, silently.

THAT LANDING PLACE IS PERMANENT, NOT TRANSITIONAL, and being exact about why matters: a person
CANNOT hand-write a valid digest, because it covers a body they have not written yet. No amount of
documenting the attributed form changes that. What documentation does control is which PLACEHOLDER
gets copied, and that choice is load-bearing in a way that is easy to get backwards -- see
`_CHECKSUM_RE` for the measurement.

WHAT IS DELIBERATELY NOT HERE. Nothing on this module's write path touches the registry: no shard,
no chart sheet, no registry file of any kind, read or written. (Precisely: the only `.json` anywhere
in reach is `.sdlc/config.json`, which `ledger.safe_append` reads inside its own guard to find out
whether the ledger is even on -- the ledger's own gate, not the registry, and a read -- plus, since
#954, this page's own provenance record under gitignored `state/features/provenance/`, which
`feature_provenance` reads and writes around each page write: Sigma's state, not a registry file.) The doc is
DOWNSTREAM of the machine truth, and a sync that wrote back into the registry would make prose an
input to it -- rule 1 inverted. Deciding WHEN to sync,
cross-checking a branch against the branches that actually exist, and folding a recovered entry back
into the registry all belong to the pick-path sync, #1473. Propagating a file to a sibling repo is
#1477. This module answers one question: given a unit and what the registry says about it, what
should `<name>.md` contain, and what was lost in getting there?

Module shape follows `feature_registry.py` and `features.py`: no third-party dependencies,
module-level constants, siblings loaded via `_load`.
"""
import collections
import contextlib
import hashlib
import importlib.util
import os
import pathlib
import re
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


registry = _load("feature_registry")
ledger = _load("ledger")            # team record (config-gated, default OFF; every call fail-open)
legacy = _load("legacy")            # #239: the same markers under the plugin's previous name

#: THE one rule for what a unit may be called, taken from the registry rather than re-derived --
#: which took it from `features.py` for the same reason. A unit name is a `feature:<name>` label, a
#: `feature/<name>` branch segment, a `units/<name>.json` shard and now a `<name>.md` filename; four
#: spellings of one rule would be four answers.
is_unit_name = registry.is_unit_name

#: The naming contract's exception, RE-EXPORTED rather than re-declared, so `except
#: feature_doc.InvalidUnitName` is a thing a caller can write.
#:
#: This is not cosmetics, and the reason is a real wrinkle in the `_load` idiom this whole plugin is
#: built on: `_load` executes a fresh module OBJECT every time it is called, so a caller that loads
#: `feature_registry` for itself holds a DIFFERENT class object from the one this module raises, and
#: `except registry.InvalidUnitName` against that other instance would not catch this one. Binding
#: the name here gives a caller a class reached through the module that actually raises it, which is
#: the only form of the check that holds. It stays a `ValueError` subclass either way, so the broad
#: form works across instances and is the fallback for anyone who does not want to know this.
InvalidUnitName = registry.InvalidUnitName

#: The suffix of the human-readable file. `.md` because it is markdown, and because the registry's
#: own `*.json` glob over `units/` must never be able to see it.
DOC_SUFFIX = ".md"

#: The markers, exactly as the design doc prints them. These are a published format -- people write
#: them by hand and later tooling will grep for them -- so the literals are a contract.
#:
#: `BEGIN_OPEN` is only the OPENING of the begin marker, not the whole of it, because this module
#: writes an attribute inside it (`sha256:...`) and must still recognise the bare documented form.
#: Recognising a superset of what it writes is the safe direction: the worst case is a block whose
#: provenance is unknown, which `sync` already handles by reporting it.
BEGIN_OPEN = "<!-- sigma:begin managed"
BEGIN_CLOSE = "-->"
END = "<!-- sigma:end managed -->"

#: The HTML comment opener, which BOTH markers begin with -- pinned by a test, because that is the
#: whole reason escaping this ONE sequence is enough to stop a value forging either of them.
#:
#: A value carrying a marker literal is not a hypothetical: this module calls the markers "a
#: published format" and anticipates a file that quotes them, and the most plausible unit to carry
#: one in its TITLE is the unit created to document this very format. Rendered verbatim, the first
#: sync writes the doc and every sync after it is `flagged`, for ever, with a diagnosis blaming the
#: file for damage this module inflicted.
COMMENT_OPEN = "<!--"

#: The digest attribute, and how much of it is written. See the module docstring for why 16 hex
#: characters rather than the full 64.
CHECKSUM_KEY = "sha256"
CHECKSUM_CHARS = 16

#: The three states a file can be in with respect to the block. `GARBLED` is not a failure to parse
#: -- it is a refusal to GUESS, and the whole done-when about never truncating rests on it.
INTACT, GARBLED, ABSENT = "intact", "garbled", "absent"

#: `start`/`stop` bound the whole block including both markers -- what a splice replaces.
#: `inner_start`/`inner_stop` bound the body alone -- what the digest covers and what `parse_block`
#: reads. All four are BYTE offsets into the bytes handed to `locate`.
Block = collections.namedtuple("Block", "state start stop inner_start inner_stop checksum reason")

#: Every outcome `sync` can report. Each is a distinct thing that happened to a real file, and the
#: caller (#1473) routes on them, so they are a closed vocabulary rather than free text.
CREATED = "created"              # no file; one was written
UNCHANGED = "unchanged"          # the block already says exactly this; nothing was written
UPDATED = "updated"              # the registry moved; the block was regenerated
OVERWRITTEN = "overwritten"      # its digest did not vouch for it; regenerated, divergence set
STAMPED = "stamped"              # it carried NO digest; regenerated and stamped, no accusation
PREPENDED = "prepended"          # a file with no block; the block went above it, every byte kept
FLAGGED = "flagged"              # the markers are damaged or it is unreadable; NOTHING written
RECOVERED = "recovered"          # no registry entry, but the block still parses; file untouched
ORPHANED = "orphaned"            # no registry entry and nothing recoverable; file untouched
NOTHING = "nothing"              # no registry entry and no file -- there is nothing to reconcile
OUTCOMES = (CREATED, UNCHANGED, UPDATED, OVERWRITTEN, STAMPED, PREPENDED, FLAGGED, RECOVERED,
            ORPHANED, NOTHING)

#: The entry fields the block states, as `(label a human reads, key in the schema)`. `tracking
#: issue` is spelled with a space on the page and with an underscore in the schema; every other
#: field is the same word twice, and the pairing is written out rather than derived so that
#: renaming a LABEL can never silently rename a KEY.
#:
#: THIS TUPLE IS BOTH DIRECTIONS. `_body` renders from it and `parse_block` reads back against it,
#: so a schema field it does not name is dropped on render AND unreadable on parse -- a second
#: silent-loss path alongside `feature_registry.normalise_entry`'s own whitelist, and one that costs
#: more, because rule 4 rebuilds a lost registry from this page. A field added to the schema is
#: added here in the same commit or it is a field no recovery can give back. (`priority`, #2261, is
#: the addition that made this worth writing down; `_is_substantive` is deliberately NOT extended
#: with it -- a bare tier and nothing else is a hollow unit, and rebuilding those is the outcome
#: that guard exists to refuse.)
FIELDS = (("title", "title"), ("owner", "owner"), ("open", "open"), ("parent", "parent"),
          ("tracking issue", "tracking_issue"), ("priority", "priority"))

#: The repos table's columns, in order. Both ownership levels appear (§7.3): `owner` here is the
#: repo's owner, and the unit's own owner is the `owner` field above it.
COLUMNS = ("repo", "branch", "owner", "authorized", "goals")

#: How a boolean and an absent value are spelled on the page. `ABSENT_MARK` is deliberately NOT a
#: backticked value, which is what lets `_decode` tell "no owner" from "an owner whose name is the
#: empty string" -- two different registry states that a shared blank spelling would collapse into
#: one, silently editing the backup on the way through.
YES, NO, ABSENT_MARK = "yes", "no", "—"

#: A field line, and a value. A value is DELIMITED BY BACKTICKS so that absent (no delimiters) and
#: empty (empty delimiters) stay distinguishable, and so that a title full of markdown renders as
#: what it is rather than as formatting. `_escape` guarantees no raw backtick inside, which is what
#: makes the greedy `(.*)` land on the closing delimiter rather than on the first one it meets.
_FIELD_RE = re.compile(r"\A- \*\*([a-z ]+):\*\*[ \t]*(.*)\Z")
_VALUE_RE = re.compile(r"\A`(.*)`\Z")

#: A table separator row (`| --- | :-- |`), which carries no data.
_SEPARATOR_CELL_RE = re.compile(r"\A:?-+:?\Z")

#: A run of digits in a goals cell. Handed to the registry's own normaliser as STRINGS rather than
#: parsed here: `_goals`/`_goal` already refuse a run too long for `int()` to be safe on, refuse
#: zero and negatives, de-duplicate in first-seen order, and this module must not hold a second
#: opinion about any of that.
_DIGITS_RE = re.compile(r"[0-9]+")

#: The digest attribute as it appears inside the begin marker.
#:
#: ANY DOCUMENTATION THAT PRINTS THIS MARKER MUST USE A PLACEHOLDER THIS PATTERN CANNOT MATCH -- a
#: word, an ellipsis, angle brackets, anything that is not CHECKSUM_CHARS hex digits. This line is
#: the only thing that decides that question, so it is the only honest place to record it.
#:
#: A hand-written block can NEVER carry a valid digest, because a person cannot compute the digest
#: of a body they have not written yet. `STAMPED` is therefore where every hand-authored block
#: lands, permanently rather than transitionally -- which makes WHICH placeholder gets copied
#: around the whole of the difference:
#:
#:     sha256:0123456789abcdef        -> parses as a digest -> MISMATCH -> accuses a named owner
#:     sha256:<written-by-sigma>  -> parses as absent   -> cannot-prove -> benign, one report
#:
#: A plausible-looking concrete digest in a document is thus strictly WORSE than printing no
#: attribute at all: every file copied from it lands as a divergence naming somebody, for exactly
#: the population `STAMPED` exists to protect. Pinned by a test, because prose in a document this
#: repository does not contain cannot be pinned any other way.
_CHECKSUM_RE = re.compile(r"\b%s:([0-9a-f]{%d})\b" % (re.escape(CHECKSUM_KEY), CHECKSUM_CHARS))

#: Characters that cannot survive a round trip through a markdown page unescaped, and what they
#: become. Each one is here for a measured reason, not for tidiness:
#:
#:  - `\` first and always, because it is the escape character: escaping it before anything else is
#:    what makes `_unescape` unambiguous (`\\n` is a literal backslash then an `n`, `\n` is a
#:    newline), and what makes `_split_cells` able to skip an escaped delimiter by pairs;
#:  - `` ` `` because it delimits every value;
#:  - `|` because a raw one splits the table row it lands in, and the value comes back truncated --
#:    silent loss, invisible in a rendered table;
#:  - `\n` and `\r` because this file is parsed by lines. `\r` matters on its own: a lone `\r` looks
#:    like a line break to a human and to some renderers while `split("\n")` never sees it, which is
#:    the same class of hole `ledger._cell` records having to close twice.
#:
#: DELIBERATELY NOT `splitlines()`'s FULL SET (`\v`, `\f`, `\x85`, ` `, ...). That set is the
#: right one for `ledger._cell`, which flattens a value into a markdown table it does not control.
#: Here the parser is this module's own and splits on `"\n"` ALONE, so those characters are not
#: structural -- they pass through verbatim and come back verbatim. Escaping them would be
#: cosmetics bought at the price of a lossier round trip.
_ESCAPES = (("\\", "\\\\"), ("`", "\\`"), ("|", "\\|"), ("\n", "\\n"), ("\r", "\\r"))
_ESCAPE_MAP = dict(_ESCAPES)
_UNESCAPE_MAP = {"\\": "\\", "`": "`", "|": "|", "n": "\n", "r": "\r"}
_UNESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})|\\(.)", re.DOTALL)

#: The surrogate range. `feature_registry.read` is TOTAL, so it hands back whatever was in the JSON
#: -- including a lone surrogate, which is ordinary JSON that `json.loads` returns as a real `str`
#: and which `.encode("utf-8")` then refuses. That is the exact defect `feature_registry.dumps`
#: records fixing on its own side (with `ensure_ascii=True`), and it would arrive here unchanged:
#: the rendered block is encoded before it is written, so an unescaped surrogate would make writing
#: fail on data reading accepted. Escaping it as `\uXXXX` keeps the round trip EXACT -- nothing is
#: lost, only the on-disk spelling changes -- and leaves the encode with nothing it can fail on.
_SURROGATE_LOW, _SURROGATE_HIGH = "\ud800", "\udfff"


def _note(message):
    """One stderr line, never an exception -- same shape and same reason as `features._note` and
    `feature_registry._note`: a diagnostic must never be the thing that breaks a pick. This is also
    the channel a flag reaches a human through when the ledger is off, which it is by default."""
    try:
        sys.stderr.write(message)
    except Exception:                     # noqa: BLE001 - a diagnostic must never break a pick
        pass


# --------------------------------------------------------------------------- paths


def doc_path(features_dir, name):
    """`<features_dir>/<name>.md` -- the one place a unit NAME becomes a doc PATH, and therefore the
    one place that has to refuse.

    Raises `InvalidUnitName` (a `ValueError` subclass) for anything `is_unit_name` rejects,
    which is what keeps `a/b`, `..` and `../../etc/passwd` from ever addressing a file. It is the
    registry's OWN exception rather than a new one, so a caller that already catches the naming
    contract it was told about catches this too.

    #1673: FOLDED, and folded through `feature_registry.unit_key` rather than through a local
    `.lower()`. Unfolded, `Voice` and `voice` were two PAGES for one unit -- the most visible member
    of this family, because the second page is a document a person opens that nothing regenerates
    and nothing reconciles. The fold is borrowed rather than repeated for the reason `unit_key`'s
    own docstring gives: the same rule written at each site is the rule that drifts, and it had
    already drifted four times.

    AFTER the guard, never before, and the refusal below stays this site's own. `is_unit_name`
    rejects `voice.lock` but accepts `voice.LOCK`, so folding first would turn an accepted name into
    the spelling of a rejected one; the suffix is appended after the fold for the same reason."""
    if not (isinstance(name, str) and is_unit_name(name)):
        raise InvalidUnitName(
            "%r is not a unit name -- a unit name is one git branch segment (no '/', no '..', no "
            "leading '.', no trailing '.' or '.lock'), because it becomes both a feature:<name> "
            "label and a %s file in the registry" % (name, DOC_SUFFIX))
    return pathlib.Path(features_dir) / (registry.unit_key(name) + DOC_SUFFIX)


# --------------------------------------------------------------------------- escaping


def _escape(text):
    """A value, safe to put on the page and to read back off it. See `_ESCAPES` for why each rule
    is there, and `_SURROGATE_LOW` for why the surrogate clause is not optional.

    THE COMMENT-OPENER CLAUSE IS WHAT KEEPS A VALUE FROM FORGING A MARKER, and the form it takes is
    the whole point: the `<` is REPLACED by `\\u003c`, never PREFIXED with a backslash. A prefix
    escapes nothing here -- `\\<!--` still contains `<!--` as a contiguous substring, so
    `locate`'s `find` would still hit it and the block would still be unreadable. Only a form that
    does not reproduce the character breaks the literal.

    IT IS DELIBERATELY NARROW: a bare `<` is left alone, because `<` is ordinary in a title
    (`a < b`) and this file exists to be read by a person. Only an opener is a hazard, and only an
    opener is escaped.

    `-->` IS DELIBERATELY NOT ESCAPED, and the reasoning is worth keeping because it is not
    obvious. `locate` looks for `-->` only to close the BEGIN marker, and only from
    `start + len(BEGIN_OPEN)` -- a position inside the marker itself, whose own `-->` therefore
    always comes first. Every value sits in the body, past that point, so a `-->` in one can never
    be mistaken for the marker's close. The single residual is a file whose begin marker a human
    has already broken by deleting its `-->`; a body `-->` then closes it early. That case is
    already malformed, nothing outside the two markers is touched, and it lands as `OVERWRITTEN`
    with the divergence reported -- so it fails safe and loudly, which is the bar. Escaping `-->`
    would cost `Manifest --> video` its readability to buy that one already-reported case."""
    out, i, text = [], 0, str(text)
    while i < len(text):
        ch = text[i]
        if ch in _ESCAPE_MAP:
            out.append(_ESCAPE_MAP[ch])
        elif text.startswith(COMMENT_OPEN, i) or _SURROGATE_LOW <= ch <= _SURROGATE_HIGH:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _unescape(text):
    """The inverse, and TOTAL over text this module did not write.

    An escape it does not recognise (`\\x`) comes back as the two characters it already was rather
    than as `x`: `_escape` can never produce one, so every such sequence is somebody's literal
    backslash out of a hand-edited block, and preserving it recovers more than decoding it would."""
    def one(match):
        if match.group(1) is not None:
            return chr(int(match.group(1), 16))
        return _UNESCAPE_MAP.get(match.group(2), match.group(0))
    return _UNESCAPE_RE.sub(one, text)


def _value(text):
    """A present value, delimited. `None` is the ABSENCE of a value and is never delimited."""
    return ABSENT_MARK if text is None else "`%s`" % _escape(text)


def _decode(cell):
    """A cell back to a value, or None when it carries no value at all.

    Stripping happens OUTSIDE the delimiters only, so a value whose own text begins or ends with a
    space survives: the delimiters are what mark where it starts and stops."""
    match = _VALUE_RE.match(cell.strip())
    return _unescape(match.group(1)) if match else None


def _flag(value):
    return YES if value else NO


def _decode_flag(cell):
    """A real `True`/`False`, never a truthy string. The registry's `authorized`/`open` rules are
    `is True`/`is not False`, so anything softer than a real boolean handed back from here would
    read as "not granted"/"still open" and this module would have said something it did not mean."""
    return cell.strip() == YES


# --------------------------------------------------------------------------- rendering


def _row(cells):
    return "| " + " | ".join(cells) + " |"


def _repos_table(repos):
    if not repos:
        return ["_No repo is recorded for this unit._"]
    lines = [_row(COLUMNS), _row(["---"] * len(COLUMNS))]
    # Sorted by repo name for the same reason `feature_registry.dumps` sorts its keys: this file
    # lives in git, and a serialiser whose order wanders makes every pick a diff.
    for name in sorted(repos):
        one = repos[name]
        goals = ", ".join(str(g) for g in one["goals"]) or ABSENT_MARK
        lines.append(_row([_value(name), _value(one["branch"]), _value(one["owner"]),
                           _flag(one["authorized"]), goals]))
    return lines


def _body(name, entry):
    """The block's body: everything between the two markers, starting and ending with a newline so
    that the markers each own their own line."""
    lines = ["",
             "<!-- Generated from the feature registry. Do not edit inside this block: it is",
             "     regenerated on every sync, and an edit here is overwritten and reported to the",
             "     ledger. Write below the end marker instead. -->",
             "",
             "# " + name,
             ""]
    for label, key in FIELDS:
        value = _flag(entry[key]) if key == "open" else _value(entry[key])
        lines.append("- **%s:** %s" % (label, value))
    lines.append("")
    lines += _repos_table(entry["repos"])
    lines.append("")
    return "\n".join(lines) + "\n"


def _lf(data):
    """Body bytes with CRLF folded to LF -- the ONE form in which a block is compared with another
    block, whether by digest or by equality.

    THE LINE ENDING IS THE CHECKOUT'S, NOT THE CONTENT'S. `.sdlc/**` is pinned to nothing, so a
    consumer repo on `core.autocrlf=true` (the Git-for-Windows default) receives every `\n` in the
    committed file as `\r\n`. Digesting those raw bytes made an ordinary checkout a PERMANENT false
    divergence: reported as edited, the whole body handed back as discarded, a NAMED OWNER accused
    in the ledger, and the block rewritten as LF -- which git converts straight back on the next
    checkout. That is the "a signal that fires every time is not a signal" failure the digest exists
    to avoid, relocated to a different population of checkouts and made worse by naming somebody.

    IT IS LOSSLESS, AND THAT IS MEASURED RATHER THAN ARGUED: `_escape`'s own `\r` rule means no body
    this module renders can contain a raw CR (pinned by a test over every field), so a CR on disk is
    always a line-ending conversion and never content. There is no content edit expressible as "a CR
    before every LF", so nothing real can hide behind this.

    A LONE `\r` IS NOT FOLDED. Classic-Mac line endings are extinct, git does not produce them, and
    folding them would widen what can hide here for nothing."""
    return data.replace(b"\r\n", b"\n")


def _checksum(body):
    """The digest of a body, over BYTES with `_lf` applied. Bytes rather than text because that is
    what is on disk and what `locate` hands back -- hashing the decoded form would make the answer
    depend on how the file happened to decode, which for a file this module refuses to decode is no
    answer at all."""
    return hashlib.sha256(_lf(body)).hexdigest()[:CHECKSUM_CHARS]


def _marker(digest):
    """The begin marker for a given digest -- the ONE place its exact spelling is decided.

    Written once so that the renderer and the check that a marker has not been TAMPERED WITH cannot
    drift: `sync` compares the marker on disk against `_marker(the digest that marker itself
    carries)`, and if those two spellings were written out separately, every ordinary sync would
    read as an edit the first time one of them changed."""
    return "%s %s:%s %s" % (BEGIN_OPEN, CHECKSUM_KEY, digest, BEGIN_CLOSE)


def render_block(name, entry):
    """The whole managed block for one unit, markers included, with no trailing newline.

    Normalises the entry first, so an abbreviated hand-written entry renders with the schema's
    defaults filled in and `render_block(n, e) == render_block(n, normalise_entry(e))` holds.

    Raises `InvalidUnitName` on a name that is not one, for the same reason `doc_path`
    does: the name is written into the page as a heading, and a name that could not address a file
    has no business naming a unit in one."""
    if not (isinstance(name, str) and is_unit_name(name)):
        raise InvalidUnitName("%r is not a unit name" % (name,))
    body = _body(name, registry.normalise_entry(entry))
    return _marker(_checksum(body.encode("utf-8"))) + body + END


def render_doc(name, entry):
    """A whole `<name>.md` for a unit that has none yet: the block, then the region that is not
    Sigma's, with one sentence in it saying so. Only ever used to CREATE -- an existing file's
    human region is never generated, only preserved."""
    return (render_block(name, entry)
            + "\n\n## Notes\n\n"
            + "Anything written below the end marker above is yours and is never touched.\n")


# --------------------------------------------------------------------------- locating


def _find_all(data, needle):
    out, at = [], data.find(needle)
    while at != -1:
        out.append(at)
        at = data.find(needle, at + len(needle))
    return out


def _marker_pair(data):
    """-> `(begin_open, end)` as BYTES: the marker pair this file's managed block is spelled with.

    #239. A doc written before the rename carries the SAME markers under the plugin's previous name,
    and reading it through the Sigma pair alone found no block at all -- `sync` then PREPENDED a
    second one and orphaned the first as prose. The previous pair is used ONLY when the file holds no
    Sigma marker whatsoever: any Sigma marker means Sigma manages its own pair, and a previous-name
    block beside it is prose owned by a teammate still on the old plugin (plan-review BLOCKER 2 --
    each plugin keeps its own block, so a mixed team cannot lock the file into GARBLED). A half-and-
    half pair (a previous-name begin with a Sigma end) therefore resolves to the Sigma pair, finds an
    end with no begin, and is GARBLED -- a refusal to guess, as for any other broken pair."""
    begin, end = BEGIN_OPEN.encode("ascii"), END.encode("ascii")
    if begin in data or end in data:
        return begin, end
    return (legacy.retired_spelling(BEGIN_OPEN).encode("ascii"),
            legacy.retired_spelling(END).encode("ascii"))


def _garbled(reason):
    return Block(GARBLED, None, None, None, None, None, reason)


def locate(data):
    """-> a `Block` describing what `data` (BYTES) says about its managed block. Never raises.

    EVERY AMBIGUOUS SHAPE IS `GARBLED`, WHICH IS A REFUSAL TO GUESS RATHER THAN A FAILURE TO PARSE,
    and the done-when about never truncating rests entirely on it. With the end marker missing there
    is no boundary, so any splice this module could invent eats somebody's prose. With the markers
    doubled -- which is exactly what a file that QUOTES the format produces, and the adopter doc for
    this very format will -- picking a pair means picking which prose to destroy.

    A begin marker whose `-->` is only found beyond the end marker is unclosed, not wide: `-->` also
    ends the END marker, so an unguarded `find` would happily return a body that swallowed it."""
    begin_open, end_marker = _marker_pair(data)
    begins = _find_all(data, begin_open)
    ends = _find_all(data, end_marker)
    if not begins and not ends:
        return Block(ABSENT, None, None, None, None, None, None)
    if len(begins) > 1:
        return _garbled("it has %d begin markers, so which block is the managed one is a guess"
                        % len(begins))
    if len(ends) > 1:
        return _garbled("it has %d end markers, so where the managed block stops is a guess"
                        % len(ends))
    if not begins:
        return _garbled("it has an end marker with no begin marker")
    if not ends:
        return _garbled("its end marker is missing, so where the managed block stops is unknown")
    start, stop = begins[0], ends[0] + len(end_marker)
    if ends[0] < start:
        return _garbled("its end marker comes before its begin marker")
    close = data.find(BEGIN_CLOSE.encode("ascii"), start + len(begin_open))
    if close == -1 or close > ends[0]:
        return _garbled("its begin marker is never closed")
    # No further ordering guard is needed, and one that looked prudent was MEASURED DEAD: the body
    # starts at `close + 3`, and the three bytes at `close` are `-`, `-`, `>` -- none of them the
    # `<` an end marker begins with -- so an end marker at or after `close` is always at or after
    # `close + 3` too. The two clauses above are the only reachable ones, and the ORDER of them is
    # what makes each diagnosis right: checking "never closed" first would report an out-of-order
    # pair as an unclosed marker and send a person looking at the wrong line.
    #
    # THE DEADNESS IS CONDITIONAL ON `close > ends[0]` SURVIVING, which is the thing to know before
    # editing either line: drop that clause and a body `-->` closes an unclosed marker early, this
    # branch becomes reachable at once, and `locate` starts returning an INTACT block whose body
    # runs backwards (`inner_start > inner_stop`).
    inner_start = close + len(BEGIN_CLOSE)
    marker = data[start:inner_start].decode("utf-8", "replace")
    found = _CHECKSUM_RE.search(marker)
    return Block(INTACT, start, stop, inner_start, ends[0], found.group(1) if found else None, None)


# --------------------------------------------------------------------------- parsing (rule 4 only)


def _split_cells(line):
    """A table row into cells, splitting on delimiters the escaper did not write.

    Scanned by pairs rather than matched with a look-behind on purpose: `a\\\\|b` is an escaped
    BACKSLASH followed by a real delimiter, and no fixed-width look-behind can tell that from an
    escaped delimiter. Consuming `\\` plus whatever follows it as one unit is the only rule that
    gets both right, and it leaves the escapes in place for `_decode` to undo."""
    cells, buf, i = [], [], 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and i + 1 < len(line):
            buf.append(ch); buf.append(line[i + 1]); i += 2
        elif ch == "|":
            cells.append("".join(buf)); buf = []; i += 1
        else:
            buf.append(ch); i += 1
    cells.append("".join(buf))
    if cells and not cells[0].strip():
        cells.pop(0)
    if cells and not cells[-1].strip():
        cells.pop()
    return cells


def _is_separator(cells):
    return bool(cells) and all(_SEPARATOR_CELL_RE.match(c.strip()) for c in cells)


def _repos_from(rows):
    """The repos table's rows -> a raw `repos` mapping, keyed by the header's own column names.

    By NAME rather than by position because recovery is the case where the block may already be
    damaged: a row that lost a column, or a table someone reordered, still yields the fields it
    still has, where a positional read would silently shift every value one column left."""
    if len(rows) < 2:
        return {}
    header = [c.strip() for c in _split_cells(rows[0])]
    repos = {}
    for row in rows[1:]:
        cells = _split_cells(row)
        if _is_separator(cells):
            continue
        by_name = dict(zip(header, cells))
        name = _decode(by_name.get("repo", ""))
        if name is None:
            continue
        repos[name] = {"branch": _decode(by_name.get("branch", "")),
                       "owner": _decode(by_name.get("owner", "")),
                       "authorized": _decode_flag(by_name.get("authorized", "")),
                       "goals": _DIGITS_RE.findall(by_name.get("goals", ""))}
    return repos


def parse_block(body):
    """-> the entry a block body states. Never raises; an unreadable body yields the defaults.

    RULE 1 SAYS PROSE IS NEVER PARSED TO RECOVER STATE, AND THIS FUNCTION IS NOT AN EXCEPTION TO IT
    -- it is rule 4's machinery, reached only when the registry has nothing to say about a unit at
    all. Nothing on the normal path calls it. `recover` is the door, and it is the only one.

    The result goes through `registry.normalise_entry`, so every type rule, default and tightening
    is the registry's single opinion: `authorized` granted only by a real `True`, `open` closed only
    by a real `False`, goals bounded, de-duplicated and in first-seen order. A second opinion here
    would be a second answer to a question the registry already answers."""
    raw, rows = {}, []
    for line in body.split("\n"):
        if line.lstrip().startswith("|"):
            rows.append(line)
            continue
        field = _FIELD_RE.match(line.rstrip())
        if not field:
            continue
        label, value = field.group(1), field.group(2)
        for shown, key in FIELDS:
            if label == shown:
                raw[key] = _decode_flag(value) if key == "open" else _decode(value)
    raw["repos"] = _repos_from(rows)
    return registry.normalise_entry(raw)


def parse_doc(text):
    """-> `(state, entry, reason)` for a whole document. The entry is None unless a block was found.
    A convenience over `locate` + `parse_block` for callers that hold text rather than offsets.

    NEVER RAISES, and the encode is why that needed saying. `locate`, `parse_block` and `recover`
    are all total, so this one -- the door a caller holding TEXT comes in by -- inherits the
    promise; a plain `str.encode("utf-8")` broke it on a lone surrogate, which the registry's total
    read side accepts and utf-8 refuses. That is the same writing-cannot-fail-on-data-reading-
    accepted shape `_SURROGATE_LOW` records closing on the render side, re-opened one function
    along. `surrogatepass` encodes it instead of refusing it, and since only marker literals -- pure
    ASCII -- are searched for in the result, what those bytes decode to is irrelevant here."""
    data = text.encode("utf-8", "surrogatepass") if isinstance(text, str) else text
    where = locate(data)
    if where.state != INTACT:
        return where.state, None, where.reason
    body = data[where.inner_start:where.inner_stop].decode("utf-8", "replace")
    return INTACT, parse_block(body), None


def _is_substantive(entry):
    """Does this entry say anything at all?

    "Rebuilt from the managed block IF IT STILL PARSES" has to mean something, and `parse_block` is
    total -- a body with every field gone parses into the schema's defaults, an entry with no title,
    no owner and no repos. Handing that back as a recovery would let a lost registry be rebuilt as a
    registry of hollow units, which is strictly worse than knowing it was lost: an absent unit makes
    someone look, an empty one does not."""
    return bool(entry["title"] or entry["owner"] or entry["parent"]
                or entry["tracking_issue"] or entry["repos"])


def recover(features_dir, name):
    """-> the entry `<name>.md`'s managed block still states, or None. Never raises.

    RULE 4, AND THE ONLY DOOR IN THIS MODULE THROUGH WHICH PROSE BECOMES STATE. The registry was
    lost; the block is the last surviving record of the unit, so parsing it is the difference
    between recovering the unit and losing it. That it exists is not licence to call it on the
    normal path -- rule 1 is a precedence rule, and this is what the LAST rule is for.

    WHAT THE FILE MAY SPEAK FOR IS BOUNDED BY ITS OWN NAME, exactly as `feature_registry.
    _read_unit_file` bounds a shard by its filename: the unit is `name`, and the heading inside the
    block is display. Otherwise a block someone copied between two files would let one unit's doc
    recover another unit's entry."""
    try:
        path = doc_path(features_dir, name)
    except ValueError:
        return None
    data, _ = _read(path)
    if data is None:
        return None
    state, entry, _reason = parse_doc(data)
    if state != INTACT or not _is_substantive(entry):
        return None
    return entry


# --------------------------------------------------------------------------- reading and writing


def _read(path):
    """-> `(bytes, None)`, `(None, None)` for a file that is not there, or `(None, reason)` for one
    that is there and cannot be read.

    THOSE LAST TWO ARE NOT THE SAME THING AND MUST NOT BE COLLAPSED. A missing file is written; an
    unreadable one -- a mode nothing can open, a directory where the file should be, a dangling
    symlink -- is left alone. Treating them alike is how a file with a year of somebody's notes in
    it becomes a fresh stub. `ValueError` sits beside `OSError` for `feature_registry._read_json`'s
    measured reason: a path carrying an embedded NUL raises it from the syscall wrapper before any
    OSError can be produced."""
    try:
        return path.read_bytes(), None
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError) as exc:
        return None, "it exists but could not be read (%s)" % exc


def _atomic_write_bytes(path, data, features_dir=None):
    """Write via a temp file in the SAME directory, then `os.replace`.

    The bytes twin of `feature_registry._atomic_write_text`, and byte-oriented for this module's
    whole reason for being: text mode would translate the newlines of a human region this module
    promised not to touch. Same directory so `os.replace` stays on one filesystem, the only case it
    is guaranteed atomic, so a reader only ever sees the fully-old or the fully-new file. `mkstemp`
    names the temp uniquely and the `.tmp` suffix keeps a crashed write's litter out of every glob
    that matters.

    `features_dir`, when given, records this page write's provenance (#954) exactly as
    `feature_registry._atomic_write_text` does for a shard: the one `os.replace` runs inside
    `registry.provenance.recorded`, after the page's chain records it and under its lock. Every
    write `sync` makes passes it."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        with (registry.provenance.recorded(features_dir, path, tmp) if features_dir is not None
              else contextlib.nullcontext()):
            os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:                   # noqa: S110 - cleanup must not mask the original failure
            pass
        raise


# --------------------------------------------------------------------------- the ledger edge


def _tell(sdlc_dir, name, goal, why, to=None):
    """Say it out loud: one stderr line always, one ledger entry when the ledger is on.

    FAIL-OPEN THROUGH `safe_append`, which reads config.json inside its own guard -- so a repo with
    no `.sdlc/config.json` at all, or an unreadable one, costs the entry and never the sync. The
    stderr line is not a fallback for that; it is the channel this reaches a human through when the
    ledger is off, which is its default.

    `note` is the kind, because none of the lifecycle kinds is honest here: nothing was claimed,
    parked or handed off. `to` is the unit's owner when there is one, which is what makes the entry
    reach a person (§7.3) rather than sit in a stream nobody reads."""
    _note("sigma: features: %s\n" % why)
    # `ref` is the path INSIDE `.sdlc`, not the absolute one: `ledger.append` caps `ref` at 120
    # characters, and an absolute path under a temp root or a deep checkout is routinely longer than
    # that -- so the absolute form is the one that arrives truncated, from the left-hand end that
    # matters least. The name also leads `why`, so the file is identified even if the cap bites.
    # #1673: the FOLDED name, because `ref` is an ADDRESS -- the path a person follows to the file.
    # `doc_path` folds, so `Voice` names `voice.md` on disk; a ref built from the raw name would
    # point at a file that is not there. The `why` text below is the record and keeps the casing its
    # author wrote; this field is the address and does not.
    fields = {"why": why, "area": registry.REGISTRY_DIRNAME,
              "ref": "%s/%s%s" % (registry.REGISTRY_DIRNAME, registry.unit_key(name), DOC_SUFFIX)}
    if isinstance(to, str) and to.strip():
        fields["to"] = to
    ledger.safe_append(sdlc_dir, "note", goal, **fields)


# --------------------------------------------------------------------------- sync


def _report(path, outcome, diverged=False, discarded=None, entry=None, reason=None):
    return {"path": path, "outcome": outcome, "diverged": diverged, "discarded": discarded,
            "entry": entry, "reason": reason}


def sync(features_dir, name, entry, goal=None, sdlc_dir=None):
    """Bring `<name>.md` into line with what the registry says about the unit. -> a report.

    `entry` is the unit's registry entry, or None when the registry has none -- which is rule 4, and
    is the one input on which this function writes NOTHING.

    THE FOUR RULES, IN ORDER, AS THIS FUNCTION APPLIES THEM:

      1. the block is rendered FROM `entry`. Nothing on this path reads prose for state;
      2. the file is spliced at the block's own boundaries, on bytes, so every byte outside it --
         above the begin marker as much as below the end marker -- is preserved exactly;
      3. a block whose digest does not vouch for its body is regenerated anyway, and the divergence
         is reported: to the caller in the report, on stderr, and to the ledger by file name;
      4. with no entry the file is never written. It is recovered from if its block still says
         something, and flagged if it does not.

    RAISES `InvalidUnitName` on a name that is not a unit name, and otherwise only what the
    filesystem raises on a write that genuinely failed -- the same division `feature_registry.
    write_unit` draws, and for the same reason: a caller writing `../escape` has a bug, not a
    corrupt file, and a disk that cannot be written to is not something to swallow.

    `goal` is what the ledger records this against; it defaults to the unit name, which is the only
    identifier this layer has. `sdlc_dir` defaults to the registry directory's parent, which is what
    `feature_registry.registry_dir` makes it."""
    path = doc_path(features_dir, name)
    # #1673: EVERY MESSAGE BELOW NAMES `path.name`, NEVER `name + DOC_SUFFIX`. `doc_path` folds, so
    # for a unit declared `Voice` the file is `voice.md` -- and a line telling somebody "Voice.md was
    # left untouched" names a file that does not exist, which is worse than saying nothing. The unit
    # still leads `why` in its declared casing through `goal`; the FILE is named as it is on disk.
    goal = name if goal is None else goal
    sdlc_dir = pathlib.Path(features_dir).parent if sdlc_dir is None else pathlib.Path(sdlc_dir)
    owner = entry.get("owner") if isinstance(entry, dict) else None

    data, unreadable = _read(path)
    if unreadable:
        _tell(sdlc_dir, name, goal,
              "%s was left untouched: %s" % (path.name, unreadable), to=owner)
        return _report(path, FLAGGED, reason=unreadable)

    if entry is None:
        # RULE 4. Nothing is written on this branch, at all: the registry is the authority on what a
        # unit is, and with the authority silent, a write would be this module inventing one.
        if data is None:
            return _report(path, NOTHING)
        recovered = recover(features_dir, name)
        if recovered is not None:
            return _report(path, RECOVERED, entry=recovered)
        _tell(sdlc_dir, name, goal,
              "%s has no registry entry and nothing recoverable in it, so it was left untouched"
              % (path.name,))
        return _report(path, ORPHANED, reason="no registry entry and nothing recoverable")

    block = render_block(name, entry)

    if data is None:
        _atomic_write_bytes(path, render_doc(name, entry).encode("utf-8"), features_dir)
        return _report(path, CREATED, entry=registry.normalise_entry(entry))

    where = locate(data)

    if where.state == GARBLED:
        # NEVER TRUNCATED. The boundaries are unknown, so every splice available is a guess about
        # which of somebody's prose to destroy.
        _tell(sdlc_dir, name, goal,
              "%s was left untouched: %s" % (path.name, where.reason), to=owner)
        return _report(path, FLAGGED, reason=where.reason)

    if where.state == ABSENT:
        # The block is Sigma's and has to exist; what is already in the file is not and must
        # survive. Prepending does both -- their bytes move down the file, they do not change.
        #
        # NOT A DIVERGENCE, deliberately: nothing was discarded, so rule 3 has nothing to say about
        # it, and `diverged=False`/`discarded=None` is the honest report. THE ONE COST, named rather
        # than left to be discovered: YAML front matter has to begin at byte 0, so a `<name>.md`
        # that opened with some no longer parses as having any. Every byte survives -- it is a
        # position change, not a loss -- but a reader should know it can happen.
        _atomic_write_bytes(path, block.encode("utf-8") + b"\n\n" + data, features_dir)
        return _report(path, PREPENDED, entry=registry.normalise_entry(entry))

    body = data[where.inner_start:where.inner_stop]
    recorded = where.checksum
    # THE BLOCK IS WHAT GETS SPLICED, SO THE CHECK HAS TO COVER ALL OF IT. The digest can only cover
    # the body -- it lives in the marker, so covering the marker would be circular -- which left the
    # begin marker's own interior checked by nothing at all: a note somebody wrote on that line was
    # deleted with no report and the result called UPDATED, which means "the registry moved" when
    # nothing had. The non-circular half is an EXACT comparison against the marker `render_block`
    # would emit for the digest that marker itself carries: nothing is hashed that contains a hash,
    # and every byte of the block is now covered by one check or the other.
    # #239: a block written under the plugin's previous name is vouched for by the same rule, spelled
    # the old way -- the digest covers the body only, so the rename never touched what it proves.
    marker_intact = recorded is not None and data[where.start:where.inner_start] in (
        _marker(recorded).encode("utf-8"),
        legacy.retired_spelling(_marker(recorded)).encode("utf-8"))
    vouched = marker_intact and recorded == _checksum(body)
    spliced = data[:where.start] + block.encode("utf-8") + data[where.stop:]

    # `vouched and` is REDUNDANT AND KEPT DELIBERATELY, which is worth stating because a mutation
    # run reports removing it as an equivalent change and a reader will wonder. Block equality
    # implies vouching -- and the premises are named, because they are what could stop holding:
    #
    #   1. `BEGIN_OPEN` contains no `sha256:`, so `_CHECKSUM_RE` has exactly one candidate to find;
    #   2. the rendered marker contains no `-->` before its own, since `sha256:` plus 16 hex
    #      characters cannot produce one;
    #   3. the rendered body contains ZERO marker literals, which is what `_escape`'s comment-opener
    #      rule guarantees. Before that rule existed this premise was FALSE for any entry carrying a
    #      marker in a value -- the equivalence survived only because such a block garbled before
    #      reaching this line, which is not a proof, it is a near miss;
    #   4. `_lf` distributes over the splice, because the block starts with `<` and ends with `>`,
    #      so no CRLF pair can straddle either seam.
    #
    # Given those, `_lf(spliced) == _lf(data)` forces the marker to match byte for byte (markers
    # carry no newline, so `_lf` is the identity on them) and the bodies to match modulo line
    # endings, hence `recorded == _checksum(body)`, hence `vouched`. The clause cannot change
    # today's outcome. It states the condition that actually matters -- do not call a block
    # UNCHANGED while its provenance is unknown -- and it is the half that survives if the digest
    # ever moves out of the marker, at which point the implication silently stops holding.
    if vouched and _lf(spliced) == _lf(data):
        # `_lf` ON BOTH SIDES, so a CRLF checkout is not a rewrite either. Reporting it as merely
        # not-a-divergence would still leave the block rewritten as LF on every single pick, which
        # git converts back on the next checkout -- a permanent diff instead of a permanent
        # accusation. Writing nothing leaves the file with the line endings its own checkout wants,
        # which is the only stable answer. The splice itself stays on RAW bytes: this decides
        # whether to write, never what to write.
        return _report(path, UNCHANGED, entry=registry.normalise_entry(entry))

    _atomic_write_bytes(path, spliced, features_dir)
    if _marker_pair(data)[0] != BEGIN_OPEN.encode("ascii"):
        # #239: said once, on the write that respells it -- silent when there was nothing old.
        sys.stderr.write("feature_doc: migrated legacy managed-block markers in %s to Sigma's on use\n"
                         % (path,))

    if vouched:
        return _report(path, UPDATED, entry=registry.normalise_entry(entry))

    discarded = data[where.start:where.stop].decode("utf-8", "replace")

    if recorded is None:
        # NO DIGEST IS "CANNOT PROVE", NOT "SOMEBODY EDITED THIS", and conflating the two meant a
        # file written by FOLLOWING THE DOCUMENTATION was reported in exactly the shape of a real
        # discarded edit, with a named owner told their work had been thrown away. It gets its own
        # outcome, its own wording and NO addressee -- a format upgrade is not somebody's business
        # to answer for. What it does NOT get is silence: the event is recorded and the replaced
        # text still comes back in `discarded`, because "cannot prove" cuts BOTH ways and this
        # branch must not claim the innocent direction either. One report per file, then the block
        # carries a digest and this never fires for it again.
        #
        # THE WORDING IS LOAD-BEARING AND IS DELIBERATELY THE WEAKER SENTENCE. "nothing is known to
        # have been edited" reads to a person as "nothing was edited", which is a fact this code
        # does not have: it has an absent checksum and no way to test the body against anything.
        # The line a person acts on has to state the burden of proof where it actually sits, so it
        # says what cannot be shown rather than what is so.
        _tell(sdlc_dir, name, goal,
              "the managed block in %s carried no checksum, so it was regenerated and stamped "
              "with one; whether anything was edited inside it cannot be shown either way"
              % (path.name,))
        return _report(path, STAMPED, discarded=discarded,
                       entry=registry.normalise_entry(entry),
                       reason="the block carried no checksum, so its provenance could not be "
                              "established")

    # RULE 3. The block carries a digest and that digest does not vouch for what is there, so
    # somebody edited inside it -- the body, or the marker line, which is equally inside the block
    # this module replaces. It is regenerated (the block is marked as not theirs) and the loss is
    # stated rather than swallowed, with the discarded text handed back in full for any caller that
    # wants more than the ledger's one capped line.
    _tell(sdlc_dir, name, goal,
          "the managed block in %s was regenerated; whether it had been edited could not be told "
          "from what was on disk; the block is generated from the registry and is not hand-editable" % (path.name,),
          to=owner)
    return _report(path, OVERWRITTEN, diverged=True, discarded=discarded,
                   entry=registry.normalise_entry(entry))
