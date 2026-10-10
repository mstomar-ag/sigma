#!/usr/bin/env python3
"""compile_plan.py (#918) -- the "given a DECIDED plan, create it for real" layer of the
sigma-scope skill (epic #902, wave 1). Takes a structured plan a reasoning layer has already
decided on (titles, bodies, priorities, and dependency edges between SIBLING issues in the same
plan, plus an optional epic wrapper) and turns it into real GitHub (or local-goals) issues: real
`priority:P<n>` label AND board field (both sides), a real `epic`-labelled tracking issue when the
plan warrants one, and real "Blocked by #N" body markers wiring the genuine dependency edges.

This module does NOT decide what a plan should contain, whether two issues are duplicates, who gets
assigned, or whether work starts now -- that reasoning belongs to sibling sub-issues of the same
epic (#916 target resolution, #917 dedup, #919 assignment + execution paths, #920 orchestration).
This is purely the mechanical compiler: structure in, real tracked issues out.

REUSE DECISION (the issue's own first design question), read `handoff.py` and `sources.py` FIRST
before assuming either fits:

  `GitHubSource.create_dependency` / `LocalSource.create_dependency` -- YES, reused directly, for
  every actual issue-creation call in this module. They already handle the per-label `gh label
  create` (idempotent, and since #1917 non-destructive: it creates a label that is missing and
  leaves one that exists alone), the create-then-assign split (never combined -- an assignee `gh` rejects must not orphan
  the issue), and -- via `create_dependency`'s own trailing `_apply_custom_fields`/`_mirror_priority`
  call -- the priority label+field DUAL WRITE the acceptance criteria asks for. Nothing about that
  machinery needs reimplementing; this module only ever has to hand it the right `labels=[...]`.

  `handoff.create_tracked_issue` -- NOT reused, deliberately, after reading it closely (not assumed).
  Its `blocks_goal=True` marker channel writes "**Blocked by:** #<new issue>" onto the CALLING
  goal's OWN body (`source.append_to_body(str(goal), ...)`) -- i.e. "the goal I'm filing FROM is
  blocked by the issue I just made". That is exactly backwards for what #918 needs: issue B (a new
  plan issue) blocked by sibling issue A (ALSO a new plan issue), with no "calling goal" involved at
  all -- there is no `goal` for create_tracked_issue's marker to land on that would mean the right
  thing. Its `same_area`/`immediately_actionable`/`owners.owner_of` axes are also aimed at "who does
  this park on", a question #919 owns, not #918. Reusing it here would have silently mis-wired every
  sibling dependency edge (or required contorting its API into a shape it wasn't built for) -- so
  this module goes one layer down instead and reuses `create_dependency` directly, the same primitive
  `create_tracked_issue` itself is a thin wrapper over. `PROPOSED_LABEL`/`proposed_label()` (the
  #233 "queued but queryable" convention) is the one piece of `handoff.py` still reused as-is, live
  (see `goal_label` below) -- that mapping (not-yet-actionable -> a DISTINCT label, not just a
  missing one) is a real, already-solved problem this module would otherwise silently reintroduce.

CREATION-ORDER DECISION (the issue's second design question): issues are created in DEPENDENCY
(topological) order, blocker before dependant, and a dependant's "Blocked by #N" marker(s) are
embedded directly in its INITIAL body at creation time -- never patched in as a second pass. This
was chosen over "create all issues first, then patch markers in" because it is strictly safer:
embedding at creation is atomic with the issue existing at all, so there is no window where a
dependant issue is live on the board without the blocker marker that is supposed to gate it (a
two-phase patch has exactly that window if the patch step itself fails, or the run is interrupted
between phases). It also costs fewer `gh` calls (no `append_to_body` read-modify-write per edge).
The ONE deliberate exception is the epic's own back-reference to its subs (`_patch_epic_with_subs`
below): the epic is created FIRST, before any sub-issue has a real number, so there is no way to
embed that reference at creation time -- it is patched in afterward, once, via `append_to_body`,
exactly the shape `triage.py`'s own `enact()` already uses for the identical primitive. That does
not weaken the "always atomic" property for genuine dependency (`blocked_by`) edges, which is the
part `backlog_check._explicit_blockers()` actually gates auto-skip on -- the epic's tracking text is
informational only (an epic is never blocked; see `_EPIC_TRACK_TMPL` below for why it is worded to
never match `_BLOCK_RE` at all).

EPIC-OR-NOT DECISION (the issue's third design question): the epic wrapper is created iff the plan
supplies one AND the plan has MORE THAN ONE issue. A single-issue plan needs no wrapper regardless
of what the caller happened to hand in -- issue count is the objective, structural signal, so this
function stays correct even if the layer deciding what belongs in a plan (#920, not built yet)
doesn't itself special-case the single-issue case.

MULTI-BLOCKER MARKER SAFETY (the regex trap named in the issue and in
the #902 plan): `backlog_check._BLOCK_RE` captures exactly ONE `#N` per
trigger-phrase occurrence -- a comma-joined "Blocked by #12, #13" silently registers only #12.
`_blocker_marker_lines()` below NEVER joins numbers with a comma or any other separator on one
phrase occurrence -- it repeats the literal `"**Blocked by:** #{n}"` template (byte-identical to
the ONE other place in this codebase that writes this marker, `handoff.py`'s own literal string,
and to `triage.py`'s `_MARKER_TMPL`) once per blocker, one per line. Every test in
`tests/test_compile_plan.py` that exercises a multi-blocker issue asserts this against the LIVE
`backlog_check._BLOCK_RE` (not a hand-copied duplicate that could quietly drift from the real one).

FAILURE-MIDWAY DECISION: nothing is rolled back on a failure (there is no `gh` bulk-delete of
issues, and closing a just-created issue would just be a second, differently-shaped failure mode to
handle) -- this module instead guarantees the report is always an honest, complete account of what
did and didn't land. A failed issue is recorded in `report["failed"]`; anything (transitively)
`blocked_by` a failed OR skipped issue is never attempted at all -- created "half-blocked" (a
dependant whose marker would have to reference an issue that doesn't exist) is a strictly worse
failure mode than not creating it, so it is skipped and recorded in `report["skipped"]` instead. A
genuinely independent issue elsewhere in the same plan is unaffected.
"""
import importlib.util
import pathlib
import re

