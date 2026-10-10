#!/usr/bin/env python3
"""Ownership enforcement -- who may file work against a unit (#1479, epic #1464, story #1427).

THE FACT THIS MODULE EXISTS TO CHANGE. `feature_registry.is_authorized` was defined in #1469,
recorded in `index.json`, rendered into `<name>.md`'s managed block by #1470 -- and called by
NOTHING. Twelve goals of this epic shipped the machinery around a grant that nothing enforced, and
`docs/branching-model.md` §12 SAID so in as many words: "No shipped code path refuses anything on
the strength of it." This is the code path, and shipping it is what deleted that sentence.

TWO OWNERS, TWO DIFFERENT QUESTIONS, BOTH REQUIRED (§12):

    entry.owner                 WHO OWNS THE UNIT -- its scope, its tags, its ledger chain,
                                whether it is still open.
    entry.repos[<repo>].owner   WHO OWNS THAT BOARD -- whether the unit may have work filed
                                against it at all. One per participating repo.

THE RULE. An issue carrying `feature:<name>` may be created DIRECTLY only by the unit owner or by
the owner of the repo it is being created in. Nobody is ever blocked from raising it: anyone else
files it as `sdlc:needs-confirmation` -- which already means "inert until its owner promotes it", so
this level adds NO new machinery to the label model -- plus a ledger entry ADDRESSED TO the owner,
so the request reaches a person rather than a board they may not be watching.

WHERE THE TWO OWNERS DISAGREE, THE BOARD OWNER WINS, AND THE MORE RESTRICTIVE GATE APPLIES. Board
ownership is authority over what becomes work in somebody's QUEUE. A unit owner deciding their unit
touches another component is a scoping call and squarely theirs; a unit owner creating a PICKABLE
issue on somebody else's board is handing them work they have not agreed to -- and an agent may pick
it up and start executing before they have seen it. So `may_file` refuses the unit owner on a board
with a different owner, and that arm (`BOARD_OWNER_WINS`) is the one this module would be wrong
without.

AUTHORISATION IS PER UNIT, NOT PER ISSUE. A board owner grants once
(`repos.<repo>.authorized = true`) and from then on EVERY issue under that unit is authorised,
including the follow-ups Sigma files itself. Per-issue approval was considered and rejected
upstream: on a boundary unit it means promoting essentially every goal forever, which converts a
real guarantee into a rubber stamp -- and a gate that is always said yes to stops being read.
ABSENT MEANS FALSE. Revoking stops FUTURE issues only; nothing here ever revisits what is filed.

THIS LEVEL NEVER WRITES `authorized`, AND THAT IS DELIBERATE RATHER THAN UNFINISHED. A grant a
machine can mint is not a grant. `authorized` moves only by a human editing the registry, exactly as
§12 specifies, which is also why there is no `grant` verb on the CLI below.

TWO LAYERS, FOR #1477'S REASON AND NOT A DIFFERENT ONE. `feature_propagate` learned that one gate is
never enough here, because the layers are reached by different callers:

  - `gate_at_filing` refuses at the moment the kit OPENS an issue (`handoff.create_tracked_issue`,
    the one place that ever does). It defends a board against Sigma itself, and it is reached by
    the loop, by `handoff.py open/track`, by retro, by decompose -- every path that files.
    IT IS ASKED ABOUT THE UNIT THE ISSUE WILL DECLARE, NOT THE ONE IT INHERITED, and the two differ
    on the path that matters: a caller-supplied `feature:<name>` label survives
    `feature_stamp.reconcile_labels` while the inherited unit becomes None, so an issue is created
    declaring a unit that the inherited value does not name. Gating on the inherited value alone
    let `handoff track --label feature:<x>` file directly-actionable work under a stranger's unit
    with no check, no warning and nobody told. `feature_stamp.declared_units` is the one answer to
    "what will this issue carry", and the caller applies this gate to every unit it returns.
  - `gate_at_pick` refuses at the moment the loop PICKS an issue somebody else already filed. It
    defends a board against every issue that never went through this kit at all: a human's
    `gh issue create --label feature:x`, another tool's, another repo's automation.

Neither alone is enough. The filing gate cannot see an issue it did not file; the pick gate cannot
un-create one. Together they cover both directions of the same sentence.

THE INFERENCE, AND IT IS THE ONLY ONE IN THE MODEL. An owner is DECLARED; failing that, the first
person to work a goal on the unit becomes owner, once, and it is changed thereafter only by editing
the registry. No reassignment on inactivity, no transfer to whoever picks up next: an owner
reassignable by whoever happens to pick up work next is not an owner, it is a lease. `claim` is
therefore guarded on ABSENCE, never on staleness, and `claim_at_pick` runs it inside `feature_sync`'s
per-unit lock so two first picks cannot both be first. It fires from ONE place -- `gate_at_pick`,
on the login that opened the goal being picked -- and `_claim_here` records why that site and no
other: the name has to come from GitHub, and that is the one point on the path where one is already
in hand at no extra cost.

IT IS ASKED PER FIELD, NOT PER ENTRY, AND #1575 IS THE WHOLE OF THAT SENTENCE. The two owners above
are two different facts, so "does this unit need an owner inferred" has two answers and the gate has
to ask the one it is about. It used to reach the inference only on `may_file`'s `NO_OWNER` arm --
NEITHER owner recorded -- which is unreachable the moment either is. A PROPAGATED entry is exactly
that shape: `feature_propagate` copies the whole entry into the sibling, unit owner included, so the
sibling's actor-free pass answers `NO_ACTOR` rather than `NO_OWNER` and the sibling never records
`repos.<repo>.owner` of its own. Every issue in the sibling that the foreign owner did not open was
then held, permanently, for a person in a different repository who may not know the unit reached
this one -- a refusal with no local remedy at all. So the board half is inferred on its own
condition (`_claimable_board`), and the entry is re-read afterwards so the verdict is answered
against what actually landed. `claim`'s per-field absence guard is what makes filling one half and
not the other the ordinary case rather than a special one.

WHY THE GRANT IS ASKED THROUGH `authorized()` AND NEVER THROUGH `registry.is_authorized` DIRECTLY.
That function does an EXACT `repos.get(repo)`. #1477 then made every other slug comparison in this
subsystem CASE-INSENSITIVE (`feature_sync.same_repo`, because `Org/Repo` and `org/repo` cannot both
exist on GitHub), and made `repos` keys human-typed for the first time. Asking `is_authorized` with a
slug spelled differently from the key on disk returns False silently -- a real grant reading as
ungranted, with no error anywhere. `authorized()` resolves the key with `feature_sync.repo_key`
first, which is the same write-side rule `_record_goal` already follows. A test pins both halves.

ONE IDENTITY NAMESPACE, AND IT IS GITHUB'S. Every name compared here is a GitHub handle: `owner`
and `repos.<repo>.owner` are handles in the design's own example (`"@app-owner"`), `owners.parse`
yields handles off CODEOWNERS, and the pick gate's side of the comparison is an issue's
`author.login`. So the filing side resolves through `whoami` -- `gh api user`, or "" -- and NOT
through `ledger.actor`, whose `$USER` and `"unknown"` fallbacks are right for naming the writer of a
ledger line and wrong for every comparison in this file. See `whoami` for the three stock-install
failures that chain produced, one of which was a wrong PERMIT rather than a wrong refusal.

WHAT REMAINS OPEN, NARROWED TO WHAT IT ACTUALLY IS: a registry whose `owner` was hand-written in
some spelling other than the owner's GitHub login. That is a DECLARED value, so nothing this module
does can second-guess it, and it fails CLOSED -- the issue is held rather than filed. It is made
diagnosable instead: the flag comment prints the recorded owners and the observed author side by
side and says that correcting the registry is the fix, rather than promoting each issue forever.

THE REMEDY A REFUSAL NAMES HAS TO BE ONE THE READER CAN PERFORM (#1569), AND FOR TWO CYCLES IT WAS
NOT. Every channel here used to offer `/sigma-promote` as a way to clear the hold. It is not one:
that command swaps two labels, and `gate_at_pick` reads no label -- it recomputes from the registry
and the issue's AUTHOR, neither of which a promotion touches. So the goal came back onto the board
and the very next pick set it aside again, which is the loop-wedging shape a refusal must never
have. `feature_propagate`'s sibling gate had the ordering right from the start ("when they accept,
`<repo>` joins `repos` and this goal can be promoted") and this module said the opposite. It now
says the same thing: the two remedies are REGISTRY EDITS -- `repos.<repo>.authorized = true`, or
correcting an owner spelled two ways -- and `/sigma-promote` is the gesture that returns the goal to
the board AFTER one of them, never instead of one. `promote._refusal` refuses the promotion that
would be undone, through `would_hold`, so the no-op is caught before it is performed rather than
discovered a pick later.

AND THE SECOND CYCLE IS NOT SILENT. `_flag` is idempotent against the issue's own timeline and the
ledger note is gated on it, which was right for bounding repeated notes and wrong for this: the
operator did what the comment said, the goal was demoted again, and neither channel said a word.
Arriving at `_flag` with the marker already present is EVIDENCE OF A PROMOTION rather than of a
repeated pick -- see that function -- so it is announced as one, in its own words, once per
demotion.

THE COST, MEASURED IN CALLS RATHER THAN ASSERTED TO BE SMALL. The pick gate reads an author only
when the answer depends on one: a granted unit, an unrecorded unit and an unadopted project each
settle at zero reads, and a unit with no owner pays exactly one read ever, because the next pick
finds the owner that read established. `claim_at_pick` adds one shard read and, once per unit, one
write. #1575's board-half inference adds the same again, at most once per unit PER REPO, plus one
re-read of the registry on the pick that lands it -- after which `board_owner` answers and neither
is paid. On the FILING side `whoami` is one `gh api user`, cached per runner, so a pass that files
several issues pays it once. Nothing here touches `work.start`'s path, which still reads the issue
exactly once (#1467) -- a requirement an identity call placed there would have broken.

MODULE SHAPE follows `feature_propagate.py`: zero third-party dependencies, module-level constants,
every public entry point total (it reports rather than raises), loaded by siblings via
`_load("feature_owner")`.
"""
import collections
import importlib.util
import json
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


