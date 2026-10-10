"""feature_registry.py -- the `.sdlc/features/` registry: schema, read, write (#1469, epic #1464).

The registry is the COMMITTED BACKUP of a unit of work. Remote is the truth -- the `feature/*`
branches that exist ARE the live set -- but a branch can be deleted, and with it every trace of what
that unit was and which goals belonged to it. So the registry has exactly one job: survive.

That job is what every test here is really about, and it splits into four requirements the issue
states as its definition of done:

  1. the schema ROUND-TRIPS -- what is read back equals what was written, or the backup is fiction;
  2. a write for unit A NEVER OPENS unit B's file, and two concurrent picks on different units never
     touch the same path -- the ledger's one-file-per-writer rule, applied one axis over. A single
     shared table rewritten on every pick has every write touching the same lines, so two concurrent
     goals corrupt the one file that exists to be a backup;
  3. a MISSING OR CORRUPT `index.json` degrades to "no units known" rather than raising -- a registry
     that throws takes out the pick path, which is worse than a registry that is merely empty;
  4. `authorized` ABSENT MEANS FALSE -- the safe state is the one you get by doing nothing.

Requirement 2 is tested by RECORDING THE PATHS TOUCHED, never by inspecting the result: a write that
opened B's file, read it, and happened to write it back unchanged passes every result-shaped
assertion there is, and is exactly the write that loses B's data when two of them interleave.

WRITTEN AGAINST A MUTATION RUN. `tests/test_features.py`'s own docstring records the lesson this
file inherits: its first version had 100% line coverage and still let seven mutants live, every one
at a LOOSENING edge -- where the line executes either way and only the accepted language changes.
So every tightening this module makes (`is True`, not truthiness; a real `False`, not a falsy one;
a bounded digit run with an end-of-STRING anchor, not `isdigit()` and not `$`) has a test that fails
when that tightening ALONE is removed, via `_mod_with` -- which rebuilds the module from source with
one substitution applied, the only way to ask "does the code actually follow this rule?" rather than
"does the rule's text appear in it?".

AND THE HARNESS ITSELF IS THE THING MOST LIKELY TO LIE. Three separate harness bugs on this one
module, each of which reported success while testing nothing, and each found only because something
downstream looked implausible:

  1. `_mod_with` replaced only the FIRST occurrence of its target -- and the module quotes its own
     rules in the docstring above the line implementing them, so two "mutants" were applied to prose
     and reported green over untouched code. Fixed by replacing every occurrence.
  2. The differs-from-HEAD gate in the external runner loaded its HEAD copy from a temp directory,
     where `_load("features")` could not resolve -- so HEAD's probe was an import error for EVERY
     mutant, which trivially differs from any real value, and the gate passed vacuously 42 times.
  3. Six probes were too weak to distinguish their own mutant (a temp file observed after it had
     already been replaced away; a smuggled key the mutant could return first anyway), reporting
     EQUIVALENT for mutants the suite does kill.

The rule that survives all three: an assertion about a mutant is worth nothing until the mutant has
been shown to behave differently from HEAD on some input. Every verdict in the committed table is
gated on that, and this file's own `_mod_with` tests each assert the mutant's behaviour, never just
that the suite went red.
"""
import importlib.util
import inspect
import json
import os
import pathlib
import re
import stat
import threading
import types

import pytest

import path_recorder

ROOT = pathlib.Path(__file__).resolve().parent.parent
P = ROOT / "skills" / "sigma-loop" / "scripts" / "feature_registry.py"
FEATURES_P = ROOT / "skills" / "sigma-loop" / "scripts" / "features.py"


def _mod():
    spec = importlib.util.spec_from_file_location("feature_registry", P)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _mod_with(old, new):
    """The module rebuilt with a source substitution applied.

    TWO GUARDS, BOTH LEARNED THE HARD WAY ON THIS FILE. The target is asserted to exist -- a harness
    whose `old` has drifted out of the module reports "no survivors" while testing nothing. And the
    substitution is applied to EVERY occurrence, not the first: this module documents its own rules
    by quoting them, so `raw.get("authorized") is True` and `parents=True, exist_ok=True` each appear
    in a DOCSTRING above the line that implements them. A first-occurrence replace mutated the prose
    and left the code untouched -- a green mutation report over an unmutated module, which is the
    precise failure a mutation run exists to rule out. Two of the three mutants here were born
    surviving for exactly that reason.

    Each caller still asserts the mutant's BEHAVIOUR differs, which is the only proof that survives
    a harness bug of a kind not yet imagined."""
    src = P.read_text(encoding="utf-8")
    assert old in src, "mutation target has drifted out of the source: %r" % (old,)
    namespace = {"__name__": "feature_registry_variant", "__file__": str(P)}
    exec(compile(src.replace(old, new), str(P), "exec"), namespace)         # noqa: S102 - test-only
    return types.SimpleNamespace(**namespace)


def _entry(**over):
    """A full, canonical entry -- every field present, so a test that removes ONE field is testing
    that field and nothing else."""
    base = {"title": "Manifest duration contract", "owner": "@unit-owner", "open": True,
            "parent": None, "tracking_issue": "org/repo#3100", "priority": None,
            "repos": {"org/repo": {"branch": "feature/int-contract", "owner": "@unit-owner",
                                   "authorized": True, "goals": [2871, 2879]}}}
    base.update(over)
    return base


def _write_index(d, doc):
    d.mkdir(parents=True, exist_ok=True)
    (d / "index.json").write_text(json.dumps(doc), encoding="utf-8")


# --------------------------------------------------------------------------- 1. the schema

def test_the_schema_version_is_the_one_the_design_names():
    """A version key nobody agrees on is not a version key. `index.json` is duplicated in full into
    every participating repo, so the string is a cross-repo contract, not an internal detail."""
    assert _mod().SCHEMA == "sigma/features@1"


def test_every_document_this_module_writes_carries_the_schema():
    r = _mod()
    assert r.document({})["schema"] == r.SCHEMA
    assert r.document({"voice": _entry()})["schema"] == r.SCHEMA


def test_a_registry_round_trips_through_the_document_form():
    """The backup's whole promise: what is read back equals what was written."""
    r = _mod()
    registry = {"int-contract": _entry(),
                "voice-interview": _entry(title="Voice interview", parent="int-contract",
                                          repos={"org/a": {"branch": "feature/voice-interview",
                                                           "owner": "@who", "authorized": False,
                                                           "goals": [7]}})}
    assert r.parse(r.document(registry)) == registry


def test_the_round_trip_is_idempotent_on_a_partial_entry():
    """A hand-written entry omitting optional fields must survive the trip too -- normalised once,
    then stable, never drifting a little further on each pass."""
    r = _mod()
    once = r.parse({"schema": r.SCHEMA, "features": {"voice": {"title": "Voice"}}})
    assert r.parse(r.document(once)) == once


def test_the_written_document_is_byte_stable_across_two_serialisations():
    """The registry is COMMITTED. A serialiser whose key order wanders makes every pick a diff."""
    r = _mod()
    registry = {"b": _entry(), "a": _entry()}
    assert r.dumps(r.document(registry)) == r.dumps(r.document(dict(reversed(list(
        registry.items())))))


def test_the_written_document_ends_in_a_newline():
    """A committed JSON file with no trailing newline is a permanent one-line diff."""
    r = _mod()
    assert r.dumps(r.document({})).endswith("\n")


# --------------------------------------------------------------------------- 4. authorized

def test_authorized_absent_means_false():
    """THE explicit absent case. `authorized` is a board owner's per-unit grant; the safe state has
    to be the one you get by doing nothing, or a repo that never opted in is opted in."""
    r = _mod()
    entry = r.parse({"schema": r.SCHEMA,
                     "features": {"voice": {"repos": {"org/repo": {"branch": "feature/voice"}}}}})
    assert entry["voice"]["repos"]["org/repo"]["authorized"] is False


def test_authorized_absent_means_false_through_the_helper_too():
    """The helper is what the access check calls, and it is handed RAW entries as often as
    normalised ones -- so the default cannot live only in the normaliser."""
    r = _mod()
    assert r.is_authorized({"repos": {"org/repo": {"branch": "feature/voice"}}}, "org/repo") is False
    assert r.is_authorized(_entry(), "org/repo") is True


def test_only_a_real_boolean_true_grants_authorisation():
    """THE loosening edge. Under `bool(value)` the string "false" GRANTS -- a non-empty string is
    truthy -- which is the exact opposite of what it says. Every one of these is a hand-edit or a
    YAML-ish round trip someone will really produce."""
    r = _mod()
    for value in ("true", "false", "True", 1, -1, [1], {"a": 1}, "0", 0.5):
        entry = r.parse({"schema": r.SCHEMA,
                         "features": {"v": {"repos": {"org/r": {"authorized": value}}}}})
        assert entry["v"]["repos"]["org/r"]["authorized"] is False, value
        assert r.is_authorized({"repos": {"org/r": {"authorized": value}}}, "org/r") is False, value


def test_a_truthy_authorisation_check_is_a_mutant_this_suite_kills():
    """Pins the tightening itself: replace `is True` with truthiness and "false" starts granting."""
    r = _mod_with('"authorized": raw.get("authorized") is True,',
                  '"authorized": bool(raw.get("authorized")),')
    entry = r.parse({"schema": r.SCHEMA, "features": {"v": {"repos": {"org/r":
                                                                      {"authorized": "false"}}}}})
    assert entry["v"]["repos"]["org/r"]["authorized"] is True, (
        "the mutant did not change behaviour, so the real assertion above proves nothing")


def test_a_truthy_check_in_the_access_helper_is_a_mutant_this_suite_kills():
    """The SECOND copy of the grant rule, mutated on its own. The helper is what the access check
    calls on raw entries, so a tightening that holds only in the normaliser holds nowhere useful."""
    r = _mod_with('return one.get("authorized") is True', 'return bool(one.get("authorized"))')
    assert r.is_authorized({"repos": {"org/r": {"authorized": "false"}}}, "org/r") is True, (
        "the mutant did not change behaviour, so the helper's own rule is untested")


def test_the_helper_and_the_normaliser_cannot_disagree_about_a_grant():
    """Two copies of "absent means false" is one copy too many. Same verdict, raw or normalised."""
    r = _mod()
    for value in ({}, {"authorized": True}, {"authorized": "true"}, {"authorized": None}):
        raw = {"repos": {"org/r": value}}
        normalised = r.parse({"schema": r.SCHEMA, "features": {"v": raw}})["v"]
        assert r.is_authorized(raw, "org/r") == r.is_authorized(normalised, "org/r") \
            == normalised["repos"]["org/r"]["authorized"]


def test_the_helper_tolerates_every_missing_layer():
    """It is called on whatever the registry happens to hold, including nothing at all. An access
    check that raises is an access check that gets wrapped in a bare `except` at the call site."""
    r = _mod()
    for entry in (None, {}, [], "x", {"repos": None}, {"repos": []}, {"repos": {"other": {}}},
                  {"repos": {"org/r": None}}, {"repos": {"org/r": "yes"}}):
        assert r.is_authorized(entry, "org/r") is False, entry


# --------------------------------------------------------------------------- the other defaults

def test_open_defaults_to_true_and_only_a_real_false_closes():
    """`open: false` is how a closed unit is marked -- labels are never deleted, so this is the one
    field tooling filters on. Closing is the direction that makes a unit's goals stop being
    recorded, so it takes an unambiguous boolean; a stringly-typed "false" is a guess."""
    r = _mod()

    def opened(value):
        raw = {} if value is _MISSING else {"open": value}
        return r.parse({"schema": r.SCHEMA, "features": {"v": raw}})["v"]["open"]

    assert opened(_MISSING) is True
    assert opened(False) is False
    for value in ("false", 0, None, "", [], "true"):
        assert opened(value) is True, value


def test_goals_keep_only_real_issue_numbers():
    """`True` must never become goal 1. `isinstance(True, int)` is True in Python, so a bool slips
    through any `isinstance(x, int)` guard that does not exclude it -- and a goal number minted from
    a boolean is a link to somebody else's issue."""
    r = _mod()
    got = r.parse({"schema": r.SCHEMA, "features": {"v": {"repos": {"org/r": {
        "goals": [2871, True, False, "2879", 0, -3, 1.5, None, "abc", [4], {"n": 5}]}}}}})
    assert got["v"]["repos"]["org/r"]["goals"] == [2871, 2879]


def test_a_bool_accepted_as_a_goal_is_a_mutant_this_suite_kills():
    r = _mod_with("if isinstance(value, bool):\n        return None",
                  "if False:\n        return None")
    got = r.parse({"schema": r.SCHEMA,
                   "features": {"v": {"repos": {"org/r": {"goals": [True]}}}}})
    assert got["v"]["repos"]["org/r"]["goals"] == [1], (
        "the mutant did not change behaviour, so the bool guard is untested")


