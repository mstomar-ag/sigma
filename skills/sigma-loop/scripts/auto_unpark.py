"""#1129/#1394: the reconciliation sweep for `sdlc:blocked` GitHub issues whose recorded
`blocked by #N` targets have all closed. ON by default since #1394 (`sources.DEFAULT_AUTO_UNPARK_MODE`),
which is also when it stopped resuming `sdlc:parked` issues -- a park is a human's (see
`compute_unpark_actions`). The history below describes the #1129 design it grew from.

The bug this fixes: `next()`/`next-batch` only ever pick issues carrying `sdlc:goal`. Parking on a
`blocked-by` finding (`backlog_check.decide()` -> `loop.py`'s `precheck`) removes `sdlc:goal` and
adds `sdlc:parked` -- the goal permanently exits the picker's candidate pool. `backlog_check.
_explicit_blockers` already correctly re-derives blocker freshness on every call (a ref only counts
as blocking if it's STILL OPEN) -- so IF a parked goal were ever re-examined, it would correctly see
a closed blocker as no longer blocking. But nothing ever re-examines one automatically: the only
code path that undoes a park (drop `sdlc:parked`, re-add `sdlc:goal`) is `triage.py`'s `enact`, the
third stage of a human-driven `survey` -> `plan --pick` -> `enact --apply` campaign, which the
autonomous loop never invokes.

This module glues two already-correct, already-existing pieces together -- no new machinery:
`backlog_check._explicit_blockers`'s own freshness re-check, and `triage.py`'s existing "v1-lite
unpark" action pair (add `sdlc:goal`, drop `sdlc:parked`) via `triage.apply_actions`/
`_execute_action`, reused live, not reimplemented.

Deliberately narrow, on purpose: a parked issue with NO machine-detectable "blocked by #N"
reference anywhere in its own title+body or comments is left completely untouched, whatever else
it was parked for (duplicate / obsoleted-by / needs_decision / a human's own manual "on purpose"
park, e.g. a deliberate sequencing checkpoint) -- this sweep only ever reverses the ONE park class
it can prove is stale, never guesses at any other. A parked issue referencing MULTIPLE blockers is
swept only once every one of them has closed.

#1186 FIX: the safety claim above used to be defeated by this module's OWN park comment. Every park
Sigma ever posts (`sources.park`/`fail`, via `_offboard`) opens with a fixed prefix --
`sources.PARK_COMMENT_PREFIX` ("Parked by Sigma — needs human review: "), and the sibling
`FAIL_COMMENT_PREFIX` -- and the word "needs" in that fixed prose is ITSELF a `_BLOCK_RE` trigger.
Left unfiltered, any `#N` the CALLER's own free-text `reason` happened to mention within the
match window read as a phantom "machine-detectable blocker reference" that had nothing to do with
a real dependency -- so a `needs_decision` park recording "PR #1234 is not approved yet" (say) was
never actually the "no machine-detectable reference" case the paragraph above promises; it silently
WAS one, on the strength of Sigma's own boilerplate. `backlog_check._strip_offboard_prefixes`
now strips exactly that fixed prefix span (never the reason that follows it) before ANY
`_BLOCK_RE`/`_referenced_blocker_refs` scan sees the text -- restoring the paragraph above to
actually true, not just not-yet-proven-false. This is a SEPARATE filter from `_is_exempt`/
`KEEP_PARKED_MARKER` below: that one is an opt-out a human deliberately posts; this one is
structural, unconditional, and needs no opt-in or human action at all.

Config: `discovery.auto_unpark.mode` -- 'on' (default since #1394) | 'off'. See `sources._auto_unpark`. GitHub
mode only, the identical reach `discovery.blocker_promotion` (#900) already has, and for the
identical reason: a local goal's own "blocked by #N" reference is a file path, never a bare issue
number `_BLOCK_RE` can match, so `LocalSource` has nothing to ever sweep.

`sweep_unpark()` itself is UNGATED -- it always computes and (optionally) applies, exactly like
`triage.enact()` is unconditional once invoked. The `discovery.auto_unpark.mode` config gate lives
solely at the one call site that runs with nobody asking for it THIS run (`loop.py`'s `_next()`,
see `_auto_unpark_sweep` there) -- a direct call here (this module's own CLI, or a supervising
cron) is itself the opt-in, the same posture `triage.py enact --apply` already has with no separate
config flag of its own.

Post-#1129-review cooldown (`result["unparked"]`): this sweep decides on TEXTUAL SHAPE alone (a
`blocked by #N` phrase + the `sdlc:parked` label) -- it has no way to tell a genuinely-stale block
apart from a human's deliberate "on purpose" checkpoint that happens to share that same shape,
since intent isn't something either can express through it. `sweep_unpark()`'s result therefore
names exactly which issues it actually flipped `sdlc:parked` -> `sdlc:goal` THIS pass
(`result["unparked"]`, apply-mode only -- a dry-run mutates nothing, so it is always `[]` there),
so `loop.py`'s `_next()` can hold that SPECIFIC goal back from its own SAME-call pick (see
`_next()`'s own docstring) -- a real window for a human to read the audit comment and re-park it
before autonomous work resumes, not just an after-the-fact notice. That window is a mitigation for
a block this sweep could not have known was deliberate ahead of time -- it is not detection.

#1152 adds actual detection for the case where a human already KNOWS a specific park is deliberate:
`KEEP_PARKED_MARKER` (below), an HTML-comment marker posted as a COMMENT on the goal issue --
mirrors #830's `DISMISS_MARKER` convention in `backlog_check.py` exactly (a comment, never a body
rewrite; see that marker's own design-rationale comment for why). A goal carrying it is skipped by
`compute_unpark_actions` before ANY `_BLOCK_RE`/`_referenced_blocker_refs` scan runs at all, on
every call regardless of `discovery.auto_unpark.mode` -- not a post-hoc filter over an already-
computed finding, so no phrase-detection quirk in the park text can ever defeat an opt-out a human
already recorded. See the README's `auto_unpark` section for the adopter-facing walkthrough of both
mechanisms together.

    python3 auto_unpark.py sweep <sdlc_dir> [--apply]
"""
import json, pathlib, sys, importlib.util

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