_HERE = pathlib.Path(__file__).resolve().parent


def _load_loop_script(name):
    """Cross-load a script from the sibling sigma-loop skill (skills/sigma-scope/scripts/ ->
    skills/sigma-loop/scripts/<name>.py) -- mirrors `sigma-doctor/scripts/doctor.py`'s own
    `_load_loop_script` (itself mirroring `backlog_check.py`'s `_load_velocity()`), the established,
    narrow, named exception to "don't reach across skill directories": `sources.py` is where
    `create_dependency`'s issue-creation machinery already lives, and `handoff.py`'s
    `PROPOSED_LABEL`/`proposed_label()` is the one already-solved "queued but queryable" convention
    this module reuses rather than reinventing (see the module docstring's REUSE DECISION). A
    module-local reimplementation of either would be exactly the hardened-sibling-divergence bug
    class this plugin's own docs already warn about (scrub.py)."""
    path = _HERE.parent.parent / "sigma-loop" / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


sources = _load_loop_script("sources")
handoff = _load_loop_script("handoff")   # PROPOSED_LABEL / proposed_label() only -- see module docstring
file_triage = _load_loop_script("file_triage")   # #1006: arm or park a plan issue (rubric slice 17)
qkind = _load_loop_script("qkind")               # #1006: the declared kind of a park at filing

#: Default priority when a plan issue (or the epic) doesn't name one -- byte-identical default to
#: `handoff.DEFAULT_PRIORITY`, so an unset priority means the same thing whichever layer created the
#: issue.
DEFAULT_PRIORITY = handoff.DEFAULT_PRIORITY

#: The label an epic tracking issue carries. Matches `triage.py`'s own `_EPIC_LABEL_SUBSTR`
#: substring check (`("epic", "needs-decomposition")`) so an issue this module creates is correctly
#: recognised as an epic by `_epic_matched_label`/`_bucket_epics`/`_bucket_shadow` everywhere else in
#: this plugin, not just by this module's own report.
EPIC_LABEL = "epic"

#: Byte-identical to the ONE existing call site (`handoff.py`'s own literal `f"**Blocked by:**
#: #{report['issue']}"`) and to `triage.py`'s `_MARKER_TMPL` -- no shared constant exists to import
#: (both of those re-type the literal rather than depending on each other), so this module does the
#: same rather than inventing new phrasing `_BLOCK_RE` would also match but a human reading three
#: markers side by side would see needlessly diverge.
_BLOCK_MARKER_TMPL = "**Blocked by:** #{n}"

