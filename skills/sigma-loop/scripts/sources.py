"""Pluggable backlog sources for the loop. A source abstracts WHERE goals come from and how their
status transitions are recorded, behind four ops: next_pending / mark_in_progress / complete / park.

- LocalSource  — goal markdown files under .sdlc/goals/ (zero-dep; the default).
- GitHubSource — open GitHub issues labelled `sdlc:goal`, via the `gh` CLI. Status maps to labels:
  in-progress -> add `sdlc:in-progress`; done -> close the issue; parked -> add `sdlc:parked` + a
  comment (the GitHub equivalent of the review queue). Requires `gh` installed + authenticated.

GitHubSource reaches GitHub only through an injectable `run` callable, so it is unit-testable
without the network or `gh`.
"""
import json, pathlib, importlib.util, re, sys, time
from datetime import datetime, timezone

_UNRESOLVED = object()   # #1216: "not yet resolved", distinct from a resolved None


def _logins(assignees):
    """Casefolded logins from EITHER assignee shape the two GitHub surfaces return.

    They genuinely differ, and mixing them up silently matches nothing:
      `gh issue list --json assignees`  -> [{"login": "me"}]   (objects)
      `gh project item-list --format json` -> ["me"]           (bare strings)

    Casefolded because GitHub logins are case-insensitive for identity while both APIs return
    the CANONICAL casing -- comparing a hand-written config value exactly would silently empty
    a backlog (#1216). Absent/empty is `[]` on both surfaces: `gh` OMITS the key entirely on a
    project item with no assignees."""
    out = set()
    for a in assignees or []:
        if isinstance(a, dict):
            a = a.get("login")
        if a:
            out.add(str(a).casefold())
    return out


_REST_PAGE_SIZE = 100    # GitHub REST's own per-page ceiling for the issues-list endpoint


def fetch_issues_rest(run, repo, labels, cap, state="open", sort="created", direction="asc",
                       assignee=None, page_size=_REST_PAGE_SIZE):
    """#1829/#1833: paginated `gh api repos/{owner}/{repo}/issues` GET — the shared REST-fetch
    primitive every graphql-search-billed `gh issue list --label ...` (or bare `--search`) call
    site in this kit migrates to. `GitHubSource._fetch_issues_rest` below is a thin wrapper over
    this exact function, bound to one instance's `self._run`/`self.repo`; `mirror.py`, `triage.py`
    and `status.py` call it directly with their OWN injectable `run` and configured `repo`, since
    none of them has (or should acquire) a `GitHubSource` instance just to reach this.

    ANDs `labels` (comma-joined — GitHub REST's own AND semantics, verified live identical to
    repeated `--label` flags on the `gh issue list` this replaces) onto the query when `labels` is
    non-empty, and omits the field entirely when it is — sending `labels=` as an empty string is
    NOT the same query to GitHub as sending no `labels=` field at all, and an unscoped caller
    (`triage.py`'s campaign survey, which filters by nothing but state/assignee) needs the latter.
    `state`/`sort`/`direction` are REST parameters native to this endpoint, so no `--search
    "sort:X"` qualifier (itself always graphql-search-billed, label or no label — it IS the search
    field, by construction) is ever needed for ordering OR for state.

    WHY NOT `gh issue list` (what this replaces): ANY `--label` flag makes `gh` route the call
    through GitHub's `search()` GraphQL field regardless of whether `--search` is also given
    (confirmed live with `GH_DEBUG=api` — see the research for #1829 (REST backlog pick)), billed
    against the shared 5000/hour `graphql` resource at ~2600+ points per call. This REST call is
    confirmed billed against the separate `core` resource instead, at ~1 point.

    MUST pass `--method GET` explicitly on every call: `gh api` defaults to POST the instant any
    `-f` field is given (verified live against a real, safe GET-only endpoint) — omitting this
    against a real issues-list endpoint would attempt to CREATE an issue.

    REPO RESOLUTION: an empty/falsy `repo` uses the literal `{owner}/{repo}` placeholder, which
    `gh api` resolves from the working directory's git remote at zero extra requests (confirmed
    live) — the same meaning an unset `--repo` already carried for `gh issue list`.

    Filters any item carrying a `pull_request` key: REST's issues-list endpoint returns PRs too
    (GitHub represents a PR as an issue for this endpoint); the graphql `search()` query this
    replaces always carried an implicit `type:issue` qualifier that excluded them (confirmed
    live). Uses the PRE-filter page length to decide whether to fetch another page, so a page that
    happens to be mostly PRs can never look short and truncate pagination early.

    `per_page` IS FIXED FOR THE WHOLE CALL, computed once from `cap` before the loop starts — see
    `GitHubSource._fetch_issues_rest`'s own docstring (this note predates the #1833 extraction and
    still names the exact live-reproduced bug this guards against) for why recomputing it from the
    FILTERED count silently re-indexes into the wrong raw offset the moment one page contains a
    filtered item.

    Raises on the first transport failure or unparseable response — does not retry internally.
    Retrying (when a caller wants it) is that caller's own concern, exactly as it was before this
    was extracted out of `GitHubSource`."""
    endpoint = "repos/%s/issues" % repo if repo else "repos/{owner}/{repo}/issues"
    per_page = min(page_size, cap) if cap else page_size
    collected = []
    page = 1
    while len(collected) < cap:
        args = ["api", endpoint, "--method", "GET"]
        if labels:      # ORDER PRESERVED from the pre-#1833 method: labels first, right after
            args += ["-f", "labels=" + ",".join(labels)]   # --method GET, exactly as every
        args += ["-f", "state=%s" % state, "-f", "sort=%s" % sort,           # existing caller's
                 "-f", "direction=%s" % direction, "-f", "per_page=%d" % per_page,   # tests (some
                 "-f", "page=%d" % page]                                    # exact-list) expect
        if assignee:
            args += ["-f", "assignee=%s" % assignee]
        raw = run(args)
        page_items = json.loads(raw or "[]")
        got = len(page_items)
        collected.extend(i for i in page_items if isinstance(i, dict) and "pull_request" not in i)
        if got < per_page:
            break
        page += 1
    return collected[:cap]


def render_label_report(repo, results):
    """#230: the per-label lines for `GitHubSource.ensure_labels_report()`'s results, plus ONE
    summary line -- `labels ensured on <repo>: ...` only when every label was measured to exist
    (created now, or read back as already there), otherwise `labels NOT ensured on <repo>: ...`
    naming each failed label. Returns `(lines, any_failed)`. Shared by `setup.py labels`,
    `sdlc_init.py --github` (through that CLI) and `loop.py start`, so the three can never word the
    same outcome differently."""
    lines = []
    for r in results:
        lines.append("  %s: %s" % (r["label"], "FAILED: " + r["reason"] if r["outcome"] == "failed"
                                    else r["outcome"]))
    failed = [r["label"] for r in results if r["outcome"] == "failed"]
    where = repo or "the current repository"
    if failed:
        lines.append("labels NOT ensured on %s: %d of %d failed (%s) -- a label that does not exist "
                     "makes the pick query return nothing; create it by hand or grant the token "
                     "label-write (Issues: write) and rerun" % (where, len(failed), len(results),
                                                                 ", ".join(failed)))
    else:
        created = sum(1 for r in results if r["outcome"] == "created")
        lines.append("labels ensured on %s: %d created, %d existed" % (where, created,
                                                                       len(results) - created))
    return lines, bool(failed)


