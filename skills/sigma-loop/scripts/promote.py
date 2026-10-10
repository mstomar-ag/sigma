#!/usr/bin/env python3
"""promote.py (#1392) -- the ONE sanctioned way to move an issue across the human approval gate.

LEGACY-ONLY LABEL (decision-rubric slice 18). Nothing writes the confirmation label any more -- a
filed follow-up is already a goal, armed or parked with a declared question -- so every bucket
below that keys on `proposed_label` lists LEFTOVERS from old boards, and this verb stays as the
way to approve or clear each one. The history that follows explains why the buckets exist.

WHY THIS EXISTS. The legacy confirmation label (`handoff.PROPOSED_LABEL`, #233) was the approval gate:
an issue Sigma filed itself carried it and deliberately does NOT carry `sdlc:goal`, so no loop
can pick it until a human says so. The approval gesture is REMOVING that label -- but nothing ever
said so out loud, and the intuitive gesture in the GitHub UI is the opposite one: ADD `sdlc:goal`.
Do that and the issue carries BOTH, which is not a state the model has a name for.

That half-promoted state used to be read two different ways by the same release:

  - `_card_is_eligible` (the board path) treats `proposed_label` as disqualifying -- card skipped.
  - `_fetch_pending` (the label path) never checked it at all -- issue picked.

Same repo, same issue, opposite answers, decided by nothing more than which `queue_source` the
config happens to use. `_fetch_pending` now excludes it too (#1392), which makes the two agree and
makes the label mean what it says -- and makes THIS module the way across, because a human doing it
by hand still has to get two labels right in the right order against an API that does not offer
transactions. `apply` is one atomic swap; `list` finds everything already stuck.

THREE BUCKETS, and the second one is the interesting one:

  awaiting    -- open, carries `proposed_label`, no `goal_label`. The ordinary approval queue.
  deadlocked  -- open, carries `blocking_label`, no `goal_label`. Something else is BLOCKED on this
                 issue, and no queue can serve it: `_blocking_priority_pending` reuses
                 `_fetch_pending`, which always prepends `--label goal_label`, and `gh issue list
                 --label` ANDs -- so `sdlc:blocking` is a TIE-BREAK among already-eligible issues,
                 never a queue of its own. A blocker without `goal_label` is therefore unreachable,
                 while `auto_unpark` refuses to resume whatever it blocks until it CLOSES. Nothing
                 breaks that cycle on its own. Surfacing it is the fix; auto-promoting it is not
                 (it is frequently the very proposal a human is supposed to approve first).
  drift       -- open, carries BOTH `goal_label` and `proposed_label`. The half-promoted state
                 above, already on the board. Repair is a single removal.

A PROMOTION THAT THE NEXT PICK WOULD UNDO IS REFUSED, NOT PERFORMED (#1569). The two
branching-model gates hand a goal to a human by writing `proposed_label`, which makes this command
the gesture that comes next -- and neither of them reads a label, so promoting one of their issues
without first editing the registry returns it to the board for exactly one pick. The operator paid a
full cycle to find that out, and the ownership gate's own text used to send them round it. Both
questions are now asked before the swap, each through its own gate's predicate (`_feature_hold`),
and the refusal names the registry edit that actually ends the hold.

NEVER AUTO-ANYTHING. Every verb here is human-invoked and synchronous, so unlike the autonomous
lifecycle transitions in `sources.py` it uses `_swap_labels` (which RAISES) rather than
`_swap_labels_best_effort` (which swallows): a human who typed a command is owed the truth about
whether it landed. A failure on one issue is reported and the rest continue -- `apply_actions`'
posture, not an abort.

THE BOARD CARD IS NOT OPTIONAL. #1391 step 4's lesson, learned the expensive way: fixing the label
and leaving the card where it sits manufactures a permanently-unpickable goal on a board-
authoritative repo -- correctly labelled, invisible to `_board_queue` forever. Every promotion
moves the card to the `ready` column and checks `_set_board_status`' honest bool.

EVERY NAMED BLOCKER MUST BE CLOSED, NOT JUST THE FIRST ONE MENTIONED (#1888). A real batch promoted
by hand one night used a check that only ever looked at an issue's FIRST `Blocked by:` line -- 5 of
12 issues were wrongly promoted because a SECOND, still-open blocker on each was never seen (one
case: a sibling issue in the very same batch that had only just been promoted, not actually closed).
`apply` now parses EVERY `Blocked by:`/`depends on`/`depends upon` reference a body names (reusing
`blocker_scan.extract_refs`, the same whole-body scanner `mirror.py` and the pick-time dependency
gate already read blockers off of) and refuses unless every one is a LIVE, confirmed CLOSED issue --
never "carries `sdlc:goal`", never "not carrying the legacy confirmation label", and never
`blockers.classify`'s adjacent `PICKABLE` verdict, which answers a different question (see
`_blocker_status`'s own docstring). `list` shows which rows declare a blocker at all, at zero extra
`gh` cost; only `apply` (and its `--dry-run`) ever live-verifies what one resolves to.

    python3 promote.py list <sdlc_dir> [--assignee X] [--json]
    python3 promote.py apply <sdlc_dir> <issue>... [--dry-run]
    python3 promote.py demote <sdlc_dir> <issue>...
"""
import json, pathlib, sys, importlib.util

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


sources = _load("sources")
handoff = _load("handoff")
triage = _load("triage")          # _config / _flags -- the shared config+flag readers, reused
blocker_scan = _load("blocker_scan")   # #1888: the ONE "Blocked by #N" vocabulary -- see _blocker_status

#: The branching-model modules, loaded on the ONE path that needs them -- a promotion of an issue
#: that declares a unit -- and never at import. Lazy for `loop._feature_owner`'s reason: they pull
#: `feature_registry`, `feature_sync` and `ledger` behind them, on behalf of a model most projects
#: never adopt, and `list`/`demote` reach none of it. MEMOISED because `apply` takes a LIST and
#: `_load` re-execs the file on every call -- a five-issue promotion would otherwise exec the same
#: five modules five times over.
_FEATURES = {}


def _feature(name):
    if name not in _FEATURES:
        _FEATURES[name] = _load(name)
    return _FEATURES[name]