sources = _load("sources")
legacy = _load("legacy")        # #239: a keep-parked opt-out written under the previous name
mirror = _load("mirror")
backlog_check = _load("backlog_check")
gate_hold = _load("gate_hold")    # #1005: a park that declares a gate kind is never resumed here
triage = _load("triage")          # apply_actions / _execute_action -- reused live, not reimplemented
blockers = _load("blockers")      # #1393: classify() -- the ONE membership-safety policy, not a second
blocker_scan = _load("blocker_scan")   # #2532: closed_state() -- the rule this file's own
                                        # _ref_is_open originated, now shared rather than sole-owned


_ISSUE_FIELDS = "number,title,body,labels"


def _fetch_parked_issues(source):
    """Every OPEN issue carrying `source.parked_label` OR `source.goal_blocked_label` --
    number/title/body/labels, deduped by issue number, TWO `gh issue list` calls (one per label,
    each independently FAIL-OPEN).

    #1358 (against #1350's own gap): `mark_blocked` (#1350) transitions an in-progress goal
    straight to `goal_blocked_label` (default `sdlc:blocked`) -- a DISTINCT state from
    `parked_label` (`_offboard`'s own `already_blocked` guard exists specifically so the two never
    coexist on one issue, see `mark_blocked`'s docstring). Before this, a goal that landed in
    `sdlc:blocked` had no discovery path anywhere in this sweep -- it could never be found here,
    and so could never auto-resume even after its blocker closed, unlike the pre-#1350 equivalent
    (a plain `sdlc:parked` issue, which this sweep has always found). Two separate `--label`
    queries, not one `--search "label:A,B"` OR query: `gh issue list --label` ANDs repeated flags
    together (requires ALL named labels present), so an OR needs either two calls or raw search
    syntax -- two calls keeps this module's existing `_repo_args()`/`--json`/`--limit` shape
    identical for both, at the cost of one more list call on every sweep pass (page-list calls are
    the cheap half of this module's own cost accounting; the `issue view`/comment calls inside
    `compute_unpark_actions` below dominate whenever anything is actually found).

    Deduped by `number` even though the two labels are meant to be mutually exclusive on any one
    issue today (see above) -- a hand-fed fixture, or a future label-model change, returning the
    same issue for both queries must never be double-counted into `compute_unpark_actions`.

    FAIL-OPEN independently per label: any error on ONE query (transport, bad JSON, a non-list
    payload) drops only that query's results, never the other's -- a `sdlc:blocked` outage must not
    also blind this pass to genuinely-parked issues, and vice versa. A missed pass is caught by the
    next one; this must never be what breaks a pick.

    Thin wrapper over `_fetch_parked_issues_status` (below) that drops the completeness flag --
    every EXISTING caller of this name only ever wanted the issues (partial data is the safe
    direction for `compute_unpark_actions`: a missed parked issue is just fewer unparks this pass,
    caught next time). `sweep_unpark` is the one caller that cannot afford that same assumption for
    the newer `compute_blocking_actions` (#1351 review finding, BLOCKING -- see
    `_fetch_parked_issues_status`'s own docstring) and uses the status-aware form instead."""
    issues, _complete = _fetch_parked_issues_status(source)
    return issues