registry = _load("feature_registry")     # the store: read, normalise_entry, is_authorized
sync = _load("feature_sync")             # same_repo/repo_key/repo_slug, and the per-unit lock
legacy = _load("legacy")                 # #239: the watermark under the previous name
gate_hold = _load("gate_hold")           # #1005: the park write, the surface probe, the shared predicate
ledger = _load("ledger")                 # team record (config-gated, default OFF; fail-open)

#: THE VERDICT VOCABULARY. Each value is a different FACT about why the answer is what it is, not a
#: severity: a caller that only wanted a boolean has `Verdict.allowed`, and the reason exists so the
#: ledger note, the issue comment and the CLI can each say the true thing rather than a generic one.
NO_ENTRY = "no-entry"                 # the registry records nothing under this unit yet
AUTHORIZED = "authorized"             # `repos.<repo>.authorized is True` -- the per-unit grant
NO_OWNER = "no-owner"                 # neither owner is recorded; there is nobody to refuse for
NO_ACTOR = "no-actor"                 # the creator could not be named, so nobody may be refused
NO_SLUG = "no-slug"                   # this checkout's own `owner/name` could not be resolved
BOARD_OWNER = "board-owner"           # the actor owns the board the issue lands on
UNIT_OWNER = "unit-owner"             # the actor owns the unit, and no board owner disagrees
BOARD_OWNER_WINS = "board-owner-wins"  # the actor owns the UNIT and somebody else owns the BOARD
NOT_AN_OWNER = "not-an-owner"         # the actor owns neither
REASONS = (NO_ENTRY, AUTHORIZED, NO_OWNER, NO_ACTOR, NO_SLUG, BOARD_OWNER, UNIT_OWNER,
           BOARD_OWNER_WINS, NOT_AN_OWNER)

#: The two that REFUSE. Kept as a tuple rather than re-derived from `allowed` at each site, for
#: `feature_sync._WORDING`'s reason: a table that has to stay total is one a reader can check.
REFUSING = (BOARD_OWNER_WINS, NOT_AN_OWNER)

#: The gate-only outcomes -- the three ways the question is not even asked.
NOT_ADOPTED = "not-adopted"           # no `.sdlc/features/`: this project has not adopted the model
NO_UNIT = "no-unit"                   # the goal declares none, so no unit's board is at stake
NO_SURFACE = "no-surface"             # this backlog source cannot mark an issue needs-confirmation
FAILED = "failed"                     # the check did not run; nothing here is claimed
GATE_OUTCOMES = REASONS + (NOT_ADOPTED, NO_UNIT, NO_SURFACE, FAILED)

#: `allowed` answers the question. `owner` is WHO TO ASK when it is False -- the board owner where
#: there is one, because the board owner wins, and the unit owner otherwise. It is populated on the
#: allowed arms too, so a caller reporting a grant can still name who granted it.
#:
#: `repo` IS THE ONE THE VERDICT WAS ANSWERED ABOUT, carried rather than re-derived. Every channel
#: that renders a refusal names the board, and a caller that had to resolve the slug again would
#: either re-run `git remote get-url` (which `tell_at_filing` did) or, worse, resolve it through a
#: different runner and get a different answer. It defaults to None so the arms that answer before a
#: repo is known -- and the outer guards, which answer without one at all -- need not invent one.
Verdict = collections.namedtuple("Verdict", "allowed reason owner repo", defaults=(None,))

#: `proceed` False means REFUSE THE PICK. `marked` says whether the issue actually became inert --
#: separate fields, and separate for `feature_propagate.Gate`'s reason: a write that did not land
#: must never be reported as a state that exists.
Gate = collections.namedtuple("Gate", "proceed outcome unit repo marked")

#: The idempotency marker for the flag comment. An HTML comment, so it is invisible in the rendered
#: issue and is not something a human reproduces by accident. DISTINCT from
#: `feature_propagate.SCOPE_MARKER` and from `feature_labels`' two, for the reason they are all
#: distinct from each other: one shared marker would silence the second problem an issue develops.
OWNER_MARKER = "<!-- sigma:feature-ownership -->"

#: WHAT `_flag` DID, and it is three values rather than a bool because the second announcement is a
#: different FACT from the first (#1569): the first says a hold exists, the second says a hold
#: somebody has already acted on is still there. The ledger note's wording is chosen from this, so a
#: caller cannot say "held" twice in a row and call the second one news.
FLAG_NONE = ""                        # nothing was posted this pass -- the timeline could not be
                                      # read, or the comment write failed
FLAG_FIRST = "first"                  # the hold was announced for the first time
FLAG_AGAIN = "again"                  # announced again, after the goal was promoted back onto the
                                      # board and this gate reached the same answer

#: The methods `gate_at_pick` needs before it may refuse anything. `fetch_author` is the one this
#: goal adds; the other three are `feature_propagate._has_surface`'s, unchanged, because the refusal
#: is the same refusal. `LocalSource` has none of them and no repository label namespace at all.
_SURFACE = ("fetch_author", "note", "fetch_comments_strict")

#: How far a failure's own text is quoted. `ledger._sanitize_free_text` caps `why` at
#: `FREE_TEXT_CAP` from the END, so identifying facts are placed early and this bounds the rest.
_DETAIL_CHARS = 120


#: `ledger.actor`'s LAST RESORT, and the reason `whoami` exists rather than a call to it. When gh
#: cannot answer and the shell has no `$USER`, that function returns this literal string, because
#: its job is to name the WRITER of a ledger line and an unattributed line is worse than a guessed
#: one. Here the same guess is not a weaker answer, it is a WRONG one: it is compared against an
#: issue's `author.login`, and two accounts whose gh resolution both failed would become each other.
#: Pinned against `ledger.actor`'s real behaviour by a test rather than by reading its source.
UNRESOLVED_ACTOR = "unknown"

#: `whoami`'s per-runner cache, `ledger._ACTOR_CACHE`'s shape and its reason: the answer cannot
#: change inside one process, and the call is a subprocess.
_WHOAMI = {}


def reset_whoami_cache():
    """Tests, and a long-lived process whose auth changed -- `ledger.reset_actor_cache`'s twin."""
    _WHOAMI.clear()


def _run_gh(argv):
    """The one gh call this module makes, in `ledger._run_gh`'s own `(argv) -> stdout` shape so a
    caller's injected runner works unchanged. Local import for `feature_sync._run`'s reason: the
    injected runner is the real path and this is the fallback for a direct CLI caller."""
    import subprocess
    proc = subprocess.run(["gh", *[str(a) for a in argv]], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "").strip() or "gh exited %s" % proc.returncode)
    return (proc.stdout or "").strip()


def whoami(run=None):
    """-> the GITHUB LOGIN this pass acts as, or "" when that could not be established.

    DELIBERATELY NOT `ledger.actor`, AND THE DIFFERENCE IS THIS WHOLE FUNCTION'S REASON. That
    function resolves `ledger.actor` -> `gh api user` -> `$USER`/`$LOGNAME` -> the literal
    `"unknown"`, and every one of those fallbacks is right for what it does: name the writer of a
    ledger line, where a guess beats an absence. Every one of them is WRONG here, because this name
    is compared against an issue's `author.login` and against a registry `owner` field, and a guess
    in that comparison is not a weaker answer -- it is a different person. Measured, on a STOCK
    install with a transient gh failure and no override:

      - `$USER` ("alice-laptop") refuses the owner's own filing as `NOT_AN_OWNER`, and -- worse --
        `claim` would write it as the unit's permanent owner, after which every issue that person
        really opens is held at every pick, fixable only by a hand edit nobody knows to make;
      - `"unknown"` is a NAMEABLE string, so `same_actor("unknown", "unknown")` is True and any two
        accounts whose gh resolution failed become each other. That is a wrong PERMIT, and
        `actor_key`'s "two people nobody can name are not the same person" invariant -- correct, and
        pinned by its own test -- was simply defeated one layer up.

    So: gh, or nothing. "" lands on `may_file`'s `NO_ACTOR` arm, which PROCEEDS -- a gate that
    cannot say who must not refuse on their behalf -- and `claim` records nothing rather than a
    name it cannot vouch for.

    IT TAKES NO `config`, AND THE OMISSION IS THE POINT rather than an oversight: there is no
    configured value it could consult that would be more true than the authenticated account -- the
    account that will AUTHOR the issue is the one `gh` is authenticated as, whatever a config file
    calls the writer -- and a parameter it accepted but ignored would read as though there were.

    NEVER RAISES. Cached per runner, so a filing path that asks repeatedly pays one subprocess."""
    key = id(run) if run is not None else "default"
    if key in _WHOAMI:
        return _WHOAMI[key]
    try:
        got = (run or _run_gh)(["api", "user", "-q", ".login"])
    except Exception as exc:              # noqa: BLE001 - an unresolved identity is not a refusal
        _note("sigma: features: could not resolve which GitHub account this is (%s) -- the "
              "ownership check proceeds and no owner is inferred, because a name that did not come "
              "from GitHub cannot be compared with one that did.\n" % _flat(exc))
        got = ""
    got = got.strip() if isinstance(got, str) else ""
    _WHOAMI[key] = got
    return got