#: The comment left on every promotion/demotion. A single, deliberate transaction with a fixed
#: prefix -- so `reconcile`'s timeline oracle reads it as ONE intent rather than as the sub-60s
#: ambiguity window two racing writers would produce, and so a human scrolling the issue can see
#: who moved it and why. Deliberately NOT a `_BLOCK_RE` trigger phrase (no "needs"/"after"/
#: "requires"/"waiting on" before a `#N`), so it can never manufacture a phantom blocker the way
#: `PARK_COMMENT_PREFIX` did before #1186.
PROMOTE_COMMENT = "Promoted to `{goal}` via /sigma-promote — approved for the loop to pick up."
DEMOTE_COMMENT = ("Returned to `{proposed}` via /sigma-promote — awaiting approval again; the loop "
                  "will not pick it up.")

#: #1006 (decision rubric): with triage on, a demotion PARKS the issue (reason and declared question kind in
#: the comment) instead of writing the legacy confirmation label.
DEMOTE_PARK_COMMENT = ("Parked via /sigma-promote: demoted out of the queue, so the loop will not pick it "
                       "up. Run /sigma-unpark to release it.")

_ISSUE_FIELDS = "number,title,labels,body"
#: `body` added #1888 so `_row` can annotate a bucket row with the `Blocked by:` reference(s) it
#: declares -- PARSING only (`blocker_scan.extract_refs`, no live `gh` call), and still exactly TWO
#: batched `issue list` calls total regardless of backlog size (one per label, in `_fetch` below),
#: never one per issue. Live verification of what a blocker resolves TO belongs to `apply`
#: (`_blocker_status`) alone -- see `survey`'s own docstring below for the cost argument against
#: repeating it here.
_LIST_LIMIT = 200


def _github(source):
    """Is this a source that can actually do any of this? GitHub-only, for the same structural
    reason `auto_unpark`/`blocker_promotion` are: a `LocalSource` goal has no label state at all,
    so there is no gate to cross. Checked on the CAPABILITIES the module really uses (the same
    `hasattr` posture `assign._promote_to_goal` established), never on the class name."""
    return all(hasattr(source, a) for a in ("_swap_labels", "_repo_args", "_run"))


#: Substrings that mark a `gh` failure as the account's API quota rather than the issue or the
#: query -- `cross_repo._RATE_LIMIT_MARKERS`' own list, for the same distinction.
_RATE_LIMIT_MARKERS = ("rate limit", "secondary rate", "abuse detection", "too many requests")


def _why(exc):
    """The reason a `gh` call failed, as one short line an operator can act on (#2757).

    The previous `_fetch` swallowed the exception whole, so a GraphQL quota exhausted by several
    concurrent loops rendered as an empty queue with no cause anywhere. The LAST non-empty line is
    `gh`'s own message (`GraphQL: API rate limit already exceeded ...`); the lines before it are the
    command echo. A rate limit is named as such, because its remedy is to wait, not to investigate."""
    hint = getattr(exc, "hint", None)       # `sources._run_gh` carries `gh`'s own short reason
    lines = [l.strip() for l in str(hint or exc).splitlines() if l.strip()]
    text = (lines[-1] if lines else "") or exc.__class__.__name__
    if text.startswith("gh ") and " failed: " in text:
        text = text.split(" failed: ", 1)[1]
    text = text[:200]
    if any(m in text.lower() for m in _RATE_LIMIT_MARKERS):
        return "rate-limited — %s; retry once the quota resets (`gh api rate_limit`)" % text
    return text


def _rate_limited(why):
    return str(why).startswith("rate-limited")


def _fetch(source, labels, assignee=None):
    """Open issues carrying ALL of `labels` -- `(issues, ok, why)`, FAIL-OPEN. `ok` is the point: an
    empty list from a failed query is indistinguishable from a genuinely empty one, and this module
    must never report "nothing to promote" on the strength of a transport error. The identical
    contract `reconcile._fetch_by_label` already states for the identical reason. `why` is the
    failure's own reason (`_why`), `""` when the read succeeded.

    #2757: OVER REST, via `sources.fetch_issues_rest`. `gh issue list --label` routes through
    GraphQL's `search()` field, the budget several concurrent `/sigma-loop` sessions exhaust -- which
    is exactly when a human runs `list` to see what is waiting. REST is a separate budget, and the
    same migration `mirror.py`, `triage.py` and `status.py` already made (#1829/#1833)."""
    try:
        login = sources.resolve_assignee_login(source._run, assignee) if assignee else None
        # DELIBERATELY NOT fail-open, unlike `mirror.py`/`triage.py`: an unscoped read rendered
        # under a "scoped to @me" header would silently show everyone's queue as yours. A human is
        # reading this to decide what to approve, so an unresolvable scope is a failed read.
        if assignee and login is None:
            return [], False, "could not resolve assignee %r to a login" % assignee
        data = sources.fetch_issues_rest(source._run, getattr(source, "repo", None), list(labels),
                                         cap=_LIST_LIMIT, assignee=login)
        return (data, True, "") if isinstance(data, list) else ([], False, "unreadable response")
    except Exception as exc:                            # noqa: BLE001 - fail-open by contract
        return [], False, _why(exc)


def _names(issue):
    return {(l.get("name") or "") for l in (issue.get("labels") or [])}


def _row(issue, why):
    row = {"number": str(issue.get("number")), "title": issue.get("title") or "",
          "labels": sorted(_names(issue)), "why": why}
    # #1888: parsing only, no live `gh` call (see `_ISSUE_FIELDS`'s own comment and `survey`'s
    # docstring) -- a human scanning `list` sees which rows declare a blocker at all and knows to
    # run `apply --dry-run` (which DOES live-verify) before actually approving one of them.
    refs = blocker_scan.extract_refs(issue.get("body") or "", self_ref=issue.get("number"))
    if refs:
        row["blockers"] = [r["ref"] for r in refs]
    return row


