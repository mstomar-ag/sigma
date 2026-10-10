#!/usr/bin/env python3
"""The registry entry copied to sibling repos, one-directionally (#1477, epic #1464, story #1427).

#1473 records a goal under its unit in THIS repo. This is the half that reaches the OTHERS, and
§7.1 states the whole of what it must do:

    Each participating repo's `.sdlc/features/` carries the WHOLE entry -- both repos' branches and
    both repos' goals -- not just its own half.

The reason is durability of understanding: somebody who later loses access to one repo, or who only
ever had one, can still see the whole picture of what that unit was instead of seeing half of it and
guessing. A registry describing only its own side is exactly as incomplete as no registry, for the
person who needs it most. Sigma performs the copy on goal pick, because a human step that must
happen in two repos is a step that will be half-done.

--------------------------------------------------------------------------------------------------
THE THREE RULES, AND WHAT EACH ONE REFUSES

1. THE COPY IS WHOLE, AND A HALF ENTRY IS WORSE THAN NONE. `feature_registry.read` REPLACES a unit's
   entry with its shard rather than merging the two, so whatever a write omits is gone from the
   effective registry -- and the loss becomes permanent the moment anything folds the shards back
   into the chart sheet. That cuts both ways here, and both directions are defended:

     - LOCALLY, a pick that wrote only what it knew (its own repo) would erase the sheet's other
       repos. `feature_sync.amend` already read-modify-writes the EFFECTIVE entry for exactly this
       reason; nothing in this module weakens it;
     - REMOTELY, copying our entry over the destination's would erase the goals that repo recorded
       ITSELF and has not propagated back yet. So the copy is a read-modify-write against the
       destination too (`merge_entry`), never a blind overwrite. §7.1's "a file copy, not a merge of
       a shared table" is about not merging one SHARED table across units; a per-unit shard is
       literally the file it means, and reconciling two versions of that one file is what keeps the
       copy whole rather than lossy.

2. DISCOVERY FLOWS ONE WAY: REGISTRY -> REPOS, NEVER REPO -> REGISTRY. Propagation reaches the repos
   `repos` already names. A goal picked in a repo that is NOT named, referencing an existing unit, is
   the other direction -- a SCOPE EXPANSION, which §7.3 makes the unit owner's decision. It lands as
   `sdlc:needs-confirmation` plus a ledger entry to the UNIT owner, and never a silent registry edit.
   Adding the repo automatically because a goal mentioned a label would let any issue in any repo
   widen a unit into a codebase its owner never agreed to touch -- through the one field the registry
   exists to be authoritative about.

   THE RULE IS ENFORCED IN TWO LAYERS BECAUSE IT HAS TWO ENTRY POINTS. `gate_at_pick` refuses the
   pick before anything is built, which needs a backlog source and therefore only exists inside the
   loop; `feature_sync._record_goal` refuses the WRITE, which is reached by a bare `work.py start`
   run by hand as well. Either alone leaves a hole: the gate cannot defend a hand-run start, and the
   registry refusal cannot stop the goal from being worked. Together, `repos` is unchanged on every
   path and the goal is inert on the one that matters.

   AN ENTRY NAMING NO REPOS IS NOT AN EXPANSION. It has made no statement about scope -- `repos` is
   filled by propagation, so an empty block is the ordinary state of every freshly-recorded unit
   (`cross_repo.decide` names the same case for the same reason). An entry naming `a/b` and not
   `c/d` HAS made one, and widening it is somebody's decision.

3. MISSING ACCESS LOSES NOTHING AND BLOCKS NOTHING. Where Sigma cannot write a sibling it
   updates the end it can and ledgers the rest, addressed to that repo's owner (§7.3), so anybody
   who does have access can complete it. `ledger.py` already has a `to` field and `addressed_to()`;
   no new transport, exactly as the issue requires. The half that could not be written is never
   silently dropped and never blocks the half that could -- two siblings, one reachable, still lands
   the reachable one.

--------------------------------------------------------------------------------------------------
NO WRITE TO A FOREIGN REPO WITHOUT A MEASURED `granted`, AND THAT IS INHERITED RATHER THAN INVENTED

This module never asks GitHub whether it may write somewhere. It reads the verdict #1472 already
recorded at pick (`cross_repo.recorded`), and writes ONLY on `granted`. `unknown` is not a soft
`granted` -- it selects no action at all, exactly as it selects no tier there.

That inheritance is worth more than the saved API call. `check_access` refuses to return ANY verdict
from an identity it could not pin, because `gh`'s active account is a device-global keyring slot any
other tool can switch. So a drifted account yields `unknown`, and `unknown` writes nothing: a
misconfigured machine can never commit into a stranger's repository through this path. Re-asking
here would have had to re-derive that whole argument, and a second answer to one question is how the
two drift.

The cost of the same rule is stated rather than hidden: `check_at_pick` runs in `loop._next`, so a
goal started outside the loop has NO recorded decision, and every sibling is then held and ledgered
rather than written. That is the safe direction, it is visible in the report and on the console, and
the next pick through the loop repairs it.

--------------------------------------------------------------------------------------------------
WHAT IS COPIED, AND WHAT DELIBERATELY IS NOT

WHAT A COPY DOES NOT PRESERVE, said before the list of what it does: `registry.parse` ->
`normalise_entry` is a WHITELIST, so a field a newer Sigma adds inside `sigma/features@1` --
an entry-level key, or a per-repo one -- does not survive the round trip and is dropped from the
sibling's file. The refusal in `_their_entry` covers a schema-STRING bump and nothing narrower. So
an addition to an entry requires a schema bump, or a peer on an older plugin erases it; that rule is
recorded beside `feature_registry.SCHEMA`, which is where the schema is owned.

ONLY `units/<name>.json`. Not `index.json` -- it is the DERIVED chart sheet, written by an explicit
fold and never on a pick path, and copying it would carry every OTHER unit's state into a repo that
has its own. Not `<name>.md` either, and that one is a ruling rather than an omission: the `.md` is
the human-readable projection, its managed block is regenerated from the entry by `feature_doc.sync`
on the receiving repo's own next pick, and it carries a human-owned prose region below that block
which a blind Contents-API write cannot see and would destroy. The structured record is the thing
§7.1 is about; the projection follows it locally, where the file's own bytes are in hand.

THE DESTINATION IS THE SIBLING'S DEFAULT BRANCH, which is what omitting `branch` from the Contents
API selects. Not the unit's feature branch: the registry exists BECAUSE a feature branch can be
deleted and take every trace of the unit with it, so a backup living on that same branch would not
be one. §4's "nobody commits directly to a feature branch" points the same way.

THE HONEST LIMIT: `main` under required status checks (§9's default) refuses a direct write, and the
refusal arrives here as a failed write -- which is ledgered to that repo's owner, with the reason
GitHub gave, rather than retried or hidden. That is tier 2's promise applied to the registry copy:
nothing is lost when access is missing.

Module shape follows `feature_sync.py`: zero third-party dependencies, module-level constants,
siblings loaded by file path, every ledger write fail-open, and nothing here may break a pick.
"""
import base64
import collections
import hashlib
import importlib.util
import json
import pathlib
import re
import sys

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