def resolve_assignee_login(run, raw):
    """The LOGIN to pass into REST's `assignee=` query param, or None to skip assignee scoping
    entirely — for standalone callers (`mirror.py`, `triage.py`, `status.py`) that have no
    `GitHubSource` instance to hang caching off. Mirrors `GitHubSource._assignee_login`'s exact
    normalization rule (that method's own docstring is the fuller account of why each clause
    exists):

      - empty/unset -> None (no filter at all — matches what an unset `--assignee` already meant).
      - the literal `"@me"` (this kit's own documented convention, and `/sigma-init`'s shipped
        default) is a `gh`-CLI SERVER-SIDE alias, not a login; REST's `assignee=` has no such
        alias of its own (a hard 422 from the live API, confirmed) — resolved here via a live
        `gh api user` call.
      - anything else -> `raw.lstrip("@").casefold()`, so a handle typed with a leading `@` or
        display casing still matches `assignees[].login`.

    Deliberately UNCACHED, unlike `_assignee_login`: every caller of this function runs once per
    process (a one-shot script invocation, not `GitHubSource`'s own long-running retry loop), so
    there is no repeated call within one run for a cache to save, and no state to go stale across
    calls that never happen. Fails OPEN on a resolution failure (offline, expired auth): returns
    None rather than raising, so a caller degrades to an unscoped read/count instead of crashing
    outright — the same trade `_assignee_login` makes, for the same reason."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw != "@me":
        return raw.lstrip("@").casefold()
    try:
        login = ((run(["api", "user", "--jq", ".login"]) or "").strip() or "").casefold()
    except Exception:
        return None
    return login or None


try:                    # portable output: force UTF-8 so the board warnings (which embed the em-dash
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")   # default board title) reach a
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")   # non-UTF-8 stderr instead of
except Exception:       # being swallowed by their fail-open guard (the Windows cp1252 default)
    pass

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


legacy = _load("legacy")
hsg = _load("hard_stop_guard")      # decision rubric slice 10: a no-op until hard_stops.enabled is true
discovery = _load("discovery")
state = _load("state")
frontmatter = _load("frontmatter")   # LocalSource.fetch_title_body's title/body split (#519)
ledger = _load("ledger")             # blocker-promotion's ledger edge channel (#900) -- safe to load
                                      # at module level: ledger.py has no reference back to this module.
gh_session = _load("gh_session")     # #78: tell a Claude Code Remote session's gh proxy block apart
                                      # from a real auth failure -- see that module's own docstring.
gh_api = _load("gh_api")             # #895: issue READS go REST first (gh_api.read_issue)
feature_labels = _load("feature_labels")   # #1468: the never-create rule for `feature:*` labels.
                                      # Safe at module level: it loads `features` + `ledger`, and
                                      # neither has any reference back to this module.
feature_registry = _load("feature_registry")   # #2262: `feature_rank`'s read of a unit's OWN
                                      # recorded priority (#2261). Safe at module level: it loads
                                      # only `features` (for `is_unit_name`) and stdlib, and has no
                                      # reference back to this module.

_backlog_check_module = None         # lazily filled by _get_backlog_check() below -- see its own
                                      # docstring for why this can NEVER be a top-level `_load(...)` here.


def _get_backlog_check():
    """Lazy, cached, function-scoped load of backlog_check.py -- deliberately never a top-level
    `_load("backlog_check")` in this module (unlike `discovery`/`state`/`frontmatter`/`ledger`
    above). backlog_check.py's OWN top level does `sources = _load("sources")`: `_load` always
    re-execs a fresh module (neither side registers into `sys.modules`, so nothing short-circuits
    a repeat), so a top-level cross-load HERE recurses forever -- sources -> backlog_check ->
    sources -> backlog_check -> ... (confirmed empirically with a throwaway copy of both files
    before writing this: RecursionError, not a lint concern). Loading lazily, on first actual USE
    rather than at either module's import time, breaks the cycle: by the time this function first
    runs, sources.py's own module-level code has already finished, so backlog_check.py's
    `_load("sources")` call just re-execs a second, independent, self-contained sources.py module
    that never calls back into this function during ITS OWN load (nothing at THIS module's top
    level calls it). Cached after the first call, the same shape as ledger.py's own
    `_scrub_module()` -- the identical cross-module-reference problem, resolved the same way --
    so a hot path (`GitHubSource._promote_blockers`, every board touch once opted in) does not
    re-exec ~700 lines of backlog_check.py (plus a second full sources.py re-exec) on every call.

    `backlog_check._BLOCK_RE` must always be read fresh off the module THIS function returns,
    never captured into a local/module-level alias -- reading `_get_backlog_check()._BLOCK_RE` at
    each use is what keeps this the same live-attribute-read contract `triage.py`'s
    `_scan_block_edges` already relies on (a monkeypatch of the real regex, on the object this
    returns, must be observable)."""
    global _backlog_check_module
    if _backlog_check_module is None:
        _backlog_check_module = _load("backlog_check")
    return _backlog_check_module


def _order(config):
    """`discovery.order` -> 'priority' (default) | 'created'. An unrecognised value falls back to the
    default rather than raising: a typo in config must not crash the pick. Shared by both sources so
    one config key means the same thing in local and github mode."""
    value = ((config or {}).get("discovery") or {}).get("order")
    return value if value in discovery.ORDERS else discovery.DEFAULT_ORDER


def _priority_aliases(config):
    """`discovery.priority_aliases` -> `{alternate spelling: canonical P0-P4 tier}`, or `{}` when
    unset/malformed. Opt-in: an adopter whose board/labels already use their own vocabulary
    (`Critical`/`High`/`Medium`/`Low`, or anything else) can map it onto the P0-P4 the picker
    actually understands, instead of getting zero priority support. A non-dict value (typo'd
    config) is treated the same as unset rather than raising — same defensive shape as `_order`.
    Shared by both sources, passed straight through to `discovery.priority_rank`, so one config
    key means the same thing in local frontmatter and GitHub labels/fields alike."""
    value = ((config or {}).get("discovery") or {}).get("priority_aliases")
    return value if isinstance(value, dict) else {}


def _blocker_promotion(config):
    """`discovery.blocker_promotion.mode` -> 'off' (default) | 'smart' | 'always'. An unrecognised
    or missing value falls back to 'off' rather than raising or silently promoting anything -- a
    typo in config must not activate a feature nobody asked for, same defensive shape as `_order`.
    Shared by both sources so one config key means the same thing in local and github mode (#900);
    passed straight through to `discovery.blocker_promotion_rank`."""
    block = ((config or {}).get("discovery") or {}).get("blocker_promotion")
    value = block.get("mode") if isinstance(block, dict) else None
    return value if value in discovery.BLOCKER_PROMOTION_MODES else discovery.DEFAULT_BLOCKER_PROMOTION_MODE


def _feature_priority(config):
    """`discovery.feature_priority.enabled` -> bool, DEFAULT TRUE SINCE #2284 (was False, #2262).
    Mirrors `_blocking_priority_override`'s own #1394 inversion exactly: only the literal boolean
    `False` turns it OFF now; a missing key, a typo'd string, a number, `None`, or a
    `feature_priority` value that is not even a dict all read as ON (the new default) -- the
    mirror image of the old "only True turns it on" rule. The property that mattered is preserved
    either way: a config mistake never changes behaviour, it just leaves you on the default,
    which is now on rather than off.

    DEFAULT-ON BECAUSE THE TIE-BREAK CAN STRUCTURALLY NEVER FIRE ON ITS OWN. `_feature_rank`/
    `_board_feature_rank` both resolve to `discovery.UNPRIORITISED` -- the identical sentinel a
    feature-less issue already gets -- for any unit that has never had a priority RECORDED
    (`define.py set-priority`, #2266), so turning this on costs an existing install nothing until
    the day someone actually calls `set-priority` for the first time. Owner decision, 2026-09-09,
    made during Epic #2260's own pre-merge review: verified no existing repo (this one, os, op, oi)
    can possibly have a recorded priority yet, since the write surface is the same epic."""
    block = ((config or {}).get("discovery") or {}).get("feature_priority")
    value = block.get("enabled") if isinstance(block, dict) else None
    return value is not False


def _no_dangling_goal_enabled(config):
    """`discovery.no_dangling_goal.enabled` -> True | False (default False). #2263 of
    design #2253 (D-7): the top-level opt-in for "no work sits outside the structure" --
    a goal declaring NO unit at all (`features.NONE`) is either set aside under `sdlc:needs-unit` or
    attributed to the `core` catch-all (see `_no_dangling_goal_core`), gated jointly on THIS being
    True and on `.sdlc/features/` existing (the second half of D-7's gate lives in
    `feature_labels._handle_no_unit_at_pick`, the only reader of this attribute, because the
    registry-directory check is a filesystem stat this module has no reason to make on every source
    construction).

    Same nested-block defensive shape as `_feature_priority`/`_blocker_promotion` immediately
    above -- only the literal boolean `True` engages it; a missing key, a typo'd string, a number,
    `None`, or a `no_dangling_goal` value that is not even a dict all read as OFF, so a config
    mistake can never silently set aside -- or silently re-attribute -- a goal nobody asked to
    touch. OFF by default: an adopter who never sets this key gets a pick byte-identical to before
    this feature existed, down to never calling `mark_needs_unit`/`list_needs_unit` at all."""
    block = ((config or {}).get("discovery") or {}).get("no_dangling_goal")
    value = block.get("enabled") if isinstance(block, dict) else None
    return value is True


def _no_dangling_goal_core(config):
    """`discovery.no_dangling_goal.core` -> the catch-all unit NAME (a non-empty, stripped string),
    or None. Only consulted by a caller that already checked `_no_dangling_goal_enabled` -- this
    function reads exactly one key, the same one-key-per-function discipline `_feature_priority`
    and `_no_dangling_goal_enabled` both keep, so a caller reading both together is reading two
    independent config facts, not one compound one that could silently disagree with itself.

    A missing key, `None`, an empty/whitespace-only string, or any non-string value (a number, a
    list, `True`) all read as "no catch-all configured" -- which selects the SET-ASIDE behaviour
    (`feature_labels._set_aside_no_unit`), never tier 2 of `feature_classify`'s chain. That is
    deliberate, and it fails towards the LOUDER of the two outcomes on purpose: a malformed `core`
    value must not silently start attributing real, undeclared work to a name nobody typed
    correctly.

    #2363 REVERSES D-6 OF design #2253: `core` WAS a sentinel string this codebase never
    checked against the registry -- `feature_labels._attribute_to_core` (deleted) wrote a comment
    naming it and attached no label, specifically because a real `feature:core` unit did not exist
    yet. It is now bootstrapped as a REAL registered unit (branch, label, registry entry), so THIS
    function does no unit-name-shape validation ITSELF -- it is a plain string read, unvalidated --
    but the value it returns IS handed to `git`-adjacent machinery downstream:
    `feature_classify.classify`/`classify_for_filing` resolve it through
    `feature_registry.resolve_open_unit`, which performs that validation (and answers `None` for a
    name that is not a real, open, registered unit) before anything is written. Per
    design #2253 B-4, the literal name `core` was checked clear of any collision on the
    three repos this design targets before it was bootstrapped -- that check was a one-time,
    per-adopter pre-flight step, not something this function verifies from config alone."""
    block = ((config or {}).get("discovery") or {}).get("no_dangling_goal")
    value = block.get("core") if isinstance(block, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _no_dangling_goal_live_judge_block(config):
    """`discovery.no_dangling_goal.live_judge` -> the nested dict, or `None`. One-key-per-function
    discipline continues below (`_no_dangling_goal_live_judge_enabled`/`_rounds`/
    `_spend_ceiling_usd_per_day` each read exactly one key), this is just the shared nested-lookup
    step every one of them starts from, to avoid four independent copies of the same two `.get()`
    calls drifting apart."""
    block = ((config or {}).get("discovery") or {}).get("no_dangling_goal")
    live_judge = block.get("live_judge") if isinstance(block, dict) else None
    return live_judge if isinstance(live_judge, dict) else None


def _no_dangling_goal_live_judge_enabled(config):
    """`discovery.no_dangling_goal.live_judge.enabled` -> True | False (default False). #2380 of
    plan #2260 (live-judge): a FURTHER, deliberate opt-in stacked on top of
    `_no_dangling_goal_enabled` -- turning THAT on alone still behaves exactly as it did before
    this slice existed (tiers 1/3 stay unreachable; `_default_judge` always abstains to the
    catch-all). This key is what routes pick-time and filing-time classification through a REAL,
    metered, live model call (`feature_judge.live_judge`) instead. Same defensive shape as
    `_no_dangling_goal_enabled` itself, and for the same reason, now doubly important: only the
    literal boolean `True` engages it, so a missing key or a typo can never silently start
    spending real money nobody asked for."""
    value = (_no_dangling_goal_live_judge_block(config) or {}).get("enabled")
    return value is True


def _no_dangling_goal_live_judge_rounds(config):
    """`discovery.no_dangling_goal.live_judge.rounds` -> an int of 2 or more, default 3 (#2380's own
    self-consistency majority-vote design -- see `feature_judge.assign`). Any missing, non-numeric,
    or non-positive value falls back to the default rather than raising or silently disabling the
    majority-vote safety property (a `rounds` of 1 or 0 would make "majority" meaningless) a typo'd
    config value might otherwise produce.

    #2428, A REAL BUG FOUND LIVE AND FIXED HERE: the guard above is this function's own, and until
    now it read `value > 0` -- accepting `rounds: 1` verbatim despite the very docstring paragraph
    it sits under already naming that value as the one that breaks the design. `feature_judge.
    _majority`'s own gate is `top_count < 2 -> None` (ABSTAIN), which with exactly one round can
    NEVER be satisfied (`top_count` tops out at 1) -- so the measured consequence of the old bug
    was not "trust whichever single answer came back", it was a GUARANTEED abstain on every call:
    a real, metered `ask_claude` subprocess spend for a vote that could never win, every single
    time. `value > 1` is the fix that matches this docstring's own stated intent exactly."""
    value = (_no_dangling_goal_live_judge_block(config) or {}).get("rounds")
    try:
        value = int(value)
    except (TypeError, ValueError):
        return 3
    return value if value > 1 else 3


def _no_dangling_goal_live_judge_spend_ceiling_usd_per_day(config):
    """`discovery.no_dangling_goal.live_judge.spend_ceiling_usd_per_day` -> a positive float,
    default 5.0. Read by `feature_judge.live_judge` off the `GitHubSource` attribute this resolves
    into (never by re-reading config directly -- `feature_judge.py` deliberately never imports or
    reads `discovery.*` itself, matching `no_dangling_goal_core`'s own split) as the rolling-24h
    dollar ceiling `feature_judge._spend_ceiling_ok` fails CLOSED against. A missing, non-numeric
    or non-positive configured value falls back to the conservative default rather than disabling
    the ceiling outright -- a malformed number here must never silently mean "unlimited real
    spend"."""
    value = (_no_dangling_goal_live_judge_block(config) or {}).get("spend_ceiling_usd_per_day")
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 5.0
    return value if value > 0 else 5.0


#: Valid values for the `discovery.auto_unpark.mode` config key (#1129) -- see `_auto_unpark`.
AUTO_UNPARK_MODES = ("off", "on")
#: #1394: ON by default. A goal that recorded a blocker and is waiting for it to close is waiting on
#: MACHINE-resolvable work, not on a person -- so resuming it when that work lands is the loop doing
#: its job, not an extra feature. Opting OUT is now the deliberate gesture.
#:
#: This is only safe because the sweep no longer touches `parked_label` (see
#: `auto_unpark.compute_unpark_actions`): it reverses the loop's OWN `sdlc:blocked` state and never
#: a human's park. Turning it on while it still swept parked issues would have made the documented
#: guarantee "nothing automatic ever un-parks a human's park" false.
DEFAULT_AUTO_UNPARK_MODE = "on"


def _auto_unpark(config):
    """`discovery.auto_unpark.mode` -> 'on' (default since #1394, `DEFAULT_AUTO_UNPARK_MODE`) | 'off'.
    An unrecognised or missing value falls back to that default rather than raising. It was opt-in
    (#1129); #1394 made it default-on once the sweep stopped resuming `sdlc:parked` issues.

    Unlike `blocker_promotion`'s genuine three-way `smart`/`always` distinction (two materially
    different RULES for computing a promoted rank), there is no analogous middle mode here to
    mirror honestly: a parked goal's recorded blocker is either still open or it isn't, so this is
    a plain two-value gate, not a three-value one padded out to look consistent.

    Read by `loop.py`'s `_next()` (gates whether the unattended per-pick sweep runs at all) --
    NEVER by `auto_unpark.sweep_unpark()` itself, which is unconditional once called, the same
    posture `triage.py`'s `enact()` already has: a direct call (this module's own CLI, or a human/
    cron invoking it deliberately) IS the opt-in, same as `enact --apply` needs no separate config
    flag either. Does not reach local-goals mode, for the same structural reason `blocker_promotion`
    doesn't (see its own docstring/README section): a local goal's "blocked by #N" reference is a
    file path, never a bare issue number `backlog_check._BLOCK_RE` can match, so there is nothing
    for `LocalSource` to ever sweep and the key is simply never read there."""
    block = ((config or {}).get("discovery") or {}).get("auto_unpark")
    value = block.get("mode") if isinstance(block, dict) else None
    return value if value in AUTO_UNPARK_MODES else DEFAULT_AUTO_UNPARK_MODE


def _blocking_priority_override(config):
    """`discovery.blocking_priority_override` -> bool, **default True since #1394** (was False in
    #1352). Set it to the literal `false` to turn the override off. Formerly off by default, matching
    (`blocker_promotion.mode`, `auto_unpark.mode`, `parallel.enabled`) -- the SAME defensive shape
    `_auto_unpark`/`_blocker_promotion` immediately above already establish: only the literal
    boolean `True` turns it on, anything else (missing, a typo'd string, a number, null) reads as
    off, so a config mistake can never silently activate a scheduling override nobody asked for.

    When on: any `goal_label` issue that ALSO carries `blocking_label` sorts ahead of EVERY other
    pending issue, regardless of priority tier -- a STRUCTURAL override, not a priority nudge (a
    promoted P0 can still tie with other real P0s; this doesn't). See `GitHubSource.
    _blocking_priority_pending` (label-queue path) and `_board_queue` (board-authoritative path) --
    both need this, independently, per the epic's own adversarial plan-review finding (§10.3 of the
    design doc): a fix to only one would silently fail to surface a blocker on whichever path a
    given repo's config actually routes through.

    GitHub discovery mode only, for the identical structural reason `_auto_unpark`/
    `blocker_promotion` already don't reach `LocalSource`: `sdlc:blocking` is a GitHub-issue-number
    label, and `LocalSource` goals have no label state at all."""
    #
    # #1394: the default INVERTED to True, and the defensive shape inverted with it. The old rule was
    # "only the literal `True` turns it on, so a config typo can never silently activate a
    # scheduling override nobody asked for". The mirror is "only the literal `False` turns it off" --
    # a missing key, a typo'd string, a number or null all land on the DEFAULT, which is now on. The
    # property that mattered is preserved: a config mistake never changes behaviour silently, it
    # just leaves you with the default either way.
    #
    # Default-on because an issue other work is BLOCKED on is the most valuable thing in the queue
    # by definition -- everything waiting on it is idle until it lands. Sorting it first is what
    # makes the chain drain innermost-first instead of at whatever depth priority happens to pick.
    value = ((config or {}).get("discovery") or {}).get("blocking_priority_override")
    return value is not False


class LocalSource:
    """Goals are markdown files under <sdlc>/goals/. Delegates to the file-based discovery + state."""
    def __init__(self, sdlc_dir, config=None):
        self.sdlc_dir = sdlc_dir
        self.goals_dir = str(pathlib.Path(sdlc_dir) / "goals")
        # `config` is OPTIONAL and trailing on purpose: `LocalSource(sdlc_dir)` is constructed
        # positionally by callers and tests that predate ordering, and must keep working untouched.
        self.order = _order(config)
        self.priority_aliases = _priority_aliases(config)

    def next_pending(self, skip=()):
        return discovery.next_pending(self.goals_dir, skip, order=self.order, aliases=self.priority_aliases)

    def read_degraded(self):
        """#1084: LocalSource has no read-failure-vs-genuinely-empty ambiguity to report today --
        `discovery.next_pending()`'s one real local failure mode (an unreadable goal file) RAISES
        rather than degrading (see discovery.py:102, unguarded `path.read_text()`; a separate,
        smaller, pre-existing hygiene gap, out of scope here). This method exists so LocalSource
        states its own contract explicitly, symmetrically with GitHubSource.read_degraded()
        (sources.py:1880), rather than every current and future call site relying forever on the
        getattr(source, "read_degraded", lambda: False) duck-typed default."""
        return False

    def mark_in_progress(self, goal):
        state.set_in_progress(self.sdlc_dir, goal)

    def release(self, goal, reason, note=None):
        """#841: local counterpart to GitHubSource.release — undoes a claim that was never
        started. `state.release` sets status back to `pending` so `next_pending` offers this goal
        again exactly like one that was never picked; the journey-log note (`note()`, the same
        machinery every other LocalSource audit trail already uses) is the local mirror of the
        GitHub issue comment. Unlike park/fail, this never queues into review-queue.md — nothing
        here needs a human decision.

        POST-REVIEW FIX (PR #1107, Finding 3): `state.release` now refuses (returns False, goal
        left untouched) when the goal is already `done`/`parked`/`failed` — release is only
        meaningful for a goal still claimed/in-progress. Still journals the request either way (so
        a release attempt is never silently invisible in the journey log — cheap, a local file
        write) but with an HONEST message: "released" only when something actually changed, a
        distinct no-op message when it did not. Returns the same bool `state.release` reports."""
        released = state.release(self.sdlc_dir, goal)
        if released:
            text = note or "Released — claimed but not started"   # #2009: see GitHubSource.release
        else:
            text = "Release requested, but this goal is already done/parked/failed — no change made"
        if reason:
            text += ": " + reason
        self.note(goal, text)
        return released

    def complete(self, goal):
        state.complete(self.sdlc_dir, goal)

    def park(self, goal, reason, tier=None):
        state.park(self.sdlc_dir, goal, reason, tier=tier)

    def fail(self, goal, reason):
        state.fail(self.sdlc_dir, goal, reason)

    def mark_qc(self, goal):
        pass            # QC is a board-only stage; the local source has no QC column

    def await_merge(self, goal, note=None):
        """#232: local counterpart -- the goal stays `in_progress` (never re-offered by
        `next_pending`, never `done`) until the merge-reconcile pass observes its PR merged; the
        journey log records why."""
        if note:
            self.note(goal, note)

    def _resolve_ref(self, goal):
        """Resolve `goal` to a real goal-file Path. Every LONG-STANDING LocalSource caller already
        passes a real path (what `next_pending()`/`discovery.next_pending` hands back), and that
        case is untouched here — a literal existing path always wins outright, checked first.

        Added for #921 (sigma-scope epic #902's own integration validation surfaced this): the ONE
        exception is a bare numeric id — exactly what `create_dependency`'s own RETURN VALUE is
        (`gid`, matching `GitHubSource.create_dependency`'s bare-issue-number return, by design, per
        that method's own docstring: "the signature matches GitHubSource's deliberately"). Until
        this fix, a caller that chains `create_dependency()`'s return straight into `append_to_body`
        or `note()` — exactly what `compile_plan.py`'s own `_patch_epic_with_subs` does for the
        epic's "Tracks #N" back-reference, the first caller in this codebase to do that — silently
        failed in local-goals mode alone: `GitHubSource.append_to_body`/`note` accept that same bare
        id directly (an issue number IS the `gh` handle), so the mismatch never showed up against
        the GitHub side these methods were written to mirror. Resolved by glob-scanning
        `<digits>-<slug>.md` — the SAME id convention `create_dependency`'s own `highest` computation
        above already uses — so this stays one, single source of truth for what a local goal's
        leading id means, not a second, independently-maintained parser of it.

        A `goal` that is neither a real path NOR a resolvable numeric id is returned unchanged
        (as a bare `Path(goal)`) so an existing caller's "missing/unreadable file" error still
        surfaces exactly as before — this only ever ADDS a resolution, never removes the fallback
        that already made a genuinely bad reference fail loudly."""
        literal = pathlib.Path(goal)
        if literal.is_file():
            return literal
        ref = str(goal).strip()
        if ref.isdigit():
            target = int(ref)
            for p in pathlib.Path(self.goals_dir).glob("*.md"):
                lead = p.name.split("-", 1)[0]
                if lead.isdigit() and int(lead) == target:
                    return p
        return literal

    def note(self, goal, text):
        # journey-log: append a timestamped note for this goal under .sdlc/journey/<stem>.md
        jdir = pathlib.Path(self.sdlc_dir) / "journey"
        state.refuse_symlinks(self.sdlc_dir, jdir, create_parents=True)    # #708
        jdir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # `_resolve_ref` handles a bare create_dependency-returned id the same way append_to_body
        # does (#921); `.stem` on its result still strips directories/extensions either way, so no
        # traversal is possible (#486) whether `goal` arrived as a real path or a bare numeric id.
        stem = self._resolve_ref(goal).stem
        state.refuse_symlinks(self.sdlc_dir, jdir / (stem + ".md"))        # #708
        with (jdir / (stem + ".md")).open("a", encoding="utf-8") as f:
            f.write(f"\n## {ts}\n{text}\n")

    def append_to_body(self, goal, marker):
        """Local counterpart to GitHubSource.append_to_body — same contract: read the goal file's
        CURRENT body, append `marker` to it, write it back; never touch the frontmatter block above
        it, never overwrite what was already there. Without this (#726), `create_tracked_issue()`'s
        `hasattr(source, "append_to_body")` gate on a working `LocalSource` silently skipped the
        whole step: `create_dependency` succeeded, so the caller saw a clean report, but the
        "Blocked by" marker landed nowhere.

        This is the MACHINE-readable channel, exactly as on the GitHub side: `backlog_check.py`'s
        `_explicit_blockers()` regexes a local goal's own BODY text — `frontmatter.strip()`'d, read
        fresh from disk by `_build_corpus()` — for `blocked by ... #N`, so the marker has to land
        inside the actual goal file's body, never the separate journey-log file `note()` (above)
        writes to, or a later precheck can never see it however clearly a human would read the
        journey entry.

        `goal` is a path to the goal .md file — the same convention every other LocalSource method
        already uses (`state.py`'s status writers take `goal_path` directly and write straight to
        it; `fetch_title_body` below does `pathlib.Path(goal)`; `discovery.next_pending` is what
        hands back that full path to begin with, never a bare id) — OR a bare numeric id, resolved
        via `_resolve_ref` (#921; see its own docstring). A missing/unreadable file raises rather
        than degrading silently, on purpose: the caller (`handoff.create_tracked_issue`) already
        wraps this call in a try/except that turns any raised exception into a `report["warnings"]`
        entry, so raising is what makes THAT existing warning path fire instead of a quiet no-op."""
        path = self._resolve_ref(goal)
        text = path.read_text(encoding="utf-8")
        body = frontmatter.strip(text)
        fence = text[:len(text) - len(body)]      # the frontmatter block, byte-for-byte, untouched
        state.atomic_write_text(path, fence + body.rstrip() + "\n\n" + marker + "\n")

    def create_dependency(self, title, body, assignee, labels=(), goal_label=True):
        """Local counterpart to GitHubSource.create_dependency — write a NEW goal file and return its
        id. Without this, a local backlog could only ADVANCE work items, never create one: every
        caller routes through `handoff.create_tracked_issue`, which selects a backlog by
        `hasattr(source, "create_dependency")` and so silently skipped creation here while still
        writing the ledger entry.

        The signature matches GitHubSource's deliberately. Because the call site is duck-typed, a
        drifted signature would fail at runtime in local mode only — exactly where nobody is looking.

        `goal_label` maps onto a state the kit already has rather than a new one (see `discovery.py`):
        True -> `status: pending` (auto-picked by `next_pending`), False -> `status: proposed`
        ("awaiting HUMAN promotion", which `next_pending` skips). That is the same
        filed-but-not-auto-picked contract the GitHub side gets by withholding the goal label.

        `assignee` and `labels` have no local machinery to route them — there is no assignee filter
        and no label index over goal FILES. They are written into the body rather than dropped: a
        caller that resolved an owner deserves to see where it went.

        Id allocation is max-existing + 1, unlocked. Every state file in this kit assumes one worker
        per `.sdlc` (see `slices.py`), so a lock here would be the only one of its kind and would
        still not make two workers on one tree safe."""
        goals = pathlib.Path(self.goals_dir)
        goals.mkdir(parents=True, exist_ok=True)
        highest = 0
        for p in goals.glob("*.md"):
            lead = p.name.split("-", 1)[0]
            if lead.isdigit():
                highest = max(highest, int(lead))
        gid = highest + 1

        # Frontmatter is line-based and `frontmatter.parse` splits on the first colon then strips
        # surrounding quotes — so the value may contain colons, but never a newline or a quote.
        clean = " ".join(str(title).replace('"', "").split())
        slug = re.sub(r"[^a-z0-9]+", "-", clean.lower()).strip("-")[:48] or "goal"
        path = goals / ("%04d-%s.md" % (gid, slug))

        trailer = []
        if assignee:
            trailer.append("Owner: %s" % assignee)
        if labels:
            trailer.append("Labels: %s" % ", ".join(labels))
        # `id` bare, `title` quoted — matching the convention every hand-written goal already uses,
        # so a machine-filed goal is indistinguishable from one a human wrote.
        text = (
            "---\n"
            "id: %04d\n"
            'title: "%s"\n'
            "lane: auto\n"
            "status: %s\n"
            "---\n\n%s\n" % (gid, clean, "pending" if goal_label else "proposed",
                             (str(body or "").rstrip()
                              + ("\n\n" + "\n".join(trailer) if trailer else "")))
        )
        path.write_text(text, encoding="utf-8")
        return gid

    def fetch_title_body(self, goal):
        """Local-mode counterpart to GitHubSource.fetch_title_body (#519): `goal` is a path to a
        goal .md file, not an issue number. Title prefers the file's own frontmatter `title:`
        field (the convention every goal file already carries — see discovery.py's status/lane
        reads); body is the markdown AFTER the frontmatter fence (`frontmatter.strip`), so a
        reader like goal_size.classify never counts YAML keys as goal content. Falls back to the
        bare filename stem when there is no frontmatter title, and to the raw text when the file
        has no frontmatter fence at all. A missing/unreadable file degrades to `("", "")` rather
        than raising — the same read-only, never-mutating contract as the GitHub counterpart."""
        path = pathlib.Path(goal)
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return {"title": "", "body": ""}
        title = frontmatter.get(text, "title") or path.stem
        return {"title": title, "body": frontmatter.strip(text)}


def _run_gh(args, binary="gh"):
    """Run `gh <args>`, return stdout; raise a helpful RuntimeError on failure (binary is for tests).

    The raised error's own `.hint` attribute carries just the short reason (`gh`'s own stderr, or the
    auth-check fallback) — `str(exc)` includes the full reconstructed command line for a human reading
    a traceback, but a caller that wants to quote the failure somewhere ELSE (e.g. F14/#338's
    unassigned-fallback note, posted as a GitHub comment) needs the short form: `args` can carry an
    entire issue body, and a comment re-embedding the whole failed command line — body and all — buries
    the one line anyone actually needs to read.

    #78: when `gh`'s own stderr IS a Claude Code Remote session's proxy block rather than a genuine
    GitHub error, `gh_session.proxy_session_block` recognizes it and `hint` leads with the corrected
    diagnosis (connect the Claude GitHub App for this org) instead of the raw 403, which reads like an
    ordinary permission error and sends whoever's reading it chasing the wrong fix. Every other
    failure — a real GitHub error, `gh` missing entirely, a genuinely bad token — never matches, so
    `hint` is exactly what it always was for those."""
    import subprocess
    proc = subprocess.run([binary, *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raw_hint = proc.stderr.strip() or "is `gh` installed and authenticated? run `gh auth status`"
        hint = gh_session.proxy_session_block(raw_hint) or raw_hint
        exc = RuntimeError("gh " + " ".join(args) + " failed: " + hint)
        exc.hint = hint
        raise exc
    return proc.stdout


#: Longest reason a `release()` warning quotes from a failed `gh` call (work.py's `_READ_NOTE_CHARS`).
_RELEASE_WARNING_CHARS = 200


def _release_warning_reason(exc):
    """`exc` as ONE short line for a `release()` warning: gh's own stderr (`.hint`, as
    `_warn_field` reads it) when the error carries one, else `str(exc)`; whitespace collapsed,
    capped. `str(exc)` holds the whole argv, and for the audit comment that is `--body`: the
    caller's free-text reason must not be echoed whole into stderr or a getter."""
    text = " ".join(str(getattr(exc, "hint", None) or exc).split())
    return text if len(text) <= _RELEASE_WARNING_CHARS else text[:_RELEASE_WARNING_CHARS - 1] + "…"


DEFAULT_COMMENT_LIMIT = 20   # most-recent comments considered; see the cost note in the docstring below


# #1186: named, single-source-of-truth constants for the FIXED prefix text `_offboard`'s two callers
# (`park`/`fail`, below) prepend to every park/fail comment. Exported so `backlog_check.py`'s
# comment-scrubbing pipeline can strip exactly this span before a `_BLOCK_RE` blocker-phrase scan --
# the prefix text itself contains the word "needs" ("Parked by Sigma — needs human review: "),
# which is ITSELF a `_BLOCK_RE` trigger word. Left in the scanned haystack, any `#N` the CALLER'S OWN
# `reason` happens to mention within the ~25 characters of the 40-char match window still open after
# the boilerplate reads as a phantom blocker -- purely an artifact of Sigma's own fixed prose,
# nothing to do with a real dependency (e.g. a "needs human review: PR #1234 is not approved yet"
# park -- closing #1234, one of the things a human might do in response, would silently unpark the
# goal). A single shared constant (not a re-typed literal in backlog_check.py) means the prefix can
# never drift out of sync between the text `_offboard` actually posts and the text the scrub step
# strips.
PARK_COMMENT_PREFIX = "Parked by Sigma — needs human review: "
FAIL_COMMENT_PREFIX = "Failed in the Sigma loop — needs a fix (not a decision): "
OFFBOARD_COMMENT_PREFIXES = (PARK_COMMENT_PREFIX, FAIL_COMMENT_PREFIX)

# #1344: `park()`/`fail()` used to concatenate the caller's `reason` into the published comment
# with ZERO validation -- a reason that is nothing but the verb itself ("parked", "blocked", "n/a",
# ...) or empty after stripping produces a comment with no recoverable cause at all (observed
# downstream: "needs human review: parked"). That comment is the ONLY machine-and-human-readable
# record of why the goal left the queue (`next_pending` excludes `parked_label` unconditionally --
# see its own docstring), so a degenerate one is indistinguishable from a considered decision.
# Case/punctuation-insensitive so "Parked.", "PARKED", "n/a." all match the bare token; anything
# else, however short, is a real reason and passes through untouched -- this only catches a reason
# that IS the terminal state, not a genuinely terse one ("deploy gate" is unaffected).
_DEGENERATE_OFFBOARD_REASON_MARKER = "⚠ no reason supplied by the caller"
_DEGENERATE_OFFBOARD_REASONS = frozenset({
    "", "parked", "park", "blocked", "block", "fail", "failed", "n/a", "na",
    "see above", "unknown",
})


def _sanitize_offboard_reason(reason):
    """Return `reason` byte-for-byte unless it is degenerate (#1344), in which case replace it
    with an explicit marker -- the original text, if any, rides along in parens rather than being
    silently discarded."""
    text = reason or ""
    normalized = text.strip().strip(".!?:;,").lower()
    if normalized not in _DEGENERATE_OFFBOARD_REASONS:
        return text
    stripped = text.strip()
    if not stripped:
        return _DEGENERATE_OFFBOARD_REASON_MARKER
    return f"{_DEGENERATE_OFFBOARD_REASON_MARKER} (caller passed: {stripped!r})"


#: #650 item 5: who may issue a `sigma:` marker in an issue comment. On a public repository anyone can
#: comment, so a marker counts only from a commenter GitHub reports as OWNER, MEMBER or COLLABORATOR
#: (the #635 trust set `work._TRUSTED_ASSOCIATIONS`; tests/test_hardening_697.py pins the two equal).
#: MEMBER can be broader than write access, and a bot token with no association has its own markers
#: ignored: this is a filter on marker text, not a substitute for branch protection.
TRUSTED_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
_MARKER_TOKEN_RE = re.compile(legacy.MARKER_PREFIX_RE + ":", re.IGNORECASE)
_DEFUSED = "[defused]:"            # cannot match `legacy.has_marker` for any marker, either spelling


def _carries_marker(body):
    return bool(_MARKER_TOKEN_RE.search(body or ""))


def _ignore_note(goal, login, association):
    sys.stderr.write(f"sigma: ignoring a sigma: marker in a comment on #{goal} from "
                     f"{login or 'an unknown commenter'} ({association or 'no association'}): "
                     "only OWNER, MEMBER or COLLABORATOR comments are honoured\n")


def defuse_body(body, association, login, goal):
    """`body` unchanged when it carries no marker or its author is trusted; otherwise the same text
    with every marker token defused (so the prose still reaches a blocker-phrase scan but no marker
    reader can see a marker) and one stderr line naming the commenter. An absent or unknown
    association is untrusted: fail closed, ignore loudly."""
    if not _carries_marker(body) or association in TRUSTED_ASSOCIATIONS:
        return body
    _ignore_note(goal, login, association)
    return _MARKER_TOKEN_RE.sub(_DEFUSED, body)


def trusted_marker_comments(comments, goal):
    """The RAW gh comment dicts minus those that carry a marker from an untrusted author (each
    reported on stderr). Plain comments pass untouched. RAISES `ValueError` when a marker comment has
    no `authorAssociation` key at all (an old `gh`, an odd host): trust cannot be judged, so the strict
    reader fails the way a failed read does (every caller already parks or refuses on that)."""
    out = []
    for c in comments or []:
        if not isinstance(c, dict) or not _carries_marker(c.get("body")):
            out.append(c)
            continue
        if "authorAssociation" not in c:
            raise ValueError("a marker comment carries no authorAssociation")
        if c.get("authorAssociation") in TRUSTED_ASSOCIATIONS:
            out.append(c)
        else:
            _ignore_note(goal, (c.get("author") or {}).get("login"), c.get("authorAssociation"))
    return out


def fetch_comments(config, goal, run=None, limit=DEFAULT_COMMENT_LIMIT):
    """Fetch up to `limit` most-recent comments on issue `goal`, oldest-first:
    [{"id": str, "author": str, "body": str, "created_at": str, "association": str}, ...].

    #895: REST first via `gh_api.read_issue` (the issue GET plus only the comment pages holding the
    newest `limit`: at most 3 requests for limit <= 100), with at most ONE `gh issue view --json
    comments` fallback on rate limit / 5xx / transport, never in a cloud session. No `sdlc_dir` here,
    so no breaker: during a REST outage every call costs REST + fallback and one stderr line
    (ceiling; `comment_watch` calls this per claim per tick). Read-only, injectable `run` (default
    `_run_gh`, serving both the `api` and the fallback argv) for hermetic tests -- same DI contract as every other GitHub read in this file. FAIL-OPEN: any error
    (not `gh`, no auth, bad `goal` ref, network blip, malformed JSON) returns [] rather than raising.
    This sits on a hot path (backlog_check.precheck's pre-token check) and will sit on a periodic one
    too (a future watch tick) — neither may ever stall or crash because a comment fetch failed.

    Body text is NOT scrubbed here. Scrubbing is caller-specific (e.g. backlog_check.py calls
    scrub.scrub() explicitly, mirroring how it already scrubs title/body) — a third, silent scrub
    pass here would be a scrub callers can't see and can't reason about, which is worse than none.

    `id` is GitHub's GraphQL node id (e.g. `IC_kwDOTE1deM8AAAABNe8Wcg`) — opaque, NOT a sortable
    integer (verified live against a real repo: `gh issue view <n> --json comments`). The REST path
    emits the comment's `node_id`, which equals that id, so dedup survives a REST/fallback switch.
    A bot comment author reads `<slug>[bot]` over REST but a bare `<slug>` on the fallback
    (unmapped, follow-up). Both measured once on a public bot-filed GitHub Skills exercise issue on
    2026-10-09: REST `node_id` "IC_kwDOVCU5ZM8AAAABamNa1g" == gh `id` "IC_kwDOVCU5ZM8AAAABamNa1g";
    REST comment user "github-actions[bot]", gh comment author "github-actions". Ordering for
    "new vs. seen" is therefore by `created_at` (ISO-8601, always present, string-sortable); identity
    for "have I seen this before" is by `id` (a dedup key only, never assumed orderable).

    Cost caveat: on the REST path `limit` does bound the request count (tail pages only); on the
    fallback path `gh issue view --json comments` has no server-side count flag, so `limit` bounds
    only what this function returns, not that one call's cost. `limit=0`/None reads every page.
    """
    try:
        gh = (config.get("discovery") or {}).get("github") or {}
        data = gh_api.read_issue(run or _run_gh, int(str(goal).lstrip("#")), ["comments"], gh.get("repo") or None,
                                 gql_run=run or _run_gh, comment_limit=limit or None)
        comments = data.get("comments")
        out = []
        for c in comments or []:
            if not isinstance(c, dict):
                continue
            out.append({
                "id": str(c.get("id") or ""),
                "author": ((c.get("author") or {}).get("login") or ""),
                "body": c.get("body") or "",
                "created_at": c.get("createdAt") or "",
                "association": c.get("authorAssociation") or "",
            })
        out.sort(key=lambda c: c["created_at"])          # defensive: never assume gh's own order
        return out[-limit:] if limit else out
    except Exception:
        return []


#: The loop's board columns: (config key under `project.columns`, default name). #280: doctor's
#: unmatched-column row reads this same table, so the two can never disagree about a default.
BOARD_COLUMNS = (("backlog", "Backlog"), ("ready", "Ready"), ("in_progress", "In Progress"),
                 ("qc", "QC"), ("done", "Done"), ("blocked", "Blocked"), ("parked", "Parked"))


class GitHubSource:
    """Goals are open GitHub issues labelled `goal_label`, ordered by issue number. Status via labels;
    done closes the issue; parked labels + comments it. Talks to GitHub through `run` (default _run_gh)."""
    # #1391 step 3a: `proposed_label` was the ONE primary lifecycle label `_ensure_labels` never
    # created, despite doctor.py (#1354) treating it as primary and SKILL.md's whole
    # human-promotion gate depending on it. On a fresh repo the label simply did not exist, so the
    # gate it implements was unenforceable until somebody created it by hand.
    _LABEL_COLORS = (("goal_label", "0e8a16"), ("in_progress_label", "fbca04"), ("parked_label", "d93f0b"),
                     ("goal_blocked_label", "d93f0b"), ("blocking_label", "b60205"),
                     ("proposed_label", "d4c5f9"), ("needs_label_label", "fbca04"),
                     ("designed_label", "0e8a16"),
                     # #2263: SAME colour as `needs_label_label` -- the two are the same shape (an
                     # overlay a human clears by one gesture, never /sigma-unpark), just different
                     # reasons. See `not_eligible_labels`'s docstring for why the shape is shared.
                     ("needs_unit_label", "fbca04"),
                     # #2363: tier 4 of `feature_classify`'s classification chain -- "genuinely
                     # unknown, expected rare". A DIFFERENT colour from `needs_label_label`/
                     # `needs_unit_label` on purpose: those two self-heal the moment a human performs
                     # one specific gesture (create the label; declare a unit), while this one means
                     # every tier of automatic classification was tried and failed, which is a
                     # genuinely rarer and more open-ended state. Not used anywhere else in the
                     # existing `sdlc:*` palette (checked against `_LABEL_COLORS` above).
                     ("needs_triage_label", "5319e7"))
    #: #230: the priority tiers bootstrapped alongside `_LABEL_COLORS` -- by `ensure_labels_report`
    #: only (setup, init --github, `loop.py start`), never by the hot-path `_ensure_labels` above,
    #: whose per-pick contract (and its tests) stays the lifecycle set alone. Without these an
    #: unlabelled issue sorts last and the issue-field gate refuses `gh issue create`. P0-P3 only:
    #: P4 is still ranked if an adopter creates it, but #230 scopes the bootstrap to the four tiers
    #: work is actually filed at. Colours are the kit's own `sdlc:*` severity palette above, in the
    #: same order of urgency (b60205 > d93f0b > fbca04 > 0e8a16); the descriptions are the anchor
    #: sentences of the triage rubric (`triage._PRIORITY_PATTERNS`' comment). The ONE table: every
    #: caller reads it from here.
    _PRIORITY_LABELS = (
        ("priority:P0", "b60205", "Broken, unsafe, or silently doing the wrong thing"),
        ("priority:P1", "d93f0b", "Degrades most runs, leaves output wrong, or blocks work"),
        ("priority:P2", "fbca04", "Real, wanted, planned work; nothing broken by waiting"),
        ("priority:P3", "0e8a16", "Narrow bugs, tech debt, follow-up polish"),
    )
    #: #230: pages of 100 read by `_existing_label_names` before it gives up and falls back to
    #: create-and-classify -- 100 x 100 = 10,000 labels, far past any real repository.
    _LABEL_LIST_MAX_PAGES = 100
    # `gh project` is occasionally flaky (intermittent "unknown owner type", 5xx, rate-limit). Those
    # blips silently dropped card-status updates, drifting the board from the issues (the source of
    # truth). Retry project calls with short exponential backoff; non-transient errors still fail fast.
    _PROJECT_RETRIES = 4                         # total attempts for a `gh project` call
    _RETRY_BASE = 0.5                            # backoff seconds: base * 2**attempt (override to 0 in tests)
    # #692: `gh`'s `--limit` is a MAXIMUM, not a page size — it stops as soon as the source is
    # exhausted, so a high ceiling costs nothing on a small board (measured live against project #6:
    # `--limit 300` -> 262 items and `--limit 400` -> 268, at the same wall time as `--limit 200`).
    # The previous hard-coded 200 therefore bought nothing and silently dropped the tail of any
    # larger board: 62 of that board's 262 cards were missing from `_items`, so `_item_id` re-issued
    # `project item-add` for each of them on every board touch and `_sync_backlog`'s `on_board` set
    # was wrong for the same 62. There is no "unlimited": `--limit 0` falls back to gh's own default
    # of 30 (verified). So the ceiling cannot be removed, only raised — and `_warn_truncated` makes
    # sure that if a board ever does reach it, it says so instead of quietly losing rows again.
    _BOARD_ITEM_LIMIT = 5000                     # (override in tests to exercise the ceiling)
    _TRANSIENT = ("unknown owner type", "rate limit", "secondary rate", "429",
                  "500", "502", "503", "504", "timeout", "timed out", "try again", "temporarily")

    def __init__(self, config, run=None, sdlc_dir=None):
        gh = ((config.get("discovery") or {}).get("github")) or {}
        self._hs_config = config
        self.repo = gh.get("repo") or ""
        self.goal_label = gh.get("goal_label", "sdlc:goal")
        self.in_progress_label = gh.get("in_progress_label", "sdlc:in-progress")
        self.parked_label = gh.get("parked_label", "sdlc:parked")
        # #1205: optional, repo-local "do not auto-pick" label -- distinct from `parked_label`,
        # which the LOOP ITSELF applies when it parks a goal. This one is a human convention (spend
        # gate, legal review, third-party wait) the loop never sets or clears, only reads. Before
        # this existed, the label-queue path's ONLY exclusion beyond the caller's own skip set was
        # `parked_label` -- any repo expressing "not now" as its own label had that label be
        # entirely invisible to the picker, with nothing warning the convention was inert (see
        # `_fetch_pending` below, and `doctor.py`'s `_blocked_label_scan` for the setup-time nudge
        # when a blocked-ish label is found but this key was never set). Unset/empty by default =>
        # no filter => byte-compatible with every prior config.
        self.blocked_label = gh.get("blocked_label") or None
        # #1350: DISTINCT from `blocked_label` immediately above — that one is a human-set, read-
        # only "do not auto-pick" convention (#1205) the loop never writes. This one IS a real,
        # code-managed state label: the in-progress goal that just discovered a genuine blocking
        # dependency (see `mark_blocked` and `_offboard`'s own collision-avoidance below).
        self.goal_blocked_label = gh.get("goal_blocked_label", "sdlc:blocked")
        # #1468: DISTINCT from `goal_blocked_label` above, and the distinction is the whole reason
        # it exists. Both are "member of the backlog, currently unpickable, will resume by itself",
        # but they differ in WHY -- and `sdlc:blocked`'s why is wired into machinery that assumes it
        # owns every instance: `auto_unpark.compute_unpark_actions` strips `sdlc:blocked` from any
        # goal whose body names a `blocked by #N` whose refs have all closed, whatever set it. A
        # goal held for a missing `feature:` label that ALSO names an already-closed dependency
        # would therefore be un-blocked by that sweep and re-blocked by ours, forever: two label
        # swaps, two board moves and one FALSE "blocker closed" comment per cycle (measured; 6 open
        # issues on this repo's own board would have fired). A label of our own is what makes
        # `auto_unpark`, `compute_blocking_actions`, `blockers._act` and `unpark.brief` all remain
        # correct with no change to any of them.
        self.needs_label_label = gh.get("needs_label_label", "sdlc:needs-label")
        # #2263: the SIBLING overlay -- same shape as `needs_label_label` immediately above (member
        # of the backlog, currently unpickable, self-heals in one human gesture), a DIFFERENT
        # reason: this issue declares NO unit of work anywhere at all (neither a body `Feature:`
        # marker nor a `feature:*` label), rather than declaring one whose label does not exist yet.
        # Two reasons, two labels, for the identical `auto_unpark` flapping argument
        # `needs_label_label`'s own comment already makes -- a goal held for lacking any
        # declaration that ALSO names an already-closed `Blocked by: #N` would flap between this
        # overlay and that sweep if the two shared one label. Written and cleared only by
        # `feature_labels._handle_no_unit_at_pick`/`resume_needs_unit`, and only when
        # `no_dangling_goal_enabled` is on AND `.sdlc/features/` exists (D-7 of
        # design #2253) -- see those attributes below.
        self.needs_unit_label = gh.get("needs_unit_label", "sdlc:needs-unit")
        # #2363: the TIER-4 overlay of `feature_classify`'s classification chain -- every tier
        # (a single existing unit, the configured catch-all, an identifiable-but-unregistered
        # component) was tried and failed. Same shape as `needs_unit_label` immediately above
        # (member of the backlog, currently unpickable) with no self-healing sweep of its own: unlike
        # a missing label or an undeclared unit, "genuinely unknown" has no machine-detectable
        # condition to watch for, so a human clears it by removing the label directly (or by
        # declaring a real unit) rather than by a documented one-gesture fix a sweep re-checks.
        self.needs_triage_label = gh.get("needs_triage_label", "sdlc:needs-triage")
        # #1351: the DERIVED, self-healing auxiliary label — applied to the BLOCKER issue itself
        # (not the blocked goal `goal_blocked_label` marks), for as long as at least one open issue
        # still references it via a live "Blocked by #N" marker. Also DISTINCT from `sdlc:dependency`
        # (`handoff.DEPENDENCY_LABEL`), a static, one-time descriptive tag applied at cross-area
        # filing time and never updated — this one is a live, reconciled signal, managed entirely by
        # `auto_unpark.py`'s own sweep (`compute_blocking_actions`), never written at filing time.
        self.blocking_label = gh.get("blocking_label", "sdlc:blocking")
        # #1826: the OVERLAY `goal-design` (#1825) + `goal-review` (this issue) produce together —
        # a goal carrying it has been through a codebase-mapping design pass AND had that design
        # independently confirmed. `loop.py design_check` (#1825) already reads this literal
        # string (`"sdlc:designed" in names`, not `source.designed_label`) — its own check predates
        # this attribute and was never threaded through it, so the default here MUST stay
        # byte-identical to that literal or goal-review would write a label design_check does not
        # recognize. `mark_designed` (below) is the only writer; see its own docstring for why it
        # is a bare add, unlike every other overlay in this block.
        self.designed_label = gh.get("designed_label", "sdlc:designed")
        # #1391 step 3a: the AI-filed-awaiting-human-promotion label. Its home is
        # `ledger.handoff.proposed_label` (that is where `handoff.proposed_label(config)` reads it,
        # and an adopter may override it there), NOT under `discovery.github` like the labels above —
        # so it is resolved from the whole config, defensively, rather than from `gh`. Held here
        # so `_ensure_labels` can CREATE it, and (since #1392) so `_fetch_pending` can EXCLUDE it --
        # this class still never APPLIES it. #1391 step 3a's own note here used to add "and it must
        # never gate a pick (an issue carrying it simply has no `goal_label`, which is what makes it
        # unpickable)"; that parenthetical was the bug. It holds for a proposal filed by
        # `handoff.create_tracked_issue`, which withholds `goal_label` deliberately -- but NOT for
        # one a human half-promoted by ADDING `goal_label` without removing this label, where the
        # goal label IS present and the gate has to be this label or nothing. See `_fetch_pending`.
        _handoff = (config.get("ledger") or {}) if isinstance(config.get("ledger"), dict) else {}
        _handoff = (_handoff.get("handoff") or {}) if isinstance(_handoff.get("handoff"), dict) else {}
        self.proposed_label = _handoff.get("proposed_label") or "sdlc:needs-confirmation"
        # #1352: the config-gated "blocking work sorts first" structural override -- see
        # `_blocking_priority_override`'s own docstring for the full mechanism.
        self.blocking_priority_override = _blocking_priority_override(config)
        # Optional single-owner scope for discovery. When set (e.g. "@me"), the loop only PICKS issues
        # assigned to that user, so several people can run the loop against one shared board without
        # grabbing each other's work. Absent/empty => no filter => byte-compatible with prior behavior.
        self.assignee = gh.get("assignee") or None
        self._assignee_login_cache = _UNRESOLVED   # #1216: resolved lazily -- see _assignee_login
        self._assignee_login_warned = False        # #1216: announce a resolution failure once
        # #698: the queue's ordering, and the label prefix priority is spelled with. The prefix is
        # configurable for the same reason `goal_label`/`parked_label` are — an adopted repo may
        # already spell its own severity scheme differently, and a hard-coded prefix would silently
        # rank every one of their issues as unprioritised.
        self.order = _order(config)
        self.priority_prefix = gh.get("priority_label_prefix", "priority:")
        self.priority_aliases = _priority_aliases(config)
        # #900: opt-in blocker-priority promotion. Resolved once here, matching priority_aliases
        # immediately above — `_promote_blockers` (called from `_sync_backlog`) reads this stored
        # value rather than re-resolving config on every board touch.
        self.blocker_promotion_mode = _blocker_promotion(config)
        # #2262: opt-in feature-priority tie-break. Resolved once here, matching
        # blocker_promotion_mode immediately above — `_pick_key` reads this stored value rather
        # than re-resolving config on every sort.
        self.feature_priority_enabled = _feature_priority(config)
        # #2263: opt-in "no work sits outside the structure" -- both resolved once here, matching
        # feature_priority_enabled immediately above. `feature_labels._handle_no_unit_at_pick` reads
        # both stored values (duck-typed via `getattr`, never re-resolving config) rather than
        # importing sources.py's config readers itself -- the same "the Source resolves config once,
        # feature_labels reads attributes" split `needs_label_label` already establishes.
        self.no_dangling_goal_enabled = _no_dangling_goal_enabled(config)
        self.no_dangling_goal_core = _no_dangling_goal_core(config)
        # #2380: the LIVE-JUDGE opt-in, stacked on top of `no_dangling_goal_enabled` above -- see
        # `_no_dangling_goal_live_judge_enabled`'s own docstring for why turning this on is a
        # SEPARATE, deliberate gesture from turning `no_dangling_goal` on at all. Resolved once
        # here, matching `no_dangling_goal_core`'s own pattern exactly; `feature_judge.live_judge`
        # reads all three attributes directly off this Source (never re-parsing config itself).
        self.no_dangling_goal_live_judge_enabled = _no_dangling_goal_live_judge_enabled(config)
        self.no_dangling_goal_live_judge_rounds = _no_dangling_goal_live_judge_rounds(config)
        self.no_dangling_goal_live_judge_spend_ceiling_usd_per_day = (
            _no_dangling_goal_live_judge_spend_ceiling_usd_per_day(config))
        # NEW, optional, trailing (#900): the local `.sdlc` directory, needed only for the ledger
        # half of blocker-promotion's reverse lookup (`ledger.read_all`/`ledger.outstanding` both
        # take a filesystem path, and GitHubSource otherwise has no notion of one — every OTHER
        # piece of state this class manages lives on GitHub itself, never on disk). Absent/None
        # (every pre-#900 construction site: triage.py's two direct constructions, and every test
        # in this file before this one) degrades that ONE channel to unavailable rather than
        # raising — the explicit "Blocked by #N" body-marker channel needs no filesystem path at
        # all and keeps working regardless. `get_source(sdlc_dir, config)` already receives
        # sdlc_dir for LocalSource; this just stops dropping it on the github branch. #2262 reuses
        # this exact channel for `feature_rank`'s registry read, same degrade-to-unavailable rule.
        self.sdlc_dir = sdlc_dir
        # #2262: `.sdlc/features/` read once per process and cached -- see `_feature_registry`'s
        # own docstring for why. `_UNRESOLVED` (module-level, shared with `_assignee_login_cache`)
        # distinguishes "not read yet" from "read, and it was empty/absent".
        self._feature_registry_cache = _UNRESOLVED
        self._raw_run = run or _run_gh
        self._labels_ready = False
        self._last_goal_raw_count = None   # #230: raw size of the last goal-label read, see census
        self._label_ids = None                   # {label name -> node id}, see `_label_node_ids`
        self._inferred_owner_name = None         # (owner, name) from cwd when repo is unset
        # Projects-v2 board (opt-in). An ABSENT `project` block => disabled, so existing github
        # configs behave exactly as before; the sigma-init template ships `enabled: true` for new repos.
        self._project_cfg = gh.get("project") or {}
        self.project_enabled = bool(self._project_cfg.get("enabled", False))
        _cols = self._project_cfg.get("columns") or {}     # board column names (configurable for existing boards)
        self.col = {k: _cols.get(k, d) for k, d in BOARD_COLUMNS}
        # #693: which representation DECIDES what is queued. "status" (default) makes the board's
        # `Ready` column authoritative and the goal label a written-but-never-decisive mirror;
        # "label" is the historical rule. This is only an ESCAPE HATCH — the real backward-compat
        # gate is structural and needs no config at all: a board with no `Ready` option cannot be a
        # queue, so every existing adopter falls through to the label rule untouched (`_ready_lane`).
        self.queue_source = self._project_cfg.get("queue_source", "status")
        # #719: a real Priority column. Which side decides depends on the queue in use: the board
        # queue ranks a card by this FIELD and falls back to its `priority:P*` label only when the
        # field is blank (`_card_rank`); the label queue (no board, or no `Ready` lane) ranks by the
        # label alone (`_pick_key`). `_mirror_priority` reconciles the two on every board sync: a
        # recognised field value wins and the label is rewritten to match; a blank field is filled
        # from a recognised label; an unrecognised field value is left alone. Falsy disables it.
        self.priority_field = self._project_cfg.get("priority_field", "Priority")
        # #233: the board's Phase column, written ONLY at a phase boundary (`set_board_phase`, called
        # by `phase_report.py start`). Falsy disables it; Priority is unaffected by that switch.
        self.phase_field = self._project_cfg.get("phase_field", "Phase")
        # #233 review: WHO WRITES the Priority field on this board. Sigma's field (a board this run
        # created, `project.setup_created` naming the pinned number+owner, or this opt-in) keeps
        # #719's field-wins rule and may be CREATED by the loop. Anyone else's board: the loop never
        # creates the field and never rewrites a label from it (`_priority_owned`).
        self.mirror_priority = self._project_cfg.get("mirror_priority") is True
        self._created_board_now = False  # `_ensure_board` created the board in THIS run
        self._priority_label_writes = True  # may `_mirror_priority` rewrite a label from the field?
        self._issue_labels = {}          # {issue number -> labels}, from `_sync_backlog`'s one read
        self._field_warned = False       # #233: printed the ONE Phase/Priority warning this run?
        self._priority_options = {}      # {P0..P4 -> option id}, filled by _ensure_status_field
        self._priority_field_id = None
        self._ro_attempted = False       # resolved the read-only board queue this run?
        self._ro_ready = None            # the board's `Ready` option name, or None => label queue
        self._item_status = {}           # {issue number -> current Status name}, filled by _load_items
        self._repo_resolved = None       # #308: `repo`'s current name, casefolded ("" unreadable)
        self._repo_current = ""          # #308: ... as GitHub spells it, for issue URLs
        self._repo_failed_at = None      # #308: monotonic time of the last FAILED resolve
        self._item_priority = {}         # {issue number -> current Priority value}, same source
        # Static values for the adopter's CUSTOM single-select board fields (name -> option name),
        # applied to issues the loop itself creates so a loop-made card isn't blank on a field like
        # Priority that every human-made card carries. Empty {} => historical behavior (Status only).
        self._custom_fields = self._project_cfg.get("custom_fields") or {}
        self._board_attempted = False           # tried to ensure the board this run (success or hard-fail)
        self._board_ready = False               # board fully wired (project + status field + item cache)
        self._project_number = None
        self._project_id = None
        self._field_id = None
        self._status_options = {}               # {option name -> single-select option id} for Status
        self._all_fields = {}                   # {field name -> {"id", "options": {opt name -> id}}} all single-selects
        self._items = None                      # {issue number -> board item id}, lazily loaded
        self._owner_had_boards = False          # did the owner already have project board(s)? (dup-guard)
        self._scope_warned = False              # emitted the missing-`project`-scope note yet? (once/run)
        # #1733: a board WRITE failure that _note_scope's narrow match doesn't recognise used to
        # vanish with zero trace. Keyed by the exception's own text (not a bare bool) so two
        # genuinely different failures in one process both get reported, not just the first.
        self._board_write_failures_warned = set()
        # #280: the columns already reported as having no matching Status option this run (one
        # warning each, never a silent no-op).
        self._unmatched_warned = set()
        self._ready_variant_noticed = False   # #280: `_notice_ready_variant`, once per instance
        # #905: was the MOST RECENT `_fetch_pending` call a genuine empty read, or a give-up after
        # exhausting retries? See `read_degraded()`.
        self._last_read_degraded = False
        # gh failures from the MOST RECENT release() call that were swallowed as best-effort --
        # see release_warnings(). Seen live in the predecessor 2026-09-28: releasing a stale claim
        # reported success (`released is True`) while the label removal and audit comment both
        # silently failed, only caught by checking GitHub directly afterward.
        self._last_release_warnings = []
        # #1661: the ONE unit of work this run is confined to (`--feature <name>`), or None for the
        # unscoped default. Set once, at the top of a run, through `scope_to_feature` -- never read
        # from config, because it is a property of THIS INVOCATION, not of the project. Both stay
        # None unless a caller asks, and every use of `feature_label` below is guarded on it, which
        # is what keeps an unscoped run byte-identical: same calls, same order, same output.
        self.feature = None
        self.feature_label = None

    def scope_to_feature(self, unit):
        """Confine every pick this source makes to `unit`, for the rest of this process.

        Validation lives in the MODULE-LEVEL `scope_to_feature` (the one entry point callers use),
        so this method is the mechanism alone. The label is derived through `feature_labels.
        label_for`, never spelled out here: the unit -> label projection has one definition, and the
        query has to ask for exactly the label the attach half writes."""
        self.feature = unit
        self.feature_label = feature_labels.label_for(unit)

    def _issue_in_feature(self, issue):
        """Is this issue a MEMBER of the run's unit? True whenever no scope is in force.

        MEMBERSHIP IS THE DECLARATION, NEVER THE LABEL ALONE, and that is the whole reason this is
        a client-side test over `features.read` rather than one more `--label` on the query. The
        label is attached AT PICK (`feature_labels.attach_at_pick`), so a member nobody has picked
        yet may carry none at all -- which is the normal state of every issue in a unit that has
        just been opened. A label-scoped query would therefore silently skip exactly the goals a
        scoped run exists to reach, and would report the freshly-filed unit as drained.
        `docs/branching-model.md` §14 states this as the contract; §4/§4c are the declaration pair
        and the rule that adjudicates them, and `features.read` IS that rule -- so this asks the
        one parser rather than re-deciding what a declaration is.

        AN ISSUE THAT CONTRADICTS ITSELF IS NOT A MEMBER. `features.read` raises `AmbiguousUnit`
        rather than guessing between two rival declarations, and its own docstring obliges every
        sweep to catch that PER ISSUE -- letting it propagate would turn one hand-edited issue into
        a total outage of the queue. Excluding is also the right answer on the merits: such an issue
        is refused by `attach_at_pick` anyway, so admitting it here would only move the refusal
        later and buy a wasted claim-lock round trip.

        CASE-INSENSITIVE, for the reason `feature_labels.is_feature_label` already gives: GitHub
        label names are case-insensitively unique, so `Feature:Voice` and `feature:voice` cannot
        both exist on a repo, and a case-sensitive compare would simply be bypassed by a capital."""
        if not self.feature:
            return True
        try:
            unit = feature_labels.features.read(issue).unit
        except Exception:                 # noqa: BLE001 - AmbiguousUnit, or any unusable payload
            return False                  # unresolvable is not membership; see the docstring
        return bool(unit) and str(unit).lower() == str(self.feature).lower()

    def pending_outside_feature(self, skip=()):
        """The goal this source WOULD have offered with no `--feature` scope in force, or None.

        The one call `--feature` adds, and it is paid on the TERMINAL path only -- once, by a run
        that is already stopping -- never per pick. It exists so a scoped run can tell an operator
        which of two different facts it is reporting: "the backlog is drained", or "your unit is
        drained and the board is not, and this run structurally cannot reach the rest of it".

        `_last_read_degraded` is saved and put back. `_emit_run_stop_once` consults
        `read_degraded()` on exactly this path to decide whether a `backlog-empty` row is honest,
        and this probe is another backlog read: leaving its verdict behind would let a probe that
        happened to succeed erase a real read failure from the pick before it. Ordering the call
        after the emit would work today and silently stop working the day somebody moves it."""
        if not self.feature_label:
            return None
        saved_unit, saved_label = self.feature, self.feature_label
        saved_degraded = self._last_read_degraded
        self.feature = self.feature_label = None
        try:
            return self.next_pending(skip=skip)
        except Exception as exc:              # noqa: BLE001 - a diagnostic must never break a stop
            print("sources.py: could not check whether the board has work outside %r (%s)"
                  % (saved_unit, exc), file=sys.stderr)
            return None
        finally:
            self.feature, self.feature_label = saved_unit, saved_label
            self._last_read_degraded = saved_degraded

    def _run(self, args):
        """Single chokepoint for every `gh` call. `project` subcommands are retried with bounded
        exponential backoff on transient API errors (e.g. the intermittent "unknown owner type") so a
        blip can't silently drop a board update; everything else passes straight through. Still
        fail-open: after the last attempt the error propagates to the board layer's try/except.

        #1468: it is ALSO where `gh label create <feature:*>` is refused. A feature label is created
        BY A HUMAN, never by Sigma: attaching one is additive and reversible, while creating one
        mutates the repository's namespace permanently and a typo in a body marker would mint junk
        that outlives the unit (`docs/label-model.md` §5's flagged tier).

        WHICH CALL SITE MAKES THE CHOKEPOINT LOAD-BEARING -- corrected, because the first version of
        this paragraph named the wrong one. There are three `gh label create` sites: `_ensure_labels`
        and `create_dependency` here, and `triage._ensure_arbitrary_labels`, which lives in ANOTHER
        MODULE and reaches for this method itself. A per-call-site rule would have covered the first
        two, so `create_dependency` does NOT justify the placement -- the triage site does, because a
        guard written into this file alone would leak there silently. (`create_dependency` is still
        where this matters TODAY rather than only under L3: `handoff.py` passes a free-text `--label`
        list straight into its per-label create loop, so `handoff track --label feature:voice` reaches
        it right now.)

        THE GUARANTEE IS CLI-VERB-SHAPED, and the bound is written down so nobody reads it wider than
        it is. What is refused is the `label create` verb. `gh label clone` (which copies EVERY label
        of another repo), `gh label edit --name`, a delete-and-recreate, and the REST form
        `gh api -X POST repos/{owner}/{repo}/labels` all still mint labels and are NOT covered. None
        appears anywhere in this tree today.

        THE REFUSAL LEAVES NO DURABLE TRACE, and that is accepted rather than overlooked: this
        method has no `sdlc_dir`, and threading one through the hottest path in the class for a
        diagnostic is not worth it while the single reachable path today (`create_dependency`, via
        `handoff`) already fails loudly with a non-zero exit a human sees. Follow-up: **#1508** --
        it matters once L3 stamping runs the same path autonomously, where a loud interactive
        failure becomes a silent unattended one.

        A SILENT no-op (plus one stderr line), not a raise: every `label create` call site in this
        codebase already discards the return inside its own `try/except: pass`, so raising would be
        swallowed and tell nobody. Note what the return value means -- a create `gh` REJECTS raises
        (`_run_gh`), so `""` is the SUCCESS shape, and a refusal is therefore indistinguishable from
        having created the label, not from having failed to. Nothing downstream is broken by that: an
        `issue create --label feature:x` against a repo with no such label fails loudly, which is the
        correct outcome for filing into a unit nobody has opened."""
        if feature_labels.creates_a_feature_label(args):
            try:
                sys.stderr.write(
                    "sigma: refusing to create the label %r — Sigma attaches an existing "
                    "feature label but never creates one; a human creates the first label of a "
                    "unit (see docs/label-model.md §5)\n"
                    % next((str(a) for a in args if feature_labels.is_feature_label(a)), ""))
            except Exception:
                pass
            return ""
        if not args or args[0] != "project":
            return self._raw_run(args)
        for attempt in range(self._PROJECT_RETRIES):
            try:
                return self._raw_run(args)
            except Exception as e:
                # #233: a runner that already KILLED a hung call marks it `no_retry` -- a timeout is
                # not a blip, and retrying it would spend the whole boundary budget on one card.
                if attempt == self._PROJECT_RETRIES - 1 or getattr(e, "no_retry", False) \
                        or not self._is_transient(e):
                    raise
                time.sleep(self._RETRY_BASE * (2 ** attempt))

    @classmethod
    def _is_transient(cls, exc):
        msg = str(exc).lower()
        return any(m in msg for m in cls._TRANSIENT)

    def _repo_args(self):
        return ["--repo", self.repo] if self.repo else []

    def _read_issue(self, goal, fields, comment_limit=None):
        """#895: one issue in `gh issue view --json <fields>` shape, REST first through
        `gh_api.read_issue`, with at most ONE `gh issue view` fallback (rate limit / 5xx / transport,
        never in a cloud session). Both calls go through `self._run`, so the label refusal, project
        retry and every recording fake see them. Raises GhApiError (an Exception) on failure."""
        return gh_api.read_issue(self._run, int(str(goal).lstrip("#")), fields, self.repo or None, gql_run=self._run,
                                 sdlc_dir=self.sdlc_dir, comment_limit=comment_limit)

    def _list_issues(self, fields, labels=(), state="open", cap=None, sort="created", direction="desc"):
        """#895 slice 2c: issues in `gh issue list --json <fields>` shape, REST first through
        `gh_api.list_issues_gh`, with at most ONE `gh issue list` fallback (rate limit / 5xx / transport,
        never in a cloud session). Both calls go through `self._run`. Newest first unless `sort` is
        `updated`; the breaker/log are shared with `_read_issue` (`self.sdlc_dir`). Raises GhApiError."""
        return gh_api.list_issues_gh(self._run, fields, repo=self.repo or None, labels=labels, state=state,
                                     cap=self._BOARD_ITEM_LIMIT if cap is None else cap, sort=sort,
                                     direction=direction, gql_run=self._run, fetch=fetch_issues_rest,
                                     sdlc_dir=self.sdlc_dir)

    # ---- #895 slice 3a: issue WRITES, REST first through `gh_api` (callers stop touching `_run`) ------
    # Each wrapper sends REST through `self._run` and, per `gh_api.WRITE_POLICY`, at most ONE `gh issue ...`
    # fallback through the same `self._run`; failures raise GhApiError (an Exception). `LocalSource` has
    # none of these, so callers guard with `hasattr(source, "_issue_comment")` etc.

    def _write_kw(self):
        return {"repo": self.repo or None, "fallback_run": self._run, "sdlc_dir": self.sdlc_dir}

    @staticmethod
    def _n(goal):
        return int(str(goal).lstrip("#"))

    def _require_feature_labels(self, names):
        """Layer 2 of the feature-label refusal (#1468): a `feature:*` label may be ATTACHED only if it
        already exists, because REST would otherwise mint it. One REST GET per feature-prefixed name;
        an absent label, or any failed lookup (fail closed), raises RuntimeError with `_run`'s refusal
        wording BEFORE any write. Returns True iff a feature label was present and verified."""
        verified = False
        for name in names:
            if not feature_labels.is_feature_label(name):
                continue
            try:
                ok = gh_api.label_exists(self._run, name, self.repo or None)
                why = ""
            except Exception as exc:          # noqa: BLE001 - fail closed on ANY lookup failure
                ok, why = False, " (could not verify it exists: %s)" % (getattr(exc, "hint", None) or exc)
            if not ok:
                raise RuntimeError(
                    "sigma: refusing to create the label %r — Sigma attaches an existing feature label "
                    "but never creates one; a human creates the first label of a unit "
                    "(see docs/label-model.md §5)%s" % (str(name), why))
            verified = True
        return verified

    def _issue_comment(self, number, body):
        return gh_api.comment_issue(self._run, self._n(number), body, **self._write_kw())

    def _issue_edit_body(self, number, body):
        return gh_api.edit_issue(self._run, self._n(number), body=body, **self._write_kw())

    def _issue_add_labels(self, number, labels):
        labels = [labels] if isinstance(labels, str) else list(labels)
        verified = self._require_feature_labels(labels)
        return gh_api.add_labels(self._run, self._n(number), labels, feature_labels_exist=verified,
                                 **self._write_kw())

    def _issue_remove_label(self, number, label):
        return gh_api.remove_label(self._run, self._n(number), label, **self._write_kw())

    def _issue_add_assignees(self, number, assignees):
        assignees = [assignees] if isinstance(assignees, str) else list(assignees)
        return gh_api.add_assignees(self._run, self._n(number), assignees, **self._write_kw())

    def _issue_create(self, title, body, labels=()):
        """-> the REST issue dict, or the URL string if the ONE `gh issue create` fallback ran."""
        labels = list(labels)
        verified = self._require_feature_labels(labels)
        return gh_api.create_issue(self._run, title, body, labels, feature_labels_exist=verified,
                                   **self._write_kw())

    def _issue_close(self, number, reason=None):
        return gh_api.close_issue(self._run, self._n(number), reason=reason, **self._write_kw())

    # --- #1391 step 1: the atomic-ish label swap primitive ---------------------------------------
    # Every lifecycle transition today spends 3-5 SEPARATE `gh issue edit` calls on one issue's label
    # set, each in its own `try/except: pass` (see `_offboard` below). Measured: that is a 2^3 lattice
    # with 7 bad end states, 5 of which are label sets the state machine cannot even name -- including
    # `{}` (zero-label limbo), which no label query anywhere can return, so nothing can ever find it
    # again. 17 such illegal states are reachable across the 7 transitions; live census found 126 of
    # 605 managed sigma issues (20.8 per 100) sitting in one.
    #
    # This collapses each transition to ONE mutation request. Verified live against scratch issue
    # #1390 before this was written:
    #   - `gh issue edit --add-label X --remove-label Y` is FOUR HTTP requests, and the remove and add
    #     are dispatched 0.67ms apart IN PARALLEL -- so the intermediate state is non-deterministic
    #     (the issue can transiently carry both labels or neither, and which is undefined).
    #   - The REST alternative (`PUT /issues/{n}/labels`) is one request but REPLACES the whole set,
    #     forcing a read-modify-write with NO compare-and-swap available (If-Match and
    #     If-Unmodified-Since are both hard-rejected, HTTP 400). A real lost update was reproduced
    #     with it: a concurrent `bug` label silently destroyed, exposure window 1345-1842ms.
    #   - A single GraphQL document carrying two ALIASED root mutations is one request AND needs no
    #     read-modify-write, because `addLabelsToLabelable`/`removeLabelsFromLabelable` are
    #     server-side DELTAS. Verified: `priority:P4` survived a swap it was never sent in.
    #
    # HONEST LIMIT, stated because it matters: one request is not one TRANSACTION. GraphQL root
    # mutations execute serially and do NOT roll back -- a valid remove paired with an invalid add
    # leaves the remove COMMITTED (proved). Two things make that acceptable where today's shape is
    # not: the failure is REPORTED (non-zero exit, `errors[].path` naming the failed alias) instead of
    # silently swallowed, and ADD IS ALIASED FIRST so a partial application leaves BOTH lifecycle
    # labels -- a state that is visible, detectable and correctable -- rather than NEITHER, which is
    # invisible to every query in the system. Both orderings were verified live.
    _LABEL_SWAP_RETRIES = 4          # total attempts for one swap (override to 1 in tests)
    _LABEL_SWAP_RETRY_BASE = 0.5     # backoff seconds: base * 2**attempt (override to 0 in tests)
    #: #2392: real, live incident -- two sessions on different machines picked the SAME issue within
    #: 45 seconds of each other, root-caused to exactly this class's claim-label write failing
    #: silently under GitHub's GraphQL secondary rate limit and the pick proceeding anyway. The
    #: generic `_LABEL_SWAP_RETRIES`/`_LABEL_SWAP_RETRY_BASE` above (4 attempts, 0.5s base -> 3.5s of
    #: total backoff) were sized for episodic 5xx/timeout blips, not for a SECONDARY rate limit
    #: specifically -- GitHub's own docs say to wait "at least one minute" before retrying one of
    #: those, and that is exactly the transient class (`"secondary rate"` is already in `_TRANSIENT`)
    #: the live incident hit. `mark_in_progress`'s claim write is not an ordinary lifecycle label:
    #: `_claim`'s own docstring (loop.py) calls it "the durable, cross-session signal every other
    #: session reads" -- with the local claim lock (#387) LOCAL-machine-only by its own docstring and
    #: the ledger's publish path separately best-effort, it is the ONLY cross-machine signal left. So
    #: it alone gets a larger, dedicated retry budget: 7 attempts / 1.0s base -> 1,2,4,8,16,32s of
    #: backoff (6 sleeps, 63s total), clearing GitHub's one-minute floor with a small margin while
    #: staying bounded -- a pick can never hang past roughly a minute waiting on this write, whatever
    #: happens. `_swap_labels`/`_swap_labels_best_effort` take `retries`/`retry_base` overrides for
    #: exactly this: every OTHER caller (`_offboard`, `complete`) is untouched, still on the generic
    #: constants above, byte-for-byte.
    _CLAIM_LABEL_RETRIES = 7         # total attempts for the claim write specifically (override to 1 in tests)
    _CLAIM_LABEL_RETRY_BASE = 1.0    # backoff seconds: base * 2**attempt (override to 0 in tests)
    #: The claim write's own escalation marker (see `_escalate_claim_label_failure`) -- fixed and
    #: greppable so a monitoring script can alert on it without parsing prose, and distinct from
    #: `_swap_labels_best_effort`'s own generic "<what> label write failed" line so the one write in
    #: the whole claim path that is a durable, cross-session signal is never mistaken for an ordinary
    #: best-effort lifecycle write that failed.
    _CLAIM_LABEL_FAILURE_MARKER = "CLAIM-LABEL-WRITE-FAILED"
    #: #1986: same shape as the label-swap retry above, for the identical reason -- a REJECT
    #: verdict's `note` comment is `sigma-goal-review` §7g's ENTIRE output (no label, no board move,
    #: nothing else), so a transient `gh` hiccup at exactly that moment must not be free to erase it.
    _NOTE_RETRIES = 4                # total attempts for one comment (override to 1 in tests)
    _NOTE_RETRY_BASE = 0.5           # backoff seconds: base * 2**attempt (override to 0 in tests)
    #: GitHub node ids are opaque base64-ish tokens. Validated before being inlined into a GraphQL
    #: document so a malformed/hostile id can never break out of the literal it sits in.
    _NODE_ID_RE = re.compile(r"^[A-Za-z0-9_=-]+$")

    def _owner_name(self):
        """`('owner', 'name')` for GraphQL, which takes `owner:`/`name:` separately and has no
        equivalent of the `--repo owner/name` flag every other call in this class uses.

        PLAN-REVIEW FIX (blocking): `discovery.github.repo` ships EMPTY in the sigma-init template and
        `/sigma-setup` explicitly supports leaving it unset — in that mode every `gh` call omits
        `--repo` and `gh` infers the repository from the working directory. The first version of this
        raised outright on an unset `repo`, which would have hard-failed `mark_in_progress` (and every
        other transition, once step 2 routes them here) on the SHIPPED DEFAULT CONFIG. Reproduced
        before this fix: `_swap_labels` raised 'needs discovery.github.repo set to owner/name'.

        So an unset `repo` now resolves the same way `gh` itself would — one `gh repo view` against
        the working directory — cached per process, since it cannot change mid-run. Returns
        `(None, None)` only when BOTH the config and the working directory fail to identify a repo,
        which is a genuinely unusable state the caller must raise on."""
        parts = (self.repo or "").split("/")
        if len(parts) == 2 and all(parts):
            return parts[0], parts[1]
        if self._inferred_owner_name is None:
            try:
                data = json.loads(self._run(["repo", "view", "--json", "owner,name"]) or "{}")
                owner = ((data.get("owner") or {}).get("login")) or ""
                name = data.get("name") or ""
                self._inferred_owner_name = (owner, name) if owner and name else (None, None)
            except Exception:
                self._inferred_owner_name = (None, None)
        return self._inferred_owner_name

    def _graphql(self, document):
        """One `gh api graphql` call returning parsed `data`, raising on a GraphQL-level error.

        `gh` exits non-zero and prints the `errors` array when a mutation fails, so an exception here
        already carries the failed alias -- but a document can also return HTTP 200 with a populated
        `errors` key alongside partial `data` (exactly the partial-application case this primitive's
        own docstring warns about), and that must NOT read as success. Checked explicitly."""
        raw = self._run(["api", "graphql", "-f", "query=" + document])
        payload = json.loads(raw or "{}")
        if isinstance(payload, dict) and payload.get("errors"):
            raise RuntimeError(f"graphql error: {json.dumps(payload['errors'])[:400]}")
        return (payload or {}).get("data") or {}

    @staticmethod
    def _gql_string(value):
        """A label name, safe to inline into a GraphQL string literal. Escapes backslash and quote,
        and REFUSES a control character outright rather than emitting a document that would parse
        into something other than the caller meant -- label names come from adopter config, so they
        are not this module's to trust."""
        text = str(value)
        if any(ord(c) < 0x20 for c in text):
            raise RuntimeError("refusing to inline a label name containing a control character: %r"
                               % text)
        return text.replace("\\", "\\\\").replace('"', '\\"')

    def _label_node_ids(self, wanted=()):
        """`{label name -> node id}` for the NAMED labels, cached across the process.

        REVIEW FIX (cloud review of the atomic-swap work, bug_001 -- this was a silent
        release-defeating bug). This used to fetch `labels(first: 100)` with no pagination and no
        explicit ordering, then look the wanted names up in whatever came back. GitHub's
        `LabelConnection` caps a page at 100 nodes, so on any repo carrying more than 100 labels --
        ordinary on a mature project, which is precisely the adopter this kit is aimed at -- the
        `sdlc:*` labels could simply not be in the window. `_swap_labels`' one-shot "a label created
        since the cache warmed" refresh could not rescue it either, because it re-ran the IDENTICAL
        unpaginated query. The swap then raised `unknown label(s) on this repo`,
        `_swap_labels_best_effort` swallowed it with one stderr line, and EVERY lifecycle transition
        silently stopped writing labels -- with `reconcile.py`'s repair path, which routes through
        this same primitive, equally dead. The release's governing property ("a park that cannot
        write its labels leaves the goal re-pickable, never invisible") would not have held for the
        class of adopter most likely to install it.

        Fetching BY NAME instead of paginating is the better of the two available fixes: cost is
        constant in the number of labels actually being written (one aliased query, at most once per
        distinct label per process) rather than linear in the repo's label count, and it cannot
        regress as a repo grows. `repository.label(name:)` returns null for a name that does not
        exist, which is exactly the signal `_swap_labels` needs.

        Only labels that were FOUND are cached, so a name that is missing today is re-queried on the
        next call -- which is what makes a label created mid-run (by `_ensure_labels` on a fresh
        repo) self-healing, without the cache-invalidation dance this used to need."""
        wanted = [l for l in dict.fromkeys(wanted) if l]
        if self._label_ids is None:
            self._label_ids = {}
        unknown = [l for l in wanted if l not in self._label_ids]
        if unknown:
            owner, name = self._owner_name()
            if not owner:
                raise RuntimeError("label swap could not identify a repository: set discovery.github.repo to owner/name, or run from inside the repo")
            fields = " ".join('a%d: label(name: "%s") { id name }' % (i, self._gql_string(l))
                              for i, l in enumerate(unknown))
            data = self._graphql('query { repository(owner: "%s", name: "%s") { %s } }'
                                 % (owner, name, fields))
            repo = data.get("repository") or {}
            for i, label in enumerate(unknown):
                node = repo.get("a%d" % i)
                if isinstance(node, dict) and node.get("id"):
                    self._label_ids[label] = node["id"]
        return self._label_ids

    def _assignee_login(self):
        """The LOGIN to match issue assignees against, or None to skip the check entirely.

        Two things make this more than `return self.assignee`:

        `@me` is a `gh` SERVER-SIDE alias, not a login. It is the documented value (and what this
        kit's own template suggests), so comparing it literally against `assignees[].login` would
        match nothing and filter the ENTIRE backlog away -- turning a defense-in-depth check into a
        total outage of the picker. It is resolved once, via the authenticated user, and cached.

        FAILS OPEN, deliberately. If the login cannot be resolved (offline, auth expired), this
        returns None and a caller's client-side check is skipped rather than excluding everything
        -- whereas failing closed would stop every pick on a transient error.

        WHAT "FAILS OPEN" COSTS DIFFERS BY CALLER -- stale claim corrected by #1829's own
        post-implementation review, so spelled out here rather than asserted once and left to rot
        again. `_board_queue` passes the RAW `self.assignee` value straight into the board query's
        own `assignee:` qualifier -- GitHub resolves `@me` server-side there, with NO dependency on
        this method succeeding, so a resolution failure costs that caller ONLY its client-side
        cross-check; the query-level scope is untouched, exactly as this docstring used to claim
        for every caller. `_fetch_pending` (#1829) cannot make that claim: REST's `assignee=`
        query param does not understand the literal string `"@me"` at all (a hard 422 from the
        live API, confirmed) -- that caller has no choice but to feed this method's resolved value
        into the query itself, so a resolution failure there costs the query's own scope too, not
        just the client-side check. #1829 narrows that exposure by re-resolving once PER RETRY
        ATTEMPT rather than once for the whole call (see that call site), but cannot remove it --
        REST has no `"@me"` of its own to fall back on the way the old `--assignee` CLI flag did.

        A FAILURE IS NOT CACHED. Only a successful resolution is. Failing open on one call is a
        defensible trade; making that failure sticky and silent is not -- a long-running loop that
        blipped once at startup would then run its entire session unguarded. So a failure is
        retried on the next call (by either caller) and announced once, matching this module's
        existing loud-on-degraded convention."""
        if self._assignee_login_cache is not _UNRESOLVED:
            return self._assignee_login_cache
        raw = (self.assignee or "").strip()
        if not raw:
            self._assignee_login_cache = None            # nothing configured: a settled answer
            return None
        if raw != "@me":
            # casefolded to match `_logins`, so a handle typed with display casing still matches
            self._assignee_login_cache = raw.lstrip("@").casefold()
            return self._assignee_login_cache
        try:
            login = ((self._run(["api", "user", "--jq", ".login"]) or "").strip() or "").casefold() or None
        except Exception as exc:              # noqa: BLE001 - fail OPEN, but not silently
            login = None
            if not self._assignee_login_warned:
                self._assignee_login_warned = True
                print(f"sources.py: could not resolve `@me` to a login ({exc}) — the client-side "
                      "assignee check is skipped this pick; a caller with an independent "
                      "server-side scope (the board query) is unaffected, but a caller that must "
                      "pass this value into its own query (the REST backlog fetch, #1829) runs "
                      "unscoped until this resolves", file=sys.stderr)
        if login:
            self._assignee_login_cache = login           # cache ONLY a success
        return login

    def _issue_node_id(self, issue):
        """The GraphQL node id for an issue NUMBER (the only handle the rest of this class uses)."""
        owner, name = self._owner_name()
        if not owner:
            raise RuntimeError("label swap could not identify a repository: set discovery.github.repo to owner/name, or run from inside the repo")
        data = self._graphql('query { repository(owner: "%s", name: "%s") { issue(number: %d) '
                             '{ id } } }' % (owner, name, int(issue)))
        node = ((data.get("repository") or {}).get("issue") or {}).get("id")
        if not node:
            raise RuntimeError(f"issue {issue} has no resolvable node id")
        return node

    def _swap_labels(self, issue, add=(), remove=(), retries=None, retry_base=None):
        """Apply a lifecycle label change to `issue` as ONE mutation request. See the block comment
        above for why this shape and not `gh issue edit` or the REST set-replace.

        `add` is applied FIRST (aliased `a`), `remove` second (aliased `r`) -- deliberate, see above.
        Labels already present in `add`, or already absent from `remove`, are no-ops server-side, so
        the whole call is IDEMPOTENT and safe to retry blind (verified live: re-running a completed
        swap exits 0 and changes nothing).

        RETRIES with exponential backoff on `_TRANSIENT` errors, unlike every label write in this
        class today: `_run` short-circuits its retry logic to `gh project` subcommands only
        (`if not args or args[0] != "project"`), so a lifecycle label write currently gets ZERO
        retries -- while drawing on the SAME `graphql` rate-limit budget that was exhausted six times
        in a single 24h drain (.sdlc/DRAIN-LOG.md). The measured failure model is EPISODIC (0 of 356
        confirmed silent losses in normal operation; every real failure a correlated quota/outage
        episode), and backoff is exactly the right mitigation for a correlated episode.

        `retries`/`retry_base` default to `_LABEL_SWAP_RETRIES`/`_LABEL_SWAP_RETRY_BASE` (`None`
        means "use the class default", not "zero retries") -- every existing caller is byte-for-byte
        unchanged. #2392: `mark_in_progress` passes `_CLAIM_LABEL_RETRIES`/`_CLAIM_LABEL_RETRY_BASE`
        explicitly, a larger budget for the one write that is a durable, cross-session signal rather
        than an ordinary best-effort lifecycle label -- see that constant's own comment for why.

        RAISES on final failure -- it does not swallow. Callers decide what a failed lifecycle write
        means; today they cannot, because they are never told."""
        retries = self._LABEL_SWAP_RETRIES if retries is None else retries
        retry_base = self._LABEL_SWAP_RETRY_BASE if retry_base is None else retry_base
        add, remove = [l for l in add if l], [l for l in remove if l]
        if not add and not remove:
            return False
        node = self._issue_node_id(issue)
        # Ask for exactly the labels this swap is about. `_label_node_ids` caches only what it
        # FOUND, so a name still missing here is genuinely absent from the repo -- no second
        # invalidate-and-refetch round is needed (or possible: the old one re-ran the same query).
        ids = self._label_node_ids((*add, *remove))
        missing = [l for l in (*add, *remove) if l not in ids]
        if missing:
            raise RuntimeError(f"unknown label(s) on this repo: {', '.join(sorted(missing))}")
        for token in (node, *(ids[l] for l in (*add, *remove))):
            if not self._NODE_ID_RE.match(str(token)):
                raise RuntimeError(f"refusing to inline a malformed node id: {token!r}")
        parts = []
        if add:
            parts.append('a: addLabelsToLabelable(input: {labelableId: "%s", labelIds: [%s]}) '
                         '{ clientMutationId }'
                         % (node, ", ".join('"%s"' % ids[l] for l in add)))
        if remove:
            parts.append('r: removeLabelsFromLabelable(input: {labelableId: "%s", labelIds: [%s]}) '
                         '{ clientMutationId }'
                         % (node, ", ".join('"%s"' % ids[l] for l in remove)))
        document = "mutation { %s }" % " ".join(parts)
        for attempt in range(retries):
            try:
                self._graphql(document)
                return True
            except Exception as exc:
                if attempt == retries - 1 or not self._is_transient(exc):
                    raise
                time.sleep(retry_base * (2 ** attempt))
        return False                                      # unreachable: the loop always returns/raises

    def _swap_labels_best_effort(self, goal, add=(), remove=(), what="label change",
                                 retries=None, retry_base=None):
        """`_swap_labels`, but never raising — the shape every lifecycle transition needs.

        #1391 step 2, and this wrapper is the whole reason step 2 is safe. `_swap_labels` RAISES by
        design (a caller must be able to know a lifecycle write failed), but the transitions that
        call it CANNOT propagate that:

          - `_offboard` IS the park path. `run_loop`'s #335 handler and `_record`'s #1201 handler
            both downgrade an exception into a park — so a raising park would be asked to park
            again, and the plan review flagged that as the most likely place in this whole change
            for a real bug.
          - `mark_in_progress` raising would stop a goal being picked at all, for a label the
            docstring itself calls best-effort visibility.
          - `complete` raising would lose the terminal record for a goal that genuinely finished
            (exactly the #1201 regression).

        So: swallow, but LOUDLY — one stderr line naming the goal, the transition and the error.
        Today's call sites swallow silently and in triplicate; this is strictly more informative
        while preserving the fail-open contract every one of them documents. Returns whether the
        swap landed, so a future caller (the reconciler) can act on it without changing this one.

        `retries`/`retry_base` (#2392) pass straight through to `_swap_labels` — `None` (the
        default) keeps every existing caller on the class-wide constants, unchanged; `mark_in_progress`
        is the one caller that passes its own, larger budget (`_CLAIM_LABEL_RETRIES`/
        `_CLAIM_LABEL_RETRY_BASE`)."""
        try:
            return bool(self._swap_labels(goal, add=add, remove=remove,
                                          retries=retries, retry_base=retry_base))
        except Exception as exc:                 # noqa: BLE001 - see docstring; must never propagate
            try:
                sys.stderr.write("sigma: %s label write failed for #%s (%s) — continuing; the "
                                 "reconciler will pick this up\n" % (what, goal, exc))
            except Exception:
                pass
            return False

    def _ensure_labels(self):
        # park-exclusion depends on the parked label existing; create the labels up front so a
        # missing label can't make a parked issue re-appear forever. Best-effort.
        #
        # #1917: NO `--force`. It reads like the flag that makes a create idempotent and it is not
        # — on a label that already exists `gh` uses it to OVERWRITE the colour and description,
        # and this loop runs on the pick path, so an adopter who recoloured `sdlc:goal` to match
        # their board had it repainted on every loop start, forever, with nothing reporting it.
        # Without the flag `gh` refuses an existing label (exit 1, "already exists; use `--force`
        # to update its color and description"), which is precisely the outcome wanted, and the
        # except below used to absorb it. Read the bounded REST inventory first so the common
        # already-bootstrapped case spends no rejected writes; an unreadable inventory deliberately
        # keeps the old create-and-ignore fallback, so a missing lifecycle label still self-heals.
        if self._labels_ready:
            return
        existing = self._existing_label_names()
        for attr, color in self._LABEL_COLORS:
            name = getattr(self, attr)
            if existing is not None and name.casefold() in existing:
                continue
            try:
                self._run(["label", "create", name, *self._repo_args(), "--color", color])
                if existing is not None:
                    existing.add(name.casefold())
            except Exception:
                pass
        self._labels_ready = True

    def ensure_labels(self):
        """Public entry point for `sigma-setup`'s adoption flow (`setup.py labels`) — the same
        idempotent, colour-preserving mechanism above, exposed for a caller OUTSIDE this class.
        Kept as a thin wrapper rather than renaming `_ensure_labels` itself so its dozen existing
        internal call sites (the hot claim/park path) need no change. See `_ensure_labels`'s own
        docstring for the mechanism and #1917 for why there is no `--force`."""
        self._ensure_labels()

    def _label_endpoint(self):
        return "repos/%s/labels" % self.repo if self.repo else "repos/{owner}/{repo}/labels"

    def _existing_label_names(self):
        """#230: the repository's label names, casefolded (GitHub label names are case-insensitive),
        read by paginated REST GET -- never GraphQL, and never `gh label list`, which is. None when
        the read fails or the page ceiling is hit, so the caller falls back to create-and-classify
        rather than trusting a partial set."""
        names, page = set(), 1
        try:
            while page <= self._LABEL_LIST_MAX_PAGES:
                items = json.loads(self._run(["api", self._label_endpoint(), "--method", "GET",
                                              "-f", "per_page=100", "-f", "page=%d" % page]) or "[]")
                names.update(str(i.get("name", "")).casefold() for i in items if isinstance(i, dict))
                if len(items) < 100:
                    return names
                page += 1
        except Exception:
            return None
        return None

    def ensure_labels_report(self):
        """#230: the MEASURED bootstrap -- every `_LABEL_COLORS` label plus `_PRIORITY_LABELS`,
        each reported as `created`, `existed`, or `failed` with the reason `gh` gave. Unlike
        `_ensure_labels` (the hot path, best-effort by design), nothing here is swallowed: a caller
        prints `render_label_report` and exits non-zero on any failure.

        Cost: one paginated REST read of the repo's labels (1 call below 100 labels), then one
        `gh label create` per label MISSING -- zero writes on a repo already bootstrapped. It is
        per-repository, not per-issue: at 100x issues the cost is unchanged; at 100x labels it is
        still one read per 100. An existing label is never written, so never recoloured (#1917: no
        `--force`). If the read fails the create is attempted and `gh`'s "already exists" refusal
        is classified as `existed`. A token that can apply but not create labels passes whenever
        the labels already exist, because nothing needs creating. On full success the hot-path
        `_ensure_labels` is marked done, so a run that bootstrapped here spends nothing there."""
        wanted, seen = [], set()
        for attr, color in self._LABEL_COLORS:
            name = getattr(self, attr)
            if name and name.casefold() not in seen:
                seen.add(name.casefold()); wanted.append((name, color, ""))
        for name, color, desc in self._PRIORITY_LABELS:
            if name.casefold() not in seen:
                seen.add(name.casefold()); wanted.append((name, color, desc))
        existing = self._existing_label_names()
        results = []
        for name, color, desc in wanted:
            if existing is not None and name.casefold() in existing:
                results.append({"label": name, "outcome": "existed", "reason": ""})
                continue
            args = ["label", "create", name, *self._repo_args(), "--color", color]
            if desc:
                args += ["--description", desc]
            try:
                self._run(args)
                results.append({"label": name, "outcome": "created", "reason": ""})
            except Exception as exc:
                reason = str(getattr(exc, "hint", "") or exc).strip()
                if "already exists" in reason.lower():
                    results.append({"label": name, "outcome": "existed", "reason": ""})
                else:
                    results.append({"label": name, "outcome": "failed",
                                    "reason": (reason.splitlines() or ["unknown error"])[0]})
        if not any(r["outcome"] == "failed" for r in results):
            self._labels_ready = True
        return results

    def goal_label_census(self):
        """#230: how many open issues carry `goal_label`, as 0 or 1 -- enough to tell "nothing is
        labelled at all" from "labelled goals exist but none is pickable". ONE REST read of one
        item (PRs filtered; `fetch_issues_rest` pages past a PR), unscoped by assignee on purpose:
        the question is whether the label is in use at all. None when the read fails, so a caller
        never claims zero it did not measure.

        Free when the pick itself already saw a labelled issue: `_fetch_pending` records the raw
        (pre-filter) size of its last goal-label read, and a non-zero one answers without a call.
        The REST read is spent only when that read was empty or never ran (a board-lane pick), to
        confirm the zero unscoped by assignee."""
        if self._last_goal_raw_count:
            return self._last_goal_raw_count
        try:
            return len(fetch_issues_rest(self._run, self.repo, [self.goal_label], 1))
        except Exception:
            return None

    # F447: `_next()` trusts a `next_pending` result of "nothing pending" as FINAL — one bad read
    # here ends the whole run (`("DONE", None)`, zero claims). Historically (#447) the backlog
    # fetch was a `gh issue list --label ...` call, which routes through GitHub's
    # asynchronously-indexed GraphQL search backend WHENEVER `--label` is present — confirmed with
    # `GH_DEBUG=api`, with or without `--search` also being passed — so it was only EVENTUALLY
    # consistent: a freshly created-and-labelled issue routinely took 1-5s to become visible to
    # that exact query (measured empirically, isolated scratch repo, zero other load).
    #
    # #1829 moved this fetch to a real REST endpoint (`_fetch_issues_rest`, below) specifically
    # because that graphql-search path was also the wrong RATE-LIMIT budget for a hot pick-path
    # call, not because of this consistency concern — REST's plain issues listing is generally
    # understood to read live repository state rather than an async search index, but this task
    # found no way to disprove the old lag applies to it too without risking a side effect against
    # a real, shared, concurrently-used production repo (creating a scratch issue to race a read
    # against it). So the retry below stays exactly as it was: it is cheap on the (overwhelmingly
    # common) genuinely-drained path regardless of which of the two reasons applies, and it is
    # unconditionally good insurance against ordinary transient network/API failures either way. A
    # transient `gh`/API error collapses into the identical "nothing pending" signal in the branch
    # below — both look, from `_next()`'s side, exactly like a drained backlog, which is the
    # failure mode this retry loop exists to not mistake for one.
    _BACKLOG_READ_RETRIES = 3       # total attempts before trusting "nothing pending"
    _BACKLOG_READ_RETRY_BASE = 1.0  # backoff seconds: base * 2**attempt (override to 0 in tests)

    def _ready_lane(self):
        """The board's `Ready` option name if this board can serve as the queue, else None.

        THE BACKWARD-COMPATIBILITY GATE, and it is structural rather than a flag: an existing
        adopter's board has no `Ready` option, so this returns None and every caller falls through
        to the historical label queue, byte for byte. Nothing here ever ADDS the option — migrating
        an existing board is an explicit, human-invoked command (#696), never a loop tick.

        #280 (review of PR #325): `Ready` is matched EXACTLY, never by #280's case/whitespace rule.
        A board lane spelled `READY` / `ready` / `Ready ` would otherwise switch the queue mode
        without anyone choosing it. Such a lane keeps the label queue and prints ONE notice per
        instance naming it; setting `project.columns.ready` to the board's spelling (or running
        board_migrate.py) is the explicit opt-in, and an exact configured name wins as before.

        Strictly READ-ONLY, deliberately: this runs on the pick path, and a queue read that quietly
        created a GitHub Project (which `_ensure_board` would) is exactly the kind of surprise an
        adopter should never meet. Provisioning stays where it was — on the first status WRITE.
        Resolved once per instance and cached; fail-open, because an unreadable board must degrade
        to the label queue rather than stop the loop."""
        if self._ro_attempted:
            return self._ro_ready
        self._ro_attempted = True
        if not self.project_enabled or self.queue_source != "status":
            return None
        try:
            owner = self._proj_owner()
            if self._project_number is None:
                number, pid, _ = self._find_project(owner, self._proj_title())
                if number is None:
                    return None                      # no board to read -> label queue
                self._project_number, self._project_id = number, pid
            fields = self._list_fields(owner, self._project_number)
            fld = self._find_field(fields, self._project_cfg.get("status_field") or "Status")
            names = {o.get("name") for o in ((fld or {}).get("options") or [])}
            self._adopt_column_spellings(names)   # #280: never `ready` -- see the docstring
            if self.col["ready"] in names:
                self._ro_ready = self.col["ready"]
            else:
                self._notice_ready_variant(names)
        except Exception as exc:
            self._note_scope(exc)
            self._ro_ready = None
        return self._ro_ready

    def _card_rank(self, item):
        """Priority rank for a board card: its Priority FIELD when set, else its label.

        The field decides because a human ranking work does it on the board — that is the whole
        point of having the column. A card whose field is BLANK falls back to its label rather than
        counting as unprioritised: the forgiving direction, so turning the column on can never
        silently lose a priority that was only ever expressed as a label.

        `self.priority_aliases` (#854 follow-up, second-round independent-review finding): this is
        the BOARD-QUEUE pick path — `next_pending` calls `_board_queue()` FIRST, before `_pick_key`'s
        label-queue path is ever reached, and `_board_queue()` is what most new repos actually run
        (`queue_source` defaults to `"status"`, board-authoritative, the moment `project.enabled` is
        true — the sigma-init default). Without aliases threaded through here, `_pick_key`/
        `_mirror_priority` being alias-aware was cosmetic for exactly the scenario this whole
        feature exists for: a Ready-lane board whose Priority field predates P0-P4. The raw field
        text (line below) needs the alias map to resolve at all; the label fallback
        (`_priority_name`) already resolves aliases internally and returns a canonical `P0`-`P4`
        string, so passing aliases here too is a no-op for that branch, not a double-resolve."""
        value = item.get((self.priority_field or "").lower()) if self.priority_field else None
        if not value:
            value = self._priority_name(item.get("labels"))
        return discovery.priority_rank(value, self.priority_aliases) if value else discovery.UNPRIORITISED

    def membership_labels(self):
        """The mutually-exclusive MEMBERSHIP set: in Sigma's world / a human's exit from it /
        not admitted to it yet. Carrying more than one of these is a contradiction."""
        return (self.goal_label, self.parked_label, self.proposed_label)

    def overlay_labels(self):
        """The ADDITIVE overlays. These ride alongside `goal_label` and are never a state of their
        own: `sdlc:in-progress` (someone is on it), `sdlc:blocked` (waiting on another ISSUE),
        `sdlc:needs-label` (#1468 -- waiting on a human to create a `feature:` LABEL),
        `sdlc:needs-unit` (#2263 -- waiting on a human to declare ANY unit at all) and
        `sdlc:needs-triage` (#2363 -- every tier of automatic classification was tried and failed).
        The three overlays share a shape and differ in their reason, which is exactly why they are
        three labels: see `needs_label_label`'s own comment for the sweep collision that made one
        label wrong, `needs_unit_label`'s for the identical argument applied to a different
        declaration state, and `needs_triage_label`'s for why the third has no self-healing sweep
        of its own.

        THIS IS THE CHOKEPOINT #2263's own issue body names: `not_eligible_labels` immediately
        below folds this tuple straight in, so registering `needs_unit_label` (and, since #2363,
        `needs_triage_label`) here is what makes the pick path, `_card_is_eligible`,
        `triage._bucket_enqueued`/`_ensure_arbitrary_labels`'s callers and `status._github_counts`'s
        picker-parity all see it at once, with no second hand-written copy anywhere.

        `blocking_label` is deliberately NOT here. It is an annotation ABOUT OTHER ISSUES'
        relationship to this one, derived and self-healing — and it is the one label Sigma may
        legitimately write to an issue it does not own (a parked blocker, a human's proposal, a
        third-party issue), precisely because it grants nothing."""
        return (self.in_progress_label, self.goal_blocked_label, self.needs_label_label,
                self.needs_unit_label, self.needs_triage_label)

    def not_eligible_labels(self):
        """THE eligibility rule, in ONE place. An issue is pickable iff it carries `goal_label` and
        none of these.

        #1393: this exists because the rule was written out by hand in at least six places —
        `_fetch_pending`, `_card_is_eligible`, `triage._bucket_enqueued`, `status._github_counts`,
        and two more — and an exhaustive audit found that FIVE of the six had drifted from each
        other, each missing a different label. The result was the same issue being pickable or not
        depending on which code path asked, which is precisely the class of bug this whole release
        exists to remove. A rule stated once cannot drift from itself."""
        return tuple(l for l in (self.parked_label, self.proposed_label, *self.overlay_labels(),
                                 self.blocked_label) if l)

    def _card_not_eligible(self, number, why):
        """One stderr line explaining a skipped Ready card. A silent no-pick reads exactly like a
        broken loop -- which is how the inverted board behaviour survived as long as it did."""
        try:
            sys.stderr.write(
                "sigma: board card #%s sits in %r but its issue %s — not picking it. Labels "
                "decide eligibility; the board decides order.\n" % (number, self.col["ready"], why))
        except Exception:                     # noqa: BLE001 - a diagnostic must never break a pick
            pass

    def _card_is_eligible(self, number, names, assignees=(), login=None):
        """#1391 step 3b: is this Ready card's ISSUE actually pickable, by its labels?

        The same rule `_fetch_pending` has always applied on the label path, stated once here for the
        board path: the goal label must be PRESENT, and no not-eligible label may be. Deliberately
        NOT excluding `in_progress_label` — see the call site.

        A card whose issue carries none of these labels at all (the commonest fresh-board shape: a
        human dragged a card into `Ready` without ever labelling the issue) is NOT eligible, and that
        is a real behaviour change: "drag it to Ready" alone no longer picks it. Announced loudly
        rather than silently, because a silent no-pick reads exactly like a broken loop — which is
        how the inverted behaviour survived this long in the first place."""
        blocked = [l for l in self.not_eligible_labels() if l in names]
        if self.goal_label in names and not blocked:
            # #1437: the OWNERSHIP half of "labels decide eligibility". #1216 closed this on the
            # label path (`_fetch_pending`), but the board path never reaches that code -- and
            # `queue_source: "status"` is what the template SHIPS, so this is the default a fresh
            # adopter runs. Without it, a shared board serves every owner's Ready cards to whoever
            # picks first. The board read already carries `assignees`; it was simply unused.
            if login and login not in _logins(assignees):
                # S5: report the login as the OPERATOR wrote it, not the casefolded form
                self._card_not_eligible(number, "is not assigned to %s" % (self.assignee or login))
                return False
            return True
        why = ("carries " + ", ".join(sorted(blocked)) if blocked
               else f"has no {self.goal_label} label")
        self._card_not_eligible(number, why)
        return False

    def _board_queue(self):
        """`(ready_numbers_in_board_order, carded_open_numbers)`, or None to use the label queue.

        ONE server-filtered read. `gh project item-list --query` filters SERVER-SIDE (verified:
        `--limit 5 --query "-status:Done"` returns five NON-Done items on a board whose first ~85
        are all Done — impossible for a client-side filter), so cost scales with the open item count
        rather than the board's total size: measured 91 items / 2.45s against a 268-item board.

        `is:open` is applied by the query, so a closed issue can never be picked however stale its
        card is. Order is the board's OWN item order, which is what makes card position the sprint
        priority — drag to reorder and the loop obeys.

        #1352: when `blocking_priority_override` is on, a Ready card also carrying `blocking_label`
        sorts ahead of every non-blocking Ready card, before priority — the SAME structural override
        `_blocking_priority_pending` applies on the label-queue path (this is the board-authoritative
        half of that same feature; the design doc's own adversarial review flagged that a fix to only
        one of the two paths would silently fail on whichever path a given repo's config routes
        through). The sort key's leading element is `1` for every card whenever the override is off
        (a constant across the whole list), so `ready.sort()` orders by `(_card_rank, feature_rank,
        pos, n)` exactly as before — byte-identical behaviour, unchanged relative order.

        #2264 (D-9/BR-9 of design #2253): `feature_rank` (`_board_feature_rank`) sits at
        the SAME depth `_pick_key` gave it on the label-queue path (#2262) — immediately after
        `_card_rank`, before board position — for the identical reason: this is a completely
        separate sort key from `_pick_key`'s, and fixing only one path silently fails on whichever
        path a given repo's `queue_source` config actually routes through. It is `discovery.
        UNPRIORITISED` (a constant) whenever `discovery.feature_priority` is off, which is what
        keeps this byte-identical to the pre-#2264 tuple whenever the term is unused — same
        contract `is_blocking` already gives above. See `_board_feature_rank`'s own docstring for
        the fidelity difference from `_feature_rank` this term ships with, disclosed on purpose."""
        ready_name = self._ready_lane()
        if not ready_name:
            return None
        try:
            # #1437: the query carries the assignee scope too, so this path has the SAME
            # defence-in-depth the label path has had all along. Without it "fail open" meant
            # something much weaker here than there: `_fetch_pending` still has its server-side
            # `--assignee` flag when the client-side check is skipped, whereas this lane had NO
            # ownership dimension at all and would serve every owner's Ready cards.
            #
            # The RAW config value is used, not the resolved login, because GitHub resolves `@me`
            # server-side -- so the common case costs no `gh api user` call at all. Verified
            # against a live board: `--query "is:open assignee:@me"` filters server-side, and a
            # login owning nothing returns 0 items.
            query = "is:open"
            if self.assignee:
                query += " assignee:%s" % self.assignee
            data = self._gh_json(["project", "item-list", str(self._project_number),
                                  "--owner", self._proj_owner(), "--format", "json",
                                  "--limit", str(self._BOARD_ITEM_LIMIT), "--query", query])
        except Exception as exc:
            self._note_scope(exc)
            print("sources.py: could not read the board queue (%s) — falling back to the label "
                  "queue for this pick" % exc, file=sys.stderr)
            return None
        items = (data.get("items") if isinstance(data, dict) else data) or []
        self._warn_truncated("open board items", len(items))
        ready, carded, stranded = [], set(), {}
        seen_ready = 0                        # #1437/S3: distinguishes "lane empty" from "filtered"
        # #235 (review of PR #279, block #2): the lanes the LOOP itself moves a picked goal into. A
        # goal card anywhere else -- Backlog, a human's own `Todo` / `Needs design`, or no Status at
        # all -- can never be picked on this path (only Ready is), and the uncarded fallback below
        # only covers issues with NO card. Counting Backlog alone left a board that gained `Ready`
        # while its goal cards sat in a human's lanes reading as a silent DONE.
        in_flight = {self.col[k] for k in ("in_progress", "qc", "done", "blocked", "parked")}
        not_eligible = set(self.not_eligible_labels())
        # #1437/S1: resolved ONCE per read. Called per card, a persistent resolution failure spawns
        # one `gh api user` subprocess PER READY CARD (measured: 25 cards -> 25 calls), and the
        # once-only warning makes that silent as well as slow.
        login = self._assignee_login()
        for pos, it in enumerate(items):
            n = (it.get("content") or {}).get("number")
            if n is None:
                continue
            carded.add(int(n))
            if it.get("status") == ready_name:
                # #1391 step 3b: LABELS DECIDE ELIGIBILITY, THE BOARD DECIDES ORDER.
                #
                # This lane used to decide eligibility ENTIRELY on its own — `status == ready_name`
                # and nothing else. Measured against the repo's own board fake, all six probes
                # passed: a Ready card carrying `sdlc:parked`, `sdlc:in-progress`, `sdlc:blocked`,
                # `sdlc:needs-confirmation`, or ZERO labels was picked, while a correctly-labelled
                # `sdlc:goal` card sitting in `Blocked` was not. So on the board path the label model
                # was not merely bypassed, it was INVERTED: every "do not pick me" label ignored, the
                # one eligibility label not required, and `sdlc:needs-confirmation` — the entire
                # AI-filed-awaiting-human-promotion gate — reduced to decoration.
                #
                # This is the DEFAULT every fresh adopter gets (`project.enabled: true` +
                # `queue_source: "status"` ship in the template, and `/sigma-setup` never touches
                # them), and it has ZERO production mileage: sigma runs `project.enabled: false`
                # and `os` runs `queue_source: "label"`, so both live repos are on the label queue.
                # That is precisely why this was never felt.
                #
                # The fix keeps #693's drag-to-reorder intact — the card's position still decides
                # ORDER among eligible cards; the labels only decide WHICH cards are eligible, which
                # is the same rule `_fetch_pending` has always applied on the label path.
                # `in_progress_label` is deliberately NOT part of the exclusion (occupancy is the
                # claim's job, not a label's — excluding it here would rebuild the un-unstickable
                # claim that the whole backbone effort exists to remove).
                seen_ready += 1
                names = set(it.get("labels") or [])
                if not self._card_is_eligible(n, names, it.get("assignees"), login):
                    continue
                # (priority, board position): tiers first, drag order WITHIN a tier. Board order
                # alone meant priority governed nothing here — measured on project #6, two P0s
                # (#254, #318) queued 10th and 11th behind eight P1s.
                is_blocking = (self.blocking_priority_override
                              and self.blocking_label in names)
                ready.append((0 if is_blocking else 1, self._card_rank(it),
                             self._board_feature_rank(names), pos, int(n)))
            elif it.get("status") not in in_flight:
                names = set(it.get("labels") or [])
                if self.goal_label in names and not (names & not_eligible):
                    lane = it.get("status")
                    stranded[lane] = stranded.get(lane, 0) + 1
        # #1437/S3: only when the lane is GENUINELY empty. A lane that is full but entirely
        # FILTERED (every Ready card is someone else's, or parked) also leaves `ready` empty, and
        # telling that operator to run a seeding migration is advice that cannot help them.
        if not ready and stranded and not seen_ready:
            self._warn_unseeded(stranded)
        ready.sort()
        return [n for _, _, _, _, n in ready], carded

    def _board_feature_rank(self, names):
        """feature_rank for a BOARD card (#2264, D-9/BR-9 of design #2253): the SAME
        term `_feature_rank` inserts into the label queue's `_pick_key` (#2262), at the SAME depth
        in `_board_queue`'s own separate sort key -- the design doc's own text, quoting the code's
        prior docstring, is that fixing one path alone "would silently fail on whichever path a
        given repo's config routes through."

        Zero extra calls: `names` is the same `set(it.get("labels") or [])` `_board_queue` already
        built for `_card_is_eligible` -- bare label strings, the shape `gh project item-list`
        returns them in (never `{"name": ...}` dicts, unlike the REST issue payload `_feature_rank`
        reads).

        MEMBERSHIP RESOLVES ONLY FROM THE LABEL HERE, NEVER A BODY READ -- and that is a real,
        DISCLOSED difference from `_feature_rank`, not an oversight. `gh project item-list` returns
        a card's labels but never its issue body (`next_pending`'s own #1661 comment says the same
        thing for the identical reason), so there is no declaration to parse the way
        `features.read` parses one on the label-queue path -- only `features.parse_labels` is
        available, over the label set alone. `feature_labels.attach_at_pick` is the ONLY writer of
        the `feature:<name>` label this can see, so a genuine unit member that has never been
        picked carries none yet (`_issue_in_feature`'s own docstring states the identical fact for
        the identical reason) and resolves `discovery.UNPRIORITISED` here -- sorting as if it
        declared no unit at all. That degrades gracefully, not silently: it self-heals in exactly
        one pick (the pick that attaches the label), and it can only ever UNDER-rank a genuine
        member as unranked -- never mis-rank one into the wrong unit's slot, since a label that IS
        present is exactly as trustworthy here as it is for `_feature_rank`.

        Reuses `_unit_priority_rank`/`_feature_registry` UNCHANGED -- the same registry read
        `_feature_rank` uses once it has a unit name in hand; only how the unit name itself gets
        resolved differs between the two paths."""
        if not self.feature_priority_enabled:
            return discovery.UNPRIORITISED
        try:
            unit = feature_labels.features.parse_labels(names)
        except Exception:                 # noqa: BLE001 - AmbiguousUnit, or any unusable payload:
            return discovery.UNPRIORITISED   # a card that can't be resolved isn't ranked by it
        if not unit:
            return discovery.UNPRIORITISED
        return self._unit_priority_rank(unit)

    def _warn_unseeded(self, stranded):
        """`Ready` exists, nothing is in it, and open goal cards that are eligible by their labels
        sit OUTSIDE it -- in Backlog, in a lane of a human's own (`Todo`, `Needs design`), or with
        no Status at all (`stranded`: {lane or None: count}; the loop's own post-pick lanes are
        never counted). That is an unfinished migration (#707), or a `Ready` lane that arrived on a
        board whose cards were never moved into it (#235, review of PR #279), not a drained backlog
        -- and it does not self-heal: the queue reads empty, so `_next()` reports DONE, so no status
        write happens, so `_sync_backlog` (the only thing that would promote a Backlog or blank
        card) never runs, and nothing ever moves a card out of a human's own lane. Silence here
        looks exactly like a finished sprint, which is why it has to be loud.

        ONCE PER RUN (per source instance; `loop.py` builds one per run): the queue is read on
        every pick attempt, and repeating the same line on each is noise that buries it. Costs no
        extra call: the `is:open` read already carries each card's labels and status.
        Best-effort; never raises."""
        if getattr(self, "_unseeded_warned", False):
            return
        self._unseeded_warned = True
        try:
            total = sum(stranded.values())
            where = ", ".join("%d in %s" % (n, "no Status" if lane is None else repr(lane))
                              for lane, n in sorted(stranded.items(), key=lambda kv: -kv[1]))
            sys.stderr.write(
                "sigma: the board has a %r lane but NOTHING in it, while %d %s card(s) sit outside "
                "it (%s) — this reads as an empty queue and will not fix itself: on this board only "
                "a card in %r is picked. Move them with `board_migrate.py --owner %s --project %s "
                "--backlog <lane> --apply` (once per lane), or drag them to %r.\n"
                % (self.col["ready"], total, self.goal_label, where, self.col["ready"],
                   self._proj_owner(), self._project_number, self.col["ready"]))
        except Exception:
            pass

    def next_pending(self, skip=()):
        """The next goal to work, by whichever representation is authoritative for this repo.

        Board-enabled with a `Ready` lane: Status decides. A card in `Ready` is queued; a card in any
        other column is not, which makes In Progress / QC / Done / Blocked structurally un-pickable
        rather than merely unlikely (the label era relied on the ledger claim to avoid re-picking
        in-flight work). An issue with NO card falls back to its label — the forgiving direction, so
        a goal filed but never carded cannot silently vanish; `_sync_backlog` cards it as `Ready` on
        the next board touch. Ready cards come first, in board order; uncarded labelled issues
        follow, in priority order.

        Everything else — no board, no `Ready` option, `queue_source: "label"`, or any board read
        that fails — uses the historical label queue unchanged.

        #1837: the two calls into the label machinery below carry OPPOSITE `primary` values on
        purpose. `_first_by_label` below (no board at all) IS the picker, so it fails closed on a
        persistent, configured-assignee resolution failure. The uncarded-safety-net call
        (`primary=False`) stays fail-open — the board's own Ready lane is the real ownership
        boundary in that mode, and this fallback only ever adds to what the Ready lane already
        found, never replaces it.

        #1661: A `--feature` RUN READS THE LABEL QUEUE, whatever `queue_source` says, and the
        trade is deliberate. Membership is the DECLARATION (`docs/branching-model.md` §14, and
        `_issue_in_feature`), and `gh project item-list` returns a card's labels but never its
        BODY — so the board simply cannot see whether an unlabelled card belongs to the unit, and a
        board-ordered scoped run would skip every member that has not been picked before. Losing
        drag-order WITHIN one unit is a far smaller loss than getting membership wrong, and it
        lasts only as long as the flag does; an unscoped run on the same repo is untouched."""
        skip = {str(s) for s in skip}
        board = None if self.feature else self._board_queue()
        if board is None:
            return self._first_by_label(skip)
        ready, carded = board
        for n in ready:
            if str(n) not in skip:
                return str(n)
        labelled = self._pending_by_label(skip, primary=False) or []
        uncarded = [i["number"] for i in labelled if int(i["number"]) not in carded]
        return str(uncarded[0]) if uncarded else None

    def read_degraded(self):
        """#905: True iff the MOST RECENT `next_pending()` call's underlying backlog read gave up
        after exhausting retries, rather than genuinely finding nothing pending — `_fetch_pending`
        returns `(0, None)`/`(N, None)` indistinguishably for both, so callers that need to tell
        them apart (`_next()`'s DONE branch, before it writes a `run_stop` row) check this instead
        of trying to read the ambiguity back out of `next_pending()`'s own return value.

        Reflects only the LAST read (reset at the top of every `_fetch_pending` call), not a sticky
        "this source is broken" latch — a later, genuinely successful read clears it. When
        `next_pending()` returns an actual goal, or when the project-board `ready`-lane fast path
        answers without ever calling `_fetch_pending`, this may hold a stale value from an earlier
        call; harmless, since callers are only expected to consult it on a `None`/DONE result."""
        return self._last_read_degraded

    def _first_by_label(self, skip):
        pending = self._pending_by_label(skip)
        return str(pending[0]["number"]) if pending else None

    _BACKLOG_FETCH_CAP = 200
    _REST_PAGE_SIZE = _REST_PAGE_SIZE    # class-attr alias of the module default (tests patch either)

    def _fetch_issues_rest(self, labels, cap, assignee=None):
        """#1829/#1833: this instance's own goal-label(s)/open/oldest-first REST fetch — a thin
        binding of the module-level `fetch_issues_rest` (this file's top) to `self._run` and
        `self.repo`, through THIS class's existing `self._run()` chokepoint (the same DI/test-
        injection seam every other call in this class already uses). Returns a flat list of
        normalized `{"number", "labels", "assignees", "body"}` dicts (REST has no field-selection
        concept, so all four always come back regardless of which the caller needs), PR-filtered,
        capped at `cap` total.

        The module function carries the full rationale (why REST over `gh issue list`, the
        `--method GET` requirement, repo-placeholder resolution, PR filtering, and the fixed-
        `per_page` fix for a real live-reproduced pagination bug) — this method exists so every
        EXISTING caller in this class (`_fetch_pending`, `list_needs_label`, `_sync_backlog`)
        keeps its unchanged instance-method call shape, always scoped to `state=open,
        sort=created, direction=asc` (the ordering every one of them relies on) and this
        instance's OWN `_REST_PAGE_SIZE`, which a test may patch per-instance without reaching
        into the shared module default.

        Raises on the first transport failure — does not retry internally. `_fetch_pending`'s own
        retry loop is the sole retry authority for this read; a second retry layer here would
        double the backoff silently and make `_BACKLOG_READ_RETRIES` stop meaning what its name
        says."""
        return fetch_issues_rest(self._run, self.repo, labels, cap,
                                  assignee=assignee, page_size=self._REST_PAGE_SIZE)

    def _fetch_pending(self, extra_labels, skip, primary=True, retry_empty=True):
        """One retried, filtered, sorted fetch — `(raw_count, pending)`, `pending` sorted by
        `_pick_key`, or `(0, None)` on an exhausted-retries read failure. `extra_labels` narrows
        the query (e.g. a single priority label, ANDed onto `goal_label`) without duplicating the
        retry/filter/sort machinery — the base fetch (via `_fetch_issues_rest`, capped at
        `_BACKLOG_FETCH_CAP`) is shared by every caller. `raw_count` (BEFORE parked/skip
        filtering) is what `_pending_by_priority` uses to tell "genuinely fewer than the cap
        exist" from "the cap was hit, more may be hiding beyond it" — filtering first would make a
        backlog that happens to filter down to <200 indistinguishable from one that never had more
        to begin with.

        #1829: `extra_labels` is a plain list of 0-1 extra label strings to AND onto `goal_label`
        (every real call site passes either `[]` or a single label) — no longer raw CLI args, now
        that there is no CLI-flag shape left to splice in.

        #1837: `primary` distinguishes the label-queue's OWN picker (`_first_by_label` and
        everything it calls through `_pending_by_priority`/`_pending_by_label` — default `True`,
        the common case, since most repos have no board) from `next_pending`'s board-mode
        "labelled but uncarded" safety net, which passes `primary=False`. See the retry loop below
        for what the distinction actually changes.

        #312: `retry_empty=False` is for an OVERLAY lane (`sdlc:blocking`) whose successful empty
        read is an answer -- the lane legitimately has no members almost always -- rather than the
        drained-backlog signal #447's empty-read retry insures. It changes only that branch: an
        exception is still retried exactly as before (empty-because-error is not
        empty-because-none), and the unresolved-assignee retry is untouched. Measured: a stock
        github `next_pending` with an empty blocking lane went from 3.005s (1s + 2s backoff on the
        overlay alone) to under 10ms with the same fake runner."""
        # #1661: `--feature <name>` costs ZERO extra calls: `_fetch_issues_rest` always returns
        # `body` (REST has no field selection to opt out of it), which is what `features.read`
        # needs to adjudicate the declaration pair, and `_issue_in_feature` filters the pool
        # client-side below — so a scoped run is otherwise byte-identical to an unscoped one: same
        # call, same fields, same order, same output.
        labels = [self.goal_label, *extra_labels]
        skip = {str(s) for s in skip}                 # goals a claim lease says belong to someone else
        self._last_read_degraded = False              # #905: reset every call -- see read_degraded()
        # #1829 REQUIRED FIX (Plan-Review missed this too -- caught only by running the new test
        # against the live API): `self.assignee` may be the literal string `"@me"` -- a `gh`-CLI
        # convenience alias `gh issue list --assignee` resolves CLIENT-SIDE before ever reaching
        # GitHub. The raw REST `assignee=` query param has NO such convenience: passing `"@me"`
        # verbatim gets a hard `422 Validation Failed` from the live API (confirmed), which would
        # fail EVERY pick on any install configured with `assignee: "@me"` -- this kit's own
        # documented/tested convention. `_assignee_login()` resolves `@me` to the real login
        # (cached ONLY on success, fails OPEN to None on a resolution hiccup -- see its own
        # docstring), and that SAME resolved value now scopes both the query AND the post-fetch
        # filter below -- never the raw, possibly-"@me" config value.
        #
        # RESOLVED INSIDE THE LOOP, ONCE PER ATTEMPT, not once for the whole call -- a REQUIRED
        # FIX found only by an independent post-implementation review (not by Plan-Review or the
        # first pass at this code), because it is about a coupling this REST migration introduces
        # rather than about REST itself. Before the query depended on this resolution, a transient
        # `_assignee_login()` failure cost only the (redundant) client-side check below, because
        # the server-side scope was the RAW `--assignee` CLI value, resolved independently and
        # atomically by `gh` itself, with no dependency on this method succeeding. Now that the
        # SAME resolved value also scopes the query, resolving it ONCE before the loop would let
        # one transient hiccup (a network blip on the one-time `gh api user` call) silently
        # disable BOTH layers for the ENTIRE call -- all `_BACKLOG_READ_RETRIES` attempts, not
        # just the attempt that hit it. Resolving it fresh each attempt gives it the SAME
        # self-healing chance the loop already gives every other transient failure it tolerates: a
        # resolution that fails on attempt 1 gets attempt 2 and 3 to clear before this read's own
        # retries are exhausted.
        #
        # #1837 (closing the HONEST RESIDUAL GAP #1829's own review named): `_fetch_pending` is
        # called both as the label-queue's OWN picker (`primary=True` -- where an unscoped read is
        # a genuine ownership-guarantee break, per AGENTS.md's SAFETY rule: refuse loudly rather
        # than proceed weakly) AND as `next_pending`'s board-mode "labelled but uncarded" safety
        # net (`primary=False` -- `next_pending`'s own `labelled = self._pending_by_label(skip,
        # primary=False) or []`). The board caller stays fail-OPEN on purpose, matching
        # `test_board_fails_open_when_the_login_cannot_be_resolved` and `_board_queue`'s own
        # sibling contract unchanged: the board's Ready lane is the real ownership boundary there,
        # and this fallback is explicitly a catch-all net, not a boundary itself. Only the primary
        # picker below fails closed.
        for attempt in range(self._BACKLOG_READ_RETRIES):
            last_attempt = attempt == self._BACKLOG_READ_RETRIES - 1
            login = self._assignee_login()
            # #1837: `self.assignee` truthy with `login is None` can ONLY mean a configured `@me`
            # whose resolution genuinely failed -- `_assignee_login()` returns None from an unset
            # `self.assignee` (nothing to refuse: no ownership guarantee was ever configured) or
            # resolves a literal login immediately (never None) -- so this is an exact signal for
            # "an ownership guarantee is configured and this attempt could not confirm it", with
            # no new state needed.
            unresolved = primary and bool(self.assignee) and login is None
            try:
                issues = self._fetch_issues_rest(labels, self._BACKLOG_FETCH_CAP, assignee=login)
            except Exception as exc:
                # F4/F447: this is called first, every iteration — an unguarded raise here crashed
                # run_loop's while-loop before a single goal could even be picked. A read we can't
                # trust must not be silently mistaken for "nothing left" either, so this is loud
                # (stderr) either way — but a TRANSIENT error (network blip, rate limit — the same
                # vocabulary `_is_transient` already uses for the `project` retry below) gets a
                # few chances to clear before this pick gives up on the whole backlog. A
                # non-transient error (bad repo, no auth) still fails on the first try, same as
                # before this retry existed.
                if last_attempt or not self._is_transient(exc):
                    print(f"sources.py: could not read the backlog ({exc}) — stopping", file=sys.stderr)
                    self._last_read_degraded = True    # #905: a give-up, not a genuine empty read
                    return 0, None
                print(f"sources.py: could not read the backlog ({exc}) — retrying (attempt "
                      f"{attempt + 1}/{self._BACKLOG_READ_RETRIES})", file=sys.stderr)
                time.sleep(self._BACKLOG_READ_RETRY_BASE * (2 ** attempt))
                continue
            if not extra_labels:
                self._last_goal_raw_count = len(issues)   # #230: read by goal_label_census, free
            pending = []
            # #1437/S1: `login` is resolved once per attempt above (not once per issue) -- see
            # the #1829 comment above for why it is resolved PER ATTEMPT rather than once for the
            # whole call, and for why it now ALSO scopes the REST query itself, not just this
            # per-issue cross-check.
            for i in issues:
                names = {l.get("name") for l in (i.get("labels") or [])}
                # #1198: `mark_in_progress` writes `in_progress_label` on every pick, durably and
                # visibly to every session (a GitHub label, not a local/instant-scoped marker) --
                # but until that fix this filter excluded ONLY `parked_label`, so an issue the loop
                # had itself just claimed could be re-offered as the very next pick, including as
                # the TOP pick (oldest-first ordering favours an already-claimed issue over a
                # never-touched one). `skip` (the ledger claim-lease view) already covers "another
                # actor holds this", but only while that actor's lease is still fresh; this closes
                # the gap for good, unconditionally, the same way `parked_label` already is. A
                # crashed run does not leave a goal stuck forever behind this: `loop.py`'s
                # `_auto_reclaim_stale_claims` releases (removes the label from) a claim whose
                # ledger lease has aged past `ledger.lease.ttl_hours`, putting it back in this same
                # candidate pool.
                #
                # #1205: `blocked_label` sits alongside both of the above in the SAME exclusion, so
                # it applies everywhere this filter runs -- every `_fetch_pending` call, which is
                # every call `next_pending`/`_pending_by_label` ever make, including next-batch's
                # repeated skip-augmented refills (each refill re-runs this same query+filter from
                # scratch, so there is no separate place a blocked issue could leak back in).
                # Unlike `parked_label`/`in_progress_label`, `blocked_label` is optional and unset
                # by default -- `self.blocked_label` is `None` unless a repo configures it, so the
                # membership test is guarded and short-circuits to byte-identical behavior when
                # unset.
                #
                # #1358 (against #1350's own gap): `goal_blocked_label` (default `sdlc:blocked`) is
                # the DIFFERENT, always-on, code-managed state `mark_blocked` writes -- unlike
                # `blocked_label` immediately above, it needs no config to be live and no `and`
                # guard here. Before this line existed, a `mark_blocked` call whose own
                # `--remove-label sdlc:goal` step (see `mark_blocked`'s docstring) hit a transient
                # `gh` error while its other two steps still landed left an issue carrying BOTH
                # `sdlc:goal` AND `sdlc:blocked` at once -- not a coherent state -- and this filter
                # never checked the second label, so `next_pending()` served it right back out,
                # including to a concurrent `next_batch()` slot on this repo's own
                # `parallel.goals.enabled: true` config.
                #
                # #1392: `proposed_label` (`sdlc:needs-confirmation`) joins the exclusion, closing a
                # divergence between this path and the board path. `_card_is_eligible` (the board
                # half, #1391 step 3b) has ALWAYS treated it as disqualifying; this filter never
                # did -- so an issue carrying BOTH `sdlc:goal` and `sdlc:needs-confirmation` (the
                # exact half-promoted state a human produces by ADDING the goal label instead of
                # REMOVING the proposal one) was PICKED here and SKIPPED there. Same repo, same
                # issue, opposite answers, decided by which queue_source the config happens to use.
                # `doctor._multi_state_label_scan` and `reconcile.classify` both already call that
                # combination drift; only the picker disagreed.
                #
                # Excluding is the safe direction, and the only one consistent with what the label
                # MEANS: `sdlc:needs-confirmation` is the human approval gate (#233), and the
                # approval gesture is REMOVING it -- so while it is still present, approval has not
                # happened, whatever else was added alongside. `/sigma-promote` (#1392) is the
                # sanctioned way to perform that removal atomically, and its `drift` bucket is what
                # surfaces any issue already stuck in this state.
                if any(l in names for l in self.not_eligible_labels()):
                    continue
                if str(i["number"]) in skip:
                    continue
                # #1661: `--feature <name>`'s exclusivity, applied to the pool every caller of this
                # function shares -- `_pending_by_priority`'s tier-widening queries and
                # `_blocking_priority_pending` both come through here, so neither can reach round
                # the scope with a second query the way a filter bolted onto one call site would
                # allow. Inert (one attribute read) on an unscoped run.
                if not self._issue_in_feature(i):
                    continue
                # #1216: defense-in-depth on ownership. Until this, the ONLY thing keeping a
                # shared-board multi-session setup off another owner's issues was the server-side
                # `assignee=` field `_fetch_issues_rest` adds. If `self.assignee` is falsy for ANY reason -- unset,
                # a config load failure, a stale config -- that flag vanishes and this filter had
                # nothing that would notice, so the candidate pool silently widened to the whole
                # repo. Reproduced live TWICE here: the loop picked another collaborator's issue,
                # recognised the mismatch only AFTER picking, and parked it -- which removes
                # `sdlc:goal` and so dequeues the work from its RIGHTFUL owner's queue too.
                # Both scopes should hold; this one holds even when the query-level one does not.
                # CASEFOLDED both sides: GitHub logins are case-insensitive for identity but
                # `assignees[].login` returns the CANONICAL case, so a config written with display
                # casing ("Me-Login" copied off a profile page) would match nothing and empty the
                # whole backlog -- precisely the catastrophic mode this check exists to prevent.
                if login and login not in _logins(i.get("assignees")):
                    continue
                pending.append(i)
            # #1837: `pending` non-empty here means the fetch itself succeeded and something
            # matched -- but with `unresolved` true, it matched an UNSCOPED query (no `assignee=`
            # made it to GitHub, and the client-side check above was a no-op with `login` falsy),
            # so the primary picker has no basis to trust these are the configured owner's issues.
            # Give it the SAME self-healing chance a transient exception already gets before
            # refusing outright, since the very next attempt re-resolves `login` fresh.
            if pending and unresolved:
                if last_attempt:
                    print("sources.py: the configured assignee could not be resolved after "
                          f"{self._BACKLOG_READ_RETRIES} attempts — refusing to serve an unscoped "
                          "read to the label-queue's primary picker (the board-mode uncarded "
                          "safety net is unaffected)", file=sys.stderr)
                    self._last_read_degraded = True    # #905: a give-up, not a genuine empty read
                    return 0, None
                print("sources.py: the configured assignee could not be resolved (attempt "
                      f"{attempt + 1}/{self._BACKLOG_READ_RETRIES}) — retrying rather than serving "
                      "an unscoped read to the primary picker", file=sys.stderr)
                time.sleep(self._BACKLOG_READ_RETRY_BASE * (2 ** attempt))
                continue
            if pending:
                pending.sort(key=self._pick_key)            # priority, then bug, then oldest-first (see _pick_key)
                return len(issues), pending
            if last_attempt or not retry_empty:
                return len(issues), None
            # Loud even on the happy path's empty case — an operator watching stderr sees WHY a
            # pick took a few extra seconds instead of silently wondering later why a goal that
            # "should" have been there wasn't, the exact blind spot #447 fell into.
            print(f"sources.py: backlog query came back empty on attempt {attempt + 1}/"
                  f"{self._BACKLOG_READ_RETRIES} — retrying before trusting it (a transient "
                  "read/propagation blip, not assumed to be a genuinely drained backlog)", file=sys.stderr)
            time.sleep(self._BACKLOG_READ_RETRY_BASE * (2 ** attempt))
        return 0, None   # unreachable — the loop above always returns by its last attempt

    def _blocking_priority_pending(self, skip, primary=True):
        """#1352: `blocking_priority_override`'s structural override on the LABEL-QUEUE path — any
        `goal_label` issue that ALSO carries `blocking_label` sorts ahead of EVERY other pending
        issue, regardless of priority tier. Reuses `_fetch_pending` completely UNCHANGED with an
        extra `--label blocking_label` filter — `gh issue list --label` ANDs repeated flags, so this
        is naturally scoped to issues carrying BOTH labels, and it inherits every existing safeguard
        for free: the retry/degraded-read machinery, the parked/in-progress/blocked exclusion, and
        `_pick_key` sorting for the tie-break AMONG multiple simultaneously-blocking issues (priority,
        then bug, then oldest-first — unaffected by this override, which only decides the FIRST
        split, never the ordering within it).

        Deliberately its OWN separate query rather than a change to `_pick_key`'s sort tuple or the
        tier-widening loop below: `_pick_key`'s `(priority, not_a_bug, number)` shape and the
        widening loop's `tier_rank >= best_rank` arithmetic are both built around priority tiers
        specifically (and have their own history of subtle bugs — #813/#815/#854/#861 all live
        here) — threading a second, priority-independent dimension through that machinery would risk
        the exact same bug class in a new place. A dedicated, narrowly-filtered query sidesteps the
        200-issue fetch-window problem entirely too: a real backlog realistically never has 200+
        SIMULTANEOUSLY blocking issues, so this query is never at risk of needing its own
        tier-widening equivalent.

        `None` on a genuine read failure (mirrors `_fetch_pending`'s own `(0, None)` sentinel) — the
        caller (`_pending_by_label`) treats a failed OR genuinely-empty check identically, both
        falling through to the unmodified, normal priority-tier pool.

        #1837: `primary` passes straight through to `_fetch_pending` unchanged -- see that
        method's docstring."""
        # #312: `retry_empty=False` -- an empty blocking lane is the normal case, and both a failed
        # and an empty check fall through to the same full pool, so re-reading it only cost ~3s of
        # backoff on every pick. A raised read is still retried (see `_fetch_pending`).
        _, pending = self._fetch_pending([self.blocking_label], skip, primary=primary,
                                         retry_empty=False)
        return pending

    def _pending_by_label(self, skip, primary=True):
        """The pending queue, MERGE-ordered: whatever `_pending_by_priority` computes (the FULL,
        unmodified candidate pool, unchanged from before #1352), with any `blocking_priority_override`
        matches moved to the front as a group, everything else following in its original order.

        #1352 review finding (BLOCKING, fixed here): the FIRST version of this short-circuited —
        returned ONLY the blocking-labelled subset the moment it was non-empty, skipping the normal
        fetch entirely. That is correct for `_first_by_label`'s "just want the single top pick" use,
        but `next_pending`'s board-mode fallback (`labelled = self._pending_by_label(skip,
        primary=False) or []`, then filtered to `uncarded`) needs the FULL pool, not just the
        blocking subset — if every currently-blocking issue happened to already be carded (sitting
        in Backlog, not yet promoted
        to Ready — a normal transient state, since promotion only happens on a WRITE path this READ
        never takes), `uncarded` filtered down to empty and `next_pending()` returned `None`: a false
        "nothing pending" that silently stopped the loop even though other, perfectly pickable,
        non-blocking goals genuinely existed. Merging instead of short-circuiting means the override
        can never make MORE of the pool invisible than before — it only ever reorders what was
        already going to be returned.

        Gated on `self.order == "priority"` (#1352 review finding, notable): `order: "created"`'s
        own, twice-independently-reviewed contract is that `_pick_key` alone decides — one bucket,
        strict issue-number order, priority (and now blocking status) never affecting the pick. The
        override is fundamentally a priority-tier concept ("sorts ahead of priority ordering"); under
        `order: "created"` there is no priority ordering for it to override, so it simply never
        engages, the same way the tier-widening logic a few lines below already refuses to run under
        this mode for the identical reason."""
        pending = self._pending_by_priority(skip, primary=primary)
        if pending is None:
            return None
        if self.blocking_priority_override and self.order == "priority":
            blocking_pending = self._blocking_priority_pending(skip, primary=primary) or []
            if blocking_pending:
                blocking_numbers = {p["number"] for p in blocking_pending}
                rest = [p for p in pending if p["number"] not in blocking_numbers]
                return blocking_pending + rest
        return pending

    def _pending_by_priority(self, skip, primary=True):
        # F12: a bare listing defaults to created-DESC (newest first) with no way to ask it for
        # ASC — so fetching only 200 of a backlog > 200 fetched the 200 NEWEST, and sorting THOSE
        # ascending + taking [0] gave the oldest-of-the-newest-200, not the true oldest: the genuinely
        # old goals (highest priority under oldest-first) starved until newer ones drained the backlog
        # below 200. `_fetch_issues_rest`'s `sort=created&direction=asc` REST params (#1829; formerly
        # `--search "sort:created-asc"`, which routed the call through the graphql-search-billed
        # field instead, at ~2600+ points/call against the shared 5000/hr budget every pick draws
        # from) ask for the same thing on the cheap `core` resource instead, so the fetched page
        # already IS the 200 oldest.
        #
        # #815: that page is still ONLY the 200 oldest — a P0 filed after 200 older, lower-priority
        # goals stayed invisible until they drained, because `_pick_key` can only re-sort what
        # actually got fetched. Fully inert unless the cap is genuinely hit (`raw_count ==
        # _BACKLOG_FETCH_CAP`, checked on the UNFILTERED count — see `_fetch_pending`'s own
        # docstring for why): both this repo's own backlog and every other one measured so far sit
        # under 200 open goals, so the widening path below never runs today, and behaviour for a
        # backlog that stays under the cap is BYTE IDENTICAL to before this fix.
        #
        # When the cap IS hit, only tiers STRICTLY MORE URGENT than the best one already found in
        # the fetched window can possibly change the answer: the window holds the 200 OLDEST issues
        # overall, so anything of the SAME OR WORSE tier sitting beyond it is, by definition, NEWER
        # than every same-tier member already in hand — the in-window pick for that tier is already
        # its true oldest member. One small, targeted, single-label query per more-urgent tier
        # (cheap: at most 4 extra calls, typically 0 when the best tier found is already P0) checks
        # whether anything is hiding at a tier the capped window couldn't see far enough to reach.
        raw_count, pending = self._fetch_pending([], skip, primary=primary)
        if pending is None:
            # #1834: `_fetch_pending` returns its `(N, None)` shape for TWO different things a
            # bare `None` cannot tell apart -- a genuine read failure after exhausting retries, and
            # a successful read whose client-side filters (not-eligible labels/skip/feature-scope/
            # assignee) narrowed the result to zero. Bailing out here on EITHER meaning skipped the
            # feature-label widening query below even on the second, successful-but-empty-in-window
            # meaning -- exactly the case that query exists to rescue (a brand-new `--feature` unit
            # whose entire membership sits beyond the 200-issue window). `read_degraded()` is set
            # ONLY by the exception path (never by a merely-filtered-to-empty result -- see its own
            # docstring), so it is the one signal that actually distinguishes the two: a genuinely
            # failed read still returns `None` immediately, exactly as before, while a successful
            # read that filtered to zero falls through with `pending` as an EMPTY list instead.
            if self.read_degraded():
                return None
            pending = []
        # #1661: THE ONE PLACE THE `feature:` LABEL IS STILL QUERIED, and it is here because this is
        # the one thing the client-side membership test cannot do: reach past the window. The base
        # read is the 200 OLDEST goals, and a unit is usually NEW work -- so on a backlog deeper
        # than the cap, a scoped run would find its own unit entirely invisible and report it
        # drained, which is the most misleading thing this flag could do. One extra query, only
        # when the window is genuinely full (the same "pay only on a cap-hit" rule the tier
        # widening below already follows -- inert on this repo's own 101-goal backlog and on every
        # other one measured so far), and it still passes through `_fetch_pending`'s own membership
        # filter, so a label that disagrees with its issue's body is no more admitted here than
        # anywhere else. NOT gated on `order` the way the tier loop below is: reach is not a
        # priority concept, and `order: "created"` needs it just as much.
        #
        # THE RESIDUE, NAMED RATHER THAN LEFT TO BE FOUND: a member that declares the unit in its
        # BODY ONLY and sits beyond the window is still unreachable, because no server-side filter
        # can select on a body marker. That is #815's existing limitation, not a new one -- a
        # newly-filed P0 is invisible for the same reason -- and it has the same two remedies:
        # attach the label, or let the window drain.
        if raw_count >= self._BACKLOG_FETCH_CAP and self.feature_label:
            _, beyond = self._fetch_pending([self.feature_label], skip, primary=primary)
            if beyond:
                seen = {p["number"] for p in pending}
                pending = sorted(pending + [b for b in beyond if b["number"] not in seen],
                                 key=self._pick_key)
        if raw_count < self._BACKLOG_FETCH_CAP or self.order != "priority":
            # Independent review, before merge: the widening loop below MUST NOT run under
            # `order: "created"` — that mode's whole contract (`_pick_key`: "one bucket -> number
            # alone") is that priority never affects the pick. Without this guard, `best_rank`
            # still evaluates to UNPRIORITISED (5) for that path (every label is equally
            # "unranked" under `_pick_key`'s created-order branch), so the loop below would fire
            # on every cap-hit and let ANY priority-labelled issue beyond the window hijack the
            # answer — even one newer than the window's true oldest, silently violating the mode's
            # own guarantee. Reproduced directly: a 200-issue unprioritised window with a hidden P0
            # beyond it, under `order="created"`, returned the hidden P0 instead of the correct
            # true-oldest before this guard existed.
            return pending or None                      # nothing beyond the window — done, as before
        # #1834: `pending` can now genuinely be `[]` here (the window's own filters excluded
        # everything, and feature-label widening -- if it ran -- found nothing either), where
        # before this fix it was always non-empty by construction (`_fetch_pending` never returned
        # an empty list, only `None`, and the gate above returned before reaching this line on that
        # `None`). `pending[0]` would `IndexError` on that shape. `UNPRIORITISED` is the same rank
        # `_pick_key` already gives an unlabelled issue, so an empty window makes the loop below
        # check EVERY tier rather than stopping immediately -- the correct answer, since nothing
        # already in hand can out-rank whatever widening might still find beyond the window.
        best_rank = self._pick_key(pending[0])[0] if pending else discovery.UNPRIORITISED
        for tier_rank, tier_name in enumerate(discovery.PRIORITIES):
            if tier_rank >= best_rank:
                break                                    # this tier, and everything less urgent, can't win
            # #854 (third-round independent review): a `gh issue list` label filter is an exact
            # string match — it has no way to know "critical" means the same tier as "P0" the way
            # `discovery.priority_rank` does, so an alias-labelled issue hiding beyond the window
            # needs its OWN query under its own label text, not just the canonical one. Only ever
            # extra calls when `priority_aliases` is actually configured — with it unset (the
            # default), `label_texts` is just `[tier_name]`, byte-identical to before this existed.
            label_texts = [tier_name] + [alias for alias, canon in (self.priority_aliases or {}).items()
                                         if discovery.priority_rank(canon) == tier_rank]
            # #861: a tier's population can straddle BOTH the canonical label and a configured
            # alias — returning on the first non-empty query (as this used to) picked whichever
            # spelling happened to be queried first (canonical always leads `label_texts`), not
            # necessarily that tier's true oldest member. Every `label_text` for this tier is
            # queried and the hits are MERGED before picking, so the actual oldest across every
            # spelling wins. Still at most one extra call per alias (unchanged cost when no
            # alias hits), and with no `priority_aliases` configured `label_texts` is a single
            # element, so this degrades to exactly the old one-query-one-answer behaviour.
            tier_hits, seen = [], set()
            for label_text in label_texts:
                _, tier_pending = self._fetch_pending([self.priority_prefix + label_text], skip,
                                                       primary=primary)
                for issue in tier_pending or []:
                    if issue["number"] not in seen:        # an issue could carry both spellings at once
                        seen.add(issue["number"])
                        tier_hits.append(issue)
            if tier_hits:
                tier_hits.sort(key=self._pick_key)
                return tier_hits                            # more urgent than anything the wide window saw
        return pending or None

    def _pick_key(self, issue):
        """Sort key for the pending queue: (priority rank, feature rank, not-a-bug, issue number).

        Issue number alone was the historical key. Priority now leads it, using the SAME ranking the
        local source applies to a goal file's `priority:` frontmatter (`discovery.priority_rank`), so
        one definition governs both backends. An issue carrying SEVERAL priority labels ranks by the
        most urgent — not hypothetical, issue #381 on this repo carries both `priority:P0` and
        `priority:P1`. Absent or unrecognised ranks last, which is what makes a backlog that uses no
        priority labels at all sort exactly as it did before (#698).

        #2262 (D-3 of design #2253): `feature_rank` sits immediately after `priority_rank`
        — a LEXICOGRAPHIC tuple, never a weighted sum, because "never a replacement for issue
        priority" has to be structural: a P0 bug in any unit still outranks every P1, no matter how
        the unit-priority term compares, purely because Python tuple comparison never even looks at
        position 2 until position 0 and 1 both tie. See `_feature_rank`'s own docstring for what it
        resolves to and when.

        #813: bare issue number was the ONLY tie-break within a tier — strictly oldest-first. Two
        issues sharing a priority tier are common (P0 is rarely used; most real backlogs cluster in
        P1-P2), and oldest-first among THOSE has no relationship to severity: an older tech-debt or
        enhancement goal in the same tier as a newer production bug outranked it purely by filing
        date. `bug` is an EXISTING, already-asserted label (not a new taxonomy this introduces) —
        within one priority tier, a bug-labeled issue now sorts ahead of a non-bug one; issue number
        remains the tie-break within THAT split, so two bugs (or two non-bugs) in the same tier still
        resolve oldest-first exactly as before.

        NOTE, and it is a real limitation: this reorders only WITHIN the fetched page. `next_pending`
        asks for the 200 OLDEST matching issues (`sort:created-asc --limit 200`), so on a backlog
        deeper than that a recently-filed P0 stays invisible until older goals drain. That predates
        this change — under strict oldest-first the window was harmless, because the minimum of the
        oldest 200 IS the global minimum; it stops being harmless the moment the key is no longer
        the same one the server sorted by. Reading the board directly (#693) removes the window."""
        if self.order == "created":
            # strict, priority-blind FIFO — feature_rank is a priority-tier concept exactly like
            # `blocking_priority_override` (see its own `order == "priority"` guard elsewhere in
            # this class) and must never engage here either, config on or off. Both slots pinned
            # to the SAME sentinel so every tuple `_pick_key` ever returns is the same length —
            # mixing 3- and 4-element tuples in one `sorted()` call compares a tied prefix as
            # "shorter is less", which would silently reintroduce a priority-shaped signal into a
            # mode whose whole contract is that none exists.
            return (discovery.UNPRIORITISED, discovery.UNPRIORITISED, 0, issue["number"])
        names = [l.get("name") or "" for l in (issue.get("labels") or [])]
        not_a_bug = 0 if "bug" in names else 1
        ranks = [discovery.priority_rank(n, self.priority_aliases) for n in names if n.startswith(self.priority_prefix)]
        return (min(ranks) if ranks else discovery.UNPRIORITISED, self._feature_rank(issue),
                not_a_bug, issue["number"])

    def _feature_rank(self, issue):
        """feature_rank (#2262, D-3/D-2 of design #2253): `discovery.priority_rank`
        applied to the ISSUE's declared unit's own recorded priority (#2261's `priority` field on
        the unit's `.sdlc/features/` registry entry) — `discovery.UNPRIORITISED` when the issue
        declares no unit, the unit's registry entry carries no priority, or
        `discovery.feature_priority` is switched off. All three collapse to the SAME sentinel,
        which is what keeps the pick order byte-identical to before this term existed whenever
        none of them applies, and consulted at every tier (D-2) because it is simply the next
        element of `_pick_key`'s tuple — there is no cut, and no code path here asks the tier.

        MEMBERSHIP IS THE DECLARATION, NEVER THE LABEL ALONE — the same reason `_issue_in_feature`
        gives for the exact same read: the `feature:<name>` label is attached AT PICK
        (`feature_labels.attach_at_pick`), so a member nobody has picked yet may carry none at all.
        `features.read(issue)` resolves the declaration instead, against the FULL issue body
        `fetch_issues_rest` already fetched (GitHub's issues REST endpoint returns it whole) — zero
        extra calls, and correct for a member that has never been picked (D-9/BR-33 of the design)."""
        if not self.feature_priority_enabled:
            return discovery.UNPRIORITISED
        try:
            unit = feature_labels.features.read(issue).unit
        except Exception:                 # noqa: BLE001 - AmbiguousUnit, or any unusable payload:
            return discovery.UNPRIORITISED   # an issue that can't be resolved isn't ranked by it
        if not unit:
            return discovery.UNPRIORITISED
        return self._unit_priority_rank(unit)

    def _unit_priority_rank(self, unit):
        """The rank of `unit`'s own recorded `priority` field in `.sdlc/features/` (#2261), or
        `discovery.UNPRIORITISED` when there is no registry to read (`self.sdlc_dir` unset — the
        same degrade every other filesystem-optional channel on this class already takes, see its
        own `__init__` comment) or no entry for this unit. Matched CASE-INSENSITIVELY, the same
        rule `feature_registry.resolve_open_unit` already applies to a unit name: the registry is
        free to record a unit under whatever casing it was first declared with, and a caller's own
        casing must not silently miss it."""
        registry = self._feature_registry()
        if not registry:
            return discovery.UNPRIORITISED
        lowered = str(unit).lower()
        for name, entry in registry.items():
            if str(name).lower() == lowered:
                return discovery.priority_rank(entry.get("priority"), self.priority_aliases)
        return discovery.UNPRIORITISED

    def _feature_registry(self):
        """`.sdlc/features/`'s full registry (`feature_registry.read`), read once per process and
        cached — the registry lives on local disk, not GitHub, but a pick sorts potentially
        hundreds of issues through `_pick_key` and `feature_registry.read` walks every per-unit
        shard file on each call, so caching once per run is the same posture `_assignee_login`
        already takes for a value that cannot change mid-run. `None` (never `{}`) when `sdlc_dir`
        was never given to this instance, so "no registry to read" and "read an empty registry"
        stay distinguishable internally even though both rank UNPRIORITISED at the call site.
        `feature_registry.read` is documented never to raise (a missing/corrupt registry degrades
        to `{}`), so nothing here needs to guard against it doing so."""
        if self._feature_registry_cache is _UNRESOLVED:
            self._feature_registry_cache = (
                feature_registry.read(feature_registry.registry_dir(self.sdlc_dir))
                if self.sdlc_dir else None)
        return self._feature_registry_cache

    def mark_in_progress(self, goal):
        """Returns whether the claim-label write actually landed (`True`), or genuinely could not
        after exhausting its own retry budget (`False`) -- see `_escalate_claim_label_failure`. Still
        best-effort in the sense that matters most: this NEVER raises, and the caller (`_claim`,
        loop.py) never gates the pick on the return value -- a transient `gh` error must not stop a
        pick. #2392 explicitly does NOT change that; it only makes a genuine, retried-out failure
        loud instead of a single easily-missed stderr line."""
        self._ensure_labels()
        # #1391 step 2: routed through the swap primitive for its RETRIES (this write previously got
        # zero -- `_run` short-circuits retry to `gh project` only -- while drawing on the same
        # graphql quota measured exhausted six times in one 24h drain). Still best-effort: the loop's
        # own state/ledger track real progress, so a transient gh error must not stop a pick.
        #
        # #2392: this write is not an ordinary lifecycle label -- see `_CLAIM_LABEL_RETRIES`'s own
        # comment for why it gets ITS OWN, larger retry budget instead of the generic
        # `_LABEL_SWAP_RETRIES`/`_LABEL_SWAP_RETRY_BASE` every other `_swap_labels_best_effort` caller
        # still uses, untouched.
        ok = self._swap_labels_best_effort(goal, add=[self.in_progress_label], what="claim",
                                           retries=self._CLAIM_LABEL_RETRIES,
                                           retry_base=self._CLAIM_LABEL_RETRY_BASE)
        if not ok:
            self._escalate_claim_label_failure(goal)
        self._set_board_status(goal, self.col["in_progress"])
        return ok

    def _escalate_claim_label_failure(self, goal):
        """#2392 -- a real, live incident: two sessions on different machines picked the SAME issue
        within 45 seconds of each other, root-caused to this exact write failing under a GraphQL
        secondary rate limit and the failure going unnoticed. `_swap_labels_best_effort` already
        writes one stderr line on ANY failed lifecycle label write, but that line is identical in
        shape to a park's or a complete's -- ordinary chatter under real load, which is precisely
        what the incident report named as "easy to miss".

        This is the claim write's OWN, second and distinct escalation, emitted ON TOP OF (never
        instead of) that generic line, once `_CLAIM_LABEL_RETRIES` attempts have genuinely been
        exhausted: a fixed, greppable marker (`_CLAIM_LABEL_FAILURE_MARKER`) a human scanning a real
        run -- or a monitoring script matching on the marker, no prose-parsing required -- cannot
        scroll past unnoticed. Matches this codebase's own established shape for this kind of
        message (one clear stderr line naming the real problem and what's degraded; see
        `sync.py`'s `publish_after_write` and `doctor.py`'s own diagnostics).

        LOUD, NOT BLOCKING -- #2392's own explicit scope decision. `mark_in_progress`'s caller
        (`_claim`, loop.py) never gates the pick on this method having run, or on `mark_in_progress`'s
        return value: the pick still proceeds. Genuinely blocking a pick on a failed durable-signal
        write is the bigger, separate availability-vs-exclusion trade-off the issue names as needing
        its own explicit human sign-off; this only makes the degradation impossible to miss, it does
        not change what happens next.

        Never raises -- an escalation that itself crashes the pick would be a strictly worse outcome
        than the silent line it replaces."""
        try:
            waited = sum(self._CLAIM_LABEL_RETRY_BASE * (2 ** i)
                        for i in range(max(self._CLAIM_LABEL_RETRIES - 1, 0)))
            sys.stderr.write(
                "sigma: %s #%s -- the durable, cross-session claim signal (%s) did NOT land "
                "after %d attempts over ~%ds of backoff; another session's picker may not see this "
                "claim, so a double-pick of #%s is possible until a human or `/sigma-doctor` confirms "
                "the label by hand. This goal's own `claimed` ledger entry is flagged (see its `why`) "
                "so the failure is not lost once this line scrolls away.\n"
                % (self._CLAIM_LABEL_FAILURE_MARKER, goal, self.in_progress_label,
                   self._CLAIM_LABEL_RETRIES, waited, goal))
        except Exception:
            pass

    def release(self, goal, reason, note=None):
        """#841: undo a claim that was never started — a small, sanctioned counterpart to
        mark_in_progress, replacing the manual `gh issue edit --remove-label` + hand-written
        comment this repo's own operator had to do on #813/#815/#817/#821 before this verb
        existed. `next`/`next-batch` claim a goal via mark_in_progress as part of picking it; if
        the caller then never calls `complete`/`park`/`fail` on that SAME goal (next-batch picked
        more than got dispatched this run, or a later pick was `--skip`ped after an earlier one
        already claimed it), nothing ever undoes that claim. This does: remove the in-progress
        label, leave an audit-trail comment, and put the board card back in the Ready lane —
        WITHOUT touching the goal label, the parked label, or closing the issue. The goal is not
        done and not blocked on a human decision; it is, once again, an ordinary pending goal.

        Mirrors mark_in_progress's own label call and _offboard's own comment call exactly: each
        `gh` call individually try/excepted, so a transient error on any one of them can never
        raise into the caller or leave the goal half-released.

        POST-REVIEW FIX (PR #1107, Finding 3): refuses — a clean, SILENT no-op, no `gh` mutation
        at all — when the issue is already done (CLOSED) or parked/failed (carries
        `parked_label`; #506's `_offboard` gives both the same label, distinguished only by
        comment text, so one label check covers both). Pre-fix this fired unconditionally:
        reproduced live, calling release on a never-claimed/already-terminal issue still removed
        the in-progress label and posted the "claimed but not started" comment regardless. The
        probe (`issue view --json state,labels`) mirrors `complete()`'s own already-CLOSED probe
        (#505) — same fail-OPEN posture on a transient `gh` error: a probe failure falls through
        to the pre-fix release behavior rather than blocking a legitimate one, since no per-field
        bound can tell a real transient error from a genuine terminal state here. Returns True
        when it actually released the claim, False for the no-op.

        #1121 (follow-up from #841's own review), DELIBERATELY NOT DISTINGUISHED, DOCUMENTED
        INSTEAD: a nonexistent/deleted issue and a genuine release both return True here. The probe
        above raises on a bad issue number exactly the same way it raises on a transient network
        error (both are a bare `Exception` from `_run`/`json.loads`, with no structured
        exit-code/stderr distinction surfaced by `_run` to tell them apart), so it falls into the
        SAME `except Exception: pass` the transient-error case above already relies on — `terminal`
        stays `False`, and every call below (`--remove-label`, the audit comment, the board-status
        move) fires anyway, each already its own best-effort `try/except`. On a genuinely deleted
        issue this yields three individually-harmless no-op `gh` failures (an already-nonexistent
        issue cannot un-relabel, comment, or move) followed by an honest `return True`; on a
        genuine release it is the intended path. Left this way on purpose, not merely unnoticed:
        every other GitHub-side operation in this class (`complete()`'s own already-CLOSED probe,
        #505; the terminal-state probe just above) is fail-open/best-effort by the same design, and
        a deleted issue is already unpickable (`source.fetch_pending`/`mark_in_progress` both
        require a resolvable issue number before a claim can exist in the first place), so the
        window this could matter in — the issue existed long enough to be claimed, then vanished
        before `release` ran — is narrow, and the WORST case is one duplicate no-op `gh` attempt on
        an id that no longer resolves to anything, not a silently wrong ledger/board state.
        Genuinely telling the two apart would mean `_run` surfacing exit code/stderr to every
        caller (a wider, cross-cutting change touching every method in this class, not a local
        one) for a distinction whose only consumer today would be a caller that wants to log
        "released" vs "was already gone" — no such caller exists. Left as a smaller, separately-
        scoped follow-up if a real caller ever needs that distinction, not forced here.

        Seen live in the predecessor 2026-09-28 (its observation, not one taken on Sigma): the
        label-removal and comment `gh` calls below used to be a bare `try/except: pass` each -- a
        transient failure on EITHER was truly invisible, and `release()` still returned `True`.
        Releasing a stale claim during a migration there hit exactly that: reported success, but the
        in-progress label was still attached and no audit comment existed, only caught by checking
        GitHub directly afterward and fixing it by hand. The fail-open POSTURE is unchanged (a
        transient `gh` error here must still not raise into the caller or block the release), but a
        failure is no longer silent: it writes one stderr line (mirrors
        `_swap_labels_best_effort`'s own loud-but-non-raising shape) and is recorded in
        `release_warnings()` so a caller that wants to know can."""
        if hsg.blocks_issue_write(self, goal, "release"):    # slice 10: a foreign issue is parked, not written
            return
        terminal = False
        try:
            info = self._read_issue(goal, ["state", "labels"])
            state_now = (info.get("state") or "").upper()
            label_names = {(l.get("name") or "") for l in (info.get("labels") or [])}
            terminal = state_now == "CLOSED" or self.parked_label in label_names
        except Exception:
            pass   # best-effort probe; a transient gh error must fall through to the pre-existing
                   # (fire-anyway) release behavior, never block a legitimate one on a read that
                   # could not actually determine the issue's state -- mirrors complete()'s own
                   # state-probe fail-open posture (#505) exactly.
        self._last_release_warnings = []
        if terminal:
            return False   # already done or parked/failed -- release is only meaningful for a
                            # goal that is still claimed/in-progress; leave the issue untouched
        self._ensure_labels()
        try:
            self._issue_remove_label(goal, self.in_progress_label)       # REST first (#895 slice 3a)
        except Exception as exc:
            # best-effort visibility label; a transient gh error must not block the release -- but
            # it must not vanish either (2026-09-28, see docstring).
            detail = _release_warning_reason(exc)
            self._last_release_warnings.append("label removal failed: %s" % detail)
            try:
                sys.stderr.write("sigma: release label removal failed for #%s (%s) — continuing; "
                                 "the in-progress label may still be attached\n" % (goal, detail))
            except Exception:
                pass
        # #2009: `note` OVERRIDES the default framing, and defaults to it byte-for-byte. The
        # hard-coded line says "claimed but not started", which is FALSE for a stale-resume release
        # -- that goal has a worktree, a branch and a work record; it was unambiguously started, and
        # a self-contradictory sentence on the issue timeline is not an audit trail.
        body = note or "Released by Sigma — claimed but not started"
        if reason:
            body += ": " + reason
        try:
            self._issue_comment(goal, body)                              # REST first (#895 slice 3a)
        except Exception as exc:
            # best-effort audit trail; a transient gh error must not block the release -- but it
            # must not vanish either (2026-09-28, see docstring).
            detail = _release_warning_reason(exc)
            self._last_release_warnings.append("audit comment failed: %s" % detail)
            try:
                sys.stderr.write("sigma: release audit comment failed for #%s (%s) — continuing; "
                                 "no audit-trail comment was posted\n" % (goal, detail))
            except Exception:
                pass
        self._set_board_status(goal, self.col["ready"])   # back where next_pending's board queue offers it
        return True

    def release_warnings(self):
        """gh failures from the MOST RECENT release() call that were swallowed as best-effort (label
        removal, audit comment) -- empty list means either a clean release or a release() call
        never happened. Mirrors read_degraded()'s pattern (it returns a copy, where that returns a
        bool): reflects only the last call, not a sticky latch, and exists so a caller that wants
        to know a release only PARTIALLY landed (the claim label may still be attached, or the
        audit trail may be missing) can, without release() itself having to raise or change its
        True/False/no-op return contract that `_release()` (loop.py), the CLI `release` verb, and
        every existing test double already key on (see release()'s own docstring, 2026-09-28)."""
        return list(self._last_release_warnings)

    def mark_qc(self, goal):
        self._set_board_status(goal, self.col["qc"])     # board-only: the Review / QC quality stage

    def await_merge(self, goal, note=None):
        """#232: the goal's PR is open and awaiting a merge the loop does not perform now -- the
        goal is NOT done (done means merged), so the issue stays OPEN. Existing vocabulary only (no
        new label, no new column): NOTHING is removed -- `sdlc:goal` membership and the claim's own
        `sdlc:in-progress` overlay stay exactly as the claim left them (`_fetch_pending` excludes an
        issue carrying the overlay, so another machine never re-picks it) -- and the board card
        moves to QC, the existing review stage. The overlay is deliberately NOT re-added here:
        `mark_in_progress` stays its single writer (tests/test_docs.py pins that); on THIS machine
        `_next` also skips every goal whose work record is awaiting merge, whatever its labels say.
        `note` (first `record review` only) goes on the issue timeline so a human reading the issue
        sees why it is still open. Every write is best-effort: the local work-record flag is what
        the merge-reconcile pass reads, so a transient gh error here costs visibility, never the
        eventual close."""
        self._set_board_status(goal, self.col["qc"])
        if note:
            try:
                self.note(goal, note)
            except Exception:                   # noqa: BLE001 - audit trail is best-effort here
                pass

    def complete(self, goal):
        if hsg.blocks_issue_write(self, goal, "complete"):    # slice 10: a foreign issue is parked, not written
            return
        # #505: when this issue is ALREADY CLOSED by the time this call runs, `gh issue close
        # --comment` on an ALREADY-closed
        # issue exits 0 but silently drops the --comment text (verified live against the real gh
        # CLI: stdout empty, stderr "! Issue ... is already closed", comment never posted) -- and
        # `_run_gh` only returns stdout, discarding stderr on success, so that signal is invisible
        # through the normal `_run()` return value. Probe the state first (mirrors the
        # read-then-write idiom `append_to_body()` already uses below) so the comment always goes
        # out through whichever call will actually post it. Best-effort: any probe failure falls
        # through to today's combined call, unchanged -- a new read must not make this any more
        # fragile than it was before this fix.
        already_closed = False
        try:
            state_now = self._read_issue(goal, ["state"])["state"]
            already_closed = state_now == "CLOSED"
        except Exception:
            pass
        if already_closed:
            # TWO THINGS PRODUCE THIS STATE, and until #1649 there was only one. (a) GitHub's own
            # auto-close, from a `Closes #N` in a PR merged into the DEFAULT branch -- the norm for
            # this repo's own PRs. (b) Sigma itself: `work._close_issue_the_base_cannot` PATCHes
            # the issue closed the moment a merge lands and its OWN post-merge read of the issue
            # finds it still open (#2615: decided by the issue's live state, never by which base
            # the PR happened to land on). Under the branching model every goal's PR targets
            # `feature/<unit>`, so (b) is the ordinary producer and (a) never fires at all.
            #
            # Either way the close already happened -- only the audit-trail comment is still
            # missing, so post it standalone. Best-effort, unlike the branch below: failing
            # to leave a comment on an issue that is already genuinely done must never make
            # complete() raise -- run_loop downgrades ANY exception here into a park(), which would
            # misleadingly park/block an already-closed issue over a mere transient gh error, worse
            # than the silent-comment bug this fix closes.
            #
            # #1657 follow-up (scope): routed through `note()` -- the SAME GraphQL-retry-then-REST-
            # fallback chokepoint the audit-trail comment for a REJECT verdict already gets -- rather
            # than the bare single-shot `gh issue comment` this used before. A genuinely exhausted
            # GraphQL quota used to lose this comment outright even though a completely separate,
            # untouched REST quota was available the whole time (#1514); now it survives that the
            # same way `note()`'s own callers already do. Still wrapped in try/except: `note()`
            # RAISES once its own retries and REST fallback are both spent, and this call site's
            # contract (never make complete() raise on a merely-missing comment) is unchanged.
            try:
                self.note(goal, "Completed by the Sigma SDLC loop.")
            except Exception:
                pass
        else:
            # #895 slice 3a: REST, two mutations as `gh issue close --comment` was: the comment first
            # (non-idempotent policy), then the close (idempotent policy). Both RAISE; a comment that
            # landed with a close that failed leaves the issue OPEN and run_loop parks it, as before.
            self._issue_comment(goal, "Completed by the Sigma SDLC loop.")
            self._issue_close(goal, reason="completed")
        # #1391 step 2: routed through the swap primitive for its retries. Measured: this single
        # swallowed write durably failed on 19-27 of 333 post-fix completions (5.7-8.1%), and
        # COMPLETE is the transition where that hurts most -- it is terminal, so it never runs again
        # and nothing repairs it. (The larger cause of closed-issue residue is `complete()` never
        # running AT ALL -- 90% of sampled cases, closed by a PR's "Fixes #N" or by hand -- which no
        # write-path change can reach; that belongs to the reconciler.)
        # #1445: MEMBERSHIP goes too, not just the overlay. Until this, `complete()` removed
        # `in_progress` and left `goal_label` behind, so every issue the loop finished stayed
        # labelled as the loop's work forever -- 38 of them on `os` in ~25h across three operators,
        # and the same drift that forced stripping ~310 closed issues by hand on 2026-08-19/20.
        #
        # Safe because the close has ALREADY happened above: the `else` branch raises if
        # `issue close` fails, so this line is unreachable with the issue still open. There is no
        # ordering in which membership is dropped from something still pickable.
        #
        # Consistent with the documented terminal state ("a Done issue carries no `sdlc:*` label")
        # and with the membership rule, which reserves membership for work waiting to be PICKED.
        # A closed issue is waiting for nothing.
        self._swap_labels_best_effort(
            goal, remove=[self.in_progress_label, self.goal_label], what="complete")
        self._set_board_status(goal, self.col["done"])
        self._archive_card(goal)

    def _archive_card(self, goal):
        """Archive a finished card so the board stays close to one page. OPT-IN, `is True` only.

        Hygiene, not performance: `item-list --query` filters server-side, so the queue read already
        scales with OPEN items rather than board size (#693). What archiving fixes is a board nobody
        can read — 177 of this repo's 268 cards are Done — plus every unfiltered read and the UI.

        Strictly opt-in via `project.archive_done`, and strictly `is True`: archiving is user-visible
        and not conveniently reversible in bulk, so it must never be inferred from some neighbouring
        setting being on, and a stray `1` or `"yes"` must not switch it on by accident. An absent key
        (every existing adopter) archives nothing.

        Runs AFTER the Done status is written — archiving first would leave the Done write landing on
        an already-archived item. Fail-open like every other board write: failing to tidy a card can
        never be allowed to fail a goal that genuinely completed."""
        if not self.project_enabled or self._project_cfg.get("archive_done") is not True:
            return
        try:
            if not self._ensure_board():
                return
            item_id = self._item_id(int(goal))
            if not item_id:
                return
            self._run(["api", "graphql", "-f",
                       'query=mutation { archiveProjectV2Item(input: {projectId: "%s", itemId: "%s"}) '
                       '{ item { id } } }' % (self._project_id, item_id)])
        except Exception as exc:
            self._note_scope(exc)

    def mark_blocked(self, goal):
        """#1350: transition the CURRENT goal's own label the moment a genuine blocking dependency
        is filed against it (`handoff.create_tracked_issue`'s `blocks_goal=True` path calls this
        directly, gated on the machine-readable "Blocked by" body marker actually having landed) —
        not deferred to a later pre-work cross-check the way a generic park is. Same removal shape
        as `_offboard` (goal label, then in-progress label, both best-effort), but adds
        `goal_blocked_label` instead of `parked_label`: `sdlc:blocked` is a DISTINCT state from
        generic `sdlc:parked`, not merely a differently-worded park.

        SKILL.md's own existing flow still has the agent call `record ... parked` right after
        filing the blocker — `_offboard` (below) is what stops that from re-adding `sdlc:parked` on
        top of the `sdlc:blocked` this method just set, which would otherwise leave the issue
        carrying two primary-state labels at once on this epic's own core mechanism.

        #1358 review: also moves the board card, matching every OTHER label-transition method in
        this class (`mark_in_progress`, `complete`, `release`, `_offboard`) — the happy path masks
        this (SKILL.md's flow calls `record ... parked` moments later, whose `_offboard` moves the
        card too), but an interrupted session between the two calls (a real, documented failure
        mode on this machine) would otherwise leave the board showing the wrong column while the
        label already reads `sdlc:blocked`."""
        self._ensure_labels()
        # #1391 step 2: ONE swap instead of three independently-swallowed edits. See `_swap_labels`.
        # All-or-nothing is the SAFER failure here, not the riskier one: a swap that fails entirely
        # leaves the goal fully labelled and therefore re-pickable (it redoes work -- recoverable),
        # whereas the old shape could land the two removals and drop the add, leaving ZERO lifecycle
        # labels -- invisible to every query in the system, permanently.
        #
        # #1393: `goal_label` is KEPT. `sdlc:blocked` is a temporary OVERLAY, not an exit from
        # Sigma's world -- the same relationship `sdlc:in-progress` has to `sdlc:goal`, and for
        # the same load-bearing reason: every sweep, census, mirror and reclaim path in this kit
        # queries `--label goal_label`, so an issue that drops it is findable ONLY by whichever
        # query happens to ask for the overlay. Exactly one sweep asks for `sdlc:blocked`
        # (`auto_unpark._fetch_parked_issues`), and if that label is ever lost -- a hand edit, a
        # partial write from an older plugin -- the issue becomes an orphan with no route back.
        #
        # Eligibility is UNCHANGED and does not depend on removing the label: `_fetch_pending`
        # excludes `goal_blocked_label` on every call (including next-batch's skip-augmented
        # refills) and `_card_is_eligible` mirrors that on the board path. So a blocked goal is
        # visible to everything and pickable by nothing -- which is what "blocked" should mean.
        #
        # The one exit that DOES drop membership is a park (`_offboard`): a park is a permanent,
        # human-owned block whose whole purpose is to sit in a human's `--label sdlc:parked` review
        # queue, and putting it back into the population every sweep re-examines is the opposite of
        # what it is for.
        self._swap_labels_best_effort(
            goal, add=[self.goal_blocked_label],
            remove=[self.in_progress_label], what="block")
        self._set_board_status(goal, self.col["blocked"])

    def park(self, goal, reason, tier=None):
        if hsg.blocks_issue_write(self, goal, "park"):    # slice 10: a foreign issue is parked, not written
            return
        comment = PARK_COMMENT_PREFIX + _sanitize_offboard_reason(reason)
        # #953/#1185: no new `gh` call -- richer text in the one that already fires. `tier` is
        # optional and additive: omitted (the default, and every park loop.py's _record() doesn't
        # offer to decision_tier or that resolve() leaves inert -- see state.py's park() for the
        # exact set) leaves this comment byte-for-byte what it always was.
        if tier:
            comment += f" [decision tier: {tier}]"
        self._offboard(goal, comment)

    def fail(self, goal, reason):
        # Same board mechanics as park (the label is a visibility tag) — the issue
        # timeline carries the distinction: this needs a FIX, not a decision.
        self._offboard(goal, FAIL_COMMENT_PREFIX + _sanitize_offboard_reason(reason))

    def _offboard(self, goal, comment):
        self._ensure_labels()
        # #1350: read the CURRENT label set FIRST, before mutating anything — SKILL.md's own
        # existing flow calls `record ... parked` (which lands here via park()/fail()) right after
        # filing a blocker, and `mark_blocked` (above) has already transitioned this exact goal to
        # `sdlc:blocked` by that point. Adding `sdlc:parked` on top unconditionally, as this
        # function always used to, would leave the issue carrying TWO primary-state labels at
        # once — the collision found by an adversarial review of this epic's own PLAN before this
        # was even written. Fails open toward the PRE-EXISTING behavior (adds parked_label anyway)
        # on any read error — a false "not blocked" costs nothing new; the alternative (silently
        # skipping the one park signal a genuinely-parked issue needs) would be worse.
        #
        # #1393 DELETES #1350's `already_blocked` read entirely, rather than repurposing it.
        #
        # That read existed for exactly one decision: whether to add `parked_label` on top of a goal
        # that had already transitioned to `sdlc:blocked`, which under the old model would have left
        # two primary states. Under the membership model a park ALWAYS adds the park marker and
        # ALWAYS gives up membership, so there is no such decision left to make.
        #
        # Deriving the REMOVAL set from a read instead would have been strictly worse than deriving
        # nothing: `_swap_labels` is idempotent (removing a label the issue does not carry is a
        # server-side no-op, verified live), so naming all three costs nothing -- while a read that
        # succeeds but is STALE (GitHub's index is only eventually consistent, which this file
        # already retries around in `_fetch_pending`) would silently omit a removal and leave a
        # second primary state behind. One less `gh` call on the park path, and one less way to be
        # wrong.
        # F4: de-list FIRST and unconditionally-attempted — whatever else below fails, next_pending's
        # `--label <goal_label>` query must never re-serve this goal. This used to run LAST, so a
        # raising `issue comment` (a transient 502/rate-limit) left the goal_label untouched AND
        # crashed run_loop's while-loop before either later step ran — the exact opposite of
        # park-and-continue. Re-queue by re-adding the label.
        # #1391 step 2: ONE swap instead of three independently-swallowed edits. This is the
        # transition that produced the worst of the measured corruption -- a 2^3 lattice with 7 bad
        # end states, 5 of them label sets the state machine cannot name, including `{}` zero-label
        # limbo. The F4 "de-list FIRST" reasoning below is PRESERVED and strengthened: the removal
        # and the add now land together or not at all, so the goal can never end up de-listed
        # without its park marker. A total failure leaves it re-pickable (redoes work) rather than
        # invisible (permanently stuck) -- the right direction to fail in.
        # #1393: ONE swap naming all four labels -- `+parked`, and `-goal`/`-in-progress`/`-blocked`
        # for whichever of them is actually present. A park is the one transition that gives up
        # MEMBERSHIP, so `goal_label` goes here and only here.
        landed = self._swap_labels_best_effort(
            goal, add=[self.parked_label],
            remove=[self.goal_label, self.in_progress_label, self.goal_blocked_label],
            what="park")
        if not landed:
            self._repair_partial_park(goal)
        # #1657 follow-up (scope): routed through `note()` -- same GraphQL-retry-then-REST-fallback
        # chokepoint `note()`'s other callers already get -- rather than the bare single-shot `gh
        # issue comment` this used before. `park()`/`fail()` both land here, and this comment is
        # the ONLY record of WHY a goal was parked (a park is a human's deliberate checkpoint that
        # only /sigma-unpark reopens -- there is no other durable trail of the reason), so it must
        # not be lost to a fully-exhausted GraphQL quota any more than a REJECT verdict's comment
        # is. Still wrapped in try/except: `note()` RAISES once its own retries and REST fallback
        # are both spent, and this call site's contract (a transient gh error must not abort the
        # drain) is unchanged.
        try:
            self.note(goal, comment)
        except Exception:
            pass   # best-effort audit trail; a transient gh error must not abort the drain
        # A park and a machine block are DIFFERENT states -- a park is a human's deliberate
        # checkpoint that only /sigma-unpark reopens, a block clears itself the moment its blocker
        # closes. Both used to land in one `Blocked` column, which is the single place a person
        # actually LOOKS, so on a board carrying both options the card contradicted the labels.
        # Fail-open by construction: `_set_board_status` returns False without writing when the
        # board has no such option (and spends no `gh` call to find out), so a board with no
        # `Parked` option falls through to exactly the historical column.
        if not self._set_board_status(goal, self.col["parked"], warn=False):
            self._set_board_status(goal, self.col["blocked"])

    def _repair_partial_park(self, goal):
        """#722: finish a park whose swap failed AFTER its add landed.

        NOT atomic, and not claimed to be: `_swap_labels`' own block comment documents (proved live)
        that GraphQL root mutations run serially and do not roll back, so `+parked` can commit while
        `-goal` fails -- leaving goal+parked, the contradiction `triage._bucket_hygiene` detects.
        This is the RECOVERY half: ONE read of the issue's labels over REST (a separate rate-limit
        budget from the GraphQL one that is the usual cause of the failure), and only if
        `parked_label` is PRESENT and a lifecycle label is still there, ONE remove-only swap (its own
        bounded retries). Parked ABSENT means the add never landed, so the goal is still fully
        labelled and re-pickable -- removing `goal_label` then would create #1391's invisible
        zero-marker limbo, so nothing is touched. Remove-only is never add-first, so it cannot
        resurrect the state it repairs. Any failure here is swallowed (never aborts the park path;
        hygiene still flags what remains) and nothing loops.
        Order note (#506 vs #1391): #506 wanted goal removal first; #1391 deliberately inverted it
        (add first, so a partial failure is visible rather than invisible). This repair is what makes
        that visible state self-healing.
        Cost: the remove-only swap is GraphQL, the very budget whose exhaustion usually caused the
        failure, so it retries with backoff (about 3.5s worst case) and may itself fail (swallowed)."""
        try:
            names = {l.get("name") if isinstance(l, dict) else l
                     for l in (self.fetch_body_labels_rest(goal).get("labels") or [])}
            stale = [l for l in (self.goal_label, self.in_progress_label, self.goal_blocked_label)
                     if l in names]
            if self.parked_label in names and stale:
                self._swap_labels(goal, remove=stale)
        except Exception as exc:                 # noqa: BLE001 - must never propagate from park
            try:
                sys.stderr.write("sigma: park repair failed for #%s (%s) — goal+parked may remain "
                                 "until triage hygiene / a reconcile sweep corrects it\n" % (goal, exc))
            except Exception:
                pass

    def note(self, goal, text):
        """Record on the issue timeline (the audit trail): a journey-log / critical-insight comment.

        #1986: RETRIES on a transient failure, exactly like `_swap_labels` above and for the
        identical reason -- `sigma-goal-review`'s REJECT verdict writes NOTHING else (§7g: no label,
        no board move, no edit to the issue body), so this one comment is the ENTIRE record of a
        rejection, and a transient `gh` hiccup at exactly this moment (an active-account drift on a
        host juggling more than one `gh` login is one well-documented cause) must not be free to
        erase it. RAISES on final failure, deliberately unlike the old shape here: `loop.py`'s
        `note` CLI verb is what decides whether a failed note is fatal to the caller, and it cannot
        decide what it is never told -- this method used to be reached through a silent
        `try/except: pass` one layer up (loop.py's `note` verb, before #1986).

        A retry that turns out to have followed an actually-landed write double-posts the comment.
        Accepted, deliberately: for an audit trail that is otherwise all-or-nothing, a duplicate
        REJECT comment is a categorically safer failure than a silently missing one.

        #1657: `gh issue comment` resolves through GraphQL, whose 5,000-points/hour budget is
        shared with every other `gh`-based operation on this board (project/board writes, `gh
        issue view --json`, ...) and was confirmed exhausted for real during #1514 (a session
        heavy on `gh` calls). Retrying the SAME call against an exhausted hourly quota just
        re-fails `_NOTE_RETRIES` times in a few seconds and then raises -- the comment, which for
        a REJECT verdict is §7g's ENTIRE record, is lost. Once every GraphQL attempt above has
        come back transient, fall back ONCE to `gh api repos/{owner}/{repo}/issues/{n}/comments`
        (REST v3), which draws on a completely separate 5,000/hour budget -- the same reasoning
        a downstream goal-cost recorder already documents for its own comment mirror, and
        confirmed live in #1514: REST kept working the entire time GraphQL read exhausted. A
        non-transient GraphQL failure (bad auth, issue not found, ...) still raises immediately,
        exactly as before -- REST would fail the same way and for the same reason, so trying it
        would only spend a second quota to learn nothing new.

        POST-MERGE REVIEW FIX: REPO RESOLUTION deliberately does NOT use `self._owner_name()`,
        for the identical reason `_fetch_issues_rest` (above) already documents for itself --
        that helper's unset-`repo` fallback shells out to `gh repo view --json owner,name`, which
        is ITSELF graphql-billed. `discovery.github.repo` ships EMPTY in the sigma-init template
        and `/sigma-setup` explicitly supports leaving it unset, so calling `_owner_name()` here
        would resolve straight back onto the exhausted GraphQL quota this fallback exists to route
        around -- reproduced by a fake-`gh` test with `repo` unset: the ORIGINAL fix's
        `_owner_name()` call shelled out to `gh repo view`, hit the same simulated rate limit, and
        the comment was lost exactly as before this fallback existed (see
        `test_github_note_with_no_resolvable_repo_still_falls_back_to_rest` below). Mirroring
        `_fetch_issues_rest` exactly: when `self.repo` is set, address it directly
        (`repos/<repo>/issues/<n>/comments`); when unset, use the literal `{owner}/{repo}`
        placeholder, which `gh api` resolves from the working directory's git remote at ZERO extra
        requests (confirmed live by `_fetch_issues_rest`'s own docstring) -- no network call, no
        quota touched, so an unset `repo` can never again defeat this fallback. A REST call that
        itself fails (genuinely no git remote to infer from, bad auth, ...) raises that failure
        directly -- there is nothing further to fall back to."""
        if hsg.blocks_issue_write(self, goal, "note"):    # slice 10: a foreign issue is parked, not written
            return
        for attempt in range(self._NOTE_RETRIES):
            try:
                self._run(["issue", "comment", goal, *self._repo_args(), "--body", text])
                return
            except Exception as exc:
                if not self._is_transient(exc):
                    raise
                if attempt < self._NOTE_RETRIES - 1:
                    time.sleep(self._NOTE_RETRY_BASE * (2 ** attempt))
        endpoint = ("repos/%s/issues/%s/comments" % (self.repo, goal) if self.repo
                    else "repos/{owner}/{repo}/issues/%s/comments" % goal)
        self._run(["api", endpoint, "-X", "POST", "-f", "body=" + text])

    def fetch_title_body(self, goal):
        """Direct, read-only issue title+body through THIS source's own `_run` chokepoint (never a
        bare module-level shell-out), so any caller reading through this method (decompose_check,
        #519; `review_context._fetch_issue`, every pick-time journal read in loop.py) is fully
        exercised by the same recording-fake tests as every mutating call in this file — a
        module-level shell-out would bypass the fake and make a zero-mutation test over it vacuous.

        #1808: REST (`gh api repos/{owner}/{repo}/issues/<n>`), never `gh issue view --json
        title,body` — `issue view` is GraphQL under the hood and shares GitHub's SEPARATE, far more
        easily exhausted hourly `graphql` budget with every other `gh issue view`/`issue list`/board
        write this loop makes. Confirmed live: a `pr-review` brief for goal #1756/PR #1806 hit
        `RuntimeError: gh issue view 1756 ... failed: GraphQL: API rate limit already exceeded` on a
        completely ordinary, non-degraded goal, leaving the reviewer without the issue's acceptance
        criteria for no reason but a shared quota this call did not need to touch.
        `work.py._declared_unit` already makes the identical read (an issue's title/body/labels) via
        REST for exactly this reason — same endpoint shape here, so both call sites finally draw on
        the same, separate REST budget instead of one of them silently keeping the GraphQL exposure.

        REPO RESOLUTION mirrors `_fetch_issues_rest`/`note`'s own fallback, not `self._repo_args()`
        (that flag is for `gh issue ...` subcommands; `gh api` needs the owner/repo IN the endpoint
        path): `self.repo` set -> address it directly; unset -> the literal `{owner}/{repo}`
        placeholder, which `gh api` resolves from the working directory's git remote at ZERO extra
        requests — never `_owner_name()`'s own GraphQL `gh repo view`, which would silently reopen
        the exact quota this fix exists to route around.

        Returns `{"title": str, "body": str}`; a malformed/empty/non-object response degrades to
        `""` for either half rather than raising — unchanged from before this fix, and the reason
        every existing caller (all fail-open by their own design) needed no changes here."""
        endpoint = ("repos/%s/issues/%s" % (self.repo, goal) if self.repo
                    else "repos/{owner}/{repo}/issues/%s" % goal)
        raw = self._run(["api", endpoint])
        try:
            data = json.loads(raw or "{}")
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        return {"title": data.get("title") or "", "body": data.get("body") or ""}

    def fetch_issue_for_approval(self, goal):
        """#722 (`spend_approval.check`): body + author + `updated_at` from ONE REST read, so the
        author check and the marker parse look at the same snapshot (no gap between two reads) and
        no GraphQL quota is spent (see `fetch_title_body`, #1808). RAISES on a transport failure and
        on a non-object payload -- the caller DENIES on any exception, because "could not read
        who wrote this" must never degrade into a grant. `author` is `""` when GitHub named none."""
        endpoint = ("repos/%s/issues/%s" % (self.repo, goal) if self.repo
                    else "repos/{owner}/{repo}/issues/%s" % goal)
        data = json.loads(self._run(["api", endpoint]))
        if not isinstance(data, dict):
            raise ValueError("fetch_issue_for_approval: expected a JSON object")
        user = data.get("user")
        return {"body": data.get("body") or "",
                "author": (user.get("login") or "") if isinstance(user, dict) else "",
                "author_association": data.get("author_association") or "",
                "updated_at": data.get("updated_at") or ""}

    def fetch_comment_bodies(self, goal):
        """#722: EVERY comment body on the issue, over REST with `--paginate` (so a used-marker
        past the first page cannot be missed -- a miss would fail OPEN into reusing a grant).
        RAISES on a transport failure or an unparseable payload; the caller DENIES on any exception.
        `gh api --paginate` prints one JSON array per page back to back, so the output is decoded
        as a stream of documents rather than assumed to be one."""
        endpoint = ("repos/%s/issues/%s/comments" % (self.repo, goal) if self.repo
                    else "repos/{owner}/{repo}/issues/%s/comments" % goal)
        raw = self._run(["api", "--paginate", endpoint]) or ""
        dec, i, out = json.JSONDecoder(), 0, []
        while i < len(raw):
            if raw[i].isspace():
                i += 1
                continue
            page, i = dec.raw_decode(raw, i)
            if not isinstance(page, list):
                raise ValueError("fetch_comment_bodies: expected a JSON array per page")
            out.extend((c.get("body") or "") for c in page if isinstance(c, dict))
        return out

    def fetch_author(self, goal):
        """#1479: the login that OPENED this issue -> a string, `""` when gh did not say.

        THE ONE QUESTION OWNERSHIP ENFORCEMENT ASKS OF AN ISSUE, and it is deliberately about the
        AUTHOR rather than the assignee or the picker: the rule is about who may CREATE work
        carrying a unit's label, and an assignee is somebody the work was handed TO. #895: REST
        `issues/N` first via `_read_issue` (fallback: one `gh issue view --json author`), both
        through this source's own `_run` chokepoint, so `feature_owner.gate_at_pick` is exercised by
        the same recording fakes as every mutating call in this file. A Bot author reads `app/<slug>`
        on both paths (measured once on a public bot-filed GitHub Skills exercise issue, 2026-10-09:
        REST "github-actions[bot]", gh "app/github-actions").

        RAISES GhApiError on a failed read and on a non-dict REST payload (both take the caller's
        proceed-with-note path), and DEGRADES to `""` when the fallback's payload is malformed or
        carries no author — `fetch_body_labels`' split, for its reason: "could not read this
        issue" and "gh answered, with no author in it" are different facts, and the caller's own
        fail-open policy lives in one place. `feature_owner` treats both as "the author cannot be
        named", which PROCEEDS; keeping them distinguishable here is what lets it say which."""
        data = self._read_issue(goal, ["author"])
        author = data.get("author")
        return (author.get("login") or "") if isinstance(author, dict) else ""

    # ----- #1468: the unit-label surface `feature_labels.attach_at_pick` duck-types on -----------
    # Three narrow methods rather than one, because they answer three questions with three different
    # failure directions (see feature_labels.py's own failure table), and because keeping the `gh`
    # knowledge HERE is what lets that module stay source-agnostic -- `LocalSource` simply does not
    # define them, and the gate degrades to a no-op instead of needing to know which class it holds.

    def fetch_body_labels(self, goal):
        """`{"body": str, "labels": [...]}` -- exactly the payload `features.read` takes.

        #895: REST first via `_read_issue`, one `gh issue view` fallback. RAISES GhApiError on a
        failed read and on a non-dict REST payload, deliberately: the caller's own
        fail-open policy lives in one place, and a method that degraded a failed read to an empty
        body would make "could not read this issue" indistinguishable from "this issue declares
        nothing". Malformed JSON on the fallback path degrades rather than raising -- `features.read` is total over
        payload shape, so an unusable payload is honestly an absent declaration. Labels are handed
        back RAW (`[{"name": ...}]`); `features._label_name` reads both shapes this repo produces."""
        data = self._read_issue(goal, ["body", "labels"])
        return {"body": data.get("body") or "", "labels": data.get("labels") or []}

    def fetch_body_labels_rest(self, goal):
        """REST twin of `fetch_body_labels` -- same `{"body": str, "labels": [...]}` payload, but via
        `gh api repos/.../issues/<n>` rather than `gh issue view`, for `fetch_title_body`'s own
        measured reason (#1808): `issue view` is GraphQL, sharing GitHub's separate, far more easily
        exhausted hourly budget with every other `gh issue view`/`issue list`/board write this loop
        makes. `fetch_body_labels` itself stays GraphQL, unchanged, for its one existing caller
        (`attach_at_pick`, once per pick); THIS method exists for a caller that reads the identical
        question on a much higher-frequency trigger -- once per goal-scoped verb, potentially many
        times across one goal's life -- where adding itself to the GraphQL budget is not affordable
        (`loop._ensure_unit_tracking`, #2435).

        Same repo resolution and same degrade-on-malformed-JSON posture as `fetch_title_body` -- see
        its docstring, not repeated here. Raw labels (`[{"name": ...}]`), exactly `features.read`'s
        accepted shape and exactly what `fetch_body_labels` already hands it, so the two are
        interchangeable for that function's sake and differ only in which budget they spend."""
        endpoint = ("repos/%s/issues/%s" % (self.repo, goal) if self.repo
                    else "repos/{owner}/{repo}/issues/%s" % goal)
        raw = self._run(["api", endpoint])
        try:
            data = json.loads(raw or "{}")
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        return {"body": data.get("body") or "", "labels": data.get("labels") or []}

    def label_exists(self, name):
        """Does `name` exist on this repository? `True` / `False` / `None` when it could not be told.

        The three-valued answer is the point: a transient failure is NOT evidence a label is absent,
        and collapsing the two would either flag a human for a network blip or start a goal against
        the wrong base. Routed through `_label_node_ids`, whose `repository.label(name:)` query
        returns null for a name the repo does not have -- the same signal `_swap_labels` raises on --
        so this asks the question the write path would ask, one step earlier and without writing.

        NEVER CREATES, and cannot: this is a query, and the one method that could create is refused
        at `_run`. Self-healing by construction, too -- `_label_node_ids` caches only names it FOUND,
        so a `False` answer is re-queried on the next pick and flips the moment a human creates the
        label."""
        try:
            return name in self._label_node_ids([name])
        except Exception as exc:          # noqa: BLE001 - "could not tell" is a real answer here
            try:
                sys.stderr.write("sigma: could not look up the label %r (%s)\n" % (name, exc))
            except Exception:
                pass
            return None

    def attach_label(self, goal, name):
        """Add ONE existing label to `goal`. Returns True iff the write actually landed.

        `_swap_labels_best_effort` for its retries and its loud-but-non-raising failure, the same
        treatment every other lifecycle write in this class gets. It is also a second, structural
        guarantee that this can never create anything: `_swap_labels` resolves every name to a node
        id first and RAISES `unknown label(s) on this repo` rather than minting one."""
        return self._swap_labels_best_effort(goal, add=[name], what="feature label attach")

    def mark_designed(self, goal):
        """#1826: write the `sdlc:designed` OVERLAY — `goal-review`'s own confirmation, and the
        ONLY place that writes it (`loop.py design_check`, #1825, only ever READS it). Returns True
        iff the write landed.

        A PURE ADD, deliberately unlike `mark_blocked`/`mark_needs_label` one section below: this
        never removes `goal_label`/`parked_label`/`in_progress_label` and never moves the board
        card. Two reasons, both structural:

          - the retrofit target may already carry `sdlc:parked` — `design_check` parked it before
            filing the "Design #N" meta-issue this confirmation answers — and restoring `sdlc:goal`
            is a SEPARATE, human gesture (`/sigma-unpark`, never bundled into a label write a script
            makes on its own; see `docs/label-model.md` §3 and `design_goal.py`'s own meta-issue
            body, which states this explicitly);
          - the Product-path target (a `story`-labelled Dossier, or a freshly-created Epic child)
            never carried `sdlc:goal` to begin with, so there is nothing of that shape to preserve
            or restore either way.

        No board-status call for the identical reason: `sdlc:designed` has no column of its own in
        `docs/label-model.md` §8a — it rides alongside whatever state the issue is already in,
        exactly like `sdlc:in-progress`/`sdlc:blocked`/`sdlc:blocking` are documented to."""
        self._ensure_labels()
        return self._swap_labels_best_effort(goal, add=[self.designed_label], what="designed")

    def mark_needs_label(self, goal):
        """#1468: add the `sdlc:needs-label` OVERLAY. Returns True iff the label write landed.

        MEMBERSHIP IS KEPT, exactly as `mark_blocked` keeps it and for the identical reason: every
        sweep, census, mirror and reclaim path queries `--label goal_label`, so an issue that drops
        it is findable only by whichever query asks for the overlay. The goal becomes visible to
        everything and pickable by nothing.

        THE RETURN VALUE IS LOAD-BEARING AND THE BOARD MOVE FOLLOWS IT. `mark_blocked` moves the card
        unconditionally, so a failed label write still parks the card in `Blocked` -- harmless while
        nobody read its result, misleading now. These two methods gate the card move on the label
        actually landing, in BOTH directions, so the card can never claim a transition the labels
        did not make."""
        self._ensure_labels()
        landed = self._swap_labels_best_effort(
            goal, add=[self.needs_label_label], remove=[self.in_progress_label],
            what="needs-label")
        if landed:
            self._set_board_status(goal, self.col["blocked"])
        return landed

    def mark_needs_unit(self, goal):
        """#2263: add the `sdlc:needs-unit` OVERLAY -- the sibling of `mark_needs_label` immediately
        above, for a goal declaring NO unit at all rather than one whose label is missing. Returns
        True iff the label write landed. Same membership-kept guarantee, same load-bearing return
        value gating the board move in both directions; see `mark_needs_label`'s own docstring,
        which this one does not repeat."""
        self._ensure_labels()
        landed = self._swap_labels_best_effort(
            goal, add=[self.needs_unit_label], remove=[self.in_progress_label],
            what="needs-unit")
        if landed:
            self._set_board_status(goal, self.col["blocked"])
        return landed

    def mark_needs_triage(self, goal):
        """#2363: add the `sdlc:needs-triage` OVERLAY -- tier 4 of `feature_classify`'s
        classification chain, once every earlier tier (a single existing unit, the configured
        catch-all, an identifiable-but-unregistered component) has been tried and failed. Mirrors
        `mark_needs_unit` immediately above exactly -- same membership-kept guarantee, same
        load-bearing return value gating the board move -- see that method's own docstring.

        `_ensure_labels()` IS THE CREATION PATH: `needs_triage_label` is registered in
        `_LABEL_COLORS`, so this is the same best-effort, colour-preserving, `--force`-free
        mechanism every other lifecycle label in this class is created through (#1917) -- there is
        no separate `gh label create sdlc:needs-triage` anywhere in this module, and none is
        needed."""
        self._ensure_labels()
        landed = self._swap_labels_best_effort(
            goal, add=[self.needs_triage_label], remove=[self.in_progress_label],
            what="needs-triage")
        if landed:
            self._set_board_status(goal, self.col["blocked"])
        return landed

    def mark_needs_confirmation(self, goal):
        """#1477: hand this goal back to a human as a PROPOSAL. Returns True iff the swap landed.

        #1005: the two pick gates no longer call this unless `ai_filed.triage.enabled` is false; they
        PARK with a declared kind (`gate_hold.park_for_gate`). Kept for that switched-off mode.

        THIS ONE GIVES UP MEMBERSHIP, and that is the difference between it and `mark_needs_label`
        one method up. That one is an OVERLAY -- a missing label self-heals the moment somebody
        creates it, so the goal keeps `sdlc:goal` and a sweep clears the overlay unattended. A scope
        expansion (§7.1: a goal declaring a unit from a repo that unit does not list) needs a
        DECISION only the unit owner can take, and `sdlc:needs-confirmation` is exactly the state
        this tool already has for "inert until its owner promotes it" (#233). `/sigma-promote` is the
        one gesture that undoes it, atomically, which is the gesture a human should be making here.

        `docs/label-model.md` §2 requires it to STAND ALONE, so `goal_label` goes in the removal set
        along with the two overlays that only make sense beside it. The set is IDENTICAL to
        `_offboard`'s -- neither removes `parked_label`, because a park is a human's checkpoint and
        is not ours to clear, and neither removes `needs_label_label`, which `not_eligible_labels`
        already makes unpickable so it can only be stale, never harmful. ONE swap naming all of
        them: `_swap_labels` is idempotent, so removing a label the issue does not carry is a
        server-side no-op, while two edits can leave the issue carrying two membership labels at
        once (the 2^3 lattice #1391 measured).

        THE BOARD MOVE FOLLOWS THE LABEL WRITE, in both directions, exactly as `mark_needs_label`
        and `mark_blocked` do: a card can never claim a transition the labels did not make."""
        self._ensure_labels()
        landed = self._swap_labels_best_effort(
            goal, add=[self.proposed_label],
            remove=[self.goal_label, self.in_progress_label, self.goal_blocked_label],
            what="needs-confirmation")
        if landed:
            self._set_board_status(goal, self.col["backlog"])
        return landed

    def clear_needs_label(self, goal):
        """#1468: remove the `sdlc:needs-label` overlay and put the card back in `Ready`. Returns
        True iff the label write landed.

        Deliberately NARROW -- it removes that one label and nothing else, so membership is untouched
        and this can never be mistaken for an unpark, which is a human's gesture
        (`docs/label-model.md` §3). Removing a label that is not present is a server-side no-op
        (`_swap_labels` is idempotent and safe to retry blind), so a double call costs one request
        and changes nothing."""
        landed = self._swap_labels_best_effort(
            goal, remove=[self.needs_label_label], what="needs-label clear")
        if landed:
            self._set_board_status(goal, self.col["ready"])
        return landed

    def list_needs_label(self):
        """#1468: every OPEN issue carrying `needs_label_label`, with the body and labels the resume
        sweep needs -- ONE (or a small bounded few, on a cap-hit) REST read, no per-issue read.

        THE LABEL IS THE ATTRIBUTION. An earlier design keyed the resume off a ledger line, which
        made a correctness property depend on an OPTIONAL feature: `ledger.enabled` ships FALSE in
        `/sigma-init`'s own template, so on a stock adopter the overlay landed, nothing was recorded,
        and the goal was stuck permanently behind a comment promising it would resume by itself.
        Asking GitHub for the label cannot fail that way -- the state and its attribution are the
        same object, readable by any machine, any clone, ledger or no ledger.

        #1829: this call runs unconditionally on EVERY `_next()` pick (via
        `feature_labels.resume_needs_label`), so it shares `_fetch_pending`'s exact defect --
        a plain `gh issue list --label ...` is graphql-search-billed regardless of `--search`
        (confirmed live), NOT the `REST/core`-only call an earlier note about this function
        believed (that belief predated a later correction to a DIFFERENT, sibling claim in the
        same investigation, and was never re-checked against this function specifically -- see
        the research for #1829 (REST backlog pick)). Migrated to the same `_fetch_issues_rest`
        helper `_fetch_pending` uses. No `assignee` scoping: this sweep is deliberately unscoped by
        owner, unchanged from before -- ANY held goal must resume once its label exists, regardless
        of who is running the loop that happens to notice.

        Fail-open: an unreadable/malformed response is an empty sweep this pass, never a raise into
        the pick."""
        try:
            # `_BOARD_ITEM_LIMIT`, not a local number: this query is the SOLE resumer, so anything
            # past the cap is a goal nothing will ever release -- a correctness ceiling, not a
            # performance knob. Matches `auto_unpark._fetch_parked_issues_status`, the sibling sweep.
            issues = self._fetch_issues_rest([self.needs_label_label], self._BOARD_ITEM_LIMIT)
        except Exception as exc:          # noqa: BLE001 - a sweep must never break the pick
            try:
                sys.stderr.write("sigma: could not list %s issues (%s) — skipping the "
                                 "needs-label sweep this pass\n" % (self.needs_label_label, exc))
            except Exception:
                pass
            return []
        return [i for i in issues if isinstance(i, dict)]

    def clear_needs_unit(self, goal):
        """#2263: remove the `sdlc:needs-unit` overlay and put the card back in `Ready`. Mirrors
        `clear_needs_label` exactly, including its idempotency ("removing an absent label is a
        server-side no-op") -- see that method's own docstring."""
        landed = self._swap_labels_best_effort(
            goal, remove=[self.needs_unit_label], what="needs-unit clear")
        if landed:
            self._set_board_status(goal, self.col["ready"])
        return landed

    def list_needs_unit(self):
        """#2263: every OPEN issue carrying `needs_unit_label`, with the body and labels the resume
        sweep needs. Mirrors `list_needs_label` exactly -- same REST helper, same cost shape, same
        unscoped-by-owner reasoning -- see that method's own docstring, which this one does not
        repeat."""
        try:
            issues = self._fetch_issues_rest([self.needs_unit_label], self._BOARD_ITEM_LIMIT)
        except Exception as exc:          # noqa: BLE001 - a sweep must never break the pick
            try:
                sys.stderr.write("sigma: could not list %s issues (%s) — skipping the "
                                 "needs-unit sweep this pass\n" % (self.needs_unit_label, exc))
            except Exception:
                pass
            return []
        return [i for i in issues if isinstance(i, dict)]

    def fetch_comments_strict(self, goal):
        """Direct, read-only issue comments+labels — REST first (ALL comments, paged by the issue's own
        count; #895), one `gh issue view --json comments,labels` fallback, both through THIS source's
        own `_run` chokepoint (#522, `goal_decompose`'s `file`-mode idempotency read). A malformed
        REST payload raises GhApiError; the text below describes the fallback path's payload checks.
        Deliberately, DELIBERATELY the opposite of `fetch_title_body` above: that method degrades a
        transport failure or a malformed/non-object payload to an empty result, because a caller
        only ever uses it for a classifier that fails open by design either way. THIS method RAISES
        on all three instead — a transport failure propagates from `self._run` unguarded, a
        malformed/non-object payload raises `ValueError` explicitly (an empty `raw` is malformed
        too: no `raw or "{}"` fallback here), and a well-formed JSON OBJECT that is still missing
        the `"comments"` key entirely ALSO raises (#522 review fix 2) — a real `gh issue view --json
        comments,labels` call always returns the requested field, even as an empty array, so a bare
        `{}` is itself a signal something went wrong (a truncated/malformed transport), not
        "genuinely zero comments" (which looks like `{"comments": [], ...}`); defaulting that case
        away would just move this method's whole reason for existing one layer down. `"labels"`
        alone missing (with `"comments"` genuinely present) still defaults to `[]` — it is not the
        field this method's caller depends on for correctness. The caller (`decompose_check`'s
        `file`-mode idempotency check) must be able to tell "could not read the parent's own
        timeline" apart from "read it, found no `sigma:decompose-filed` marker" — collapsing
        those into one fail-open result could let a second, concurrent run silently file a
        duplicate meta-issue.

        Returns `{"comments": [...], "labels": [...]}` — the RAW gh JSON shapes, unnormalized
        (unlike `fetch_comments()`'s own `{"id","author","body","created_at"}` shape): the caller
        only needs comment `body` text (the marker substring) and label `name` strings
        (`area:`/`priority:`), so no normalization layer earns its keep here. Direct read of the
        one issue's own timeline — never a search-API query (eventually consistent, #447)."""
        # #895: REST first, ALL comments (paged by the issue's own count); a malformed REST payload
        # raises GhApiError. On the one-call fallback a malformed payload degrades to {} and the
        # missing-key check below raises, exactly as before.
        data = self._read_issue(goal, ["comments", "labels"])
        if "comments" not in data:
            raise ValueError("fetch_comments_strict: response has no 'comments' key — "
                             "malformed or incomplete")
        # #650: a marker from an untrusted author never reaches the readers (see `TRUSTED_ASSOCIATIONS`).
        return {"comments": trusted_marker_comments(data.get("comments") or [], goal),
                "labels": data.get("labels") or []}

    def append_to_body(self, goal, marker):
        """Append `marker` to the issue's CURRENT body — never overwrite it — read then write, not
        atomic (acceptable: a goal a loop just parked is not being concurrently edited by anyone
        else in that same instant). This is the MACHINE-readable channel: `backlog_check.py`'s
        `_explicit_blockers()` regexes the goal's own text for `blocked by ... #N`, and
        `mirror.py`'s corpus fetch is title+body ONLY — comments (see `note()` above) are never
        fetched, by design — so a dependency marker posted only as a comment is silently invisible
        to auto-skip, however clearly a human would read it on the issue page. Callers wanting
        BOTH the human-visible narrative and the machine-actionable marker call `note()` for the
        former and this for the latter — two different audiences, two different channels."""
        # #895: the `["body"]` subscript is deliberate data-loss protection -- a degraded `{}` read
        # raises KeyError BEFORE `issue edit`, so a malformed read can never blank the body.
        body = self._read_issue(goal, ["body"])["body"]
        new_body = (body or "").rstrip() + "\n\n" + marker + "\n"
        self._issue_edit_body(goal, new_body)           # REST first (#895 slice 3a); RAISES as before

    def issue_url(self, goal):
        return self._issue_url(goal)

    def create_dependency(self, title, body, assignee, labels=(), goal_label=True):
        """Open an issue carrying a cross-area dependency and hand it to its owner. Returns the new
        issue number, or None when `gh` did not hand one back.

        It carries the GOAL label deliberately (unless `goal_label=False`): an assigned goal issue is
        picked up by that person's OWN loop through the `assignee` filter, so a hand-off routes itself
        over the backlog the team already shares — no new transport and no daemon. This is the only
        place the kit ever SETS an assignee, and it is the point: parking told nobody, this tells
        exactly one person. `goal_label=False` is for a queued (not immediately-actionable) issue
        created via `handoff.create_tracked_issue` — it must NOT be auto-picked by anyone's loop until
        a human promotes it, so it deliberately does not carry the label `next_pending()` filters on.

        It also stamps the adopter's configured custom board fields (project.custom_fields) on the new
        issue, so an issue the loop creates isn't blank on Priority/Section/… while every human-made
        one carries them — the one board field-write beyond the built-in Status.

        Create and assign are deliberately TWO separate `gh` calls, never `issue create --assignee`
        combined (F14/#338, round 2 of its own review): `gh` performs those as two independent
        GraphQL mutations (`createIssue`, then `replaceActorsForAssignable`) even from one CLI
        invocation, and when the SECOND one fails — a CODEOWNERS `@org/team` owner, most often, since
        GitHub issues can only be assigned to individual collaborators, but any assignee `gh` rejects
        fails the same way — the issue it already created is not rolled back, and its number is never
        printed to stdout, so a combined call has no way to learn the orphan exists. An earlier
        version of this fix retried unassigned on that failure, which made it WORSE: a second,
        genuinely duplicate issue, with the first silently orphaned forever (confirmed empirically
        against real `gh`, not assumed). Creating unassigned FIRST and assigning as a separate,
        independently retriable step against the now-known issue number cannot ever produce a
        duplicate — a failed assign just leaves the one issue that already exists unassigned a little
        longer, with a comment on it saying why. `last_assignee_applied` records which happened, so a
        caller's own narrative (see handoff.hand_off) doesn't go on to claim an assignment that
        didn't take."""
        self._ensure_labels()
        for label in labels:
            try:
                # #1917: NO `--force` — see `_ensure_labels`. These labels are the CALLER's, not
                # the kit's (`handoff track --label ...` passes free text straight through), so a
                # repaint here lands on `priority:P1`, `area:*`, whatever the adopter colour-codes
                # by. Measured live: filing one plan flattened `priority:P1` (#d93f0b) and
                # `priority:P2` (#fbca04) both to #d4c5f9. Attaching a label must never restyle it.
                self._run(["label", "create", label, *self._repo_args(), "--color", "d4c5f9"])
            except Exception:
                pass                       # a missing label must not stop the hand-off
        # #895 slice 3a: REST `POST /issues` first (via `_issue_create`); a `feature:*` label is refused
        # unless it already exists (layer 2), and a create that fails ambiguously (timeout, 5xx) is
        # NEVER replayed -- one create call, the error raises.
        issue_labels = ([self.goal_label] if goal_label else []) + list(labels)

        self.last_assignee_applied = False
        number = self._create_issue(title, body, issue_labels)   # always unassigned -- see docstring
        if number is None:
            return None
        if assignee:
            try:
                self._issue_add_assignees(number, assignee)       # REST; verifies the login was kept
                self.last_assignee_applied = True
            except Exception as exc:
                # .hint (see _run_gh) is the short reason alone; str(exc) is the fallback for an
                # exception that never went through _run_gh (e.g. a test double) and so has no .hint.
                hint = getattr(exc, "hint", None) or str(exc)
                note = (f"Could not assign @{assignee} to this hand-off ({hint}) — left unassigned. "
                        "GitHub issues can't be assigned to a team; if that's not the cause here, the "
                        "account may not be a repo collaborator. Needs manual routing to the right owner.")
                try:
                    self._issue_comment(number, note)
                except Exception:
                    pass   # best-effort; the issue existing at all is what matters

        # Stamp the board fields on the issue we just made, INCLUDING its Priority column — the
        # label alone would leave the column blank until the next sync, and for a hand-off filed
        # with goal_label=False that sync never comes, because _sync_backlog only walks
        # goal-labelled issues. Both sides get set here, at creation.
        self._apply_custom_fields(number, labels=labels)
        return number

    def _create_issue(self, title, body, labels):
        """-> the new issue number as a string, or None when none came back. REST gives a dict
        (`str(resp["number"])`, None if absent or not an int); the ONE `gh issue create` fallback (a
        primary rate limit only) gives a URL whose last path segment is parsed as before. A failure RAISES."""
        resp = self._issue_create(title, body, labels)
        if isinstance(resp, dict):
            n = resp.get("number")
            return str(n) if isinstance(n, int) and not isinstance(n, bool) else None
        out = (resp or "").strip().splitlines()
        number = out[-1].rstrip("/").rsplit("/", 1)[-1] if out else ""
        return number if number.isdigit() else None

    # ----- Projects-v2 board (best-effort mirror of issue status onto a kanban board) -----
    # SDLC status -> GitHub's built-in "Status" single-select. The whole layer is fail-open: a missing
    # `project` token scope, an API hiccup, anything — it swallows the error so the loop never breaks.

    def _set_board_status(self, goal, status_name, warn=True):
        """Move `goal`'s card to `status_name`. Returns True only if the write GENUINELY LANDED.

        #280: `status_name` is matched against the board's Status options by `_status_option` --
        the exact spelling first, else the ONE option equal to it ignoring case and whitespace
        (`In progress` for `In Progress`). No option at all is reported ONCE per column per run
        (`_warn_unmatched_column`) instead of silently writing nothing; `warn=False` is for a caller
        with its own fallback (`_offboard`'s Parked -> Blocked).

        #1391 step 4: the return value is new. This used to return `None` unconditionally — success,
        board-disabled, board-unresolvable and a swallowed exception were all indistinguishable to
        the caller. That was fine while every caller was fire-and-forget mirroring, but a RECONCILER
        cannot be built on it: `triage._execute_action`'s new `set-status` kind has to report
        `done`/`failed` honestly, and reporting a card move as `done` when the board write silently
        failed would make the reconciler claim it had fixed a divergence it had not touched.

        Still fully fail-open — it never raises, and every existing caller ignores the return, so
        their behaviour is byte-identical. `False` means "not written", which for a board-disabled
        repo is the honest answer rather than an error."""
        if not self.project_enabled:
            return False
        try:
            if not self._ensure_board(exclude=goal):
                return False
            item_id = self._item_id(int(goal))
            name, opt = self._status_option(status_name)
            if not opt and warn:
                self._warn_unmatched_column(status_name)
            status_name = name or status_name
            if item_id and opt and self._field_id:
                self._run(["project", "item-edit", "--project-id", self._project_id, "--id", item_id,
                           "--field-id", self._field_id, "--single-select-option-id", opt])
                # #233: the same card update carries its Priority. `_sync_backlog` skips the goal
                # being moved (`exclude`), so without this the moved card's Priority waited for the
                # NEXT process's sync. Labels come from that sync's own read -- zero extra calls --
                # and a goal it never read (not goal-labelled) is left alone. `_mirror_priority`
                # never raises, so the Status write above is reported as landed whatever happens.
                #
                # #233 review: not onto a card this call just moved to Done -- a finished goal's
                # Priority is moot, and `_write_priority_field`'s #1206 guard would otherwise read
                # our own write as a STRANDED card and print a false "stuck at Done". And on a board
                # whose Priority field is not Sigma's, the label is the writer: never rewritten here.
                self._item_status[int(goal)] = status_name
                labels = self._issue_labels.get(int(goal))
                if labels is not None and status_name != self.col["done"]:
                    self._mirror_priority(int(goal), labels, item_id,
                                          label_writes=self._priority_owned())
                return True
            return False
        except Exception as exc:
            self._note_board_write_failed(exc)   # #1733: was _note_scope alone -- silent on anything
            return False                         # other than the one narrow, recognised shape

    def _apply_custom_fields(self, goal, labels=()):
        """Set the adopter's configured custom single-select board fields on an issue the loop itself
        created — so a loop-made issue isn't silently blank on a field like Priority/Section while
        every human-made issue on the board carries it. Only single-select fields are settable this
        way (like the built-in Status); a configured field the board doesn't have, or a value that
        isn't one of its options, is SKIPPED rather than guessed (/sigma-doctor flags those at setup).
        Fully fail-open: a board write never breaks the hand-off that already created the issue."""
        if not self.project_enabled or not (self._custom_fields or self.priority_field):
            return
        try:
            if not self._ensure_board(exclude=goal):
                return
            # Resolve the board FIRST, then decide whether there is anything to stamp. Checking
            # `_priority_field_id` (which only exists after the board is read) before carding is
            # what keeps an adopted board untouched: no custom fields and no Priority column means
            # we return here, and the issue is never added to the board — exactly as before.
            if not (self._custom_fields or self._priority_field_id):
                return
            item_id = self._item_id(int(goal))
            if not item_id:
                return
            # Carding the issue here to stamp its custom fields means _sync_backlog will now SKIP it as
            # "already on the board", so seed its Status here too or the card sits blank. A brand-new
            # hand-off belongs in Backlog — exactly where _sync_backlog would have placed it.
            backlog = self._status_option(self.col["backlog"])[1]
            if not backlog:
                self._warn_unmatched_column(self.col["backlog"])
            if backlog and self._field_id:
                self._run(["project", "item-edit", "--project-id", self._project_id, "--id", item_id,
                           "--field-id", self._field_id, "--single-select-option-id", backlog])
            for fname, value in self._custom_fields.items():
                fld = self._all_fields.get(fname) or {}
                opt = (fld.get("options") or {}).get(value)
                if fld.get("id") and opt:
                    self._run(["project", "item-edit", "--project-id", self._project_id, "--id", item_id,
                               "--field-id", fld["id"], "--single-select-option-id", opt])
            self._mirror_priority(int(goal), labels, item_id)
        except Exception as exc:
            self._note_board_write_failed(exc)   # #1733: was _note_scope alone, same gap as above

    @staticmethod
    def _norm_option(name):
        """#280: an option name compared ignoring case and runs of whitespace."""
        return " ".join(str(name or "").split()).casefold()

    @classmethod
    def _match_option(cls, names, want):
        """#280, THE Status-option rule -> (the board's name to use or None, [its variants]).
        The exact name wins; else the ONE option equal to `want` ignoring case and whitespace
        (GitHub's default `In progress` for our `In Progress`); several such variants and no exact
        one is ambiguous and matches nothing -- never a guess between two lanes."""
        # Deduplicated: a board with two lanes of one name (seen on a real board, research #280) is
        # one name to the loop's option dict, so it is one name here too -- doctor and the loop
        # resolve through this same function and must agree.
        names = list(dict.fromkeys(n for n in (names or ()) if isinstance(n, str)))
        if not want:
            return None, []
        if want in names:
            return want, []
        variants = [n for n in names if cls._norm_option(n) == cls._norm_option(want)]
        return (variants[0], []) if len(variants) == 1 else (None, variants)

    def _status_option(self, name):
        """(the board's spelling, option id) for column `name`, or (None, None). The Ready column
        is matched exactly (see `_ready_lane`): a write never lands in a lane the queue ignores."""
        if name == self.col["ready"]:
            opt = self._status_options.get(name)
            return (name, opt) if opt else (None, None)
        hit, _ = self._match_option(list(self._status_options), name)
        return (hit, self._status_options.get(hit)) if hit else (None, None)

    def _adopt_column_spellings(self, option_names):
        """#280: point every `self.col` entry the board spells differently (case/whitespace only)
        at the board's own spelling, IN MEMORY for this run -- so a card read back as `In progress`
        compares equal to the column the loop just wrote. Config is never written and the board is
        never renamed here: an adopted board's options are its owner's (board_setup.py maps them in
        config, #235). A column with no match keeps its configured name and is reported by
        `_warn_unmatched_column` when a write needs it."""
        names = list(option_names or ())
        for key, want in list(self.col.items()):
            if key == "ready":
                continue      # queue-mode selection is never spelling-adopted (`_ready_lane`)
            hit, _ = self._match_option(names, want)
            if hit:
                self.col[key] = hit

    def _notice_ready_variant(self, option_names):
        """#280 (review of PR #325): ONE informational line when the board has a lane that differs
        from the Ready column only in case/whitespace. The label queue stays in force (never a
        silent switch); the line names the explicit opt-ins. Never raises."""
        if self._ready_variant_noticed:
            return
        hit, variants = self._match_option(option_names, self.col["ready"])
        lanes = [hit] if hit else variants
        if not lanes:
            return
        self._ready_variant_noticed = True
        try:
            sys.stderr.write(
                "sigma: board #%s has a %s lane that differs from the Ready column %r only in case "
                "or spacing, so the loop stays on the label queue (the queue mode is never switched "
                "by spelling). To make that lane the queue, set "
                "discovery.github.project.columns.ready to %r, or run board_migrate.py.\n"
                % (self._project_number, " / ".join(repr(v) for v in lanes), self.col["ready"],
                   lanes[0]))
        except Exception:
            pass

    def _warn_unmatched_column(self, status_name):
        """#280: a card move whose column has NO matching Status option on the board. It used to
        write nothing and say nothing (the loop's own board kept GitHub's `In progress` while every
        pick asked for `In Progress`). Loud, once per column per run, never raises. `ready` is
        exempt: a board without it is the designed label queue (`_ready_lane`), not a fault."""
        key = next((k for k, v in self.col.items() if v == status_name), None)
        if key == "ready" or status_name in self._unmatched_warned:
            return
        self._unmatched_warned.add(status_name)
        opts = list(self._status_options)
        _, variants = self._match_option(opts, status_name)
        why = ("has no matching option (several differ from it only in case/spacing: %s, and none "
               "is exact)" % ", ".join(repr(v) for v in variants)) if variants else \
            "has no matching option"
        where = ("discovery.github.project.columns.%s" % key) if key else "discovery.github.project.columns"
        try:
            sys.stderr.write(
                "sigma: board column %r (project.columns.%s) %s on board #%s's %s field (options: "
                "%s) - card(s) NOT moved to it this run. Add that option on the board, or set %s to "
                "the board's spelling; /sigma-doctor lists every unmatched column.\n"
                % (status_name, key or "?", why, self._project_number,
                   self._project_cfg.get("status_field") or "Status",
                   ", ".join(opts) or "none", where))
        except Exception:
            pass

    def _warn_board_unresolved(self, owner, title):
        """Loud one-time note: board mirroring is on but the config doesn't identify an existing board,
        and the owner already has one — so sigma is NOT creating a (possibly duplicate) board. The
        opposite of the silent-create bug this guards. Printed to stderr; never raises."""
        pinned = self._project_cfg.get("number")
        why = (f"project.number={pinned} was not found under {owner}" if pinned is not None
               else f"no project.number is set and none of {owner}'s boards is titled {title!r}")
        try:
            sys.stderr.write(
                f"sigma: board mirroring OFF this run - {why}, so it will NOT create a new board "
                f"(that would risk a duplicate the loop then manages instead of yours). Set "
                f"discovery.github.project.number to the board you mean. Issues + labels still work.\n")
        except Exception:
            pass

    def _note_scope(self, exc):
        """A board write failed. If it's the missing-`project`-scope error (permanent, actionable),
        say so LOUDLY once - the board silently no-op'ing on a missing scope is a real trap. A
        transient blip (already retried in _run) stays silent: fail-open as before.

        Returns True when this recognised (and, at most once, reported) the scope error, so
        `_note_board_write_failed` (#1733) knows whether to fall back to a generic report instead.
        The other 8 call sites of this method predate that return value and all ignore it, exactly
        as before -- adding it changes nothing for them."""
        msg = str(exc).lower()
        if "project" in msg and ("scope" in msg or "auth refresh" in msg or "required scopes" in msg):
            if not self._scope_warned:
                self._scope_warned = True
                try:
                    sys.stderr.write(
                        "sigma: board updates OFF this run - the gh token lacks the `project` scope, "
                        "so cards are not being moved. Run: gh auth refresh -s project. Issues + labels "
                        "still work (the board is a mirror, not the source of truth).\n")
                except Exception:
                    pass
            return True
        return False

    def _note_board_write_failed(self, exc):
        """#1733: a board WRITE (not a read) failed and was about to vanish with zero trace --
        `_set_board_status`/`_apply_custom_fields` route here instead of straight to `_note_scope`.

        The missing-`project`-scope case still gets ITS specific, more actionable message via
        `_note_scope`, unchanged. Anything else previously matched nothing in `_note_scope` and
        printed NOTHING AT ALL -- exit 0, correct labels, a silently stale board, exactly what #1733
        reported. This is the generic net behind it: report once per DISTINCT failure text (not a
        bare once-ever flag), so two genuinely different failures within one process -- e.g.
        `_offboard`'s own `parked` then `blocked` fallback failing for two different reasons -- are
        both visible, while the same failure repeating doesn't spam.

        Scoped to these two write call sites ON PURPOSE, never a wider swap into `_note_scope`
        itself. Its other 8 call sites are NOT all reads -- corrected after review: `_archive_card`
        and `_ensure_priority_field` are writes too (an archive mutation, a `field-create`), sharing
        this same silent-swallow gap at lower severity, each already documented at its own call site
        as cosmetic-on-failure ("a missing column costs a column, never the run"; archiving is
        hygiene, not correctness). Left untouched here as a narrower fix scoped to what #1733
        actually reported (a park/status write); see #1738 for those two plus the priority/label
        write paths near `_ensure_status_field`. The one site that IS a genuine read with its own
        follow-up print, and where widening `_note_scope` itself would double-print, is the
        board-queue read falling back to the label queue -- that one's generic text ("board update
        failed") would also be flatly wrong for a read, which is the reasoning that generalises."""
        if self._note_scope(exc):
            return
        key = str(exc)
        if key in self._board_write_failures_warned:
            return
        self._board_write_failures_warned.add(key)
        try:
            sys.stderr.write(
                "sigma: board update failed (%s) - card left unmoved; issue labels remain the "
                "source of truth.\n" % exc)
        except Exception:
            pass

    def _proj_owner(self):
        return self._project_cfg.get("owner") or (self.repo.split("/")[0] if "/" in self.repo else "@me")

    def _proj_title(self):
        name = self.repo.split("/")[-1] if self.repo else "project"
        return self._project_cfg.get("title") or f"{name} — SDLC"

    def _ensure_board(self, exclude=None):
        """Find-or-create the board + its status field once per run; seed the backlog as Todo.
        Returns True only when the board is fully wired. Idempotent and attempt-once on hard failure."""
        if self._board_ready or self._board_attempted:
            return self._board_ready
        self._board_attempted = True
        owner, title = self._proj_owner(), self._proj_title()
        number, pid, created_now = self._find_project(owner, title)
        if number is None:
            # Auto-create ONLY when the owner has NO board at all (an unambiguous fresh setup). If the
            # owner already has board(s) but none matched our number/title, creating "{repo} — SDLC"
            # would silently spawn a DUPLICATE and quietly manage the wrong one — the config is
            # under-specified, so refuse and say so loudly (fail-open: issues + labels still work).
            if self._owner_had_boards:
                self._warn_board_unresolved(owner, title)
                return False
            data = self._gh_json(["project", "create", "--owner", owner, "--title", title, "--format", "json"])
            number, pid, created_now = data.get("number"), data.get("id"), True
        if number is None or pid is None:
            return False
        self._created_board_now = bool(created_now)
        self._project_number, self._project_id = number, pid
        if self.repo:
            try:
                self._run(["project", "link", str(number), "--owner", owner, "--repo", self.repo])
            except Exception:
                pass   # linking is cosmetic; items reference issues by URL regardless
        self._ensure_status_field(owner, number, created_now)
        self._load_items(owner, number)
        if self._field_id:
            self._sync_backlog(owner, number, exclude)
        self._board_ready = bool(self._field_id)
        return self._board_ready

    def _find_project(self, owner, title):
        """(number, id, created_now=False) for an existing board matching the configured number or
        the title, else (None, None, False) so the caller creates one. Also records whether the owner
        had ANY boards, so _ensure_board can refuse to create a duplicate into an owner that already
        has one (the config just didn't point at it)."""
        want_num = self._project_cfg.get("number")
        try:                                              # config may author the number as a string
            want_num = int(want_num) if want_num is not None else None
        except (TypeError, ValueError):
            want_num = None
        data = self._gh_json(["project", "list", "--owner", owner, "--format", "json", "--limit", "100"])
        projects = (data.get("projects") if isinstance(data, dict) else data) or []
        self._owner_had_boards = len(projects) > 0
        # #235: a PINNED number wins over a title match, and is honoured even when it sits outside
        # the one page of 100 boards read above (an org with >100 boards): that one board is read
        # directly -- one extra read, and only when the pin was not in the page. A pin that still
        # cannot be read falls through to the title match and then to the loud refusal, as before.
        if want_num:
            for p in projects:
                if p.get("number") == want_num:
                    return p.get("number"), p.get("id"), False
            try:
                p = self._gh_json(["project", "view", str(want_num), "--owner", owner,
                                   "--format", "json"])
                if isinstance(p, dict) and p.get("number") == want_num and p.get("id"):
                    self._owner_had_boards = True
                    return p.get("number"), p.get("id"), False
            except Exception as exc:
                self._note_scope(exc)
        for p in projects:
            if p.get("title") == title:
                return p.get("number"), p.get("id"), False
        return None, None, False

    def _ensure_status_field(self, owner, number, created_now):
        # Drive GitHub's BUILT-IN "Status" field so the default Board view groups by it natively — no
        # manual "group by" step, and no orphan second field. On a fresh board we rewrite its options
        # to our columns via GraphQL (the gh CLI has no field-edit); on an adopted board we use the
        # configured field's existing options as-is (the user set them up — like a shared team board).
        fname = self._project_cfg.get("status_field") or "Status"
        fields = self._list_fields(owner, number)
        fld = self._find_field(fields, fname)
        # `ready` sits between backlog and in-progress: Backlog = filed, Ready = the current sprint.
        # Only ever written onto a board this kit CREATES (the `created_now` branch below, and
        # `field-create` when the field is absent entirely) — an adopted board's options are used
        # as-is, which is what keeps every existing adopter on the label queue (see `_ready_lane`).
        cols = [self.col["backlog"], self.col["ready"], self.col["in_progress"], self.col["qc"],
                self.col["done"], self.col["blocked"], self.col["parked"]]
        if fld is None:
            # the configured status field doesn't exist (a custom name on an adopted board) → create it
            self._run(["project", "field-create", str(number), "--owner", owner, "--name", fname,
                       "--data-type", "SINGLE_SELECT", "--single-select-options", ",".join(cols),
                       "--format", "json"])
            fields = self._list_fields(owner, number)        # re-list to read back the new option ids
            fld = self._find_field(fields, fname)
        elif created_now:
            # fresh kit-created board: rewrite the built-in Status field's options to our columns
            # Pass the field's CURRENT options so their ids survive the rewrite (#720).
            # A board WE just created, so its options are GitHub's own defaults — which makes the
            # one rename we need (`Todo` -> our backlog column) safe to name explicitly. If GitHub
            # ever changes that default the rename simply does not fire and `Todo` survives as an
            # extra lane: cosmetic, not broken.
            # #280: `In progress` too. Real default boards carry a lowercase-p `In progress`
            # (read-only, 2026-09-29, `.sdlc/research/280.md`; others carry `In Progress`, when the
            # rename is a no-op), and without this rename rule 1 of `_options_mutation` CLAIMS it
            # under GitHub's spelling -- so the loop's own board kept
            # `In progress` while every pick looked up `In Progress` and silently moved nothing.
            # Same id-preserving update as `Todo`; board_setup.py's fresh path renames both too.
            self._run(["api", "graphql", "-f",
                       self._options_mutation(fld.get("id"), cols, fld.get("options") or [],
                                              rename={"Todo": self.col["backlog"],
                                                      "In progress": self.col["in_progress"]})])
            fields = self._list_fields(owner, number)        # re-list to read back the new option ids
            fld = self._find_field(fields, fname)
        if fld:
            self._field_id = fld.get("id")
            self._status_options = {o.get("name"): o.get("id") for o in (fld.get("options") or [])}
            self._adopt_column_spellings(list(self._status_options))
        # Cache EVERY single-select field's options (Status plus any custom Priority/Section/…), so a
        # custom-field write can resolve a field id + option id by name. Single-select fields are the
        # ones that carry `options`; a text/number/date field has none and isn't settable this way.
        self._all_fields = {f.get("name"): {"id": f.get("id"),
                                            "options": {o.get("name"): o.get("id") for o in (f.get("options") or [])}}
                            for f in fields if f.get("options")}
        self._ensure_priority_field(owner, number, created_now)

    def _ensure_priority_field(self, owner, number, created_now):
        """Find (or, on a board we created, create) the P0-P4 Priority column, and cache its option
        ids so `_sync_backlog` can mirror the label onto it.

        Created ONLY on a board Sigma itself made — the same gate that protects an adopted
        board's Status options. Adding a field to somebody else's board is additive rather than
        destructive, but it is still their board; that belongs in the opt-in migration, not in a
        loop tick. On an adopted board this just looks for the field and mirrors onto it if the
        adopter happens to have one by that name.

        Fail-open throughout: no Priority column simply means nothing is mirrored, which is exactly
        the behaviour every existing adopter already has."""
        if not self.priority_field:
            return
        # #233 review: ONE name rule for both paths (`_match_field`): the exact name, else a single
        # case-only variant (`PRIORITY`), never a guess between several. A case-only adoption, or
        # any Priority field on a board whose field is not Sigma's, is mirrored label -> field only:
        # #719's field-wins label rewrite stays exactly where it already ran (an exact-name field),
        # plus Sigma's own field, and is never extended to a column the sync did not read before.
        name, clash = self._match_field(list(self._all_fields), self.priority_field)
        if clash:
            # `gh project item-list` flattens every field under its LOWERCASED name, so with two
            # case-variants of the column one card value overwrites the other: whatever is read
            # may be the wrong field's. Mirror nothing rather than write from a guess.
            self._warn_field("the board has several %r fields differing only in case (%s), so "
                             "their values cannot be told apart" % (self.priority_field,
                                                                    ", ".join(clash)))
            return
        fld = self._all_fields.get(name) if name else None
        self._priority_label_writes = self._priority_owned() or name == self.priority_field
        if fld is None and created_now:
            try:
                self._run(["project", "field-create", str(number), "--owner", owner,
                           "--name", self.priority_field, "--data-type", "SINGLE_SELECT",
                           "--single-select-options", ",".join(discovery.PRIORITIES),
                           "--format", "json"])
                fields = self._list_fields(owner, number)
                found = self._find_field(fields, self.priority_field)
                if found:
                    fld = {"id": found.get("id"),
                           "options": {o.get("name"): o.get("id") for o in (found.get("options") or [])}}
                    self._all_fields[self.priority_field] = fld
            except Exception as exc:
                self._note_scope(exc)      # a missing column costs a column, never the run
                return
        if fld:
            self._priority_field_id = fld.get("id")
            self._priority_options = fld.get("options") or {}

    def _priority_name(self, labels):
        """The P0-P4 name this issue's labels imply, or None when it carries no recognised priority.

        Ranked by `discovery.priority_rank` — the SAME ranking `_pick_key` uses to order the queue —
        so the column and the queue can never disagree about which label wins on an issue carrying
        several (#381 here carries both P0 and P1). `labels` is the raw `gh` shape: a list of
        `{"name": ...}` dicts."""
        # Two shapes reach here: `gh issue list --json labels` gives [{"name": ...}], while
        # `gh project item-list` gives plain strings. Normalise rather than making callers care.
        names = [(l.get("name") or "") if isinstance(l, dict) else str(l or "")
                 for l in (labels or [])]
        ranks = [discovery.priority_rank(n, self.priority_aliases) for n in names if n.startswith(self.priority_prefix)]
        ranks = [r for r in ranks if r < discovery.UNPRIORITISED]
        return discovery.PRIORITIES[min(ranks)] if ranks else None

    @staticmethod
    def _options_mutation(field_id, names, existing=(), rename=None):
        """The GraphQL `updateProjectV2Field` mutation that sets a single-select field's options,
        PRESERVING the ids of the options already there. The gh CLI has no field-edit, so this is
        how the built-in Status field gets our columns.

        Preserving ids is not a nicety (#720). An ID-LESS rewrite DELETES and recreates every
        option, and GitHub then disables any built-in workflow that pointed at one. Measured on a
        throwaway project: a fresh board has all six workflows ENABLED, and an id-less rewrite turns
        five of them off — including `Item closed`, the workflow that moves a card to Done when its
        issue closes. That is the mechanism behind the 92 stranded cards on this repo's own board,
        and behind the five epic cards that had no Status at all. It is the same failure that wipes
        card values (`board_migrate.py`'s rehearsal step [3]); it just costs workflows too.

        Matching, in order:
          1. a desired name that an existing option already has, EXACTLY or differing only by
             case  -> reuse that id, and keep the EXISTING spelling rather than ours. GitHub's own
             defaults are `In progress` (lowercase p); a caller asking for `In Progress` used to
             miss this and every later rule, landing on rule 3 and creating a genuine duplicate
             lane every time setup ran (#1492). A case-only difference is a claim, never a rename —
             renaming stays exclusive to rule 2, the only rule allowed to change what a human sees.
          2. a desired name listed in `rename` as the new name of an existing option -> reuse that
             option's id, so GitHub's default `Todo` becomes our `Backlog` rather than being
             deleted alongside its workflows;
          3. anything else                                       -> a genuinely new option, no id;
          4. any existing option we never claimed                -> KEPT under its own name. Never
             delete or repurpose a lane somebody added.

        `rename` is deliberately an EXPLICIT map rather than pairing leftovers in order. An earlier
        version paired positionally and quietly renamed an adopter's `Needs design` lane to `Ready`
        — which, now that Status is the queue, would have enqueued every card sitting in it. A
        rename is only ever safe when the caller can name both sides, and the only caller that can
        is the fresh-board path, where GitHub itself created the options we are replacing.

        `existing` defaults to empty, which reproduces the historical all-id-less output exactly —
        so a caller that has not read the field yet is no worse off than before.

        Colour and description (#235, review of PR #279): an existing option that carries a valid
        `color` and a `description` is sent back WITH them, so a rewrite never recolours or blanks
        a lane a human styled. Only a new option, or an existing one read without them (`gh project
        field-list` returns just id + name), gets the colour-by-position and an empty description.

        Every interpolated value -- field id, option id, name, description -- is a JSON-quoted
        GraphQL string literal (JSON's escapes are a subset GraphQL accepts), never pasted raw: a
        human's lane named `Needs "design"` otherwise ends the string and breaks the mutation."""
        colors = ("GRAY", "YELLOW", "ORANGE", "GREEN", "RED", "BLUE", "PURPLE", "PINK")
        existing = [o for o in (existing or []) if isinstance(o, dict)]
        by_id = {o.get("id"): o for o in existing if o.get("id")}
        by_name = {o.get("name"): o.get("id") for o in existing if o.get("id")}
        # Casefolded fallback for rule 1's case-insensitive half. `setdefault` so the FIRST existing
        # option under a given casefold wins — irrelevant on a healthy board (names are unique) and
        # merely stable, not a cleanup, on one that already carries a case-duplicate.
        by_name_ci = {}
        for o in existing:
            nm, oid_ = o.get("name"), o.get("id")
            if nm and oid_:
                by_name_ci.setdefault(nm.casefold(), (nm, oid_))
        # {new name -> old name}, so a desired column can claim the id of the option it replaces.
        was = {new: old for old, new in (rename or {}).items()}

        def style(oid, position):
            o = by_id.get(oid) or {}
            color = str(o.get("color") or "").upper()
            desc = o.get("description")
            return (color if color in colors else colors[position % len(colors)],
                    desc if isinstance(desc, str) else "")

        claimed, entries = set(), []
        for i, n in enumerate(names):
            entry_name, oid = n, (by_name.get(n) or by_name.get(was.get(n)))
            if not oid:
                hit = by_name_ci.get(n.casefold())
                if hit:
                    entry_name, oid = hit          # keep GitHub's existing spelling, not ours
            if oid:
                claimed.add(oid)
            entries.append((entry_name, oid) + style(oid, i))
        for j, o in enumerate([o for o in existing if o.get("id") not in claimed]):
            entries.append((o.get("name"), o.get("id")) + style(o.get("id"), len(entries) + j))
        q = lambda v: json.dumps(str(v))        # noqa: E731 - a GraphQL string literal
        opts = ", ".join('{%sname: %s, color: %s, description: %s}'
                         % (("id: %s, " % q(oid)) if oid else "", q(n), c, q(d))
                         for n, oid, c, d in entries)
        return ('query=mutation { updateProjectV2Field(input: {fieldId: %s, singleSelectOptions: [%s]}) '
                '{ projectV2Field { ... on ProjectV2SingleSelectField { id } } } }' % (q(field_id), opts))

    def _sync_backlog(self, owner, number, exclude):
        """Seed the board with any open goal issue not yet carded (as Todo), except the one being
        actively transitioned. Cards already on the board keep their status — sync never clobbers.

        Pages from the OLDEST end (`sort=created&direction=asc`, next_pending's own qualifier —
        see F12/#348 there): this reads the same goal-labelled backlog next_pending picks from, so
        a bare created-DESC page would seed the board with the newest 200 goals and never card the
        ones actually being worked.

        #1833: migrated off `gh issue list --label <goal_label> --search sort:created-asc` onto
        the same `_fetch_issues_rest` helper `_fetch_pending`/`list_needs_label` already use — this
        call shares their exact graphql-search-billing defect (`--label` forces `gh` through the
        billed-against-the-shared-5000/hour `graphql` `search()` field regardless of `--search`,
        confirmed live) and was the one call site #1829 explicitly enumerated but left out of its
        own scope, being off the `next`/`next-batch` pick path (this one runs on every board touch
        instead — see `_ensure_board`). `#900`'s old note about a CONDITIONAL `--json number,labels`
        vs `--json number,labels,title,body` no longer applies: REST has no field-selection concept
        at all, so `title`/`body` come back on every fetch regardless of `blocker_promotion_mode` —
        at no extra request cost either way, since it was always exactly one call."""
        issues = self._fetch_issues_rest([self.goal_label], self._BOARD_ITEM_LIMIT)
        self._warn_truncated("open goal issues", len(issues))
        # #693: this query is `--label <goal_label>` ONLY, so every issue it returns is BY DEFINITION
        # already queued. Filing those under a column literally named "Backlog" is what made the
        # board unreadable — you could not tell a filed goal from a committed one by looking. Seed
        # them into `Ready` when the board has that lane, and keep using `Backlog` when it does not,
        # so an adopted board's cards land exactly where they always did.
        seed_name = self.col["ready"] if self._status_options.get(self.col["ready"]) else self.col["backlog"]
        seed = self._status_options.get(seed_name)
        if not seed:
            self._warn_unmatched_column(seed_name)      # #280: never a silently uncarded backlog
        backlog_name = self.col["backlog"]
        on_board = set(self._items or {})            # numbers already carded
        for it in issues:
            n = it.get("number")
            if n is not None:
                self._issue_labels[int(n)] = it.get("labels")   # #233: `_set_board_status` reads it
            if n is None or str(n) == str(exclude):
                continue
            was_new = int(n) not in on_board
            item_id = self._item_id(n)
            if not (item_id and seed):
                continue
            # Promote-only, and only out of Backlog. A card already in In Progress / QC / Done /
            # Blocked is never touched — that is the "sync never clobbers" invariant, narrowed to
            # the single transition that cannot lose information: a goal-labelled card sitting in
            # Backlog is queued and mis-filed, so say so. There is deliberately NO demote here: the
            # loop REMOVES the goal label when it parks (`_offboard`), so "carded but unlabelled ->
            # Backlog" would drain the whole Blocked column on the next tick.
            current = self._item_status.get(int(n))
            stale_backlog = (not was_new and seed_name != backlog_name
                             and current == backlog_name)
            # THIRD seedable case, and the one that never self-healed: a card that is already ON
            # the board carrying NO Status at all. `was_new` is False (it is carded) and blank is
            # not `backlog_name`, so neither clause above fired and the card stayed blank forever
            # — invisible to `_board_queue` (which matches `status == ready_name`) and to
            # `_warn_unseeded` (which then counted only cards in Backlog; since #235 it counts a
            # blank goal card too), so nothing even reported it.
            # Measured on this repo's board #6: 8 open cards, five of them goal-labelled
            # (#739-743) with a correctly mirrored Priority but no Status. Those five are the
            # natural experiment that proves the asymmetry — `_mirror_priority` handles its own
            # blank case explicitly (`elif not field_value and label_value`) and this did not, so
            # the same sync pass filled one column and skipped the other on the same card.
            # Blank is not a lane anybody chose, so seeding it cannot lose a human's decision —
            # which is what keeps this inside the "sync never clobbers" invariant.
            if was_new or stale_backlog or not current:
                self._run(["project", "item-edit", "--project-id", self._project_id, "--id", item_id,
                           "--field-id", self._field_id, "--single-select-option-id", seed])
                # #900 round 2: write straight through to the in-memory snapshot too, not just the
                # board -- `_promote_blockers` below reads `self._item_status` to decide what's
                # genuinely pickable, and without this it would still see this card's PRE-seed
                # status (Backlog, or blank) even though this exact write just made it Ready.
                # Fixes the "stale_backlog" half of a gap the uncarded case already handled
                # correctly by accident (no recorded status reads as forgiving); this makes both
                # halves correct for the same reason instead of one being an accident.
                self._item_status[int(n)] = seed_name
            self._mirror_priority(int(n), it.get("labels"), item_id)
        self._promote_blockers(issues)

    def _promote_blockers(self, issues):
        """Opt-in blocker priority promotion (#900): when `discovery.blocker_promotion.mode` is
        "smart" or "always", a blocker's priority is raised to match the urgency of what it blocks.
        Today the loop only PARKS the blocked issue and never touches the blocker's own priority,
        so a P2 blocker sitting behind a P0 can sit at P2 forever. Runs once per open `sdlc:goal`
        issue, on every board touch, from `_sync_backlog` — the same cadence `_mirror_priority`
        already runs at, over the exact same fetched `issues` (`_sync_backlog` only requests the
        `title`/`body` this needs when this mode is not "off" to begin with — see its docstring).

        Gated FIRST on `self.blocker_promotion_mode != "off"` so an adopter who never sets this key
        pays NOTHING beyond that one already-cheap attribute check: no ledger read, no regex scan,
        no extra `gh` call.

        REVERSE LOOKUP (given a blocker, what does it directly block?) — two independent, additive
        channels, mirroring backlog_check.py's own `_explicit_blockers()` + ledger-hand-off split
        for the FORWARD question ("what blocks THIS goal"):
          1. EXPLICIT: `backlog_check._BLOCK_RE`, read LIVE off the module every call (via
             `_get_backlog_check()` — never copied into a local pattern, so a monkeypatch of the
             real regex is observable, the same live-attribute-read contract `triage.py`'s
             `_scan_block_edges` already relies on), over each issue's own title+body — the exact
             haystack shape and precision rule (the referenced issue must be a real OPEN member of
             this corpus, never the referencing issue itself) as `_explicit_blockers()` /
             `_scan_block_edges()`.
          2. LEDGER: `ledger.outstanding()` hand-off entries — the entry's `goal` is the blocked
             issue, `ledger.handoff_key()` (its `issue` field, falling back to `goal`) is the
             blocker. This channel is what SURVIVES a park that strips `sdlc:goal` from the blocked
             issue (a ledger entry is a local file, independent of GitHub label state) — the
             explicit channel alone cannot see that case: `_sync_backlog`'s own corpus is
             `--state open --label sdlc:goal`, so a blocked issue already parked for exactly this
             reason has already lost the label this whole scan depends on to see it at all. Needs
             `self.sdlc_dir` (optional, #900) — absent (every pre-#900 construction site), this
             channel is silently unavailable and the explicit channel alone still works.
        Both channels are restricted to blockers that are THEMSELVES members of this corpus:
        promoting a blocker means reading its current label / rank / board item, which this pass
        only has on hand for issues `_sync_backlog` already fetched — anything else would cost
        additional `gh` calls this mechanism has no budget for. The blocked side of an edge is
        restricted the same way, for the same reason: computing "has other unblocked work at its
        tier" needs that issue's own rank too.

        TRANSITIVE WALK: if C blocks B blocks A, C must ALSO promote — `resolve()` below calls
        `discovery.blocker_promotion_rank` once per edge, walking from the most-blocked end
        inward, feeding one level's PROMOTED (not original) rank in as the next level's `own_rank`,
        exactly as s0's own docstring documents and its own test proves composes correctly.
        Guarded against a cycle in the (hand-authored, so never fully trusted) blocking graph: a
        back-edge into a node still being resolved is broken by treating it as contributing no
        further promotion pressure (that node's own current rank, not a re-entrant call), and every
        cycle-involved ref is reported once via stderr — detected and neutralised, never a hang or
        a crash.

        WRITE: performed through the exact same mechanism `_mirror_priority` itself writes through
        (`_write_priority_label` / `_write_priority_field`) — never a second, parallel write path.
        Every write is paired with an issue comment naming the cause (`_write_blocker_promotion`);
        never a silent label change. A promotion can only ever raise a blocker's urgency, never
        lower it (`discovery.blocker_promotion_rank`'s own `min()` guarantees this), so a blocker
        already at least as urgent as everything it blocks costs nothing to check twice.

        Fully fail-open, one outer try/except around the whole pass — like every other board
        write in this class, a broken promotion pass must never break the sync it rides along on."""
        if self.blocker_promotion_mode == "off":
            return
        try:
            by_ref = {str(it["number"]): it for it in issues if it.get("number") is not None}
            open_refs = set(by_ref)
            if not open_refs:
                return

            directly_blocks = {}                        # blocker ref -> [directly-blocked ref, ...]

            def add_edge(blocked, by):
                by, blocked = str(by), str(blocked)
                if by == blocked or by not in open_refs or blocked not in open_refs:
                    return                               # self-reference, or outside this corpus
                directly_blocks.setdefault(by, [])
                if blocked not in directly_blocks[by]:
                    directly_blocks[by].append(blocked)

            backlog_check = _get_backlog_check()
            for ref, it in by_ref.items():
                haystack = (it.get("title") or "") + "\n" + (it.get("body") or "")
                for m in backlog_check._BLOCK_RE.finditer(haystack):
                    add_edge(ref, m.group(2))

            if self.sdlc_dir:
                try:
                    for h in ledger.outstanding(ledger.read_all(self.sdlc_dir)):
                        add_edge(h.get("goal"), ledger.handoff_key(h))
                except Exception:
                    pass         # the ledger is a second, additive channel; explicit markers alone still work

            if not directly_blocks:
                return                                   # nothing blocks anything in this corpus

            def own_rank(ref):
                return discovery.priority_rank(self._priority_name(by_ref[ref].get("labels")))

            # "Has other unblocked work at its tier" (per dependent): some OTHER issue at the same
            # priority tier that is not itself blocked AND is genuinely pickable next by the actual
            # picker this promotion mechanism only ever runs under (`_board_queue`, gated on
            # `project_enabled` -- see this method's own docstring). Open + goal-labelled + not
            # blocked alone is NOT that picker's definition once a Ready lane exists: `_board_queue`
            # only ever hands out a card whose Status equals the Ready lane name (line ~548) -- a
            # card already sitting in In Progress / QC / Done / Blocked can never be picked next,
            # however open and unblocked its issue looks. Reviewer-proved gap: #921(P0) blocked by
            # #922(P2), with a third #923(P0) open and unblocked but already carded In Progress --
            # the pre-fix version counted #923 as "other unblocked work" and wrongly withheld
            # #922's promotion, even though #923 can never actually be picked next.
            #
            # `self._item_status` (populated by `_load_items`, already read for this exact sync --
            # zero extra `gh` cost) and `self._ready_lane()` (cached after its first call) are
            # cross-checked per candidate: a ref WITH a recorded board status only counts as
            # pickable when that status IS the Ready lane. A ref with NO recorded status (never
            # carded) is deliberately still counted as pickable, NOT excluded: it is exactly what
            # THIS SAME `_sync_backlog` pass's own per-issue loop, immediately above, is seeding
            # into Ready right now (`_item_status` is a pre-sync snapshot -- see `_load_items` --
            # so an issue that loop is about to card straight into Ready still reads as "no card"
            # here). Confirmed against the existing test suite: an uncarded same-tier sibling was
            # already treated as "other work" before this fix and every passing test that relies on
            # that (none of them card anything) would go the wrong way if "no card yet" were
            # excluded instead of the historical, forgiving default `_ready_lane`'s own docstring
            # already documents for a single uncarded issue.
            #
            # `_ready_lane()` can still be None even though `_promote_blockers` only ever runs with
            # `project_enabled` True: an adopted board with `queue_source: "label"`, or one with no
            # `Ready` option at all, has no Status concept the picker uses for queuing --
            # `next_pending` falls all the way through to the historical label queue in that case
            # (`_first_by_label`), whose OWN pickability rule is exactly "open + goal-labelled +
            # not parked", i.e. this corpus by construction. The sane fallback with no Ready lane to
            # check against is therefore the pre-fix rule, unchanged -- it already matches the
            # queue that would actually serve the pick.
            blocked_refs = {b for lst in directly_blocks.values() for b in lst}
            ready_name = self._ready_lane()
            # #1468, stated rather than left to be rediscovered: this pool is built from the
            # unfiltered `--label goal_label` corpus and is THE ONE consumer in this file that does
            # not consult `not_eligible_labels()`. So a goal carrying an OVERLAY -- `sdlc:in-progress`
            # today, `sdlc:needs-label` now -- counts as "other unblocked work" at its tier even
            # though the picker would refuse it. That is pre-existing (in-progress goals have always
            # counted) and it errs in the SAFE direction: over-counting alternatives makes this
            # UNDER-promote blockers, i.e. it declines to grant membership it was not certain about,
            # never the reverse. Narrowing it would change promotion decisions for `sdlc:in-progress`
            # too, which is a different question from this one.
            #
            # FOLLOW-UP: **#1520**, and the reason it is filed rather than left as this comment is
            # that this comment is now the only thing stopping the next author from "fixing" it in
            # the UNSAFE direction -- narrowing the pool without noticing that fewer alternatives
            # means MORE promotions, i.e. more writes to issues the loop does not own.
            pickable_by_rank = {}
            for ref in open_refs:
                if ref in blocked_refs:
                    continue                              # itself blocked -> not "other unblocked work"
                status = self._item_status.get(int(ref))
                if ready_name is not None and status and status != ready_name:
                    continue                              # carded, but not in Ready -> can't be picked next
                pickable_by_rank.setdefault(own_rank(ref), []).append(ref)

            def has_other_unblocked_work(ref):
                # #900 round 2: `ref`'s OWN tier for this question must be its EFFECTIVE (promoted)
                # rank when one has already been resolved, not its raw label rank -- mirrors
                # triage.py's `_effective_rank`'s already-reasoned identical choice (its own
                # docstring's "RAW VS EFFECTIVE" section) for the exact same question on a
                # different graph shape. TWO call sites (round-3 review correction -- an earlier
                # draft of this comment said "only ever called from resolve() below", which named
                # just one): inside `resolve()`'s own `deps` comprehension below (`resolve(b)` runs
                # before `has_other_unblocked_work(b)` in that same tuple, left-to-right evaluation
                # guarantees `resolved[b]` is populated by then), AND in the `cause = next(...)`
                # search further down, reached only after the `for ref in list(directly_blocks):
                # resolve(ref)` loop has fully finished -- every reachable ref is in `resolved` by
                # that point too, for a different reason (the walk already completed, not
                # same-tuple ordering). `resolved[ref]` is populated at both call sites, just not
                # for the same reason; `.get(..., own_rank(ref))` is a defensive fallback either
                # way, not the expected path at either site. The POOL being searched
                # (`pickable_by_rank`, bucketed by each
                # OTHER candidate's own raw rank) is deliberately left as-is: those candidates
                # aren't being promoted by this check, they're just "is there a same-tier
                # alternative sitting ready" -- exactly how triage.py's own `n["priority"]`
                # comparison on the other side of its identical check stays raw too.
                return bool(pickable_by_rank.get(resolved.get(ref, own_rank(ref))))

            resolved, in_progress, stack, cycles = {}, set(), [], set()

            def resolve(ref):
                if ref in resolved:
                    return resolved[ref]
                if ref in in_progress:
                    # Every node from `ref`'s FIRST appearance on the stack through to the current
                    # top is genuinely IN this cycle, not just `ref` (the single node the DFS
                    # happened to re-enter on) -- for A->B->C->A, re-entering on A alone used to
                    # name only A, silently leaving B and C unreported even though both are equally
                    # cycle-involved. `stack` mirrors `in_progress` (push/pop symmetric with
                    # add/discard below) purely to recover that membership cheaply on the rare path
                    # where a cycle is actually found; it changes nothing about the recursion itself.
                    cycles.update(stack[stack.index(ref):])
                    return own_rank(ref)                  # break the cycle: no promotion pressure from it
                in_progress.add(ref)
                stack.append(ref)
                deps = [(resolve(b), has_other_unblocked_work(b)) for b in directly_blocks.get(ref, ())]
                stack.pop()
                in_progress.discard(ref)
                resolved[ref] = discovery.blocker_promotion_rank(
                    own_rank(ref), deps, self.blocker_promotion_mode)
                return resolved[ref]

            for ref in list(directly_blocks):
                resolve(ref)

            if cycles:
                self._warn_blocking_cycle(cycles)

            for ref, new_rank in resolved.items():
                old_rank = own_rank(ref)
                if new_rank >= old_rank:
                    continue                              # never a promotion (min() can't exceed own_rank)
                # The direct dependent(s) that actually won the min() -- restricted to the SAME
                # eligibility rule blocker_promotion_rank itself applied, so "smart" mode can never
                # misattribute the cause to a dependent that was excluded from its own computation.
                cause = next((b for b in directly_blocks.get(ref, ())
                             if resolved[b] == new_rank and
                             (self.blocker_promotion_mode == "always" or not has_other_unblocked_work(b))),
                            None)
                self._write_blocker_promotion(ref, by_ref[ref], old_rank, new_rank, cause)
        except Exception as exc:
            self._note_scope(exc)

    def _write_blocker_promotion(self, ref, it, old_rank, new_rank, cause):
        """Write ONE blocker's promoted priority + its audit comment naming the cause. THREE
        independent `gh` calls -- label write, field write, comment post -- each independently
        try/excepted, mirroring `_offboard`'s own shape for a multi-call sequence (one call's
        failure can never suppress reporting on, or silently mask, another's).

        Two network calls can never be made truly atomic, so `#900`'s "never a silent label
        change" requirement isn't met by preventing a partial write (impossible) -- it's met by
        making the one DANGEROUS partial outcome loud and specific instead of indistinguishable
        from an ordinary failure. That outcome is: the label write (the source of truth --
        `_write_priority_label`'s own docstring) lands, and THEN the comment explaining why fails.
        Reported via `_warn_promotion_comment_failed`, unconditionally -- never the generic,
        scope-gated `_note_scope`, which for a non-scope error prints nothing at all -- so nothing
        else can fold "the audit trail broke for THIS issue" into an ordinary transient error.

        The label write gates the rest, unlike `_offboard`'s fully-uniform sequence: `_offboard`'s
        comment ("Parked -- needs review") is true regardless of which earlier label edit landed,
        but THIS comment is itself a factual claim ("priority promoted X -> Y") -- posting it after
        a label write that never actually happened would assert a change that didn't occur, which
        is worse than the silence #900 already accepts as unavoidable for an ordinary failure. So a
        failed label write is reported once (`_note_scope`, matching every other board write's
        fail-open convention in this class) and nothing further is attempted for this blocker --
        byte-identical to this method's own pre-split behavior for that specific case. The field
        write, in contrast, carries no textual claim and stays independent of the comment either
        way -- its own failure is a cosmetic loss (`_mirror_priority`: "a column is a nicety"),
        never a reason to withhold the comment that a successful label write already made true."""
        canon = discovery.PRIORITIES[new_rank]
        try:
            self._write_priority_label(int(ref), canon, it.get("labels"))
        except Exception as exc:
            self._note_scope(exc)
            return
        try:
            item_id = self._item_id(int(ref))
            if item_id:
                self._write_priority_field(int(ref), new_rank, item_id)
        except Exception as exc:
            self._note_scope(exc)
        old_label = discovery.PRIORITIES[old_rank] if old_rank < discovery.UNPRIORITISED else "unprioritised"
        if cause:
            reason = "blocks #%s" % cause
            if self.blocker_promotion_mode == "smart":
                reason += ", which has no other unblocked %s work right now" % canon
        else:
            reason = "blocks other work in this backlog"    # defensive; min() guarantees a cause exists
        try:
            self.note(ref, "priority promoted %s -> %s: %s" % (old_label, canon, reason))
        except Exception as exc:
            self._warn_promotion_comment_failed(ref, old_label, canon, exc)

    def _warn_promotion_comment_failed(self, ref, old_label, canon, exc):
        """The one #900 outcome that must never look like an ordinary, silently-swallowed failure:
        the priority LABEL already changed (`_write_blocker_promotion` only ever reaches this point
        after that write succeeded) but the audit-trail COMMENT explaining why then failed to post.
        Two independent gh calls can't be made atomic, so this can't be prevented -- only made loud.
        Matches this file's own convention for a loud, specific note naming the issue
        (`_warn_blocking_cycle`, the #814 correction inside `_mirror_priority`); best-effort, never
        raises, and deliberately NOT routed through `_note_scope` (which stays silent for anything
        that isn't the missing-project-scope message shape -- exactly the genericness this exists
        to avoid)."""
        try:
            sys.stderr.write(
                "sigma: issue #%s's priority label was promoted %s -> %s, but the audit-trail "
                "comment explaining why FAILED to post (%s) -- the label changed WITHOUT its "
                "explanation. Leave a comment on #%s by hand so the change is not silently "
                "unexplained.\n" % (ref, old_label, canon, exc, ref))
        except Exception:
            pass

    def _warn_blocking_cycle(self, refs):
        """A cycle in the blocking graph (#A blocked by #B blocked by ... blocked by #A) is
        hand-authored data, so it should never happen -- but it must never hang the loop when it
        does either. Loud, matching this file's own established convention (`_note_scope`,
        `_warn_unseeded`, `_warn_truncated`) for every other automated degradation; best-effort,
        never raises."""
        try:
            sys.stderr.write(
                "sigma: a priority-promotion cycle was detected among issue(s) %s -- each was "
                "resolved by treating the cycle as if it stopped there, rather than looping "
                "forever. Fix the \"Blocked by\"/hand-off chain by hand.\n"
                % ", ".join("#" + r for r in sorted(refs, key=lambda x: (len(x), x))))
        except Exception:
            pass

    def _option_for_rank(self, rank):
        """A field option id for a P0-P4 `rank` when the field has no literal `P<n>` option of its
        own — an adopter running `priority_aliases` who wants to keep their OWN vocabulary
        (Critical/High/Medium/Low, say) as the field's only options, never adding P0-P4 at all.
        Returns the id of the first configured alias, matched case-insensitively, that both maps
        to this rank AND names a real option on this field — or None when no alias reaches here
        (unconfigured, or none of the aliases that map to this rank exist as an actual option)."""
        if not self.priority_aliases:
            return None
        lower_options = {name.lower(): opt_id for name, opt_id in self._priority_options.items()}
        for alias_text, mapped in self.priority_aliases.items():
            if discovery.priority_rank(mapped) == rank:
                opt_id = lower_options.get(str(alias_text).strip().lower())
                if opt_id:
                    return opt_id
        return None

    def _mirror_priority(self, n, labels, item_id, on_error=None, label_writes=None):
        """Keep a card's Priority column and its `priority:P*` label in agreement. The FIELD wins.

        Three cases, and only the first two write anything — a board already in agreement costs
        nothing, which is what makes this safe to run on every sync:

        * field RECOGNISED, label disagrees  -> the LABEL is corrected, always to the CANONICAL
          `priority:P<n>` spelling — never the field's own text, alias or not, so a label never
          carries anything but the one vocabulary every other reader (issue search, `handoff.py`,
          local-goals mode) already understands. The field is what the queue ranks by, so leaving
          the label stale would make all of those disagree with the order work actually runs in.
        * field BLANK (not just unrecognised — see below), label recognised -> the FIELD is filled,
          preferring a literal `P<n>` option and falling back to a configured alias' own option
          (`_option_for_rank`) when the field doesn't offer P0-P4 at all.
        * neither recognised, or they already agree -> nothing.

        "Recognised" runs the raw text through `discovery.priority_rank` WITH `self.priority_aliases`
        for the field side (so `Critical` counts as P0 once an adopter configures that), and without
        aliases for the label side (`_priority_name` already resolved any alias in the label itself,
        so its return value is always a canonical `P0`-`P4` string or None).

        This distinction — recognised vs merely non-blank — is the actual correctness fix, aliases
        configured or not: the field previously won on ANY non-blank text, so a stray value left over
        from before sigma managed the field (a typo, unrelated text, an adopter's own vocabulary
        nobody told sigma about yet) silently overwrote a genuinely correct label with something
        `priority_rank` then couldn't parse, sinking a real priority to UNPRIORITISED. An unrecognised
        field value is now treated exactly like a blank one would be for the "does the field object"
        question — but NOT filled from the label either (that's still gated on truly blank), since a
        human may have put that text there for a reason sigma just doesn't understand yet: no
        recognised priority on EITHER side, or the field's one is unrecognised, is always left alone
        rather than guessed at. Fail-open throughout — a column is a nicety, never a reason to fail a
        sync.

        #233 review: `label_writes` False (a Priority field that is not Sigma's: see
        `_priority_owned`) drops the first case entirely -- the LABEL is the only writer, so the loop
        never rewrites a label from the field, and never overwrites a recognised field value either
        (the sync's own exact-name field-wins pass would then fight it). Only a BLANK field is
        filled from the label. None -> `self._priority_label_writes`, set by the field's resolver."""
        if not (self._priority_field_id and self._priority_options and item_id):
            return
        if label_writes is None:
            label_writes = self._priority_label_writes
        field_value = self._item_priority.get(n)
        label_value = self._priority_name(labels)
        field_rank = discovery.priority_rank(field_value, self.priority_aliases) if field_value else discovery.UNPRIORITISED
        label_rank = discovery.priority_rank(label_value) if label_value else discovery.UNPRIORITISED
        try:
            if label_writes and field_rank < discovery.UNPRIORITISED and field_rank != label_rank:
                canon = discovery.PRIORITIES[field_rank]
                # #814: this correction used to be silent. field_value is only ever fresher than
                # label_value when the board has genuinely been the live interface since the label
                # was set — but there is no recency signal on either side to confirm that, so a
                # field left stale from BEFORE the board was in active use silently overwrites a
                # more-recently-updated label. Loud, matching this file's own established
                # convention for every other automated correction (_note_scope, _warn_unseeded).
                sys.stderr.write(
                    "sigma: issue #%s's priority label (%s) disagreed with its board field "
                    "(%s) — the field wins, correcting the label to match. If the LABEL was set "
                    "more recently than the board field, this is backwards; fix it on the board.\n"
                    % (n, label_value or "none", field_value))
                self._write_priority_label(n, canon, labels)
            elif not field_value and label_rank < discovery.UNPRIORITISED:
                self._write_priority_field(n, label_rank, item_id)
        except Exception as exc:
            (on_error or self._note_scope)(exc)   # #233's boundary path passes its one-warning sink

    def _write_priority_label(self, n, canon, labels):
        """Set issue `n`'s priority label to the canonical `priority:P<n>` spelling for `canon` —
        never the field's own text, alias or not, so a label never carries anything but the one
        vocabulary every other reader (issue search, `handoff.py`, local-goals mode) understands.
        Also removes every OTHER priority-prefixed label the issue carries: an issue can hold
        several (#381 on this repo carries both P0 and P1), and leaving a stray behind would
        re-create a disagreement on the very next pass.

        THE one place an issue's priority label is ever written. Extracted from `_mirror_priority`
        (field wins a field/label disagreement) so `_promote_blockers` (#900) can write through the
        exact same mechanism instead of a second, parallel one — never a behavior change for
        `_mirror_priority` itself, which now just calls straight through to this."""
        # #895 slice 3a: REST, SEQUENTIAL and still NOT atomic. The add goes FIRST, then one DELETE per
        # stale priority label (it was four unordered parallel requests inside one `gh issue edit`). A
        # partial failure leaves two priority labels, visible. Self-heal is NARROW (`_priority_name`
        # takes the MIN rank): if the new label is LOWER priority than the stale one (a demotion) the
        # stale label still wins, the disagreement persists and the next pass retries the remove; if it
        # OUTRANKS the stale one (a promotion) and the remove fails, the effective priority is already
        # right, no disagreement is detected and the stale lower label is never removed by the loop.
        # `PUT /labels` is deliberately NOT used (replace-set lost update). A `priority:P*` label that is
        # missing from the repo is MINTED by REST (documented GitHub behaviour, UNMEASURED here).
        stale = [name for name in [(l.get("name") if isinstance(l, dict) else str(l or ""))
                                   for l in (labels or [])]
                 if name.startswith(self.priority_prefix) and name != self.priority_prefix + canon]
        self._issue_add_labels(n, [self.priority_prefix + canon])
        for name in stale:
            self._issue_remove_label(n, name)

    def _write_priority_field(self, n, rank, item_id):
        """Set the board's Priority field for issue `n`'s card to `rank`'s canonical `P<n>` option,
        preferring a literal option and falling back to a configured alias' own option
        (`_option_for_rank`) when the field doesn't offer P0-P4 at all. No-op — including when the
        field isn't provisioned at all — rather than a guess or a raise.

        THE one place the Priority field is ever written. Extracted from `_mirror_priority` (label
        wins a field/label disagreement), guarded independently here (unlike `_mirror_priority`'s
        own three-part top guard, which this duplicates defensively): `_promote_blockers` (#900)
        calls this directly, without first passing through `_mirror_priority`'s own guard, so this
        has to be safe to call standalone.

        #1206: also a no-op — loud, not silent — when issue `n`'s own card is currently sitting at
        Done. `n` is open by construction here (both callers only ever reach this over the open,
        goal-labelled corpus `_sync_backlog` fetched), so a Done card at this point is a STRANDED
        one: GitHub's built-in workflows move a card TO Done in exactly one direction (closing an
        issue fires "Item closed") and there is no "Item reopened" workflow to move it back, so
        reopening the issue leaves the card sitting at Done for the rest of its life unless a human
        resets it. Writing the Priority field there would be silent: nothing about a Priority edit
        signals "this card is stuck", and every open-issue metric looks fine right up until someone
        actually opens the board. Skip rather than reset the Status here — the reset itself is a
        judgement call (`/sigma-doctor`'s sibling check names the card for a human instead) — and say
        so on stderr every time, matching this file's own loud-correction convention (`_mirror_priority`
        #814) rather than swallowing it. `self._item_status` is the same pre-write-this-pass snapshot
        `_promote_blockers`' own "other unblocked work" check already reads (populated by
        `_load_items`, refreshed in-place by `_sync_backlog`'s own seed step) — never a fresh read
        just for this guard. Checked AFTER the field-provisioned guard below (not before): with no
        Priority field on the board at all, this would already be a no-op, and warning about a
        write that was never going to happen on every stranded card, every sync, on a board that
        doesn't even have the column, is pure noise."""
        if not (self._priority_field_id and self._priority_options and item_id):
            return
        canon = discovery.PRIORITIES[rank]
        option = self._priority_options.get(canon) or self._option_for_rank(rank)
        if not option:
            return              # the board's Priority field offers neither P<n> nor an alias for it
        # #233 review: checked AFTER the option is resolved -- a card that was never going to be
        # written (no matching option) is not reported as skipped.
        if self._item_status.get(int(n)) == self.col["done"]:
            sys.stderr.write(
                "sigma: issue #%s is open but its board card is stuck at %s -- GitHub has no "
                "\"Item reopened\" workflow to move a card back once the issue reopens, so it stays "
                "there until a human resets it. Skipping the Priority field write on this card; "
                "run /sigma-doctor to see it flagged.\n" % (n, self.col["done"]))
            return
        self._run(["project", "item-edit", "--project-id", self._project_id, "--id", item_id,
                   "--field-id", self._priority_field_id, "--single-select-option-id", option])
        self._item_priority[n] = canon

    def _item_id(self, n):
        """Board item id for issue `n`, adding the issue to the board if it isn't there yet (cached)."""
        n = int(n)
        if self._items is None:
            self._load_items(self._proj_owner(), self._project_number)
        if n in self._items:
            return self._items[n]
        if self.repo:
            # #308 review: resolve the configured name before an add even when no card on the board
            # named the repo yet (cached), so a stale config still adds under the CURRENT name.
            self._resolved_repo()
        data = self._gh_json(["project", "item-add", str(self._project_number), "--owner", self._proj_owner(),
                              "--url", self._issue_url(n), "--format", "json"])
        iid = data.get("id")
        if iid:
            self._items[n] = iid
        return iid

    def _warn_truncated(self, what, got):
        """A read that came back exactly at the ceiling MAY have lost rows — say so. Silence here is
        what made #692 invisible for so long: a truncated board read looks identical to a small one.
        Best-effort and never raises: a warning that breaks the loop is worse than the bug."""
        if got < self._BOARD_ITEM_LIMIT:
            return
        try:
            sys.stderr.write(
                "sigma: read %d %s, which is the --limit ceiling (%d) — the result may be "
                "TRUNCATED and cards past it will be treated as absent. Raise "
                "GitHubSource._BOARD_ITEM_LIMIT, or archive completed cards to shrink the board.\n"
                % (got, what, self._BOARD_ITEM_LIMIT))
        except Exception:
            pass

    def _load_items(self, owner, number):
        self._items = {}
        self._item_status = {}          # #693: sync's promote guard needs each card's CURRENT column
        self._item_priority = {}        # #719: and the mirror needs its CURRENT priority value
        data = self._gh_json(["project", "item-list", str(number), "--owner", owner,
                              "--format", "json", "--limit", str(self._BOARD_ITEM_LIMIT)])
        items = (data.get("items") if isinstance(data, dict) else data) or []
        self._warn_truncated("board items", len(items))
        mine = self.repo.casefold()
        for it in items:
            n = (it.get("content") or {}).get("number")
            # #233 review: a board may carry cards from SEVERAL repos, and issue numbers are per
            # repo -- `acme/other#11` must never be mistaken for our #11 and have its card moved.
            # A row that names no repository (older `gh`, or a fake) is kept, as before.
            # #308: a card names the repo's CURRENT name, so after a rename/transfer with a stale
            # `discovery.github.repo` every card used to read as foreign; the resolved name counts.
            where = str((it.get("content") or {}).get("repository") or "").casefold()
            if mine and where and where != mine and where != self._resolved_repo():
                continue
            if n is not None:
                self._items[int(n)] = it.get("id")
                self._item_status[int(n)] = it.get("status")
                # `gh project item-list` flattens a custom single-select onto the item under the
                # lowercased field name — verified live on project #6: {'status': 'Backlog',
                # 'priority': 'P3'}. Caching it here is what makes the mirror diff-only, so a board
                # already in agreement costs zero writes.
                self._item_priority[int(n)] = it.get((self.priority_field or "").lower())

    #: #308 review: how long a FAILED resolve of the repo's current name is trusted before the next
    #: need reads again. A long-lived source (the watch daemon's) must not keep one blip forever;
    #: within the window a failure is cached, so an outage costs one read per window, not per card.
    _REPO_RETRY_S = 300

    def _resolved_repo(self):
        """#308: `discovery.github.repo` as GitHub resolves it NOW (casefolded `full_name`) -- the
        same repository under its current name after a rename or transfer, which GitHub redirects
        (REST `repos/<old>` answers with the new `full_name`; measured read-only in
        the evidence recorded on #308). One `gh api repos/<repo>` read, made when a card names a repository
        other than the configured one, or before the first `item-add`. A success is cached for the
        instance; a failure only for `_REPO_RETRY_S`. Unreadable -> "" (matches no card): the strict
        #233 rule, so another repo's card is still never written."""
        if self._repo_resolved == "" and self._repo_failed_at is not None and \
                time.monotonic() - self._repo_failed_at >= self._REPO_RETRY_S:
            self._repo_resolved = None
        if self._repo_resolved is None:
            self._repo_resolved, self._repo_failed_at = "", None
            try:
                data = self._gh_json(["api", "repos/%s" % self.repo])
                full = data.get("full_name") if isinstance(data, dict) else None
                if isinstance(full, str) and full.count("/") == 1:
                    self._repo_resolved = full.casefold()
                    self._repo_current = full
            except Exception:
                pass
            if not self._repo_resolved:
                self._repo_failed_at = time.monotonic()
        return self._repo_resolved

    def _list_fields(self, owner, number):
        data = self._gh_json(["project", "field-list", str(number), "--owner", owner,
                              "--format", "json", "--limit", "100"])
        return (data.get("fields") if isinstance(data, dict) else data) or []

    @staticmethod
    def _find_field(fields, name):
        return next((f for f in fields if f.get("name") == name), None)

    # ----- #233: Phase (and Priority) at a phase boundary -----------------------------------------

    #: The one-card read's page sizes. An issue on more than `_CARD_BOARDS` boards may not see the
    #: pinned one (it then reads as uncarded, with the one warning); GitHub caps a project at 50
    #: fields, so `_CARD_FIELDS` reads them all. Neither depends on how many cards the board has.
    _CARD_BOARDS = 50
    _CARD_FIELDS = 50

    def _pinned_number(self):
        try:
            value = int(self._project_cfg.get("number"))
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def _priority_owned(self):
        """#233 review: is this board's Priority field SIGMA'S? True for a board `_ensure_board`
        created in this run, the board `board_setup.py` created (`project.setup_created` naming
        the pinned number AND owner, `setup_marker`), or the explicit opt-in `project.mirror_priority: true`. Only then may
        the loop create the field, and only then does #719's field-wins rule rewrite labels from it
        on #233's paths. Anyone else's board keeps the label as the one writer."""
        if self.mirror_priority or self._created_board_now:
            return True
        made = self.setup_marker(self._project_cfg.get("setup_created"))
        if made is None:
            return False
        # #233 review (block #2): the marker names a board by number AND owner -- a hand-made board
        # of another owner that happens to reuse the number is not the one board_setup created.
        return made == ((self._project_number or self._pinned_number()),
                        str(self._proj_owner() or "").casefold())

    @staticmethod
    def setup_marker(value):
        """`project.setup_created` -> (number, casefolded owner), or None when it names no board
        this code can vouch for. THE one parse, shared with `board_setup.py` (its writer). Only the
        `{"number": N, "owner": "<login>"}` form counts; the older bare-number form (number only,
        so it cannot tell two owners' boards apart) reads as NOT ours -- the safe direction: the
        label stays the one Priority writer. A re-pin drops it, never upgrades it: the owner beside
        it cannot vouch for it (#308 review); `mirror_priority: true` is the lever."""
        if not isinstance(value, dict):
            return None
        owner = value.get("owner")
        try:
            number = int(value.get("number"))
        except (TypeError, ValueError):
            return None
        if isinstance(value.get("number"), bool) or number <= 0 \
                or not isinstance(owner, str) or not owner.strip() or owner.startswith("@"):
            return None
        return number, owner.strip().casefold()

    @staticmethod
    def _match_field(names, name):
        """THE field-name rule, shared by the sync (`_ensure_priority_field`) and the phase path:
        -> (the board's name to use or None, [every case-variant of `name` when that is ambiguous]).
        The exact name wins; else a SINGLE case-only variant is adopted under its own spelling (never
        renamed, never shadowed by a duplicate); several case-only variants and no exact one is
        ambiguous and adopts nothing. An exact name with case-variants beside it is also reported
        (second element non-empty) so a caller whose reads flatten by lowercased name can refuse."""
        if not name:
            return None, []
        names = [str(n) for n in names if n is not None]
        variants = [n for n in names if n.casefold() == str(name).casefold()]
        if name in names:
            return name, (variants if len(variants) > 1 else [])
        if len(variants) == 1:
            return variants[0], []
        return None, variants

    @staticmethod
    def _single_select(field):
        return field is not None and (field.get("options") is not None
                                      or field.get("type") == "ProjectV2SingleSelectField"
                                      or field.get("dataType") == "SINGLE_SELECT")

    @staticmethod
    def _create_field_mutation(project_id, name, options):
        """`createProjectV2Field` for a single-select with fixed `options` [(name, colour)], every
        literal JSON-quoted exactly as `_options_mutation` quotes them (#235's escaping). It asks for
        the new field's options back, so a create costs no re-read."""
        q = lambda v: json.dumps(str(v))        # noqa: E731 - a GraphQL string literal
        opts = ", ".join("{name: %s, color: %s, description: %s}" % (q(n), c, q(""))
                         for n, c in options)
        return ("query=mutation { createProjectV2Field(input: {projectId: %s, dataType: SINGLE_SELECT, "
                "name: %s, singleSelectOptions: [%s]}) { projectV2Field { ... on "
                "ProjectV2SingleSelectField { id name options { id name } } } } }"
                % (q(project_id), q(name), opts))

    def _card_query(self, owner, name, n):
        q = lambda v: json.dumps(str(v))        # noqa: E731 - a GraphQL string literal
        # #233 review (block #2): `viewer { login }` rides the SAME read, so a `project.owner` of
        # `@me` is resolved to a login and compared like any other owner -- never a wildcard.
        return ("query=query { viewer { login } repository(owner: %s, name: %s) { issue(number: %d) { "
                "labels(first: 100) { nodes { name } } "
                "projectItems(first: %d, includeArchived: false) { nodes { id "
                "project { id number owner { ... on Organization { login } ... on User { login } } "
                "fields(first: %d) { nodes { ... on ProjectV2FieldCommon { id name dataType } "
                "... on ProjectV2SingleSelectField { options { id name } } } } } "
                "fieldValues(first: %d) { nodes { ... on ProjectV2ItemFieldSingleSelectValue { "
                "name optionId field { ... on ProjectV2FieldCommon { id name } } } } } } } } } }"
                % (q(owner), q(name), int(n), self._CARD_BOARDS, self._CARD_FIELDS,
                   self._CARD_FIELDS))

    def _read_card(self, n, number):
        """ONE GraphQL read: issue `n` of THIS repo, its labels, and its card on the pinned board
        `number` (with that board's fields and the card's single-select values keyed by FIELD ID).
        -> dict, or None when the issue has no card on that board. Scoped by repository, so a
        multi-repo board's same-numbered card from another repo can never be the one returned; and
        by board number + owner, so a board that merely matches the title never stands in for an
        unreadable pin. Cost is independent of the board's card count (research/233: `item-list
        --limit 5000` was 6.5s on 246 cards; this read measured 0.66-0.72s on the same board)."""
        owner, name = self._owner_name()
        if not (owner and name):
            raise RuntimeError("no repository to read issue #%d from (set discovery.github.repo)" % n)
        data = self._gh_json(["api", "graphql", "-f", self._card_query(owner, name, n)])
        issue = (((data.get("data") or {}).get("repository") or {}).get("issue")) \
            if isinstance(data, dict) else None
        if not issue:
            raise RuntimeError("issue #%d was not found in %s/%s" % (n, owner, name))
        want_owner = str(self._proj_owner() or "").strip()
        if not want_owner or want_owner.startswith("@"):
            # #233 review (block #2): `@me` (board_migrate --owner @me, or `_proj_owner`'s own
            # fallback) used to SKIP the owner check, so the first card on ANY board numbered
            # `number` -- an org's #5 as readily as the user's own #5 -- was written. It is the
            # viewer's login, read in this same query; unresolved, nothing is written.
            viewer = ((data.get("data") or {}).get("viewer") or {}).get("login")
            if not (isinstance(viewer, str) and viewer.strip()):
                raise RuntimeError("could not resolve project.owner %r to a login, so board #%d "
                                   "cannot be told from another owner's board of that number"
                                   % (want_owner or "@me", number))
            want_owner = viewer.strip()
        for node in ((issue.get("projectItems") or {}).get("nodes") or []):
            proj = (node or {}).get("project") or {}
            login = str((proj.get("owner") or {}).get("login") or "")
            if proj.get("number") != number or not proj.get("id") or not node.get("id"):
                continue
            if login.casefold() != want_owner.casefold():
                continue
            fields = [f for f in ((proj.get("fields") or {}).get("nodes") or []) if f and f.get("id")]
            values = {}
            for v in ((node.get("fieldValues") or {}).get("nodes") or []):
                fid = ((v or {}).get("field") or {}).get("id")
                if fid:
                    values[fid] = v.get("name")
            labels = [lb.get("name") for lb in ((issue.get("labels") or {}).get("nodes") or [])
                      if isinstance(lb, dict)]
            return {"project_id": proj["id"], "item_id": node["id"], "fields": fields,
                    "values": values, "labels": labels}
        return None

    def _warn_field(self, what, written=False):
        """THE one warning a run prints about Phase/Priority (#233), whatever else goes wrong after
        it: the board is a mirror, so one line saying it is behind is the whole useful signal.
        `written`: a notice about a board shape, not a failed write -- it must not claim one."""
        if self._field_warned:
            return
        self._field_warned = True
        hint = (" Run: gh auth refresh -s project." if "scope" in what.lower() else "")
        lead = "board Phase/Priority note" if written else "board Phase/Priority not written"
        try:
            sys.stderr.write("sigma: %s - %s. The goal continues unaffected; issue labels remain "
                             "the source of truth.%s\n" % (lead, what, hint))
        except Exception:
            pass

    def _resolve(self, fields, name):
        """`_match_field` over a field list -> the field dict, or None (warning once when the name is
        ambiguous between case-variants: which one a human means cannot be guessed)."""
        chosen, clash = self._match_field([f.get("name") for f in fields], name)
        if chosen is None and clash:
            self._warn_field("the board has several %r fields differing only in case (%s) and none "
                             "named exactly %r" % (name, ", ".join(map(repr, clash)), name))
            return None
        if clash:
            self._warn_field("the board has several %r fields differing only in case (%s); only "
                             "the exact %r is used" % (name, ", ".join(map(repr, clash)), name),
                             written=True)
        return next((f for f in fields if f.get("name") == chosen), None) if chosen else None

    def set_board_phase(self, goal, token, vocabulary):
        """#233: set issue `goal`'s card Phase to `token`, and mirror its Priority from its labels.
        Called once per phase boundary by `phase_report.py start`; returns True only when the Phase
        value on the card is now `token`.

        `vocabulary` is [(token, colour)] from `phase_report.PHASE_TOKENS` -- passed in, never copied
        here, so the board's options and the output contract cannot say different things.

        Gated: a no-op with ZERO gh calls unless `project.enabled` and a `project.number` is pinned
        (the operator's explicit choice of board), and `goal` is an issue number. Light on purpose:
        ONE read (`_read_card`: this issue's card on the pinned board, by repository and number) and
        at most one write per field; a card whose Phase already reads `token` costs no write at all.
        No repo link, no backlog sync, no board creation, no whole-board item list.

        Phase is Sigma's own column: absent, it is created ONCE (single-select, fixed options) on the
        pinned board. Priority is created only where the field is Sigma's (`_priority_owned`); on
        anyone else's board it is mirrored label -> field into an existing column only, and a blank
        one only, and a label is never rewritten from it. A field is matched by `_match_field`
        (exact, else one case-only variant); its options exactly, then by case, and a missing one is
        NEVER appended (the loop cannot read option colours, and #235's rule is to refuse rather than
        reset them). Nothing is renamed, recoloured, reordered or dropped. Status is never written
        and an uncarded goal is never carded here. Fail-open: never raises, and at most ONE stderr
        line per run (`_warn_field`)."""
        number = self._pinned_number()
        if not (self.project_enabled and number) or not str(goal).isdigit():
            return False
        n = int(goal)
        try:
            card = self._read_card(n, number)
            if card is None:
                self._warn_field("issue #%d has no card on board #%d yet (or the board is not "
                                 "readable)" % (n, number))
                return False
            self._project_number, self._project_id = number, card["project_id"]
            fields, item_id = card["fields"], card["item_id"]
            wanted = []
            if self.priority_field and self._priority_owned():
                wanted.append((self.priority_field, [(p, "GRAY") for p in discovery.PRIORITIES]))
            if self.phase_field:
                wanted.append((self.phase_field, list(vocabulary)))
            for name, options in wanted:
                if self._match_field([f.get("name") for f in fields], name) != (None, []):
                    continue                      # present, or ambiguous (`_resolve` warns below)
                try:
                    made = self._gh_json(["api", "graphql", "-f",
                                          self._create_field_mutation(card["project_id"], name,
                                                                      options)])
                    fld = (((made.get("data") or {}).get("createProjectV2Field") or {})
                           .get("projectV2Field")) if isinstance(made, dict) else None
                    if fld and fld.get("id"):
                        fields.append(dict(fld, dataType="SINGLE_SELECT"))
                except Exception as exc:
                    # #308: two first phase starts race to create the field; GitHub refuses the
                    # second ("Name has already been taken"). Whatever the refusal says, re-read the
                    # card ONCE: if the field is there now, the winner made it -- adopt it and go
                    # on. Only a field still absent is a failure worth the one warning.
                    again = self._read_card(n, number)
                    if again is not None and again["item_id"] == item_id and self._match_field(
                            [f.get("name") for f in again["fields"]], name) != (None, []):
                        card, fields = again, again["fields"]
                        continue
                    self._warn_field("could not create the %r field (%s)"
                                     % (name, getattr(exc, "hint", None) or exc))
            self._mirror_board_priority(n, card, fields, item_id)
            return self._write_board_phase(card, fields, item_id, token) if self.phase_field \
                else False
        except Exception as exc:
            self._warn_field(str(getattr(exc, "hint", None) or exc))
            return False

    def _mirror_board_priority(self, n, card, fields, item_id):
        if not self.priority_field:
            return
        fld = self._resolve(fields, self.priority_field)
        if fld is None:
            return                     # not Sigma's to create, its create already warned, or ambiguous
        if not self._single_select(fld):
            self._warn_field("the board's %r field is not single-select" % fld.get("name"))
            return
        status = self._resolve_quiet(fields, self._project_cfg.get("status_field") or "Status")
        self._item_status[n] = card["values"].get((status or {}).get("id"))   # #1206's Done guard
        self._priority_field_id = fld.get("id")
        self._priority_options = {o.get("name"): o.get("id") for o in (fld.get("options") or [])}
        self._item_priority[n] = card["values"].get(fld.get("id"))
        self._mirror_priority(n, card["labels"], item_id,
                              label_writes=self._priority_owned(),
                              on_error=lambda exc: self._warn_field(
                                  "the Priority write failed (%s)" % (getattr(exc, "hint", None) or exc)))

    def _resolve_quiet(self, fields, name):
        chosen, _clash = self._match_field([f.get("name") for f in fields], name)
        return next((f for f in fields if f.get("name") == chosen), None) if chosen else None

    def _write_board_phase(self, card, fields, item_id, token):
        fld = self._resolve(fields, self.phase_field)
        if fld is None:
            return False                 # its create already warned, or ambiguous (warned)
        if not self._single_select(fld):
            self._warn_field("the board's %r field is not single-select" % fld.get("name"))
            return False
        opts = [o for o in (fld.get("options") or []) if isinstance(o, dict)]
        opt = next((o for o in opts if o.get("name") == token), None) or next(
            (o for o in opts if str(o.get("name") or "").casefold() == token.casefold()), None)
        if opt is None:
            self._warn_field("the %r field has no %r option (add it on the board by hand; the "
                             "loop never edits a field's options)" % (fld.get("name"), token))
            return False
        if card["values"].get(fld.get("id")) == opt.get("name"):
            return True                              # already there: dedupe, zero writes
        try:
            self._run(["project", "item-edit", "--project-id", self._project_id, "--id", item_id,
                       "--field-id", fld.get("id"), "--single-select-option-id", opt.get("id")])
        except Exception as exc:
            self._warn_field("the Phase write failed (%s)" % (getattr(exc, "hint", None) or exc))
            return False
        return True

    def _issue_url(self, n):
        # #308: an issue URL under a repo's OLD name does not resolve (`resource(url:)` is null,
        # measured), so once the current name is known it is the one used.
        repo = self._repo_current or self.repo
        if not repo:
            try:
                repo = (self._gh_json(["repo", "view", "--json", "nameWithOwner"]) or {}).get("nameWithOwner", "")
            except Exception:
                repo = ""
        return f"https://github.com/{repo}/issues/{n}"

    def _gh_json(self, args):
        return json.loads(self._run(args) or "{}")


class FeatureScopeError(ValueError):
    """`--feature` was handed something this run cannot honour: a name that could never be a unit,
    or a backlog whose goals have no way of declaring one.

    Its own type, not a bare ValueError, so the CLI can turn it into one clear line and an exit
    code rather than a traceback -- the same shape `state.ConfigMissing` already has in `loop.main`.
    """


def scope_to_feature(source, unit):
    """Confine `source` to ONE unit of work for the rest of this run. -> the unit name, or None.

    THE ONE ENTRY POINT. `GitHubSource.scope_to_feature` is the mechanism; this is where the two
    ways of asking for something impossible are refused, UP FRONT, before a single query is made.

    THE NAME IS CHECKED BY `features._is_unit_name`, NOT BY A SECOND COPY OF THE RULE. That
    predicate is the one `features.parse_body` and `features.parse_labels` already apply, and it is
    what `feature_registry.is_unit_name` re-exports for the same reason: a unit name becomes BOTH a
    `feature:<name>` label and a `feature/<name>` branch segment, so it has to be a name git accepts
    (measured against `git check-ref-format` in `features.py`'s own rule 4 -- no `..`, no trailing
    `.`, no `.lock` suffix, no `/`). A local copy here would be free to drift, and a name this
    accepted but the parser rejected would scope a run to a label no issue can ever carry.

    REFUSED, NOT SILENTLY MATCHED AGAINST NOTHING. `gh issue list --label <a label the repo does not
    have>` returns an empty list rather than an error (measured), so every failure of this kind
    looks exactly like a drained unit. A typo would therefore report "your unit is empty" and stop,
    which is the single most misleading thing this feature could do.

    A SOURCE WITH NO UNIT TO SCOPE TO IS REFUSED FOR THE SAME REASON. `LocalSource`'s goals are
    FILES: they carry no labels and there is no `feature:` namespace for them to sit in, so a
    `--feature` run against one would match zero goals forever. Duck-typed on the method rather than
    on the class, so a future source that CAN express units needs no change here."""
    if unit is None:
        return None
    unit = str(unit)
    if not feature_labels.features._is_unit_name(unit):
        raise FeatureScopeError(
            "--feature %r is not a unit name. A unit becomes both a %s<name> label and a "
            "%s<name> branch segment, so it has to be a name git accepts as one branch segment: "
            "no spaces, no '/', no '..', and not ending in '.' or '.lock'. Nothing was queried."
            % (unit, feature_labels.features.LABEL_PREFIX, feature_labels.features.BRANCH_PREFIX))
    scope = getattr(source, "scope_to_feature", None)
    if not callable(scope):
        raise FeatureScopeError(
            "--feature needs a backlog whose goals can declare a unit, and this project's "
            "discovery.source is 'local-goals' -- a goal there is a markdown file and carries no "
            "%s<name> label, so the scope would match nothing at all rather than confine anything. "
            "Nothing was queried." % feature_labels.features.LABEL_PREFIX)
    scope(unit)
    return unit


def get_source(sdlc_dir, config):
    """Factory: pick the backlog source from config.discovery.source (default 'local-goals')."""
    source = ((config.get("discovery") or {}).get("source")) or "local-goals"
    if source == "github":
        # #900: forward sdlc_dir (already this function's own parameter) into GitHubSource too —
        # it was only ever threaded to LocalSource before. GitHubSource needs it for the ledger
        # half of blocker-priority-promotion's reverse lookup; see its __init__ docstring.
        return GitHubSource(config, sdlc_dir=sdlc_dir)
    return LocalSource(sdlc_dir, config)