def survey(sdlc_dir, config, source=None, run=None, assignee=None):
    """The three buckets plus a `complete` flag. Read-only; safe to run at any time.

    `assignee` defaults to `discovery.github.assignee`, which a bare `/sigma-init` scaffold SHIPS AS
    NULL (#2255 -- not yet decided; `/sigma-setup` fills it in with `@me`) and which reads as empty
    either way on an install that never ran `/sigma-setup` -- so the default scope is normally every
    open issue, not "mine". `render` says which scope it used out loud, because an empty result
    under an unstated scope reads as "there is nothing to approve" when it may only mean "nothing
    is assigned to a filter nobody set".

    #1888: each row NAMES a `Blocked by:` reference it declares (`_row`, parsing only) but never
    LIVE-VERIFIES what it resolves to. This is a deliberate cost split, not a gap matching `apply`'s
    (which does live-verify): `_fetch` below is exactly TWO batched `gh issue list` calls regardless
    of how many issues land in either bucket, and that `O(1)`-in-backlog-size shape is the entire
    reason `survey`/`list` stays cheap enough to run freely. Adding one `gh issue view` per
    blocker-per-row would turn that into `O(N)` `gh` calls at 10x/100x backlog size -- a real
    scalability regression, and real exposure against this account's shared GraphQL rate limit,
    for a command meant to be run often. `apply --dry-run` is where a human sees live-verified
    state, per the issue's own text; this bucket view is only meant to tell them WHEN to check."""
    source = source or sources.GitHubSource(config, run=run)
    if not _github(source):
        return {"buckets": {}, "complete": False, "assignee": None,
                "degraded": ["promotion needs github discovery mode -- this source has no labels"]}
    gh = ((config or {}).get("discovery") or {}).get("github") or {}
    assignee = assignee if assignee is not None else (gh.get("assignee") or None)
    goal, proposed = source.goal_label, source.proposed_label
    blocking = source.blocking_label

    proposed_issues, ok_p, why_p = _fetch(source, [proposed], assignee)
    blocking_issues, ok_b, why_b = _fetch(source, [blocking], assignee)

    awaiting, drift = [], []
    for it in proposed_issues:
        names = _names(it)
        (drift if goal in names else awaiting).append(
            _row(it, "carries both %s and %s — approval was never completed" % (goal, proposed)
                 if goal in names else "awaiting approval"))
    deadlocked = [
        _row(it, "other work is blocked on this, but it has no %s so nothing can pick it" % goal)
        for it in blocking_issues
        # An issue that is BOTH blocking and awaiting approval belongs in exactly one bucket, and it
        # is this one: `deadlocked` names the consequence (work is stalled behind it), `awaiting`
        # only names the state. Reporting it twice would double-count the queue.
        if goal not in _names(it)]
    seen = {r["number"] for r in deadlocked}
    awaiting = [r for r in awaiting if r["number"] not in seen]

    degraded = []
    if not ok_p:
        degraded.append("could not read the %s queue (%s) — awaiting and half-promoted are UNKNOWN, "
                        "not empty" % (proposed, why_p))
    if not ok_b:
        degraded.append("could not read the %s queue (%s) — deadlocked blockers are UNKNOWN, and "
                        "an awaiting row may belong there" % (blocking, why_b))
    # #2757: per-bucket read status, so neither `render_survey` nor a `--json` caller can mistake
    # `[]` from a failed read for an empty queue.
    read = {"awaiting": ok_p, "drift": ok_p, "deadlocked": ok_b}
    errors = {k: (why_p if k != "deadlocked" else why_b) for k, ok in read.items() if not ok}
    return {"buckets": {"awaiting": awaiting, "deadlocked": deadlocked, "drift": drift},
            "complete": ok_p and ok_b, "assignee": assignee, "degraded": degraded,
            "read": read, "errors": errors}


def _state(source, number):
    """`_read_state` without the reason -- the shape `_blocker_status` reads."""
    return _read_state(source, number)[0]


def _read_state(source, number):
    """`((labels, closed, author, body) | None, why)` -- see below; `why` names a failure (#2757).

    `(labels, closed, author, body)` for one issue, or `None` when it cannot be read. Refusing on
    an unreadable issue is deliberate: every refusal below is a SAFETY check, and a check that fails
    open is not a check.

    `author` RIDES ON THE READ THAT WAS ALREADY BEING MADE (#1569), and so does `body` (#1888): the
    ownership gate decides on the account that OPENED the issue, and the blocker check needs the
    issue's own text -- one more `--json` field on a call this function already makes costs nothing,
    while a second `gh issue view` for either would double the cost of every promotion. `author` is
    `""` when gh named nobody (`feature_owner.would_hold` reads that as "no actor", refusing nobody);
    `body` is `""` when gh returns none, which `blocker_scan.extract_refs("")` reads as "declares no
    blocker" -- the correct, zero-cost common case.

    THIS SAME FUNCTION IS ALSO THE BLOCKER VERIFIER (#1888): `_blocker_status` calls `_state(source,
    ref)` for each `Blocked by: #N` the target issue names, and reads `closed` off the result --
    exactly the live, authoritative `state == "CLOSED"` check the target issue itself already gets,
    applied consistently to a reference instead of a second, differently-shaped `gh` call."""
    try:
        data = source._read_issue(number, ["labels", "state", "author", "body", "stateReason"])   # #895: REST first
        if not data.get("state"):
            return None, "unreadable response"
        author = data.get("author")
        # #2532: MERGED (a PR) resolves; CLOSED-without-merge (also a PR) does NOT; an ordinary
        # closed ISSUE still does -- `blocker_scan.closed_state` is the one shared implementation,
        # also used by `blockers.py` and `auto_unpark.py`'s own `_ref_is_open`, so this rule is
        # never independently reinvented a second time.
        return ({(l.get("name") or "") for l in (data.get("labels") or [])},
                blocker_scan.closed_state(str(data.get("state") or "").upper(),
                                          data.get("stateReason") or ""),
                (author.get("login") if isinstance(author, dict) else "") or "",
                data.get("body") or ""), ""
    except Exception as exc:                            # noqa: BLE001 - unreadable == refuse
        return None, _why(exc)