registry = _load("feature_registry")     # the store: schema, read, normalise_entry, dumps
sync = _load("feature_sync")             # repo_slug, the runner shape, the scope-expansion rule
ledger = _load("ledger")                 # team record (config-gated, default OFF; fail-open)
legacy = _load("legacy")                 # #239: records written under the previous name
gate_hold = _load("gate_hold")           # #1005: the park write, the surface probe, the shared predicate

#: WHY `cross_repo` IS NOT LOADED HERE. It loads `work` at ITS module level and `work` loads this
#: module, so an eager `_load("cross_repo")` would recurse -- `_load` has no `sys.modules` cache to
#: break the cycle. `_cross_repo()` below loads it on first use, which is also the only point at
#: which a project has proved it has a sibling to propagate to at all.
_CROSS_REPO = None

#: The `.sdlc` directory a sibling repo carries. NOT knowable from here -- this checkout's own
#: `sdlc_dir` says nothing about somebody else's -- so it is the convention `/sigma-init` scaffolds
#: and every adopter has, named as an assumption rather than derived from one.
SIBLING_SDLC_DIRNAME = ".sdlc"

#: The record this module writes, and where. Same shape and same reason as
#: `cross_repo.RECORD_SCHEMA`: a per-goal file under `.sdlc/state/` (covered by
#: `setup.RUNTIME_IGNORES`) is the natural watermark for "has this ask already been made?".
SCHEMA = "sigma/propagation@1"
RECORD_DIRNAME = "propagation"

#: The outcome of one pass.
NOT_ADOPTED = "not-adopted"       # no `.sdlc/features/` -- this project has not adopted the model
NO_UNIT = "no-unit"               # the goal declares none, so there is nothing to copy
NO_ENTRY = "no-entry"             # the registry records nothing under that unit
NO_SIBLINGS = "no-siblings"       # the unit names only this repo; nothing spans a boundary
NO_SLUG = "no-slug"               # this repo's own `owner/name` could not be resolved -- see below
PROPAGATED = "propagated"         # the pass ran; see `copies` for what happened to each sibling
FAILED = "failed"                 # something went wrong; nothing here is claimed
OUTCOMES = (NOT_ADOPTED, NO_UNIT, NO_ENTRY, NO_SIBLINGS, NO_SLUG, PROPAGATED, FAILED)

#: What happened to ONE sibling.
COPIED = "copied"                 # the whole entry was written there
IDENTICAL = "identical"           # it already says exactly this; nothing was written
HELD = "held"                     # it could not be written, and the remainder was ledgered
COPY_OUTCOMES = (COPIED, IDENTICAL, HELD)

#: Why a sibling was HELD. Each is a different fact and none of them is "we gave up".
NO_DECISION = "no-decision"       # #1472 recorded no verdict for it -- the pick ran outside the loop
NOT_GRANTED = "not-granted"       # the recorded verdict is `denied` or `unknown`; neither authorises
UNREADABLE = "unreadable"         # its current file could not be read, so nothing may overwrite it
WRITE_FAILED = "write-failed"     # the write was attempted and GitHub refused it
HELD_REASONS = (NO_DECISION, NOT_GRANTED, UNREADABLE, WRITE_FAILED, NO_SLUG)

#: WHICH HELD REASONS REACH AN OWNER, AND THE RULE BEHIND THE LIST: only the ones that are about a
#: REPO. Those name a half of this unit that a person with access to that repo has to apply, so they
#: are addressed to that repo's owner (§7.3) and a ledger entry is the only way they arrive.
#:
#: `NO_SLUG` is the one left off, and deliberately: it is about THIS checkout's configuration, not
#: about any sibling. Sending "set discovery.github.repo" to the owners of every repo the unit names
#: would put a note nobody addressed can act on in front of several people at once. It goes to
#: stderr and into the clause `work.start()` prints, which is where whoever is running the pick is
#: already looking -- the identical split `feature_sync` draws for its own `NO_REPO`.
LEDGERED_REASONS = (NO_DECISION, NOT_GRANTED, UNREADABLE, WRITE_FAILED)

#: `cross_repo`'s verdict vocabulary, borrowed BY VALUE and not by import -- see `_CROSS_REPO` for
#: why the module cannot be loaded at this level. The two are pinned equal by a test rather than by
#: an import, exactly as `feature_sync._SLUG_RE` is pinned against `cross_repo._REPO_RE`.
#: ONLY `GRANTED` AUTHORISES A WRITE. `UNKNOWN` is not a soft `DENIED` and neither is a soft
#: `GRANTED`: both hold, and the report says which it was.
GRANTED = "granted"
DENIED = "denied"
UNKNOWN = "unknown"

#: The gate's outcomes.
EXPANSION = "scope-expansion"     # this repo is not in `repos`; the unit owner decides
IN_SCOPE = "in-scope"             # this repo is named, or the unit names none yet
GATE_OUTCOMES = (NOT_ADOPTED, NO_UNIT, NO_ENTRY, EXPANSION, IN_SCOPE, FAILED)

#: `proceed` False means REFUSE THE PICK. `marked` says whether the issue actually became inert --
#: separate fields, because a write that did not land must never be reported as a state that exists
#: (`feature_labels`' REFUSED_WRITE_FAILED rule, and the reason it is a distinct outcome there).
Gate = collections.namedtuple("Gate", "proceed outcome unit repo marked")

#: The idempotency marker for the flag comment. An HTML comment, so it is invisible in the rendered
#: issue and is not something a human reproduces by accident -- the two properties a "have I already
#: said this?" probe needs. DISTINCT from `feature_labels`' two markers for the same reason they are
#: distinct from each other: one shared marker would silence the second problem an issue develops.
SCOPE_MARKER = "<!-- sigma:feature-scope-expansion -->"

#: `gh` reports the status as `(HTTP 404)`; some shapes print `HTTP 404:` instead. A faithful port of
#: `cross_repo._STATUS_RE`, anchored on the literal word for its reason: a bare three-digit run
#: anywhere in a message (a repo named `sku-404`, an issue number, a byte count) must never be read
#: as a status. A port and not an import -- see `_CROSS_REPO`. A missed 404 falls through to
#: `UNREADABLE`, which writes nothing, so anchoring tightly costs a held sibling and never a blind
#: overwrite.
_STATUS_RE = re.compile(r"\bhttp[ /]?(\d{3})\b")

#: How far a failure's own text is quoted into the ledger note. `ledger._sanitize_free_text` caps
#: `why` at `FREE_TEXT_CAP` from the END, so the reason is placed EARLY in the sentence and this
#: bound keeps it from crowding out the identifying half.
_DETAIL_CHARS = 120

#: How many held siblings the one-line clause names before it stops. A clause appended to a result
#: an agent reads back has to stay one line; the count says how many it did not name rather than
#: dropping them silently. Same rule and same reason as `feature_sync._CLAUSE_CAP`.
_CLAUSE_CAP = 2


def _cross_repo():
    """#1472's module, loaded on FIRST USE. Returns None if it cannot be loaded, which degrades to
    "no verdicts", which holds every sibling -- the safe direction."""
    global _CROSS_REPO
    if _CROSS_REPO is None:
        try:
            _CROSS_REPO = _load("cross_repo")
        except Exception:                 # noqa: BLE001 - a missing consumer must not break a pick
            return None
    return _CROSS_REPO


def _note(message):
    """One stderr line, never an exception -- the shape every module on this path holds, for the same
    reason: a diagnostic must never be the thing that breaks a pick."""
    try:
        sys.stderr.write(message)
    except Exception:                     # noqa: BLE001 - a diagnostic must never break a pick
        pass


