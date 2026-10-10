#!/usr/bin/env python3
"""Cross-area hand-off: park WITH a successor instead of parking into silence.

The loop already parks correctly when it hits something it must not decide alone. But a very common
blocker is not a decision at all — it is a dependency in code someone else owns. Parking that one
tells nobody: the queue entry is local and gitignored, the issue comment is unaddressed, and no code
path in the kit has ever set an assignee. The work stalls until a human happens to notice.

A hand-off closes that. It resolves the owner from the repo's own CODEOWNERS, opens an issue in their
area carrying the dependency, assigns it to them, records the fact in the team ledger addressed to
them, and links it from the blocked issue. Then the goal parks as before and the loop moves on.

The routing then happens by itself: the new issue carries the GOAL label and an assignee, so the
owner's own loop picks it up through the `discovery.github.assignee` filter. No new transport, no
daemon — the backlog everyone already shares does the delivery.

Every step degrades honestly. No owner in CODEOWNERS, no `gh`, or a local backlog: the ledger entry
is still written, so the team can still see what is blocked on whom. Zero deps.

GENERALIZED (issue #462): a formal cross-area hand-off was never the only issue Sigma itself
opens — a same-area follow-up finding (a review comment, a mid-goal discovery) is just as real, and
until now had no disciplined path at all: the agent filed it by hand via a bare `gh issue create`,
with no label and no assignee, easy to lose in a long session. `create_tracked_issue()` below is the
one real place the kit ever opens an issue on its own behalf; `hand_off()` is now a thin, behavior-
preserving wrapper around it — a formal hand-off is exactly its
`same_area=False, immediately_actionable=True, blocks_goal=True` special case. The `track` CLI verb
is the same-area/non-blocking sibling of `open`, for a finding that doesn't need a human decision.
"""
import hashlib
import importlib.util
import pathlib
import re
import sys