def _note(message):
    """One stderr line, never an exception -- the shape every module on this path holds, for the
    same reason: a diagnostic must never be the thing that breaks a pick."""
    try:
        sys.stderr.write(message)
    except Exception:                     # noqa: BLE001 - a diagnostic must never break a pick
        pass


def _flat(value):
    return " ".join(str(value).split())[:_DETAIL_CHARS]


# --------------------------------------------------------------------------- who is who


def actor_key(value):
    """The comparison key for one person's name, or "" when there is no name in it.

    TWO NORMALISATIONS, EACH MEASURED AGAINST A REAL SPELLING IN THIS REPO RATHER THAN GUESSED AT:

      - THE LEADING `@` IS DROPPED. `docs/branching-model.md`'s own example entry writes
        `"owner": "@unit-owner"`, and `owners.parse` strips the `@` off every CODEOWNERS line
        (`tests/test_handoff.py` asserts it), so the same person arrives spelled both ways from two
        stores this module reads side by side. Comparing them raw makes one person two.
      - THE CASE IS IGNORED. GitHub logins are case-insensitively unique, exactly as repo slugs are
        (`feature_sync.same_repo`) and unit names are (`feature_registry._read_unit_file`); this
        subsystem already rules that two casings were never two things, and a third opinion here
        would be the drift those two comments exist to refuse.

    A non-string, or a string with nothing in it, is "" -- which `same_actor` then refuses to match
    against anything, INCLUDING another "". Two people nobody can name are not the same person.

    AND SO IS THE LITERAL `"unknown"`, which is the belt to `whoami`'s braces. That string is
    `ledger.actor`'s last-resort stand-in for an absent name; `whoami` exists so it never reaches
    this module through the front door, but it can still arrive from a hand-edited registry, from a
    ledger-derived value some future caller passes, or from a version of `ledger.actor` this file
    does not control. Refusing it HERE makes the invariant above true of the sentinel too, in the
    one place both sides of every comparison pass through.

    THE COST, STATED: a real GitHub account literally named `unknown` could never own a unit or be
    matched as one. That is the trade, taken knowingly -- the collision it prevents is a wrong
    PERMIT between two unrelated accounts, and the one it costs is a refusal that fails open
    (`NO_ACTOR`, `NO_OWNER`) and is fixable by declaring `owner` explicitly."""
    if not isinstance(value, str):
        return ""
    got = value.strip()
    if got.startswith("@"):
        got = got[1:].strip()
    got = got.lower()
    return "" if got == UNRESOLVED_ACTOR else got


def same_actor(a, b):
    """Are these two names the same person? False whenever either is unnameable -- see `actor_key`
    for why two blanks must not match."""
    key = actor_key(a)
    return bool(key) and key == actor_key(b)


def _named(value):
    """The name AS WRITTEN when there is one, else None. The write-side half of `actor_key`: having
    decided what counts as a name, a write records the author's own spelling rather than the
    lower-cased key -- `feature_sync.repo_key`'s ruling, one axis over.

    IT REFUSES EXACTLY WHAT `actor_key` REFUSES, and delegates rather than restating: a value this
    module will not COMPARE is a value it must not RECORD either, or the registry ends up holding a
    name that can never match anything. That covers the `"unknown"` sentinel in both directions --
    it is neither inferred as an owner nor read back as one."""
    if not isinstance(value, str) or not actor_key(value):
        return None
    return value.strip()


def unit_owner(entry):
    """Who owns the UNIT, or None. Tolerates a malformed entry for `is_authorized`'s reason: an
    access check that raises is one somebody wraps in a bare `except` at the call site."""
    return _named(entry.get("owner")) if isinstance(entry, dict) else None


def board_owner(entry, repo):
    """Who owns the BOARD this issue lands on, or None.

    Resolved through `feature_sync.repo_key`, never by an exact `repos[repo]`: see the module
    docstring. A repo the unit does not name has no board owner HERE -- that case is #1477's scope
    expansion, and answering it twice under two names would tell an owner about a decision nobody
    was trying to take."""
    if not isinstance(entry, dict):
        return None
    repos = entry.get("repos")
    if not isinstance(repos, dict):
        return None
    one = repos.get(sync.repo_key(repos, repo))
    return _named(one.get("owner")) if isinstance(one, dict) else None


def authorized(entry, repo):
    """Has this repo's board owner granted the unit? -> a real bool.

    `feature_registry.is_authorized` IS the rule -- `authorized is True`, so the string "false"
    cannot grant under a truthiness test and neither can "true", `1` or `[]`. This function adds
    exactly one thing to it and nothing else: the KEY it is asked under. See the module docstring
    for the silently-inert lookup that omission produces."""
    if not isinstance(entry, dict):
        return False
    repos = entry.get("repos")
    return registry.is_authorized(entry, sync.repo_key(repos, repo))


# --------------------------------------------------------------------------- the policy


def may_file(entry, repo, actor):
    """-> `Verdict`. May `actor` create an issue carrying this unit's label, directly, in `repo`?

    NEVER RAISES, on any input, for `feature_registry.is_authorized`'s stated reason: an access
    check that raises is an access check somebody wraps in a bare `except`, and a swallowed
    exception defaults to whatever that call site felt like.

    THE ARMS ARE ORDERED SO THE CHEAP, TOTAL ANSWERS COME FIRST, and that ordering is load-bearing
    rather than tidy. `gate_at_pick` calls this with `actor=None` BEFORE it reads the issue's author
    over the network: any arm above `NO_ACTOR` settles the question without a name, so a granted
    unit and an unowned unit each cost zero reads. The `NO_ACTOR` arm is what tells the caller the
    answer genuinely depends on who is asking.

      NO_ENTRY          the unit does not exist yet, so this act is what CREATES it. There is no
                        owner to refuse on behalf of, and refusing here would make the first issue
                        of every unit inert -- the one issue that establishes the unit at all.
      NO_SLUG           this checkout's own `owner/name` could not be resolved, so there is no BOARD
                        to ask about -- neither the grant nor the board owner can be read under a
                        repo nobody could name. Falling back to the unit owner alone would refuse a
                        pick over OUR configuration gap rather than over anybody's authority, which
                        is the one refusal nobody can act on. `feature_sync` draws the identical
                        line for its own `NO_REPO`: report the gap, record nothing, hold nothing.
      AUTHORIZED        the grant. Per unit, so it covers every issue under it forever, follow-ups
                        included, and it is checked before any owner because it is exactly the
                        decision that makes ownership stop mattering for this repo.
      NO_OWNER          nobody is recorded either way. Enforcing an owner nobody named would be this
                        module inventing an authority.
      NO_ACTOR          the creator cannot be named. A gate that cannot say WHO must not refuse.
      BOARD_OWNER       the actor owns this board. The plainest allowed case, and it is checked
                        before the unit owner so the two-owner arms below cannot reach a person who
                        is both.
      UNIT_OWNER        the actor owns the unit AND no board owner disagrees.
      BOARD_OWNER_WINS  the actor owns the unit and somebody ELSE owns the board. §12.2's ruling,
                        and the arm the whole level turns on -- see the module docstring.
      NOT_AN_OWNER      neither.

    `owner` is the person to ASK on a refusal: the board owner where there is one (they are the one
    who wins, and the only one who can write the grant), the unit owner otherwise."""
    try:
        return _may_file(entry, repo, actor)
    except Exception as exc:              # noqa: BLE001 - an access check must never raise
        _note("sigma: features: the ownership check did not run (%s); the filing is treated as "
              "permitted, because a gate that cannot answer must not cost somebody the finding it "
              "could not rule on.\n" % _flat(exc))
        return Verdict(True, FAILED, None)