def _flat(value):
    return " ".join(str(value).split())[:_DETAIL_CHARS]


# --------------------------------------------------------------------------- the destination path


def sibling_path(name):
    """`.sdlc/features/units/<name>.json` -- where this unit's record lives in ANOTHER repo.

    THE REFUSAL IS `feature_registry.unit_path`'S, not a second copy of it -- and ONLY the refusal.
    This is a path in a repository we do not even own, the one destination where a name that escapes
    its directory would be worst, so the guard is borrowed rather than reimplemented. Calling it and
    then building the string with explicit `/` joins is deliberate: the GitHub Contents API
    addresses POSIX paths, and `pathlib` on Windows would hand it backslashes.

    THE FILENAME IS BORROWED TOO, AND THAT IS #1672. It used to be rebuilt here from the RAW name
    while only the refusal was delegated, so this wrote `units/Voice.json` into a sibling whose own
    picks write `units/voice.json` -- two shards for one unit, in someone else's registry, created
    by us. The fix is not a local `name.lower()`: that would be a SECOND definition of the fold,
    free to drift from `unit_path` exactly as three previous rounds of this bug drifted (#1566,
    #1577, #1638). So the basename is read off what the delegate already returned, and the
    guard/fold ORDERING comes with it -- `is_unit_name` rejects `voice.lock` and accepts
    `voice.LOCK`, so a fold applied before the guard would turn an accepted name into the spelling
    of a rejected one, and that ordering now cannot be rearranged from here.

    WHAT IT DOES NOT DO: a `units/<Name>.json` an older plugin already wrote into a sibling stays
    there. It is inert -- `feature_registry.read` globs sorted and last-file-wins, and the folded
    spelling sorts last -- but this module does not delete in a tree it does not own. Measured while
    fixing it: a sibling holding its own `units/voice.json` had its recorded goals read as `{}` by
    the OLD address and merged correctly by this one, so the fold is what PRESERVES that repo's
    entry, not what endangers it.

    Only `PurePath.name` crosses over, never the assembled path: the explicit `/` join stays because
    the GitHub Contents API addresses POSIX paths and `pathlib` on Windows would hand it backslashes.

    RAISES `feature_registry.InvalidUnitName` (a `ValueError`) on anything that is not a unit name."""
    shard = registry.unit_path(pathlib.Path(SIBLING_SDLC_DIRNAME) / registry.REGISTRY_DIRNAME, name)
    return "/".join((SIBLING_SDLC_DIRNAME, registry.REGISTRY_DIRNAME, registry.UNITS_DIRNAME,
                     shard.name))


def document_text(name, entry, alongside=None):
    """The exact bytes a unit's record is written as, here and in every sibling.

    ONE SERIALISER, reached through `feature_registry`'s own `document` + `dumps`, so a propagated
    file and a locally-written one are byte-identical for the ordinary one-key document. Two
    spellings would make a sibling's `read()` and ours answer differently about the same unit for no
    reason but whitespace, and would make the "is it already identical?" comparison fire on every
    pick forever.

    `alongside` carries whatever ELSE the destination's document already held (F7). A second unit
    key in a `units/<name>.json` is malformed by `_read_unit_file`'s own rule, so nothing readable
    is lost by dropping it -- but dropping it is still an unannounced deletion in a tree we do not
    own, and this module's whole posture is that it does not do those. `name` is written LAST, so
    the entry this copy is about always wins its own key."""
    doc = dict(alongside or {})
    doc[name] = registry.normalise_entry(entry)
    return registry.dumps(registry.document(doc))


def digest(text):
    """A short, stable fingerprint of a document. Only ever compared against another one produced
    here, so the truncation is a length choice and not a security claim."""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


# --------------------------------------------------------------------------- the merge


def _fill(into, addition, key):
    """`addition`'s value only where `into` states nothing. The destination's own words are never
    replaced -- §12.2's ruling that the registry narrows its own end and never rewrites what it did
    not establish, applied to a file in somebody else's repository."""
    if not into.get(key) and addition.get(key):
        into[key] = addition[key]


def merge_entry(into, addition):
    """Union `addition` into `into` without ever removing or overwriting. -> a normalised entry.

    THE DIRECTION IS THE WHOLE POINT: `into` is the DESTINATION -- what that repo already says --
    and it wins every disagreement. A blind copy of our entry over theirs would erase the goals that
    repo recorded itself and has not propagated back yet, which is the same half-entry failure
    `write_unit` documents locally, committed into a repository we do not own.

    THE RULES, EACH CHOSEN RATHER THAN FALLEN INTO:

      - `title`, `owner`, `parent`, `tracking_issue`: filled where the destination states nothing,
        never replaced. Propagation makes a repo's picture COMPLETE; it does not adjudicate whose
        wording is right;
      - `open`: the destination's WHENEVER IT STATES ONE, and ours only when it states none at all.
        Closing a unit is a human's decision recorded where they took it, and reopening one is
        `feature_sync.reconcile`'s explicit non-decision -- a copy that could close (or reopen)
        somebody else's unit would be the loudest possible violation of "never a silent registry
        edit". The `in` test is load-bearing and cannot be replaced by reading the normalised value:
        `normalise_entry` applies the `open: true` DEFAULT, so an absent key and a stated `true` are
        indistinguishable afterwards -- and a first copy into a repo that has no file yet would then
        silently reopen a unit our own entry says is closed;
      - `repos`: the UNION of the keys, because that is the whole picture §7.1 asks for. Per repo,
        `branch` and `owner` are filled where empty, and `goals` is an order-preserving union with
        the destination's own order kept first -- `feature_registry._goals` de-duplicates and
        coerces, so appending blindly is correct and idempotent;
      - `authorized`: the DESTINATION'S, always, and never propagated. It is the board owner's
        per-feature grant (§7.3), and copying `true` into a repo would let one repo's registry hand
        itself permission in another -- through the one field §7.3 exists to make deliberate.
        Absent means false, so the default is the safe state."""
    stated_open = isinstance(into, dict) and "open" in into
    into = registry.normalise_entry(into)
    addition = registry.normalise_entry(addition)
    if not stated_open:
        into["open"] = addition["open"]
    for key in ("title", "owner", "parent", "tracking_issue"):
        _fill(into, addition, key)
    for repo, theirs in addition["repos"].items():
        # THE DESTINATION'S OWN KEY when it already names this repo in another casing (F2). Two
        # casings were never two repos, so writing under ours would add a duplicate key to somebody
        # else's file -- the same ruling `feature_sync.repo_key` applies on the local write.
        mine = into["repos"].setdefault(sync.repo_key(into["repos"], repo),
                                        {"branch": None, "owner": None, "authorized": False,
                                         "goals": []})
        _fill(mine, theirs, "branch")
        _fill(mine, theirs, "owner")
        mine["goals"] = list(mine.get("goals") or []) + list(theirs.get("goals") or [])
    return registry.normalise_entry(into)


# --------------------------------------------------------------------------- the record


