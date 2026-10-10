#!/usr/bin/env python3
"""Unit completion signalling -- Sigma SURFACES, a human DECIDES (#1478, epic #1464, story #1427).

THE RULE THIS MODULE IS, IN ONE SENTENCE: **Sigma does not decide that a unit of work is
finished.** "No open issues" is not "the unit is done" -- a backlog can empty for a minute while the
work is plainly incomplete, and the person who knows the difference is not this process. So the
`feature/<unit>` -> integration-branch pull request is raised by a human, and reviewed.

What Sigma may do is SAY SO: *"unit `int-contract` -- 0 open issues, 12 closed. Ready for
review?"* That is the whole of the default behaviour, and it costs one REST read per goal that
finishes on a unit.

--------------------------------------------------------------------------------------------------
THE THREE MODES, AND WHY THE DEFAULT IS THE ONE THAT DOES NOTHING

`work.unit_completion` in `.sdlc/config.json`:

  - `surface`   (DEFAULT) -- say it on stderr, and put one addressed note in the ledger. No pull
                request is opened. No pull request is merged. Nothing is labelled, nothing parks.
  - `draft-pr`  -- additionally open the landing pull request AS A DRAFT and park it for a human. A
                draft is structurally unmergeable on GitHub, so the draft IS the park; readying it,
                reviewing it and merging it stay a person's decisions.
  - `off`       -- not even the measurement. Zero calls.

ONLY THE EXACT LITERALS `draft-pr` AND `off` SELECT THE NON-DEFAULTS. Every other value -- `draft`,
`"true"`, `True`, `1`, a typo, a type -- lands on `surface`. This is `discovery.blocking_priority_
override`'s own rule ("a typo lands on the default rather than silently changing how your queue is
ordered"), pointed at the setting where being wrong actually costs something: `draft-pr` writes to
GitHub, and nobody should reach it by misspelling something else.

`work.policy()` resolves an unrecognised `auto_merge` to `off` rather than to its default, and the
difference is deliberate rather than an inconsistency: there, `off` IS the default, so "fall to the
default" and "fall to the safest" are the same sentence. Here they differ, and the default is chosen
because surfacing cannot do damage -- it prints and it appends. The value worth protecting against
an accident is the one that opens a pull request, and only its exact spelling reaches it.

--------------------------------------------------------------------------------------------------
WHAT IS DELIBERATELY NOT HERE: THE MERGE

#1474 built the tier-1 both-green check and, on review, unwired it from the merge path. Its
docstring carries the full trace; the short form is that every merge `work.merge()` can perform is
`sdlc/<n>` -> `feature/<unit>` INSIDE ONE REPO, which cannot land half of a cross-repo pair -- and
the merge that could is the one this file's own issue reserves for a human. §7.2 of the design said
Sigma "enforces this by refusing to merge"; §8 says completion is human-only. §8 won.

So `#1474`'s third acceptance criterion -- "both green and approved merges the pair back to back" --
**still has no owner, on purpose.** Nothing in this module merges anything, and the absence is
pinned by a test asserting no `gh pr merge` appears in the recorded call list in EITHER mode. If
that landing should be automated, that is a decision for a human to take explicitly; it is not a gap
for the completion signal to quietly fill.

--------------------------------------------------------------------------------------------------
HOW #1474's CHECK IS CONSUMED: REPORTED, NEVER GATING

`work.sibling_gate` answers "is the other half of this cross-repo unit ready?". Here it is called
once, when a unit looks complete, and its answer is written into the surfaced line and into the
draft's body. It gates NOTHING, and that is the load-bearing decision of this file:

  GATING THE DRAFT ON IT REBUILDS THE DEADLOCK #1474 TRACED. While a unit is being built, neither
  repo's landing PR exists -- so if repo A refuses to open its draft until repo B's landing PR is
  green, and B refuses for the same reason, the unit can never be landed by anyone. A DRAFT CANNOT
  MERGE, so opening one while the sibling is not ready lands nothing at all; it is precisely what
  breaks the symmetry, by giving the human in the other repo something to see.

THE `reason_class` OBLIGATION #1474 HANDED OVER, AND WHAT WAS DECIDED. That docstring measured all
20 reachable `sibling_gate` reasons against `loop._reason_class` / `loop._mechanical_unknown_detail`
-- 18 land in `unknown` and none is recognised as mechanical -- and said the remedy depends on how
this goal surfaces a `False`, so it added no classifier entries for strings nothing could yet emit.

**The decision: nothing here ever parks, so no classifier entry is owed, and none was added.** A
`sibling_gate` refusal becomes one clause of prose on a line a human reads. It never reaches
`source.park()`, never strips `sdlc:goal`, never dequeues a goal, and never becomes a park `detail`
-- so it never reaches `_reason_class` at all, and adding needles to `work.MECHANICAL_PARK_PREFIXES`
for it would be classifying something that is not classified. That is #1474's own rule ("only once
something actually parks on them") honoured rather than quietly dropped, and it is PINNED rather
than promised: `test_a_not_ready_sibling_never_parks_the_goal_that_closed_the_unit` drives the real
`loop._record` done path with a refusing sibling and asserts `source.park` was not called and the
recorded outcome is still `done`. A later change that DOES park on one of these will fail that test,
and the obligation lands back where it belongs -- on whoever makes that change.

--------------------------------------------------------------------------------------------------
WHERE IT RUNS, AND WHY THERE

`loop._record(..., "done")`, at the end, after every piece of the goal's own bookkeeping. That is
the ONE moment "0 open issues" can newly become true: `source.complete(goal)` closed the issue three
lines earlier. `work.finish()` was the alternative and is wrong -- it refuses outright on the
documented `auto_merge: off` / fork / read-only paths, so on exactly those repos the signal would
never fire at all.

IT IS NOT ON A HOT PATH, which matters because `sibling_gate` can wait up to 450s on a sibling whose
own CI is still running. Nothing waits on this answer: the goal is over, its outcome is recorded,
and the loop's next pick is a separate call. The wait is also bounded by construction to at most
once per unit ever -- the sibling is only consulted after the board says zero open issues, which is
true for exactly one goal in a unit's life.

WHAT THE TALLY COUNTS, SAID PLAINLY BECAUSE IT IS THE ONE THING A READER WILL ASSUME WRONG. It is
the issues carrying `feature:<unit>` IN THIS REPO -- one REST read, with no cross-repo fan-out.
A cross-repo unit's other half is not counted and is not meant to be: that repo runs its own loop
and reaches its own completion, and the question "is the other side ready to land" is a different
question with a different answer, which is exactly the one `sibling_gate` gives. Counting both here
would put this repo's signal at the mercy of a board it may not even be able to read.

COST, exactly, AND ON WHICH BUDGET -- the unit `docs/branching-model.md` §6e asks for, because
GitHub meters REST and GraphQL on SEPARATE hourly buckets and an exhausted GraphQL quota has blocked
this loop before while REST still had headroom (#1209):

  | call                                          | transport | paid per                        |
  |-----------------------------------------------|-----------|---------------------------------|
  | `gh api repos/<slug>/issues?...&state=open`   | REST      | goal that FINISHES on a unit    |
  | `gh api repos/<slug>/issues?...&state=closed` | REST      | goal that CLOSES a unit         |
  | `work.sibling_gate`                           | `gh`      | goal that CLOSES a cross-repo unit |
  | `gh api repos/<slug>/pulls?head=...`          | REST      | `draft-pr` only, same goal      |
  | `gh api repos/<slug>/pulls` (create)          | REST      | `draft-pr` only, same goal      |

  Zero of any kind when the model is not adopted, when the goal is not on a `feature/<name>` base,
  or under `off`. EVERY call this module makes is REST, which is a deliberate choice and not an
  accident of style: `gh issue list`, `gh pr list` and `gh pr create` are all GraphQL operations
  under the hood, and the first of those would sit on the DEFAULT, always-on path while the last is
  precisely the mutation #1209 removed from `work.pr()`. `work.pr()`'s docstring carries the
  measurement; this module follows it rather than restating it.

  WHAT REST COSTS IN EXCHANGE, AND IT IS NOT ONE THING BUT TWO -- the second one shipped a real
  defect and is written out at `ISSUE_JQ` and `_issue_rows`. `GET /issues` returns PULL REQUESTS
  TOO, which `gh issue list` filtered for you; and it filtered them SERVER-SIDE, before `per_page`
  was applied, which a `--jq` filter cannot do. So the wrapper was doing two jobs and moving off it
  inherited both: drop the pull requests, AND do not let a client-side filter shrink an
  already-capped page into a false empty. `_issue_rows` filters in Python, reports whether the
  server's page was FULL, and `_signal` refuses rather than concluding when a full page leaves
  nothing behind it.

--------------------------------------------------------------------------------------------------
FAILS CLOSED, AND NEVER RAISES

An unreadable board claims nothing: no completion is reported and, in `draft-pr` mode, no pull
request is opened. Empty stdout is NOT an empty backlog -- `gh api --jq` prints `[]` for no results,
so silence is a failed read, and reading it as "no open issues" would declare every unit on the repo
complete the moment `gh` went quiet. `_landing_pull_requests` keeps the same distinction
`work._sibling_pull_requests` keeps one layer over: "GitHub said there is none" and "we could not
ask" are different answers, and only the first permits opening one.

AND A DECISION A HUMAN ALREADY TOOK IS NEVER OVERWRITTEN. The landing-PR read is `state=all`, so a
pull request somebody CLOSED -- "not yet, about this exact unit" -- is visible, and `draft-pr`
refuses rather than raising a fresh one over it. Reading only the OPEN ones cannot tell that
decision apart from "nobody ever opened one", which in the one module whose whole subject is that a
human decides is the worst available failure; it also defeats the idempotence guard's own purpose,
since `record done` has no once-only guard and the duplicate protection would vanish exactly once
the first landing PR was closed.

Totality is the same promise `feature_sync.sync_at_pick` and `work.sibling_gate` make, for the same
reason: a signal is a courtesy, and losing the goal's terminal bookkeeping because the courtesy blew
up would be a real loss. Everything resolves to a report; `FAILED` claims nothing.

Module shape follows `feature_sync.py` / `feature_registry.py`: zero third-party dependencies,
module-level constants, loaded by siblings via `_load("unit_completion")`.
"""
import importlib.util
import json
import pathlib
import re
import sys
import time
import urllib.parse

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