def _may_file(entry, repo, actor):
    if not isinstance(entry, dict) or not entry:
        return Verdict(True, NO_ENTRY, None)
    if not (isinstance(repo, str) and repo.strip()):
        return Verdict(True, NO_SLUG, unit_owner(entry), None)
    board, unit = board_owner(entry, repo), unit_owner(entry)
    ask = board or unit
    if authorized(entry, repo):
        return Verdict(True, AUTHORIZED, ask, repo)
    if not ask:
        return Verdict(True, NO_OWNER, None, repo)
    if not actor_key(actor):
        return Verdict(True, NO_ACTOR, ask, repo)
    if same_actor(actor, board):
        return Verdict(True, BOARD_OWNER, board, repo)
    if same_actor(actor, unit):
        # THE RESTRICTIVE GATE, AND THE ARM THE WHOLE LEVEL TURNS ON. The actor owns the unit; a
        # board owner still standing at this line is NECESSARILY somebody else, because the arm
        # above already returned for the case where the two are the same person. So `if board` is
        # the whole condition, and it is deliberately not spelled `if board and not
        # same_actor(actor, board)` -- a second, always-true comparison would read as though the
        # question were still open here.
        if board:
            return Verdict(False, BOARD_OWNER_WINS, board, repo)
        return Verdict(True, UNIT_OWNER, unit, repo)
    return Verdict(False, NOT_AN_OWNER, ask, repo)


# --------------------------------------------------------------------------- the inference


def claim(entry, repo, actor):
    """The first person to work a goal on this unit becomes its owner. -> what was set.

    Mutates `entry` in place and returns the field paths it filled, `[]` when it filled none --
    which is the ordinary case, on every pick after the first.

    AT MOST ONCE, AND THE GUARD IS ABSENCE. Every branch here is `if there is nothing recorded`,
    never `if what is recorded looks stale`: an owner reassignable by whoever happens to pick up
    work next is not an owner, it is a lease. A declared owner is therefore never overwritten, and
    neither is an inferred one -- once it is written, the registry is the only thing that can change
    it, by hand.

    IT NEVER CREATES A `repos` ENTRY. Claiming a board the unit does not name would widen the unit
    into a repo its owner never agreed to, through the back door #1477 closed at the front; the repo
    half is filled only where the unit already names that repo. `sync.repo_key` picks the key that
    is already there, so a second casing is never minted beside it.

    BOTH HALVES ARE FILLED, and by the same person, because at this moment they are the same fact:
    the first person to work the unit is the only evidence either field has. They diverge later
    exactly as any hand edit makes them diverge, and `may_file` is written for that divergence.

    THEY ALSO ARRIVE DIVERGED, which is #1575's case and the reason the two guards are separate `if`
    statements rather than one. A propagated entry names the unit's owner (inferred in the repo it
    came from) and no owner for the board it just landed on, so this function is called to fill the
    SECOND field only -- and it does, because each guard asks about its own field. The caller that
    reaches it in that state is `_claimable_board`'s arm in `gate_at_pick`, not the `NO_OWNER` one."""
    who = _named(actor)
    if who is None or not isinstance(entry, dict):
        return []
    filled = []
    if not _named(entry.get("owner")):
        entry["owner"] = who
        filled.append("owner")
    repos = entry.get("repos")
    if isinstance(repos, dict):
        key = sync.repo_key(repos, repo)
        one = repos.get(key)
        if isinstance(one, dict) and not _named(one.get("owner")):
            one["owner"] = who
            filled.append("repos.%s.owner" % key)
    return filled


def claim_at_pick(sdlc_dir, goal, unit, repo, actor):
    """Record the inferred owner for a unit that has none. -> a report. NEVER RAISES.

    `actor` IS REQUIRED AND IS NEVER RESOLVED HERE. The name this writes is compared against issue
    authors forever after, because inference happens once -- so it takes one the caller obtained
    FROM GITHUB and guesses nothing of its own. `_claim_here` is that caller, and it passes the
    login that opened the goal being picked.

    RUNS UNDER THE UNIT'S OWN LOCK, via `feature_sync.amend`, which is what keeps two simultaneous
    first picks from both being first. Deliberately a SEPARATE `amend` from the one that records the
    goal rather than folded into `_record_goal`: that function is #1477's, its refusal is about a
    different question, and a module that owns a policy should own the write that applies it. The
    cost is one shard read per pick and one WRITE per unit ever -- `amend` writes nothing when
    nothing changed, and after the first pick nothing changes.

    `repo` IS HANDED IN, ALREADY RESOLVED -- by `gate_at_pick`'s own `repo_slug` call, one frame up,
    which is also the slug `may_file` was answered about. It is deliberately NOT re-derived here: a
    second resolution is a second chance to disagree with the one the refusal was decided under.

    (This paragraph used to credit `sync_at_pick` for that slug, from when the claim lived on
    `work.start`'s path. It does not run before this any more -- the caller moved and the sentence
    did not. Recorded rather than quietly swapped, because a provenance claim that has drifted is
    worse than none: it tells a reader to go looking in the wrong place.)"""
    report = {"goal": str(goal), "unit": unit, "repo": repo, "claimed": [], "actor": None,
              "landed": False, "why": ""}
    try:
        return _claim_at_pick(sdlc_dir, goal, unit, repo, actor, report)
    except Exception as exc:              # noqa: BLE001 - never break a start over a record
        report["why"] = _flat(exc)
        _note("sigma: features: the ownership claim for %s did not run (%s); the goal is "
              "recorded and the unit's owner is whatever it already was.\n" % (goal, report["why"]))
        return report


def _needs_owner(entry, repo):
    """Is there an owner field on this entry that inference could still fill? A cheap, lock-free
    early-out, and ONLY that: the authoritative "at most once" guard is `claim`'s own, inside the
    lock. A racer that reads Yes here and loses the race then writes nothing, because by the time it
    holds the lock `claim` sees an owner. What this buys is that the common case -- every pick after
    a unit's first -- takes no lock and writes nothing."""
    if not isinstance(entry, dict):
        return False
    if not _named(entry.get("owner")):
        return True
    repos = entry.get("repos")
    if not isinstance(repos, dict):
        return False
    one = repos.get(sync.repo_key(repos, repo))
    return isinstance(one, dict) and not _named(one.get("owner"))


def _claimable_board(entry, repo):
    """The `repos` key whose `owner` a pick would fill from the issue's author, or None. (#1575)

    IT IS `claim`'S OWN REPO-HALF CONDITION, ASKED WITHOUT WRITING, and it is spelled out here for
    the reason `feature_sync.is_scope_expansion` is: two callers need the same question answered,
    and the alternative is two spellings of it. `gate_at_pick` uses it to decide whether the board
    half can still be inferred before it refuses anybody; `would_hold` uses it so `promote` does not
    refuse a promotion that the very next pick would allow.

    IT ANSWERS None FOR A REPO THE UNIT DOES NOT LIST, and that falls out of `repo_key` rather than
    being asserted here: the key it resolves is absent from `repos`, so there is no entry whose
    owner could be filled. That is the same line `claim` draws and the same one `_claim_here` draws
    through `is_scope_expansion` -- claiming a board the unit never named would widen the unit
    through the back door #1477 closed at the front.

    A `repos` BLOCK THAT IS EMPTY OR MISSING IS ALSO None, deliberately: an empty block is the
    ordinary state of a freshly recorded unit, and there is no board entry there to name an owner
    on. Nothing is created."""
    if not isinstance(entry, dict):
        return None
    repos = entry.get("repos")
    if not isinstance(repos, dict):
        return None
    key = sync.repo_key(repos, repo)
    one = repos.get(key)
    return key if isinstance(one, dict) and not _named(one.get("owner")) else None


def _claim_at_pick(sdlc_dir, goal, unit, repo, actor, report):
    who = _named(actor)
    report["actor"] = who
    if who is None or not unit or not isinstance(repo, str):
        return report
    features_dir = registry.registry_dir(sdlc_dir)
    if not features_dir.is_dir():
        return report
    if not _needs_owner(registry.read(features_dir).get(unit), repo):
        return report
    filled = []

    def _claim(entry, _filled=filled):
        del _filled[:]                    # `amend` may retry, and one pass claims it once
        _filled.extend(claim(entry, repo, who))

    amended = sync.amend(sdlc_dir, unit, _claim)
    report["claimed"] = list(filled)
    report["landed"] = bool(amended["written"])
    if filled:
        _note("sigma: features: unit %s had no recorded owner, so %s -- the first person to "
              "work a goal on it -- is now its owner (%s). Change it by editing the registry.\n"
              % (unit, who, ", ".join(filled)))
    return report


# --------------------------------------------------------------------------- the ledger edge