_HERE = pathlib.Path(__file__).resolve().parent
#: #1204: dedup.py lives in the SIBLING skill sigma-scope, not this one -- the same cross-skill,
#: load-by-file-path pattern backlog_check.py already uses for sigma-velocity's velocity.py
#: (`_load_velocity()`). `tests/test_import_boundary.py` bans only PACKAGE-level `import`
#: statements between skills/hooks and the private side, and explicitly blesses this exact
#: spec_from_file_location sibling-loading pattern for skills/hooks internally.
_DEDUP_PATH = _HERE.parent.parent / "sigma-scope" / "scripts" / "dedup.py"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _load_path(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ledger = _load("ledger")
owners = _load("owners")
sources = _load("sources")
mirror = _load("mirror")                    # #1204: mirror.fetch_dependency_records's own corpus
dedup = _load_path(_DEDUP_PATH, "dedup")     # #1204: the reused duplicate-search engine (#917)
feature_stamp = _load("feature_stamp")       # #1471: the unit an issue Sigma files inherits

DEFAULT_PRIORITY = "P1"
DEPENDENCY_LABEL = "sdlc:dependency"
#: #233: the DISTINCT label every queued (immediately_actionable=False) tracked issue carries. It is
#: what makes the loop-proposed-but-unpromoted set QUERYABLE — before this, such an issue was only
#: identifiable by what it was MISSING (`sdlc:goal`), so nothing could surface the pending count and
#: it grew unnoticed six times in one run (#224–#232). Orthogonal to `sdlc:goal`: it never causes a
#: pick (`next_pending` filters on `sdlc:goal` alone), so the deliberate-promotion design is intact.
PROPOSED_LABEL = "sdlc:needs-confirmation"
#: #1347: guaranteed on EVERY issue create_tracked_issue files — this is the one place the kit
#: ever opens an issue on its own behalf, so this is the one place that can make "this originated
#: as an AI-discovered finding" a reliable, always-present signal rather than an optional
#: `--label` a caller has to remember to type (SKILL.md previously showed it only as an example).
FOLLOWUP_LABEL = "sdlc:followup"


def _settings(config):
    return (ledger.settings(config).get("handoff") or {})


def dependency_label(config):
    return _settings(config).get("label") or DEPENDENCY_LABEL


def proposed_label(config):
    return _settings(config).get("proposed_label") or PROPOSED_LABEL


def project_root(sdlc_dir):
    return pathlib.Path(sdlc_dir).resolve().parent


def _resolve_reused_blocker(sdlc_dir, config, source, goal, issue, run=None):
    """Make a REUSED duplicate actually workable before the current goal is blocked behind it.

    Returns a list of warnings (possibly empty). Never raises and never blocks the filing: this is
    a repair on a path that already succeeded, so any failure degrades to the pre-#1393 behaviour
    (the goal blocks behind whatever the duplicate is) with the reason said out loud.

    Deliberately delegates to `blockers.resolve` rather than re-deciding: there is exactly one
    policy for "may Sigma grant this issue membership?", and it lives there."""
    try:
        blockers = _load_sibling("blockers")
        result = blockers.resolve(sdlc_dir, config, source, str(goal), [str(issue)], run=run)
        out = []
        for r in result["results"]:
            if r.get("acted"):
                out.append(f"reused blocker #{r['ref']} was not pickable — {r['detail']}")
            elif r["ref"] in result["surfaced"]:
                out.append(f"this goal will wait on #{r['ref']}, which nothing can pick right now: "
                           f"{r['detail']}")
        return out
    except Exception as exc:                            # noqa: BLE001 - never break a filing
        return [f"could not check whether the reused blocker #{issue} is workable: {exc}"]


def _load_sibling(name):
    """`blockers` is loaded lazily and by path, not at module import: `blockers` itself imports
    `handoff` (for the provenance labels), so a top-level import here would be a cycle."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


#: #1479: `feature_owner` is loaded on FIRST FILING THAT DECLARES A UNIT, not at import. Lazy for
#: `loop._feature_propagate`'s reason rather than for style: it pulls `feature_registry`,
#: `feature_sync`, `feature_doc`, `features` and `ledger` behind it, and every OTHER filing --
#: including every filing in a project that never adopted the branching model -- would pay for a
#: chain it cannot reach. The accessor exists so a test can substitute it.
_FEATURE_OWNER = None


def _feature_owner():
    global _FEATURE_OWNER
    if _FEATURE_OWNER is None:
        _FEATURE_OWNER = _load_sibling("feature_owner")
    return _FEATURE_OWNER


#: #1820: `feature_registry` is loaded on FIRST USE OF `target_unit`, not at import -- the same
#: lazy shape and the same reason as `_FEATURE_OWNER` above. `feature_stamp` (loaded eagerly, at
#: module scope) is reached by EVERY filing regardless of unit adoption; `feature_registry` is
#: reached only on the uncommon path where a caller explicitly names a unit to target, so an eager
#: import here would tax every ordinary filing for a chain most of them never touch -- exactly what
#: `_feature_owner`'s own docstring names `feature_registry` as one of the modules its own lazy
#: pattern exists to shield callers from. It also re-`_load`s its own private copy of `features.py`
#: (no `sys.modules` caching in `_load`), so an eager import here would pay a second parse+exec of
#: `features.py` on every `handoff.py` import, not just the ones that use it.
_FEATURE_REGISTRY = None


def _feature_registry():
    global _FEATURE_REGISTRY
    if _FEATURE_REGISTRY is None:
        _FEATURE_REGISTRY = _load_sibling("feature_registry")
    return _FEATURE_REGISTRY


#: #2363: `feature_classify` is loaded lazily, on first use of automatic classification, not at
#: import -- the same lazy shape and the same reason as `_FEATURE_REGISTRY` immediately above: it
#: is reached only on the uncommon path where a filing declares no unit and no `--target-unit` was
#: given, so an eager import here would tax every ordinary filing for a chain most of them never
#: touch.
_FEATURE_CLASSIFY = None


def _feature_classify():
    global _FEATURE_CLASSIFY
    if _FEATURE_CLASSIFY is None:
        _FEATURE_CLASSIFY = _load_sibling("feature_classify")
    return _FEATURE_CLASSIFY


#: #2380: `feature_judge` is loaded lazily too, matching `_FEATURE_CLASSIFY` immediately above --
#: reached only on the uncommon path where a filing is eligible for automatic classification AND
#: `no_dangling_goal_live_judge_enabled` is on, so an eager load here would tax every ordinary
#: filing (including every filing on a repo that never turned this on) for a chain it never
#: reaches. See `feature_judge.py`'s own module docstring for why an eager load of it at THIS
#: module's own scope would also be unsafe (it eagerly loads `feature_classify`, which eagerly
#: loads `feature_labels`, which loads `feature_judge` back lazily only -- never at module scope).
_FEATURE_JUDGE = None


def _feature_judge():
    global _FEATURE_JUDGE
    if _FEATURE_JUDGE is None:
        _FEATURE_JUDGE = _load_sibling("feature_judge")
    return _FEATURE_JUDGE


def _auto_classify_unit(sdlc_dir, config, source, issue_title=None, issue_body=None):
    """#2363: is this filing eligible for automatic classification, and if so, what unit does it
    resolve to? `None` on every gate that is not met. Gated IDENTICALLY to the pick-time
    integration (`feature_labels._handle_no_unit_at_pick`): opted in
    (`discovery.no_dangling_goal.enabled`), a catch-all configured
    (`discovery.no_dangling_goal.core`), and `.sdlc/features/` adopted -- so a repo that never
    turned this on pays nothing beyond the same cheap attribute reads that gate already costs, and
    an unadopted repo is never retroactively attributed (D-7 of design #2253, the
    identical reasoning `_handle_no_unit_at_pick` already applies).

    `issue_title`/`issue_body` (#2383, slice H of epic #2260's completion work): the new issue's
    own already-computed title/body text -- `create_tracked_issue`'s own `heading`/`text` locals,
    the REAL final content the new issue is about to be filed with -- threaded straight through to
    `classify_for_filing`'s same-named kwargs (see that function's own docstring for what it does
    with them). Both default to `None`, so a call that omits them (there is none left inside this
    module after this slice, but the signature stays backward compatible for any other caller)
    behaves exactly as it did before this slice existed.

    Never raises: every failure here degrades to "no classification", the same fail-open posture
    every other step of `create_tracked_issue` already has."""
    if source is None or not getattr(source, "no_dangling_goal_enabled", False):
        return None
    core = getattr(source, "no_dangling_goal_core", None)
    if not core:
        return None
    try:
        if not _feature_registry().registry_dir(sdlc_dir).is_dir():
            return None
    except Exception:                                          # noqa: BLE001 - never break a filing
        return None
    try:
        # #2380: a FURTHER, independent opt-in (`no_dangling_goal_live_judge_enabled`, resolved by
        # the Source the same way `no_dangling_goal_core` immediately above already is) routes
        # this through a REAL, metered live model call (`feature_judge.live_judge`) instead of the
        # judge-less default that always abstains. OFF (the default) passes `judge=None`, byte-
        # identical to omitting the keyword entirely -- see `feature_labels._handle_no_unit_at_pick`
        # for the identical gating pattern at the pick-time integration point.
        judge = (_feature_judge().live_judge
                 if getattr(source, "no_dangling_goal_live_judge_enabled", False) else None)
        return _feature_classify().classify_for_filing(
            sdlc_dir, source, config, core, judge=judge,
            issue_title=issue_title, issue_body=issue_body)
    except Exception as exc:                                   # noqa: BLE001 - never break a filing
        print(f"sigma: automatic classification skipped (non-fatal): {exc}", file=sys.stderr)
        return None


def _ownership_verdict(sdlc_dir, config, goal, units, run):
    """#1479: may this account file directly under the units this issue will declare?
    -> `(verdict, unit)` for the FIRST refusal, else `(None, None)`.

    `units` IS `feature_stamp.declared_units`, NOT THE INHERITED UNIT, and that distinction is the
    whole of a measured bypass. `reconcile_labels` returns `unit=None` in both of its cases while
    LEAVING the caller's `feature:<name>` label in `labels` -- deliberately, since an explicit label
    beats an inherited default -- so `handoff track --label feature:<x>` created directly-actionable
    work carrying a stranger's unit while this function was asked about `None` and answered
    "no question arises". Recorded `create_dependency` call before the fix: `goal_label=True`, the
    unit label present, no `sdlc:needs-confirmation`, and zero warnings.

    EVERY DECLARED UNIT IS ASKED, AND THE FIRST REFUSAL WINS. Two rival `feature:` labels is a state
    `features.read` refuses outright, so there is no right one to pick -- and checking only the
    first would let the gate be walked past by typing a second label. The more restrictive reading
    is the one this level takes everywhere else.

    `(None, None)` means the question does not arise -- nothing is declared, or the module could not
    even be loaded. A load failure is a filing that behaves exactly as it did before this level
    existed, which is the same fail-open posture `gate_at_filing` takes internally: a gate that
    cannot answer must never cost the finding."""
    if not units:
        return None, None
    try:
        owner = _feature_owner()
        for unit in units:
            # `gh_run=`, NEVER `run=`: this function's runner answers `(argv) -> stdout` (it is the
            # one `ledger.actor` takes), and `gate_at_filing`'s `run` is the `(cwd, argv)` GIT
            # runner `repo_slug` needs. An earlier revision passed one value to both parameters'
            # worth of consumers and each failure landed on ALLOW -- see `gate_at_filing`'s own
            # docstring for the measurement. Naming the gh one explicitly is what keeps that
            # unrepresentable rather than merely avoided.
            verdict = owner.gate_at_filing(sdlc_dir, config, goal, unit, gh_run=run)
            if verdict is not None and not verdict.allowed:
                return verdict, unit
    except Exception as exc:                            # noqa: BLE001 - never break a filing
        print(f"sigma: ownership check skipped (non-fatal): {exc}", file=sys.stderr)
    return None, None


def _duplicate_search(sdlc_dir, config, title, text, exclude_refs=None, run=None):
    """#1204: is there ALREADY a tracked issue covering this exact root cause? Reuses dedup.py's
    engine (#917) unchanged -- the only new thing here is the corpus it's handed.

    In github mode, `mirror.fetch_dependency_records` supplies a corpus deliberately NOT scoped by
    `discovery.github.assignee` (a follow-up filed by a different person/session must still be
    found) and covering BOTH `discovery.github.goal_label` and the `sdlc:needs-confirmation` label (a queued
    follow-up withholds the goal label by design -- see PROPOSED_LABEL -- so a query restricted to
    it alone can never dedup one follow-up against another). When that corpus isn't available (not
    github mode, no `gh`, offline, any error -- `fetch_dependency_records` is fail-open and returns
    `None`), `records=None` reaches `dedup.find_candidates` unchanged, which falls through to its
    own default corpus (`backlog_check._build_corpus`: the cached pick-time mirror in github mode,
    the local goal files otherwise) -- the same graceful degrade every other step in
    `create_tracked_issue` already has, never an empty, un-searched corpus where a cheaper one
    exists.

    `exclude_refs` (#1204 post-implementation fix): the CURRENT goal this follow-up is being filed
    FROM is itself a corpus member (a local goal file is on disk; a github goal is itself an open
    issue) -- and a same-area follow-up routinely reuses the parent goal's own vocabulary ("the
    retry wrapper releases the pooled connection twice", filed FROM a goal titled "add backoff to
    the retry wrapper"). Measured against a real, non-degenerate fixture, that alone scores 0.72 --
    well past the 0.45 duplicate line -- with no unrelated prior issue involved at all. Without this,
    `create_tracked_issue` would read the goal's own presence in the corpus as "this exact thing was
    already filed", silently skip opening the real new issue, and instead post a "duplicate" comment
    onto the goal's OWN issue -- for what is realistically the common case, not an edge case. Passed
    straight through to `dedup.find_candidates`'s own `exclude_refs` (built for exactly this: a
    resolved target's own ref must never register as its own duplicate).

    Never raises: any failure anywhere in this chain degrades to "no duplicate found" (an empty
    pack), so a corpus-fetch failure can never cost a follow-up its filing, only its dedup check."""
    try:
        gh = ((config or {}).get("discovery") or {}).get("github") or {}
        records = mirror.fetch_dependency_records(
            sdlc_dir, config, run=run, labels=[gh.get("goal_label", "sdlc:goal"),
                                                proposed_label(config)])
        return dedup.find_candidates(sdlc_dir, text, config=config, title=title, records=records,
                                     exclude_refs=exclude_refs)
    except Exception:                                          # noqa: BLE001 - fail-open by contract
        return {"schema": dedup.SCHEMA, "candidates": [], "degraded": ["error"]}


def _issue_id_from_ref(ref):
    """dedup's local-mode candidate `ref` is the goal file's full PATH (it mirrors
    `backlog_check._build_corpus`'s own local-mode doc shape) -- every local-mode call this module
    makes with an issue id (`append_to_body`/`note`/the ledger's own `issue=` field) wants the bare
    id `LocalSource.create_dependency` itself returns instead (an int, the goal file's leading
    `<digits>-` prefix, #921's own convention). A github-mode `ref` is already a bare issue-number
    STRING and passes through unchanged."""
    s = str(ref)
    if s.isdigit():
        return s
    lead = pathlib.Path(s).name.split("-", 1)[0]
    return int(lead) if lead.isdigit() else s


def _duplicate_context_comment(goal, area, why):
    """Posted to the EXISTING issue when a file-time duplicate search finds one — the new context a
    second filing attempt would otherwise have carried, so a human reviewing the existing issue
    sees that it was independently rediscovered, not silently dropped."""
    return (f"Also found from goal `{goal}` (area `{area}`) while about to file what looked like a "
            f"NEW issue for the same thing: {why}\n\n"
            "An automated duplicate search (#1204) matched this issue closely enough that a "
            "second one was not opened. Reply here if this is actually a different problem.")


def issue_body(goal, area, why, blocked_by_url=""):
    """The body a human (or their loop) reads cold. It has to say what is blocked, on what, and what
    'done' means — an unactionable hand-off is worse than none, because it looks handled."""
    lines = [
        f"Blocking another area's work. Raised automatically by the SDLC loop from goal `{goal}`.",
        "",
        f"**Area:** `{area}`",
        f"**What is needed:** {why}",
        "",
        "**Done when:** the dependency above exists and the blocked goal can proceed.",
        "",
        "Reply on this issue if this should be re-scoped, re-assigned, or declined — the blocked "
        "goal is parked until then.",
    ]
    if blocked_by_url:
        lines.insert(4, f"**Blocked goal:** {blocked_by_url}")
    return "\n".join(lines)


def _tracked_issue_body(goal, area, why, blocks_goal):
    """Default body for a tracked issue when the caller doesn't supply one — the `track` CLI verb has
    no `--body` flag, so this is what a same-area follow-up or a queued finding gets. `hand_off()`
    always supplies its own body via `issue_body()` instead (unchanged): that template's wording
    ("blocking another area's work") is specific to a blocking cross-area dependency and would be
    misleading here."""
    lines = [
        f"Raised automatically by the SDLC loop from goal `{goal}`.",
        "",
        f"**Area:** `{area}`",
        f"**What is needed:** {why}",
    ]
    if blocks_goal:
        lines += [
            "",
            "**Done when:** the work above exists and the blocked goal can proceed.",
            "",
            "Reply on this issue if this should be re-scoped, re-assigned, or declined — the "
            "blocked goal is parked until then.",
        ]
    return "\n".join(lines)


#: #1551: `upstream` is loaded on FIRST FILING, not at import -- the same reason and the same shape
#: as `_FEATURE_OWNER` above. It pulls `work` behind it for one call to `work.stem`, and `work`
#: pulls `state`, `ledger`, `scrub`, `gh_session` and `features` behind it. Measured on this
#: machine, best of seven cold imports of `handoff` alone: 87ms eager, 40ms lazy -- paid by every
#: consumer of this module, including the many that never file anything. The accessor exists so a
#: test can substitute the module.
_UPSTREAM = None


def _upstream():
    global _UPSTREAM
    if _UPSTREAM is None:
        _UPSTREAM = _load("upstream")
    return _UPSTREAM


def _upstream_runner(upstream_run, run):
    """#1551: the runner `upstream.route` performs its ONE outbound reach with, or `None` for the
    real `gh`.

    THE ISOLATION IDIOM OF THIS SUITE IS `run=<fake>`, AND IT HAS TO KEEP HOLDING. `upstream.route`
    falls back to a real `gh` subprocess when it is handed nothing, so a caller that injected `run`
    -- and had no reason to know a second runner existed -- would have its fully-faked filing
    perform a live `POST /repos/<upstream>/issues` against somebody's real tracker. That is not a
    hypothetical: it opened a real issue on this repository during this goal's own review, while the
    injected runner recorded zero calls.

    So the precedence is explicit and the fallthrough is last: an explicit `upstream_run` wins (it
    is the higher-fidelity shape); otherwise an injected `run` is ADAPTED; only a caller who
    injected NOTHING reaches the network. `tests/test_upstream.py::
    test_injecting_run_alone_can_never_reach_the_network` counts real-`gh` entries to hold it,
    rather than asserting on an outcome string that a swallowed exception would fake."""
    if upstream_run is not None:
        return upstream_run
    if run is not None:
        return _upstream().adapt_runner(run)
    return None


def create_tracked_issue(sdlc_dir, config, goal, area, why, *,
                          same_area, immediately_actionable, blocks_goal,
                          priority=DEFAULT_PRIORITY, title=None, body=None,
                          extra_labels=(), source=None, run=None, dedup=True,
                          upstream_run=None, target_unit=None, idempotency_key=None):
    """Open a tracked issue, address it, record it. The one real place the kit ever opens an issue on
    its own behalf — `hand_off()` below is a thin wrapper around this. Generalizes what used to be
    hand-off-only discipline (owner resolution, labels, the `gh` call, the ledger write, the dual
    comment+body-marker channel) to EVERY issue Sigma itself creates, closing the "orphan issue"
    bug: a same-area follow-up finding had no disciplined path at all before this and got filed by
    hand, unlabeled and unassigned (#462).

    `goal` keeps `hand_off()`'s existing meaning: the currently-in-progress goal this issue is being
    filed FROM, never the new issue itself.

    Three REQUIRED, keyword-only booleans — Python raises `TypeError` if a caller omits one. That is
    the actual mechanism behind "explicit, unavoidable, never a silent default": a default here is
    exactly how the orphan-issue bug, and a false-blocking bug, would each quietly reappear.

      same_area — who gets assigned.
        True  → `ledger.actor(config, run)` — I'm filing a follow-up in the area I'm already
                working; assign it to me.
        False → `owners.owner_of(project_root(sdlc_dir), area, config)` — a cross-area hand-off,
                CODEOWNERS-resolved, `None` with no matching entry (a caller must handle that
                gracefully — this function already does). #2395: when `immediately_actionable`
                is (finally) True and no owner resolved, `report["owner"]` falls back to
                `ledger.actor(config, run)` — the same actor `same_area=True` uses — so the new
                issue is never left both carrying `sdlc:goal` AND unassigned, which would make it
                invisible to every account's assignee-scoped discovery. A queued (non-actionable)
                filing with no owner stays genuinely unassigned; see the fallback's own comment.

      immediately_actionable — the `sdlc:goal` gate.
        True  → the new issue carries the goal label, so `next_pending()`'s own `--label` filter
                auto-picks it for whoever it's assigned to.
        False → queued: filed, but not auto-picked until a human promotes it.

      blocks_goal — gates exactly two things, nothing else.
        True  → write the `**Blocked by:** #N` marker onto the CURRENT goal's own issue body (via
                `source.append_to_body`), so `backlog_check._explicit_blockers()` auto-skips that
                goal until the new issue closes — a genuine blocking dependency. `hand_off()` always
                pins this True: a hand-off is BY DEFINITION a blocking dependency.
        False → no marker, and the current goal is never treated as blocked. A non-blocking,
                merely-related finding must never auto-park unrelated work — that is the real
                correctness bug this axis exists to prevent; a design that conflated "blocking
                dependency" with "follow-up finding" would ship it.

      target_unit — (#1820, optional, default None) cross-map this filing onto a DIFFERENT unit
        than the one it would otherwise inherit from `goal`. The common case — omitting it — files
        exactly as before #1820 existed. When given, it must name a currently-OPEN unit already
        registered in `.sdlc/features/index.json`
        (`feature_registry.resolve_open_unit`); an unknown or CLOSED unit is refused identically —
        never distinguished, so nothing downstream is tempted to special-case "closed" into a
        weaker, still-permitted retarget — and the filing proceeds with whatever `goal`'s own
        inheritance produced instead, with a warning naming why. THIS IS AN OPERATOR/CLI-LEVEL
        MECHANISM ONLY (apart from the rebase upkeep's own per-conflict findings, which pass it
        with `goal=None`; see the no-goal path below): nothing in `skills/sigma-loop/SKILL.md`'s own autonomous-filing guidance
        passes this parameter, and this issue does not change that — deciding a discovered issue
        "clearly belongs" to a different unit stays a human (or an explicit, separately-designed
        trigger) supplying the name, never a similarity judgment this function makes for itself.

        #2363: THAT "separately-designed trigger" NOW EXISTS, and it is deliberately SEPARATE from
        `target_unit` rather than a second meaning of it. When `target_unit` is omitted AND nothing
        was inherited from the filing goal (`unit` stays falsy after `feature_stamp.unit_of`),
        `_auto_classify_unit` is consulted -- config-gated exactly like the pick-time classification
        chain (`discovery.no_dangling_goal.enabled` + `.core` + an adopted registry), and a no-op on
        every repo that has not opted in. An explicit `target_unit` ALWAYS wins outright: the two
        blocks are `if`/`elif`, so classification is never even attempted when one was passed.

    Labels always applied: `priority:<priority>`, `area:<area>`, any `extra_labels`, plus the goal
    label iff `immediately_actionable` (`GitHubSource.create_dependency`'s own `goal_label=`).

    Ledger shape reuses two existing kinds, adds no new one — gated on `blocks_goal` too, not just
    `same_area` (PR #466 review finding): `backlog_check._ledger_signals()` is a SECOND, independent
    blocking mechanism from the body-marker/`_explicit_blockers()` channel above — it treats any
    `ledger.outstanding()` entry (`kind == "handoff"`, not yet acked) as a confident block against
    the entry's own `goal` — the side that filed it (`handoff_key()` only supplies the finding's
    `ref`, i.e. which target has to land first; #532). That makes THIS gate strictly MORE
    load-bearing than it was when the block landed on `handoff_key()`: every `kind="handoff"` row
    now blocks the goal it names, in github and local mode alike, with no accidental miss to soften
    a wrongly-written one. Writing `kind="handoff"` unconditionally for every `same_area=False`
    call — including `blocks_goal=False`, a fully sanctioned "cross-area FYI, not a blocker"
    combination — would therefore park the FILING goal on a finding that was never a blocker,
    exactly the false-blocking bug this whole axis exists to prevent, one layer deeper than the
    body marker:
      `(not same_area) and blocks_goal` (a genuine cross-area BLOCKING dependency) → `kind="handoff"`,
        `to=<resolved owner>`, `state="open"` — byte-identical to `hand_off()`'s pre-existing write
        (`hand_off()` always pins `blocks_goal=True`, so its behavior is completely unchanged), so it
        still participates in `ledger.outstanding()`/`unanswered()` and is answerable via
        `handoff.py ack`.
      Anything else (`same_area=True`, OR `blocks_goal=False` regardless of `same_area`) →
        `kind="note"`, `to=<ledger.actor(config, run) if same_area else the resolved owner>`, no
        `state` — deliberately NOT a "handoff": `outstanding()` only ever looks at `kind ==
        "handoff"`, so this can never get stuck as a permanently-unanswered hand-off nobody was ever
        meant to `ack`, AND never triggers `_ledger_signals()`'s ledger-based block either. A
        `same_area=True` note is self-addressed so a LATER session by the same actor (after a
        compact, a crash, or just picking the loop back up) is reminded the tracked issue exists; a
        `same_area=False, blocks_goal=False` note is addressed to the CODEOWNERS-resolved owner
        instead, so they still see it for visibility — just without the (incorrect, for a
        non-blocker) outstanding-hand-off treatment.

    dedup (#1204 review, optional, default True): the ONE escape hatch from the file-time duplicate
    search below -- every caller except `decompose_check`'s `file` mode wants it on. That caller
    passes `dedup=False` because its own body is `decompose_goal._META_BODY`, a fixed, multi-
    paragraph template with only the parent id/title varying per filing -- almost none of a 500-char
    mirror excerpt (`mirror._EXCERPT_CHARS`) is that varying part. Measured against real, unrelated
    parents (`tests/test_handoff.py`'s dedup-collision regression): two DIFFERENT parents' decompose
    issues score 0.5+ against each other from shared boilerplate ALONE, well past the 0.45 duplicate
    line -- so the search would routinely make decompose_check reuse the WRONG parent's meta-issue,
    silently orphaning the RIGHT parent's decomposition (its own DECOMPOSE_FILED_MARKER idempotency
    scan, which IS scoped correctly per-parent by reading THAT parent's own comment thread, would
    then treat the goal as already handled forever). `dedup=False` skips `_duplicate_search`
    entirely -- `duplicate_of` stays `None`, `candidates` stays empty, filing behaves exactly as it
    did before #1204 introduced the search. Every other caller's body carries enough per-filing,
    non-templated content that this false-positive mode doesn't apply, so they keep the default.

    #1471: every issue this files also inherits the UNIT OF WORK of the goal it was filed from --
    both halves of it, the `feature:<name>` label and the two-line body marker, resolved from the
    filing goal here rather than passed in by a caller. Same argument as `FOLLOWUP_LABEL` above: an
    inherited unit a caller has to remember to supply is one that goes missing on the very issues
    nobody wrote by hand, which is where the chain matters most. A goal that declares no unit files
    exactly as it did before, and every failure degrades to that same no-unit filing with the reason
    said out loud in `warnings` -- `feature_stamp` never fails a filing. See that module for the two
    placements (body marker BEFORE creation, label AFTER) and why the reverse loses issues.

    #1551: WHOSE BOARD IS THIS FOR? A finding about SIGMA ITSELF is never filed here. This is
    the only place the kit opens an issue on its own behalf, so it is the only place that can ask
    the question at all -- and it asks it BEFORE anything is written, because a kit finding must not
    reach an adopter's board and then be relabelled or closed; it must not reach it. `upstream.route`
    classifies the finding structurally -- does its evidence cite a path that EXISTS under the
    plugin's install path, under one of the plugin's SIGNATURE directories (`skills/`, `hooks/`,
    `.claude-plugin/`), under a top-level directory this project does not have? -- files it on
    `ledger.handoff.upstream_repo` when one is configured AND `cross_repo` measures `granted` for
    it, and otherwise raises it through the ledger addressed to the operator, the same
    `to`-addressed transport every other blocked cross-repo ask already uses. `withheld` is the one
    field honoured here, and honouring it skips the ISSUE, the narrative comment and the body marker
    alike. `issue_attempted` deliberately stays False: withholding is a routing decision that
    succeeded, not a filing that failed. The failing direction is towards filing locally; Sigma
    developing Sigma is covered twice (a short-circuit when the two trees overlap, and the
    project-owns-its-own-`skills/` conjunct in the ordinary installed layout, where they do not);
    and `ledger.handoff.kit_findings: "file-locally"` turns the whole thing off -- see upstream.py,
    which owns all of that reasoning.

    `upstream_run` (optional): a `cross_repo`-shaped `(returncode, stdout, stderr)` runner for the
    ONE place this function reaches GitHub outside the backlog source -- the upstream access check
    and the upstream filing. Its shape is not `run`'s (stdout only), which carries neither the exit
    code nor the error text the three-valued access check is built on.

    NOT NAMED `gh_run`, and the near-miss is the reason. `_ownership_verdict` above passes
    `gate_at_filing(gh_run=run)`, where `gh_run` means the STDOUT-ONLY runner -- the exact opposite
    of what the same name would mean here, in the same file, two hundred lines apart. #1479's own
    docstring records what one crossed runner cost there: every ownership verdict landing on ALLOW.
    A distinct name makes the crossing unrepresentable instead of merely avoided.

    A caller who injects only `run` still gets isolation: `_upstream_runner` below ADAPTS it rather
    than ignoring it, so the real `gh` is reachable only from a call that injected nothing at all.
    `None` for both (every caller today) means the real `gh`.

    Returns a report dict (`goal`, `area`, `owner`, `issue`, `entry`, `warnings`, `duplicate_of`,
    `issue_attempted`, `unit`, `routing`); never raises. `routing` (#1551) is `upstream.route`'s own
    report -- always present, and `routing["withheld"]` is False on every ordinary filing.
    `unit` (#1471) is the unit the new issue was LABELLED
    with -- deliberately not "stamped with", which the first version of this line said and which is
    false on one real path: when no placement of the body marker reads back, the marker is withheld
    and the label is still attached, so the issue genuinely IS in that unit while its body says
    nothing. The accompanying warning is what distinguishes the two, and it is always present.
    `unit` is None when the goal declared none, when a declaration could not be inherited safely,
    when a caller-supplied `feature:` label overruled it, and on the duplicate-reuse path, where
    neither half is written. `duplicate_of` (#1204) is the pre-existing
    issue's id when a file-time duplicate search found one closely-matching enough that no new issue
    was opened -- `None` otherwise (also `None`, always, when `dedup=False`).

    #1203: `issue_attempted` is the one field a caller (the CLI dispatcher, chiefly) needs and could
    not otherwise get -- `report["issue"]` being falsy is ambiguous between two entirely different
    situations: "there was a capable source and it genuinely came back without an issue to show for
    it" (a real defect worth failing loud over) and "there was never anything to try" (no source
    configured, or one honestly lacking `create_dependency` -- the documented, deliberately degraded
    local/no-`gh` mode). Only the former should ever make a CLI verb exit non-zero; without this
    field a caller would have to re-derive `source is not None and hasattr(source,
    "create_dependency")` itself, duplicating the exact test this function already performs a few
    lines down. Set True as soon as that capable-source branch is entered -- BEFORE the #1204
    duplicate search runs -- because a confident duplicate match (reusing an existing issue instead
    of calling `create_dependency` at all) is still a capable source successfully resolving this
    filing, not a "nothing to try" case; `report["issue"]` ends up truthy either way (the reused
    issue's id, or a freshly-created one's), so it never trips the `issue_attempted and not issue`
    failure check regardless. Only a genuine failure to create OR reuse an issue leaves both
    `issue_attempted` True and `issue` falsy."""
    # THE NO-GOAL PATH (part B, level 3): `goal=None` means a finding the engine files about a UNIT, with no goal to
    # inherit a unit from, no goal issue to comment on and no goal to block. It differs from the ordinary path in
    # exactly these ways and no others: no `unit_of` lookup; no metered auto-classification (an unnamed unit stays
    # unnamed); no ownership verdict, so no note is written to a unit owner (a finding must not be addressed to
    # whoever filed it, or autowatch can start a paid run from it); no comment or body marker on a goal; the ledger
    # row and the upstream router are keyed on a SCOPED key rather than a goal; the fuzzy duplicate search is OFF
    # (`idempotency_key` replaces it) and the key rides in the body as an inert line. A caller passing a goal gets
    # the code below byte for byte as before.
    nogoal = goal is None
    if nogoal:
        dedup = False
        scope = idempotency_key or hashlib.sha256(("%s\0%s" % (title, why)).encode("utf-8", "replace")).hexdigest()[:16]
        key_goal = "upkeep-" + re.sub(r"[^A-Za-z0-9_-]", "-", str(scope))[:40]
        if idempotency_key and body is not None:
            body = "%s\n\nconflict-id: %s\n" % (body.rstrip("\n"), idempotency_key)
    else:
        key_goal = goal
    report = {"goal": "" if nogoal else str(goal), "area": area, "owner": None, "issue": None,
              "entry": None, "warnings": [], "duplicate_of": None, "issue_attempted": False,
              "unit": None, "routing": None}

    if same_area:
        report["owner"] = ledger.actor(config, run)          # never raises, never empty (see actor())
    else:
        try:
            report["owner"] = owners.owner_of(project_root(sdlc_dir), area, config)
        except Exception as exc:                               # noqa: BLE001 - roster is advisory
            report["warnings"].append(f"could not read CODEOWNERS: {exc}")
        if not report["owner"]:
            report["warnings"].append(
                f"no owner for area {area!r} — recording unaddressed; "
                "add the area to CODEOWNERS or to config ledger.owners")

    if source is None:
        try:
            source = sources.get_source(sdlc_dir, config)
        except Exception as exc:                               # noqa: BLE001
            report["warnings"].append(f"no backlog source: {exc}")

    # #1551: WHOSE BOARD IS THIS FINDING FOR? Decided here, at the one place the kit ever opens an
    # issue on its own behalf, and decided BEFORE anything is written -- a finding about Sigma
    # itself must never reach an adopter's board at all, not reach it and get relabelled. Classified
    # over the CALLER's own words (`title`, `why`, `body`), never the rendered template below: the
    # template is metadata this function adds, and a classifier that read it would be judging its
    # own boilerplate. `route` writes nothing and says nothing on the overwhelmingly common
    # `project` answer, so an ordinary filing is byte-identical to what it was before this existed;
    # see upstream.py for the routing and for why the failing direction is towards filing locally.
    routing = _upstream().route(sdlc_dir, config, key_goal, title, why, body,
                             run=_upstream_runner(upstream_run, run))
    report["routing"] = routing

    if routing["withheld"]:
        # The adopter's board is not written -- not the issue, not the narrative comment, not the
        # body marker. `issue_attempted` deliberately stays False: withholding is a routing decision
        # that SUCCEEDED, not a filing that failed, and #1203's exit-code contract exists to make a
        # genuine creation failure loud. The ledger write below still runs, as it does on every
        # other path where no issue could be opened.
        report["warnings"] += routing["warnings"]
    elif source is not None and hasattr(source, "create_dependency"):
        heading = title or (f"[{area}] {'dependency' if blocks_goal else 'finding'} from "
                            f"{pathlib.Path(str(goal)).name}")
        text = body if body is not None else _tracked_issue_body(goal, area, why, blocks_goal)
        # #1471: the new issue inherits the UNIT of the goal it is being filed FROM. Resolved here
        # rather than passed in by each caller for precisely the reason FOLLOWUP_LABEL is added
        # here: this is the one place the kit opens an issue on its own behalf, and an inherited
        # unit a caller has to remember to pass is an inherited unit that goes missing. Resolving it
        # is all that happens at this point -- neither half is WRITTEN until there is an issue this
        # call actually opened to write it onto (see the two sites below).
        unit, unit_warnings = (None, []) if nogoal else feature_stamp.unit_of(source, goal)
        report["warnings"] += unit_warnings
        # #1820: an EXPLICIT `target_unit` overrides the inherited one outright -- the whole point
        # of a caller naming a different unit is that the discovered issue does NOT belong to the
        # goal it is being filed from. Resolved against the registry (`.sdlc/features/index.json`,
        # #1820's own named source of truth) rather than trusted verbatim: only a currently-OPEN,
        # registered unit may be targeted, and a closed or unknown one is refused IDENTICALLY (see
        # `feature_registry.resolve_open_unit`'s own docstring) -- never silently reopening a
        # finished unit, and never failing the filing over a bad flag value. `unit`/`unit_warnings`
        # from the block above are otherwise untouched, so everything downstream (`reconcile_labels`,
        # `declared_units`, `_ownership_verdict`, `stamp_body`, `attach`) needs no change at all: a
        # targeted unit and an inherited one are indistinguishable to every later step.
        if target_unit:
            resolved = _feature_registry().resolve_open_unit(sdlc_dir, target_unit)
            if resolved:
                unit = resolved
            else:
                report["warnings"].append(
                    "not targeting unit %r: it is not a known OPEN unit in this repository's "
                    "registry (.sdlc/features/index.json) -- filing without it (a closed unit is "
                    "never retargeted, explicitly or automatically, and an unknown one cannot be "
                    "invented)" % target_unit)
        elif not unit and not nogoal:
            # #2363: automatic classification -- ONLY when no explicit `--target-unit` was given
            # (an explicit human target always wins outright, hence the `elif`) AND nothing was
            # inherited from the filing goal above (`unit` still falsy). Config-gated the same way
            # the pick-time chain is (`_auto_classify_unit`'s own docstring), so this is a no-op on
            # every repo that has not opted in. `target_unit`/`unit` from here on are otherwise
            # untouched, so `reconcile_labels`, `declared_units`, `_ownership_verdict`, `stamp_body`
            # and `attach` below need no change at all -- a classified unit and an inherited one are
            # indistinguishable to every later step.
            classified = _auto_classify_unit(sdlc_dir, config, source,
                                              issue_title=heading, issue_body=text)
            if classified:
                unit = classified
                report["warnings"].append(
                    "classified under unit %r: no unit was declared, inherited, or explicitly "
                    "targeted (discovery.no_dangling_goal.core, #2363)" % classified)
        labels = [f"priority:{priority}", f"area:{area}", *extra_labels]
        # #1347: guaranteed, not a caller-supplied extra — dedup against a caller that already
        # passed it explicitly (harmless either way, but a repeated label in one `gh` call is
        # needless noise).
        if FOLLOWUP_LABEL not in labels:
            labels.append(FOLLOWUP_LABEL)
        # #1471 review: a rival unit can arrive as a LABEL too, not only in the body -- `handoff
        # track --label feature:billing` puts one straight into `create_dependency(labels=)`. Filing
        # that beside an inherited unit produced two rival `feature:` labels plus a body marker, the
        # one state `features.read` raises on, with an empty `warnings` and `unit` reporting success
        # -- and a REGRESSION, since the same command used to file a readable `label_only` issue.
        # Settled here, over the ASSEMBLED list rather than over `extra_labels`, so a future label
        # source inherits the rule for free -- the same chokepoint argument as everything else here.
        reconciled = feature_stamp.reconcile_labels(labels, unit)
        labels, unit = reconciled.labels, reconciled.unit
        report["warnings"] += reconciled.warnings
        # #1479: OWNERSHIP IS DECIDED FIRST, BEFORE THE #1393 UPGRADE BELOW, and the ordering is
        # the fix to a contradiction rather than a preference. An issue carrying a unit's label may
        # be filed DIRECTLY only by the unit's owner or by the owner of the board it lands on
        # (§12); anyone else is never blocked from raising it, but it lands as a proposal, which is
        # what `sdlc:needs-confirmation` has always meant. The #1393 block resolves an INTERNAL
        # incoherence (a blocker nothing may pick) and says so in a warning; authority is not
        # something Sigma may upgrade away on its own behalf, so when ownership refuses, that
        # upgrade must not run AND its warning must not be emitted -- it would be false of the call
        # actually made, and it would come first in the list.
        #
        # THE WARNING AND THE LEDGER NOTE ARE BOTH DEFERRED to the branch that actually files (see
        # below): on the duplicate-reuse path nothing is opened and nothing is stamped, so saying
        # "filed as a proposal" there would describe a filing that did not happen.
        ownership, ownership_unit = (None, None) if nogoal else _ownership_verdict(
            sdlc_dir, config, goal, feature_stamp.declared_units(labels, unit), run)
        if ownership is not None:
            immediately_actionable = False
        # #1393: `blocks_goal=True` together with `immediately_actionable=False` is INCOHERENT --
        # it asserts "real work is stalled behind this" and "nobody may pick this up" at the same
        # time. Accepting the combination is how Sigma manufactured its own deadlocks: the
        # blocker was filed with `proposed_label` and no `goal_label`, so no queue could serve it,
        # while `auto_unpark` refuses to resume the goal it blocks until it CLOSES. Neither side
        # could move, and the only ledger entry written was a `note`, which autowatch does not
        # watch -- so nobody was told either.
        #
        # UPGRADED, not refused. Refusing is the other defensible answer, but it leaves the caller
        # with a blocked goal and no blocker recorded anywhere, which is strictly worse. The
        # warning is what makes it visible rather than silent: the caller asked for a proposal and
        # got a goal, and they are told exactly why.
        if blocks_goal and not immediately_actionable and ownership is None:
            report["warnings"].append(
                "filed #<new> as an actionable goal, not a proposal: it was requested as queued "
                "(needs human approval) AND as blocking this goal, which cannot both be true -- a "
                "blocker nothing may pick is a deadlock, since the goal it blocks only resumes when "
                "it closes")
            immediately_actionable = True
        # #2395: "no owner resolved" and "nobody can ever pick this up" are two DIFFERENT failures,
        # and until now a cross-area filing with no CODEOWNERS/`ledger.owners` match conflated
        # them. The label side was already correct -- `immediately_actionable` (by now at its FINAL
        # value, past both upgrades above) alone decides `sdlc:goal`, never `report["owner"]` -- but
        # an issue that DOES carry `sdlc:goal` and carries NO assignee is invisible to every
        # account's `discovery.github.assignee`-scoped pick query (`sources.py::_card_is_eligible` /
        # `_fetch_pending`'s server-side `assignee=` filter), and `/sigma-setup` itself defaults that
        # setting to `"@me"` on first GitHub adoption -- so this is the common configuration, not an
        # edge case. Nobody's `@me` ever equals "unassigned", so the issue sits in the backlog
        # carrying `sdlc:goal` and reachable by NOBODY's loop: unowned had silently become
        # unpickable too. Falling back to the filing actor -- the same actor `same_area=True`
        # already assigns to, unconditionally -- keeps it genuinely pickable (by that actor's own
        # loop, at minimum) while the "no owner for area" warning above still tells a human to fix
        # CODEOWNERS/`ledger.owners` so it routes to the right person. Gated on
        # `immediately_actionable`: a QUEUED issue withholds `sdlc:goal` entirely (see #233 below),
        # so no discovery query ever looks at its assignee, and there is nothing to repair here --
        # leaving `report["owner"]` at `None` for that case is unchanged, exactly as tested by
        # `test_handoff_still_records_when_no_owner_is_declared`'s own queued/actionable siblings.
        if (not same_area) and immediately_actionable and not report["owner"]:
            report["owner"] = ledger.actor(config, run)
            report["warnings"].append(
                f"self-assigning to {report['owner']!r} so this stays pickable despite no "
                "resolvable owner -- an unassigned sdlc:goal issue is invisible to any "
                "assignee-scoped discovery (discovery.github.assignee, \"@me\" by default)")
        # #233: a queued (not immediately-actionable) issue withholds `sdlc:goal` so it is never
        # auto-picked -- but that alone makes it identifiable ONLY by a MISSING label, which nothing
        # can query, so the pending-proposal set grew unnoticed. The DISTINCT `sdlc:needs-confirmation` label,
        # added at this single labels-assembly point (the one funnel every proposal passes through --
        # retro, `track --queue queued`, decompose), is what `/sigma-status` counts. Added ONLY here,
        # never for immediately_actionable=True: an actionable issue is a real goal, not a proposal.
        if not immediately_actionable:
            labels.append(proposed_label(config))
        # goal_label is passed only when it DIFFERS from create_dependency's own default (True) --
        # not "goal_label=immediately_actionable" unconditionally -- so a source implementation that
        # predates this parameter (a test double, or a future non-GitHub source) keeps working
        # exactly as before for the (overwhelmingly common) immediately_actionable=True case; only
        # `immediately_actionable=False`, a genuinely new capability, requires a source that
        # understands the new parameter at all.
        create_kwargs = {"labels": labels}
        if not immediately_actionable:
            create_kwargs["goal_label"] = False

        # #1203: a capable source is about to be given a real chance to resolve this filing --
        # either by the #1204 duplicate reuse just below, or by create_dependency further down.
        # Both outcomes leave report["issue"] truthy on success; only a genuine failure of BOTH
        # leaves it falsy while this stays True, which is exactly the signal the CLI dispatcher
        # needs (see this function's own docstring, and main()'s open/track handling).
        report["issue_attempted"] = True

        # #1204: search for an already-filed tracked issue covering the same root cause BEFORE
        # opening a new one -- dedup.py's engine (#917), reused rather than reimplemented, run here
        # over THIS follow-up's own title+body (never the current goal's own text). See
        # _duplicate_search's docstring for the corpus this draws on and why it's not restricted by
        # discovery.github.assignee. Fail-open by contract: any error degrades to an empty pack, so
        # this can only ever ADD a dedup check, never block a filing that would otherwise succeed.
        #
        # #1204 review: `dedup=False` (see this function's own docstring) skips the search
        # altogether rather than running it and discarding the result -- a caller whose body is a
        # near-fixed template (decompose_check's file mode) gets neither the false-positive risk
        # nor the wasted corpus fetch.
        candidates = []
        duplicate = None
        if dedup:
            dup_pack = _duplicate_search(sdlc_dir, config, heading, text, exclude_refs=[str(goal)],
                                         run=run)
            candidates = dup_pack.get("candidates") or []
            duplicate = next((c for c in candidates if c.get("strength") == "duplicate"), None)

        if duplicate is not None:
            # a confident match: reuse the existing issue instead of filing a second one. The
            # existing issue's id becomes report["issue"] so every step below (ledger entry, the
            # "Blocked by" body marker, the narrative comment on the CURRENT goal) links to it
            # exactly as it would a freshly-opened one -- the relationship between the current goal
            # and the dependency is identical either way, only WHICH issue embodies it differs. This
            # also satisfies #1203's exit-code contract for free: report["issue"] is truthy, so
            # `issue_attempted and not issue` never fires for a duplicate-reuse outcome -- reusing an
            # existing issue is success, not a failed filing.
            report["duplicate_of"] = report["issue"] = _issue_id_from_ref(duplicate["ref"])
            report["warnings"].append(
                f"duplicate found — reusing existing issue #{report['issue']} "
                f"(score {duplicate['score']}) instead of filing a new one")
            # #1471: NOTHING is stamped onto a reused issue -- not the marker, not the label. This
            # is an issue Sigma did not just open: it may already belong to another unit, and a
            # second declaration is the one state no reader can resolve (`features.AmbiguousUnit`,
            # which would then break every sweep that reads it). Said out loud rather than done
            # silently, because the follow-up's unit genuinely ends up recorded nowhere, and that
            # is the fact a human reviewing the reuse needs. `report["unit"]` stays None for the
            # same reason: it names what was stamped, and nothing was.
            if unit:
                report["warnings"].append(
                    f"issue #{report['issue']} was reused rather than filed, so it was NOT stamped "
                    f"with unit {unit!r}: an issue Sigma did not just open may already belong "
                    "to another unit, and a second declaration would make it unreadable")
            # #1393: the reused issue may be one NOTHING CAN PICK, and this path never went near
            # the incoherence guard above -- that one only governs the labels handed to
            # `create_dependency` for a NEWLY filed blocker. `_duplicate_search`'s corpus
            # deliberately includes `proposed_label` issues, so a confident match can be an open
            # proposal carrying `sdlc:needs-confirmation` and no `sdlc:goal`. Blocking the current
            # goal behind it (via `mark_blocked` below) recreates exactly the deadlock this release
            # exists to remove: the goal waits for an issue to close, and no queue can serve it.
            #
            # Resolving it here reuses `blockers`' one policy rather than a second copy: Sigma's
            # own unruled follow-up is promoted, a teammate's is granted membership, and a
            # human-filed proposal / parked / third-party blocker is left untouched and warned about
            # so the caller can see why the goal is about to wait on something nobody will pick.
            if blocks_goal and source is not None:
                report["warnings"] += _resolve_reused_blocker(sdlc_dir, config, source, goal,
                                                              report["issue"], run)
            if hasattr(source, "note"):
                try:
                    # str(...): `report["issue"]` is a bare Python `int` in local mode
                    # (`_issue_id_from_ref`'s own documented local-mode return) -- `LocalSource.
                    # note`'s `_resolve_ref` calls `pathlib.Path(goal)` UNCONDITIONALLY first, which
                    # raises `TypeError` on a raw int (confirmed: `pathlib.Path(3)` is not
                    # str/PathLike). `_resolve_ref`'s own numeric-id fallback (#921) only ever
                    # accepted a STRINGIFIED id -- every other call site in this module already
                    # passes `str(goal)`, never a bare int; this is the one call this fix adds, and
                    # it has to follow the same convention. A no-op in github mode, where
                    # `_issue_id_from_ref` already returns a string.
                    source.note(str(report["issue"]), _duplicate_context_comment(goal, area, why))
                except Exception as exc:                        # noqa: BLE001 - never block the park
                    report["warnings"].append(
                        f"could not comment on the existing duplicate issue: {exc}")
        else:
            if candidates:
                # weaker (merely "related") matches don't block filing -- but the caller/reviewer
                # should still be able to see them without re-running the search themselves.
                report["warnings"].append(
                    "related (non-duplicate) issue(s) found, filed anyway: "
                    + ", ".join(f"#{c['ref']} ({c['score']})" for c in candidates))
            # #1471, the body half: composed in BEFORE creation, so the marker is atomic with the
            # issue existing at all -- there is no window in which a machine-filed issue is live on
            # the board declaring nothing. Deliberately AFTER `_duplicate_search` has already run
            # over `text`: the marker is metadata Sigma adds, not a description of the finding.
            #
            # WHAT THAT ORDERING ACTUALLY BUYS, measured rather than asserted -- the first version
            # of this comment claimed the marker "never joins the corpus", which is too strong. It
            # never joins the QUERY side. It DOES join the corpus side from the second filing
            # onward, via `mirror.normalize_issue`'s 500-char `body_excerpt`. So the shape is
            # asymmetric, not absent, and the asymmetry is the useful direction: on a genuine
            # near-duplicate pair the score moves 0.8452 -> 0.7695 (-0.076) with only the corpus
            # stamped, where stamping BOTH sides would move it to 0.8736 (+0.028). It errs towards
            # MISSING a duplicate rather than inventing one, and against a 0.45 duplicate line
            # neither effect changes an outcome. Same family as the shared-boilerplate inflation
            # that made decompose_check opt out of the search entirely (`dedup=False`, above), just
            # far smaller.
            if unit:
                stamped = feature_stamp.stamp_body(text, unit)
                text, unit = stamped.body, stamped.unit
                report["warnings"] += stamped.warnings
                report["unit"] = unit
            try:
                report["issue"] = source.create_dependency(heading, text, report["owner"], **create_kwargs)
                if not report["issue"]:
                    # #1203 shape (b): `gh` ran, raised nothing, but produced no usable issue number
                    # (GitHubSource._create_issue's own `return ... if number.isdigit() else None`)
                    # -- previously the one failure shape that left `warnings` completely empty.
                    # Still never raises; this only makes the existing warnings channel honest about
                    # it too.
                    report["warnings"].append(
                        "could not open the tracked issue: the source returned no issue number")
            except Exception as exc:                           # noqa: BLE001 - never block the park
                report["warnings"].append(f"could not open the tracked issue: {exc}")
            # #1471, the label half. Attached AFTER creation rather than handed to
            # `create_dependency`'s own `labels=`, which looks atomic and is the LOSSY route:
            # `gh issue create --label feature:x` fails the WHOLE create when the label does not
            # exist on the repo, so the follow-up would be lost in exactly the case this has to
            # survive. Attaching afterwards degrades to `body_only` -- the issue exists and declares
            # its unit where a human reads it, and `feature_labels.attach_at_pick` attaches the
            # label the first time that follow-up is itself picked. `report["issue"]` is checked
            # because a create that produced no number has nothing to label.
            if unit and report["issue"]:
                report["warnings"] += feature_stamp.attach(source, report["issue"], unit)
            # #1479: and the owner is told, HERE rather than at the gate, so the note can name the
            # issue they are being asked to promote. `tell_at_filing` writes nothing on an allowed
            # verdict, so the reading of the verdict stays in exactly one place -- the gate.
            # `report["issue"]` GATES BOTH, for `feature_stamp.attach`'s reason one line up: a
            # create that produced no number has nothing to promote, and telling an owner that an
            # issue of theirs is waiting when no issue exists is a note about a non-event.
            if ownership is not None and report["issue"]:
                # THE WHY-CLAUSE IS `feature_owner.refusal_clause`'s, NEVER A THIRD PHRASING OF IT.
                # This warning used to say "neither that unit's owner nor the owner of the board" on
                # BOTH arms -- so a unit's own owner, refused because the board is somebody else's,
                # was told they own neither. That is the same defect that was already fixed twice,
                # in the ledger note and in the issue comment, and it reappeared here because the
                # sentence was written out a third time. Now there is one function and three
                # subjects. `ownership.owner` already carries the registry's own spelling (`@handle`
                # in the design's example entries), so nothing prepends a second `@`; the LEDGER
                # half is normalised to a bare login by `_tell`, because every consumer of the `to`
                # field compares against `ledger.actor`.
                owner_module = _feature_owner()
                report["warnings"].append(
                    f"filed under unit {ownership_unit!r} as a proposal, not an actionable goal: "
                    + owner_module.refusal_clause(ownership, ownership_unit, ownership.repo,
                                                  "this account")
                    + ". "
                    + (f"{ownership.owner} has been asked through the ledger; they can promote "
                       f"this one issue, or authorise the whole unit once. "
                       if ownership.owner else "")
                    + ("This goal is ALSO blocked behind it, so it waits until that happens."
                       if blocks_goal else "Nothing is blocked behind it."))
                owner_module.tell_at_filing(sdlc_dir, goal, ownership_unit, ownership,
                                            issue=report["issue"],
                                            actor=owner_module.whoami(run))
    else:
        report["warnings"].append("backlog source cannot open issues — ledger entry only")

    # #466 review: gate on blocks_goal too, not solely same_area -- see the docstring above. Only a
    # genuine cross-area BLOCKING dependency (same_area=False AND blocks_goal=True, hand_off()'s own
    # always-pinned case) may write kind="handoff" -- that is the one kind ledger.outstanding() /
    # backlog_check._ledger_signals() treat as a real, confident block. Everything else is a "note",
    # regardless of same_area, so it can never be mistaken for one.
    if (not same_area) and blocks_goal:
        report["entry"] = ledger.safe_append(
            sdlc_dir, "handoff", goal, config=config, to=report["owner"], area=area,
            issue=int(report["issue"]) if str(report["issue"] or "").isdigit() else None,
            priority=priority, why=why, state="open")
    else:
        report["entry"] = ledger.safe_append(
            sdlc_dir, "note", key_goal, config=config, to=None if nogoal else report["owner"], area=area,
            issue=int(report["issue"]) if str(report["issue"] or "").isdigit() else None,
            priority=priority, why=why)

    # #726: tracked independently of report["issue"] -- whether the machine-readable marker ITSELF
    # actually got written, which is the one thing the final guard below needs to know. report["issue"]
    # alone conflates two different failure shapes: no issue existed to hold a marker (the #469 case),
    # and an issue exists but the source has no WORKING append_to_body at all (a working
    # create_dependency with no append_to_body -- LocalSource's own shape before #726 -- sailed
    # straight through a guard that only ever checked report["issue"]).
    marker_written = False
    if report["issue"] and source is not None and not nogoal:
        # two channels, two audiences (#376): a human-visible narrative comment, AND (blocks_goal
        # only) a machine-readable body marker so a future precheck() run can auto-skip the CURRENT
        # goal without a human re-stating what already happened. "Blocked by #N" is the exact phrase
        # backlog_check.py's _BLOCK_RE requires -- an earlier version of the narrative said
        # "Blocked on", which never matched at all; fixed here too so the human-visible text is
        # consistent with the machine-readable one, not just superficially similar.
        #
        # F14/#338: a resolved owner does not mean the assignment took -- create_dependency falls
        # back to opening the issue unassigned when gh rejects it (a team, most often) and records
        # which happened via last_assignee_applied. A source that predates this (or doesn't expose
        # it) defaults to True: its assignment always either took or raised, so there was never a
        # silent gap to report.
        assignee_applied = getattr(source, "last_assignee_applied", True)
        # #2133: `report["duplicate_of"]` (#1204) is set ONLY on the file-time-duplicate-search
        # reuse path a few dozen lines up -- an EXISTING issue that search matched, not one this
        # call opened. Narrating that path as "opened #N" below is exactly the defect #2133
        # records: a caller reads a reuse as a creation, the mistake is silent, and it exits 0.
        # `disposition` is the one word both branches vary on; everything else about the sentence
        # (who it's assigned to, whether it blocks) is unchanged, because a reused issue can be
        # genuinely reassigned/blocking too -- only WHICH verb describes what just happened differs.
        disposition = "matched existing" if report["duplicate_of"] else "opened"
        if blocks_goal:
            narrative = (f"Blocked by a `{area}` dependency — {disposition} #{report['issue']}"
                        + (f" and assigned to @{report['owner']}" if report["owner"] and assignee_applied else "")
                        + f" ({priority}). Parking this goal until it lands.")
        else:
            # deliberately does not match backlog_check._BLOCK_RE -- a non-blocking finding must
            # never read as a park-worthy dependency, in the human-visible channel either.
            narrative = (f"Related `{area}` issue filed — {disposition} #{report['issue']}"
                        + (f" and assigned to @{report['owner']}" if report["owner"] and assignee_applied else "")
                        + f" ({priority}).")
        if hasattr(source, "note"):
            try:
                source.note(str(goal), narrative)
            except Exception as exc:                            # noqa: BLE001
                report["warnings"].append(f"could not comment on the issue: {exc}")
        if blocks_goal and hasattr(source, "append_to_body"):
            try:
                source.append_to_body(str(goal), f"**Blocked by:** #{report['issue']}")
                marker_written = True
            except Exception as exc:                            # noqa: BLE001
                report["warnings"].append(
                    f"could not record the machine-readable blocker on the blocked issue's body: {exc}")
        # #1350: transition the CURRENT goal to sdlc:blocked immediately — not deferred to a later
        # pre-work cross-check. Gated on `marker_written`, not `blocks_goal` alone: if the
        # machine-readable "Blocked by" marker never actually landed, the goal isn't ACTUALLY
        # blocked in any enforceable sense (backlog_check._explicit_blockers() has nothing to
        # find), so transitioning its label here would be actively misleading. `hasattr` gates this
        # to sources that implement it (GitHub only, matching this epic's own scope) — a source
        # without it (LocalSource) degrades to exactly today's behavior.
        if marker_written and hasattr(source, "mark_blocked"):
            try:
                source.mark_blocked(str(goal))
            except Exception as exc:                            # noqa: BLE001
                report["warnings"].append(f"could not transition the goal to sdlc:blocked: {exc}")

    # #469/#726: blocks_goal=True has exactly ONE enforcement channel when same_area=True — the body
    # marker just above. Unlike (not same_area) and blocks_goal, which is ALSO backed by the ledger's
    # own kind="handoff" regardless of whether an issue exists (hand_off()'s own docstring: "the
    # ledger entry is written regardless, because a hand-off nobody can see is the bug this function
    # exists to fix"), same_area cannot reuse that fallback — kind="handoff" is reserved for a genuine
    # cross-area dependency precisely so it can never become a permanently-unanswered entry nobody was
    # ever meant to ack (#466 review, still true above: this block changes nothing about which kind
    # gets written). So when the body marker never actually landed, the caller's explicit
    # blocks_goal=True silently did not happen.
    #
    # #726: gated on `not marker_written`, not `not report["issue"]` (#469's original condition).
    # report["issue"] alone only ever caught HALF of "the marker never landed" -- the half where no
    # issue existed at all (no create_dependency, or the call raised). A source with a WORKING
    # create_dependency but no append_to_body (or one whose append_to_body itself raised) left
    # report["issue"] truthy, so that guard never fired even though the marker was never written
    # anywhere -- exactly LocalSource's shape before #726, discovered in #469's own post-PR review.
    # marker_written is the one signal that is honest about BOTH shapes with a single check. Never
    # raises (this function's own documented contract): surfaced the same way every other failure
    # here already is, through report["warnings"], which both CLI verbs (`open` and `track`) already
    # print to stderr unconditionally — no new surfacing mechanism, just the missing message.
    if blocks_goal and same_area and not marker_written:
        report["warnings"].append(
            "blocks_goal=True could not be honored: the \"Blocked by\" marker was never written to "
            "the goal's body (no issue number was available, or the source has no working "
            "append_to_body), and a same-area block has no ledger fallback (that channel is "
            "reserved for a genuine cross-area handoff) — this goal is NOT actually blocked")
    return report


def hand_off(sdlc_dir, config, goal, area, why, priority=DEFAULT_PRIORITY,
             title=None, source=None, run=None, target_unit=None):
    """Open the dependency, address it, record it. A thin, behavior-preserving wrapper: a hand-off is
    BY DEFINITION a blocking, cross-area, immediately-actionable dependency, so this pins
    `create_tracked_issue()`'s three axes accordingly and supplies the hand-off-specific title/body
    template (`issue_body()`, unchanged) — see `create_tracked_issue()` for the machinery this now
    shares with every other issue the loop creates. Returns a report dict; never raises.

    `issue` is None when the host could not create one (local backlog, no `gh`, or a failed call) —
    the ledger entry is written regardless, because a hand-off nobody can see is the bug this
    function exists to fix.

    `target_unit` (#1820): passed straight through to `create_tracked_issue` — see its own
    docstring. A cross-area dependency can belong to a different unit than the goal it was
    discovered from just as easily as a same-area follow-up can."""
    if source is None:
        try:
            source = sources.get_source(sdlc_dir, config)
        except Exception:                                       # noqa: BLE001 - create_tracked_issue
            source = None                                       # retries resolution and reports below

    heading, body = title, None
    if source is not None and hasattr(source, "create_dependency"):
        blocked_url = source.issue_url(goal) if hasattr(source, "issue_url") else ""
        heading = title or f"[{area}] dependency for {pathlib.Path(str(goal)).name}"
        body = issue_body(goal, area, why, blocked_url)

    return create_tracked_issue(sdlc_dir, config, goal, area, why,
                                 same_area=False, immediately_actionable=True, blocks_goal=True,
                                 priority=priority, title=heading, body=body,
                                 extra_labels=[dependency_label(config)],
                                 source=source, run=run, target_unit=target_unit)


def acknowledge(sdlc_dir, config, issue, state, why="", goal=None, area=None):
    """The other half. Reading a hand-off obliges an answer: taking it, needing time, declining it,
    or closing it out. `deferred` deliberately does NOT settle the hand-off — a promise to look later
    is not a resolution, and the team view keeps showing it.

    F22/#347: `issue` cannot key a LOCAL hand-off — `hand_off()` writes `issue=None` for one (no
    `gh`, or a local backlog), so `ledger.settlement_key()` falls back to `goal`. Settling that
    hand-off means writing an `ack` whose own key falls back the same way, which is why `goal` is
    accepted here: leaving `issue` falsy makes the line below store `issue=None` on the entry, so
    `settlement_key()` reads `goal` on BOTH sides and the two halves meet. Passing an `issue` still
    wins when present, matching `settlement_key()`'s own precedence exactly.

    `area` (#533, optional): narrows an issue-less ack to the ONE hand-off on `goal` carrying that
    area, instead of the default settle-every-area-on-this-goal fallback — see
    `ledger.settlement_key()`'s own docstring for the matching rule. Written as given, never
    validated here (the CLI dispatcher owns the warn-don't-refuse UX around a typo'd or ambiguous
    value; this function's only job is to write the entry)."""
    return ledger.safe_append(sdlc_dir, "ack", goal or f"issue-{issue}", config=config,
                              issue=int(issue) if str(issue).isdigit() else None,
                              state=state, why=why, area=area)


def _routing_clause(report):
    """#1551: what the one stdout line says when the finding was routed off this board. Empty on
    every ordinary filing, so no existing caller's output changes."""
    routing = report.get("routing") or {}
    if not routing.get("withheld"):
        return ""
    if routing.get("issue"):
        return (" — withheld (a Sigma finding); filed upstream as "
                f"{routing['upstream']}#{routing['issue']}")
    return " — withheld (a Sigma finding); raised in the ledger for you to forward"


def _issue_clause(report):
    """#2133: the fragment naming what happened to `report["issue"]`, shared verbatim by both
    `open` and `track`'s own stdout lines (previously two copies of the same literal). Before this,
    both printed `" as #N"` regardless of HOW #N came to be — including the #1204 dedup-reuse path,
    where #N is an EXISTING issue the file-time duplicate search matched, not one this call opened.
    `report["duplicate_of"]` is exactly that signal (set only on the reuse path, `None` on every
    path that created something or created nothing at all) — reporting a reuse as `as #N` is the
    same "reads as measured, isn't" shape #2133 records for the narrative comment."""
    if not report["issue"]:
        return " (no issue opened)"
    if report["duplicate_of"]:
        return f" as matched existing #{report['issue']}"
    return f" as #{report['issue']}"


USAGE = ("usage: handoff.py open <dir> <goal> --area A --why TEXT [--priority P --title T] | "
         "track <dir> <goal> --area A --why TEXT --queue actionable|queued "
         "--assignee same-area|cross-area --blocks yes|no "
         "[--priority P --title T --label L --body-file F] | "
         f"ack <dir> --issue N | --goal G [--area A] --state {'|'.join(ledger.STATES)} [--why TEXT]")


def main(argv):
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    if len(argv) >= 3 and argv[1] == "open":
        sdlc_dir, goal = argv[2], argv[3] if len(argv) > 3 else ""
        flags = ledger._flags(argv[4:])
        area, why = flags.get("area"), flags.get("why")
        if not goal or not area or not why:
            print("handoff.py open needs <goal> --area <area> --why <text> "
                  "[--target-unit NAME]", file=sys.stderr)
            return 2
        config = ledger._config(sdlc_dir)
        report = hand_off(sdlc_dir, config, goal, area, why,
                          priority=flags.get("priority", DEFAULT_PRIORITY),
                          title=flags.get("title"),
                          # #1820: optional, and its absence changes nothing -- see hand_off's own
                          # docstring and create_tracked_issue's `target_unit` doc for the scope
                          # boundary (operator/CLI-level only; the autonomous loop does not pass this).
                          target_unit=flags.get("target-unit"))
        for warning in report["warnings"]:
            print(f"handoff: {warning}", file=sys.stderr)
        print(f"handed off to {report['owner'] or '(unowned)'}"
              + _issue_clause(report)                          # #2133: "matched existing #N" on reuse
              # #1551: the one line a caller reads must not report "no issue opened" as though the
              # filing had failed, when it was routed away from this board on purpose. Same
              # argument as the `in unit` clause below: the failure mode of a silent decision is
              # that nobody notices it was made.
              + _routing_clause(report)
              # #1471: the inherited unit, on the one line a caller actually reads. The failure
              # mode of a stamp is SILENCE, so "which unit did this land in?" must not require
              # opening the issue to answer. It names what the issue is LABELLED with -- true on
              # every path this prints on; a body marker that could not be placed is a warning on
              # stderr immediately above, never an absence here.
              + (f" in unit {report['unit']}" if report["unit"] else "")
              + (f"; ledger {report['entry']['id']}" if report["entry"] else "; ledger off"))
        # #1599: a hand-off is the one entry whose whole purpose is that a NAMED PERSON sees it, and
        # this path is where a human opens one by hand -- outside any loop, so nothing else was ever
        # going to publish it. Unconditional (not gated on `report["entry"]`): a filing whose own
        # ledger write failed may still have OLDER unpublished entries behind it, and pushing those
        # is strictly better than skipping. See sync.publish_after_write for why this publishes
        # rather than starting a watcher, and why it never touches the exit code below.
        ledger.report_publish_state(sdlc_dir, config)
        # #1203: exit 0 regardless of whether the issue was actually created used to make a
        # failed filing look identical to a successful one -- the one line above was the ONLY
        # trace, easy to miss in a long autonomous run. `issue_attempted` (see create_tracked_issue's
        # own docstring) is what tells a genuine creation failure apart from the deliberately
        # degraded "nothing to try" cases (no source, or a source with no create_dependency at
        # all), which must keep exiting 0 exactly as before.
        if report["issue_attempted"] and not report["issue"]:
            print(f"handoff: issue creation failed -- goal {goal!r}, area {area!r}, intended "
                  f"owner {report['owner'] or '(unowned)'} -- nothing was filed (see the warning "
                  "above for the cause)", file=sys.stderr)
            return 1
        return 0
    if len(argv) >= 3 and argv[1] == "track":
        sdlc_dir, goal = argv[2], argv[3] if len(argv) > 3 else ""
        flags = ledger._flags(argv[4:])
        area, why = flags.get("area"), flags.get("why")
        # Every safety axis is a REQUIRED value flag, never a bare boolean -- _flags() would accept a
        # bare `--immediately-actionable` as "true", but a caller who forgets it would then silently
        # get "false", exactly the silent-default failure mode this whole design exists to close. A
        # missing OR misspelled value on any of the three is the same hard usage error, exit 2,
        # nothing written -- never a default.
        queue_map = {"actionable": True, "queued": False}
        assignee_map = {"same-area": True, "cross-area": False}
        blocks_map = {"yes": True, "no": False}
        queue, assignee, blocks = flags.get("queue"), flags.get("assignee"), flags.get("blocks")
        if (not goal or not area or not why or queue not in queue_map
                or assignee not in assignee_map or blocks not in blocks_map):
            print("handoff.py track needs <goal> --area <area> --why <text> "
                  "--queue actionable|queued --assignee same-area|cross-area --blocks yes|no "
                  "[--priority P --title T --label L1,L2 --body-file F --target-unit NAME]",
                  file=sys.stderr)
            return 2
        # #522: --body-file reads a file verbatim as the new issue's body -- the way the
        # `goal_decompose` file-mode meta-goal (and any other multi-paragraph tracked issue) hands
        # `track` a body too long for a CLI arg. Read BEFORE any create so a missing file can never
        # half-file an issue (ledger._flags parses it to the key "body-file", hyphen kept as-is).
        body = None
        body_file = flags.get("body-file")
        if body_file:
            try:
                body = pathlib.Path(body_file).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                # #522 review fix 7: UnicodeDecodeError is NOT an OSError subclass -- a binary or
                # wrongly-encoded file used to crash with a raw traceback instead of the same usable
                # refusal a missing file already gets.
                print(f"handoff.py track: could not read --body-file {body_file!r}: {exc}",
                      file=sys.stderr)
                return 2
        config = ledger._config(sdlc_dir)
        report = create_tracked_issue(
            sdlc_dir, config, goal, area, why,
            same_area=assignee_map[assignee], immediately_actionable=queue_map[queue],
            blocks_goal=blocks_map[blocks], priority=flags.get("priority", DEFAULT_PRIORITY),
            title=flags.get("title"), body=body,
            # --label takes a COMMA-SEPARATED list, so ONE filing can carry both the class label
            # and any further labels of the caller's OWN taxonomy (`--label
            # sdlc:followup,flaky-test`) -- the kit defines none of those and reads none of them
            # (#1602); `priority:` and `area:` are already derived from their own flags below. A
            # follow-up filed mid-run is only
            # worth filing if it is enqueue-ready: assigned and fully labelled at filing time, it
            # joins the backlog immediately instead of waiting for a human to triage it in the
            # morning. Split HERE, not in `ledger._flags`: that parser is shared by every verb in
            # the plugin and pinned since #541, so teaching IT about commas would silently change
            # how every flag everywhere reads one. Empty elements are dropped and each is stripped —
            # a trailing comma or a typed space must not become a nameless label.
            extra_labels=[part.strip() for part in (flags.get("label") or "").split(",")
                          if part.strip()],
            # #1820: optional, and its absence changes nothing -- see create_tracked_issue's own
            # `target_unit` doc for the scope boundary (operator/CLI-level only; the autonomous
            # loop's own SKILL.md-driven filing does not pass this).
            target_unit=flags.get("target-unit"))
        for warning in report["warnings"]:
            print(f"handoff: {warning}", file=sys.stderr)
        print(f"tracked to {report['owner'] or '(unowned)'}"
              + _issue_clause(report)                          # #2133: "matched existing #N" on reuse
              + _routing_clause(report)                        # #1551, as in `open` above
              + (f" in unit {report['unit']}" if report["unit"] else "")     # #1471, as in `open`
              + (f"; ledger {report['entry']['id']}" if report["entry"] else "; ledger off"))
        ledger.report_publish_state(sdlc_dir, config)       # #1599, as in `open` above
        # #1203: same exit-code discipline as `open` above -- see that block's comment.
        if report["issue_attempted"] and not report["issue"]:
            print(f"handoff: issue creation failed -- goal {goal!r}, area {area!r}, intended "
                  f"owner {report['owner'] or '(unowned)'} -- nothing was filed (see the warning "
                  "above for the cause)", file=sys.stderr)
            return 1
        return 0
    if len(argv) >= 3 and argv[1] == "ack":
        sdlc_dir = argv[2]
        flags = ledger._flags(argv[3:])
        issue, goal, state, area = (flags.get("issue"), flags.get("goal"), flags.get("state"),
                                    flags.get("area"))
        # F22/#347: a local/issue-less hand-off has no `<n>` to give — requiring --issue
        # unconditionally made it unanswerable forever. --goal is the symmetric alternative
        # (see acknowledge()); at least one of the two must identify which hand-off this answers.
        if not (issue or goal) or state not in ledger.STATES:
            print("handoff.py ack needs --issue <n> (or --goal <goal> [--area A] for a "
                  f"local/issue-less hand-off) --state {'|'.join(ledger.STATES)}", file=sys.stderr)
            return 2
        # #533: --area only means anything for an issue-less (goal-keyed) ack -- github mode settles
        # exactly by issue, unambiguous either way. NEVER refuses (a typo'd/ambiguous --area still
        # writes -- see acknowledge()'s own docstring and the module's dangling-ack-writes-freely
        # precedent): a hard refusal here would make every --area either a silent bypass or a hard
        # stop on the git-synced-but-not-yet-pulled path F22/#347 already had to solve once. Just
        # warn on stderr so a caller notices; stdout stays the bare entry id either way.
        if not issue:
            open_here = [h for h in ledger.outstanding(ledger.read_all(sdlc_dir))
                         if str(h.get("goal")) == str(goal)]
            live = sorted({h.get("area") for h in open_here if not h.get("issue")})
            # #770: a --goal ack writes issue=None, so settlement_key() keys it by (goal, area) and
            # can NEVER match an issue-BEARING outstanding hand-off (keyed by str(issue)) — e.g. one a
            # LocalSource minted with a real local issue number. The issue-less warnings below only
            # ever look at issue-LESS hand-offs (they filter `not h.get("issue")`), so such an ack
            # settled nothing, SILENTLY (exit 0, no stderr). Flag it loudly instead, naming the issue
            # number to pass via --issue — the one invocation that actually settles it. Scoped by
            # --area when given, matching the issue-less path's own scoping just below.
            orphaned = sorted({h["issue"] for h in open_here
                              if h.get("issue") and (not area or h.get("area") == area)})
            if orphaned:
                nums = ", ".join(f"#{n}" for n in orphaned)
                print(f"handoff: --goal cannot settle issue-bearing hand-off(s) {nums} on {goal} "
                      "(a --goal ack writes issue=None) — pass --issue <n> to settle one",
                      file=sys.stderr)
            if area:
                if area not in live:
                    print(f"handoff: --area {area!r} matched no outstanding hand-off on {goal} "
                          f"(live: {', '.join(live) or 'none'})", file=sys.stderr)
            elif len(live) > 1:
                print(f"handoff: settled {len(live)} hand-offs on {goal} ({', '.join(live)}) — "
                      "pass --area to settle one", file=sys.stderr)
        config = ledger._config(sdlc_dir)
        entry = acknowledge(sdlc_dir, config, issue, state, flags.get("why", ""),
                            goal=goal, area=area)
        print(entry["id"] if entry else "OFF (config: \"ledger\": {\"enabled\": true})")
        # #1599: the ledger is an ack's ONLY delivery channel -- unlike `open`/`track` it writes no
        # issue, so an unpublished ack answers nobody at all. It is also the verb most often typed
        # on a machine that is not looping, since answering a hand-off is exactly the thing you do
        # between runs.
        ledger.report_publish_state(sdlc_dir, config)
        return 0
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