registry = _load("feature_registry")   # registry_dir / read / is_unit_name
features = _load("features")           # BRANCH_PREFIX -- the one rule for what a unit branch is
labels = _load("feature_labels")       # label_for -- the one rule for what a unit's LABEL is
ledger = _load("ledger")               # team record (config-gated, default OFF; fail-open)
upkeep = _load("feature_upkeep")       # the upkeep gate: pure, path-free, the only reader of its config block

#: `work` and `feature_sync` are loaded on FIRST USE, not at import. Lazy for the reason
#: `work._feature_sync()` is lazy -- `work` alone pulls `state`, `ledger`, `gh_session` and
#: `scrub` behind it -- and, more importantly, because `_load` has NO `sys.modules` cache: a module
#: -level `_load("work")` here would be a live cycle the moment anything in `work` ever loads this
#: file back. Deferring it means the edge only ever exists one way, at call time.
_WORK = None
_FEATURE_SYNC = None


def _work():
    global _WORK
    if _WORK is None:
        _WORK = _load("work")
    return _WORK


def _feature_sync():
    global _FEATURE_SYNC
    if _FEATURE_SYNC is None:
        _FEATURE_SYNC = _load("feature_sync")
    return _FEATURE_SYNC


#: The config key, under `work` -- beside `base` and `auto_merge`, which are the two settings it is
#: about: `base` is the branch the landing PR targets, and `auto_merge` is the neighbouring answer to
#: "may Sigma land a GOAL branch", which this one deliberately does not extend to a UNIT branch.
CONFIG_KEY = "unit_completion"

#: The three modes. See the module docstring for why only the exact literals select the non-defaults.
OFF, SURFACE, DRAFT_PR = "off", "surface", "draft-pr"
MODES = (OFF, SURFACE, DRAFT_PR)
DEFAULT_MODE = SURFACE

#: What one pass concluded about the unit.
NOT_ADOPTED = "not-adopted"   # no `.sdlc/features/` -- the branching model was never adopted here
NO_UNIT = "no-unit"           # this goal is not on a `feature/<unit>` base; there is no unit to ask about
DISABLED = "disabled"         # mode `off` -- not even the measurement
UNREADABLE = "unreadable"     # the board could not be read; NOTHING is claimed
INCONCLUSIVE = "inconclusive" # the board WAS read and one page cannot answer; NOTHING is claimed
INCOMPLETE = "incomplete"     # issues are still open under this unit (or there are none at all)
COMPLETE = "complete"         # zero open, at least one closed -- the signal this module exists for
FAILED = "failed"             # the pass itself went wrong; nothing here is claimed
OUTCOMES = (NOT_ADOPTED, NO_UNIT, DISABLED, UNREADABLE, INCONCLUSIVE, INCOMPLETE, COMPLETE, FAILED)
#: The two that claim nothing, kept as a pair because every consumer treats them alike and only a
#: reader cares which it was: `UNREADABLE` is "we could not ask", `INCONCLUSIVE` is "we asked, and
#: one page of a paged endpoint cannot answer". Collapsing them would lose the second's diagnosis.
CLAIMS_NOTHING = (UNREADABLE, INCONCLUSIVE)

#: What was DONE about a `COMPLETE` unit. `SURFACED` is the default and the only one that writes
#: nothing to GitHub; the other three are `draft-pr` mode's outcomes, and two of the four are
#: refusals, which is the ratio a mode that writes to somebody's repo ought to have.
SURFACED = "surfaced"             # said out loud, and nothing else. THE DEFAULT.
DRAFTED = "drafted"               # a draft landing PR was opened and parked for a human
DRAFT_EXISTS = "draft-exists"     # a PR on this head was already open; nothing was opened
DRAFT_DECLINED = "draft-declined" # a human already CLOSED or MERGED the landing PR; not re-raised
DRAFT_REFUSED = "draft-refused"   # draft mode asked for, and refused -- see `why`
ACTIONS = (SURFACED, DRAFTED, DRAFT_EXISTS, DRAFT_DECLINED, DRAFT_REFUSED)

#: THE PAGE SIZE, one number for both reads, at REST's own ceiling.
#:
#: ONE HUNDRED IS THE API'S OWN LIMIT, not a taste: `per_page` is capped at 100, and the only way
#: past it is `--paginate`, which fetches EVERY page -- an unbounded number of calls on the very
#: budget §6e asks this module to account for. One bounded page and an honest `N+` beats an exact
#: number nobody asked for at a cost nobody can predict.
#:
#: IT IS ALSO THE OVER-FETCH THAT MAKES THE VERDICT SOUND. The page bounds issues AND pull requests
#: together (see `ISSUE_JQ`), so a small page can be filled entirely by pull requests and leave no
#: issues behind it. Asking for the ceiling makes that vanishingly unlikely -- and `_issue_rows`
#: reports whether the page was FULL, so the remaining case is refused rather than guessed. An
#: earlier revision asked for five here, which is how a busy unit's landing pull requests could
#: read as an empty backlog.
#:
#: It is deliberately NOT the naming cap. Those are different questions and conflating them is what
#: made the report's numbers describe pull requests while claiming to describe issues.
PAGE_LIMIT = 100