def _blocker_status(source, number, body):
    """`[(ref, phrase, status)]` for every explicit `Blocked by:`/`depends on`/`depends upon`
    reference `number`'s own body names -- `status` one of `"CLOSED"`, `"OPEN"`, `"UNREADABLE"`.
    Empty when the body declares nothing, which is the overwhelming common case and costs nothing
    beyond the `issue view` `_state()` already makes for `number` itself.

    #1888. THE BUG THIS CLOSES: a check that only ever looked at the FIRST `Blocked by:` line in an
    issue body wrongly promoted 5 of 12 issues in one real batch, because a second, still-open
    blocker was never seen. `promote.py apply` had NO blocker-checking code at all before this --
    the mistake was a human/agent's own by-hand reading of the body, run before calling `apply` on
    issues that had already (wrongly) looked clear. This function is what makes that check
    structural instead of a habit.

    REUSES `blocker_scan.extract_refs` -- the SAME whole-body, every-match scanner (`finditer`, not
    `.search`) that `mirror.py` and the pick-time dependency gate (`loop._pick_dependency_hold`)
    already read every blocker off of. No second parser is written here; the multi-match half of
    this fix is a reuse, not a build.

    ONE LIVE `gh issue view` PER UNIQUE BLOCKER (via `_state`), never the board mirror's cache: a
    promotion is a rare, human-paced, explicitly-scoped gesture over a handful of issue numbers, not
    the whole-backlog scan `survey` performs (see `survey`'s own docstring for why the identical
    live-check approach is deliberately NOT repeated there), so it can afford the live read
    `_pick_dependency_hold` avoids paying on every pick candidate.

    DO NOT reuse `blockers.classify`/`blockers.verdict_for` here, even though they look adjacent --
    `classify` special-cases `closed` to `STALE` before it ever reaches the `PICKABLE` branch, so
    the two verdicts cannot literally co-occur, but `PICKABLE` still answers a different question
    than this check needs: it fires the moment a blocker carries `goal_label` and is not (yet)
    closed -- "is this blocker now eligible to be picked", for a goal that may still be mid-work --
    never "has it actually finished". Treating `PICKABLE` as "resolved" here would SILENTLY
    REPRODUCE #1888's exact defect -- a sibling only just promoted in the same batch is not done.
    `_state`'s plain `closed` bool is the only correct signal for THIS check.

    FAILS CLOSED ON PURPOSE: `UNREADABLE` counts as unresolved, never as safe. This is not a new
    asymmetry against this file's usual fail-open posture -- it is `_state()`'s OWN existing
    fail-closed behaviour for the target issue's state (`test_promote_refuses_when_the_current_
    state_cannot_be_read`: "every refusal ... is a SAFETY check, and a check that fails open is not
    a check"), applied consistently to a reference instead of only to the issue being promoted."""
    refs = blocker_scan.extract_refs(body, self_ref=number)
    out = []
    for item in refs:
        ref, phrase = item["ref"], item["phrase"]
        state = _state(source, ref)
        status = "UNREADABLE" if state is None else ("CLOSED" if state[1] else "OPEN")
        out.append((ref, phrase, status))
    return out


def _blocker_note(checked):
    """`""` when nothing was checked, else `"; blockers checked: #A CLOSED, #B OPEN"` -- the issue's
    own explicit ask: dry-run (and the final applied) output should show every blocker checked and
    its resolved state, so a human can catch a mismatch visually, not just trust the tool."""
    if not checked:
        return ""
    return "; blockers checked: " + ", ".join("#%s %s" % (ref, status)
                                              for ref, _phrase, status in checked)


def _blocker_refusal(number, unresolved):
    """`None` when nothing is unresolved, else the one sentence naming EVERY blocker `number` names
    that is not confirmed CLOSED -- never just the first, which is the structural fix #1888 exists
    for."""
    if not unresolved:
        return None
    named = ", ".join("#%s (%s)" % (ref, status) for ref, _phrase, status in unresolved)
    return ("#%s names a blocker that is not confirmed CLOSED: %s — a sibling that was only just "
            "promoted (or is otherwise still open) is not done. Close every one first, then "
            "promote." % (number, named))


def _feature_hold(sdlc_dir, config, number, names, author):
    """The sentence naming a `sdlc:needs-confirmation` gate that would set this goal STRAIGHT BACK
    aside, or None. #1569.

    WHY A PROMOTION NEEDS TO ASK THIS AT ALL. Both branching-model pick gates hand a goal to a human
    by writing `proposed_label`, which makes `/sigma-promote` the gesture that comes next -- and
    neither gate reads a label. `feature_owner.gate_at_pick` recomputes from the registry and the
    issue's author; `feature_propagate.gate_at_pick` recomputes from the registry and this repo's
    slug. A promotion changes neither input, so promoting one of those issues without first editing
    the registry puts it back on the board for exactly one pick. That is a no-op the operator pays a
    full cycle to discover, and the loop it produced is what #1569 records. Refusing it here is the
    same move `_refusal` already makes for a park: name the route, do not perform the dead end.

    IT ASKS EACH GATE THROUGH THAT GATE'S OWN PREDICATE, never through a copy of its policy.
    `feature_owner.would_hold` is the ownership question asked without the writes (it accounts for
    the board-owner inference a pick would perform, so this never refuses a promotion the pick would
    have allowed); `feature_sync.is_scope_expansion` is the scope question, and its docstring is
    explicit that it is one definition with more than one caller.

    IT REPRODUCES EVERY ARM THE SCOPE GATE HAS. This paragraph used to record one it did not: #1568
    briefly taught the gate to ask the REMOTE whether a unit with no local entry already had a
    branch, and to hold the goal when it did, which was a network call this command had no other
    reason to make. THAT ARM WAS WITHDRAWN (#1645) -- §14's own adoption order pushes the branch
    first and declares the unit last, so "live branch, no entry" is the state of every brand-new
    unit's first pick, and the arm refused every first adoption. The two are now the same answer to
    the same question. Measured, on a registry that is adopted and records nothing under `voice`,
    with `feature/voice` live on the remote:

        feature_propagate.gate_at_pick -> Gate(proceed=True, outcome='no-entry'), zero calls made
        promote._scope_expansion       -> None

    Kept as prose rather than deleted because the shape of the mistake is the useful part: no local
    evidence separates a brand-new unit from an unlisted one, so a promotion cannot refuse on that
    input either, and reaching for the only remote fact to hand is what produced an arm that blocked
    all adoption.

    NEVER RAISES, AND FAILS OPEN ON EVERY AXIS -- an unadopted project, an issue declaring no unit,
    two rival unit labels, an unreadable registry. Each answers None.

    #1005: THE BODY MOVED to `gate_hold.feature_hold` (scope first, then ownership, the pick's own
    order), so `/sigma-unpark` asks the very same question; this keeps the name and the prose."""
    return _feature("gate_hold").feature_hold(sdlc_dir, config, number, names, author)


def _scope_expansion(sdlc_dir, config, unit):
    """The repo this goal is in when that repo is NOT one the unit lists, else None. #1477's
    question, asked through #1477's own predicate (now `gate_hold.scope_expansion`)."""
    return _feature("gate_hold").scope_expansion(sdlc_dir, config, unit)