def record_path(sdlc_dir, goal):
    """The one place a goal becomes this module's record path, and therefore the one place that
    refuses. `cross_repo.decision_path`'s reasoning verbatim, including borrowing `work.stem` rather
    than re-deriving it: two opinions about what a goal's filename is would be two files. An empty
    stem is refused as well as an unsafe one -- `state.unsafe_goal_reason` is about path ESCAPE and
    considers `""` safe, but a record filed under no goal is one nothing can ever find again."""
    cross = _cross_repo()
    if cross is None:
        raise RuntimeError("cross_repo could not be loaded, so no record path can be resolved")
    stem = cross.work.stem(goal)
    if not str(stem).strip():
        raise ValueError("a propagation record needs a goal to be filed under, and %r reduces to "
                         "nothing" % (goal,))
    reason = cross.state.unsafe_goal_reason(stem)
    if reason:
        raise ValueError("unsafe goal %r for the propagation record: %s" % (goal, reason))
    return pathlib.Path(sdlc_dir) / "state" / RECORD_DIRNAME / (str(stem) + ".json")


def recorded(sdlc_dir, goal):
    """What the last pass concluded for this goal, or None. Never raises; absent and corrupt both
    mean "nothing was recorded", which suppresses nothing."""
    try:
        got = json.loads(record_path(sdlc_dir, goal).read_text(encoding="utf-8"))
    except Exception:                     # noqa: BLE001 - absent and corrupt both mean "no record"
        return None
    return got if isinstance(got, dict) and legacy.schema_is(got.get("schema"), SCHEMA) else None