#: The projection each read asks REST for, as a `--jq` program.
#:
#: IT PROJECTS; IT DOES NOT FILTER, AND THAT DIVISION IS THE WHOLE LESSON OF THIS FILE'S SECOND
#: REVIEW. An earlier revision put `select(.pull_request | not)` in here, and it was correct about
#: WHAT to drop and wrong about WHERE: `per_page` is applied by the SERVER and `--jq` runs on the
#: CLIENT, so the cap bounded issues-AND-PULL-REQUESTS and the filter then removed the pull requests
#: from an already-truncated page. A page that happened to be all pull requests read as an empty
#: backlog -- `open: 0`, `complete`, and in `draft-pr` a landing PR opened for an unfinished unit.
#: On the DEFAULT path, in the UNSAFE direction, which is the one thing this module exists to
#: forbid. `gh issue list --limit N` could not do that, because ITS filter ran server-side; moving
#: off the wrapper inherited both of its jobs and only one of them was noticed.
#:
#: So the boolean is passed THROUGH and the filtering happens in `_issue_rows`, in Python, where the
#: tests execute it. That is not merely tidier: a filter inside the `--jq` string is invisible to
#: every test in this suite, because the fake runner returns canned stdout and never runs jq -- a
#: harness structurally blind to an entire class of defect, which is how the first version shipped.
#:
#: `pull_request != null` RATHER THAN A DERIVED NAME, so the guard survives the projection being
#: changed or dropped entirely: if this program were deleted and `gh` returned raw issue objects,
#: `row["pull_request"]` would be the API's own object -- truthy -- and `_issue_rows` would still
#: drop it. A rename to `is_pr` would fail OPEN in exactly that case.
ISSUE_JQ = "[.[] | {number, title, pull_request: (.pull_request != null)}]"

#: The membership scan's own projection (#1570). `ISSUE_JQ` plus the two fields `features.read`
#: takes -- and it is a SECOND constant rather than a widening of the first because the two reads
#: ask different questions: the label read asks "is anything picked on this unit still open", and
#: this asks "does anything DECLARE this unit". Bodies are big, and the label read must not start
#: carrying every issue's prose to answer a question that never needed it.
#:
#: `labels` rides along because `features.read` is the DUAL reader -- body and label together, with
#: the body winning a conflict -- and handing it a payload with the label half missing would make
#: every `label_only` issue read as declaring nothing. REST's own `[{"name": ...}]` shape is passed
#: through raw; `features._label_name` reads it.
BACKLOG_JQ = "[.[] | {number, title, body, labels, pull_request: (.pull_request != null)}]"

#: HOW MANY PAGES OF THE OPEN BACKLOG THE MEMBERSHIP SCAN WILL READ BEFORE IT GIVES UP.
#:
#: `PAGE_LIMIT`'s comment rejects `--paginate` because it is an unbounded number of calls on the
#: budget §6e asks this module to account for. That reasoning is kept, not overturned: this walks
#: pages EXPLICITLY, stops the moment the server returns a short page, and REFUSES (`INCONCLUSIVE`)
#: rather than guessing if it runs out. Bounded, and the bound is stated here rather than implied.
#:
#: TWENTY IS CHOSEN AGAINST THE FAILURE, NOT AGAINST THE COST. Hitting the cap makes the signal go
#: quiet for that unit until the backlog shrinks, so the number has to be past any real one: 2,000
#: OPEN issues is far beyond the repositories this runs on (the kit's own board carries ~250). The
#: calls are only ever spent on the goal that would otherwise have CLAIMED completion -- see
#: `_declared_open` for the full cost -- so a generous cap buys correctness at a price nobody pays
#: on the common path.
BACKLOG_PAGES = 20

#: The landing PR projection. `state` and `merged_at` are what tell an OPEN conversation from one a
#: human ENDED, and `draft` is what `parked` is read off -- see `_draft`.
PR_JQ = "[.[] | {number, url: .html_url, draft, state, merged_at}]"

#: How many pull requests on one head this looks at. Small for `work.SIBLING_PR_LIMIT`'s reason: one
#: head can legitimately carry several, and the point of a cap is to SEE the second rather than
#: silently take the first.
PR_LIMIT = 5

#: How many issues the reason NAMES before it stops counting them out. The overflow is COUNTED, not
#: dropped (`feature_sync._CLAUSE_CAP`'s rule): a line that silently stopped at five would read the
#: same for five open issues and for forty. This is a rendering cap and nothing else -- it is
#: deliberately NOT the page size, which is what the earlier revision conflated.
NAMED_LIMIT = 5


def _note(message):
    """One stderr line, never an exception -- the shape `features._note`, `feature_registry._note`
    and `feature_sync._note` all hold, for the same reason: a diagnostic must never be the thing
    that breaks the caller."""
    try:
        sys.stderr.write(message)
    except Exception:                     # noqa: BLE001 - a diagnostic must never break a caller
        pass


# --------------------------------------------------------------------------- the mode


def mode(config):
    """`surface` (default) | `draft-pr` | `off`.

    TOTAL ON ANY INPUT, including a config that is not a mapping at all: this is called before the
    outer guard in `signal()` gets a chance to catch anything, and a mode reader that raises would
    turn a garbled config file into a crashed loop record."""
    work_cfg = config.get("work") if isinstance(config, dict) else None
    value = work_cfg.get(CONFIG_KEY) if isinstance(work_cfg, dict) else None
    if isinstance(value, str) and value in (OFF, DRAFT_PR):
        # Exact, case-sensitive, and not `.strip().lower()`: the point is that nothing REACHES
        # `draft-pr` by accident, and every normalisation is one more spelling that does.
        return value
    return DEFAULT_MODE


# --------------------------------------------------------------------------- the unit


def unit_of(rec):
    """Which unit this goal's work record says it is on -> a unit name, or None.

    READ, NEVER RE-RESOLVED. `work.start()` already asked the issue exactly once (#1467's single
    `gh api repos/<slug>/issues/<n>`) and persisted the answer as the record's `base`. Re-reading the
    issue here would be a second reader of one declaration -- the bug `cross_repo.unit_of`'s
    docstring records this epic already making once -- and it would cost a network call per goal.

    A sub-unit branch (`feature/<name>/<sub>`, which the epic allows to depth 3) resolves to None
    rather than to `<name>/<sub>`: `is_unit_name` rejects a `/`, the registry is keyed by unit name,
    and answering about the parent unit when the goal is on a child would be a guess. A goal on
    `base: main` -- every ordinary goal -- resolves to None the same way, at no cost."""
    base = (rec or {}).get("base") if isinstance(rec, dict) else None
    prefix = features.BRANCH_PREFIX
    if not isinstance(base, str) or not base.startswith(prefix):
        return None
    name = base[len(prefix):]
    return name if registry.is_unit_name(name) else None


# --------------------------------------------------------------------------- reads


#: What `gh api` substitutes when this module has no slug of its own: gh's own placeholders, filled
#: from the checkout's git remote. The same pattern `work.pr()` and `work.protection()` already use.
PLACEHOLDER_REPO = "{owner}/{repo}"
PLACEHOLDER_OWNER = "{owner}"


def _repo_ref(config, run, cwd, remote):
    """(repo_ref, owner_ref) -- what to put in a REST path, and in a `head=` filter.

    `feature_sync.repo_slug` IS THE RULE, called rather than re-derived: it prefers
    `discovery.github.repo` because that is the board the goal NUMBER came from (a checkout that is
    a fork of somewhere else does not change which board filed the work), and it refuses `gh`'s own
    `{owner}/{repo}` placeholder as a literal. An empty answer is not a failure -- gh's placeholders
    resolve the checkout's own remote, which is the same answer `repo_slug`'s own fallback reaches
    by parsing that remote itself."""
    slug = _feature_sync().repo_slug(config, run, cwd, remote)
    if not slug:
        return PLACEHOLDER_REPO, PLACEHOLDER_OWNER
    return slug, slug.split("/")[0]