def _refusal(source, number, names, closed, demote, hold=None, unresolved=None):
    """The one reason this issue must not be touched, or None. Every message names the ROUTE, not
    just the problem -- a refusal inside the tool built to remove dead ends must not itself be one.

    `hold` IS `_feature_hold`'S ANSWER, COMPUTED BY THE CALLER AND HANDED IN. It is a parameter
    rather than a call from inside here because the other arms read nothing but the labels this
    function was already given, and a registry read buried under a label check is a cost nobody
    reading the signature would expect. `unresolved` (#1888) is `_blocker_status`'s answer, handed in
    the same way and for the same reason."""
    if closed:
        return "#%s is closed — reopen it first if it should be worked" % number
    if demote and source.parked_label in names:
        # #1393: a park is a HUMAN's membership decision. Writing `proposed_label` onto a parked
        # issue would leave TWO membership labels at once -- and it would also be Sigma
        # overwriting a human's state with a different human-gated state, which is nobody's idea of
        # an undo. The undo for a park is /sigma-unpark.
        return ("#%s carries %s — a park is a human's decision, not something to demote. Run "
                "/sigma-unpark on it instead." % (number, source.parked_label))
    if demote:
        return None
    # #1468: `needs_label_label` is deliberately NOT folded in with the pair above it. It is not a
    # human's hold and `/sigma-unpark` is not its remedy -- the goal is already approved and already
    # carries `goal_label`; it is waiting on somebody creating one `feature:` label, after which the
    # loop clears the overlay itself. `unpark.list_parked` queries only `parked_label` and
    # `goal_blocked_label`, so a held goal is not even LISTED by that command: naming it would send
    # an operator to one that cannot see the issue.
    #
    # IT IS CHECKED LAST, AND THE ORDER IS THE POINT. An issue can carry BOTH overlays -- a goal
    # parked or genuinely blocked that also declares a unit whose label is missing -- and for that
    # issue this message would be actively wrong: it promises that creating the label is enough,
    # when the park or the blocker still stands and a human must still act on it. The parked/blocked
    # message is the STRONGER claim (a human must decide), so it wins; the needs-label message is
    # only reached when it is the sole thing holding the goal, which is when it is true.
    if source.parked_label in names or source.goal_blocked_label in names:
        held = source.parked_label if source.parked_label in names else source.goal_blocked_label
        return ("#%s carries %s — unparking is a different decision from approving. Run "
                "/sigma-unpark on it instead." % (number, held))
    # #1888: ABOVE `_feature_hold`/`needs_label_label`, BELOW parked/goal-blocked -- and the order is
    # a judgement, stress-tested in plan-review and not overturned. A human's park is the strongest
    # claim (nothing here should override a decision already made) and wins above. Below it, an
    # unresolved `Blocked by:` reference is a fact about whether the WORK ITSELF is ready -- closer
    # in kind to the `closed` check at the top of this function than to the branching-model PROCESS
    # gates beneath it (unit ownership, a missing `feature:` label), so it is checked first among
    # those. Only ONE reason is ever shown per issue; this ordering decides which one, not whether
    # the others also apply.
    if unresolved:
        return _blocker_refusal(number, unresolved)
    # #1569: ABOVE `needs_label_label`, AND THE ORDER IS THE SAME JUDGEMENT THE COMMENT BELOW MAKES.
    # A branching-model hold is one Sigma will not clear on its own -- only a human editing the
    # registry ends it -- while a missing `feature:` label self-heals the moment somebody creates it
    # and the loop then strips the overlay unattended. So on the rare issue carrying both, the
    # stronger claim wins, exactly as the park beats the needs-label message for the same reason.
    if hold:
        return hold
    if source.needs_label_label in names:
        return ("#%s carries %s — it is already approved and waiting on a `feature:` label that "
                "does not exist yet. Create that label; Sigma clears the overlay itself."
                % (number, source.needs_label_label))
    # #2263: SAME TIER as `needs_label_label` immediately above -- both are self-healing overlays a
    # human clears by editing the issue itself, never by /sigma-unpark or /sigma-promote, so neither
    # claim is stronger than the other. Checked second only so an order is picked rather than left
    # to name-list ordering: the two verdicts that produce them (`features.BODY_ONLY` vs
    # `features.NONE`) are mutually exclusive by construction, so a real issue should never carry
    # both labels at once.
    if getattr(source, "needs_unit_label", None) and source.needs_unit_label in names:
        return ("#%s carries %s — it is already approved and waiting on a declared unit of work "
                "(a `Feature:` body marker or a `feature:` label). Declare one; Sigma clears "
                "the overlay itself." % (number, source.needs_unit_label))
    return None