def _tell(sdlc_dir, goal, unit, why, to=None):
    """One stderr line always, one ledger entry when the ledger is on -- the same shape and the same
    reasoning as `feature_propagate._tell` and `feature_sync._tell`, including `kind="note"` rather
    than `handoff`: `backlog_check._ledger_signals` reads a hand-off as a real block, so raising an
    ownership ask that way would PARK the very goal that raised it."""
    _note("sigma: features: %s\n" % why)
    fields = {"why": why, "area": registry.REGISTRY_DIRNAME,
              "ref": "%s/%s" % (registry.REGISTRY_DIRNAME, unit)}
    # THE ADDRESS IS WRITTEN IN THE LEDGER'S OWN NAMESPACE, NOT THE REGISTRY'S. `verdict.owner` is
    # the registry's spelling and the model's example entries carry a leading `@`, while every
    # consumer of `to` -- `ledger.addressed_to`/`mine`, `autowatch._find_candidate`,
    # `watch_classify`, `triage` -- compares against a bare login. Measured before any of this
    # existed: a note written `to="@here-owner"` was retrieved by `addressed_to(entries,
    # "here-owner")` ZERO times, so the ask looked routed and reached nobody.
    #
    # THIS LINE IS NO LONGER WHAT MAKES THAT TRUE, AND THE COMMENT THAT SAID SO IS GONE (#1637).
    # It claimed the other registry-owner writers still wrote raw `@handle`s and that normalising
    # here bought THIS module's entries a delivery the others did not get. #1574 moved
    # canonicalisation to a chokepoint in `ledger.append`, so a writer can no longer land a raw
    # handle in `to` at all. Measured, all five `_tell` sites writing `to="@Here-Owner"`:
    #
    #     feature_doc / feature_sync / feature_propagate / feature_owner / unit_completion
    #         -> to='here-owner' on all five, addressed_to(entries, "here-owner") -> 5 of 5
    #
    # WHY THE CALL STAYS ANYWAY: `address_key` is idempotent, so this is belt-and-braces rather
    # than a second rule -- and it is the same function `append` uses, not a copy, so the two
    # cannot disagree. `ledger.address_key` and `ledger.addressed_to` carry the full account,
    # including why normalising only the write side would once have made things worse.
    addressed = ledger.address_key(to)
    if addressed:
        fields["to"] = addressed
    ledger.safe_append(sdlc_dir, "note", goal, **fields)


def refusal_clause(verdict, unit, repo, subject, quote=str):
    """THE ONE SENTENCE THAT SAYS WHY A REFUSAL HAPPENED. Every channel renders this and none of
    them branches for itself.

    IT IS A FUNCTION BECAUSE COPYING THE BRANCH HAS NOW FAILED TWICE. `_why` (the ledger note and
    stderr) branched on `BOARD_OWNER_WINS` from the start; `_text` (the issue comment) did not, and
    told a unit's own owner they "own neither" three lines above a table naming them the unit owner.
    That was fixed by extracting `_because` -- and the fix left a THIRD channel, the warning
    `handoff` puts in the filing report, still saying the same false thing to the operator running
    the session. Three renderings of one fact is three places to forget; one function called three
    times is none. A fourth channel can only be added by calling it.

    THE THREE DIFFER ONLY IN SUBJECT AND MARKUP, which is why one function can serve them: the
    ledger says "dana", the issue comment says "`dana` — the account that opened it —", the report
    says "this account". `quote` is `_code` for the markdown channel and `str` for the plain-text
    ones, so a unit name never arrives unquoted in a comment or backticked in a console line."""
    if verdict.reason == BOARD_OWNER_WINS:
        return ("%s owns unit %s, but %s's board belongs to somebody else, and where the two "
                "disagree the board owner decides what becomes work in their queue"
                % (subject, quote(unit), quote(repo)))
    return "%s owns neither unit %s nor %s's board" % (subject, quote(unit), quote(repo))


def _why(goal, unit, repo, verdict, who, issue=None, again=False):
    """The sentence both gates put in front of an owner.

    THE FACTS LEAD, AND SO DOES THE REMEDY, because `ledger._sanitize_free_text` caps `why` at
    `FREE_TEXT_CAP` (200) from the END. The goal, the unit and the repo can never be truncated away
    -- and neither, now, can the registry edit that clears the hold, which is why it is stated
    BEFORE the diagnosis rather than after it. Measured: the previous ordering put the remedy last
    and a real note ran to ~247 characters, so the operator's only actionable sentence was the one
    the cap ate. The diagnosis is the part that may be clipped here, and it is carried in full by
    the flag comment on the issue, which nothing truncates.

    IT DOES NOT OFFER `/sigma-promote` AS A REMEDY, AND SAYING SO OUT LOUD IS THE POINT (#1569). That
    command changes two labels; `gate_at_pick` reads none, so the goal returns to the board and the
    next pick reaches the same answer. Naming it here sent the operator round a loop with no exit,
    which is the failure this text now names rather than causes.

    `again` IS A DIFFERENT FACT, NOT A LOUDER ONE. It means the goal was promoted back and set aside
    a second time -- see `_flag` for why arriving there twice is evidence of a promotion -- and an
    owner reading "still held" for the second time needs to know it is the second time.

    `who` IS NAMED, AND IT IS THE FIELD THAT MAKES A WRONG REFUSAL DIAGNOSABLE. The identity this
    module compares is a GitHub handle on both sides (see the module docstring); a project whose
    recorded owner is spelled some other way would hold every issue its real account opens, and
    without the observed name in the note that reads as "Sigma stopped" rather than as "these
    two strings are the same person spelled differently"."""
    what = ("issue %s (filed from goal %s)" % (issue, goal)) if issue else ("goal %s" % goal)
    because = refusal_clause(verdict, unit, repo, who or "its creator")
    state = ("carries unit %s and was set aside AGAIN after a promotion" % unit) if again else \
            ("carries unit %s and is inert" % unit)
    return ("%s %s: set repos.%s.authorized = true or correct its owner; /sigma-promote will not. "
            "Why: %s" % (what, state, repo, because))


# --------------------------------------------------------------------------- the filing gate


def gate_at_filing(sdlc_dir, config, goal, unit, actor=None, gh_run=None, run=None, cwd=None,
                   remote=None):
    """-> `Verdict`. May the account running this pass file an issue under `unit`, directly?

    TWO RUNNERS, TWO PARAMETERS, AND NEITHER EVER DEFAULTS TO THE OTHER. This function needs a GIT
    runner (`repo_slug`, contract `(cwd, argv) -> stdout`) and a GH runner (`whoami`, contract
    `(argv) -> stdout`). An earlier revision took ONE `run` and handed it to both, reasoning that a
    caller's injected runner should not be bypassed -- and it was not bypassed, it was MISAPPLIED.
    Measured with `discovery.github.repo` unset, which `repo_slug`'s own docstring calls the real
    install path rather than a theoretical one: a 1-arity gh runner made `repo_slug` raise on
    arity, so its git fallback was structurally dead and the verdict came back `no-slug`; a 2-arity
    git runner made `whoami` raise, and the verdict came back `no-actor`. Both of those ALLOW, so
    whichever runner a caller injected, one consumer broke silently and the gate stopped gating.
    Splitting them makes that unrepresentable rather than merely unlikely: there is no single value
    a caller can pass that reaches the wrong consumer. `handoff` has only a gh runner and passes
    only `gh_run`, which leaves `repo_slug` on `feature_sync._run` exactly as it was before either
    revision -- so the guarantee the first version stated in a comment is now held by the shape.

    CALLED BY `handoff.create_tracked_issue`, the one place the kit ever opens an issue on its own
    behalf, so every filing path inherits this without any caller remembering to -- the identical
    argument `FOLLOWUP_LABEL` and #1471's unit inheritance are both settled there for.

    IT NEVER STOPS A FILING. A refusal changes the LABELS, never whether the issue exists: nobody is
    ever blocked from raising it. That is the whole shape of this level, and it is why this function
    returns a verdict rather than raising -- the caller withholds `sdlc:goal` and adds
    `sdlc:needs-confirmation`, which is what "inert until promoted" already means everywhere else.

    NEVER RAISES; every internal failure resolves to `FAILED`, which is ALLOWED. A gate that cannot
    answer must not cost a finding, and the finding is the thing that would be lost."""
    try:
        features_dir = registry.registry_dir(sdlc_dir)
        if not unit or not features_dir.is_dir():
            return Verdict(True, NO_UNIT if not unit else NOT_ADOPTED, None)
        entry = registry.read(features_dir).get(unit)
        repo = sync.repo_slug(config, run or sync._run,
                              str(cwd or pathlib.Path(sdlc_dir).parent),
                              remote or sync.DEFAULT_REMOTE)
        # `whoami`, NEVER `ledger.actor` -- see that function for the three stock-install failures
        # the fallback chain produces. "" here lands on `NO_ACTOR`, which proceeds.
        return may_file(entry, repo, _named(actor) or whoami(gh_run))
    except Exception as exc:              # noqa: BLE001 - never break a filing over a check
        _note("sigma: features: the ownership check for a filing from %s did not run (%s); the "
              "issue is filed exactly as it would have been before this level existed.\n"
              % (goal, _flat(exc)))
        return Verdict(True, FAILED, None)