def _rows(run, cwd, argv):
    """(rows, error) -- exactly one of which is set. Never raises.

    THE DISTINCTION THIS KEEPS is `work._sibling_pull_requests`'s, and it is kept here for a sharper
    reason: this function's caller reads "no open issues" as a unit being finished. An empty stdout
    from a dead `gh` read as an empty list would declare every unit on the repo complete at once."""
    try:
        out = run(cwd, argv)
    except Exception as exc:              # noqa: BLE001 - a failed read answered nothing
        return None, "could not be read (%s)" % " ".join(str(exc).split())
    if not (out or "").strip():
        return None, "could not be read (gh answered nothing at all)"
    try:
        rows = json.loads(out)
    except Exception:                     # noqa: BLE001 - an unreadable reply answered nothing
        return None, "could not be read (gh answered with something that is not JSON)"
    if not isinstance(rows, list):
        return None, "could not be read (gh answered with a %s, not a list)" % type(rows).__name__
    return rows, None


def _is_pull_request(row):
    """Is this `/issues` row actually a pull request?

    TRUTHINESS OF `pull_request`, deliberately, so this is right for BOTH shapes it can arrive in:
    the boolean `ISSUE_JQ` projects, and the API's own object if that projection is ever changed or
    dropped. A row this cannot read at all is treated as a pull request -- i.e. NOT counted as an
    open issue -- which is the fail-closed direction here, because an uncounted row can only make a
    unit look LESS finished."""
    if not isinstance(row, dict):
        return True
    return bool(row.get("pull_request"))


def _issue_rows(run, cwd, repo_ref, label, state, limit):
    """(issues, page_full, error) for the issues carrying `feature:<unit>` in ONE state.

    THE THREE-VALUED RETURN IS THE FIX FOR THIS MODULE'S WORST DEFECT, and every part of it earns
    its place:

      * `issues` is the rows that are NOT pull requests -- filtered HERE, in Python, where the tests
        execute the filter. See `ISSUE_JQ` for why it is not filtered in the `--jq` program;
      * `page_full` says the SERVER returned a full page, so rows exist that this never saw. It is
        computed on the RAW page, because that is what `per_page` bounded. Computing it on the
        survivors -- which the previous revision did -- makes it describe a list the server never
        capped, so a full page of 5 rows with 3 pull requests reported "2 open issues" and
        `truncated: false`: a truncated page rendered as an exact measurement;
      * `error` keeps `_rows`' distinction between "GitHub said there are none" and "we could not
        ask", which is the difference between an empty backlog and a dead `gh`.

    THE CALLER MUST NOT READ AN EMPTY `issues` AS "NONE EXIST" WHILE `page_full` IS TRUE. That is
    the whole hazard: `per_page` runs on the server and bounds issues-and-pull-requests together, so
    an empty remainder behind a full page proves nothing at all. `_signal` refuses there rather than
    guessing, and `PAGE_LIMIT` is at REST's ceiling so it essentially never has to.

    The label is percent-encoded because it goes in a query STRING and contains a `:`. Unit names
    are `[A-Za-z0-9._-]` by `features._UNIT_RE`, so today that colon is the only character this
    changes -- which is the reason to encode the whole label rather than that one character: the
    rule stays right if the name rule ever widens."""
    path = "repos/%s/issues?labels=%s&state=%s&per_page=%d" % (
        repo_ref, urllib.parse.quote(label, safe=""), state, limit)
    rows, error = _rows(run, cwd, ["gh", "api", path, "--jq", ISSUE_JQ])
    if error is not None:
        return None, False, error
    return [r for r in rows if not _is_pull_request(r)], len(rows) >= limit, None


def _landing_pull_requests(run, cwd, repo_ref, owner_ref, branch):
    """EVERY pull request on `feature/<unit>`, open or closed -- `state=all`, deliberately.

    THE CLOSED ONES ARE THE POINT, and reading only the open ones was a real defect this replaces.
    A landing PR a human CLOSED is that human saying "not yet" about this exact unit; a query that
    cannot see it cannot tell that decision apart from "nobody ever opened one", so the next goal
    to finish on the unit opened a fresh draft straight over it. In the one module whose entire
    subject is that a human decides, silently reversing the human's decision is the worst available
    failure -- and it also defeated the guard's own stated purpose, since `record done` has no
    once-only guard and the duplicate protection therefore vanished exactly when it was needed.

    `head=<owner>:<branch>` is REST's own fork-aware filter format, the same one `work.pr()` uses,
    and `work.pr()`'s reasoning carries over unchanged: this tool only ever pushes to its own
    remote, so both sides of that filter name the same owner."""
    path = "repos/%s/pulls?head=%s:%s&state=all&per_page=%d" % (
        repo_ref, urllib.parse.quote(owner_ref, safe="{}"),
        urllib.parse.quote(branch, safe="/"), PR_LIMIT)
    return _rows(run, cwd, ["gh", "api", path, "--jq", PR_JQ])


def _declared_open(run, cwd, repo_ref, unit):
    """Every OPEN issue whose DECLARATION puts it in `unit` -> `(members, ambiguous, capped, error)`.

    THE LABEL IS NOT THE MEMBERSHIP, AND THIS IS THE READ THAT SAYS SO (#1570). `feature:<unit>` is
    attached at PICK, so an issue that declares the unit in its body and has not been picked yet
    carries no label at all -- and the whole unpicked backlog was therefore invisible to a count
    keyed on the label. One finished goal could report a unit COMPLETE while the work plainly was
    not, which is the one claim in this module nobody re-checks. The model's own rule (§4c) is that
    on any disagreement THE BODY WINS, so the body is what is asked.

    ONE READER, NEVER A SECOND. Membership is answered by `features.read` -- the same function the
    pick path, the stamper and the cross-repo check ask -- and this file parses no marker of its
    own. A completion pass that re-implemented the body rule would be a copy free to drift from the
    thing it is supposed to agree with, and drifting on "what belongs to this unit" is exactly how
    the defect above got here.

    `features.read`'s own obligation is honoured: it PROPAGATES `AmbiguousUnit` for an issue that
    contradicts itself, and its docstring requires any sweep over many issues to catch that per
    issue rather than let one hand-edited issue take the whole pass down. Caught here, collected,
    and handed back separately -- the caller refuses on them instead of guessing, because an issue
    whose unit cannot be read is not evidence that this unit is finished.

    WHAT IT COSTS, STATED (§6e's unit of account):

      * NOTHING on the common path. The caller only reaches this after the label read came back
        empty, which is the pass that was about to claim COMPLETE -- a unit still being worked
        costs the one call it always cost.
      * `ceil(open issues / 100)` REST calls when it does run, capped at `BACKLOG_PAGES`. On the
        kit's own board (~250 open issues) that is three.
      * ZERO per-issue reads. `body` and `labels` come back INSIDE the list page, so widening the
        measure costs pages, not issues. That is the whole reason this is a list walk and not the
        obvious `gh issue view <n>` per candidate, which §6e already charges per goal CONSIDERED.
      * REST, never GraphQL -- the same transport #1209's doctrine pins the rest of this file to,
        and a different bucket from the 35-45 GraphQL calls a pick spends.

    THE SEARCH API WAS CONSIDERED AND REFUSED. `search/issues?q=...in:body` answers this in one
    call, but its index lags writes by seconds to minutes, so a just-filed issue is invisible to
    it -- and invisible reads as "no open members", which is the UNSAFE direction. Paying three
    calls to never claim a completion that is not true is the trade this module exists to make.

    `capped` is computed on the RAW page for `_issue_rows`' reason: `per_page` bounds issues AND
    pull requests together on the server, so a short page after filtering proves nothing while a
    short page from the server proves there is no page behind it."""
    members, ambiguous = [], []
    for page in range(1, BACKLOG_PAGES + 1):
        path = "repos/%s/issues?state=open&per_page=%d&page=%d" % (repo_ref, PAGE_LIMIT, page)
        rows, error = _rows(run, cwd, ["gh", "api", path, "--jq", BACKLOG_JQ])
        if error is not None:
            return None, None, False, error
        for row in rows:
            if _is_pull_request(row):
                continue
            try:
                verdict = features.read(row)
            except features.AmbiguousUnit:
                ambiguous.append(row)
                continue
            # Case-insensitively, for `features._single`'s own reason: GitHub label names are
            # case-insensitively unique, so two spellings of one unit are one unit.
            if verdict.unit and verdict.unit.lower() == unit.lower():
                members.append(row)
        if len(rows) < PAGE_LIMIT:
            return members, ambiguous, False, None
    return members, ambiguous, True, None