#: The epic's own back-reference to a sub-issue. Deliberately does NOT use any of `_BLOCK_RE`'s
#: trigger words ("blocked by", "depends on", "depends upon", "needs", "after", "requires", "waiting
#: on") -- an epic is never itself blocked (it carries no `sdlc:goal` label so the loop never picks
#: it regardless; it closes only when every sub-issue closes, an explicit human/loop step, not an
#: auto-skip precondition), and misreading its own tracking text as a blocker would be the exact
#: "epic issue's own checklist gets misread as the epic being blocked" bug
#: the #902 plan already documents hitting once.
_EPIC_TRACK_TMPL = "Tracks #{n}"

#: The sub-issue's own back-reference to its epic. Also trigger-word-free for the same reason.
_PART_OF_EPIC_TMPL = "Part of epic #{n}."

#: The epic's own back-reference to the Dossier/Story (or Tech-side retrofit goal) it was produced
#: from (#1827, `docs/dossier-pipeline.md` §8). Unlike `_EPIC_TRACK_TMPL` above, the
#: originating issue's number is already known BEFORE the epic is created, so this is embedded in
#: the epic's initial body at creation time -- it never needs `_patch_epic_with_subs`'s two-phase
#: create-then-patch dance. Trigger-word-free for the same reason `_EPIC_TRACK_TMPL`/
#: `_PART_OF_EPIC_TMPL` are: verified live against `blocker_scan.TRIGGERS`
#: (`"blocked by"`/`"depends on"`/`"depends upon"`/`"needs"`/`"after"`/`"requires"`/`"waiting on"`)
#: -- "from" is not one of them, and the resulting text produces zero `_BLOCK_RE` matches.
_ORIGINATES_FROM_TMPL = "Originates from Story #{n}."

#: The id shapes a `.sdlc/design/<n>.md` write-up uses for its own NON-SLICE rows -- `BR-n` (Blast
#: radius), `D-n` (Doubts), `B-n` (Blockers), since #1975/#1976 `X-n` (Out of scope) and `PC-n`
#: (Premise check), and since #2028 `S-n` (Seeds). A `Seeds` row is the likeliest of the family to
#: be miswritten as an edge -- it is the one row type that names an unfinished piece of work, so
#: "slice 4 depends on S-2 being swept" is a sentence a pass can reach for; it is still not a
#: ticket. None of them is a slice, so none of them can ever be a
#: `blocked_by` key, and this module deliberately does NOT learn to resolve one (#1956): a design
#: Blocker has no ticket for a key to name, so "resolving" it could only mean filing a real,
#: pickable issue for something with no scope, or dropping the reference -- which is the bug. What
#: it does instead is RECOGNISE the shape, so the refusal below says where the reference belongs
#: rather than reading as an ordinary typo. `skills/sigma-goal-design/SKILL.md` §5 forbids these in
#: the `Depends on` column at the source; `skills/sigma-goal-review/SKILL.md` step 4b adjudicates one
#: rather than dropping it. Behaviour is unchanged either way: the same `ValueError`, raised before
#: any `gh` call, with zero side effects.
_DESIGN_ARTIFACT_ID_RE = re.compile(r"^(?:BR|PC|D|B|X|S)-\d+$")

#: Appended to the unknown-key refusal when, and only when, at least one unknown key has that
#: shape. Kept separate from the generic sentence so a plain typo is not buried under advice about
#: a document it has nothing to do with.
_DESIGN_ID_HINT = (
    " -- {ids!r} is the shape a `.sdlc/design/<n>.md` write-up gives its own Blast radius / Premise "
    "check / Seeds / Out of scope / Doubts "
    "/ Blockers rows, which are NOT slices: a slice's `Depends on` cell carries slice ids only, and "
    "a Blocker that gates a slice is recorded in that design's own Blockers entry, naming the slice "
    "it gates")


#: The directory `docs/dossier-pipeline.md` §7d-iii and `skills/sigma-goal-review/SKILL.md` step 4b
#: BOTH mandate for a Dossier-pipeline plan file (`.sdlc/state/goal-review/<n>.plan.json`; the skill
#: even ships the `mkdir -p` for it). Read as the two TRAILING parts of the plan's own parent path,
#: never as a substring -- `/tmp/state/goal-review-scratch/x.json` is not this directory. `main()`
#: turns `--forbid-priority` on by itself for a plan that lives here: see its docstring for why a
#: flag alone would just be a second honour-system rule.
_DOSSIER_PLAN_DIR = ("state", "goal-review")