def _store(sdlc_dir, report):
    """Persist, best-effort. A record that cannot be written costs the NEXT pass its suppression --
    one duplicate note -- and must never cost this pass its copy."""
    try:
        path = record_path(sdlc_dir, report["goal"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    except Exception as exc:              # noqa: BLE001 - never break a pick over a record
        _note("sigma: features: the propagation record for %s could not be written (%s); the "
              "next pick may repeat a note somebody has already seen.\n" % (report.get("goal"), exc))


def _ask(report):
    """The fingerprint of what this pass is ASKING FOR -- which siblings are held, why, and the
    document they are held on. Compared against the prior record so a re-pick of one goal does not
    put the same note in front of the same owner again (#1472's N3, inherited).

    THE DIGEST IS IN THE FINGERPRINT ON PURPOSE. A held sibling whose entry has GROWN is a different
    ask -- there is more to apply than last time -- and it must reach its owner even though the
    reason is unchanged. Suppression is for repetition, never for news."""
    return sorted("%s|%s|%s|%s" % (repo, one["outcome"], one["reason"], report["digest"])
                  for repo, one in report["copies"].items()
                  if one["outcome"] == HELD and one["reason"] in LEDGERED_REASONS)


# --------------------------------------------------------------------------- the ledger edge


def _tell(sdlc_dir, goal, unit, why, to=None):
    """One stderr line always, one ledger entry when the ledger is on -- the same shape and the same
    reasoning as `feature_sync._tell`, including `kind="note"` rather than `handoff`:
    `backlog_check._ledger_signals` reads a hand-off as a real block, so raising a missing copy that
    way would PARK the very goal whose pick just found it."""
    _note("sigma: features: %s\n" % why)
    fields = {"why": why, "area": registry.REGISTRY_DIRNAME,
              "ref": "%s/%s" % (registry.REGISTRY_DIRNAME, unit)}
    if isinstance(to, str) and to.strip():
        fields["to"] = to
    ledger.safe_append(sdlc_dir, "note", goal, **fields)


_HELD_WORDING = {
    NO_SLUG: "this checkout's own `owner/name` could not be resolved from `discovery.github.repo` "
             "or from its git remote, so unit %(unit)s's siblings cannot be told apart from this "
             "repo itself and NOTHING was copied. Set discovery.github.repo",
    NO_DECISION: "no access verdict was recorded for %(repo)s, so unit %(unit)s was not copied "
                 "there -- this goal was started outside the loop, where the pick-time check runs. "
                 "Re-pick it through the loop, or apply the entry by hand",
    NOT_GRANTED: "unit %(unit)s could not be copied into %(repo)s: this account has no confirmed "
                 "write access there (%(detail)s), so that repo's copy of the entry needs somebody "
                 "who has",
    UNREADABLE: "unit %(unit)s could not be copied into %(repo)s: its current record could not be "
                "read (%(detail)s), and nothing overwrites a file nobody managed to look at",
    WRITE_FAILED: "unit %(unit)s could not be written into %(repo)s (%(detail)s) -- the entry is "
                  "recorded in full here and that repo's copy is the half still owed",
}


def _surface_held(sdlc_dir, report, entry, prior):
    """Say what this pass could not write -- everything on stderr, the per-REPO halves to an owner.

    THE SPLIT IS `LEDGERED_REASONS`, and it is about who can act. A half that a person with access
    to a named repo has to apply is addressed to that repo's owner (§7.3), falling back to the unit
    owner when the entry names none -- a note in a stream nobody reads is not surfacing. A finding
    about THIS checkout's own configuration (`NO_SLUG`) belongs on the console of whoever is running
    the pick instead, which is the identical split `feature_sync._surface` draws for `NO_REPO`.

    An entry with no resolvable owner at either level is still WRITTEN, and `unaddressed` is what
    reports that it reached nobody -- the same honesty `cross_repo._raise_unavailable` records."""
    held = {repo: one for repo, one in report["copies"].items() if one["outcome"] == HELD}
    for repo in sorted(held):
        if held[repo]["reason"] not in LEDGERED_REASONS:
            _note("sigma: features: %s\n" % (_HELD_WORDING[held[repo]["reason"]] %
                  {"unit": report["unit"], "repo": repo, "detail": held[repo]["detail"] or "-"}))
    held = {r: o for r, o in held.items() if o["reason"] in LEDGERED_REASONS}
    if not held:
        return
    if isinstance(prior, dict) and prior.get("ask") == report["ask"]:
        report["raised"] = prior.get("raised") or []
        report["unaddressed"] = prior.get("unaddressed") or []
        report["raise_suppressed"] = True
        return
    for repo in sorted(held):
        one = held[repo]
        to = (entry["repos"].get(repo) or {}).get("owner") or entry.get("owner")
        if not to:
            report["unaddressed"].append(repo)
        _tell(sdlc_dir, report["goal"], report["unit"],
              _HELD_WORDING[one["reason"]] % {"unit": report["unit"], "repo": repo,
                                              "detail": one["detail"] or "-"}, to=to)
        report["raised"].append({"repo": repo, "to": to})


# --------------------------------------------------------------------------- the remote file


class _Absent(Exception):
    """The destination has no such file yet. NOT a failure -- the create path."""


class _Unreadable(Exception):
    """The destination has a file there and this code cannot read it as this unit's record."""


def _their_entry(current, unit):
    """-> `(the destination's entry for this unit, the key IT spells it with, its WHOLE document)`.

    THE WHOLE DOCUMENT COMES BACK BECAUSE THE WRITE MUST NOT DELETE WHAT IT DID NOT COME FOR (F7).
    A `units/<name>.json` carrying a second unit key is malformed by `_read_unit_file`'s own rule
    and nothing readable is lost by dropping it -- but an unannounced deletion in a foreign tree is
    exactly what this module's stated rule ("a file we cannot read is not a file we may replace")
    exists to refuse. Rebuilding the document around the other keys costs nothing and deletes
    nothing.

    RAISES `_Unreadable` WHENEVER A FILE IS THERE AND DOES NOT YIELD THIS UNIT, and that refusal is
    the point of the function. `parse` degrades an unknown schema, a non-object, or a key that is
    not a unit name to "nothing readable" -- which is its specified behaviour and exactly right for a
    READER, and a licence to overwrite for nobody. Without this, a destination carrying a
    `sigma/features@2` document (or one this version simply cannot parse) would merge into `{}`,
    produce our entry verbatim, and be CLOBBERED with it -- the half-entry failure §7.1 forbids,
    performed on the file a future version of this tool wrote.

    THE PREDICATE IS `_read_unit_file`'S, borrowed rather than re-derived: what a unit file may speak
    for is bounded by its own filename, matched CASE-INSENSITIVELY, and a file that names a different
    unit than its path is one `feature_registry.read` already drops rather than trusts. The key that
    comes back is the DESTINATION'S OWN SPELLING, for that function's reason -- that is what its
    author wrote, and a copy is not the place to correct it.

    THE PROMISE THIS REFUSAL MAKES IS EXACTLY ONE SCHEMA VERSION WIDE (F6). It stops a document
    whose `schema` STRING this version cannot read from being replaced. It does NOT preserve fields
    a future Sigma adds INSIDE `sigma/features@1`: `registry.parse` -> `normalise_entry` is
    a whitelist, so any entry-level or per-repo key outside the `@1` schema is dropped on the way
    through and does not come back out in the document this copy writes. The consequence is a rule
    for the schema rather than a bug here, and it is stated where `SCHEMA` is defined: an addition
    to an entry requires a schema BUMP, because a peer running an older Sigma will otherwise
    erase it from the sibling's file on the next pick. `test_a_field_added_inside_at1_is_dropped_by_a_copy`
    pins the behaviour so it is known rather than discovered.

    An ABSENT file (`current is None`) is not unreadable: it is the create path, and it merges into
    an empty entry, which is our entry in full."""
    if current is None:
        return {}, unit, {}
    try:
        payload = json.loads(current)
    except Exception as exc:              # noqa: BLE001 - every parse failure is one refusal
        raise _Unreadable("the destination's record is not JSON (%s)" % exc)
    # #239 (plan-review BLOCKER 1): `registry.parse` now READS the previous name's schema id, which
    # must not become a licence to REWRITE a sibling repository the old plugin may still own -- that
    # plugin cannot read the Sigma id this write would leave behind. Refused like any file this
    # version will not replace; `migrate.py` run in THAT repository is the explicit cutover.
    if isinstance(payload, dict) and legacy.is_legacy_schema(payload.get("schema")):
        raise _Unreadable("the destination's record still carries the plugin's previous name in its "
                          "schema id (%r); run skills/sigma-doctor/scripts/migrate.py --apply in that "
                          "repository first, so it is not overwritten behind its owner's back"
                          % (payload.get("schema"),))
    known = registry.parse(payload)
    for name in known:
        if name.lower() == unit.lower():
            return known[name], name, known
    raise _Unreadable("the destination has a record at that path and it does not describe unit %r "
                      "-- it may be a schema this version cannot read, so it is not overwritten"
                      % (unit,))


def _read_remote(run, cwd, repo, path):
    """-> `(text, blob_sha)` for one sibling's record. Raises `_Absent` on a 404.

    A READ THAT FAILED IS NOT "THE FILE IS ABSENT", and the two are told apart by the status rather
    than by the presence of an exception -- writing on the strength of a 502 would replace a document
    nobody managed to look at. Same distinction `feature_sync.live_branches` draws one module along,
    for the same reason it gives."""
    try:
        raw = run(cwd, ["gh", "api", "repos/%s/contents/%s" % (repo, path)])
    except Exception as exc:              # noqa: BLE001 - the status decides which failure this is
        found = _STATUS_RE.search(str(exc).lower())
        if found and found.group(1) == "404":
            raise _Absent(str(exc))
        raise
    payload = json.loads(raw or "null")
    if not isinstance(payload, dict):
        raise ValueError("gh answered with a %s, not a file object" % type(payload).__name__)
    content, encoding = payload.get("content"), payload.get("encoding")
    if encoding != "base64" or not isinstance(content, str):
        raise ValueError("the contents API answered with encoding %r, which cannot be decoded"
                         % (encoding,))
    return base64.b64decode(content).decode("utf-8", errors="replace"), payload.get("sha")


def _write_remote(run, cwd, repo, path, text, sha, message):
    """PUT one sibling's record. `branch` is deliberately OMITTED, which selects that repository's
    DEFAULT branch -- see the module docstring for why a backup may not live on the branch whose
    deletion is the reason the backup exists.

    `sha` is sent only when the file already exists: the Contents API reads its presence as "update
    this exact blob" and its absence as "create", and sending a stale one is how a concurrent edit
    is refused rather than silently clobbered."""
    args = ["gh", "api", "-X", "PUT", "repos/%s/contents/%s" % (repo, path),
            "-f", "message=" + message,
            "-f", "content=" + base64.b64encode(text.encode("utf-8")).decode("ascii")]
    if isinstance(sha, str) and sha:
        args += ["-f", "sha=" + sha]
    return run(cwd, args)


def _copy(kind, reason="", detail=""):
    return {"outcome": kind, "reason": reason, "detail": detail}


def _copy_one(run, cwd, repo, unit, entry, verdict, message):
    """Copy the whole entry into ONE sibling. -> that sibling's result. Never raises."""
    if verdict is None:
        return _copy(HELD, NO_DECISION)
    if verdict != GRANTED:
        return _copy(HELD, NOT_GRANTED, verdict)
    try:
        path = sibling_path(unit)
    except ValueError as exc:             # the naming contract, caught broadly on purpose: `_load`
        return _copy(HELD, UNREADABLE, _flat(exc))     # gives every module its own exception class
    sha, current = None, None
    try:
        current, sha = _read_remote(run, cwd, repo, path)
    except _Absent:
        pass
    except Exception as exc:              # noqa: BLE001 - never overwrite what could not be read
        return _copy(HELD, UNREADABLE, _flat(exc))
    # WHAT THAT REPO ALREADY SAYS, read through the SAME parser its own `read()` uses. Guarded on
    # its own: one sibling's corrupt file must cost that sibling and never the ones after it.
    try:
        theirs, key, whole = _their_entry(current, unit)
    except _Unreadable as exc:            # a file we cannot read is not a file we may replace
        return _copy(HELD, UNREADABLE, _flat(exc))
    mine = entry
    merged = merge_entry(theirs, mine)
    text = document_text(key, merged, whole)
    if current is not None and current == text:
        return _copy(IDENTICAL)
    try:
        _write_remote(run, cwd, repo, path, text, sha, message)
    except Exception as exc:              # noqa: BLE001 - a refused write is ledgered, never dropped
        return _copy(HELD, WRITE_FAILED, _flat(exc))
    return _copy(COPIED, "", digest(text))


# --------------------------------------------------------------------------- the pass


def _report(goal, unit):
    return {"schema": SCHEMA, "outcome": PROPAGATED, "goal": str(goal), "unit": unit, "repo": None,
            "siblings": [], "copies": {}, "digest": "", "ask": [], "raised": [], "unaddressed": [],
            "raise_suppressed": False, "why": "", "note": ""}


def propagate_at_pick(sdlc_dir, config, goal, unit, run=None, cwd=None, remote=None):
    """THE PICK-PATH COPY. Put the whole entry in every sibling repo `repos` names.

    NEVER RAISES. A registry is a record; losing one is bad, and losing the GOAL because the record
    could not be copied is worse. Everything resolves to a report and `FAILED` claims nothing -- the
    posture `feature_sync.sync_at_pick` and `cross_repo.check_at_pick` both take, for the reason they
    both give.

    RUNS AFTER `feature_sync.sync_at_pick`, NOT INSTEAD OF IT. That pass records this goal under the
    unit and reconciles the entry against the branches that exist; this one reads the RESULT of that
    -- `registry.read`'s union, the whole entry -- and copies it out. Reversing the order would
    propagate an entry missing the goal whose pick triggered it.

    `unit` IS HANDED IN, ALREADY RESOLVED, exactly as it is for `sync_at_pick`: there is no code path
    here that reads an issue, so "no second network read" is structural rather than a promise.

    THE COST, STATED: nothing at all for a project with no registry, a goal declaring no unit, or a
    unit naming only this repo -- no module load, no `gh` call. Otherwise ONE `gh api .../contents/`
    read per sibling per pick, plus a write only when the destination does not already say exactly
    this. There is deliberately no local watermark to skip that read: a watermark would let a sibling
    whose copy was hand-edited away drift forever, and the read IS the measurement. The number of
    siblings is whatever a human committed to `repos`, and no timeout is imposed on the runner --
    that is `work._run`'s posture for every call on this pick path (`git fetch`, `git ls-remote`, the
    issue read), inherited on purpose rather than answered with a second runner."""
    report = _report(goal, unit)
    try:
        return _propagate_at_pick(sdlc_dir, config, goal, unit, run, cwd, remote, report)
    except Exception as exc:              # noqa: BLE001 - "never raises" has to be total
        report["outcome"] = FAILED
        report["why"] = _flat(exc)
        _note("sigma: features: the registry was not propagated for %s (%s); the entry is "
              "recorded here and the sibling copies are owed.\n" % (goal, report["why"]))
        return report


def _propagate_at_pick(sdlc_dir, config, goal, unit, run, cwd, remote, report):
    features_dir = registry.registry_dir(sdlc_dir)
    if not features_dir.is_dir():
        # THE CHEAPEST STEP IS THE OPT-OUT, exactly as `feature_sync._sync_at_pick` and
        # `cross_repo._check_at_pick` order their own: a project that never adopted the branching
        # model pays no read, no `gh` call and no new failure mode it did not ask for.
        report["outcome"] = NOT_ADOPTED
        return report
    if not unit:
        report["outcome"] = NO_UNIT
        return report
    raw = registry.read(features_dir).get(unit)
    if raw is None:
        report["outcome"] = NO_ENTRY
        report["why"] = "the registry records nothing under %r, so there is nothing to copy" % unit
        return report
    entry = registry.normalise_entry(raw)
    run = run or sync._run
    cwd = str(cwd or pathlib.Path(sdlc_dir).parent)
    remote = remote or sync.DEFAULT_REMOTE
    here = sync.repo_slug(config, run, cwd, remote)
    report["repo"] = here
    if here is None:
        # F1. WITHOUT OUR OWN SLUG THERE IS NO SIBLING SET, ONLY A LIST OF REPOS, and the difference
        # is the whole safety property of this module. `r != here` with `here` None is true of EVERY
        # key, so the repo this pick is standing in would join its own sibling list and be committed
        # into -- on its DEFAULT branch, through the Contents API, outside the PR flow, racing the
        # local write the same pass just made. The grant gate does not stop it: `cross_repo`
        # measures every repo the entry names, ours included, so `granted` for ourselves is the
        # ordinary recorded state of any cross-repo unit.
        #
        # `feature_sync.is_scope_expansion` takes the SAME reading of the SAME None one module over
        # ("a repo that could not be resolved is not an expansion"), and this used to take the
        # opposite one. One value, one reading: an unresolved slug is IGNORANCE, and ignorance
        # writes nothing.
        report["outcome"] = NO_SLUG
        report["siblings"] = sorted(entry["repos"])
        report["why"] = "no `owner/name` resolved for this checkout, so no repo is a sibling"
        for repo in report["siblings"]:
            report["copies"][repo] = _copy(HELD, NO_SLUG, remote)
        _surface_held(sdlc_dir, report, entry, recorded(sdlc_dir, goal))
        _store(sdlc_dir, report)
        report["note"] = _clause(report)
        return report
    report["siblings"] = sorted(r for r in entry["repos"] if not sync.same_repo(r, here))
    if not report["siblings"]:
        report["outcome"] = NO_SIBLINGS
        return report

    report["digest"] = digest(document_text(unit, entry))
    verdicts = _verdicts(sdlc_dir, goal)
    message = ("chore(features): propagate unit %s from %s (goal %s)"
               % (unit, here or "an unnamed repo", report["goal"]))
    for repo in report["siblings"]:
        report["copies"][repo] = _copy_one(run, cwd, repo, unit, entry, verdicts.get(repo), message)
    report["ask"] = _ask(report)
    _surface_held(sdlc_dir, report, entry, recorded(sdlc_dir, goal))
    _store(sdlc_dir, report)
    report["note"] = _clause(report)
    return report


def _verdicts(sdlc_dir, goal):
    """-> `{repo: verdict}` from the landing decision #1472 recorded at pick, or `{}`.

    READ, NEVER RE-TAKEN. This module has no `run` it could hand an access check and no code path
    that would perform one -- which is what makes "the access check happens once, at pick" a property
    of the shape rather than a convention, the same way `cross_repo.recorded` makes it one for the
    merge gate. Everything is tolerated into `{}`, which holds every sibling: an unreadable decision
    is ignorance, and ignorance writes nothing."""
    cross = _cross_repo()
    if cross is None:
        return {}
    try:
        decision = cross.recorded(sdlc_dir, goal)
    except Exception:                     # noqa: BLE001 - not knowing is not permission
        return {}
    repos = decision.get("repos") if isinstance(decision, dict) else None
    if not isinstance(repos, dict):
        return {}
    return {repo: one["verdict"] for repo, one in repos.items()
            if isinstance(repo, str) and isinstance(one, dict) and isinstance(one.get("verdict"), str)}


def _clause(report):
    """The one-line clause `work.start()` appends to its own result, or "".

    ONLY WHAT SOMEBODY HAS TO ACT ON. A copy that landed is the ordinary case and says nothing; a
    HELD sibling is a half of this unit that now needs a person, so it is named on the line the
    operator is already reading. The overflow is COUNTED rather than dropped -- the same rule
    `feature_sync._clause` states, so two findings and nine never print the same text."""
    held = sorted(r for r, one in report["copies"].items() if one["outcome"] == HELD)
    if not held:
        return ""
    said = ", ".join("%s (%s)" % (r, report["copies"][r]["reason"]) for r in held[:_CLAUSE_CAP])
    more = len(held) - _CLAUSE_CAP
    return " — propagation: %s%s" % (said, (" +%d more" % more) if more > 0 else "")


# --------------------------------------------------------------------------- the one-way gate


def _scope_text(unit, repo, owner):
    return (
        "%s\n"
        "**Sigma has set this goal aside.** It declares unit `%s`, and the registry does not "
        "list `%s` among that unit's repos.\n\n"
        "Adding a repo to a unit is an **expansion of that unit's scope**, which is its owner's "
        "decision (§7.3) — not something a goal in an unlisted repo may assert by referencing a "
        "label. Doing it automatically would let any issue in any repo widen a unit into a codebase "
        "its owner never agreed to touch.\n\n"
        "%s has been asked through the ledger. When they accept, `%s` joins `repos` and this goal "
        "can be promoted with `/sigma-promote`; propagation then reaches it like any other sibling."
        % (SCOPE_MARKER, unit, repo, owner or "The unit owner", repo))


def _has_surface(source, config=None):
    """Can this source hold a goal? With triage on it needs `park` (the hold is a park); a source that
    can only write the old label keeps doing that; with neither the gate reports no surface. The probe
    MUST accept `park`: if it asked for the label method alone, a source without it would read as no
    surface and the gate would fail open."""
    return gate_hold.surface_mode(source, config, ("note", "fetch_comments_strict")) is not None


def _flag(source, goal, text):
    """Post the flag comment, once per pass per timeline read. -> True iff this call posted it.

    Idempotent against the ISSUE'S OWN TIMELINE rather than local state, because the state that
    matters is the one a human reads -- `feature_labels._flag`'s reasoning and its honest bound: the
    probe is a read followed by a write with nothing serialising them, so two machines whose windows
    overlap can each post one comment. That costs a duplicate comment, never a wrong state."""
    try:
        seen = source.fetch_comments_strict(goal)
    except Exception as exc:              # noqa: BLE001 - a read we cannot trust posts nothing
        _note("sigma: features: could not read #%s's comments (%s) — the scope-expansion flag "
              "was not posted this pass; the next pick retries it\n" % (goal, exc))
        return False
    for comment in seen.get("comments") or []:
        if legacy.has_marker((comment or {}).get("body") or "", SCOPE_MARKER):
            return False
    try:
        source.note(goal, text)
        return True
    except Exception as exc:              # noqa: BLE001 - flagging must never break the pick
        _note("sigma: features: could not comment on #%s (%s) — the refusal stands, but nobody "
              "has been told on the issue itself\n" % (goal, exc))
        return False


def gate_at_pick(sdlc_dir, source, goal, config, unit, run=None, cwd=None, remote=None):
    """-> `Gate`. Is this goal's repo one the unit's owner has agreed it may touch? (§7.1, §12.2)

    CALLED INSIDE `loop._next`'S CLAIM LOCK, right after the unit label is attached and BEFORE
    `mark_in_progress` -- the last moment at which nothing has acted on the goal. A refusal here is
    the cheapest possible one: no worktree, no branch, no registry write, no PR.

    `sdlc:needs-confirmation` STANDS ALONE (`docs/label-model.md` §2): the goal gives up membership
    and waits for a human, which is exactly what "inert until its owner promotes it" already means
    everywhere else in this tool. That is a real gate rather than an overlay, and it is the right one
    here for the reason the label exists -- unlike a missing label, which self-heals the moment
    somebody creates it, a scope expansion needs a DECISION that only the unit owner can take.

    NEVER RAISES, and the totality is layered rather than promised. The detection is local (a
    registry read), so the outer guard only ever covers a failure to DETECT -- which proceeds, since
    a gate that cannot answer must not stop a queue. Every call into `source` is guarded on its own,
    so a source that raises from all three methods still yields the refusal detection already
    reached.

    THE THREE DIRECTIONS, each chosen rather than fallen into:

      - THE MARK LANDED           -> REFUSE, `marked` True, and NOW write the durable things: one
                                     flag comment (idempotent against the issue's own timeline) and
                                     one ledger entry to the unit owner.
      - THE MARK DID NOT LAND     -> REFUSE, `marked` False, and write NOTHING durable -- no comment,
                                     no ledger entry. `feature_labels`' REFUSED_WRITE_FAILED rule
                                     verbatim: a write that did not land is not evidence of a state,
                                     so nothing may claim one, and a network blip must not summon a
                                     human. It is also what bounds the noise: the goal keeps
                                     `sdlc:goal`, so it is re-picked, and a note written here would
                                     be written again on every pick until the blip cleared.
                                     `loop._next` skips it for this call and the next pick retries.
      - THE SOURCE HAS NO SURFACE -> PROCEED. `LocalSource` has none of these methods and no
                                     repository label namespace at all; refusing forever with no way
                                     to signal is the "issue nobody can find" this codebase never
                                     converges on. `feature_sync._record_goal` still refuses the
                                     registry widening on that path, so `repos` is unchanged either
                                     way -- the goal is worked, and the field is defended.

    THE LEDGER ENTRY IS RAISED ONLY WHEN THE PICK IS ACTUALLY REFUSED, and that is not a detail. A
    refused pick never reaches `work.start()`, so `feature_sync._surface` -- which ledgers the same
    finding to the same owner -- never runs for it. When this gate PROCEEDS, that pass does run and
    does raise it. One event, one note, on every path.

    THE ONE RESIDUE, NAMED RATHER THAN LEFT TO BE FOUND. `blockers.classify` returns `PROMOTED` for
    an issue carrying BOTH `sdlc:followup` and `sdlc:needs-confirmation` (`blockers.py`, the
    `proposed_label` arm), and `blockers._act` then grants membership back and strips the proposal
    label -- reached through `verdict_for`/`resolve`/`_act` and `auto_unpark`, whenever something
    declares itself blocked by that issue. That is `docs/label-model.md` §2's "Sigma's OWN
    unapproved follow-up" row, justified there by `needs-confirmation` meaning *no human has ever
    ruled on it*. A goal held HERE has been ruled on, just not on this question, so the promotion
    undoes this refusal.

    (`sources._promote_blockers` is NOT the promoter and an earlier version of this note said it
    was. That function is #900's PRIORITY promotion and never writes a lifecycle label. The wrong
    pointer is recorded rather than quietly swapped, because it also reached `docs/label-model.md`,
    which `AGENTS.md` calls the contract for every `sdlc:*` label.)

    HOW REACHABLE IT ACTUALLY IS, re-derived rather than waved at. A `{followup, needs-confirmation}`
    issue is not pickable at all (`not_eligible_labels` refuses `proposed_label`), so this gate can
    only put an issue INTO that shape after a human promoted it out of it. The full path is: #1471
    stamps unit U onto a follow-up Sigma files in repo R; a human promotes it; it is picked;
    this gate refuses it, restoring `needs-confirmation` beside the permanent `sdlc:followup`; and
    something then names it as a blocker. R need not have been listed at the parent's pick either --
    `Gate(True, NO_ENTRY, ...)` proceeds for a unit with no entry yet, and a later hand edit can
    create U naming only S, or remove R from it. So it is a real path, not a nearly-impossible one.

    IT IS BOUNDED, WHICH IS THE PART THAT MAKES LEAVING IT DEFENSIBLE. The promotion fires on a
    BLOCKER event (a block being recorded, or an unpark sweep), not on every pick, so the worst case
    is one promote/refuse pair per such event -- two label swaps -- and never the unbounded flap
    `feature_labels` measured for `sdlc:blocked`. The goal ends each cycle inert, which is the safe
    end.

    WHAT WOULD CLOSE IT, for whoever does: `classify` decides on LABELS alone and cannot see the
    flag comment this gate posts, so distinguishing "held for scope" from "never ruled on" needs a
    signal in the label set. Choosing one is a change to that row's policy, which is ownership
    enforcement's (#1479) to make; guessing at it from here would be a second opinion about a
    contract this goal does not own."""
    try:
        return _gate_at_pick(sdlc_dir, source, goal, config, unit, run, cwd, remote)
    except Exception as exc:              # noqa: BLE001 - a gate that cannot answer must not block
        _note("sigma: features: the scope check for %s did not run (%s); the pick carries on "
              "and the registry still refuses to widen the unit.\n" % (goal, _flat(exc)))
        return Gate(True, FAILED, unit, None, False)


def _gate_at_pick(sdlc_dir, source, goal, config, unit, run, cwd, remote):
    features_dir = registry.registry_dir(sdlc_dir)
    if not features_dir.is_dir():
        return Gate(True, NOT_ADOPTED, unit, None, False)
    if not unit:
        return Gate(True, NO_UNIT, unit, None, False)
    raw = registry.read(features_dir).get(unit)
    run = run or sync._run
    cwd = str(cwd or pathlib.Path(sdlc_dir).parent)
    # NO LOCAL ENTRY PROCEEDS. #1568 IS OPEN AGAINST THIS LINE AND ITS FIX WAS WITHDRAWN (#1645).
    # Read the whole of this before reinstating anything: the obvious repair has been tried, shipped
    # and reverted, and the paragraphs below are in the order the argument actually went.
    #
    # 1. WHAT #1568 REPORTED, AND IT IS STILL TRUE. Propagation is ONE-DIRECTIONAL -- an entry only
    #    ever reaches repos the unit already lists -- so a repo the unit does NOT list is, by
    #    construction, a repo whose registry is empty. The proceed condition below ("a unit nobody
    #    has recorded is CREATED by this pick") is therefore identical to the condition defining the
    #    expansion this gate exists to catch. The gate cannot fire in its own case.
    #
    # 2. WHAT WAS TRIED. A second arm asked the REMOTE whether `feature/<unit>` already existed,
    #    reasoning that a unit live elsewhere is not a unit this pick is creating. It was routed
    #    around `is_scope_expansion` rather than through it, because that predicate answers False
    #    for an entry naming no repos -- deliberately, since an empty `repos` block is the ordinary
    #    state of every freshly recorded unit.
    #
    # 3. WHY IT WAS WITHDRAWN. It is wrong against this project's OWN documented adoption order:
    #    §14 pushes the branch FIRST and declares the unit LAST. So on the first pick of a brand-new
    #    unit the branch exists, the registry is adopted, and no entry names the unit -- which is
    #    indistinguishable from the expansion the arm was meant to catch. Measured: that pick had
    #    `sdlc:goal` stripped and was set aside, so every first adoption refused itself.
    #
    # 4. WHAT THAT LEAVES. There is no LOCAL evidence separating the two states, and the one remote
    #    fact to hand does not separate them either -- both show an empty registry and a live
    #    branch. A gate that blocks all adoption is strictly worse than a gate that cannot fire, so
    #    this returns to the prior behaviour and #1568 carries what was learned. A real fix needs
    #    evidence from the unit's HOME registry, which this gate does not have and does not fetch.
    #
    # `promote._feature_hold` mirrors this exactly and says so; the two must not drift apart again.
    if raw is None:
        return Gate(True, NO_ENTRY, unit, None, False)
    entry = registry.normalise_entry(raw)
    repo = sync.repo_slug(config, run, cwd, remote or sync.DEFAULT_REMOTE)
    if not sync.is_scope_expansion(entry, repo):
        return Gate(True, IN_SCOPE, unit, repo, False)
    owner = entry.get("owner")
    mode = gate_hold.surface_mode(source, config, ("note", "fetch_comments_strict"))
    if mode is None:
        _note("sigma: features: goal %s declares unit %s from %s, which the unit does not list "
              "— this backlog source cannot park or mark it, so the pick carries on and "
              "the registry refuses the widening instead.\n" % (goal, unit, repo))
        return Gate(True, EXPANSION, unit, repo, False)
    marked = False
    try:
        if mode == gate_hold.PARK:
            marked = gate_hold.park_for_gate(
                source, goal, "scope_hold",
                "%s declares unit %s from %s, which the unit does not list" % (goal, unit, repo))
        else:
            marked = source.mark_needs_confirmation(goal) is not False
    except Exception as exc:              # noqa: BLE001 - the refusal stands whatever the write did
        _note("sigma: features: could not hold #%s (%s) — the pick is refused "
              "and the next one retries the hold\n" % (goal, _flat(exc)))
    if not marked:
        # THE TRANSIENT DIRECTION, and nothing durable is written on it. See the docstring: the goal
        # still carries `sdlc:goal`, so it comes back, and a note written here would be written
        # again every pick until the write started landing.
        return Gate(False, EXPANSION, unit, repo, False)
    # F5. THE FLAG COMMENT IS THE WATERMARK FOR THE LEDGER NOTE, and one gates the other rather
    # than each carrying its own idempotency. `_flag` returns True only when it actually POSTED --
    # it probes the issue's own timeline for `SCOPE_MARKER` first -- so the pair is written on the
    # pick that discovers the expansion and on no later one. Without this the note was
    # unconditional, and a goal re-picked after a `/sigma-promote` that did not also edit `repos`
    # (exactly the order the flag comment invites) put a fresh, identical note in front of the same
    # owner every time.
    #
    # THE RESIDUAL, STATED: when the timeline cannot be READ, `_flag` posts nothing and returns
    # False, so the note is LATE rather than duplicated -- the next pick retries both together.
    # That is `feature_labels._flag`'s own trade ("a duplicate comment is a worse failure than a
    # late one"), and the label is already on the issue by this point, so the goal is inert
    # whether or not either message landed this pass.
    #
    # THE `why` LEADS WITH THE FACTS, because `ledger._sanitize_free_text` caps it at
    # `FREE_TEXT_CAP` from the END: the goal, the unit and the repo can never be truncated away.
    if _flag(source, goal, _scope_text(unit, repo, owner)
             + (gate_hold.PARK_NOTE if mode == gate_hold.PARK else "")):
        _tell(sdlc_dir, goal, unit,
              "goal %s declares unit %s from %s, which that unit does not list -- adding a repo "
              "expands the unit's scope, so the goal is inert until you accept it"
              % (goal, unit, repo), to=owner)
    return Gate(False, EXPANSION, unit, repo, True)


# --------------------------------------------------------------------------- CLI


USAGE = "usage: feature_propagate.py show <sdlc_dir> <goal>"


def main(argv):
    """`feature_propagate.py show <sdlc_dir> <goal>` -- read back what the last pick propagated.

    Read-only on purpose, and the omission is `cross_repo.main`'s and `feature_sync.main`'s: the copy
    belongs to the pick, and a second way to run it is a second answer."""
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    if len(argv) >= 4 and argv[1] == "show":
        got = recorded(argv[2], argv[3])
        print(json.dumps(got, indent=2, sort_keys=True) if got else
              "nothing was propagated for %s" % argv[3])
        return 0
    print(USAGE, file=sys.stderr)
    return 2


def _symlink_guard(argv):
    """#708: refuse a committed symlink under .sdlc/state or .sdlc/journey before any write."""
    import importlib.util as _u
    import pathlib as _p
    spec = _u.spec_from_file_location("_guard_state", _p.Path(__file__).resolve().parent / "state.py")
    mod = _u.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.guard_argv(argv, _p.Path(__file__).name)


if __name__ == "__main__":
    raise SystemExit(_symlink_guard(sys.argv) or main(sys.argv))