# --------------------------------------------------------------------------- surfacing


def _tell(sdlc_dir, unit, goal, why, to=None):
    """One stderr line always; one ledger entry when the ledger is on.

    `kind="note"`, never `handoff`, and the reason is `feature_sync._tell`'s verbatim:
    `backlog_check._ledger_signals` reads a hand-off as a real BLOCK, so telling somebody a unit
    looks finished that way would park the next goal that touches it. Addressed to the unit's owner
    (§7.3) when the registry names one -- `note` is personal unless it names a `to`, so an
    unaddressed note would be a message to the machine that wrote it."""
    _note("sigma: unit-completion: %s\n" % why)
    fields = {"why": why, "area": registry.REGISTRY_DIRNAME,
              "ref": "%s/%s" % (registry.REGISTRY_DIRNAME, unit)}
    if isinstance(to, str) and to.strip():
        fields["to"] = to
    ledger.safe_append(sdlc_dir, "note", goal, **fields)


def _named(row):
    """One issue as a human reads it: `#12 (its title)`, or `#12` when the title is unusable.

    The title comes out of `ISSUE_JQ`, which is the whole reason that projection asks for one. An
    earlier revision asked for a title, never read it, and justified its page size with a naming
    that did not happen -- a comment describing behaviour the code does not have."""
    if not isinstance(row, dict):
        return "(an entry that is not an issue)"
    number = row.get("number")
    title = row.get("title")
    # `isinstance(True, int)` is True in Python, so a bare int check renders `#True` for a row whose
    # `number` is a boolean. `feature_registry._goal` documents this exact trap and excludes bools
    # for the same reason; found here by a test written for the non-dict guard next door.
    ref = ("#%s" % number if isinstance(number, int) and not isinstance(number, bool)
           else "(an issue with no number)")
    return "%s (%s)" % (ref, " ".join(str(title).split())) if isinstance(title, str) and title.strip() else ref


def _names(report):
    """The open issues, named, with the overflow COUNTED rather than dropped.

    `feature_sync._clause`'s rule: a line that silently stopped at `NAMED_LIMIT` would read the same
    for five open issues and for forty, which is the under-reporting this module has already been
    caught doing once. The cap is a RENDERING cap and has nothing to do with the page size."""
    shown = report["open_issues"][:NAMED_LIMIT]
    more = len(report["open_issues"]) - len(shown)
    return ", ".join(shown) + (" +%d more" % more if more > 0 else "")


def _count(n, truncated, noun):
    """`1 issue` / `12 issues` / `100+ issues`.

    THE `+` IS THE POINT, and it applies to BOTH halves. A page-capped count printed as a bare
    number reads as a measurement -- which is what `PAGE_LIMIT`'s comment has always said, and what
    the open half used to do anyway. One function, so the two can no longer disagree.

    `truncated` NOW COMES FROM THE RAW PAGE, not from the survivors (`_issue_rows`), so `n` and the
    `+` describe the same thing: `n` issues were seen, and there may be more. The previous revision
    computed the flag on the filtered list, which made `5 open issues, truncated: false` the report
    for a full page holding three pull requests and two issues."""
    return "%s%s %s%s" % (n, "+" if truncated else "", noun, "" if n == 1 and not truncated else "s")


def _tally(report):
    """`0 open issues, 12 closed` -- the sentence the whole feature is a delivery mechanism for."""
    return "%s, %s closed" % (_count(report["open"], report["open_truncated"], "open issue"),
                              "%d+" % report["closed"] if report["truncated"] else report["closed"])


def _sibling_clause(report):
    """The sibling verdict as a clause, or "". Prose, never a code: the whole point of
    `sibling_gate` returning sentences is that the person reading this line can act on them."""
    sibling = report["sibling"]
    if not sibling:
        return ""
    return " Cross-repo: %s" % sibling["reason"]


def _draft_body(report, base):
    """What the draft says about itself. The first line is the one that matters and it is first on
    purpose -- a reviewer arriving at a Sigma-authored PR has to know, before anything else,
    that no machine decided this was finished and no machine will land it."""
    return ("Sigma did NOT decide this unit is finished, and will not merge this pull request.\n"
            "\n"
            "Unit `%s` — every issue carrying `%s` is closed (%s). This pull request was opened as a "
            "DRAFT by Sigma's unit-completion signal (`work.unit_completion: \"draft-pr\"`) and "
            "is parked: readying it, reviewing it and merging it are yours.\n"
            "\n"
            "Landing `%s` → `%s`.%s\n"
            % (report["unit"], labels.label_for(report["unit"]), _tally(report),
               features.BRANCH_PREFIX + report["unit"], base, _sibling_clause(report)))


# --------------------------------------------------------------------------- the pass


def _report(goal, mode_):
    """The pass's whole account of itself.

    `open_issues` NAMES what is still open, which is what the page size's own comment always claimed
    it was for and what the CLI verb's reader actually wants. `open_truncated` sits beside
    `truncated` for the closed half deliberately: a count capped at a page boundary and reported as
    a bare number reads as a measurement, and that is as true of `open` as of `closed`. BOTH FLAGS
    ARE COMPUTED ON THE SERVER'S RAW PAGE, because `per_page` is what bounded it -- see
    `_issue_rows`.

    THERE IS NO `note` FIELD. An earlier revision carried one -- the one-line clause `feature_sync`
    hands back for `work.start()` to append -- and nothing here has that caller: `loop._record`
    discards the report and the CLI dumps the whole dict, so it was set once and read by nobody."""
    return {"outcome": NOT_ADOPTED, "action": None, "mode": mode_, "goal": goal, "unit": None,
            "open": None, "open_issues": [], "open_truncated": False, "ambiguous": [],
            "closed": None, "truncated": False, "sibling": None, "pr": None,
            "parked": False, "why": ""}


def signal(sdlc_dir, config, goal, run=None, sleep=time.sleep):
    """Has this goal's unit of work just become complete, and what should be said about it?

    -> a report dict; see `OUTCOMES` and `ACTIONS`. NEVER RAISES, TOTALLY -- the outer guard is what
    makes that a promise rather than an aspiration, exactly as in `work.sibling_gate` and
    `feature_sync.sync_at_pick`. `run` and `sleep` are injection points for tests.

    NOTHING IT RETURNS IS A GATE. No caller is expected to branch on the outcome, and the two that
    exist (`loop._record` and the CLI verb) do not: the report is for a human and for a test."""
    report = _report(goal, mode(config))
    try:
        return _signal(sdlc_dir, config, goal, run, sleep, report)
    except Exception as exc:              # noqa: BLE001 - "never raises" has to be total
        report["outcome"] = FAILED
        report["why"] = " ".join(str(exc).split())
        _note("sigma: unit-completion: the completion check for %s did not run (%s); nothing is "
              "claimed about any unit.\n" % (goal, report["why"]))
        return report