#: The refusal's own sentence, in one place because two arms of the message would drift.
_PRIORITY_REFUSAL = (
    "compile_plan: this plan may not carry a 'priority' -- {offenders} -- but a Dossier-pipeline "
    "plan must OMIT it (docs/dossier-pipeline.md 7f: omit priority on the epic and on every child, "
    "and do not derive one either). A priority written here is judgement applied to a design "
    "artifact that never expressed one, and it lands as a real priority:P<n> label and a real board "
    "field, indistinguishable from one a human set. Delete the key; DEFAULT_PRIORITY ({default!r}) "
    "applies. Sequencing goes in blocked_by edges, which are checkable.")


def _refuse_priorities(plan):
    """Refuse a plan whose epic or any child carries a `priority` key (#2027), BEFORE anything is
    created and before the backlog source is even resolved.

    REFUSE RATHER THAN STRIP, the same adjudication #1956 made for a design-artifact id: of the two
    routes the issue named, stripping is "the bug moved into code where it is harder to see" -- it
    produces a board that looks right while the layer that wrote the number never learns it was
    wrong, and it silently discards a real `P0` alongside an invented `P2`. The recovery from the
    refusal is deleting keys from a local one-shot JSON file and re-running one command; the
    durable artifacts of the run (`sdlc:designed` on the story, `.sdlc/design/<n>.md`) are already
    written and are not lost.

    PRESENCE, not truthiness: an explicit `"P1"` is still the caller's own judgement, it just
    happens to collide with `DEFAULT_PRIORITY`. That is precisely the case a strip could never tell
    apart from an omission, and the reason this end refuses.

    EVERY offender in ONE message -- the run that motivated this (story #2017) had six, and naming
    them one at a time would cost six edit-and-re-run round trips, which is how a refusal stops
    being read. Offenders are named by `key`, falling back to plan index, because this runs BEFORE
    `_validate_and_order` and a plan may legally not have reached the point of having keys yet."""
    offenders = []
    epic_data = plan.get("epic")
    if isinstance(epic_data, dict) and "priority" in epic_data:
        offenders.append("the epic carries priority=%r" % (epic_data["priority"],))
    for index, item in enumerate(plan.get("issues") or []):
        if isinstance(item, dict) and "priority" in item:
            key = item.get("key")
            where = f"issue {key!r}" if key is not None else f"the issue at index {index}"
            offenders.append("%s carries priority=%r" % (where, item["priority"]))
    if offenders:
        raise ValueError(_PRIORITY_REFUSAL.format(offenders="; ".join(offenders),
                                                  default=DEFAULT_PRIORITY))


def _validate_and_order(items):
    """Structural validation + a deterministic topological (dependency) order, BEFORE any `gh` call
    is ever made -- a structurally invalid plan (duplicate key, an edge to a key outside this same
    plan, a self-edge, or a genuine cycle) fails closed with ZERO side effects, never a partial
    create. Raises `ValueError` naming the concrete problem; returns the ordered list of keys
    otherwise (blocker keys always precede everything that names them in `blocked_by`).

    Kahn's algorithm, with one deliberate refinement for determinism: among several keys that become
    "ready" (all blockers already ordered) at the same step, this keeps them in the plan's own
    original relative order rather than an arbitrary set order -- so re-running this against the
    same plan always produces the same creation order, which is what makes
    `test_creation_order_is_dependency_order_blocker_before_dependant` (and any future caller
    reasoning about `report["order"]`) meaningful rather than incidental.

    `blocked_by` may only reference OTHER keys in THIS SAME plan (the issue's own scope: "dependency
    edges to siblings in the same plan") -- an already-existing, external issue number is not this
    function's concern at all; a caller wanting that writes it directly into the issue's own `body`
    text, since it needs no order resolution (the number already exists)."""
    keys = [it.get("key") for it in items]
    missing = [i for i, k in enumerate(keys) if k is None]
    if missing:
        raise ValueError(f"compile_plan: issue(s) at index {missing!r} have no 'key'")
    seen, dupes = set(), []
    for k in keys:
        if k in seen and k not in dupes:
            dupes.append(k)
        seen.add(k)
    if dupes:
        raise ValueError(f"compile_plan: duplicate issue key(s) in plan: {dupes!r}")
    key_set = set(keys)

    edges_out = {}
    for it in items:
        k = it["key"]
        if not it.get("title"):
            raise ValueError(f"compile_plan: issue {k!r} has no title")
        blockers = list(it.get("blocked_by") or [])
        if k in blockers:
            raise ValueError(f"compile_plan: issue {k!r} cannot be blocked_by itself")
        unknown = [b for b in blockers if b not in key_set]
        if unknown:
            design_ids = [b for b in unknown
                          if isinstance(b, str) and _DESIGN_ARTIFACT_ID_RE.match(b)]
            hint = _DESIGN_ID_HINT.format(ids=design_ids) if design_ids else ""
            raise ValueError(
                f"compile_plan: issue {k!r} is blocked_by unknown key(s) {unknown!r} -- blocked_by "
                "may only reference OTHER issues in this same plan" + hint)
        edges_out[k] = blockers

    ordered, resolved, remaining = [], set(), list(keys)
    while remaining:
        ready = [k for k in remaining if all(b in resolved for b in edges_out[k])]
        if not ready:
            raise ValueError(f"compile_plan: dependency cycle among issue key(s) {sorted(map(str, remaining))!r}")
        ready_set = set(ready)
        for k in remaining:
            if k in ready_set:
                ordered.append(k)
                resolved.add(k)
        remaining = [k for k in remaining if k not in resolved]
    return ordered