def tell_at_filing(sdlc_dir, goal, unit, verdict, issue=None, actor=None):
    """Put a refused filing in front of the owner it was refused for. -> True iff a note was made.

    SEPARATE FROM `gate_at_filing`, AND CALLED AFTER THE ISSUE EXISTS, so the note can name it. The
    gate has to run BEFORE the create (it decides the labels the create is given); the note is worth
    more once there is a number in it, and an owner who is asked to promote something needs to know
    what. Splitting them is the smaller cost.

    THE REPO COMES OFF THE VERDICT rather than being resolved again. It used to re-run `repo_slug`
    -- a second `git remote get-url` for a fact the gate had already established, and a second place
    a runner contract could be got wrong. A verdict that names its own repo removes both.

    NEVER RAISES, and writes nothing on an allowed verdict -- so a caller may call it unconditionally
    and the reading of the verdict stays in one place."""
    try:
        if verdict is None or verdict.allowed:
            return False
        _tell(sdlc_dir, goal, unit,
              _why(goal, unit, verdict.repo, verdict, actor, issue), to=verdict.owner)
        return True
    except Exception as exc:              # noqa: BLE001 - never break a filing over a note
        _note("sigma: features: could not tell %s that a filing under unit %s is waiting on "
              "them (%s); the issue is inert either way.\n" % (verdict.owner, unit, _flat(exc)))
        return False


# --------------------------------------------------------------------------- the pick gate


def _has_surface(source, config=None):
    """The reads the gate needs plus a write it can hold with: `park` when triage is on, else (or
    for a source that cannot park) the old label write. See `gate_hold.surface_mode`."""
    return (all(callable(getattr(source, m, None)) for m in _SURFACE)
            and gate_hold.surface_mode(source, config, ()) is not None)


def _author(source, goal):
    """The login that opened `goal`, or None. Never raises -- an unanswered question is not an
    answer, and both readers of this treat None as "carry on"."""
    try:
        return source.fetch_author(goal) or None
    except Exception as exc:              # noqa: BLE001 - a read we cannot trust decides nothing
        _note("sigma: features: could not read who opened #%s (%s) -- the pick carries on, "
              "because a gate that cannot name the author must not refuse on their behalf\n"
              % (goal, _flat(exc)))
        return None


def _claim_here(sdlc_dir, goal, unit, repo, entry, author):
    """Name the owner of a unit that has none, from the login that opened the goal being picked.

    WHY HERE AND NOT ON `work.start`'S PATH, which is where this began. The name this inference
    records is compared against issue authors forever after -- inference happens once -- so it has
    to come from GitHub, and `work.start` has no GitHub identity in hand. Every way of giving it one
    was worse, measured rather than assumed: `ledger.actor` invents `$USER` or the literal
    `"unknown"` (the exact hole this remediation closes); a `gh api user` of its own is a hidden
    subprocess whose answer depends on the machine, which makes the TEST SUITE's behaviour depend on
    it too; and routing that call through `start`'s injected runner breaks #1467's measured
    requirement that the pick path read the issue EXACTLY ONCE. Here the login is already in hand,
    on a read this gate makes for its own reasons, through the backlog source's own runner -- so it
    costs no new call, is in the same namespace as every later comparison by construction, and is
    exercised by the same fakes as every other pick-path write.

    WHAT IT COSTS, AND THE COST IS *WHICH PERSON*, NOT A DELAY. One `gh issue view --json author`
    per pick of a unit that has an entry and no owner -- at most once per unit, because the next
    pick finds an owner. The part that matters is the other half: the FIRST pick of a brand-new unit
    returns `NO_ENTRY` above this point (deliberately -- creating a registry entry is
    `sync_at_pick`'s job, not a gate's), so the unit is created ownerless and is named by the author
    of the SECOND goal picked onto it. Those are frequently different people, and the naming is
    once-only and hand-edit-only thereafter, so a passer-by's issue can durably own a unit somebody
    else created -- observable by exactly the person who then finds their own issues held. And
    during that window every ask addressed to `entry.owner` by `feature_propagate`, `feature_sync`,
    `unit_completion` and `feature_doc` lands unaddressed, which is worst at precisely the moment a
    new unit raises the most propagation and scope questions. "Named one pick late" undersells both;
    it is written out here so a reader budgeting for a delay is not surprised by an identity.

    THE FOURTH OPTION, COSTED RATHER THAN LEFT UNCONSIDERED, because it is the one that removes that
    cost. The author string is in hand HERE, and `work.start` runs a moment later on the same goal,
    after `sync_at_pick` has created the entry: thread the login forward and claim there, and the
    unit is named by the author of the FIRST goal, with no new gh call, no machine dependence and no
    second issue read. It is strictly better on the property this paragraph is about, and it is not
    taken in this change for one structural reason: `loop._next` returns `(kind, goal)` and nothing
    else, so carrying a value from the pick to the start means widening that seam -- a new return
    field or a state file read by `work.start` -- and `loop`->`work` currently passes only the goal
    id, on purpose. Widening it is a change to the loop's own contract, with its own blast radius
    and its own tests, and doing it inside an ownership goal would hide a seam change under a policy
    change. Filed as the follow-up; until it lands, the two costs above are the honest ones.

    A HAND-RUN `work.py start` THEREFORE NAMES NOBODY, deliberately, and that is not the two-layer
    hole it looks like: inference is a RECORD, not a defence. The field it fills is defended by
    #1477's registry-write refusal whatever reaches it, and the next loop pick names the owner.

    -> True IFF A WRITE ACTUALLY LANDED. #1575's caller re-reads the registry on that answer, so it
    has to mean the durable thing and not the attempted one: `amend` writes nothing when `claim`
    filled nothing, and a write that did not land is not evidence of a state (`feature_labels`'
    REFUSED_WRITE_FAILED rule, which this module holds everywhere else). A False here therefore
    leaves the verdict answered against the entry as it still is on disk, which is the refusal."""
    if author is None:
        return False
    if sync.is_scope_expansion(entry, repo):
        # THE INVARIANT THE OLD SITE STATED, AND IT HAD TO MOVE WITH THE CLAIM. On `work.start`'s
        # path this was `report["recorded"]` -- "a goal refused as a scope expansion worked nothing
        # and may claim nothing". `loop._next` short-circuits this gate behind #1477's, which
        # preserves it for the ordinary path -- but that gate FAILS OPEN (a module it cannot load,
        # any internal `FAILED`), and `feature_sync._record_goal` then refuses the write anyway. So
        # without this line a unit could be named, once and permanently, by a goal that was never
        # recorded under it. Asked through `is_scope_expansion` rather than re-derived: that
        # function is ONE definition with two callers by design, and this is the third asking the
        # same question rather than a second opinion about it.
        _note("sigma: features: unit %s was not named from #%s: %s is not one of the repos that "
              "unit lists, so this goal is not recorded under it either.\n" % (unit, goal, repo))
        return False
    try:
        report = claim_at_pick(sdlc_dir, goal, unit, repo, author)
        return bool(report.get("claimed") and report.get("landed"))
    except Exception as exc:              # noqa: BLE001 - a record must never break a pick
        _note("sigma: features: could not record an owner for unit %s (%s); the pick carries "
              "on and the next one retries it.\n" % (unit, _flat(exc)))
        return False


def _text(unit, repo, entry, verdict, who):
    """The flag comment. It says WHO opened the issue and WHO is recorded, side by side.

    THAT PAIRING IS THE POINT, and it is what a wrong refusal needs. Both sides of the comparison
    are GitHub handles (see the module docstring); a project whose registry spells an owner some
    other way would hold every issue its real account opens, and a comment that named only the
    refusal would read as "Sigma stopped" rather than as "these two strings are one person."""
    return (
        "%s\n"
        "**Sigma has set this goal aside.** It carries unit `%s`, and %s\n\n"
        "| | recorded |\n|---|---|\n"
        "| unit owner | %s |\n| `%s` board owner | %s |\n| opened this issue | %s |\n\n"
        "Filing work against a unit is the unit owner's call, or the call of whoever owns the board "
        "it lands on (§12) — and where those two disagree, the **board owner wins**, because board "
        "ownership is authority over what becomes work in somebody's queue. An agent may pick this "
        "up and start executing it before its owner has seen it, which is what this gate exists to "
        "prevent.\n\n"
        "%s has been asked through the ledger. **Two ways to clear it, and both are registry "
        "edits:** authorise the whole unit once by setting `repos.%s.authorized = true`, after "
        "which every issue under `%s` is filed directly, follow-ups included; or — if two of the "
        "names above are one person spelled two ways — correct the registry, which is the fix "
        "rather than approving each issue forever.\n\n"
        "`/sigma-promote` returns this issue to the board once one of those is done. **On its own it "
        "does not clear this hold**: this gate reads the registry and the account that opened the "
        "issue, not the labels, so a promotion with neither edit behind it comes straight back here "
        "on the next pick."
        % (OWNER_MARKER, unit, _because(unit, repo, verdict, who), _code(unit_owner(entry)) or "—",
           repo, _code(board_owner(entry, repo)) or "—", _code(who) or "—",
           verdict.owner or "The owner", repo, unit))