def _signal(sdlc_dir, config, goal, run, sleep, report):
    """The body `signal` wraps. Split so the totality guard above has nothing to skip."""
    if report["mode"] == OFF:
        # THE CHEAPEST STEP IS THE OPT-OUT, the order `feature_sync._sync_at_pick` and
        # `cross_repo._check_at_pick` both open with -- ahead even of the disk check below.
        report["outcome"] = DISABLED
        return report
    features_dir = registry.registry_dir(sdlc_dir)
    if not features_dir.is_dir():
        report["outcome"] = NOT_ADOPTED   # the branching model was never adopted here
        return report
    work = _work()
    rec = work._record(sdlc_dir, goal)
    unit = unit_of(rec)
    if not unit:
        report["outcome"] = NO_UNIT       # an ordinary goal on the configured base
        return report
    report["unit"] = unit

    run = run or work._run
    settings = work.settings(config)
    # The worktree when there is one, else the project root -- `work.root`'s own rule. `cwd` matters
    # here only when this module has NO slug of its own, in which case gh's `{owner}/{repo}`
    # placeholders resolve against the checkout's remote; a goal whose worktree has already been
    # released still has a project root to ask from.
    cwd = work.root(sdlc_dir, goal, config)
    repo_ref, owner_ref = _repo_ref(config, run, cwd, settings["remote"])
    label = labels.label_for(unit)

    # THE LABEL READ IS A BOUND, NOT THE MEMBERSHIP (#1570). It stays first because it is one call
    # and it answers the common case -- a unit still being worked -- without carrying a single
    # issue body. It can only ever make a unit look LESS finished than it is: an issue labelled for
    # this unit whose BODY declares another belongs to the other one (§4c, body wins) and is
    # counted here anyway, which holds a unit open that could have closed. That is the direction
    # this module is allowed to be wrong in. The direction it is NOT allowed to be wrong in --
    # claiming a completion -- is what `_declared_open` below is for, and no COMPLETE is reported
    # without it.
    rows, page_full, error = _issue_rows(run, cwd, repo_ref, label, "open", PAGE_LIMIT)
    if error is not None:
        report["outcome"] = UNREADABLE
        report["why"] = ("the open issues under `%s` %s — refusing rather than reporting a "
                         "completion nothing measured" % (label, error))
        _note("sigma: unit-completion: %s\n" % report["why"])
        return report
    report["open"] = len(rows)
    report["open_truncated"] = page_full
    report["open_issues"] = [_named(r) for r in rows]
    if rows:
        # ANSWERED, AND THE ANSWER IS NO. The common case, and it stops here at one call: no closed
        # tally, no sibling read, and nothing said -- a line per goal about a unit that is plainly
        # still being worked is how a channel gets ignored. Definitive whatever the page held: a
        # surviving issue is an open issue, and no cap can hide one it already returned.
        report["outcome"] = INCOMPLETE
        report["why"] = ("unit `%s` still has %s: %s"
                         % (unit, _count(len(rows), page_full, "open issue"), _names(report)))
        return report
    if page_full:
        # A FULL PAGE WITH NO ISSUES LEFT IN IT PROVES NOTHING, and this is the guard the whole
        # cap-then-filter defect turns on. `per_page` runs on the SERVER and bounds issues AND pull
        # requests together, so "every row on the page was a pull request" is not "there are no open
        # issues" -- the issues may simply be on page two. Claiming completion here is the exact
        # unsafe direction this module exists to forbid, and in `draft-pr` it opens a pull request
        # for a unit that is still being worked.
        report["outcome"] = INCONCLUSIVE
        report["why"] = ("the first %d rows carrying `%s` were all pull requests, so whether any "
                         "open issue is behind them is not knowable from one page — claiming "
                         "nothing" % (PAGE_LIMIT, label))
        _note("sigma: unit-completion: %s\n" % report["why"])
        return report

    # NOTHING PICKED ON THIS UNIT IS STILL OPEN. That is NOT "this unit is finished" (#1570): the
    # label is attached at pick, so everything nobody has picked yet is invisible to the read above.
    # The measure the model actually defines -- the body, §4c -- is asked here, and only here, on
    # the one path that was about to claim a completion.
    members, ambiguous, capped, error = _declared_open(run, cwd, repo_ref, unit)
    if error is not None:
        report["outcome"] = UNREADABLE
        report["why"] = ("the open backlog %s — refusing rather than reporting a completion "
                         "nothing measured" % error)
        _note("sigma: unit-completion: %s\n" % report["why"])
        return report
    if capped:
        report["outcome"] = INCONCLUSIVE
        report["why"] = ("the open backlog is longer than the %d issues this reads, so whether an "
                         "unpicked issue declares unit `%s` is not knowable — claiming nothing"
                         % (BACKLOG_PAGES * PAGE_LIMIT, unit))
        _note("sigma: unit-completion: %s\n" % report["why"])
        return report
    if ambiguous:
        # AN ISSUE THAT CONTRADICTS ITSELF HAS NO UNIT, so it cannot be counted in and it cannot be
        # counted out. Refusing is the only honest answer and it is also the safe one -- it leaves
        # the unit open -- and it is reached only on the pass that was about to claim COMPLETE, not
        # on every goal. The issues are NAMED, because one human edit clears it.
        report["ambiguous"] = [_named(r) for r in ambiguous]
        report["outcome"] = INCONCLUSIVE
        report["why"] = ("%s declare more than one unit, so whether they belong to `%s` cannot be "
                         "read — claiming nothing until that is edited: %s"
                         % (_count(len(ambiguous), False, "open issue"), unit,
                            ", ".join(report["ambiguous"][:NAMED_LIMIT])))
        _note("sigma: unit-completion: %s\n" % report["why"])
        return report
    if members:
        # THE UNPICKED BACKLOG, which the label read structurally could not see.
        report["open"] = len(members)
        report["open_issues"] = [_named(r) for r in members]
        report["outcome"] = INCOMPLETE
        report["why"] = ("unit `%s` still has %s nothing has picked yet: %s"
                         % (unit, _count(len(members), False, "open issue"), _names(report)))
        return report

    # THE CLOSED HALF IS STILL COUNTED BY LABEL, DELIBERATELY, AND IT ERRS TOWARDS LEAVING THE UNIT
    # OPEN. It decides only "empty label or finished work", never "complete", so undercounting it
    # costs a completion that is not announced -- §8f's own approximation ("no open goals" read as
    # "no goals recorded ... it errs towards leaving a unit open"), applied to the same question.
    # Scanning the CLOSED backlog by body would be unbounded in a way the open half is not: a
    # long-lived repository's closed issues outnumber its open ones by an order of magnitude, and
    # `BACKLOG_PAGES` would be reached on repositories where the open scan is three calls. So the
    # two halves count different populations, on purpose, and the asymmetry is in the safe
    # direction: a unit worked entirely without labels reports INCOMPLETE rather than COMPLETE.
    closed, closed_full, error = _issue_rows(run, cwd, repo_ref, label, "closed", PAGE_LIMIT)
    if error is not None:
        report["outcome"] = UNREADABLE
        report["why"] = ("the closed issues under `%s` %s — an unreadable half is not a tally"
                         % (label, error))
        _note("sigma: unit-completion: %s\n" % report["why"])
        return report
    report["closed"] = len(closed)
    report["truncated"] = closed_full
    if not closed:
        if closed_full:
            # Same guard, same reason, on the half that decides "empty label" rather than the half
            # that decides "complete". A page full of closed pull requests is not an empty label.
            report["outcome"] = INCONCLUSIVE
            report["why"] = ("the first %d closed rows carrying `%s` were all pull requests, so "
                             "whether this unit has any closed issues is not knowable from one "
                             "page — claiming nothing" % (PAGE_LIMIT, label))
            _note("sigma: unit-completion: %s\n" % report["why"])
            return report
        # ZERO OPEN AND ZERO CLOSED IS AN EMPTY LABEL, not a finished body of work. Firing here would
        # announce the completion of every unit the moment its label was created.
        report["outcome"] = INCOMPLETE
        report["why"] = ("unit `%s` has no issues at all under `%s`, which is an empty label rather "
                         "than finished work" % (unit, label))
        return report

    report["outcome"] = COMPLETE
    # #1474's check, consumed HERE and nowhere else, and REPORTED rather than obeyed -- see the
    # module docstring for why gating on it rebuilds the symmetric deadlock it was unwired over.
    ok, why = work.sibling_gate(sdlc_dir, config, goal, run=run, sleep=sleep)
    if why:                               # `(True, "")` means "did not apply"; say nothing about it
        report["sibling"] = {"ok": ok, "reason": why}
    return _act(sdlc_dir, config, goal, run, cwd, repo_ref, owner_ref, settings, report)