def _fetch_parked_issues_status(source):
    """Same fetch as `_fetch_parked_issues` above, plus a `complete` flag: `True` only if BOTH
    label queries succeeded (no exception, a list payload) -- `False` the moment either one didn't,
    even though `issues` itself still carries whatever the other query DID return (unchanged,
    partial-data-is-fine behaviour for `compute_unpark_actions`).

    #1351 review finding (BLOCKING): `compute_blocking_actions`'s `to_remove` diff
    (`currently_blocking - live_blocker_refs`) cannot tell "genuinely nothing is blocked this pass"
    apart from "we couldn't check" from an empty `issues` list alone -- and unlike
    `compute_unpark_actions`, where partial data only ever costs a missed ADD (safe), a wrongly-empty
    `blocked_issues` here costs a wrongful REMOVE of every currently-correct `sdlc:blocking` label,
    a real write, from a plain transient outage. `sweep_unpark` reads `complete` to skip the whole
    blocking-reconciliation pass (not just report partial data) whenever either query failed, so a
    network blip costs one skipped pass, exactly the guarantee this module's other primitives
    already give -- never a destructive write."""
    seen = {}
    complete = True
    for label in (source.parked_label, source.goal_blocked_label):
        try:
            # #895 slice 2c: REST first, newest first, ONE `gh issue list` fallback (see sources._list_issues)
            issues = source._list_issues(_ISSUE_FIELDS.split(","), labels=[label], state="open")
        except Exception:
            complete = False
            continue
        if not isinstance(issues, list):
            complete = False
            continue
        for it in issues:
            if isinstance(it, dict) and "number" in it:
                seen[it["number"]] = it
    return list(seen.values()), complete


def _fetch_blocking_issues(source):
    """#1351: every issue number currently carrying `source.blocking_label` -- the "before"
    picture `compute_blocking_actions` below diffs against to decide what to remove (self-healing)
    versus what's already correctly present (add-only-what's-missing, matching every other action
    computation in this module). FAIL-OPEN: any error (transport, bad JSON, a non-list payload)
    returns an empty set this pass -- a transient outage here must only ever cost one skipped
    reconciliation pass, never a raise that could take the whole sweep down with it.

    `--state all`, deliberately NOT `--state open` (#1351 review finding, MINOR): the label's own
    removal condition is "no OPEN issue references it any more" -- a blocker that resolves by
    CLOSING (rather than by its referencing text being edited away) must still be visible to this
    query so the diff below can strip the now-stale label from it. An open-only query drops a
    blocker from every future snapshot the instant it closes, and the label would then sit there
    forever, unrevisited -- `gh issue edit --remove-label` works identically on a closed issue, so
    there's no cost to reaching it here."""
    try:
        issues = source._list_issues(["number"], labels=[source.blocking_label], state="all")
    except Exception:
        return set()
    if not isinstance(issues, list):
        return set()
    return {str(it["number"]) for it in issues if isinstance(it, dict) and "number" in it}


def _label_names(issue):
    return {l.get("name") for l in (issue.get("labels") or []) if isinstance(l, dict)}


def _ref_is_open(source, ref, cache):
    """Live state for issue/PR `ref` (`gh issue view --json state,stateReason`), memoized in `cache`
    across one sweep pass -- several parked issues can share the same blocker, and memoizing keeps
    the cost at one call per DISTINCT ref, never one per (parked issue, ref) pair.

    PR-aware (#1186): `gh issue view` resolves PR numbers too, and a PR's `state` can be `MERGED` as
    well as `OPEN`/`CLOSED` -- a bare `state != "CLOSED"` reads a MERGED pr as still open (never
    resolves) and a CLOSED-without-merge one as resolved (resolves immediately), both backwards:
    merging is what actually resolves whatever a PR was blocking, so a closed-without-merge PR --
    the referenced change never landed -- must NOT read as resolved just because it shares the same
    bare "CLOSED" state a genuinely-closed ISSUE also reports. `stateReason` is the free,
    single-call signal that tells the two apart: an ISSUE-only GraphQL field, populated
    ("COMPLETED"/"NOT_PLANNED") whenever an issue is actually closed, but EMPTY for a PR in any
    state (open, closed, or merged) -- confirmed empirically against this repo's own real closed
    issues and closed/merged PRs.

      - state == "MERGED"                   -> resolved (merging is what resolves a PR blocker)
      - state == "CLOSED" and no stateReason -> a closed-without-merge PR -> still open/blocking
      - otherwise (OPEN, or a CLOSED issue with a stateReason) -> state != "CLOSED", unchanged

    Unreadable/unrecognised -> True (treated as still open): the safe direction, unchanged. This
    sweep's whole job is a write it cannot easily undo (re-adding `sdlc:goal` may let the loop pick
    the goal back up before a human ever notices); a blocker whose true state this pass could not
    confirm must never be read as license to fire it."""
    if ref in cache:
        return cache[ref]
    try:
        data = source._read_issue(ref, ["state", "stateReason"])     # #895: REST first, GhApiError -> open
        state = (data.get("state") or "").upper()
        state_reason = data.get("stateReason") or ""
        # #2532: this three-way rule is now `blocker_scan.closed_state` -- extracted so `promote.py`
        # and `blockers.py` share it rather than each reinventing their own (unfixed) copy. `open`
        # is the logical negation of `closed`; the docstring above still carries the full case-by-
        # case reasoning, since `closed_state`'s own docstring restates it from the CLOSED side.
        result = not blocker_scan.closed_state(state, state_reason)
    except Exception:
        result = True
    cache[ref] = result
    return result