def _again_text(unit, repo, entry, verdict, who):
    """The SECOND flag comment: what this goal gets when it was promoted and set aside again.

    IT IS A DIFFERENT TEXT, NOT A REPEAT OF THE FIRST, because the reader is in a different place.
    Somebody has already read the first comment and acted -- the promotion is the evidence -- so the
    thing they need is not the table again, it is the sentence saying which act they performed and
    why it was not the one that clears this. The table is kept, below the answer rather than above
    it, because the mismatch it exposes (`opened this issue` against the two recorded owners) is
    exactly what a person who has now tried twice should be looking at.

    IT CARRIES `OWNER_MARKER` TOO. The marker is the watermark for "this hold has been announced",
    and a third cycle must find one whether or not the first comment is still on the timeline --
    a deleted first comment would otherwise make cycle three read as cycle one."""
    return (
        "%s\n"
        "**Promoted, and set aside again.** This goal carries unit `%s`, and %s\n\n"
        "`/sigma-promote` returns board membership. It does not change what this gate measures — "
        "the registry, and the account that opened the issue (§12) — so the next pick reached the "
        "same answer it did before.\n\n"
        "**What clears it:** set `repos.%s.authorized = true` in the registry, after which every "
        "issue under `%s` is filed directly, follow-ups included; or, if two of the names below are "
        "one person spelled two ways, correct the registry. Then promote it, and it stays.\n\n"
        "| | recorded |\n|---|---|\n"
        "| unit owner | %s |\n| `%s` board owner | %s |\n| opened this issue | %s |"
        % (OWNER_MARKER, unit, _because(unit, repo, verdict, who), repo, unit,
           _code(unit_owner(entry)) or "—", repo, _code(board_owner(entry, repo)) or "—",
           _code(who) or "—"))


def _because(unit, repo, verdict, who):
    """The ISSUE COMMENT's rendering of `refusal_clause` -- the markdown one, with the subject named
    rather than referred to, because the reader of a comment may not be the person who filed."""
    named = _code(who)
    said = ("%s — the account that opened it —" % named) if named else "The account that opened it"
    return refusal_clause(verdict, unit, repo, said, quote=_code) + "."


def _code(value):
    return ("`%s`" % value) if value else ""


def _flag(source, goal, text, again_text):
    """Post the flag comment. -> `FLAG_FIRST`, `FLAG_AGAIN`, or `FLAG_NONE`.

    Read against the ISSUE'S OWN TIMELINE rather than local state, because the state that matters is
    the one a human reads -- `feature_propagate._flag`'s reasoning and its honest bound: the probe is
    a read followed by a write with nothing serialising them, so two machines whose windows overlap
    can each post one comment. That costs a duplicate comment, never a wrong state.

    THE MARKER'S PRESENCE IS EVIDENCE OF A PROMOTION, NOT OF A REPEATED PICK, and that one fact is
    what turns a second comment from spam into a signal (#1569). `sources.mark_needs_confirmation`
    removes `goal_label` in the SAME atomic swap that adds `proposed_label`, and both queues then
    refuse the result -- `_fetch_pending` excludes `proposed_label` (#1392) and `_card_is_eligible`
    treats it as disqualifying -- so an issue this gate has marked is unpickable until somebody puts
    `sdlc:goal` back. Reaching this function again therefore means the goal WAS promoted and is
    being set aside a second time. It used to return False there and the ledger note was gated on
    it, so the operator performed the remedy the comment named, the goal was demoted again, and
    neither channel said a word.

    ONE COMMENT PER DEMOTION, NEVER ONE PER PICK, which is the bound the old idempotency was
    protecting and it still holds. The transient direction never reaches here (`marked` is False and
    nothing durable is written); the marked direction ends the goal's pickability. So the count is
    bounded by promotions -- a human's, or `blockers.classify`'s `PROMOTED` arm for a
    `{followup, needs-confirmation}` issue, which is the one automated promoter that can reach it
    and is itself bounded to one pair per blocker event (`feature_propagate.gate_at_pick` derives
    that path in full). An automated promoter silently undoing a human's hold is a thing worth one
    comment, not a reason to say nothing.

    THE FIRST TEXT IS STILL POSTED EXACTLY ONCE. `again_text` is a different comment for a different
    fact, so the table-and-explanation body never appears twice on one issue however many cycles it
    goes round."""
    try:
        seen = source.fetch_comments_strict(goal)
    except Exception as exc:              # noqa: BLE001 - a read we cannot trust posts nothing
        _note("sigma: features: could not read #%s's comments (%s) — the ownership flag was not "
              "posted this pass; the next pick retries it\n" % (goal, _flat(exc)))
        return FLAG_NONE
    again = any(legacy.has_marker((comment or {}).get("body") or "", OWNER_MARKER)
                for comment in (seen.get("comments") or []))
    try:
        source.note(goal, again_text if again else text)
        return FLAG_AGAIN if again else FLAG_FIRST
    except Exception as exc:              # noqa: BLE001 - flagging must never break the pick
        _note("sigma: features: could not comment on #%s (%s) — the refusal stands, but nobody "
              "has been told on the issue itself\n" % (goal, _flat(exc)))
        return FLAG_NONE


def gate_at_pick(sdlc_dir, source, goal, config, unit, run=None, cwd=None, remote=None):
    """-> `Gate`. Was this issue filed by somebody entitled to file against the unit it carries?

    CALLED INSIDE `loop._next`'S CLAIM LOCK, right after the unit label is attached and after
    #1477's scope gate -- the last moment at which nothing has acted on the goal. A refusal here is
    the cheapest possible one: no worktree, no branch, no registry write, no PR.

    IT ASKS ABOUT THE AUTHOR, NEVER ABOUT THE PICKER, and that is the difference between this gate
    and every other gate on the pick path. The rule is about who may CREATE work carrying the unit's
    label; who later picks up work that was legitimately created is a different question and this
    level does not have an opinion about it. So a stranger picking the owner's issue proceeds, and
    the owner picking a stranger's issue does not.

    THE AUTHOR READ IS PAID ONLY WHEN THE ANSWER DEPENDS ON IT. `may_file` is called first with no
    actor: a granted unit, an unowned unit, an unrecorded unit and an unadopted project each settle
    without a name, and only a unit that has an owner and no grant reaches `fetch_author`. That is
    one `gh issue view --json author` on exactly the picks the gate exists for, and none on the
    rest.

    `sdlc:needs-confirmation` STANDS ALONE (`docs/label-model.md` §2): the goal gives up membership
    and waits for a human, which is what "inert until its owner promotes it" already means
    everywhere else in this tool. It is the right label for the same reason #1477's gate uses it --
    unlike a missing label, which self-heals the moment somebody creates it, this needs a DECISION.

    THE DECISION IS A REGISTRY EDIT, AND `/sigma-promote` IS WHAT COMES AFTER IT (#1569). This
    paragraph used to end "and `/sigma-promote` is exactly the gesture that ends it", which is true of
    #1477's gate and false of this one: that command swaps two labels, and nothing below reads a
    label. Every text this gate writes now names the two registry edits that DO clear it and says
    that promoting alone does not; `would_hold` lets `promote` refuse the no-op rather than perform
    it. See the module docstring for the loop that wording produced.

    NEVER RAISES, and the totality is layered rather than promised. The detection is a registry read
    plus one issue read; the outer guard only ever covers a failure to DETECT, which PROCEEDS, since
    a gate that cannot answer must not stop a queue. Every call into `source` is guarded on its own.

    THE THREE DIRECTIONS, each chosen rather than fallen into, and each `feature_propagate`'s:

      - THE MARK LANDED       -> REFUSE, `marked` True, and NOW write the durable things: one flag
                                 comment and, gated on that comment actually having been POSTED, one
                                 ledger entry to the owner. One watermark for both -- but the
                                 watermark is the DEMOTION, not the hold (#1569): a second arrival
                                 means the goal was promoted back (`_flag` derives why), so it gets
                                 its own shorter comment and its own note saying the promotion did
                                 not clear it. The first, long text is still posted exactly once.
      - THE MARK DID NOT LAND -> REFUSE, `marked` False, and write NOTHING durable.
                                 `feature_labels`' REFUSED_WRITE_FAILED rule verbatim: a write that
                                 did not land is not evidence of a state, so nothing may claim one,
                                 and a network blip must not summon a human. It also bounds the
                                 noise -- the goal keeps `sdlc:goal`, so it is re-picked, and a note
                                 written here would be written again every pick until it cleared.
      - THE SOURCE HAS NO SURFACE -> PROCEED. `LocalSource` has none of these methods and no
                                 repository label namespace at all; refusing forever with no way to
                                 signal is the "issue nobody can find" this codebase never converges
                                 on. A local backlog has no author to read either, so there is
                                 nothing this gate could have measured.

    ONE HONEST GAP IN THAT TABLE, AND IT IS A SHARED SHAPE RATHER THAN A ONE-OFF: on the FIRST
    direction, if the label write lands and the comment then fails, nothing is written and nobody is
    told -- and unlike the second direction there is no next pick to retry on, because `sdlc:goal`
    is already gone. The stderr line `_flag` prints ("the next pick retries it") is inherited from
    `feature_propagate.gate_at_pick`, WHICH HAS THIS EXACT SHAPE AND IS ALREADY MERGED, and whose
    reasoning holds only on the `not marked` path. That is why it is named here rather than fixed
    here: the coupling exists to bound REPEATED notes across picks, on the marked path there are no
    further picks, and decoupling it (`if marked: _tell(...)`, with the comment as only its own
    watermark) is a change both call sites want or neither should get. Filing it against both is the
    right move; fixing one and leaving its twin is how two spellings of one rule start.

    THE RESIDUE THIS GATE INHERITS RATHER THAN INTRODUCES, named because `feature_propagate`'s
    docstring handed the decision here: `blockers.classify` returns `PROMOTED` for an issue carrying
    BOTH `sdlc:followup` and `sdlc:needs-confirmation`, so something naming a goal held here as a
    blocker can promote it back out. It is bounded the same way (the promotion fires on a blocker
    event, not on every pick, and the goal ends each cycle inert), and closing it needs a signal
    `classify` can SEE -- it reads labels alone, and neither gate's flag comment is one. This goal
    deliberately does NOT add that signal: a new label in the `sdlc:*` namespace is a change to the
    contract in `docs/label-model.md` that every adopter's board would have to learn, made on behalf
    of a path that ends inert either way. It is recorded in §2b-iii instead, where the next person
    to widen that row will read it."""
    try:
        return _gate_at_pick(sdlc_dir, source, goal, config, unit, run, cwd, remote)
    except Exception as exc:              # noqa: BLE001 - a gate that cannot answer must not block
        _note("sigma: features: the ownership check for %s did not run (%s); the pick carries "
              "on.\n" % (goal, _flat(exc)))
        return Gate(True, FAILED, unit, None, False)