def _act(sdlc_dir, config, goal, run, cwd, repo_ref, owner_ref, settings, report):
    """What is DONE about a complete unit. `surface` writes nothing to GitHub; `draft-pr` opens one
    draft, or refuses and says why."""
    unit = report["unit"]
    branch = features.BRANCH_PREFIX + unit
    entry = registry.normalise_entry(registry.read(registry.registry_dir(sdlc_dir)).get(unit))
    if report["mode"] == DRAFT_PR:
        _draft(run, cwd, repo_ref, owner_ref, settings, branch, report, sdlc_dir=sdlc_dir,
               config=config, goal=goal)
    else:
        report["action"] = SURFACED
        report["why"] = ("unit `%s` looks complete — %s. Sigma does not decide a unit is "
                         "finished: raising the `%s` → `%s` pull request is yours.%s"
                         % (unit, _tally(report), branch, settings["base"] or "the integration "
                            "branch", _sibling_clause(report)))
    # NO `report["note"]` IS SET HERE. An earlier revision set one and `_report`'s docstring said in
    # capitals that the field did not exist -- a function contradicting its own docstring three lines
    # of prose apart. The field went because nothing consumes it: `loop._record` discards the report
    # and the CLI dumps the whole dict; `feature_sync`'s `note` exists because `work.start()` appends
    # it to its own line, and this has no such caller.
    _tell(sdlc_dir, unit, goal, report["why"], to=entry.get("owner"))
    return report


def _pick_landing(rows):
    """(open_pr, ended_pr) out of every PR on the head -- exactly one of which the caller acts on.

    AN OPEN CONVERSATION OUTRANKS AN ENDED ONE, which is the only ordering that can be right: if a
    landing PR is open right now, that is the live state whatever happened before it. Only when
    none is open does a closed one become the answer, and then it is the answer this module must
    not talk over."""
    open_pr = merged_pr = closed_pr = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("state") == "open":
            open_pr = open_pr or row
        elif row.get("merged_at"):
            merged_pr = merged_pr or row
        elif closed_pr is None:
            closed_pr = row
    # A MERGED ROW OUTRANKS A MERELY CLOSED ONE, and the tier is not decorative. A head can carry
    # both -- an abandoned attempt and the one that actually landed -- and taking whichever came
    # first in the reply would report "closed without merging" about a unit that HAS landed, chosen
    # by row order on an endpoint this module sets no `sort` or `direction` on. The two were split
    # precisely because they are different news to a person; letting an unpinned API default decide
    # which one a person hears undoes that.
    return open_pr, (merged_pr or closed_pr)


def _pr_ref(row):
    return row.get("url") or "#%s" % row.get("number")