# --- #1152: a human's DELIBERATE park should survive the sweep forever, on request --------------
# Mirrors #830's `DISMISS_MARKER` convention (`backlog_check.py`) exactly: an HTML-comment marker
# POSTED AS A COMMENT on the goal issue (never a body rewrite -- same reasoning that marker's own
# design-rationale comment gives for why a body rewrite is a far more invasive ask than "leave a
# comment"). Unlike `DISMISS_MARKER`, this marker carries no `kind=`/`ref=` pair: #830 dismisses ONE
# specific finding against ONE specific ref, but this opts a goal OUT OF THE SWEEP ENTIRELY,
# whatever it was parked for -- there is nothing finding-shaped to key it to, so bare presence on
# the issue is the whole signal.
KEEP_PARKED_MARKER = "sigma:keep-parked"


def keep_parked_comment(reason=""):
    """The narrative + machine marker a human (or a script/agent acting on their behalf) posts as a
    COMMENT on a parked goal issue to opt THIS ONE goal out of the sweep, permanently, regardless of
    `discovery.auto_unpark.mode` -- mirrors `backlog_check.dismiss_comment`'s own narrative-then-
    marker shape exactly, so the two "already handled, on purpose" conventions in this repo read
    consistently. Posting it needs no new gh-mutating code: the existing `loop.py note <sdlc_dir>
    <goal> "<this text>"` verb already does it."""
    reason = (reason or "").strip()
    tail = f" — {reason}" if reason else ""
    return (f"Auto-unpark opt-out: this park is a deliberate checkpoint, not a stale block{tail} "
            f"— exempted from the auto_unpark sweep. <!-- {KEEP_PARKED_MARKER} -->")


def _is_exempt(raw_comments):
    """True when ANY of `raw_comments` (the RAW, unfiltered, uncapped per-comment text -- see
    `backlog_check._fetch_scrubbed_comments`) carries `KEEP_PARKED_MARKER`. Deliberately the raw
    text, never a capped/derived excerpt: same reasoning as #830 finding 4 -- a marker sitting past
    an excerpt cap in a long comment must never be silently missed.

    `compute_unpark_actions` calls this FIRST, before any `_BLOCK_RE`/`_referenced_blocker_refs`
    scan runs at all -- an issue this returns True for is skipped outright, never reaching the
    textual-shape match, so no phrase-detection quirk in the park text can ever defeat an opt-out a
    human already recorded (#1152's own acceptance criterion: exclusion before matching, never a
    post-hoc filter on an already-computed finding)."""
    return legacy.has_marker("\n".join(raw_comments or []), KEEP_PARKED_MARKER)