def test_a_goal_written_as_an_absurdly_long_digit_string_does_not_raise():
    """`str.isdigit()` is not enough to make `int()` safe: since the CVE-2020-10735 fix `int()`
    REFUSES an over-long digit string (ledger._seq documents the same trap), and `isdigit()` is also
    true of Unicode digits `int()` cannot parse at all. A backup that raises on a hand-edit is
    worse than one that drops a number."""
    r = _mod()
    got = r.parse({"schema": r.SCHEMA, "features": {"v": {"repos": {"org/r": {
        "goals": ["9" * 5000, "²", "٠١", " 12 ", "12\n", "1234567890123", 34]}}}}})
    assert got["v"]["repos"]["org/r"]["goals"] == [34]


def test_isdigit_instead_of_a_bounded_digit_run_is_a_mutant_this_suite_kills():
    r = _mod_with("_GOAL_RE.match(value)", "value.isdigit()")
    with pytest.raises(ValueError):
        r.parse({"schema": r.SCHEMA,
                 "features": {"v": {"repos": {"org/r": {"goals": ["²"]}}}}})


def test_a_dollar_anchor_instead_of_end_of_string_is_a_mutant_this_suite_kills():
    """`$` also matches immediately before a TRAILING NEWLINE, so `"12\\n"` would parse as goal 12.
    This is the same widening the no-strip rule refuses on the other side of the value, and `\\Z`->`$`
    is the likelier of the two future edits by a wide margin -- an independent mutation run found it
    live while the strip half was already pinned."""
    r = _mod_with(r'_GOAL_RE = re.compile(r"[0-9]{1,9}\Z")',
                  r'_GOAL_RE = re.compile(r"[0-9]{1,9}$")')
    got = r.parse({"schema": r.SCHEMA,
                   "features": {"v": {"repos": {"org/r": {"goals": ["12\n"]}}}}})
    assert got["v"]["repos"]["org/r"]["goals"] == [12], (
        "the mutant did not change behaviour, so the end-of-string anchor is untested")


def test_widening_the_digit_bound_is_a_mutant_this_suite_kills():
    """The bound is what makes the `int()` on the next line unconditionally safe rather than
    safe-in-practice, and the docstring justifies the SPECIFIC number -- so something has to hold
    it. The committed corpus only tested `"9"*5000`, which every plausible bound rejects."""
    r = _mod_with(r'_GOAL_RE = re.compile(r"[0-9]{1,9}\Z")',
                  r'_GOAL_RE = re.compile(r"[0-9]{1,20}\Z")')
    got = r.parse({"schema": r.SCHEMA,
                   "features": {"v": {"repos": {"org/r": {"goals": ["1234567890123"]}}}}})
    assert got["v"]["repos"]["org/r"]["goals"] == [1234567890123], (
        "the mutant did not change behaviour, so the digit bound is untested")


def test_stripping_a_goal_string_before_matching_is_a_mutant_this_suite_kills():
    """The tolerance NOT granted, pinned so it cannot be granted by accident. Nothing that
    serialises JSON emits `" 12 "`; widening for it buys a case that does not occur."""
    r = _mod_with("_GOAL_RE.match(value)", "_GOAL_RE.match(value.strip())")
    got = r.parse({"schema": r.SCHEMA,
                   "features": {"v": {"repos": {"org/r": {"goals": [" 12 "]}}}}})
    assert got["v"]["repos"]["org/r"]["goals"] == [12], (
        "the mutant did not change behaviour, so the no-strip rule is untested")


def test_goals_are_deduplicated_in_first_seen_order():
    """#1473 re-picks a goal onto a unit it is already recorded under, and the record has to stay a
    set of goals rather than a tally of picks. Order is PICK order, deliberately not sorted: the
    sequence is itself part of what the backup remembers."""
    r = _mod()
    got = r.parse({"schema": r.SCHEMA, "features": {"v": {"repos": {"org/r": {
        "goals": [9, 3, 9, "3", 4]}}}}})
    assert got["v"]["repos"]["org/r"]["goals"] == [9, 3, 4]


def test_a_repo_entry_that_is_not_a_mapping_becomes_an_empty_one_rather_than_vanishing():
    """The repo KEY is the fact worth keeping -- that this unit touches that repo. Its garbled
    detail degrades to the safe defaults; dropping the key would lose the relationship itself."""
    r = _mod()
    got = r.parse({"schema": r.SCHEMA, "features": {"v": {"repos": {"org/r": "feature/v"}}}})
    assert got["v"]["repos"]["org/r"] == {"branch": None, "owner": None, "authorized": False,
                                          "goals": []}


def test_a_non_string_repo_key_is_dropped():
    r = _mod()
    got = r.parse({"schema": r.SCHEMA, "features": {"v": {"repos": {"org/r": {}, "": {}}}}})
    assert list(got["v"]["repos"]) == ["org/r"]


def test_a_non_string_title_or_owner_degrades_rather_than_raising():
    r = _mod()
    got = r.parse({"schema": r.SCHEMA,
                   "features": {"v": {"title": 7, "owner": ["@a"], "tracking_issue": {}}}})
    assert got["v"]["title"] == "" and got["v"]["owner"] is None
    assert got["v"]["tracking_issue"] is None


def test_parent_is_kept_verbatim_because_resolving_it_is_not_this_modules_job():
    """`parent` carries the sub-child relationship. It is never turned into a path here -- only
    `unit_path` does that, and it validates independently -- so keeping an unresolvable value beats
    deleting it: the registry is a record, not a cache."""
    r = _mod()
    got = r.parse({"schema": r.SCHEMA, "features": {"v": {"parent": "../../etc"}}})
    assert got["v"]["parent"] == "../../etc"
    assert r.parse({"schema": r.SCHEMA, "features": {"v": {"parent": 3}}})["v"]["parent"] is None


def test_priority_is_kept_verbatim_because_ranking_it_is_not_this_modules_job():
    """#2261. `priority` is `parent`'s rule, for `parent`'s reason: the registry is a record, not a
    cache. `discovery.priority_rank` is the ONE opinion about what a priority string means -- it
    already accepts a bare tier, any case, a value still carrying the `priority:` label prefix, and
    an adopter's configured aliases, and it ranks anything it cannot classify as unprioritised
    rather than guessing. A second opinion here would be a second answer to that question, and the
    one thing a normaliser must never do is decide that a value it does not recognise was not
    said."""
    r = _mod()
    for written in ("P0", "p3", "priority:P1", "Critical", "  P2  ", ""):
        got = r.parse({"schema": r.SCHEMA, "features": {"v": {"priority": written}}})
        assert got["v"]["priority"] == written, written
    assert r.parse({"schema": r.SCHEMA, "features": {"v": {"priority": 0}}})["v"]["priority"] is None
    assert r.parse({"schema": r.SCHEMA, "features": {"v": {}}})["v"]["priority"] is None


def test_a_registry_carrying_a_priority_round_trips_through_the_document_form():
    """The invariant slice 1 of `.sdlc/design/2253.md` names explicitly: `parse(document(r)) == r`
    has to keep holding once the entry grows a seventh key. Stated over a registry whose units carry
    DIFFERENT priorities, so a normaliser that dropped the field, or defaulted every unit to one
    value, fails here rather than passing on a coincidence."""
    r = _mod()
    registry = {"int-contract": _entry(priority="P0"),
                "voice-interview": _entry(priority="P3"),
                "core": _entry()}
    assert r.parse(r.document(registry)) == registry
    assert [e["priority"] for e in r.parse(r.document(registry)).values()] == ["P0", "P3", None]


def test_dropping_priority_from_the_normaliser_is_a_mutant_this_suite_kills():
    """The whole of slice 1's registry half is one line in a fixed dict, so the line is pinned as a
    mutant rather than merely exercised: the normaliser returns a WHITELIST, and a key it does not
    name is dropped on every round trip. The mutant's own behaviour is asserted against HEAD's, not
    just the suite's colour."""
    r, variant = _mod(), _mod_with('            "priority": _text(raw.get("priority")),\n', "")
    doc = {"schema": r.SCHEMA, "features": {"v": {"title": "T", "priority": "P0"}}}
    assert r.parse(doc)["v"]["priority"] == "P0"
    assert "priority" not in variant.parse(doc)["v"]
    assert variant.parse(doc)["v"]["title"] == "T"           # the mutant loses THAT field and no other


def test_the_priority_field_did_not_bump_the_schema_string():
    """B-1 of `.sdlc/design/2253.md`, resolved: backward compatibility over loud refusal. Bumping
    the version is what makes an older reader refuse the whole document -- every unit, every goal,
    every grant -- and the team chose the narrower loss. This test is the record that the choice was
    made deliberately: if a later change bumps the string, this line is what asks whether the reason
    was good enough to blind every install that has not upgraded."""
    assert _mod().SCHEMA == "sigma/features@1"


def test_an_older_install_loses_the_priority_field_and_nothing_else():
    """The accepted cost of staying on `@1`, MEASURED rather than asserted in prose. The mutant here
    is not a hypothetical -- removing the `priority` line from the normaliser reconstructs exactly
    what a plugin predating #2261 does with a document this one writes, so the blast radius of B-1's
    decision is read off a real run.

    Both halves matter. The field is gone, which is the cost. Everything else is intact, which is
    the reason the cost was accepted -- and it is the half a bump to `@2` would have destroyed, since
    a document declaring a version the reader does not know contributes NOTHING at all."""
    r, older = _mod(), _mod_with('            "priority": _text(raw.get("priority")),\n', "")
    mine = _entry(priority="P0", parent="program", tracking_issue="org/repo#412")
    document = r.document({"int-contract": mine})
    theirs = older.parse(document)["int-contract"]
    assert "priority" not in theirs
    for key in ("title", "owner", "open", "parent", "tracking_issue", "repos"):
        assert theirs[key] == r.normalise_entry(mine)[key], key
    # And the whole document is still READABLE by that older install -- the unit did not vanish.
    assert list(older.parse(document)) == ["int-contract"]


# --------------------------------------------------------------------------- names are paths

_MISSING = object()


def test_the_legal_unit_name_rule_is_features_pys_own_not_a_second_copy():
    """A unit name becomes BOTH a `feature:<name>` label and a filename here, so two rules would be
    two answers. The `.lock` clause is the tell: git rejects `voice.lock` but ACCEPTS `voice.LOCK`,
    so a case-insensitive re-derivation gets that one pair wrong and nothing else."""
    r = _mod()
    spec = importlib.util.spec_from_file_location("features", FEATURES_P)
    features = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(features)
    for name in ("voice", "voice-interview", "v1.2", "voice.LOCK", "a.lockfile", "A", "x9",
                 "voice.lock", "a/b", "..", "a..b", ".hidden", "x.", "", "-x", "voice ", "TBD."):
        assert r.is_unit_name(name) == features._is_unit_name(name), name


def test_unit_path_refuses_a_name_that_is_not_one():
    """A name that is not a name is a PATH. `../../etc/passwd` as a unit name must never become a
    file, and must never become one silently either -- the write side raises where the read side
    degrades, because a caller writing a bad name has a bug, not a corrupt file."""
    r = _mod()
    for name in ("../escape", "a/b", "", ".", "..", "voice.lock", None, 7, "x."):
        with pytest.raises(ValueError):
            r.unit_path("/tmp/nowhere", name)


def test_write_unit_refuses_an_illegal_name_and_writes_nothing(tmp_path):
    r = _mod()
    with pytest.raises(ValueError):
        r.write_unit(tmp_path, "../escape", _entry())
    assert not list(tmp_path.rglob("*")), sorted(p.name for p in tmp_path.rglob("*"))


def test_a_unit_key_that_is_not_a_legal_name_is_dropped_on_read(tmp_path):
    """A key the write side could never round-trip is a key the read side must not hand out: there
    is no file it could ever live in, and `index.json` is copied in wholesale from sibling repos, so
    it is genuinely an untrusted document."""
    r = _mod()
    _write_index(tmp_path, {"schema": r.SCHEMA,
                            "features": {"ok": _entry(), "../../etc/passwd": _entry(),
                                         "a/b": _entry(), "voice.lock": _entry(), "": _entry()}})
    assert list(r.read(tmp_path)) == ["ok"]


# ------------------------------------------------------- 3. missing or corrupt degrades, never raises

def test_a_missing_features_directory_is_no_units_known(tmp_path):
    assert _mod().read(tmp_path / "nothing-here") == {}


def test_a_missing_index_is_no_units_known(tmp_path):
    tmp_path.joinpath("features").mkdir()
    assert _mod().read(tmp_path / "features") == {}