def _blocker_marker_lines(numbers):
    """One `**Blocked by:** #{n}` line per blocker, never comma-joined -- see the module docstring's
    MULTI-BLOCKER MARKER SAFETY section. `numbers` is already the real, created issue numbers (this
    function is only ever called once every blocker in the list has actually been created)."""
    return [_BLOCK_MARKER_TMPL.format(n=n) for n in numbers]


def _append_extra(body, extra_lines):
    if not extra_lines:
        return body or ""
    return (body or "").rstrip() + "\n\n" + "\n".join(extra_lines)


def _create_epic(source, epic_data, report):
    """Create the epic tracking issue. Never carries the goal label (structural: `triage.py`'s own
    `_epic_matched_label`/`_bucket_shadow` logic assumes "an epic has no `sdlc:goal` label by
    definition" -- this is not a style choice, it's what keeps the loop from ever trying to pick an
    epic as a goal). Returns the new issue number, or None on any failure (recorded in
    `report["warnings"]`, never raised -- an epic that fails to create must not abort the sub-issues
    that still can)."""
    priority = epic_data.get("priority") or DEFAULT_PRIORITY
    labels = [f"priority:{priority}", EPIC_LABEL]
    body = epic_data.get("body") or ""
    originates_from = epic_data.get("originates_from")
    if originates_from is not None:
        body = _append_extra(body, [_ORIGINATES_FROM_TMPL.format(n=originates_from)])
    try:
        number = source.create_dependency(
            epic_data.get("title") or "Epic", body, None,
            labels=labels, goal_label=False)
    except Exception as exc:                                       # noqa: BLE001 - report, don't abort
        report["warnings"].append(f"could not create the epic tracking issue: {exc}")
        return None
    if number is None:
        report["warnings"].append("could not create the epic tracking issue: gh returned no issue number")
        return None
    return number


def _patch_epic_with_subs(source, epic_number, created_numbers, report):
    """The one legitimate two-phase (create-then-patch) step in this module -- see the module
    docstring's CREATION-ORDER DECISION for why every OTHER body write here is embedded at creation
    instead. Best-effort: a failure here means the epic exists but its checklist is incomplete,
    never a reason to un-create the sub-issues that already genuinely landed."""
    if not created_numbers:
        return
    lines = [_EPIC_TRACK_TMPL.format(n=n) for n in created_numbers]
    try:
        source.append_to_body(str(epic_number), "\n".join(lines))
    except Exception as exc:                                       # noqa: BLE001
        report["warnings"].append(
            f"epic #{epic_number} created, but could not patch its body with its sub-issues: {exc}")


def _triage_for(plan_item, priority, triage, config):
    """#1006: the triage outcome for one plan issue -> `Decision(kind, priority, reason)`, or None when
    triage is switched off (the legacy filing). A plan issue is a human-confirmed filing at its own plan
    priority: the deny-list is the only thing that can stop it (it parks). `triage` is the injected
    `decide(title, body, flags, config)` (default `file_triage.decide`). Never raises: any error parks."""
    decide = triage if triage is not None else file_triage.decide
    try:
        d = decide(plan_item["title"], plan_item.get("body") or "",
                   {"human_confirmed": True, "priority": priority}, config)
        return None if d.kind == "queue" else d
    except Exception:                                               # noqa: BLE001
        return file_triage.Decision("park", None, "triage could not decide")


