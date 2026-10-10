#!/usr/bin/env python3
"""assign.py (#919, wave 2 of epic #902) -- sigma-scope's assignment resolution + the three
execution paths. Acts on the SAME just-created plan a caller has ALREADY run through #918's
`compile_plan.compile_plan()`: this module never creates an issue itself, it only decides who owns
the freshly-created ones and what happens next.

TWO GENUINELY COUPLED THINGS, both against that same plan/report pair:

1. `resolve_assignment()` -- a three-way decision, PRESENTED, never silently auto-picked: the
   current user, a CODEOWNERS-resolved owner (`owners.owner_of`, reused live, not reimplemented --
   the issue's own explicit instruction), or one of the top-N currently-active repo members when
   CODEOWNERS doesn't resolve. See its own docstring and `_ACTIVE_MEASURE` for the "active" measure
   this module picked and why.

2. `execute()` -- once a caller has picked one login from `resolve_assignment()`'s own `options` AND
   one of the three paths below (mirrors `/sigma-triage plan`'s existing run-now-vs-file-and-stop
   choice at its own step 5 -- see `skills/sigma-triage/SKILL.md` -- reused as UX, not reinvented):

   - `PATH_FILE_AND_STOP` -- assign/leave-unassigned exactly as decided, write one ledger entry.
     Nothing else happens: the issues stay exactly as #918 left them (with triage on, #1006: armed
     at their plan priority or parked; with it off, `sdlc:needs-confirmation`, not actionable).
   - `PATH_START_HANDOFF` -- ALSO promotes every issue to `sdlc:goal` (see PROMOTION DECISION below),
     computes the wave workflow via `triage.schedule_waves` (reused directly, never a second
     scheduler), posts a ledger entry AND a GitHub comment on the plan's own anchor issue marking
     the hand-off starting point, embedding that precomputed workflow.
   - `PATH_START_SELF` -- same promotion + same `schedule_waves` reuse, but persists the workflow as
     a `.sdlc/plans/<issue>-<slug>.md` file (matching `900-blocker-priority-promotion.md`'s own
     convention -- see NAMING DECISION below) and then actually starts the drain (see DRAIN DECISION
     below), since self-assigned "start now" means the current session begins working immediately.

NODE-BUILDING DECISION (the issue's own second design question): `_nodes_from_plan` builds
`schedule_waves`' own `{"id","needs","priority","model","title"}` node shape directly from the SAME
`plan` dict `compile_plan.compile_plan()` was called with, plus the `report` it returned --
deliberately NOT `triage._build_nodes`, which reads a `--from-survey` snapshot file that cannot
possibly exist yet for issues created moments ago in this very run (confirmed by reading
`_build_nodes`/`_survey_index` directly, not assumed). `schedule_waves` itself needed NO adapter or
shim at all once fed nodes in that exact shape -- verified by reading its real signature
(`schedule_waves(nodes, cap, aliases=None, blocker_promotion_mode=...)`, `triage.py:1124`) rather
than assuming compatibility, exactly as the issue asks.

PROMOTION DECISION: `compile_plan.compile_plan()`'s own docstring is explicit that "WHETHER a
freshly-compiled plan starts work now is #919's job, not #918's" -- and #918 always files with
`goal_label=False` (`sdlc:needs-confirmation`, the #233 "queued but queryable" convention, reused live via
`handoff.proposed_label`), so the loop's own `next_pending()` (which filters on `sdlc:goal` alone)
can never see these issues at all until something promotes them. `_promote_to_goal` is that
something: ONE ATOMIC label swap per issue (`sources._swap_labels`, #1392 -- see that function's own
docstring for why the `gh issue edit --add-label X --remove-label Y` this used to run is not one
write at all), run ONLY for the two "start now" paths -- `PATH_FILE_AND_STOP` never calls it, so a filed-and-
stopped issue is untouched from how #918 left it, matching the issue's own "Nothing else happens"
wording literally.

ASSIGNMENT-ON-AN-EXISTING-ISSUE: `sources.py` has no reusable "assign an already-created issue"
method (`GitHubSource.create_dependency`'s own inline `--add-assignee` call only ever runs against
an issue it JUST created). Rather than add one to an already-shipped, already-tested module for a
single caller, `_apply_assignment`/`_promote_to_goal` reuse `source._run`/`source._swap_labels`
directly, cross-module -- the exact same "private-but-cross-module-reused, no new sources.py method"
precedent `triage._fetch_issue_state` already establishes for an identical shape of need (one more
`gh issue ...` subcommand against an issue that already exists).

NAMING DECISION (the issue's own third design question): the self-assigned plan file uses
`<issue>-<slug>.md`, NOT a bare `<issue>.md` -- verified by reading `slices.py`'s `goal_stem()` and
`manifest_path()` directly: a bare `<issue>.md` is where `slices.py` looks for a GOAL's OWN prose
plan when that goal ALSO gets a `<issue>.slices.json` beside it (intra-goal, worktree-parallel
slices). This epic's own batch of freshly-created issues gets its parallelism from GitHub-level
`schedule_waves` waves instead (inter-issue, not intra-goal), so there is no `.slices.json` this
file could ever pair with -- `<issue>-<slug>.md` (matching `900-blocker-priority-promotion.md`'s own
form) is the correct convention here, not the bare form `505.md`/`506.md` also use for a plain
single-goal fix with no slices file either. `<issue>` is the plan's own ANCHOR (see `_anchor_issue`):
the epic when the plan had one, else the first issue in creation/dependency order.

DRAIN DECISION (the issue's own acceptance criterion: "start the drain immediately... check what
mechanism the codebase already uses ... rather than inventing a new entrypoint"): there is NO
existing command that targets one specific plan file -- verified by reading every verb `loop.py`'s
own `_dispatch` exposes. `loop.py` always drains the WHOLE backlog by label/priority/dependency; a
`.sdlc/plans/**/*.md` file (this module's own included) is documentation a human reads, never a
pointer any drain mechanism consults -- `/sigma-triage`'s own SKILL.md says this explicitly about its
structurally identical `.sdlc/plans/triage/active.json`: "No new mechanism is needed -- the loop's
`next_pending` + `backlog_check` handle the sequencing." So the closest existing primitive, and the
one `_start_drain` below actually calls, is the same sequence `/sigma-triage`'s own "Start
now" flow documents: `loop.session_start` and `state.start_run` (the literal actions `loop.py
start` performs) then `loop._next(sdlc_dir, source, config)` (`loop.py next` -- claims and
returns the first ready goal, using the SAME lease/lock machinery a concurrent loop needs, not a
re-derived copy of it).

REUSE, NOT REIMPLEMENTATION, throughout: `owners.owner_of` (CODEOWNERS), `ledger.actor`/
`ledger.safe_append` (current-user resolution + the team ledger), `handoff.DEFAULT_PRIORITY`/
`handoff.proposed_label` (the #233 convention), `triage.schedule_waves`/`triage._cap_from_config`/
`triage._is_github`/`triage._gh_cfg` (wave scheduling + config plumbing, #900's own precedent),
`sources._priority_aliases`/`sources._blocker_promotion` (threaded into `schedule_waves` exactly as
`triage.plan_cmd` already does), `state.start_run`/`state.unsafe_goal_reason` (run bookkeeping +
path safety, `slices.py`'s own precedent), `loop._next` (the drain primitive). Nothing here
reimplements CODEOWNERS parsing, wave scheduling, or the claim/lease protocol.

FAIL-OPEN, LIKE EVERY SIBLING IN THIS TOOLCHAIN: neither public function raises for a runtime
failure (a missing `gh`, an unreachable repo, a read-only filesystem) -- every such failure is
caught and reported in the returned dict's own `warnings`. `execute()` DOES raise `ValueError` for a
caller-error `path` value (same posture `compile_plan.compile_plan` takes for a structurally invalid
plan: a programming mistake, not a runtime condition, fails loudly and immediately).

    python3 assign.py resolve <sdlc_dir> <area>
    python3 assign.py execute <sdlc_dir> --plan <plan.json> --report <report.json> --area <area>
        [--assignee <login>] --path file-and-stop|start-now-handoff|start-now-self
"""
import importlib.util
import json
import os
import pathlib
import re