def compute_unpark_actions(sdlc_dir, config, source, issues, run=None, open_cache=None):
    """(actions, resolved) over every issue in `issues` (already known to carry `parked_label` OR
    `goal_blocked_label` -- see `_fetch_parked_issues`, #1358). `actions` is the enact-shaped,
    action list -- ONE `swap-label` per eligible issue (#1392), plus a card move where a board
    exists. #1394: only `goal_blocked_label` issues are eligible; a `parked_label` one is skipped
    outright, because this sweep decides on textual shape and a park is a human's decision. See the
    skip's own comment below. `resolved` is `{issue: [ref, ...]}` naming which now-closed blocker(s) made it
    eligible, for the audit-trail comment `sweep_unpark` posts on apply.

    Step 0, per issue, before anything else: `_is_exempt` on the issue's raw comment text. A goal
    carrying `KEEP_PARKED_MARKER` is skipped right here -- BEFORE any `_BLOCK_RE`/
    `_referenced_blocker_refs` scan even runs -- never touched regardless of what its own park text
    would otherwise match. See `KEEP_PARKED_MARKER`'s own comment above for why this must run first,
    not as a filter over an already-computed finding.

    Eligibility for every non-exempt issue: `backlog_check._referenced_blocker_refs` scans the
    issue's own raw title+body PLUS its comment text (capped, dismissal-comment-filtered, and
    Sigma's own park/fail boilerplate PREFIX stripped -- #1186; `backlog_check.
    _strip_offboard_prefixes` -- the same three-step pipeline `_explicit_blockers` already scans when
    a goal is first considered for park, so this reads the identical text a human would see quoted
    back in the park comment, MINUS the fixed prose Sigma itself wrote around it; derived from
    the SAME raw-comment fetch the exemption check above already made, never a second `gh` call) for
    every `#N` a `blocked by/depends on/needs/after/requires/waiting on` phrase names, regardless of
    N's CURRENT state.

    - Empty refs -> not this sweep's concern: parked for a different reason (or a marker that has
      since been edited away), left untouched.
    - Non-empty -> re-run `_explicit_blockers` itself, UNCHANGED, over a minimal, freshly-fetched-
      this-pass corpus of just those refs' live open/closed state (`_ref_is_open`). Still finding
      anything means at least one referenced blocker is still open -- leave parked. Finding nothing
      means every one of them has closed -- eligible.

    `open_cache`: optional, shared `_ref_is_open` memo dict (#1351 review finding, notable-severity
    efficiency gap) -- `sweep_unpark` builds ONE cache and passes the SAME dict into both this call
    and `compute_blocking_actions`'s, since a ref's open/closed state within one sweep pass is
    identical to either caller; a fresh `{}` (the default when omitted, e.g. every direct-call test
    in this module) is exactly the prior, unshared behaviour."""
    actions, resolved = [], {}
    open_cache = open_cache if open_cache is not None else {}
    for it in issues:
        n = str(it.get("number"))
        goal_doc = {"ref": n, "raw": (it.get("title") or "") + "\n" + (it.get("body") or "")}
        raw_comments = backlog_check._fetch_scrubbed_comments(sdlc_dir, config, goal_doc, run=run)
        if _is_exempt(raw_comments):
            continue                                    # #1152: deliberate park, opted out -- untouched
        if gate_hold.declared_gate_kind(raw_comments):
            continue                                    # #1005: scope_hold / owner_hold wait for the registry edit
        extra_text = backlog_check._cap_join_excerpts(
            backlog_check._strip_offboard_prefixes(
                backlog_check._filter_dismissal_comments(raw_comments)))
        refs = backlog_check._referenced_blocker_refs(goal_doc, extra_text)
        if not refs:
            continue
        docs = [{"ref": r, "open": _ref_is_open(source, r, open_cache)} for r in refs]
        if backlog_check._explicit_blockers(goal_doc, docs, extra_text):
            continue                                    # at least one referenced blocker is still open
        names = _label_names(it)
        # #1394: a PARKED goal is never resumed here. The sweep decides on TEXTUAL SHAPE alone --
        # its own docstring says it "has no way to tell a genuinely-stale block apart from a human's
        # deliberate 'on purpose' checkpoint that happens to share that shape" -- which was tolerable
        # while it was opt-in, and is not now that it runs by default. A human parking something with
        # "waiting on the pricing call, see #123" would have that park silently reversed the moment
        # #123 closed, making the guarantee this release documents to its adopters ("nothing
        # automatic ever un-parks a human's park") false.
        #
        # Scoped to `goal_blocked_label`, the sweep reverses only the loop's OWN state, so
        # default-on is safe by construction rather than by an opt-out marker nobody remembers to
        # post. `/sigma-unpark` is the route for a parked goal, with a person answering the question
        # that caused the park.
        #
        # `compute_blocking_actions` deliberately still scans BOTH: a parked issue can name a live
        # blocker, and that blocker genuinely blocks something. Only RESUMING narrows.
        if source.parked_label in names:
            continue
        # #1392: ONE atomic `swap-label` action, replacing the add/remove PAIR this used to emit.
        # The stakes are highest exactly here: this is the automatic sweep, running unattended, and
        # a half-applied unpark left `sdlc:goal` added with `sdlc:parked` never removed -- which
        # `_fetch_pending` excludes, so the goal stayed just as unpickable as before while now
        # carrying two primary lifecycle labels for the reconciler to find later. See
        # `triage._execute_action`'s `swap-label` branch.
        #
        # #1358's add-only-what's-missing rule is PRESERVED exactly: each label still appears only
        # when it genuinely needs changing (an issue found via the `goal_blocked_label` query carries
        # `sdlc:blocked`, not `sdlc:parked`, and vice versa -- `mark_blocked`/`_offboard` keep the two
        # mutually exclusive), so a second sweep over an already-swept issue still computes zero
        # actions. Only the write count changed, never which labels are named.
        add = [source.goal_label] if source.goal_label not in names else []
        # #1393: `in_progress_label` joins the removal set. An issue that reached this sweep in the
        # {parked, in-progress} shape is a half-applied park from before the atomic swap -- real and
        # measured on live boards. Restoring membership without clearing the stale claim would hand
        # back a goal that `_fetch_pending` then refuses (it excludes in-progress), i.e. an unpark
        # that unparks nothing.
        remove = [l for l in (source.parked_label, source.goal_blocked_label,
                              source.in_progress_label) if l in names]
        if add or remove:
            actions.append({"action": "swap-label", "issue": n,
                            "detail": triage._swap_detail(add, remove),
                            "add": add, "remove": remove, "result": None, "error": None})
            # #1393: MOVE THE CARD. `triage._execute_action` gained a `set-status` kind for exactly
            # this and had NO producer anywhere in the kit -- so every automatic unpark fixed the
            # label and left the card in `Blocked`. On a board-authoritative repo that manufactures a
            # permanently-unpickable goal: correctly labelled, invisible to `_board_queue` forever.
            # That is #1391 step 4's own lesson, and the sweep was still walking into it.
            # Gated on the board actually existing: `_set_board_status` returns False for a
            # board-DISABLED repo, `_execute_action` raises on False, and `sweep_unpark` credits an
            # unpark only when EVERY action for that issue succeeded -- so emitting this
            # unconditionally would make every label-queue adopter's unparks read as failures and
            # suppress their audit comment and cooldown credit. No board, no card to move.
            if getattr(source, "project_enabled", False):
                actions.append({"action": "set-status", "issue": n, "detail": source.col["ready"],
                                "result": None, "error": None})
        resolved[n] = sorted(refs, key=int)
    return actions, resolved