def promote(sdlc_dir, config, numbers, source=None, run=None, apply=True, demote=False):
    """Cross the gate (or come back across it) for each issue, one atomic swap apiece.

    Returns `{"results": [...], "apply": bool}` -- one entry per issue with `number`, `outcome`
    (`promoted`/`demoted`/`would`/`skipped`/`failed`), and a human-readable `detail`. One failure
    never stops the rest, matching `triage.apply_actions`' own explicit contract.

    ORDER MATTERS, and it is: read state -> refuse or swap -> move the card -> comment. The swap is
    first among the writes because it is the one that decides pickability; the card move is second
    because a promoted-but-unmoved card is the unpickable-goal trap (#1391 step 4); the comment is
    last and best-effort, because an audit line is not worth failing a landed transition over."""
    source = source or sources.GitHubSource(config, run=run)
    out = {"apply": bool(apply), "results": []}
    if not _github(source):
        out["results"] = [{"number": str(n), "outcome": "skipped",
                           "detail": "promotion needs github discovery mode"} for n in numbers]
        return out
    goal, proposed = source.goal_label, source.proposed_label
    # #1393: demotion gives up MEMBERSHIP, so it must clear the overlays with it -- an overlay is
    # only ever legitimate alongside `goal_label`, and leaving `sdlc:in-progress` or `sdlc:blocked`
    # on an issue that is no longer a goal is the orphan shape the census exists to find.
    # #1006: with triage on (the default) a demotion parks rather than writing the legacy label.
    park_demote = demote and not _feature("file_triage").is_off(config)
    add, remove = ((([source.parked_label] if park_demote else [proposed]), [goal, *source.overlay_labels()])
                   if demote else ([goal], [proposed]))
    verb = "demoted" if demote else "promoted"

    for number in numbers:
        n = str(number)
        state, why = _read_state(source, n)
        if state is None:
            # #2757: a rate limit is its own outcome -- the issue is fine and the same command will
            # succeed unchanged once the quota resets; "could not read" sent people looking at it.
            out["results"].append({"number": n,
                                   "outcome": "rate-limited" if _rate_limited(why) else "failed",
                                   "detail": "could not read the current state of #%s: %s"
                                             % (n, why or "no reason given")})
            continue
        names, closed, author, body = state
        # Asked only on the PROMOTE direction: a demotion is taking work out of the queue, which is
        # where a held goal is going anyway, so a gate that would hold it is not a reason to refuse.
        hold = None if demote else _feature_hold(sdlc_dir, config, n, names, author)
        # #1888: same direction-asymmetry as `hold` just above, and for the same reason -- a demotion
        # never pays for (or is refused by) blockers it is about to leave the queue anyway.
        checked = [] if demote else _blocker_status(source, n, body)
        unresolved = [c for c in checked if c[2] != "CLOSED"]
        refusal = _refusal(source, n, names, closed, demote, hold, unresolved)
        if refusal:
            out["results"].append({"number": n, "outcome": "skipped", "detail": refusal})
            continue
        # Already where it is going: report it honestly rather than counting a no-op as a win.
        # `_swap_labels` is idempotent, so this is a nicety, not a correctness guard -- except for
        # the one case that matters, `both == True`, which is the `drift` bucket and IS a real
        # repair (remove-only), so it must not be short-circuited here.
        settled = (add[0] in names and goal not in names) if demote else \
                  (goal in names and proposed not in names)
        if settled:
            out["results"].append({"number": n, "outcome": "skipped",
                                   "detail": "#%s already carries %s — nothing to do"
                                             % (n, add[0])})
            continue
        this_add = [l for l in add if l not in names]
        this_remove = [l for l in remove if l in names]
        # #1888: every blocker CHECKED (not just an unresolved one -- unresolved already returned
        # above) is named here too, on both the dry-run and the real outcome -- the issue's own
        # explicit ask, so a human can catch a mismatch visually rather than just trust the tool.
        note = _blocker_note(checked)
        if not apply:
            out["results"].append({"number": n, "outcome": "would",
                                   "detail": triage._swap_detail(this_add, this_remove) + note})
            continue
        try:
            # Never both-empty: `settled` above already returned for that case, and `_swap_labels`
            # answers a both-empty call with False, which is indistinguishable from a failed write.
            source._swap_labels(n, add=this_add, remove=this_remove)
        except Exception as exc:                        # noqa: BLE001 - reported, never raised
            out["results"].append({"number": n, "outcome": "failed",
                                   "detail": "label write did not land: %s" % exc})
            continue
        detail = triage._swap_detail(this_add, this_remove) + note
        column = source.col["backlog" if demote else "ready"]
        moved = source._set_board_status(n, column)
        # `_set_board_status` returns False for a board-DISABLED repo too -- the honest answer, but
        # not a problem there: a label-queue adopter has no card to move, and warning them on every
        # single promotion would be pure noise. Only a repo that actually has a board can have a
        # card stuck behind its label.
        if not moved and source.project_enabled:
            # NOT a failure of the promotion -- the label landed and the issue IS across the gate on
            # a label-queue repo. But on a board-authoritative one the card is now the thing holding
            # it back, and saying so is the entire point of `_set_board_status` returning an honest
            # bool (#1391 step 4). Loud, and carried in the result.
            detail += ("; the label landed but the board card did not move to %r — move it by hand "
                       "if this repo picks from the board" % column)
        audit = (DEMOTE_COMMENT.format(proposed=proposed) if demote
                 else PROMOTE_COMMENT.format(goal=goal))
        if park_demote:
            audit = DEMOTE_PARK_COMMENT + "\n" + _feature("qkind").render_line("needs_decision")
        tried = []                                      # the comment was attempted (never post twice)
        try:
            dr = _feature("decision_record")
            if dr.enabled(config):                      # slice 8: store record + attributed comment
                rec = dr.build("demote" if demote else "promote", n, "unknown", verb, "", "human")

                def _post(num, body):
                    tried.append(num)
                    source._issue_comment(num, body)
                err = dr.write_all(sdlc_dir, rec, config, source, text=audit, poster=_post)["error"]
                detail += "; " + err if err else ""
        except Exception as exc:                        # noqa: BLE001 - never break the gesture
            detail += "; decision record failed: %s" % exc
        if not tried:
            try:
                source._issue_comment(n, audit)         # REST first (#895 3a)
            except Exception:                           # noqa: BLE001 - audit trail is best-effort
                pass
        out["results"].append({"number": n, "outcome": verb, "detail": detail})
    return out


def render_survey(result):
    scope = result.get("assignee")
    lines = ["promote: %s" % ("scoped to @%s" % scope if scope
                              else "every open issue (discovery.github.assignee is not set)")]
    labels = {"awaiting": "Awaiting approval", "deadlocked": "Blocking other work, unpickable",
              "drift": "Half-promoted (carries both labels)"}
    # #2757: the degraded notes come FIRST. At the bottom they sat after the very counts they said
    # were wrong, and a reader (human or agent) skimming the top saw "— 0" and stopped.
    for note in result.get("degraded") or []:
        lines.append("NOTE: %s" % note)
    read = result.get("read") or {}
    errors = result.get("errors") or {}
    for key in ("deadlocked", "awaiting", "drift"):
        rows = (result.get("buckets") or {}).get(key) or []
        if read.get(key) is False:
            lines.append("%s — UNKNOWN (read failed: %s)" % (labels[key],
                                                             errors.get(key) or "no reason given"))
            continue
        lines.append("%s — %d" % (labels[key], len(rows)))
        for r in rows:
            lines.append("  #%s %s" % (r["number"], r["title"]))
            lines.append("    %s" % r["why"])
            if r.get("blockers"):
                # #1888: parsing only -- this is NOT a live-verified state, just a pointer telling a
                # human which rows are worth an `apply --dry-run` before approving.
                lines.append("    declares blocker(s) %s — verify each is CLOSED before approving"
                             % ", ".join("#" + b for b in r["blockers"]))
        if not rows:
            lines.append("  none")
    return "\n".join(lines)