_HERE = pathlib.Path(__file__).resolve().parent


def _load_loop_script(name):
    """Cross-load a script from the sibling sigma-loop skill -- mirrors `compile_plan.py`'s own
    `_load_loop_script` (itself mirroring `sigma-doctor/scripts/doctor.py` and `backlog_check.py`'s
    `_load_velocity()`): the established, narrow, named exception to "don't reach across skill
    directories" this whole epic already relies on."""
    path = _HERE.parent.parent / "sigma-loop" / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ledger = _load_loop_script("ledger")
owners = _load_loop_script("owners")
sources = _load_loop_script("sources")
handoff = _load_loop_script("handoff")     # DEFAULT_PRIORITY / proposed_label() reuse only
triage = _load_loop_script("triage")       # schedule_waves / _cap_from_config / _is_github / _gh_cfg
state = _load_loop_script("state")         # start_run() / unsafe_goal_reason() reuse
loop = _load_loop_script("loop")           # _next() -- the literal "loop.py next" primitive

SCHEMA = "sigma-scope-assign/v1"

PATH_FILE_AND_STOP = "file-and-stop"
PATH_START_HANDOFF = "start-now-handoff"
PATH_START_SELF = "start-now-self"
PATHS = (PATH_FILE_AND_STOP, PATH_START_HANDOFF, PATH_START_SELF)