def compute_blocking_actions(sdlc_dir, config, source, blocked_issues, currently_blocking, run=None,
                             open_cache=None):
    """#1351: `sdlc:blocking` — derived, self-healing, applied to the BLOCKER issue itself (not the
    blocked goal `compute_unpark_actions` above manages). `blocked_issues` is the SAME list
    `_fetch_parked_issues` already returned for this pass (no second fetch); `currently_blocking` is
    `_fetch_blocking_issues`'s own "before" snapshot. `open_cache`: see `compute_unpark_actions`'s
    own doc for this shared-memo param -- `None` (the default) makes a fresh dict, unchanged prior
    behaviour.

    For each blocked/parked issue, reuses the EXACT same detection pipeline `compute_unpark_actions`
    does — `_referenced_blocker_refs` (the superset, any state) narrowed by a fresh
    `_explicit_blockers` re-check (still-open only) — never new regex/detection logic (#1186's own
    lesson: Sigma's own park-comment boilerplate can itself trigger a naive scan).

    Deliberately does NOT skip `_is_exempt` issues the way `compute_unpark_actions` does (#1351
    review finding, notable-severity correctness bug in the original version, which did skip them):
    `KEEP_PARKED_MARKER` opts a goal OUT OF AUTO-UNPARK ELIGIBILITY -- a human's "this park is a
    deliberate checkpoint" -- it says nothing about whether the blocker the goal's own text still
    names should keep reading as "something depends on you". Skipping exempt issues here meant an
    exempt goal's live "Blocked by #N" reference contributed nothing to `live_blocker_refs`, so a
    blocker already correctly labeled `sdlc:blocking` on the strength of that exact reference got
    wrongly stripped the very next sweep, even though the exempt issue was still open and still
    naming it. `_is_exempt`/`KEEP_PARKED_MARKER` remain relevant ONLY to `compute_unpark_actions`'s
    own re-add decision above, not to this function's bookkeeping.
    Deliberately a SEPARATE pass over the same `blocked_issues` rather than merged into
    `compute_unpark_actions` above: the two need different signals from the identical scan (that
    function wants "are ALL refs closed"; this one wants "which SPECIFIC refs are still open"), and
    keeping them independent avoids entangling two already-subtle pieces of logic into one harder-to-
    verify function, at the cost of one extra `gh issue view`/comment fetch per issue per sweep pass
    — cheap relative to the correctness risk of merging them.

    RETROACTIVE, for free: `blocked_issues` already includes issues carrying the OLD, generic
    `parked_label` (see `_fetch_parked_issues`), not just the new `goal_blocked_label` — an issue
    filed before this epic shipped, still generically `sdlc:parked` but still naming a live open
    blocker, gets that blocker labeled `sdlc:blocking` exactly the same way a fresh `sdlc:blocked`
    issue would. No separate migration pass needed.

    MULTI-BLOCKER, correctly: a blocker referenced by more than one open issue stays in the live set
    (and therefore keeps `sdlc:blocking`) until ALL referencing issues' scans stop finding it —
    `live_blocker_refs` is a plain union across EVERY issue in `blocked_issues`, exempt or not (see
    the exemption note above).

    Add-only-what's-missing / remove-only-what's-stale, exactly like `compute_unpark_actions`'s own
    action shape: `to_add = live_blocker_refs - currently_blocking`,
    `to_remove = currently_blocking - live_blocker_refs`."""
    live_blocker_refs = set()
    open_cache = open_cache if open_cache is not None else {}
    for it in blocked_issues:
        n = str(it.get("number"))
        goal_doc = {"ref": n, "raw": (it.get("title") or "") + "\n" + (it.get("body") or "")}
        raw_comments = backlog_check._fetch_scrubbed_comments(sdlc_dir, config, goal_doc, run=run)
        extra_text = backlog_check._cap_join_excerpts(
            backlog_check._strip_offboard_prefixes(
                backlog_check._filter_dismissal_comments(raw_comments)))
        refs = backlog_check._referenced_blocker_refs(goal_doc, extra_text)
        if not refs:
            continue
        docs = [{"ref": r, "open": _ref_is_open(source, r, open_cache)} for r in refs]
        findings = backlog_check._explicit_blockers(goal_doc, docs, extra_text)
        live_blocker_refs.update(f["ref"] for f in findings)

    to_add = live_blocker_refs - currently_blocking
    to_remove = currently_blocking - live_blocker_refs

    # #1393: MEMBERSHIP travels with the blocking label. `sdlc:blocking` says "other work is waiting
    # on this issue" -- and an issue other work is waiting on is, by definition, waiting to be
    # PICKED. But this function was the only writer of that label in the codebase and it granted no
    # membership, so a blocker lacking `goal_label` got marked as blocking and stayed pickable by
    # nothing: `_blocking_priority_pending` reuses `_fetch_pending`, whose base query always carries
    # `--label goal_label`, and `gh issue list --label` ANDs. The deadlock, now labelled.
    #
    # WHEN it is safe to grant is not decided here: `blockers.classify` already encodes that policy
    # for the block-RECORDING path, and a second copy of it would drift. A parked blocker
    # (`chained`) and a human-filed proposal (`needs_human`) are waiting for a HUMAN, so they keep
    # exactly their own label; a third-party issue (`unmanaged`) was never Sigma's to adopt.
    # Those three get `sdlc:blocking` alone -- today's behaviour -- and are what
    # `/sigma-doctor`'s unreachable-blocker check and `/sigma-promote`'s `deadlocked` bucket exist for.
    actions = []
    for r in sorted(to_add, key=int):
        add, remove = [source.blocking_label], []
        verdict, state = blockers.verdict_for(source, config, r, run)
        if state is not None:
            if verdict in (blockers.PROMOTED, blockers.ROUTED):
                if source.goal_label not in state["labels"]:
                    add.append(source.goal_label)
                # legacy leftover: stripped when membership is granted, so no {goal, leftover} drift
                if source.proposed_label in state["labels"]:
                    remove.append(source.proposed_label)
        # A read failure degrades to the blocking label ALONE -- today's behaviour -- never to a
        # guessed membership grant. Fail open toward doing less, and let doctor surface it.
        actions.append({"action": "swap-label", "issue": r,
                        "detail": triage._swap_detail(add, remove),
                        "add": add, "remove": remove, "result": None, "error": None})

    # Membership is NEVER revoked here. An issue that stopped blocking something is still a goal --
    # it is simply no longer holding anything up -- and taking away a membership this sweep did not
    # necessarily grant would be the loop deleting work from its own backlog.
    actions += [{"action": "remove-label", "issue": r, "detail": source.blocking_label,
                "result": None, "error": None} for r in sorted(to_remove, key=int)]
    return actions