def _park_child(source, number, reason, report, key):
    """#1006: park a freshly filed plan issue with a declared question kind. Never raises."""
    text = "Parked at filing: %s\n%s" % (reason, qkind.render_line("needs_decision"))
    try:
        source.park(str(number), text)
    except Exception as exc:                                        # noqa: BLE001
        report["warnings"].append(
            f"could not park {key!r} (#{number}) at filing ({exc}): it carries no goal label and no "
            "parked label until a human looks at it")


def compile_plan(sdlc_dir, config, plan, *, source=None, goal_label=False,
                 forbid_priority=False, triage=None):
    """Turn a DECIDED plan into real tracked issues. Never raises for a runtime/`gh` failure (every
    such failure is caught and reported in the returned dict); DOES raise `ValueError` for a
    structurally invalid plan (see `_validate_and_order`), and only BEFORE any issue is created, so
    a caller can trust that a raise means nothing happened at all.

    `plan` shape (a plain dict -- this repo's own established convention for machine-plan data, see
    `triage.py`'s `plan.json`; no schema-validation dependency):
        {
          "epic": {"title": str, "body": str, "priority": "P0".."P4" (optional),
                   "originates_from": <issue number> (optional, #1827 -- the Dossier/Story or
                   Tech-side retrofit goal this epic was produced from; written onto the epic's
                   own body, once, at creation, as "Originates from Story #{n}.")} | None,
          "issues": [
              {"key": <hashable, unique within this plan>, "title": str, "body": str,
               "priority": "P0".."P4" (optional, default DEFAULT_PRIORITY; FORBIDDEN outright
                when `forbid_priority=True` -- see that keyword below),
               "blocked_by": [<key>, ...] (optional, keys of OTHER issues in this SAME plan)},
              ...
          ],
        }

    `goal_label` (default False): whether every created PLAN issue (never the epic -- see
    `_create_epic`) is immediately actionable (`sdlc:goal`, auto-picked) or merely filed
    (`sdlc:needs-confirmation`, matching `handoff.py`'s #233 convention -- queued but queryable, never just a
    missing label). Deliberately a single caller-supplied axis, not decided by this module: WHETHER
    a freshly-compiled plan starts work now is #919's job (assignment + the three execution paths),
    not #918's -- this function only ever does what the caller explicitly asks for.

    `forbid_priority` (default False, #2027): whether a `priority` key anywhere in the plan (on the
    epic or on any child) is a REFUSAL rather than an input. OPT-IN deliberately, and the default
    is what keeps it that way: this same function is the sigma-scope path's compiler, and
    `skills/sigma-scope/SKILL.md` mandates "a real `P0`-`P4` priority" per issue there, so a blanket
    guard would break the caller it was never aimed at. The Dossier pipeline is the one that opts
    in, because `docs/dossier-pipeline.md` §7f forbids the key on that path -- and until this flag
    existed that rule was honour-system prose, which the 2026-09-01 validation run (story #2017)
    walked straight past on all six children with nothing to detect it. Checked as the FIRST
    statement of this function: before the empty-`issues[]` early return (an epic-only plan is
    still a plan), before `_validate_and_order`, and before the source is resolved -- so a raise
    still means nothing whatsoever happened.

    `source`: an object with `create_dependency`/`append_to_body` (the `GitHubSource`/`LocalSource`
    duck-typed contract `handoff.create_tracked_issue` already establishes). Resolved via
    `sources.get_source(sdlc_dir, config)` when not given, matching that function's own degrade
    path: a source that fails to resolve, or one that can't open issues at all, means nothing is
    created and `report["warnings"]` says why -- never a raise.

    `triage` (#1006, decision rubric slice 17): `decide(title, body, flags, config)` for each plan issue,
    default `file_triage.decide`. With triage on (the default) a plan issue is armed at its own plan
    priority (`sdlc:goal`, never the proposed label) unless the deny-list parks it, and the report gains
    a `parked` mapping `{key: reason}`. With `ai_filed.triage.enabled` false nothing here runs and the
    filing is exactly as before (no `parked` key).

    Returns a report dict, always fully populated:
        {"epic": <number> | None, "issues": {key: number, ...}, "failed": {key: error str, ...},
         "skipped": {key: reason str, ...}, "order": [key, ...actually attempted, in order],
         "warnings": [str, ...]}
    """
    if forbid_priority:
        _refuse_priorities(plan)   # raises ValueError; FIRST statement -- see the kwarg's docstring
    report = {"epic": None, "issues": {}, "failed": {}, "skipped": {}, "order": [], "warnings": []}
    items = list(plan.get("issues") or [])
    if not items:
        return report

    order = _validate_and_order(items)          # raises ValueError; zero side effects before this line
    by_key = {it["key"]: it for it in items}

    if source is None:
        try:
            source = sources.get_source(sdlc_dir, config)
        except Exception as exc:                                    # noqa: BLE001
            report["warnings"].append(f"no backlog source: {exc}")
            source = None
    if source is None or not hasattr(source, "create_dependency"):
        report["warnings"].append("backlog source cannot open issues -- nothing created")
        return report

    epic_data = plan.get("epic")
    epic_number = None
    if epic_data and len(items) > 1:
        epic_number = _create_epic(source, epic_data, report)
        report["epic"] = epic_number

    for key in order:
        item = by_key[key]
        blockers = list(item.get("blocked_by") or [])
        unmet = [b for b in blockers if b in report["failed"] or b in report["skipped"]]
        if unmet:
            report["skipped"][key] = (
                "not created: blocked_by sibling(s) that never landed: "
                + ", ".join(repr(b) for b in unmet))
            continue

        extra = []
        if epic_number is not None:
            extra.append(_PART_OF_EPIC_TMPL.format(n=epic_number))
        if blockers:
            extra += _blocker_marker_lines([report["issues"][b] for b in blockers])
        body = _append_extra(item.get("body"), extra)
        priority = item.get("priority") or DEFAULT_PRIORITY
        labels = [f"priority:{priority}"]
        decision = _triage_for(item, priority, triage, config)
        child_goal_label = goal_label
        if decision is not None:
            report.setdefault("parked", {})
            child_goal_label = decision.kind == "arm"
        elif not goal_label:
            labels.append(handoff.proposed_label(config))

        report["order"].append(key)
        try:
            number = source.create_dependency(item["title"], body, None,
                                               labels=labels, goal_label=child_goal_label)
        except Exception as exc:                                    # noqa: BLE001
            report["failed"][key] = str(exc)
            continue
        if number is None:
            report["failed"][key] = "gh returned no issue number"
            continue
        report["issues"][key] = number
        if decision is not None and decision.kind != "arm":
            report["parked"][key] = decision.reason
            _park_child(source, number, decision.reason, report, key)

    if epic_number is not None:
        _patch_epic_with_subs(source, epic_number, list(report["issues"].values()), report)

    return report