@pytest.mark.parametrize("text", [
    "",                                   # truncated to nothing by a crash mid-write
    "   \n",
    "{",                                  # truncated JSON
    "not json at all",
    "[]",                                 # valid JSON, wrong top-level type
    '"a string"',
    "null",
    "17",
    '{"schema": "sigma/features@1"}',                       # no features key
    '{"schema": "sigma/features@1", "features": []}',       # features is not a mapping
    '{"schema": "sigma/features@1", "features": "voice"}',
    '{"features": {"voice": {}}}',                              # no schema key
    '{"schema": "sigma/features@2", "features": {"voice": {}}}',   # a version we cannot read
    '{"schema": 1, "features": {"voice": {}}}',
])
def test_a_corrupt_or_unreadable_index_degrades_to_no_units_known(tmp_path, text):
    """A registry that throws takes out the pick path. Empty is a recoverable state; an exception
    escaping into the loop is not."""
    d = tmp_path / "features"
    d.mkdir()
    (d / "index.json").write_text(text, encoding="utf-8")
    assert _mod().read(d) == {}


def test_an_index_of_undecodable_bytes_degrades_rather_than_raising(tmp_path):
    """A plain utf-8 read raises UnicodeDecodeError -- a ValueError, NOT an OSError -- on one bad
    byte, and a BOM on line 1 silently eats the document. `ledger.read_all` carries both guards for
    the same reason; this is the same file family and the same failure."""
    d = tmp_path / "features"
    d.mkdir()
    (d / "index.json").write_bytes(b"\xff\xfe{\x00bad")
    assert _mod().read(d) == {}


def test_one_bad_byte_costs_one_character_not_the_whole_registry(tmp_path):
    """`errors="replace"` earns its place here rather than in the never-raises promise: with the
    guards widened to catch `ValueError`, a strict decode no longer ESCAPES -- it just throws the
    entire document away over a single byte. A backup that discards every unit because one title
    picked up a stray byte is exactly the loss it exists to prevent."""
    r = _mod()
    d = tmp_path / "features"
    d.mkdir()
    good = json.dumps({"schema": r.SCHEMA, "features": {"voice": _entry(title="TITLE")}})
    (d / "index.json").write_bytes(good.replace("TITLE", "TIT?E").encode("utf-8")
                                   .replace(b"?", b"\xff"))
    got = r.read(d)
    assert list(got) == ["voice"] and got["voice"]["title"] == "TIT�E"


def test_a_byte_order_mark_does_not_hide_the_document(tmp_path):
    r = _mod()
    d = tmp_path / "features"
    d.mkdir()
    (d / "index.json").write_bytes(b"\xef\xbb\xbf" + json.dumps(
        {"schema": r.SCHEMA, "features": {"voice": _entry()}}).encode("utf-8"))
    assert list(r.read(d)) == ["voice"]


def test_an_index_that_cannot_be_opened_degrades_to_no_units_known(tmp_path):
    """Not the same failure as corrupt content: a directory in the file's place, or a mode nothing
    can read, raises OSError from `open` before any parsing happens."""
    r = _mod()
    d = tmp_path / "features"
    d.mkdir()
    (d / "index.json").mkdir()                       # a directory where the file should be
    assert r.read(d) == {}


def test_a_units_directory_that_cannot_be_listed_yields_no_units(tmp_path):
    """MEASURED, and the previous docstring here had it exactly backwards -- it claimed `glob()`
    "raises for a directory it cannot stat", which would have made this test the thing that pinned
    `_unit_files`' `except OSError`. It does not: `pathlib.Path.glob` swallows its own scan errors,
    so a mode-000 directory returns [] just as a missing one does, and this test passes with that
    guard deleted. What it pins is therefore the OUTCOME (an unreadable units directory costs the
    units, not the process), not the mechanism. The mechanism that genuinely needs a guard is the
    NUL path, which raises `ValueError` -- see the test two above and `_unit_files`' own docstring,
    which is the one this file now agrees with."""
    if os.geteuid() == 0:                            # root ignores the mode bits
        pytest.skip("running as root; the mode has no effect")
    r = _mod()
    d = tmp_path / "features"
    (d / "units").mkdir(parents=True)
    os.chmod(d / "units", 0)
    try:
        assert r.read(d) == {}
    finally:
        os.chmod(d / "units", stat.S_IRWXU)


def test_a_deeply_nested_index_degrades_rather_than_raising(tmp_path):
    """A deeply nested document raises RecursionError -- a RuntimeError, NOT a ValueError -- so
    `except ValueError` alone lets it escape. The tripping depth is interpreter-dependent, which is
    exactly why the TYPE is caught rather than a depth being bounded."""
    d = tmp_path / "features"
    d.mkdir()
    (d / "index.json").write_text("[" * 200000 + "]" * 200000, encoding="utf-8")
    assert _mod().read(d) == {}


def test_one_corrupt_unit_file_does_not_blind_the_others(tmp_path):
    """The whole point of per-unit files: one bad file costs one unit, never the registry."""
    r = _mod()
    r.write_unit(tmp_path, "alpha", _entry())
    r.write_unit(tmp_path, "beta", _entry())
    r.unit_path(tmp_path, "beta").write_text("{ truncated", encoding="utf-8")
    assert list(r.read(tmp_path)) == ["alpha"]


def test_a_path_the_operating_system_cannot_express_degrades_rather_than_raising(tmp_path):
    """MEASURED, and it corrected the guard. `pathlib.Path.glob` swallows the scan errors an
    `except OSError` was written for -- a missing, unlistable or not-actually-a-directory path all
    return [] on CPython 3.13. The failure that DOES escape is an embedded NUL, which raises
    `ValueError` from the syscall wrapper, not `OSError`, and so walked straight through a guard
    that named only the latter. Same shape as `ledger.read_all`'s RecursionError note: the exception
    you have to catch is the one whose base class you did not think of."""
    r = _mod()
    assert r.read(str(tmp_path) + "/na\x00me") == {}


def test_a_diagnostic_that_cannot_be_written_is_still_not_an_exception(monkeypatch):
    """`_note` is the fail-open half of the schema check. A closed or broken stderr -- a detached
    process, a full pipe -- must cost the message, never the read."""
    r = _mod()

    class Broken:
        def write(self, *_a, **_kw):
            raise RuntimeError("stderr is gone")

    monkeypatch.setattr(r.sys, "stderr", Broken())
    assert r.parse({"schema": "sigma/features@2", "features": {"v": {}}}) == {}


def test_reading_never_raises_on_a_hostile_registry(tmp_path):
    """The blanket promise, stated once as a corpus so a new tolerance cannot be added without a
    case here: whatever is on disk, `read` returns a mapping."""
    r = _mod()
    d = tmp_path / "features"
    (d / "units").mkdir(parents=True)
    (d / "index.json").write_text('{"schema": "sigma/features@1", "features": {"ok": {}}}',
                                  encoding="utf-8")
    (d / "units" / "a.json").write_text("", encoding="utf-8")
    (d / "units" / "b.json").write_bytes(b"\x00\x01\x02")
    (d / "units" / "c.json").write_text("[]", encoding="utf-8")
    (d / "units" / "d.json").mkdir()
    (d / "units" / "not-json.txt").write_text("ignored", encoding="utf-8")
    got = r.read(d)
    assert isinstance(got, dict) and list(got) == ["ok"]


# --------------------------------------------------------------------------- 2. write isolation

# The recorder is shared, in `tests/path_recorder.py`; its docstring says why it listens to the
# interpreter's audit events rather than patching `open` (issue #241: blind on Python 3.10).
_Recorder = path_recorder.Recorder


def test_the_path_recorder_actually_records(tmp_path):
    """The positive control for the control. If this fails, every isolation test below is vacuous."""
    r = _mod()
    with _Recorder() as rec:
        r.write_unit(tmp_path, "alpha", _entry())
    assert str(r.unit_path(tmp_path, "alpha").resolve()) in rec.paths(tmp_path)


def test_the_path_recorder_records_an_open_not_only_a_replace(tmp_path):
    """The control for the `open` spies specifically. A read is the operation that legitimately
    opens a file, so it is the only way to show the open route is wired at all."""
    r = _mod()
    r.write_index(tmp_path, {"alpha": _entry()})
    with _Recorder() as rec:
        r.read(tmp_path)
    assert str(r.index_path(tmp_path).resolve()) in {str(pathlib.Path(x).resolve())
                                                     for x in rec.opened}


def test_the_isolation_check_catches_a_write_that_reads_a_sibling(tmp_path):
    """The control that matters: the module rebuilt doing the precise thing its docstring forbids --
    reading a sibling unit before writing its own -- and the recorder must see it. Without this, the
    isolation tests below could be passing because the harness is blind rather than because the
    write is clean, and no result-shaped assertion would tell the two apart."""
    r = _mod_with("    _protect(features_dir)\n    _atomic_write_text(path, dumps(",
                  "    _protect(features_dir)\n"
                  "    _read_json(units_dir(features_dir) / ('beta' + UNIT_SUFFIX))\n"
                  "    _atomic_write_text(path, dumps(")
    r.write_unit(tmp_path, "beta", _entry())
    with _Recorder() as rec:
        r.write_unit(tmp_path, "alpha", _entry())
    beta = str((r.units_dir(tmp_path) / ("beta" + r.UNIT_SUFFIX)).resolve())
    assert beta in rec.paths(tmp_path), (
        "the sabotaged write read beta and the recorder did not notice -- every isolation "
        "assertion in this file would be vacuous")


def test_a_write_for_one_unit_never_opens_another_units_file(tmp_path):
    """REQUIREMENT 2, proved by the paths touched rather than by the result. A write that reads B,
    holds it, and writes it back unchanged passes every result-shaped assertion and still loses B
    when two of them interleave."""
    r = _mod()
    r.write_unit(tmp_path, "beta", _entry(title="Beta"))
    r.write_index(tmp_path, {"beta": _entry(title="Beta")})

    with _Recorder() as rec:
        r.write_unit(tmp_path, "alpha", _entry(title="Alpha"))

    touched = rec.paths(tmp_path)
    assert str(r.unit_path(tmp_path, "alpha").resolve()) in touched          # positive control
    assert str(r.unit_path(tmp_path, "beta").resolve()) not in touched
    assert str(r.index_path(tmp_path).resolve()) not in touched
    assert not [p for p in touched if "beta" in pathlib.Path(p).name]


def test_a_write_leaves_every_other_units_bytes_untouched(tmp_path):
    """The result-shaped half, kept as well as the path-shaped one: they fail for different
    reasons, and a byte comparison is what catches a write that truncates rather than opens."""
    r = _mod()
    r.write_unit(tmp_path, "beta", _entry(title="Beta"))
    before = r.unit_path(tmp_path, "beta").read_bytes()
    r.write_unit(tmp_path, "alpha", _entry(title="Alpha"))
    assert r.unit_path(tmp_path, "beta").read_bytes() == before


def test_two_writes_on_different_units_share_no_file_path(tmp_path):
    """REQUIREMENT 2, second clause. The intersection of the two writes' file paths must be empty;
    the shared DIRECTORY is named as the deliberate carve-out and asserted to be exactly that."""
    r = _mod()
    with _Recorder() as first:
        r.write_unit(tmp_path, "alpha", _entry())
    with _Recorder() as second:
        r.write_unit(tmp_path, "beta", _entry())

    assert first.files(tmp_path) and second.files(tmp_path)                  # positive control
    assert first.files(tmp_path) & second.files(tmp_path) == set()
    shared = first.paths(tmp_path) & second.paths(tmp_path)
    assert all(pathlib.Path(p).is_dir() for p in shared), sorted(shared)


def test_a_write_never_rewrites_the_chart_sheet(tmp_path):
    """`index.json` is the one path every unit WOULD share. Keeping the pick path off it is what
    makes the per-unit files worth having at all."""
    r = _mod()
    r.write_index(tmp_path, {"beta": _entry()})
    before = r.index_path(tmp_path).read_bytes()
    r.write_unit(tmp_path, "alpha", _entry())
    assert r.index_path(tmp_path).read_bytes() == before