def render_promote(result):
    mode = "APPLIED" if result["apply"] else "DRY-RUN"
    lines = ["promote (%s): %d issue(s)" % (mode, len(result["results"]))]
    for r in result["results"]:
        lines.append("  [%s] #%s: %s" % (r["outcome"], r["number"], r["detail"]))
    counts = {}
    for r in result["results"]:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    lines.append("summary: " + ", ".join("%d %s" % (v, k) for k, v in sorted(counts.items())))
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------
# migrate-confirmation (decision rubric, slice 20, #1010)
#
# One-time move of every issue still carrying the confirmation label: armed (`sdlc:goal`) or an ordinary
# park, decided by the SAME rubric triage (`file_triage.decide`) and the SAME blocker and gate predicates
# `apply` uses. Dry run by default (zero writes). Adds no gh call site: reads go through
# `sources.fetch_issues_rest` / `source._read_issue`, writes through `source._swap_labels` (ONE mutation per
# issue, so a parked-plus-label pair can never exist), `source._issue_comment` and `source._set_board_status`.
# ---------------------------------------------------------------------------------------------------
import collections, re

#: First line of the comment this verb leaves; its presence on an issue means "already explained".
MIGRATE_MARKER = "<!-- sigma:migrate-confirmation -->"
#: Copies of the two comment markers the gate writers use (loading those modules here would pull the whole
#: branching model in); `test_markers_match_their_modules` pins each to its owner.
_OWNER_MARKER = "<!-- sigma:feature-ownership -->"
_SCOPE_MARKER = "<!-- sigma:feature-scope-expansion -->"
_FOLLOWUP_LABEL = "sdlc:followup"
#: PROVISIONAL: the most labelled issues one run will read; a fetch that reaches it reports itself incomplete.
#: Override with `ai_filed.migration.max_issues`.
MIGRATE_FETCH_CAP = 10000
_PRIORITY = re.compile(r"priority:(P[0-4])$")

Arm = collections.namedtuple("Arm", "priority reason qkind")
Park = collections.namedtuple("Park", "reason qkind")


def _label_names(issue):
    return [(l.get("name") if isinstance(l, dict) else l) or "" for l in (issue.get("labels") or [])]


def _comment_bodies(issue):
    return [(c.get("body") if isinstance(c, dict) else c) or "" for c in (issue.get("comments") or [])]


def classify_population(issue):
    """`ownership_hold`, `scope_hold`, `followup` or `human` for one labelled issue: the gate flag comments
    first (they carry a registry decision), then the follow-up label, else a human-typed proposal."""
    bodies = _comment_bodies(issue)
    if any(_OWNER_MARKER in b for b in bodies):
        return "ownership_hold"
    if any(_SCOPE_MARKER in b for b in bodies):
        return "scope_hold"
    if _FOLLOWUP_LABEL in _label_names(issue):
        return "followup"
    return "human"


def _own_priority(issue):
    found = [m.group(1) for m in (_PRIORITY.match(n) for n in _label_names(issue)) if m]
    return min(found) if found else None


def plan_for(issue, config, unresolved=(), hold=None):
    """`Arm` or `Park` for one labelled issue. `unresolved` is the blocker refs not confirmed CLOSED, `hold`
    the gate-hold sentence or None (both from the `apply` predicates). Never arms past a hold, a blocker,
    or the deny-list; a human proposal keeps its own priority, an AI-filed one gets the bucket's."""
    pop = classify_population(issue)
    if pop == "ownership_hold":
        return Park("ownership hold: a non-owner filing; the unit owner decides through the registry", "owner_hold")
    if pop == "scope_hold":
        return Park("scope-expansion hold: the unit owner decides through the registry", "scope_hold")
    if unresolved:
        return Park("open blocker(s) not confirmed closed: " + ", ".join(unresolved), "dependency")
    if hold:
        return Park("gate hold: " + str(hold), "needs_decision")
    ft = _feature("file_triage")
    mine = _own_priority(issue)
    title, body = issue.get("title") or "", issue.get("body") or ""
    if pop == "human":
        d = ft.decide(title, body, {"human_confirmed": True, "priority": mine}, config)
    else:
        d = ft.decide(title, body, {}, config)
    if d.kind != "arm":
        return Park(d.reason, "needs_decision")
    pri = d.priority
    if pop == "followup" and mine and int(mine[1]) >= int(pri[1]):
        pri = mine                                       # PROVISIONAL: never raise an existing priority
    return Arm(pri, d.reason, None)


def _migrate_comment(plan, pop):
    if isinstance(plan, Arm):
        return ("%s\nMigrated off the retired confirmation queue: armed for the loop at %s (population: %s; %s). "
                "Pick-time guards still apply." % (MIGRATE_MARKER, plan.priority, pop, plan.reason))
    return ("%s\nMigrated off the retired confirmation queue: parked (population: %s; %s). Run /sigma-unpark "
            "to release it.\n%s" % (MIGRATE_MARKER, pop, plan.reason, _feature("qkind").render_line(plan.qkind)))


def _migrate_cap(config):
    blk = (config or {}).get("ai_filed") if isinstance(config, dict) else None
    mig = blk.get("migration") if isinstance(blk, dict) else None
    cap = mig.get("max_issues") if isinstance(mig, dict) else None
    return cap if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0 else MIGRATE_FETCH_CAP


def _migrate_one(source, config, sdlc_dir, item, apply, rep_item):
    n = rep_item["number"]
    data = source._read_issue(n, ["labels", "state", "author", "body", "stateReason", "comments"])
    names = set(_label_names(data))
    closed = blocker_scan.closed_state(str(data.get("state") or "").upper(), data.get("stateReason") or "")
    if closed:
        rep_item.update(outcome="closed", detail="closed: labels kept")
        return
    if source.proposed_label not in names:
        rep_item.update(outcome="noop", detail="no longer carries %s" % source.proposed_label)
        return
    author = data.get("author")
    author = (author.get("login") if isinstance(author, dict) else "") or ""
    issue = {"title": item.get("title") or "", "body": data.get("body") or "", "labels": sorted(names),
             "comments": _comment_bodies(data)}
    pop = classify_population(issue)
    unresolved = [("#%s (%s)" % (r, s)) for r, _p, s in _blocker_status(source, n, issue["body"]) if s != "CLOSED"]
    try:
        hold = _feature_hold(sdlc_dir, config, n, names, author)
    except Exception:                                   # noqa: BLE001 - the hold probe fails open, like apply
        hold = None
    plan = plan_for(issue, config, unresolved, hold)
    arm = isinstance(plan, Arm)
    rep_item.update(population=pop, action="arm" if arm else "park")
    add, remove = ([source.goal_label], [source.proposed_label]) if arm else \
                  ([source.parked_label], [source.proposed_label])
    if arm:
        if plan.priority != _own_priority(issue):
            add.append("priority:" + plan.priority)
            remove += [x for x in sorted(names) if _PRIORITY.match(x)]
    elif source.goal_label in names:
        remove.append(source.goal_label)                # a half-promoted pair is repaired by the park
    add = [l for l in add if l not in names]
    detail = "%s (%s; %s)" % ("arm at " + plan.priority if arm else "park", pop, plan.reason)
    if not apply:
        rep_item.update(outcome="would-arm" if arm else "would-park", detail=detail)
        return
    source._swap_labels(n, add=add, remove=remove)      # ONE mutation: the removal rides with the addition
    rep_item.update(outcome="armed" if arm else "parked", detail=detail)
    if arm and hasattr(source, "_set_board_status"):
        try:
            source._set_board_status(n, source.col["ready"])
        except Exception:                               # noqa: BLE001 - the label landed; the card is best-effort
            rep_item["detail"] += "; board card not moved"
    if not any(MIGRATE_MARKER in b for b in issue["comments"]):
        try:
            source._issue_comment(n, _migrate_comment(plan, pop))
        except Exception:                               # noqa: BLE001 - audit line is best-effort
            rep_item["detail"] += "; comment did not post"