#: The "active repo member" measure this module picked (issue's own open design question), and why.
#: RECENT ASSIGNMENT VOLUME, not recent COMMIT volume, for two concrete reasons:
#:   1. It already yields real GitHub LOGINS, directly usable for `gh issue edit --add-assignee`. A
#:      git commit author's local `user.name`/`user.email` has no reliable mapping to a GitHub login
#:      without a FURTHER, per-author network round-trip (`gh api search/users` or similar) -- commit
#:      volume would need that extra lookup before it could ever be used to actually assign anyone.
#:   2. It costs exactly ONE bounded `gh issue list` call, REST-search-backed like `triage.py`'s own
#:      `_resolve_missing_picks` -- never GraphQL. This repo's CI has repeatedly hit the GraphQL rate
#:      limit; a measure that adds its own further GraphQL-heavy calls (`gh api graphql` pagination
#:      over commits) would make that concretely worse for no correctness gain.
#: Recent COMMIT volume is a real, viable alternative -- arguably a more direct signal of who is
#: actively CODING right now, versus who merely carries open issue assignments -- and is the natural
#: next refinement if this measure proves too issue-centric in practice (e.g. a repo where triage
#: happens on a board nobody who's currently coding is assigned issues on yet). Deliberately not
#: built here, to keep this module's one `gh` dependency to the single bounded call below.
_ACTIVE_MEASURE = (
    "recent assignment volume: how many of the most recently touched issues (--state all, a bounded "
    "sample) each login is currently an assignee of, ranked highest first"
)