#: The CLI's own usage line, in ONE place -- it is printed from two arms below and #1919 added a
#: flag to it; two hand-typed copies had already drifted once.
_USAGE = ("usage: compile_plan.py <sdlc_dir> --plan <path.json> [--actionable] [--json] "
          "[--forbid-priority]")


def _json_report(report):
    """`report` as a JSON-safe dict. Only the KEYS move: `compile_plan`'s own docstring types a plan
    `key` as merely "hashable", so `issues`/`failed`/`skipped`/`order` may legally be keyed by int.
    `json.dumps` would stringify a dict's int keys silently and raise on a tuple one HALFWAY through
    writing the object; doing it here means the payload has the same shape whatever the plan used,
    and an unserializable key fails before a single byte reaches stdout."""
    return {"epic": report["epic"],
            "issues": {str(k): v for k, v in report["issues"].items()},
            "failed": {str(k): v for k, v in report["failed"].items()},
            "skipped": {str(k): v for k, v in report["skipped"].items()},
            "order": [str(k) for k in report["order"]],
            "warnings": list(report["warnings"]),
            **({"parked": {str(k): v for k, v in report["parked"].items()}} if "parked" in report else {})}


def _is_dossier_plan(plan_path):
    """Whether this plan file lives in the directory the Dossier pipeline mandates for it. The
    TRAILING path parts, compared as parts -- a substring test would fire on
    `/tmp/state/goal-review-scratch/x.json` and would be a different, sloppier rule than the one
    two documents actually write down. Any path that cannot be resolved at all is simply not this
    directory (fail open: the flag is still there, and this end exists to catch a caller who forgot
    it, never to invent a refusal out of a stat failure)."""
    try:
        parts = pathlib.Path(plan_path).resolve().parts
    except (OSError, ValueError):                                   # noqa: BLE001 - fail open
        return False
    return parts[-1 - len(_DOSSIER_PLAN_DIR):-1] == _DOSSIER_PLAN_DIR