def migrate(source, config, apply=False, sdlc_dir=".sdlc"):
    """Report dict: `apply`, `refused` (reason or None), `error`, `total` (M: every labelled issue fetched,
    open or closed, or None when the fetch failed), `covered` (N: handled without a read or write failure),
    `complete` (error-free, N == M, not cut off by the cap) and `items`. Dry run (`apply=False`) makes no write."""
    rep = {"apply": bool(apply), "refused": None, "error": "", "total": None, "covered": 0,
           "complete": False, "truncated": False, "items": []}
    if not _github(source):
        rep["refused"] = "migration needs github discovery mode"
        return rep
    if _feature("file_triage").is_off(config):
        rep["refused"] = ("ai_filed.triage.enabled is false: the confirmation queue is still the intended state, "
                          "so nothing is migrated")
        return rep
    cap = _migrate_cap(config)
    try:
        # Paged by REST page size past the 200-issue backlog window; state=all so closed issues are COUNTED
        # (and left untouched) and covered N of M is honest.
        issues = sources.fetch_issues_rest(source._run, getattr(source, "repo", None), [source.proposed_label],
                                           cap=cap, state="all")
    except Exception as exc:                            # noqa: BLE001 - reported, exit non-zero
        rep["error"] = _why(exc)
        return rep
    rep["total"] = len(issues)
    rep["truncated"] = len(issues) >= cap
    for item in issues:
        row = {"number": str(item.get("number")), "outcome": "failed", "detail": "", "population": "", "action": ""}
        try:
            if str(item.get("state") or "").lower() == "closed":
                row.update(outcome="closed", detail="closed: labels kept")
            else:
                _migrate_one(source, config, sdlc_dir, item, apply, row)
        except Exception as exc:                        # noqa: BLE001 - one failure never stops the rest
            row.update(outcome="failed", detail="not migrated: %s" % _why(exc))
        rep["items"].append(row)
    rep["covered"] = sum(1 for r in rep["items"] if r["outcome"] != "failed")
    rep["complete"] = rep["covered"] == rep["total"] and not rep["truncated"]
    return rep


def render_migrate(rep):
    mode = "APPLIED" if rep["apply"] else "DRY-RUN"
    if rep["refused"]:
        return "migrate-confirmation (%s): refused: %s" % (mode, rep["refused"])
    if rep["error"]:
        return "migrate-confirmation (%s): could not list the labelled issues: %s\ncovered 0 of unknown" % (
            mode, rep["error"])
    lines = ["migrate-confirmation (%s): %d issue(s)" % (mode, len(rep["items"]))]
    for r in rep["items"]:
        lines.append("  [%s] #%s: %s" % (r["outcome"], r["number"], r["detail"]))
    counts = collections.Counter(r["outcome"] for r in rep["items"])
    lines.append("summary: " + (", ".join("%d %s" % (v, k) for k, v in sorted(counts.items())) or "nothing to do"))
    lines.append("covered %d of %d" % (rep["covered"], rep["total"]))
    if rep["truncated"]:
        lines.append("INCOMPLETE: the fetch reached its cap; raise ai_filed.migration.max_issues and rerun")
    return "\n".join(lines)


_USAGE = ("usage: promote.py list <sdlc_dir> [--assignee X] [--json]\n"
          "       promote.py apply <sdlc_dir> <issue>... [--dry-run]\n"
          "       promote.py demote <sdlc_dir> <issue>...\n"
          "       promote.py migrate-confirmation <sdlc_dir> [--dry-run | --apply]")


def _numbers(argv_tail):
    return [a for a in argv_tail if not str(a).startswith("--")]


def main(argv, run=None, source=None):
    if argv[1:] in (["-h"], ["--help"]):
        print(_USAGE)
        return 0
    if len(argv) >= 3 and argv[1] == "migrate-confirmation":
        flags = argv[3:]
        if set(flags) - {"--dry-run", "--apply"} or ("--dry-run" in flags and "--apply" in flags):
            print(_USAGE, file=sys.stderr)
            return 2
        config = triage._config(argv[2])
        rep = migrate(source or sources.GitHubSource(config, run=run), config, apply="--apply" in flags,
                      sdlc_dir=argv[2])
        print(render_migrate(rep))
        return 2 if rep["refused"] else (0 if rep["complete"] else 1)
    if len(argv) >= 3 and argv[1] == "list":
        sdlc_dir = argv[2]
        flags = triage._flags(argv[3:])
        assignee = None
        tail = argv[3:]
        if "--assignee" in tail:
            i = tail.index("--assignee")
            assignee = tail[i + 1] if i + 1 < len(tail) else None
        result = survey(sdlc_dir, triage._config(sdlc_dir), run=run, assignee=assignee)
        print(json.dumps(result, indent=2) if flags.get("json") else render_survey(result))
        return 0 if result["complete"] else 1
    if len(argv) >= 4 and argv[1] in ("apply", "demote"):
        sdlc_dir = argv[2]
        numbers = _numbers(argv[3:])
        if not numbers:
            print(_USAGE, file=sys.stderr)
            return 2
        result = promote(sdlc_dir, triage._config(sdlc_dir), numbers, run=run,
                         apply="--dry-run" not in argv[3:], demote=(argv[1] == "demote"))
        print(render_promote(result))
        return 1 if any(r["outcome"] in ("failed", "rate-limited") for r in result["results"]) else 0
    print(_USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