def _as_int(value):
    """Best-effort int coercion -- `report["issues"]` values are digit STRINGS (`create_dependency`'s
    own return contract, `sources.py`'s `_create_issue`), but `triage._build_nodes`'s established
    convention (and `schedule_waves`' own tie-break `n["id"]`) is real ints, so every node id and
    `needs` entry this module builds is coerced once, here, rather than sorting/comparing strings
    lexically (`"10" < "9"`) against a scheduler that elsewhere always compares numerically. Falls
    back to the raw value on anything non-numeric rather than raising -- defensive only; every real
    call site here only ever feeds this digit strings."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


# --------------------------------------------------------------------------- 1. assignment resolution


def _active_members(config, run=None, limit=3, sample_size=50, sdlc_dir=None):
    """Up to `limit` GitHub logins, ranked by `_ACTIVE_MEASURE`, or `[]` with a `warnings` entry on
    any degrade (non-github discovery mode, no `gh`, unparseable output) -- fail-open, matching every
    other opt-in `gh` read in this toolchain (`triage._resolve_missing_picks`'s own posture).
    ONE bounded list of the newest `sample_size` issues in every state (no label), REST first through
    `gh_api.list_issues_gh` (#895 slice 2c), with at most ONE `gh issue list --state all --limit N
    --json assignees` fallback on a rate limit / 5xx / transport failure (never in a cloud session).
    A page that is not a JSON list is an error, so it lands in the `except` arm below. `run` is
    injectable (default `sources._run_gh`) so tests stay hermetic; `sdlc_dir`, when given, shares the
    REST breaker/fallback log with the other reads."""
    if not triage._is_github(config):
        return [], ["active-member ranking needs github discovery mode -- none configured"]
    gh_cfg = triage._gh_cfg(config)
    run_ = run or sources._run_gh
    try:
        items = sources.gh_api.list_issues_gh(run_, ["assignees"], repo=gh_cfg.get("repo") or None, state="all",
                                              cap=sample_size, gql_run=run_, fetch=sources.fetch_issues_rest,
                                              sdlc_dir=sdlc_dir)
    except Exception as exc:                                        # noqa: BLE001 - fail-open
        return [], [f"could not rank active repo members: {exc}"]
    counts, order = {}, []
    for item in items:
        if not isinstance(item, dict):
            continue
        for a in (item.get("assignees") or []):
            login = (a or {}).get("login") if isinstance(a, dict) else None
            if not login:
                continue
            if login not in counts:
                counts[login] = 0
                order.append(login)
            counts[login] += 1
    ranked = sorted(order, key=lambda login: (-counts[login], login))
    return ranked[:max(0, limit)], []


def resolve_assignment(sdlc_dir, config, area, run=None, active_limit=3, sample_size=50):
    """The three-way decision, PRESENTED, never silently auto-picked -- `options` is the exact list a
    caller (a human, or a future orchestrating skill's own AskUserQuestion) chooses exactly one entry
    from; this function itself never picks.

    Tier 1, always offered -- "self": `ledger.actor(config, run)`, byte-identical in meaning to
    `handoff.create_tracked_issue`'s own `same_area=True` case ("I'm the one running this, assign it
    to me").

    Tier 2 -- "codeowners": `owners.owner_of` (REUSED live, not reimplemented -- the issue's own
    explicit instruction), offered whenever it resolves an owner for `area`.

    Tier 3 -- "active": up to `active_limit` currently-active repo members (see `_active_members`),
    offered ONLY when tier 2 comes back empty -- mirrors the issue's own phrasing ("...or one of the
    currently-active repo members IF CODEOWNERS doesn't resolve cleanly") and skips the one extra
    `gh` call whenever it isn't needed at all.

    Returns a pack (`schema`, `area`, `current_user`, `codeowners_owner`, `active_members`,
    `options`, `measure`, `warnings`); never raises."""
    warnings = []
    current_user = ledger.actor(config, run)

    codeowners_owner = None
    try:
        project_root = str(pathlib.Path(sdlc_dir).resolve().parent)
        codeowners_owner = owners.owner_of(project_root, area, config)
    except Exception as exc:                                        # noqa: BLE001 - roster is advisory
        warnings.append(f"could not read CODEOWNERS for area {area!r}: {exc}")

    active_members = []
    if not codeowners_owner:
        active_members, active_warnings = _active_members(
            config, run=run, limit=active_limit, sample_size=sample_size, sdlc_dir=sdlc_dir)
        warnings += active_warnings

    options = [{"choice": "self", "login": current_user}]
    if codeowners_owner:
        options.append({"choice": "codeowners", "login": codeowners_owner})
    for login in active_members:
        options.append({"choice": "active", "login": login})

    return {"schema": SCHEMA, "area": area, "current_user": current_user,
            "codeowners_owner": codeowners_owner, "active_members": active_members,
            "options": options, "measure": _ACTIVE_MEASURE, "warnings": warnings}


# --------------------------------------------------------------------------- 2. execution paths


def _anchor_issue(report):
    """The one issue every multi-issue action anchors on: the epic when the plan had one --
    `compile_plan`'s own "front door", already carrying every sub-issue's `Tracks #N` back-reference
    -- else the FIRST issue in creation/dependency order. `report["order"]`, not `report["issues"]`'s
    own dict iteration order: `order` is EXPLICITLY the dependency-ordered list per
    `compile_plan.compile_plan`'s own docstring; relying on dict insertion order instead would be an
    accident of the interpreter, not a documented contract."""
    if report.get("epic") is not None:
        return _as_int(report["epic"])
    issues = report.get("issues") or {}
    for key in (report.get("order") or []):
        if key in issues:
            return _as_int(issues[key])
    return None


def _nodes_from_plan(plan, report):
    """Build `schedule_waves`' own node shape (`{"id","needs","priority","model","title"}`) straight
    from the plan `compile_plan.compile_plan()` was called with plus the report it returned --
    deliberately NOT `triage._build_nodes` (see the module docstring's NODE-BUILDING DECISION). Only
    successfully-created issues (`report["issues"]`) ever become nodes; a `blocked_by` key that
    failed or was skipped can never appear in any surviving node's `needs` either, because
    `compile_plan` itself already guarantees that -- a dependant of a failed/skipped blocker is
    always ITSELF skipped, never created (`compile_plan.compile_plan`'s own FAILURE-MIDWAY
    DECISION) -- so no defensive re-check against `report["failed"]`/`report["skipped"]` is needed
    here beyond the plain `if b in id_by_key` membership test."""
    issues = report.get("issues") or {}
    id_by_key = {key: _as_int(number) for key, number in issues.items()}
    by_key = {item.get("key"): item for item in (plan.get("issues") or [])}
    nodes = []
    for key, number in id_by_key.items():
        item = by_key.get(key) or {}
        needs = sorted({id_by_key[b] for b in (item.get("blocked_by") or []) if b in id_by_key})
        nodes.append({"id": number, "needs": needs,
                      "priority": item.get("priority") or handoff.DEFAULT_PRIORITY,
                      "model": item.get("model"), "title": item.get("title") or f"#{number}"})
    nodes.sort(key=lambda n: n["id"])
    return nodes


def _apply_assignment(source, issue_numbers, assignee):
    """Best-effort assignment against each ALREADY-CREATED issue, one REST call per issue
    (`source._issue_add_assignees`, #895 slice 3a; the module docstring's ASSIGNMENT-ON-AN-EXISTING-ISSUE
    section predates it and describes the old `source._run` reuse).
    Per-issue try/except (one failure never stops the rest); never raises. A github-incapable
    `source` (no `_issue_add_assignees` -- e.g. `LocalSource`) degrades to a single warning and assigns
    nothing, matching `_active_members`'s own github-only posture."""
    if not hasattr(source, "_issue_add_assignees"):
        return [], ["assignment needs github discovery mode -- source has no _issue_add_assignees"]
    assigned, warnings = [], []
    for number in issue_numbers:
        try:
            source._issue_add_assignees(number, assignee)       # REST first through gh_api (#895 3a)
            assigned.append(number)
        except Exception as exc:                                    # noqa: BLE001 - best-effort
            warnings.append(f"could not assign @{assignee} to #{number}: {exc}")
    return assigned, warnings


def _promote_to_goal(source, issue_numbers, config, *, triaged=False, parked=()):
    """Flip each issue from filed-but-not-actionable to immediately actionable: add the source's own
    configured goal label (default `sdlc:goal`), remove `handoff.proposed_label(config)` (default
    `sdlc:needs-confirmation`) -- ONE ATOMIC swap per issue. See the module docstring's PROMOTION
    DECISION for why this step exists and why `PATH_FILE_AND_STOP` never calls it.

    #1392: routed through `sources.GitHubSource._swap_labels` instead of the `gh issue edit
    --add-label X --remove-label Y` this used to run. That command is NOT one write: it was measured
    (this repo, live) to dispatch FOUR HTTP requests 0.67ms apart, IN PARALLEL, with no ordering and
    no atomicity -- so a partial application left the issue carrying either BOTH labels (the
    half-promoted `sdlc:goal` + `sdlc:needs-confirmation` state, which `_fetch_pending` now refuses
    to pick) or NEITHER (zero-lifecycle-label limbo, invisible to every label query the kit makes).
    This was the last non-atomic lifecycle write outside the recovery paths: #1391 step 2 converted
    all four of `GitHubSource`'s own transitions and missed this one, because it lives in a
    different skill.

    `_swap_labels` also brings retries this call never had, and RAISES on final failure rather than
    returning a success it did not achieve -- the per-issue `except` below turns that into the same
    per-issue warning the old code produced, so `execute()`'s contract is unchanged: one failed
    promotion never stops the rest."""
    if triaged:
        # #1006: the report came from a triage-on compile, so every child is already armed (`sdlc:goal`) or
        # parked at filing and nothing here adds or removes a membership label. A parked child is NOT
        # promoted: starting work is not a way around a park.
        held = {_as_int(n) for n in parked}
        return [n for n in issue_numbers if n not in held], \
               ["#%s stays parked (see its park comment); /sigma-unpark is the way out" % n
                for n in issue_numbers if n in held]
    if not (hasattr(source, "_swap_labels") and hasattr(source, "_repo_args")):
        return [], ["promotion to goal needs github discovery mode -- source has no _swap_labels"]
    goal_label = getattr(source, "goal_label", "sdlc:goal")
    proposed = handoff.proposed_label(config)
    promoted, warnings = [], []
    for number in issue_numbers:
        try:
            source._swap_labels(str(number), add=[goal_label], remove=[proposed])
            promoted.append(number)
        except Exception as exc:                                    # noqa: BLE001 - best-effort
            warnings.append(f"could not promote #{number} to {goal_label!r}: {exc}")
    return promoted, warnings


def _workflow_lines(waves):
    """One line per wave: how many issues, whether they're safe to run in parallel, and which ones --
    shared, byte-identical rendering for both the hand-off comment and the self-assigned plan file,
    so the two never describe the same computed schedule differently."""
    lines = []
    for i, wave in enumerate(waves, start=1):
        ids = ", ".join(f"#{n['id']}" for n in wave)
        note = " -- no edges between these, safe to run in parallel" if len(wave) > 1 else ""
        lines.append(f"Wave {i} ({len(wave)} issue(s)){note}: {ids}")
    return lines


def _dependency_lines(nodes):
    """One `#<blocked> blocked by #<blocker>` line per edge, NEVER comma-joined across several
    blockers on one line -- the exact multi-blocker regex trap `compile_plan._blocker_marker_lines`
    already guards against (`backlog_check._BLOCK_RE` only captures ONE `#N` per trigger-phrase
    occurrence). Deliberately reuses the literal phrase "blocked by" -- the SAME wording
    `compile_plan`'s own `_BLOCK_MARKER_TMPL` already wrote onto these issues' real bodies -- so this
    prose only ever reinforces an edge that is ALREADY a genuine, already-written marker; it can
    never assert a blocking relationship that isn't already true and already recorded."""
    lines = []
    for n in nodes:
        for need in n["needs"]:
            lines.append(f"#{n['id']} blocked by #{need}")
    return lines


def _handoff_comment(assignee, issue_numbers, waves):
    lines = [f"Starting hand-off to @{assignee}: {len(issue_numbers)} issue(s) across "
            f"{len(waves)} wave(s). Precomputed workflow (priority + dependency order):", ""]
    lines += _workflow_lines(waves)
    return "\n".join(lines)


def _ledger_why(path, assignee, issue_count, anchor):
    who = f"@{assignee}" if assignee else "left unassigned"
    verb = {PATH_FILE_AND_STOP: "filed and stopped",
           PATH_START_HANDOFF: "started now, handed off",
           PATH_START_SELF: "started now, self-assigned"}[path]
    anchored = f", anchored on #{anchor}" if anchor is not None else ""
    return f"{verb}: {issue_count} issue(s), {who}{anchored}"


def _slugify(text, max_len=40):
    text = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return text[:max_len].rstrip("-") or "plan"


def _plan_title(plan, report, anchor):
    """The plan file's own heading title: the epic's title when the anchor IS the epic, else the
    anchor issue's own title from the plan's `issues` array."""
    if report.get("epic") is not None and _as_int(report["epic"]) == anchor:
        return (plan.get("epic") or {}).get("title") or f"plan {anchor}"
    by_key = {item.get("key"): item for item in (plan.get("issues") or [])}
    issues = report.get("issues") or {}
    for key in (report.get("order") or []):
        if key in issues and _as_int(issues[key]) == anchor:
            return (by_key.get(key) or {}).get("title") or f"plan {anchor}"
    return f"plan {anchor}"


def _plan_path(sdlc_dir, anchor, title):
    """`.sdlc/plans/<anchor>-<slug>.md` -- see the module docstring's NAMING DECISION. Defends the
    constructed stem with `state.unsafe_goal_reason` (the same shared path-safety validator
    `slices.py`'s own `manifest_path` uses) even though `anchor` is always a bare digit and `_slugify`
    already strips everything but `[a-z0-9-]`, so the combined stem cannot structurally contain a
    path-traversal sequence -- belt-and-suspenders, matching this codebase's own standing bias
    (`triage._validate_slug`'s docstring) toward reusing the shared validator rather than trusting a
    constructor's own narrowing logic never to regress."""
    stem = f"{anchor}-{_slugify(title)}"
    if state.unsafe_goal_reason(stem):
        stem = f"{anchor}-plan"
    return pathlib.Path(sdlc_dir) / "plans" / f"{stem}.md"


def _render_plan_md(anchor, title, assignee, area, nodes, waves, generated_at):
    """`.sdlc/plans/<anchor>-<slug>.md`'s content -- mirrors `900-blocker-priority-promotion.md`'s
    own shape (see that file for the single-goal precedent) with a multi-issue table in place of its
    prose, since this plan covers several freshly-created issues, not one."""
    wave_of = {n["id"]: i for i, wave in enumerate(waves, start=1) for n in wave}
    who = f"@{assignee}" if assignee else "no one (left unassigned, an explicit choice)"
    assignment_line = f"Self-assigned to {who}" if assignee else who.capitalize()
    lines = [f"# Plan — #{anchor}: {title}", "",
            "## Assignment",
            f"{assignment_line} (area: `{area}`) via sigma-scope's assignment resolution "
            "(#919) — \"start now, self-assigned\" path.", "",
            "## Issues", "", "| Issue | Wave | Priority | Title | Blocked by |", "|---|---|---|---|---|"]
    for n in nodes:
        needs = ", ".join(f"#{x}" for x in n["needs"]) or "—"
        lines.append(f"| #{n['id']} | {wave_of.get(n['id'], '—')} | {n['priority'] or '—'} | "
                     f"{n['title']} | {needs} |")
    lines.append("")

    dep_lines = _dependency_lines(nodes)
    if dep_lines:
        lines.append("## Dependency map")
        lines += [f"- {d}" for d in dep_lines]
        lines.append("")

    lines.append("## How to run")
    lines += _workflow_lines(waves)
    lines += ["",
             "This repo has no single \"drain against one specific plan file\" command — a plan file",
             "under `.sdlc/plans/` is documentation, matching this file's own naming convention (see",
             "`900-blocker-priority-promotion.md`). The real drain target is the issues themselves:",
             "each one above now carries `sdlc:goal`, its `priority:P<n>` label, an assignee, and",
             "(for anything past wave 1) a real `**Blocked by:** #N` body marker — the same native",
             "primitives `/sigma-triage`'s own \"Start now\" step already relies on.",
             "- Continue draining with `loop.py next .sdlc` (or `/sigma-loop`) once the current wave",
             "  lands; a later wave's blocker marker parks it automatically until then.", ""]

    lines += ["## Status",
             f"Drain started {generated_at} by {who} — {len(nodes)} issue(s) across "
             f"{len(waves)} wave(s)."]
    return "\n".join(lines) + "\n"


def _start_drain(sdlc_dir, config, source, run=None):
    """The literal, existing mechanism this codebase already uses to "start a drain" -- see the
    module docstring's DRAIN DECISION. `state.start_run` (== `loop.py start`) then `loop._next` (==
    `loop.py next`, claims and returns the first ready goal via its own real lease/lock machinery).
    Register the same session marker that `loop.py start` registers before resetting the cursor;
    the admission cap now reads that marker as its authoritative count.
    Never raises: a failure at either step is recorded in the returned dict's own `warnings`; the
    plan file `execute()` already wrote stays a completely valid, actionable artifact regardless."""
    result = {"run_started": False, "picked_kind": None, "picked": None, "warnings": []}
    try:
        session_pid = os.getppid()
        loop.session_start(sdlc_dir, session_pid)
        state.start_run(sdlc_dir)
        result["run_started"] = True
    except Exception as exc:                                        # noqa: BLE001 - fail-open
        result["warnings"].append(f"could not start the run: {exc}")
        return result
    try:
        kind, payload = loop._next(sdlc_dir, source, config, session_pid=session_pid)
        result["picked_kind"], result["picked"] = kind, payload
    except Exception as exc:                                        # noqa: BLE001 - fail-open
        result["warnings"].append(f"could not pick the first goal: {exc}")
    return result


def execute(sdlc_dir, config, plan, report, area, assignee, path, *, source=None, run=None, now=None):
    """Given #918's `(plan, report)` pair, resolve assignment + one of the three execution paths.

    `plan` -- the SAME decided plan dict handed to `compile_plan.compile_plan()`.
    `report` -- that SAME call's own returned report (`epic`, `issues`, `failed`, `skipped`,
        `order`, `warnings`).
    `area` -- the CODEOWNERS area this plan belongs to (ledger bookkeeping only; assignment itself
        was already decided by the caller via `resolve_assignment()` before this is ever called).
    `assignee` -- the resolved GitHub login to assign, or `None`/falsy to leave every issue
        unassigned (a caller-sanctioned choice, not a failure).
    `path` -- one of `PATH_FILE_AND_STOP` / `PATH_START_HANDOFF` / `PATH_START_SELF`.

    Raises `ValueError` for an unrecognised `path` (a caller bug, same posture
    `compile_plan.compile_plan` takes for a structurally invalid plan). Never raises for any runtime
    failure -- everything else degrades into the returned dict's own `warnings`.

    Returns a report dict: `{"path", "area", "assignee", "anchor_issue", "assigned", "promoted",
    "workflow", "comment_posted", "ledger_entry", "plan_file", "drain", "warnings"}`."""
    if path not in PATHS:
        raise ValueError(f"assign.execute: unknown path {path!r} -- expected one of {PATHS}")

    result = {"path": path, "area": area, "assignee": assignee, "anchor_issue": None,
              "assigned": [], "promoted": [], "workflow": None, "comment_posted": False,
              "ledger_entry": None, "plan_file": None, "drain": None, "warnings": []}

    order = report.get("order") or []
    issues = report.get("issues") or {}
    issue_numbers = [_as_int(issues[k]) for k in order if k in issues]
    if not issue_numbers:
        result["warnings"].append(
            "nothing to act on -- the plan report has no successfully created issues")
        return result

    anchor = _anchor_issue(report)
    result["anchor_issue"] = anchor

    if source is None:
        try:
            source = sources.get_source(sdlc_dir, config)
        except Exception as exc:                                    # noqa: BLE001
            result["warnings"].append(f"no backlog source: {exc}")
            source = None

    if assignee and source is not None:
        assigned, assign_warnings = _apply_assignment(source, issue_numbers, assignee)
        result["assigned"] = assigned
        result["warnings"] += assign_warnings

    waves = None
    if path in (PATH_START_HANDOFF, PATH_START_SELF):
        if source is not None:
            triaged = "parked" in report          # written only by a triage-on compile (#1006)
            held = [issues[k] for k in (report.get("parked") or {}) if k in issues]
            promoted, promote_warnings = _promote_to_goal(source, issue_numbers, config,
                                                          triaged=triaged, parked=held)
            result["promoted"] = promoted
            result["warnings"] += promote_warnings
        nodes = _nodes_from_plan(plan, report)
        cap = triage._cap_from_config(config)
        aliases = sources._priority_aliases(config)
        bp_mode = sources._blocker_promotion(config)
        try:
            waves = triage.schedule_waves(nodes, cap, aliases=aliases, blocker_promotion_mode=bp_mode)
            result["workflow"] = {"cap": cap, "waves": [[n["id"] for n in w] for w in waves]}
        except ValueError as exc:
            result["warnings"].append(f"could not compute a wave schedule: {exc}")

    entry = ledger.safe_append(
        sdlc_dir, "note", str(anchor) if anchor is not None else "sigma-scope-plan", config=config,
        to=assignee, area=area, issue=anchor if isinstance(anchor, int) else None,
        why=_ledger_why(path, assignee, len(issue_numbers), anchor))
    result["ledger_entry"] = entry["id"] if entry else None

    if path == PATH_START_HANDOFF:
        if anchor is not None and waves is not None and source is not None and hasattr(source, "note"):
            narrative = _handoff_comment(assignee, issue_numbers, waves)
            try:
                source.note(str(anchor), narrative)
                result["comment_posted"] = True
            except Exception as exc:                                # noqa: BLE001
                result["warnings"].append(f"could not comment on #{anchor}: {exc}")

    if path == PATH_START_SELF:
        if anchor is not None and waves is not None:
            title = _plan_title(plan, report, anchor)
            generated_at = ledger._stamp(now)
            content = _render_plan_md(anchor, title, assignee, area, nodes, waves, generated_at)
            plan_path = _plan_path(sdlc_dir, anchor, title)
            try:
                plan_path.parent.mkdir(parents=True, exist_ok=True)
                plan_path.write_text(content, encoding="utf-8")
                result["plan_file"] = str(plan_path)
            except OSError as exc:
                result["warnings"].append(f"could not write the plan file: {exc}")
        if source is not None:
            result["drain"] = _start_drain(sdlc_dir, config, source, run=run)

    return result


# --------------------------------------------------------------------------- CLI


def _flag_value(argv, name):
    for i, token in enumerate(argv):
        if token == name and i + 1 < len(argv):
            return argv[i + 1]
    return None


USAGE = ("usage: assign.py resolve <sdlc_dir> <area> | "
         "execute <sdlc_dir> --plan <plan.json> --report <report.json> --area <area> "
         f"[--assignee <login>] --path {'|'.join(PATHS)}")


def main(argv):
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    import sys

    if len(argv) >= 4 and argv[1] == "resolve":
        sdlc_dir, area = argv[2], argv[3]
        config = ledger._config(sdlc_dir)
        print(json.dumps(resolve_assignment(sdlc_dir, config, area), ensure_ascii=False, sort_keys=True))
        return 0

    if len(argv) >= 3 and argv[1] == "execute":
        sdlc_dir = argv[2]
        rest = argv[3:]
        plan_path, report_path = _flag_value(rest, "--plan"), _flag_value(rest, "--report")
        area = _flag_value(rest, "--area") or ""
        assignee = _flag_value(rest, "--assignee")
        assignee = None if assignee in (None, "none", "") else assignee
        path = _flag_value(rest, "--path")
        if not plan_path or not report_path or path not in PATHS:
            print("usage: assign.py execute <sdlc_dir> --plan <plan.json> --report <report.json> "
                  f"--area <area> [--assignee <login>] --path {'|'.join(PATHS)}", file=sys.stderr)
            return 2
        try:
            plan = json.loads(pathlib.Path(plan_path).read_text(encoding="utf-8"))
            report = json.loads(pathlib.Path(report_path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"assign.py: could not read --plan/--report: {exc}", file=sys.stderr)
            return 2
        config = ledger._config(sdlc_dir)
        result = execute(sdlc_dir, config, plan, report, area, assignee, path)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 1 if result["warnings"] else 0

    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv))