_UNPARK_COMMENT = ("Auto-unparked by Sigma — blocker(s) {refs} closed; re-added {label} "
                   "for re-examination.")


def sweep_unpark(sdlc_dir, config, apply=False, run=None):
    """The whole sweep pass -- `triage.enact()`'s own shape (`sdlc_dir, config, ..., apply=False,
    run=None` -> a result dict; dry-run computes and reports, never writes). UNGATED: see this
    module's own docstring for why the `discovery.auto_unpark.mode` config check is the CALLER's
    job, not this function's. Defends its own reach regardless of caller, though: a no-op outside
    GitHub mode (`mirror.is_github_mode`), the same guard `backlog_check._fetch_scrubbed_comments`
    already applies for the identical reason.

    `apply=True`: labels are mutated through `triage.apply_actions`/`_execute_action` -- the exact
    primitive `enact --apply`'s own unpark case already uses, not a reimplementation -- then, for
    every issue whose actions ALL landed (`done`, never `failed`), one audit-trail comment naming
    the closed blocker(s) (`_UNPARK_COMMENT`). `discovery.blocker_promotion`'s own README section
    states its rule plainly -- "every promotion is paired with an explanatory comment... never a
    silent label change" -- and an UNATTENDED write needs that even more than a human-reviewed
    `enact --apply` does (a human who just built the plan already has full context; nobody is
    watching this sweep run). Best-effort: a comment failure never flips an already-successful
    label edit back to reporting failure, and never retries.

    `result["unparked"]`: the SAME "all landed" issue numbers named above, independent of whether
    their audit comment itself succeeded (best-effort, per above) -- a sorted list, `[]` whenever
    `apply` is false (a dry-run mutates nothing, so nothing was really unparked yet). This is the
    cooldown hook: `loop.py`'s `_auto_unpark_sweep`/`_next()` read it to keep the very goal this
    call just unparked from ALSO being the one this same call's own pick returns -- see this
    module's own docstring and `_next()`'s for the full mechanism.

    #1351 review findings (two, both fixed here):

    BLOCKING -- the `sdlc:blocking` reconciliation only runs when `_fetch_parked_issues_status`
    reports `complete` (both label queries succeeded): a wrongly-EMPTY `issues` list from a
    transient outage would otherwise make `compute_blocking_actions` conclude nothing is blocked
    and strip every currently-correct `sdlc:blocking` label -- a destructive write from nothing more
    than a network blip. It also runs inside its own `try/except`, isolated from the unpark
    computation above it: a raise inside the newer, less-proven blocking code must cost only this
    one reconciliation pass, never silently discard the unrelated, already-computed unpark actions
    -- the fail-open guarantee this module's other primitives already give.

    NOTABLE -- the unpark audit-comment/cooldown check below (`resolved`'s "did ALL of this issue's
    actions land" test) is computed from `unpark_results` ONLY, never the combined `actions` list.
    A blocker issue can itself be independently eligible to unpark in the SAME pass (an overlapping
    issue-number topology: #7 is a parked goal AND is #42's live blocker), and an unrelated failure
    on #7's `sdlc:blocking` edit must never mask #7's own, fully-successful unpark -- checking
    against the combined list would count #7 as "not fully landed" and silently drop its audit
    comment and cooldown entry over an action that was never part of the unpark decision at all."""
    if not mirror.is_github_mode(config):
        return {"apply": bool(apply), "checked": 0, "eligible": 0, "actions": [], "unparked": []}
    source = sources.GitHubSource(config, run=run)
    issues, complete = _fetch_parked_issues_status(source)
    open_cache = {}
    unpark_actions, resolved = compute_unpark_actions(sdlc_dir, config, source, issues, run=run,
                                                       open_cache=open_cache)
    blocking_actions = []
    if complete:
        try:
            currently_blocking = _fetch_blocking_issues(source)
            blocking_actions = compute_blocking_actions(sdlc_dir, config, source, issues,
                                                         currently_blocking, run=run,
                                                         open_cache=open_cache)
        except Exception:
            blocking_actions = []
    n_unpark = len(unpark_actions)
    actions = unpark_actions + blocking_actions
    # `and actions`, unlike `enact()`'s own unconditional `if apply:` — deliberate, not an oversight.
    # `enact --apply` is a rare, human-triggered, one-off campaign action, where three extra
    # idempotent `label create` calls are noise-level cost. This sweep runs automatically
    # on every unattended pick (`loop.py`'s `_next()`); the overwhelmingly common case is nothing
    # parked at all, and on that case `actions` is already `[]` -- ensuring labels nobody is about
    # to reference would be three pure-waste calls, every single cycle, forever.
    if apply and actions:
        source._ensure_labels()
    actions = triage.apply_actions(source, actions, apply)
    unpark_results = actions[:n_unpark]
    unparked = []
    if apply:
        by_issue = {}
        for a in unpark_results:
            by_issue.setdefault(a["issue"], []).append(a)
        for n, refs in resolved.items():
            done = by_issue.get(n)
            if done and all(a["result"] == "done" for a in done):
                unparked.append(n)
                text = _UNPARK_COMMENT.format(refs=", ".join(f"#{r}" for r in refs),
                                              label=source.goal_label)
                posted = []

                def _post(num, body, _n=n):
                    source.note(num, body)
                    posted.append(num)                  # a record failure after this must not re-post
                try:
                    dr = _load("decision_record")
                    if dr.enabled(config):              # slice 8: autonomous record + same-id comment
                        dr.write_all(sdlc_dir, dr.build("sweep", n, "dependency", "unpark",
                                                        "blocker(s) closed: " + ", ".join(
                                                            f"#{r}" for r in refs),
                                                        dr.how_for("sweep")),
                                     config, source, text=text, poster=_post)
                        continue
                except Exception:
                    if posted:
                        continue
                try:
                    source.note(n, text)
                except Exception:
                    pass
    return {"apply": bool(apply), "checked": len(issues), "eligible": len(resolved),
           "actions": actions, "unparked": sorted(unparked, key=int)}