def _record_unit_landing_merge(run, cwd, sdlc_dir, config, goal, unit, repo_ref, branch, pr_number):
    """Observe an already-merged unit landing PR, exactly once. Gated identically to work.py's
    `_record_confirmed_merge` -- ledger.enabled or journal_on, never receipt ownership -- and
    shares verify_merge.py's `state/feature-merge-deliveries/<entry_key>.json` idempotency file,
    so the same PR merge is recorded once however it is first observed: by this module, or by a
    human running `/sigma-rebase` on the same landing."""
    if not (ledger.enabled(config) or ledger.journal_on(sdlc_dir, config)):
        return None
    work_module = _work()
    receipt = _load("merge_observation")
    try:
        raw = json.loads(run(cwd, ["gh", "api", "repos/%s/pulls/%s" % (repo_ref, pr_number)]))
        merge_sha, merged_at = raw.get("merge_commit_sha"), raw.get("merged_at")
        if (raw.get("number") != int(pr_number) or not merged_at
                or not isinstance(merge_sha, str)
                or not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", merge_sha)):
            raise ValueError("canonical PR merge facts are incomplete")
        gated = upkeep.enabled(config or {})
        owner = receipt.landing_owner_id(branch) if gated else unit
        facts = work_module._receipt_parent_facts(raw, goal or unit, "unit", owner,
                                                   "unit_completion._draft")
        ownership = receipt.ownership_key(facts)
        entry_key, observation_key = receipt.observation_keys(ownership, merge_sha)
        if gated and owner != unit and not (pathlib.Path(sdlc_dir) / "state" / "feature-merge-deliveries"
                                            / (entry_key + ".json")).exists():
            # Legacy lookup: a landing already recorded under the bare-unit key keeps that identity, so it is
            # finished there (a no-op when complete) and never recorded a second time under the branch key.
            legacy = dict(facts, owner_id=str(unit))
            legacy_ownership = receipt.ownership_key(legacy)
            legacy_entry, legacy_observation = receipt.observation_keys(legacy_ownership, merge_sha)
            if (pathlib.Path(sdlc_dir) / "state" / "feature-merge-deliveries" / (legacy_entry + ".json")).exists():
                ownership, entry_key, observation_key = legacy_ownership, legacy_entry, legacy_observation
    except Exception as exc:                        # noqa: BLE001 - best-effort observation
        print("unit landing merge observation facts pending: %s" % exc, file=sys.stderr)
        return None
    entry_required, journal_required = ledger.enabled(config), ledger.journal_on(sdlc_dir, config)
    path = pathlib.Path(sdlc_dir) / "state" / "feature-merge-deliveries" / (entry_key + ".json")
    try:
        delivery = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        delivery = {}
    if delivery.get("entry_key") not in (None, entry_key) or delivery.get("observation_key") not in (None, observation_key):
        return None
    delivery.update({"unit": str(unit), "pr": int(pr_number), "ownership_key": ownership,
                     "merge_sha": merge_sha, "entry_key": entry_key, "observation_key": observation_key,
                     "entry_delivered": bool(delivery.get("entry_delivered")) or not entry_required,
                     "journal_delivered": bool(delivery.get("journal_delivered")) or not journal_required})
    work_module._try_write_merge_delivery(path, delivery)
    if not delivery["entry_delivered"]:
        try:
            if ledger.safe_append(sdlc_dir, "merged", branch, config=config, pr=str(pr_number),
                                  why="unit landing PR #%s merged" % pr_number,
                                  merged_entry_key=entry_key) is not None:
                delivery["entry_delivered"] = True
                work_module._try_write_merge_delivery(path, delivery)
        except Exception as exc:                    # noqa: BLE001 - best-effort observation
            print("unit landing merge entry delivery pending: %s" % exc, file=sys.stderr)
    if not delivery["journal_delivered"]:
        try:
            if ledger.safe_append(sdlc_dir, "merge_observed", branch, config=config, stream=ledger.EVENTS,
                                  observation_key=observation_key, subject_kind="branch", subject=str(branch),
                                  pr=int(pr_number), merge_sha=merge_sha) is not None:
                delivery["journal_delivered"] = True
                work_module._try_write_merge_delivery(path, delivery)
        except Exception as exc:                    # noqa: BLE001 - best-effort observation
            print("unit landing merge journal observation pending: %s" % exc, file=sys.stderr)
    return raw


def _draft(run, cwd, repo_ref, owner_ref, settings, branch, report, sdlc_dir=None, config=None, goal=None):
    """Open the landing pull request AS A DRAFT, once, and park it.

    THE DRAFT IS THE PARK. GitHub refuses to merge a draft at the merge ENDPOINT -- a mechanism, not
    a policy, and auto-merge cannot be armed on one either -- so there is no state to add and no
    label to invent. Inventing an `sdlc:` label meaning "parked" would have added one more
    authoritative state with no reconciler behind it, which is a failure this repo has already paid
    for once. Readying it is the human gesture that ends the park, and nothing here can perform it.

    FOUR WAYS IT REFUSES, and every one of them still surfaces the signal:
      * no `work.base` -- "whichever branch the loop was started on" is not a pull request's base,
        and guessing the repo's default branch would target a unit's whole body of work at whatever
        `main` happens to be. Fail closed;
      * the head PR list could not be READ -- "could not ask" is not "there is none", and treating
        it as none is exactly how the duplicate gets opened;
      * a PR already OPEN on this head -- idempotence, and not cosmetic: this fires on the goal that
        closes a unit, and `record done` has no once-only guard, so a re-picked goal or a resumed
        loop would otherwise stack duplicates;
      * a landing PR a human already ENDED -- closed, or merged. See `_landing_pull_requests` for
        why this one is the whole reason the read is `state=all`."""
    base = settings["base"]
    if not (isinstance(base, str) and base.strip()):
        report["action"] = DRAFT_REFUSED
        report["why"] = ("unit `%s` looks complete — %s, but `work.base` is empty, so which branch "
                         "`%s` lands on is a guess — opening no pull request. Set `work.base`, or "
                         "raise it yourself.%s"
                         % (report["unit"], _tally(report), branch, _sibling_clause(report)))
        return
    rows, error = _landing_pull_requests(run, cwd, repo_ref, owner_ref, branch)
    if error is not None:
        report["action"] = DRAFT_REFUSED
        report["why"] = ("unit `%s` looks complete — %s, but the pull requests on `%s` %s — "
                         "opening none rather than risking a duplicate.%s"
                         % (report["unit"], _tally(report), branch, error, _sibling_clause(report)))
        return
    open_pr, ended_pr = _pick_landing(rows)
    if open_pr is not None:
        report["action"] = DRAFT_EXISTS
        report["pr"] = _pr_ref(open_pr)
        # `parked` IS THE PR'S OWN `draft` FLAG, NOT A CONSTANT. The PR found here need not be the
        # one this feature opened: a human may already have raised it, or readied a draft this
        # feature opened last week. Reporting `parked: True` for a ready pull request under review
        # would be this module claiming a state it neither created nor can see -- and `parked` is
        # the field a reader uses to decide whether anything still waits on a person.
        report["parked"] = open_pr.get("draft") is True
        report["why"] = ("unit `%s` looks complete — %s, and `%s` already has an open %s pull "
                         "request (%s) — leaving it alone.%s"
                         % (report["unit"], _tally(report), branch,
                            "draft" if report["parked"] else "ready", report["pr"],
                            _sibling_clause(report)))
        return
    if ended_pr is not None:
        # A HUMAN ALREADY ANSWERED THIS QUESTION. Merged means the unit landed and there is nothing
        # left to raise; closed unmerged means somebody looked at exactly this landing and said not
        # yet. Both are decisions, and re-raising either is this module overruling the person it
        # exists to defer to -- so ONE action covers both, because nothing a consumer does differs
        # between them, while the reason names which it was, because a human reading the line cares.
        merged = bool(ended_pr.get("merged_at"))
        # Round-8 review finding: a unit landing merged outside `/sigma-rebase` (a human merged it
        # by hand, or `_draft` re-runs and finds it already landed) never recorded the `merged`
        # entry / `merge_observed` event this whole goal exists to write. Plan-committed, flagged
        # blocking in plan-review R8/R9, shipped unwired -- wired here, same-skill reuse of
        # merge_observation's key derivation and work.py's delivery-write helper, gated exactly
        # like every other merge-observation call site: ledger.enabled/journal_on, never receipt
        # ownership (cycle-6 ruling).
        if merged and sdlc_dir is not None:
            _record_unit_landing_merge(run, cwd, sdlc_dir, config, goal, report["unit"], repo_ref,
                                       branch, ended_pr.get("number"))
        report["action"] = DRAFT_DECLINED
        report["pr"] = _pr_ref(ended_pr)
        report["why"] = ("unit `%s` looks complete — %s, and its landing pull request (%s) was "
                         "already %s — opening no new one, because that was somebody's decision "
                         "and reversing it is not this module's to do.%s"
                         % (report["unit"], _tally(report), report["pr"],
                            "merged" if merged else "closed without merging",
                            _sibling_clause(report)))
        return
    title = "%s: unit of work looks complete (%s)" % (report["unit"], _tally(report))
    # REST, never `gh pr create` -- the mutation #1209 removed from `work.pr()` for the separate
    # GraphQL budget. `-f` for the string fields and `-F` for `draft`, and the split is not
    # cosmetic: gh's `-F` gives `true` its JSON boolean type, while `-f` would send the STRING
    # "true" for a field the API specifies as a boolean. `--jq .html_url` reads the url straight
    # off the create's own reply, so opening the PR and learning its address is one round trip.
    argv = ["gh", "api", "repos/%s/pulls" % repo_ref,
            "-f", "title=%s" % title, "-f", "body=%s" % _draft_body(report, base),
            "-f", "base=%s" % base, "-f", "head=%s" % branch, "-F", "draft=true",
            "--jq", ".html_url"]
    try:
        out = run(cwd, argv)
    except Exception as exc:              # noqa: BLE001 - a refused create is a note, never a crash
        report["action"] = DRAFT_REFUSED
        report["why"] = ("unit `%s` looks complete — %s, but the draft pull request could not be "
                         "opened (%s) — raising it is yours.%s"
                         % (report["unit"], _tally(report), " ".join(str(exc).split()),
                            _sibling_clause(report)))
        return
    # Receipt sharing turns a created draft into a durable Sigma-owned PR only after the
    # canonical REST response has been published to the shared authority.  The create reply is a
    # URL, so re-read the PR object rather than constructing ownership from that mutable string.
    if sdlc_dir is not None and _work()._receipt_sharing_enabled(config or {}):
        number = (out or "").rstrip("/").split("/")[-1]
        try:
            raw = json.loads(run(cwd, ["gh", "api", "repos/%s/pulls/%s" % (repo_ref, number)]))
            owner = (_load("merge_observation").landing_owner_id(branch)
                     if upkeep.enabled(config or {}) else report["unit"])
            facts = _work()._receipt_parent_facts(raw, goal or report["unit"], "unit", owner,
                                                   "unit_completion._draft")
            status = _work()._publish_parent_receipt(sdlc_dir, config, facts)
        except Exception as exc:
            report["action"] = DRAFT_REFUSED
            report["why"] = "unit draft receipt pending: %s" % exc
            return
        if status != "receipt published":
            report["action"] = DRAFT_REFUSED
            report["why"] = "unit draft receipt pending: %s" % status
            return
    report["action"] = DRAFTED
    report["parked"] = True
    report["pr"] = (out or "").strip().splitlines()[-1].strip() if (out or "").strip() else None
    report["why"] = ("unit `%s` looks complete — %s. Opened %s as a DRAFT (`%s` → `%s`) and parked "
                     "it: Sigma never readies or merges it.%s"
                     % (report["unit"], _tally(report), report["pr"] or "the landing pull request",
                        branch, base, _sibling_clause(report)))


# --------------------------------------------------------------------------- CLI


USAGE = "usage: unit_completion.py check <sdlc_dir> <goal>"


def main(argv):
    """`unit_completion.py check <sdlc_dir> <goal>` -- run the completion signal for one goal.

    THERE IS NO `--surface-only` FLAG, and the omission is `cross_repo.main`'s and
    `feature_sync.main`'s: the mode is the project's decision, recorded in its config, and a second
    way to run the check is a second answer to a question that has one."""
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    if len(argv) >= 4 and argv[1] == "check":
        state = _load("state")
        report = signal(argv[2], state.load_config(argv[2]), argv[3])
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