def _gate_at_pick(sdlc_dir, source, goal, config, unit, run, cwd, remote):
    features_dir = registry.registry_dir(sdlc_dir)
    if not features_dir.is_dir():
        return Gate(True, NOT_ADOPTED, unit, None, False)
    if not unit:
        return Gate(True, NO_UNIT, unit, None, False)
    entry = registry.read(features_dir).get(unit)
    repo = sync.repo_slug(config, run or sync._run,
                          str(cwd or pathlib.Path(sdlc_dir).parent),
                          remote or sync.DEFAULT_REMOTE)
    # THE ACTOR-FREE PASS FIRST. Everything that does not depend on WHO is settled here, so the
    # network read below is paid only by the picks this gate exists for. See the docstring.
    settled = may_file(entry, repo, None)
    if settled.reason not in (NO_ACTOR, NO_OWNER):
        return Gate(True, settled.reason, unit, repo, False)
    if not _has_surface(source, config):
        return Gate(True, NO_SURFACE, unit, repo, False)
    author = _author(source, goal)
    if settled.reason == NO_OWNER:
        # THE INFERENCE, AND THIS IS WHERE IT LIVES -- see `_claim_here` for why here and not on
        # `work.start`'s path. The pick always PROCEEDS after it: a unit nobody owned refuses
        # nobody, and the claim is a record rather than a decision.
        _claim_here(sdlc_dir, goal, unit, repo, entry, author)
        return Gate(True, NO_OWNER, unit, repo, False)
    if author is None:
        return Gate(True, NO_ACTOR, unit, repo, False)
    # #1575. THE BOARD HALF IS ITS OWN QUESTION, AND IT IS ASKED BEFORE ANYBODY IS REFUSED. The arm
    # above fires only when NEITHER owner is recorded, which a PROPAGATED entry never is: it arrives
    # naming the unit's owner -- the one inferred in the repo it came from -- and naming nobody for
    # the board it just landed on. So the sibling fell through to a refusal and never recorded a
    # board owner of its own, permanently, because the only arm that infers one was already behind
    # it. That refusal held every issue in this repo for a person in a DIFFERENT repository, who may
    # not know the unit reached this one, and no local act could clear it.
    #
    # The entry is RE-READ rather than mutated in place: `claim_at_pick` writes through
    # `feature_sync.amend`, under the unit's lock, to a copy it read itself -- so the object here is
    # not the one that changed, and the only honest way to answer the verdict against what LANDED is
    # to read it back. Gated on `_claim_here`'s own bool, so a write that did not land leaves the
    # refusal exactly where it was; and gated on `_claimable_board` first, so a unit whose board
    # owner is already recorded (every pick after this one) pays neither the claim nor the re-read.
    if _claimable_board(entry, repo) and _claim_here(sdlc_dir, goal, unit, repo, entry, author):
        entry = registry.read(features_dir).get(unit)
    verdict = may_file(entry, repo, author)
    if verdict.allowed:
        return Gate(True, verdict.reason, unit, repo, False)
    marked = False
    parked = gate_hold.surface_mode(source, config, ()) == gate_hold.PARK
    try:
        if parked:
            marked = gate_hold.park_for_gate(
                source, goal, "owner_hold",
                "%s is held by unit ownership (%s)" % (goal, verdict.reason))
        else:
            marked = source.mark_needs_confirmation(goal) is not False
    except Exception as exc:              # noqa: BLE001 - the refusal stands whatever the write did
        _note("sigma: features: could not hold #%s (%s) — the pick is refused "
              "and the next one retries the hold\n" % (goal, _flat(exc)))
    if not marked:
        # THE TRANSIENT DIRECTION, and nothing durable is written on it. See the docstring.
        return Gate(False, verdict.reason, unit, repo, False)
    said = _flag(source, goal,
                 _text(unit, repo, entry, verdict, author) + (gate_hold.PARK_NOTE if parked else ""),
                 _again_text(unit, repo, entry, verdict, author)
                 + (gate_hold.PARK_NOTE if parked else ""))
    if said:
        # ONE WATERMARK FOR BOTH CHANNELS, still -- but the watermark is now the DEMOTION rather
        # than the hold, so a remedy that was performed and did not work is announced instead of
        # swallowed (#1569). `_flag` says which of the two it was; the note says the same.
        _tell(sdlc_dir, goal, unit,
              _why(goal, unit, repo, verdict, author, again=(said == FLAG_AGAIN)),
              to=verdict.owner)
    return Gate(False, verdict.reason, unit, repo, True)


def would_hold(sdlc_dir, config, unit, author, run=None, cwd=None, remote=None):
    """-> the `Verdict` a pick would REFUSE on, or None when a pick would not refuse. NEVER RAISES.

    THE SAME QUESTION `gate_at_pick` ASKS, WITHOUT THE SOURCE AND WITHOUT THE WRITES, so `promote`
    can refuse a promotion the very next pick would undo instead of performing it as a no-op
    (#1569). It exists rather than a second copy of the policy in that module for the reason
    `refusal_clause` exists: this file has already paid twice for the same fact being written out in
    a second place.

    IT MUST FAIL OPEN AND IT DOES, on every axis. A project that has not adopted the model, a unit
    the registry does not name, an author nobody could read, a registry that could not be read at
    all: each answers None, because a WRONG refusal inside `/sigma-promote` is the dead end that
    command exists to remove, while a missing one costs only the behaviour that shipped before this.

    IT ACCOUNTS FOR THE INFERENCE, WHICH IS WHY IT IS NOT SIMPLY `may_file`. A pick with a claimable
    board (#1575) names this repo's board owner from the author and then permits them, so answering
    on the registry as it stands today would refuse a promotion that the pick allows -- the exact
    false refusal this function is here to prevent. `_claimable_board` is the one predicate both
    sides ask.

    IT READS NO NETWORK. `repo_slug` prefers `discovery.github.repo` and falls back to the local git
    remote; everything else is a registry read. The author is handed IN, by a caller that already
    had the issue open."""
    try:
        features_dir = registry.registry_dir(sdlc_dir)
        if not unit or not features_dir.is_dir():
            return None
        entry = registry.read(features_dir).get(unit)
        repo = sync.repo_slug(config, run or sync._run,
                              str(cwd or pathlib.Path(sdlc_dir).parent),
                              remote or sync.DEFAULT_REMOTE)
        if _claimable_board(entry, repo):
            return None
        verdict = may_file(entry, repo, _named(author))
        return None if verdict.allowed else verdict
    except Exception as exc:              # noqa: BLE001 - a check that cannot answer refuses nobody
        _note("sigma: features: could not work out whether unit %s still holds this issue (%s); "
              "it is treated as unheld, because a promotion refused over our own gap is the dead "
              "end /sigma-promote exists to remove.\n" % (unit, _flat(exc)))
        return None


# --------------------------------------------------------------------------- CLI


USAGE = "usage: feature_owner.py check <sdlc_dir> <unit> <repo> [actor]"


def main(argv):
    """`feature_owner.py check <sdlc_dir> <unit> <repo> [actor]` -- why is this refused?

    READ-ONLY, and there is deliberately no `grant` verb: `authorized` moves only by a human editing
    the registry (§12), and a CLI that could write it would make the grant something a machine can
    mint. Same omission, same reason, as `feature_sync.main`'s missing `sync`."""
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    if len(argv) >= 5 and argv[1] == "check":
        sdlc_dir, unit, repo = argv[2], argv[3], argv[4]
        actor = argv[5] if len(argv) >= 6 else None
        entry = registry.read(registry.registry_dir(sdlc_dir)).get(unit)
        verdict = may_file(entry, repo, actor)
        print(json.dumps({"unit": unit, "repo": repo, "actor": actor, "allowed": verdict.allowed,
                          "reason": verdict.reason, "owner": verdict.owner},
                         indent=2, sort_keys=True))
        return 0
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