def render_sweep(result):
    mode = "APPLIED" if result["apply"] else "DRY-RUN"
    lines = [f"sweep-unpark ({mode}): {result['checked']} parked issue(s) checked, "
            f"{result['eligible']} eligible — {len(result['actions'])} action(s)"]
    for a in result["actions"]:
        # #1392: shares `triage._ACTION_VERBS` rather than re-deriving the verb from a two-way
        # guess -- that guess rendered every action that was not `add-label` as "remove label",
        # which a `swap-label` action is not.
        verb = triage._ACTION_VERBS.get(a["action"], a["action"])
        line = f"  [{a['result']}] #{a['issue']}: {verb} {a['detail']}"
        if a["result"] == "failed" and a.get("error"):
            line += f" — {a['error']}"
        lines.append(line)
    if not result["actions"]:
        lines.append("  no actions needed" if result["checked"] else "  nothing parked")
    if result["apply"]:
        done = sum(1 for a in result["actions"] if a["result"] == "done")
        failed = sum(1 for a in result["actions"] if a["result"] == "failed")
        lines.append(f"summary: {done} done, {failed} failed")
    else:
        would = sum(1 for a in result["actions"] if a["result"] == "would")
        lines.append(f"summary: {would} would-do (pass --apply to execute)")
    return "\n".join(lines)


_SWEEP_USAGE = "usage: auto_unpark.py sweep <sdlc_dir> [--apply]"


def sweep_cmd(sdlc_dir, config, argv_tail, run=None):
    result = sweep_unpark(sdlc_dir, config, apply=("--apply" in argv_tail), run=run)
    print(render_sweep(result))
    return 1 if any(a["result"] == "failed" for a in result["actions"]) else 0


def main(argv):
    if argv[1:] in (["-h"], ["--help"]):
        print(_SWEEP_USAGE)
        return 0
    if len(argv) >= 3 and argv[1] == "sweep":
        return sweep_cmd(argv[2], triage._config(argv[2]), argv[3:])
    print(_SWEEP_USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