def main(argv):
    """Thin CLI: `compile_plan.py <sdlc_dir> --plan <path.json> [--actionable] [--json]
    [--forbid-priority]`. Reads a plan as a JSON file, compiles it against the configured backlog
    source, and prints a short summary. Exit codes: 0 (every plan issue landed), 1 (at least one
    failed or was skipped), 2 (usage error, a structurally invalid plan, or a plan that carries a
    priority it may not -- nothing was created in any of those cases).

    `--forbid-priority` (#2027) turns on `compile_plan`'s own opt-in refusal of a `priority` key
    (see that function's docstring for why it is opt-in rather than blanket). It is ALSO turned on,
    without the flag, for any plan file living in `_DOSSIER_PLAN_DIR` -- and that backstop is the
    point of the change rather than a nicety. The rule it enforces was already written in
    `docs/dossier-pipeline.md` §7f and in `skills/sigma-goal-review/SKILL.md`, and a real run walked
    past both; a caller that can skip a paragraph of prose can skip a flag in the same paragraph, so
    a flag alone would have been the same honour system with an extra step. The plan's own PATH is
    contractual (both documents mandate it, `mkdir -p` included), so it is evidence about which
    pipeline produced the plan that does not depend on the producer remembering anything.

    `--json` (#1919) is the MACHINE channel, added because `skills/sigma-goal-review/SKILL.md` steps
    4b/4e are written against `report["epic"]` and `report["issues"]` while mandating this CLI,
    which had no way to emit either: stdout carried prose, in DEPENDENCY (topological) rather than
    plan order, and the warnings went to a different stream entirely. With the flag, stdout is
    EXACTLY one JSON object -- the whole report, `_json_report`'s shape -- and nothing else, so
    `json.loads` on the captured stdout is the entire parse. Ordering stops mattering because the
    consumer reads by KEY.

    Deliberately NOT changed by the flag: the stderr diagnostics (warnings, FAILED, SKIPPED) still
    print exactly where they always did, so a human running it by hand still sees why something did
    not land -- the JSON carries the same facts, it does not relocate them. Nor the exit codes. On a
    usage or unreadable-plan failure there is no report at all, so stdout stays EMPTY rather than
    emitting an empty object: "nothing was created" and "created nothing" have to stay
    distinguishable."""
    if argv[1:] in (["-h"], ["--help"]):
        print(_USAGE)
        return 0
    import json
    import sys

    if len(argv) < 2:
        print(_USAGE, file=sys.stderr)
        return 2
    sdlc_dir = argv[1]
    rest = argv[2:]
    plan_path, actionable, as_json, forbid_priority = None, False, False, False
    i = 0
    while i < len(rest):
        if rest[i] == "--plan" and i + 1 < len(rest):
            plan_path = rest[i + 1]
            i += 2
        elif rest[i] == "--actionable":
            actionable = True
            i += 1
        elif rest[i] == "--json":
            as_json = True
            i += 1
        elif rest[i] == "--forbid-priority":
            forbid_priority = True
            i += 1
        else:
            i += 1
    if not plan_path:
        print(_USAGE, file=sys.stderr)
        return 2
    forbid_priority = forbid_priority or _is_dossier_plan(plan_path)

    try:
        plan = json.loads(pathlib.Path(plan_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"compile_plan.py: could not read plan {plan_path!r}: {exc}", file=sys.stderr)
        return 2

    ledger = _load_loop_script("ledger")
    config = ledger._config(sdlc_dir)
    try:
        report = compile_plan(sdlc_dir, config, plan, goal_label=actionable,
                              forbid_priority=forbid_priority)
    except ValueError as exc:
        print(f"compile_plan.py: {exc}", file=sys.stderr)
        return 2

    for warning in report["warnings"]:
        print(f"compile_plan: {warning}", file=sys.stderr)
    if as_json:
        print(json.dumps(_json_report(report)))
    else:
        if report["epic"] is not None:
            print(f"epic: #{report['epic']}")
        for key, number in report["issues"].items():
            print(f"created {key!r} as #{number}")
    for key, err in report["failed"].items():
        print(f"FAILED {key!r}: {err}", file=sys.stderr)
    for key, why in report["skipped"].items():
        print(f"SKIPPED {key!r}: {why}", file=sys.stderr)
    return 1 if (report["failed"] or report["skipped"]) else 0


if __name__ == "__main__":
    import sys
    # The script-time floor: this run's wall time, recorded to the session resolved at exit.
    sys.exit(_load_loop_script("timing_store").timed_main(main, sys.argv, "compile_plan"))