def test_two_concurrent_writers_on_different_units_both_land(tmp_path):
    """The real thing, not a simulation: two threads racing on a features dir that does not exist
    yet, so both hit the same `mkdir` at the same moment. Without `exist_ok` one of them dies."""
    r = _mod()
    errors, ready = [], threading.Barrier(9)

    def write(name):
        try:
            ready.wait(timeout=10)
            for i in range(20):
                r.write_unit(tmp_path / "features", name,
                             _entry(title="%s-%d" % (name, i)))
        except BaseException as exc:                       # noqa: BLE001 - the assertion is below
            errors.append("%s: %r" % (name, exc))

    names = ["u%d" % i for i in range(9)]
    threads = [threading.Thread(target=write, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors
    got = r.read(tmp_path / "features")
    assert sorted(got) == sorted(names)
    for name in names:
        assert got[name]["title"] == "%s-19" % name


def test_dropping_exist_ok_from_the_shared_mkdir_is_a_mutant_this_suite_kills(tmp_path):
    r = _mod_with("path.parent.mkdir(parents=True, exist_ok=True)",
                  "path.parent.mkdir(parents=True, exist_ok=False)")
    r.write_unit(tmp_path, "alpha", _entry())
    with pytest.raises(FileExistsError):
        r.write_unit(tmp_path, "beta", _entry())


def test_a_write_is_atomic_so_a_reader_never_sees_a_half_file(tmp_path):
    """A partial write is worse than no write: the backup's job is to be readable after the crash
    that made it necessary. The temp file must live in the SAME directory -- `os.replace` is only
    guaranteed atomic within one filesystem -- and must not be visible to the unit glob."""
    r = _mod()
    with _Recorder() as rec:
        r.write_unit(tmp_path, "alpha", _entry())
    target = str(r.unit_path(tmp_path, "alpha").resolve())
    assert rec.replaced and rec.replaced[-1] == target, rec.replaced
    tmp_written = rec.replaced[-2]
    assert pathlib.Path(tmp_written).parent == r.unit_path(tmp_path, "alpha").parent
    assert not tmp_written.endswith(".json")
    assert pathlib.Path(tmp_written).name.startswith("alpha.json."), (
        "a leftover temp file has to name the unit it belongs to, or the litter a crash leaves is "
        "untraceable to the write that left it")


def test_a_failed_write_leaves_no_litter_and_still_raises(tmp_path, monkeypatch):
    """The write side does NOT fail open -- a caller told the unit was recorded when it was not is
    the one way this module could lose the backup silently. So the failure propagates, and the temp
    file goes with it: a directory filling with half-written units is its own second failure, and
    the cleanup must not mask the original exception either."""
    r = _mod()
    r.write_unit(tmp_path, "alpha", _entry())            # so units/ exists and is not the failure
    before = sorted(p.name for p in r.units_dir(tmp_path).iterdir())

    def boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(r.os, "replace", boom)
    with pytest.raises(OSError):
        r.write_unit(tmp_path, "beta", _entry())
    assert sorted(p.name for p in r.units_dir(tmp_path).iterdir()) == before


def test_a_cleanup_failure_never_masks_the_write_that_failed(tmp_path, monkeypatch):
    """The trap inside the trap: a bare `except` around the cleanup that let the CLEANUP's error
    escape would report "gone" for a write that actually failed on "disk full", and the operator
    would go looking at the wrong thing."""
    r = _mod()

    def replace_boom(*_a, **_kw):
        raise OSError("disk full")

    def unlink_boom(*_a, **_kw):
        raise OSError("temp file already gone")

    monkeypatch.setattr(r.os, "replace", replace_boom)
    monkeypatch.setattr(r.os, "unlink", unlink_boom)
    with pytest.raises(OSError, match="disk full"):
        r.write_unit(tmp_path, "alpha", _entry())


def test_a_leftover_temp_file_is_not_mistaken_for_a_unit(tmp_path):
    """A crash between mkstemp and replace leaves litter. It must cost nothing on the next read."""
    r = _mod()
    r.write_unit(tmp_path, "alpha", _entry())
    (r.units_dir(tmp_path) / "beta.json.abc123.tmp").write_text(
        json.dumps({"schema": r.SCHEMA, "features": {"beta": _entry()}}), encoding="utf-8")
    assert list(r.read(tmp_path)) == ["alpha"]


def test_only_json_files_under_units_are_read_at_all(tmp_path):
    """The `*.json` glob, tested where it is the ONLY thing standing in the way. The filename-match
    rule catches most litter on its own -- a temp file's own content names the real unit, which its
    truncated name no longer matches -- so the glob looks redundant until a file's truncated name
    matches a key it happens to carry. Then it is the only guard left, and a phantom unit appears in
    the registry from a file no write ever finished."""
    r = _mod()
    r.write_unit(tmp_path, "alpha", _entry())
    litter = "ghost.json.tmp"
    smuggled = litter[:-len(r.UNIT_SUFFIX)]      # the stem the filename match would compute, and
    assert r.is_unit_name(smuggled)              # a legal unit name, so nothing else stops it
    (r.units_dir(tmp_path) / litter).write_text(json.dumps(
        {"schema": r.SCHEMA, "features": {smuggled: _entry(title="phantom")}}), encoding="utf-8")
    assert list(r.read(tmp_path)) == ["alpha"]


# --------------------------------------------------------------------------- shards vs chart sheet

def test_a_unit_file_only_speaks_for_the_unit_its_filename_names(tmp_path):
    """What a per-unit file can say about the registry is bounded by its own name -- otherwise a
    write to alpha's path could rewrite beta, and the isolation would be a convention, not a
    structure."""
    r = _mod()
    r.units_dir(tmp_path).mkdir(parents=True)
    r.unit_path(tmp_path, "alpha").write_text(json.dumps({"schema": r.SCHEMA, "features": {
        "alpha": _entry(title="Alpha"), "beta": _entry(title="Smuggled")}}), encoding="utf-8")
    got = r.read(tmp_path)
    assert list(got) == ["alpha"] and got["alpha"]["title"] == "Alpha"


def test_a_unit_file_naming_a_different_unit_entirely_declares_nothing(tmp_path):
    r = _mod()
    r.units_dir(tmp_path).mkdir(parents=True)
    r.unit_path(tmp_path, "alpha").write_text(json.dumps(
        {"schema": r.SCHEMA, "features": {"beta": _entry()}}), encoding="utf-8")
    assert r.read(tmp_path) == {}


def test_a_unit_files_own_spelling_survives_a_case_insensitive_filesystem(tmp_path):
    """macOS APFS and Windows NTFS resolve `Alpha.json` and `alpha.json` to one file, and GitHub
    label names are case-insensitively unique too -- so two casings were never two units. The file's
    own key is what the registry reports, matched to the filename case-insensitively."""
    r = _mod()
    r.units_dir(tmp_path).mkdir(parents=True)
    r.unit_path(tmp_path, "alpha").write_text(json.dumps(
        {"schema": r.SCHEMA, "features": {"Alpha": _entry(title="Alpha")}}), encoding="utf-8")
    assert list(r.read(tmp_path)) == ["Alpha"]


def test_a_unit_file_wins_over_the_chart_sheet_for_its_own_unit(tmp_path):
    """The chart sheet is a snapshot; a unit file is a write that has not been folded into one yet.
    So for a unit both mention, the file is the later statement."""
    r = _mod()
    r.write_index(tmp_path, {"alpha": _entry(title="stale"), "beta": _entry(title="Beta")})
    r.write_unit(tmp_path, "alpha", _entry(title="fresh"))
    got = r.read(tmp_path)
    assert got["alpha"]["title"] == "fresh"
    assert got["beta"]["title"] == "Beta"


def test_the_chart_sheet_alone_is_enough_when_no_unit_files_exist(tmp_path):
    """A fresh clone, or a registry propagated in from a sibling repo, has only the committed sheet."""
    r = _mod()
    r.write_index(tmp_path, {"alpha": _entry(), "beta": _entry()})
    assert sorted(r.read(tmp_path)) == ["alpha", "beta"]


def test_a_unit_file_replaces_the_sheets_entry_rather_than_merging_into_it(tmp_path):
    """Merging two half-descriptions of one unit invents a third that neither source ever said."""
    r = _mod()
    r.write_index(tmp_path, {"alpha": _entry(repos={"org/old": {"branch": "feature/alpha",
                                                               "goals": [1]}})})
    r.write_unit(tmp_path, "alpha", _entry(repos={"org/new": {"branch": "feature/alpha",
                                                             "goals": [2]}}))
    assert list(r.read(tmp_path)["alpha"]["repos"]) == ["org/new"]


def test_a_unit_file_wins_even_when_the_sheet_spells_the_unit_differently(tmp_path):
    r = _mod()
    r.write_index(tmp_path, {"Alpha": _entry(title="stale")})
    r.write_unit(tmp_path, "alpha", _entry(title="fresh"))
    got = r.read(tmp_path)
    assert list(got) == ["alpha"] and got["alpha"]["title"] == "fresh"


def test_read_unit_reports_only_that_units_own_file(tmp_path):
    r = _mod()
    r.write_index(tmp_path, {"alpha": _entry(title="sheet")})
    assert r.read_unit(tmp_path, "alpha") is None
    r.write_unit(tmp_path, "alpha", _entry(title="file"))
    assert r.read_unit(tmp_path, "alpha")["title"] == "file"
    assert r.read_unit(tmp_path, "beta") is None
    assert r.read_unit(tmp_path, "../escape") is None            # degrades, never raises


def test_what_write_unit_writes_is_what_read_gives_back(tmp_path):
    """The round trip through the real filesystem, not only through the document form."""
    r = _mod()
    entry = _entry(parent="int-contract",
                   repos={"org/a": {"branch": "feature/v", "owner": "@a", "authorized": True,
                                    "goals": [1, 2]},
                          "org/b": {"branch": "feature/v", "owner": "@b", "goals": []}})
    r.write_unit(tmp_path, "voice", entry)
    assert r.read(tmp_path)["voice"] == r.parse(r.document({"voice": entry}))["voice"]


# --------------------------------------------------------------------- #1820: resolve_open_unit
#
# The one new entry point this issue adds: "is `name` a known, OPEN unit?" -- the check
# `handoff.create_tracked_issue`'s `target_unit` override needs before it may stamp a unit onto an
# issue nobody inherited it from. Deliberately in THIS module, not `feature_stamp.py`: it is a pure
# registry READ, the same shape as `read_unit`, and belongs beside it.

def test_resolve_open_unit_returns_the_registered_spelling_of_a_known_open_unit(tmp_path):
    r = _mod()
    sdlc = tmp_path / ".sdlc"
    r.write_index(r.registry_dir(sdlc), {"voice-interview": _entry(open=True)})
    assert r.resolve_open_unit(sdlc, "voice-interview") == "voice-interview"


def test_resolve_open_unit_matches_case_insensitively_but_returns_the_registered_casing(tmp_path):
    """Two casings of one unit are one unit everywhere else in this module (`_drop_same_unit`,
    `unit_key`) -- the registry's OWN spelling is what the label and the branch already use, so a
    caller's own casing must never override it."""
    r = _mod()
    sdlc = tmp_path / ".sdlc"
    r.write_index(r.registry_dir(sdlc), {"Voice-Interview": _entry(open=True)})
    assert r.resolve_open_unit(sdlc, "voice-interview") == "Voice-Interview"
    assert r.resolve_open_unit(sdlc, "VOICE-INTERVIEW") == "Voice-Interview"


def test_resolve_open_unit_refuses_a_closed_unit(tmp_path):
    """#1820's own stated exclusion: a finished unit must never be retargeted onto, explicitly or
    automatically -- so this must refuse even though the caller named it correctly. Indistinguishable
    from "unknown" on purpose (see the function's own docstring): nothing downstream may special-case
    "closed" into a weaker refusal."""
    r = _mod()
    sdlc = tmp_path / ".sdlc"
    r.write_index(r.registry_dir(sdlc), {"done-unit": _entry(open=False)})
    assert r.resolve_open_unit(sdlc, "done-unit") is None


def test_resolve_open_unit_refuses_an_unregistered_name(tmp_path):
    r = _mod()
    sdlc = tmp_path / ".sdlc"
    r.write_index(r.registry_dir(sdlc), {"voice-interview": _entry(open=True)})
    assert r.resolve_open_unit(sdlc, "billing") is None


def test_resolve_open_unit_refuses_an_illegal_name(tmp_path):
    """An illegal unit name (a `/`, a `.lock` suffix, empty) can never be a legal registry key --
    refused exactly as every other entry point into this model refuses one (`features.py` rule 4)."""
    r = _mod()
    sdlc = tmp_path / ".sdlc"
    r.write_index(r.registry_dir(sdlc), {"voice-interview": _entry(open=True)})
    assert r.resolve_open_unit(sdlc, "a/b") is None
    assert r.resolve_open_unit(sdlc, "voice.lock") is None
    assert r.resolve_open_unit(sdlc, "") is None
    assert r.resolve_open_unit(sdlc, None) is None


def test_resolve_open_unit_degrades_to_none_when_the_registry_is_missing(tmp_path):
    """`read()`'s own total-read guarantee, inherited rather than re-implemented: a missing
    `.sdlc/features/` is "no units known", never an error."""
    r = _mod()
    sdlc = tmp_path / ".sdlc"           # nothing written at all -- not even the directory
    assert r.resolve_open_unit(sdlc, "voice-interview") is None


# --------------------------------------------------------------------------- #2362: resolve_any_unit
#
# A sibling to `resolve_open_unit` above, for a caller that must match a unit REGARDLESS of its
# `open` state -- a future tier-1 auto-classifier (slice B) reopening a closed unit on a confident
# match needs to find that unit at all before it can decide whether to reopen it. Built here, in
# this module, for the same reason `resolve_open_unit` is: a pure registry READ belongs beside
# `read_unit`, not in a caller.

def test_resolve_any_unit_returns_the_registered_spelling_of_a_known_open_unit(tmp_path):
    r = _mod()
    sdlc = tmp_path / ".sdlc"
    r.write_index(r.registry_dir(sdlc), {"voice-interview": _entry(open=True)})
    assert r.resolve_any_unit(sdlc, "voice-interview") == "voice-interview"


def test_resolve_any_unit_matches_a_closed_unit_too(tmp_path):
    """The whole point of this function, as opposed to `resolve_open_unit`: a CLOSED unit must
    still resolve, because a caller deciding whether to reopen one has to be able to find it
    first."""
    r = _mod()
    sdlc = tmp_path / ".sdlc"
    r.write_index(r.registry_dir(sdlc), {"done-unit": _entry(open=False)})
    assert r.resolve_any_unit(sdlc, "done-unit") == "done-unit"


def test_resolve_any_unit_matches_case_insensitively_but_returns_the_registered_casing(tmp_path):
    """Same rule as `resolve_open_unit`: two casings are one unit, and the registry's OWN spelling
    wins over whatever casing the caller typed."""
    r = _mod()
    sdlc = tmp_path / ".sdlc"
    r.write_index(r.registry_dir(sdlc), {"Voice-Interview": _entry(open=False)})
    assert r.resolve_any_unit(sdlc, "voice-interview") == "Voice-Interview"
    assert r.resolve_any_unit(sdlc, "VOICE-INTERVIEW") == "Voice-Interview"


def test_resolve_any_unit_refuses_an_unregistered_name(tmp_path):
    r = _mod()
    sdlc = tmp_path / ".sdlc"
    r.write_index(r.registry_dir(sdlc), {"voice-interview": _entry(open=True)})
    assert r.resolve_any_unit(sdlc, "billing") is None


def test_resolve_any_unit_refuses_an_illegal_name(tmp_path):
    r = _mod()
    sdlc = tmp_path / ".sdlc"
    r.write_index(r.registry_dir(sdlc), {"voice-interview": _entry(open=True)})
    assert r.resolve_any_unit(sdlc, "a/b") is None
    assert r.resolve_any_unit(sdlc, "voice.lock") is None
    assert r.resolve_any_unit(sdlc, "") is None
    assert r.resolve_any_unit(sdlc, None) is None


def test_resolve_any_unit_degrades_to_none_when_the_registry_is_missing(tmp_path):
    r = _mod()
    sdlc = tmp_path / ".sdlc"           # nothing written at all -- not even the directory
    assert r.resolve_any_unit(sdlc, "voice-interview") is None


def test_resolve_any_unit_finds_a_unit_that_exists_only_in_the_chart_sheet(tmp_path):
    """THE CRITICAL CASE: a unit whose latest state lives only in `index.json`, with no per-unit
    shard file under `units/` yet, must still resolve. `read_unit` -- a shard-only read -- cannot
    see this unit at all, which is exactly why `resolve_any_unit` is specified to go through
    `read()` (the index+shard union) instead: a caller (slice B) that resolves a unit and then
    reopens it via a naive `read_unit`-shaped write would silently lose every field `index.json`
    alone was carrying.

    Constructed directly: `write_index` writes only the chart sheet, and `units/` is never touched,
    so no shard file for this unit exists on disk at all."""
    r = _mod()
    sdlc = tmp_path / ".sdlc"
    features_dir = r.registry_dir(sdlc)
    r.write_index(features_dir, {"sheet-only": _entry(open=False, title="Sheet-only unit")})

    # Sanity: confirm the shard-only read genuinely cannot see it -- otherwise this test would not
    # be exercising the distinction it claims to.
    assert r.read_unit(features_dir, "sheet-only") is None
    assert not (r.units_dir(features_dir) / "sheet-only.json").exists()

    assert r.resolve_any_unit(sdlc, "sheet-only") == "sheet-only"


# --------------------------------------------------------------------------- layout

def test_the_registry_sits_at_sdlc_features(tmp_path):
    """`.sdlc/features/` is committed -- it is not under any of setup.py's RUNTIME_IGNORES, and it
    must not be, because a gitignored backup is not a backup."""
    r = _mod()
    import importlib.util as u
    spec = u.spec_from_file_location("setup", ROOT / "skills" / "sigma-setup" / "scripts" / "setup.py")
    setup = u.module_from_spec(spec)
    spec.loader.exec_module(setup)
    assert r.registry_dir(tmp_path / ".sdlc") == tmp_path / ".sdlc" / "features"
    assert not [ig for ig in setup.RUNTIME_IGNORES if ig.rstrip("/").endswith("features")]


def test_the_unit_files_live_beside_the_chart_sheet_not_on_top_of_it(tmp_path):
    """`index` is itself a legal unit name, so per-unit files cannot share the sheet's directory
    without one day colliding with it."""
    r = _mod()
    assert r.is_unit_name("index")
    assert r.unit_path(tmp_path, "index") != r.index_path(tmp_path)
    assert r.unit_path(tmp_path, "index").parent != r.index_path(tmp_path).parent


def test_write_index_writes_the_whole_registry_as_one_document(tmp_path):
    r = _mod()
    r.write_index(tmp_path, {"alpha": _entry(), "beta": _entry()})
    doc = json.loads(r.index_path(tmp_path).read_text(encoding="utf-8"))
    assert doc["schema"] == r.SCHEMA and sorted(doc["features"]) == ["alpha", "beta"]


def test_the_chart_sheet_never_emits_a_key_that_is_not_a_unit_name(tmp_path):
    """The name rule has to hold on the WAY OUT as well as the way in. `index.json` is propagated
    verbatim into sibling repos (§7.1), so a sheet this code emits is a sheet other repos trust --
    and dropping a bad key only on read leaves every one of them to catch it independently."""
    r = _mod()
    r.write_index(tmp_path, {"ok": _entry(), "../../etc/passwd": _entry(), "a/b": _entry(),
                             "voice.lock": _entry()})
    doc = json.loads(r.index_path(tmp_path).read_text(encoding="utf-8"))
    assert list(doc["features"]) == ["ok"]


def test_a_unit_file_wins_by_replacement_not_by_insertion_order(tmp_path):
    """Pins the mechanism, not only the outcome. `read` drops the sheet's entry for a unit BEFORE
    inserting the file's, so the rule holds however the mapping is later written to; an
    implementation that merely inserted last would be at the mercy of its caller's dict."""
    r = _mod()
    r.write_index(tmp_path, {"alpha": _entry(title="stale")})
    r.write_unit(tmp_path, "alpha", _entry(title="fresh"))
    got = r.read(tmp_path)
    assert len(got) == 1 and got["alpha"]["title"] == "fresh"


# ------------------------------------- writing cannot fail on data reading accepted (§4a, B1)

#: Strings a propagated, hand-edited or tool-mangled `index.json` can genuinely carry, every one of
#: which `read` accepts. The lone surrogates are the ones that mattered: `"\ud800"` is ordinary JSON
#: text, `json.loads` hands it back as a real `str`, and encoding it as UTF-8 raises.
_HOSTILE_TEXT = ("a\ud800b", "a\udfffb", "↔ dash", "café", "emoji \U0001f600",
                 "nul\x00inside", "﻿bom", "line\nbreak", "\\backslash\\", '"quoted"')


@pytest.mark.parametrize("text", _HOSTILE_TEXT)
def test_writing_never_fails_on_a_value_reading_accepted(tmp_path, text):
    """THE PROMISE, AS A PROPERTY. The read side is total, so it hands back whatever an untrusted
    `index.json` carried; the write side then has to be able to write that back, because
    `write_index(dir, read(dir))` is literally the fold #1473 performs. It could not: a lone
    surrogate reached the serialiser and raised `UnicodeEncodeError`."""
    r = _mod()
    _write_index(tmp_path, {"schema": r.SCHEMA, "features": {"voice": {"title": text}}})
    registry = r.read(tmp_path)
    assert registry["voice"]["title"] == text
    r.write_index(tmp_path, registry)                    # the fold -- must not raise
    r.write_unit(tmp_path, "voice", registry["voice"])   # nor the pick-path write
    assert r.read(tmp_path)["voice"]["title"] == text    # and it must come back unchanged


@pytest.mark.parametrize("text", _HOSTILE_TEXT)
def test_what_is_written_is_ascii_so_the_encode_step_has_nothing_left_to_fail_on(tmp_path, text):
    """The mechanism behind the property above, pinned separately: `ensure_ascii=True` leaves the
    serialised document ASCII-only, and an ASCII string cannot fail to encode as UTF-8. Testing the
    property alone would let someone "fix" it with `errors="surrogatepass"`, which writes bytes no
    other JSON reader on the planet can decode."""
    r = _mod()
    r.write_index(tmp_path, {"voice": {"title": text}})
    r.write_unit(tmp_path, "voice", {"title": text})
    for path in (r.index_path(tmp_path), r.unit_path(tmp_path, "voice")):
        assert path.read_bytes().isascii(), path.name


def test_the_naming_contract_has_a_type_of_its_own(tmp_path):
    """`UnicodeEncodeError` IS a `ValueError`, so a contract spelled `ValueError` said "bad unit
    name" about an encoding failure. A named subclass lets a caller catch exactly what it was
    promised, and still subclasses `ValueError` so nothing catching the broad form changes."""
    r = _mod()
    assert issubclass(r.InvalidUnitName, ValueError)
    with pytest.raises(r.InvalidUnitName):
        r.write_unit(tmp_path, "../escape", _entry())
    with pytest.raises(r.InvalidUnitName):
        r.unit_path(tmp_path, "voice.lock")
    # and the failure the old contract could be confused with no longer happens at all
    r.write_unit(tmp_path, "voice", _entry(title="a\ud800b"))


def test_a_registry_that_is_not_a_mapping_writes_an_empty_document(tmp_path):
    """Found by hunting the class rather than fixing the one instance reported: `document` did
    `(registry or {}).items()`, so a truthy non-mapping -- a list of entries, say -- raised
    `AttributeError` out of `write_index`. Same shape as the other two: content is untrusted."""
    r = _mod()
    for registry in ([], [1], "voice", 7, None, (), 0.5):
        assert r.document(registry)["features"] == {}, registry
    r.write_index(tmp_path, ["not", "a", "mapping"])
    assert r.read(tmp_path) == {}


def test_the_access_check_never_raises_on_the_repo_it_is_asked_about(tmp_path):
    """`repos.get(repo)` raises `TypeError` on an unhashable repo. The helper tolerated every
    malformed ENTRY and no malformed REPO -- but the repo key is registry content too, and this
    module trusts none of that."""
    r = _mod()
    entry = _entry()
    for repo in (["org/r"], {"a": 1}, {1, 2}, None, 7, 0.5, ("org", "r")):
        assert r.is_authorized(entry, repo) is False, repo


# --------------------------------------------------- an unusable shard costs its unit outright

def test_a_corrupt_shard_costs_its_unit_rather_than_serving_the_stale_sheet(tmp_path, capsys):
    """THE RULING. `read`'s own docstring promises one unreadable unit file costs exactly that one
    unit; measured, a stale chart-sheet entry silently stood in for it instead. The shard is the
    write surface and is authoritative for its unit, so the sheet's entry is by construction the
    state BEFORE the write that produced the shard -- answering with it is the failure this
    registry exists to prevent, dressed as success. An absent unit makes the caller look."""
    r = _mod()
    r.write_index(tmp_path, {"alpha": _entry(title="STALE-SHEET"), "beta": _entry(title="Beta")})
    r.write_unit(tmp_path, "alpha", _entry(title="fresh"))
    r.unit_path(tmp_path, "alpha").write_text("{ truncated", encoding="utf-8")

    got = r.read(tmp_path)
    assert "alpha" not in got, got.get("alpha", {}).get("title")
    assert got["beta"]["title"] == "Beta"                 # every other unit is untouched
    assert "alpha.json" in capsys.readouterr().err        # and the cause is findable


def test_a_shard_in_a_schema_we_cannot_read_costs_its_unit_too(tmp_path):
    """The sharper half: a `@2` shard is not damaged, it is NEWER. Reading the `@1` sheet's entry
    for that unit would be answering a question about it with state a later version has already
    moved past."""
    r = _mod()
    r.write_index(tmp_path, {"alpha": _entry(title="STALE-SHEET")})
    r.units_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    r.unit_path(tmp_path, "alpha").write_text(json.dumps(
        {"schema": "sigma/features@2", "features": {"alpha": {"title": "v2"}}}),
        encoding="utf-8")
    assert r.read(tmp_path) == {}


def test_a_shard_naming_the_wrong_unit_costs_the_unit_its_filename_claims(tmp_path):
    """A file that claims to be alpha's record and is not one is not a usable record of alpha,
    whatever else it contains -- so alpha is unknown, and the smuggled unit is still not created."""
    r = _mod()
    r.write_index(tmp_path, {"alpha": _entry(title="STALE-SHEET")})
    r.units_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    r.unit_path(tmp_path, "alpha").write_text(json.dumps(
        {"schema": r.SCHEMA, "features": {"beta": _entry(title="Smuggled")}}), encoding="utf-8")
    assert r.read(tmp_path) == {}


def test_an_unusable_shard_for_a_unit_the_sheet_never_mentioned_changes_nothing(tmp_path):
    """The drop must not reach past its own unit -- it removes alpha, never the whole registry."""
    r = _mod()
    r.write_index(tmp_path, {"beta": _entry(title="Beta")})
    r.units_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    r.unit_path(tmp_path, "alpha").write_text("{ truncated", encoding="utf-8")
    assert list(r.read(tmp_path)) == ["beta"]


# ------------------------------------------------------- the whole-entry obligation, and its cost

def test_a_shard_written_as_a_delta_erases_the_rest_of_the_entry(tmp_path):
    """The measured consequence of "replaces rather than merges", pinned so the obligation
    `write_unit`'s docstring now states has a test behind it: a pick that knows only its own repo
    and writes only that repo destroys the unit's other repos, their goals, and its tracking
    issue. The fix is a contract on the caller -- pass the whole entry -- not a merge here, because
    a merge could never tell a deliberate removal from an omission."""
    r = _mod()
    r.write_index(tmp_path, {"alpha": _entry(
        tracking_issue="org/a#1",
        repos={"org/a": {"branch": "feature/alpha", "goals": [1]},
               "org/b": {"branch": "feature/alpha", "goals": [3]}})})
    r.write_unit(tmp_path, "alpha", {"title": "Alpha",
                                     "repos": {"org/a": {"branch": "feature/alpha",
                                                         "goals": [1, 2]}}})
    got = r.read(tmp_path)["alpha"]
    assert list(got["repos"]) == ["org/a"] and got["tracking_issue"] is None


def test_two_picks_on_the_SAME_unit_lose_a_write_and_this_is_a_known_limitation(tmp_path):
    """The cost the module docstring now states, made deterministic. Measured independently at 10
    OS processes: 2-4 of 10 goal numbers recorded, the rest silently lost, the file never corrupt.
    That is read-modify-write with no lock, and #1469 scopes it out ("different units"); #1473 owns
    the pick-path write and is the layer that can serialise it. Pinned here so the limitation is
    discovered by reading this file, not by losing a goal."""
    r = _mod()
    r.write_unit(tmp_path, "alpha", _entry(repos={"org/a": {"goals": [1]}}))

    first = r.read(tmp_path)["alpha"]                    # both picks read the same state
    second = r.read(tmp_path)["alpha"]
    first["repos"]["org/a"]["goals"] = [1, 2]
    second["repos"]["org/a"]["goals"] = [1, 3]
    r.write_unit(tmp_path, "alpha", first)               # then both write it back
    r.write_unit(tmp_path, "alpha", second)

    assert r.read(tmp_path)["alpha"]["repos"]["org/a"]["goals"] == [1, 3]   # goal 2 is gone


# --------------------------------------------------------------- #1566: one unit, one shard


def test_two_casings_of_one_unit_resolve_to_one_shard_path(tmp_path):
    """#1566. `_read_unit_file` already treats two casings as one unit -- its docstring says so
    outright -- but `unit_path` concatenated the RAW name, so the write side disagreed with the read
    side. On a case-sensitive filesystem that is two files for one unit.

    ASSERTED ON THE DERIVED PATH, not on filesystem behaviour, deliberately: this host's filesystem
    is case-INSENSITIVE, so `Voice.json` and `voice.json` resolve to one file here and the bug is
    invisible to any test that writes and reads back. Comparing the two paths as strings fails on
    every platform when the fold is missing, and that is the only formulation that catches a
    regression on the Linux hosts where it actually bites."""
    m = _mod()
    d = tmp_path / "features"
    assert m.unit_path(d, "Voice") == m.unit_path(d, "voice")
    assert m.unit_path(d, "VOICE") == m.unit_path(d, "voice")


def test_the_shard_filename_is_folded_but_the_declared_casing_is_what_is_returned(tmp_path):
    """The fold is a property of the KEY, never of the record. A unit called `Voice` is still called
    `Voice` everywhere a person reads it; the derived address stops caring about the difference.

    THE COUNT USED TO BE IN THIS DOCSTRING AND IS NOT ANY MORE (#1638). It said "the two derived
    addresses -- its file and its lock", which was wrong in both directions, and every later attempt
    to restate the number went stale again within two goals. A prose count of a set nothing measures
    is how #1566 came to be believed complete, so there is no number here now:
    `test_every_address_builder_is_classified` below holds the whole inventory -- discovered, not
    written down -- and this test holds only its own key."""
    m = _mod()
    d = tmp_path / "features"
    assert m.unit_path(d, "Voice").name == "voice" + m.UNIT_SUFFIX
    m.write_unit(d, "Voice", m.normalise_entry({"title": "the voice unit"}))
    assert m.read_unit(d, "voice") is not None, "the other casing must find the same shard"
    assert "Voice" in m.read(d), "the entry keeps the casing its author wrote"


def test_a_unit_name_ending_in_uppercase_LOCK_still_addresses_a_file(tmp_path):
    """The `.lock` clause is why the fold needs a test of its own. `is_unit_name` rejects
    `voice.lock` because git rejects that ref, but ACCEPTS `voice.LOCK` because git accepts it --
    so folding the name turns an accepted name into the spelling of a rejected one. That is safe
    only because the fold happens AFTER the guard and the suffix is appended after the fold; this
    test is what keeps that ordering from being rearranged."""
    m = _mod()
    d = tmp_path / "features"
    p = m.unit_path(d, "voice.LOCK")
    assert p.name == "voice.lock" + m.UNIT_SUFFIX
    m.write_unit(d, "voice.LOCK", m.normalise_entry({}))
    assert m.read_unit(d, "voice.LOCK") is not None


# ------------------- #1638/#1674: the inventory of derived unit keys, and which of them fold
#
# #1566 folded a shard path and a lock key and was believed to have folded "the derived keys".
# #1577 then found `feature_rebase.lock_path` and `filed_path` unfolded; #1638 found
# `feature_propagate.sibling_path` unfolded; and measuring the whole family for #1638's doc
# reconciliation found two more that no issue had named. Three separate rounds of "and one more",
# every one of them found by a human reading, because the per-site tests each asserted about their
# own site and nothing asserted about the SET.
#
# So this asserts about the set. #1638 built the sets; #1674 replaced what FINDS them, because the
# finding was the half that still needed a human to remember. Its discovery matched a function whose
# NAME ended `_path`, whose LAST PARAMETER was spelled `name` or `unit`, and which was public --
# three closed vocabularies, each a list of what somebody thought of, which is the failure this test
# exists to stop wearing a different hat. Measured against five deliberately-unfolded keys injected
# into `feature_registry.py`, it caught one and let four through:
#
#     def widget_path(features_dir, name)       -> RED, caught
#     def widget_path(features_dir, unit_name)  -> GREEN, missed
#     def widget_file(features_dir, name)       -> GREEN, missed
#     def _widget_path(features_dir, name)      -> GREEN, missed
#     def widget_dir(features_dir, unit)        -> GREEN, missed
#
# `_address_builders` below finds all five, because it asks what the function DOES -- it joins a
# path -- and a name, a visibility and a parameter spelling are all things it no longer reads.

#: Derived keys that fold two casings of one unit onto one address. Each is a filesystem key: two
#: spellings would be two files, two locks, or two worktrees for one unit on a case-sensitive host.
_FOLDS = {
    ("feature_registry", "unit_path"),        # #1566 — the shard
    ("feature_sync", "lock_path"),            # #1566 — the per-unit write lock
    ("feature_rebase", "lock_path"),          # #1577 — the upkeep lock
    ("feature_rebase", "filed_path"),         # #1577 — the "already filed this conflict" marker
    ("feature_rebase", "blocked_path"),       # #144 — the "upkeep is refusing" marker the doctor reads
    ("feature_rebase", "ack_path"),           # #2756 — the sanctioned-exit ack file
    ("feature_propagate", "sibling_path"),    # #1672 — a shard path in ANOTHER repo
    ("feature_rebase", "worktree_path"),      # #1673 — the throwaway upkeep checkout
    ("feature_doc", "doc_path"),              # #1673 — the `<name>.md` page a person opens
    ("feature_upkeep_state", "unit_state_path"),    # #919 — the per-unit upkeep state file
}

#: Derived keys that do NOT fold — A RECORD OF OPEN DEFECTS, never a design decision. When one is
#: fixed this test fails, which is deliberate: the fix has to move the entry up and correct the
#: prose that counts them in the same change, rather than leaving one more stale count behind.
#:
#: IT IS CURRENTLY EMPTY, AND THAT IS A MEASUREMENT RATHER THAN A CLAIM. `sibling_path` was #1638's own and
#: the worst of them — it named a file in a repository we do not own — and it was fixed by #1672.
#: `worktree_path` and `doc_path` sat here as measured-but-unreported defects, were filed as #1673
#: off this very set, and were fixed by it. Every derived key the discovery below finds now folds.
#:
#: THE MECHANISM WORKED, AND THAT IS WHY THE SET STAYS. Both fixes were forced through this pair of
#: sets: moving an entry up FAILS this test until the prose counting it is corrected in the same
#: change, which is exactly what caught the three stale counts #1673 shipped with. An empty set is
#: not a set to delete — it is where the next unfolded key gets recorded, and the discovery below
#: fails on one that is in neither set, so a new address builder cannot quietly skip the decision.
_DOES_NOT_FOLD = set()

#: Address builders that take NO unit name, so there is nothing for them to fold: each module's
#: `_load` sibling-importer (its `name` is a MODULE name, and folding it would break the import on a
#: case-sensitive host), two that take a GOAL or a path, and three that take only a directory.
#:
#: THIS SET IS THE ONE A LAZY ANSWER LANDS IN, and the honest limit of the whole guard.
#: `_load("feature_doc")` and an unfolded `widget_path(dir, name)` are behaviourally IDENTICAL --
#: both split two casings, one of them correctly -- so nothing can measure which of the two a new
#: builder is. The guard forces the DECISION and cannot make it. What it must therefore not do is
#: make the decision cheap to dodge, which is what
#: `test_nothing_declared_free_of_unit_names_touches_the_unit_name_vocabulary` is for: it is a
#: one-way cross-check over THIS set, and it fires when a member's code names the unit-name
#: vocabulary. It is deliberately NOT an auto-classifier -- run the other way it would have declared
#: all four mutants above benign, since none of them names any of those either.
_NO_UNIT_NAME = {
    ("feature_backup", "_load"),              # the sibling importer, once per module
    ("feature_propagate", "_symlink_guard"),  # #708: loads state.py by path; no unit name involved
    ("feature_sync", "_symlink_guard"),
    ("feature_doc", "_load"),                 # the sibling importer, once per module
    ("feature_frontier", "_load"),
    ("feature_labels", "_load"),
    ("feature_owner", "_load"),
    ("feature_propagate", "_load"),
    ("feature_rebase", "_load"),
    ("feature_registry", "_load"),
    ("feature_stamp", "_load"),
    ("feature_sync", "_load"),
    ("feature_upkeep_drift", "_load"),        # #919: the sibling importer, once per module
    ("feature_upkeep_state", "_load"),        # #919: the sibling importer, once per module
    ("feature_propagate", "record_path"),     # keyed by GOAL, not by unit
    ("feature_rebase", "rebase_stopped"),     # takes a path that is already built
    # #278: keyed by a git BRANCH name (which may be a goal's `sdlc/<n>`), never a unit name; its
    # stem is case-folded anyway and the exact branch is checked inside (see its docstring).
    ("feature_rebase", "_refused_marker"),
    ("feature_registry", "registry_dir"),     # directories: no name reaches them at all
    ("feature_registry", "index_path"),
    ("feature_registry", "units_dir"),
    # #514: the Sigma-only copy of the SHEET -- `<sdlc>/state/backup/index-sigma.json`, a fixed
    # filename under the project's state directory; no unit name reaches it.
    ("feature_registry", "mirror_path"),
    # #314: the recovery TEXT for a legacy-id delta record -- joins this module's own directory
    # with `feature_sync.py` and prints the features dir it was handed; no unit name reaches a path.
    ("feature_registry", "delta_recovery"),
    # #327: the doctor row's TEXT names the `units/` directory it counted; no unit name reaches it.
    ("feature_sync", "legacy_delta_row"),
    # #2265: `feature_frontier._config(sdlc_dir)` joins `sdlc_dir / "config.json"` -- the same
    # "directories: no name reaches them at all" shape as the three `feature_registry` entries
    # directly above. It takes only an `.sdlc` root, never a unit name, and is not a unit-scoped
    # address at all (it reads the project's whole `config.json`, unrelated to any one unit).
    ("feature_frontier", "_config"),
    ("feature_classify", "_load"),            # the sibling importer, once per module
    # #2363: `_evidence_exists(sdlc_dir, evidence_path)` joins the PROJECT ROOT with an
    # unregistered, judge-supplied EVIDENCE path (tier 3's "concrete evidence" check) -- never a
    # unit name. The whole point of tier 3 is that no unit is registered for this address yet, so
    # there is nothing here for a unit name to fold.
    ("feature_classify", "_evidence_exists"),
    ("feature_judge", "_load"),               # the sibling importer, once per module
    # #2380: `_spend_path(sdlc_dir)` joins the project's `.sdlc/state/` directory with the fixed
    # filename `feature-judge-spend.json` -- the SAME "directories: no name reaches them at all"
    # shape as `feature_registry.registry_dir`/`index_path`/`units_dir` and
    # `feature_frontier._config` immediately above. It is ONE shared ledger for the whole repo, not
    # a per-unit address, so there is no unit name here for a casing to fold.
    ("feature_judge", "_spend_path"),
    # #2388: `_spend_lock_path(sdlc_dir)` joins the same `.sdlc/state/` directory with the fixed
    # filename `feature-judge-spend.lock` -- the dedicated lock file guarding the spend ledger's
    # check-through-record window. Identical shape to `_spend_path` immediately above: ONE shared
    # lock for the whole repo, not a per-unit address, so there is no unit name here for a casing
    # to fold either.
    ("feature_judge", "_spend_lock_path"),
}

#: EVERY SET ABOVE IS SPELLED `set()` WHEN EMPTY, NEVER `{}`: an empty brace literal is a DICT, and
#: the union in `_classified()` then raises `TypeError` instead of measuring anything. That is not
#: hypothetical — it is what happened the first time `_DOES_NOT_FOLD` emptied, and the union caught
#: it. The note is on all three because any of them can empty, not only the one that has.
def _classified():
    return _FOLDS | _DOES_NOT_FOLD | _NO_UNIT_NAME


#: The names a function cannot handle a unit name without touching. Used ONE WAY ONLY — see
#: `_NO_UNIT_NAME`.
_UNIT_NAME_VOCABULARY = ("is_unit_name", "InvalidUnitName", "unit_key", "unit_path", "UNIT_SUFFIX")

_SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _feature_sources():
    return sorted(_SCRIPTS.glob("feature*.py"))


def _joins_a_path(node):
    """Does this function's body build a filesystem address?

    `a / b` AND `a /= b`. A reader calls both a pathlib join and only the AST tells them apart --
    `/=` is an `ast.AugAssign`, never an `ast.BinOp` -- so a predicate that read `BinOp` alone was
    blind to a literal `/` while its own docstring said "THE PREDICATE IS `/`". Found in review."""
    import ast
    for statement in node.body:
        for child in ast.walk(statement):
            if isinstance(child, (ast.BinOp, ast.AugAssign)) and isinstance(child.op, ast.Div):
                return True
    return False


def _builders_in(source, stem):
    """The address builders in ONE module's source.

    Split out from `_address_builders` so the discovery can be driven on SYNTHETIC source, which is
    the only way to exercise a branch the real tree does not currently contain. Not a refactor for
    its own sake: a mutation run found the nested-function branch surviving -- 20 builders with the
    walk and 20 without -- because no module here has a nested builder today, which is the same
    unexercised-branch defect `_code_of` was given a subject for."""
    import ast
    found = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _joins_a_path(node):
            found[(stem, node.name)] = node
    return found


def _address_builders():
    """Every function in a `feature*` module that JOINS A PATH, found from the source.

    THE PREDICATE IS STRUCTURAL RATHER THAN LEXICAL. A `pathlib` division is what building a
    filesystem address looks like in these modules, and unlike a naming convention it is not
    something a new function can accidentally opt out of by being called something else. Name,
    visibility and parameter spellings are all deliberately unread: those were #1638's discovery and
    they leaked four of five injected keys.

    Nested functions are walked, so a builder returned by a factory is discovered under its own name
    -- AND SO IS ITS PARENT, whose body contains the join. Both are reported and both must be
    classified; the parent is not a false positive to suppress, because a factory that assembles an
    address is exactly as able to leave it unfolded as the function it returns.

    WHAT IT CANNOT SEE, MEASURED RATHER THAN ASSUMED. It reads a `/` operator, in one file-naming
    convention, so all of these are invisible to it:

      * an address built as `"/".join(...)`, `os.path.join(...)`, an f-string, `.joinpath()`, the
        multi-argument `pathlib.Path(a, b)` constructor, or `.with_name()` / `.with_suffix()` on a
        path that is already correct;
      * a builder bound as a module-level `lambda` rather than written as a `def` -- there is no
        `FunctionDef` to find;
      * a derived key that is not a filesystem path at all;
      * ANY MODULE NOT MATCHING `feature*.py`. That glob is itself a naming convention, and
        therefore the same closed vocabulary this discovery replaced for function names, one level
        up: an unfolded unit address in `unit_completion.py`, in `work.py`, or in a new module named
        anything else is not looked at. Measured 2026-08-25 -- every `/` join in the other 37
        scripts is either a `_load` module-importer or keyed by a goal, so the glob is complete
        today. That is a measurement of today, not a property of tomorrow.

    Two of those gaps are live rather than hypothetical:

      * `feature_propagate.sibling_path` RETURNS `"/".join(...)` on purpose -- its docstring gives
        the reason, that the GitHub Contents API addresses POSIX paths and `pathlib` on Windows
        would hand it backslashes -- and is discovered here only because a `/` join appears in one
        of that call's arguments. A second sibling-repo address written the same way would not be.
      * `feature_labels.label_for(unit)` returns `feature:<unit>` UNFOLDED today, and the six inline
        `features.BRANCH_PREFIX + unit` sites derive a git branch name. Neither is a filesystem
        address; whether either should fold is a live question and a separate one -- a git ref IS
        case-sensitive, so folding a branch name is not obviously right the way folding a filename
        is.

    So the family this guard covers is FILESYSTEM ADDRESSES -- #1674's own words, "path, lock,
    filename" -- and the label and branch families are stated here as out of scope rather than left
    to look covered."""
    found = {}
    for path in _feature_sources():
        found.update(_builders_in(path.read_text(encoding="utf-8"), path.stem))
    return found


def _code_of(node):
    """A function's CODE, with its docstring gone.

    Comments are absent from the AST already, and the docstring is dropped here on purpose: every
    one of these modules documents the unit-name rules by QUOTING them, so a prose-inclusive read
    would report `registry_dir` as handling unit names because the paragraph above it mentions
    `unit_path`. That is the same first-occurrence trap `_mod_with` records at the top of this
    file, one layer up."""
    import ast
    import copy
    stripped = copy.deepcopy(node)
    if (stripped.body and isinstance(stripped.body[0], ast.Expr)
            and isinstance(stripped.body[0].value, ast.Constant)
            and isinstance(stripped.body[0].value.value, str)):
        stripped.body = stripped.body[1:] or [ast.Pass()]
    return ast.unparse(stripped)


def _declared_free_but_naming(builders, declared):
    """Members of `declared` whose CODE names the unit-name vocabulary — the check's answer, split
    from the test so its FIRING path can be driven on synthetic source.

    Not a refactor for its own sake either: a mutation run emptied `_UNIT_NAME_VOCABULARY` outright
    and the suite stayed green, because on today's tree the answer is the empty list either way. A
    check whose positive path the suite never runs is decoration, whatever it would do in principle."""
    offenders = []
    for key in sorted(declared):
        node = builders.get(key)
        if node is None:
            continue
        named = [word for word in _UNIT_NAME_VOCABULARY if word in _code_of(node)]
        if named:
            offenders.append((key[0], key[1], named))
    return offenders


def _loaded(stem):
    spec = importlib.util.spec_from_file_location(stem, _SCRIPTS / (stem + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_address_builder_is_classified(tmp_path):
    """Discovered, not listed. The whole point is to notice a key nobody thought to mention — a
    listed set is the same prose count this test exists to replace, only written in Python."""
    keys = set(_address_builders())
    assert keys == _classified(), (
        "a function in a `feature*` module builds a filesystem address and nothing says whether a "
        "unit name reaches it — add it to _FOLDS, _DOES_NOT_FOLD or _NO_UNIT_NAME in this file: %s"
        % sorted(keys ^ _classified()))


def test_every_classified_unit_address_still_folds_the_way_it_is_recorded(tmp_path):
    """Each member's behaviour, driven rather than read.

    Each key is CALLED with two casings and the results compared, which is the only formulation that
    works on this repo's case-insensitive development host — where the filesystem hides the whole
    defect — and on the Linux hosts where it bites. Only the two sets that take a unit name are
    called; `_NO_UNIT_NAME` is declared and never invoked, which is what keeps this from importing a
    module or writing a file merely to discover something."""
    builders = _address_builders()
    root = str(tmp_path / ".sdlc")
    (tmp_path / ".sdlc" / "features").mkdir(parents=True)
    first = {"features_dir": pathlib.Path(root) / "features", "sdlc_dir": root}

    def address(fn, unit):
        params = list(inspect.signature(fn).parameters)
        for param in params[:-1]:
            assert param in first, (
                "a recorded builder's parameter %r has no fixture here, so its fold cannot be "
                "checked — add it to `first` above (#1674)" % param)
        return str(fn(*[first[p] for p in params[:-1]], unit))

    for key in sorted(_FOLDS | _DOES_NOT_FOLD):
        assert key in builders, (
            "%s.%s is recorded here but no longer builds an address — if it was deleted or renamed, "
            "remove it from its set (#1674)" % key)
        fn = getattr(_loaded(key[0]), key[1], None)
        assert fn is not None, (
            "%s.%s builds an address but is not reachable as a module attribute — discovery walks "
            "nested functions and class bodies, and neither can be CALLED through the module, so a "
            "fold recorded for one cannot be checked. Lift it to module level (#1674)" % key)
        folded = address(fn, "Voice") == address(fn, "voice")
        assert folded == (key in _FOLDS), (
            "%s.%s now %s two casings of one unit — move it between _FOLDS and _DOES_NOT_FOLD, and "
            "correct every prose count of the folded set along with it (#1638)"
            % (key[0], key[1], "folds" if folded else "splits"))


def test_nothing_declared_free_of_unit_names_touches_the_unit_name_vocabulary(tmp_path):
    """The one-way cross-check on `_NO_UNIT_NAME` — the set a lazy answer lands in.

    Membership of that set cannot be measured (see its own comment), so what is measured instead is
    the thing a wrong answer leaves behind: a function that really handles a unit name has to guard
    it, fold it, or build its address, and all four of those are spelled with the names below.
    Measured over the tree as it stands, this separates the two groups exactly — every folded
    builder names at least one, and every one declared unit-name-free names none. (The sets above
    are the count: prose counts of them went stale as members were added, so none is repeated here.)

    ONE WAY ONLY, AND THAT IS THE WHOLE DESIGN. Run the other way, as an auto-classifier, it would
    have waved through every one of the four keys #1674's discovery was written to catch: none of
    `widget_path`, `widget_file`, `_widget_path` or `widget_dir` names any of these either, because
    not guarding is precisely what is wrong with them."""
    builders = _address_builders()
    for key in sorted(_NO_UNIT_NAME):
        assert key in builders, (
            "%s.%s is recorded as taking no unit name but no longer builds an address — if it was "
            "deleted or renamed, remove it from _NO_UNIT_NAME (#1674)" % key)
    offenders = _declared_free_but_naming(builders, _NO_UNIT_NAME)
    assert not offenders, (
        "declared in _NO_UNIT_NAME as taking no unit name, but its code names the unit-name "
        "vocabulary — decide whether a unit name now reaches this address, and move it to _FOLDS or "
        "_DOES_NOT_FOLD if it does (#1674): %s" % offenders)


def test_code_of_drops_the_docstring_so_a_quoted_rule_is_not_read_as_code(tmp_path):
    """`_code_of`'s docstring-stripping, exercised rather than asserted.

    A mutation run over the guard found this branch SURVIVING: removing the strip changed nothing,
    because no `_NO_UNIT_NAME` member happens to quote the vocabulary in its prose TODAY. An
    unexercised branch justified only by a comment is the decoration this repo's rules forbid, and
    the risk it guards is real and specific — every module here documents the unit-name contract by
    QUOTING it, which is the same first-occurrence trap `_mod_with` records at the top of this file.
    So the branch gets a subject of its own: prose that names the vocabulary and code that does not
    must read clean, and code that names it must not."""
    import ast
    quoted = ast.parse(
        'def f(sdlc_dir):\n'
        '    """Refuses what `is_unit_name` refuses, the way `unit_path` does."""\n'
        '    return pathlib.Path(sdlc_dir) / "x"\n').body[0]
    assert not [w for w in _UNIT_NAME_VOCABULARY if w in _code_of(quoted)], (
        "a rule QUOTED in prose is being read as code — the cross-check would report every module "
        "that documents itself (#1674)")

    used = ast.parse(
        'def f(sdlc_dir, name):\n'
        '    """Nothing quoted here."""\n'
        '    return pathlib.Path(sdlc_dir) / unit_key(name)\n').body[0]
    assert [w for w in _UNIT_NAME_VOCABULARY if w in _code_of(used)] == ["unit_key"], (
        "stripping the docstring dropped the CODE too — the cross-check would then never fire on "
        "anything (#1674)")


def test_discovery_sees_a_nested_builder_its_parent_and_an_augmented_join(tmp_path):
    """The two discovery branches the real tree does not currently exercise.

    Both were found SURVIVING a mutation run, and by inspection alone both looked fine: restricting
    the walk to top-level functions changed the count not at all (20 either way), because no module
    here nests a builder — and the `/=` arm had no subject at all while the docstring above claimed
    the predicate was `/`. An unexercised branch justified only by a comment is what
    `test_code_of_drops_the_docstring_so_a_quoted_rule_is_not_read_as_code` exists to stop, and this
    is the same defect two functions over. Driven on synthetic source, because a subject that does
    not exist in the tree is the whole reason the branches were unexercised."""
    source = ('def factory(features_dir):\n'
              '    def widget_path(name):\n'
              '        return pathlib.Path(features_dir) / (name + ".w")\n'
              '    return widget_path\n'
              '\n'
              'def augmented(features_dir, name):\n'
              '    p = pathlib.Path(features_dir)\n'
              '    p /= name\n'
              '    return p\n'
              '\n'
              'def joins_nothing(features_dir):\n'
              '    return str(features_dir) + "/x"\n')
    found = set(_builders_in(source, "synthetic"))
    assert ("synthetic", "widget_path") in found, (
        "a builder NESTED in a factory is not discovered — the walk no longer descends (#1674)")
    assert ("synthetic", "factory") in found, (
        "the parent must be discovered too: its own body contains the join, and a factory that "
        "assembles an address can leave it unfolded exactly as the function it returns can (#1674)")
    assert ("synthetic", "augmented") in found, (
        "`p /= name` is an ast.AugAssign, not an ast.BinOp — a predicate reading only BinOp is "
        "blind to a literal `/` while claiming the predicate IS `/` (#1674)")
    assert ("synthetic", "joins_nothing") not in found, (
        "string concatenation is the STATED limit of this predicate — if it starts matching, the "
        "docstring's inventory of what it cannot see is wrong (#1674)")


def test_the_unit_name_cross_check_fires_when_a_declared_member_names_the_vocabulary(tmp_path):
    """The FIRING path of `_declared_free_but_naming`, which the tree cannot exercise.

    Every `_NO_UNIT_NAME` member is clean today, so the check's answer is the empty list whether or
    not it works: a mutation run emptied `_UNIT_NAME_VOCABULARY` entirely and the suite stayed
    green. That is the identical failure the docstring-strip branch had, on the sibling half of the
    same check, so it gets the identical treatment — a synthetic subject that must fire.

    The `clean` case is not filler. It is the reason the docstring is stripped at all: these modules
    document the unit-name contract by QUOTING it, so a prose-inclusive read would report a
    directory helper as handling unit names on the strength of the paragraph above it."""
    source = ('def clean(sdlc_dir):\n'
              '    """Built beside what `unit_path` refuses, said in prose and nowhere else."""\n'
              '    return pathlib.Path(sdlc_dir) / "x"\n'
              '\n'
              'def dirty(sdlc_dir, name):\n'
              '    return pathlib.Path(sdlc_dir) / unit_key(name)\n')
    builders = _builders_in(source, "synthetic")
    assert set(builders) == {("synthetic", "clean"), ("synthetic", "dirty")}

    assert _declared_free_but_naming(builders, {("synthetic", "clean")}) == [], (
        "a rule QUOTED in prose fired the check — every module that documents itself would be "
        "reported (#1674)")
    assert (_declared_free_but_naming(builders, set(builders))
            == [("synthetic", "dirty", ["unit_key"])]), (
        "a declared-clean member whose CODE folds a unit name did NOT fire the check — the "
        "vocabulary is the only thing standing between the third set and a free pass (#1674)")


# ------------------- #327 review block #2: every writer of a unit record or the index, classified
#
# Tier-1 classify's reopen called `write_unit` DIRECTLY, so it skipped the one legacy-delta rule
# every other Sigma write went through, and a newer post-conversion record's P0 vanished with nothing
# said. `write_unit` now applies that rule itself (`LegacyDeltaConflict`), so no caller can bypass
# it -- and this inventory is DISCOVERED from the tree, not written down, so a new direct writer
# fails here until someone says, in this file, why it is safe.

#: (module, enclosing function, callee) -> why that call is safe. A key missing here fails the test.
_REGISTRY_WRITE_CALLERS = {
    # THE write surface: under the unit's lock, `delta_verdict` first (report, never raise), and
    # `write_unit`'s own refusal caught as the same report if the record changed in between.
    ("feature_sync", "amend", "write_unit"):
        "the one rule, reported: refused -> `refused`/`delta_conflict`; discard -> `discarded`",
    # The documented recovery: the delta merge, under the unit's lock, refused when newer.
    ("feature_sync", "repair", "write_unit"):
        "the one rule, reported: refused units are returned, never raised",
    # The chart sheet only; never a unit record. `monotonic_violations` refuses a shrinking index,
    # and `write_index` writes nothing when the bytes are unchanged (the delta's age survives).
    ("feature_sync", "fold", "write_index"):
        "index only, monotonic-checked; a changed index is the newer file (docs/upgrading.md)",
    # #514: the explicit lever after the old plugin's fold emptied the sheet. Index only; adds WHOLE
    # missing units from Sigma's own copy beneath what `read` serves (the sheet and shards win),
    # refuses when nothing is missing or the sheet moved, and is never run unprompted.
    ("feature_sync", "recover", "write_index"):
        "index only; whole missing units, explicit gesture, refused when the sheet changed",
}

#: Modules OUTSIDE `feature_registry` that both name the registry's files and contain a write
#: primitive -- i.e. could write `features/units/*.json` or `features/index.json` without the
#: registry. Each is said here to be one that does not, or does so under a rule of its own.
_REGISTRY_ADJACENT_WRITERS = {
    "migrate": "the converter: refuses a legacy delta record (names `repair`), converts the index "
               "only once no record is left in the previous schema, under the unit's lock",
    "coexist": "reads `features/` once to take the one-time backup; writes only under "
               "`state/backup/` (and its own owner/notice markers)",
    "feature_propagate": "writes a SIBLING repository's record through the host API, destination "
                         "wins every field, and refuses a sibling record in the previous schema",
    "work": "names `.sdlc/features/units/` only to allow-list it in the secret-file check",
    # #514: `recover --discard` renames Sigma's OWN recovery copy under `state/backup/`; every write
    # of the sheet itself goes through `write_index` (classified above).
    "feature_sync": "renames only the recovery copy under `state/backup/`; the sheet and shards "
                    "are written by `write_index` / `write_unit` through the callers above",
    # #514: names `features/index.json` only to stat it before calling `guard_sheet`, which
    # writes nothing but the recovery copy under `state/backup/`.
    "loop": "stats `features/index.json` before `feature_registry.guard_sheet`, which writes only "
            "the recovery copy under `state/backup/`",
    "slack_commands_listen": "writes only its own heartbeat/pid files; 'units' is prose",
}

_WRITE_PRIMITIVE = re.compile(
    r"os\.replace\(|\.write_text\(|\.write_bytes\(|_atomic_write|open\([^)]*[\"']w")
_NAMES_REGISTRY_FILES = re.compile(
    r"UNITS_DIRNAME|INDEX_NAME|[\"']units[\"']|[\"']index\.json[\"']|features/units|features/index")


def _registry_write_calls(root):
    """-> {(module stem, enclosing function, callee)} for every call of `write_unit`/`write_index`
    under `skills/` and `hooks/`, outside `feature_registry` itself. By AST, so a call spelled
    `registry.write_unit(`, `feature_registry.write_unit(` or a bare `write_unit(` is all found."""
    import ast
    found = set()
    for path in sorted(list(root.glob("skills/**/*.py")) + list(root.glob("hooks/**/*.py"))):
        if path.stem == "feature_registry":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call):
                    f = node.func
                    name = (f.attr if isinstance(f, ast.Attribute)
                            else f.id if isinstance(f, ast.Name) else None)
                    if name in ("write_unit", "write_index"):
                        found.add((path.stem, fn.name, name))
    return found


def _registry_adjacent_writers(root):
    out = set()
    for path in sorted(list(root.glob("skills/**/*.py")) + list(root.glob("hooks/**/*.py"))):
        src = path.read_text(encoding="utf-8")
        if (path.stem != "feature_registry" and _NAMES_REGISTRY_FILES.search(src)
                and _WRITE_PRIMITIVE.search(src)):
            out.add(path.stem)
    return out


def test_every_registry_writer_is_classified():
    found = _registry_write_calls(ROOT)
    assert ("feature_sync", "amend", "write_unit") in found, "the AST scan has gone blind"
    unclassified = sorted(found - set(_REGISTRY_WRITE_CALLERS))
    assert not unclassified, (
        "a new caller writes a unit record or the index without being classified -- route it "
        "through `feature_sync.amend` (the unit's lock and the one legacy-delta rule), or say "
        "here why it is safe: %r" % (unclassified,))
    stale = sorted(set(_REGISTRY_WRITE_CALLERS) - found)
    assert not stale, "classified callers that no longer exist -- remove them: %r" % (stale,)
    for hook in ROOT.glob("hooks/*.sh"):
        text = hook.read_text(encoding="utf-8")
        assert "write_unit" not in text and "write_index" not in text, hook
    adjacent = _registry_adjacent_writers(ROOT)
    assert not sorted(adjacent - set(_REGISTRY_ADJACENT_WRITERS)), (
        "a module names the registry's files and writes something -- if it can write "
        "`features/units/*.json` or `features/index.json`, route it through the registry; either "
        "way classify it in _REGISTRY_ADJACENT_WRITERS: %r"
        % sorted(adjacent - set(_REGISTRY_ADJACENT_WRITERS)))


def test_the_writer_inventory_sees_a_direct_write_unit_call(tmp_path):
    """The control: a synthetic tree whose module calls `write_unit` directly (the classify reopen
    this inventory exists for) and one that writes `index.json` by hand are both reported."""
    (tmp_path / "skills" / "x" / "scripts").mkdir(parents=True)
    (tmp_path / "skills" / "x" / "scripts" / "feature_classify.py").write_text(
        "def _tier1(d, r, e):\n    feature_registry.write_unit(d, r, e)\n")
    (tmp_path / "skills" / "x" / "scripts" / "rogue.py").write_text(
        "def f(p):\n    (p / 'index.json').write_text('{}')\n")
    assert _registry_write_calls(tmp_path) == {("feature_classify", "_tier1", "write_unit")}
    assert _registry_adjacent_writers(tmp_path) == {"rogue"}


def test_write_index_leaves_an_unchanged_index_alone(tmp_path):
    """#327 review (c): identical bytes are not rewritten, so the index's file time -- which the
    legacy-delta rule reads -- moves only when its content does."""
    m = _mod()
    m.write_index(tmp_path, {"alpha": _entry()})
    path = m.index_path(tmp_path)
    os.utime(path, (1_000_000, 1_000_000))
    m.write_index(tmp_path, {"alpha": _entry()})
    assert path.stat().st_mtime == 1_000_000
    m.write_index(tmp_path, {"alpha": _entry(title="changed")})
    assert path.stat().st_mtime != 1_000_000
