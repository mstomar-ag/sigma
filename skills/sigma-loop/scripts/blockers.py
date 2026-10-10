#!/usr/bin/env python3
"""blockers.py (#1393) -- a blocked goal must be RESOLVED, routed, or parked for a named reason.
Never silently skipped.

THE DEADLOCK THIS EXISTS TO CLOSE. When a goal hits a dependency, Sigma records the block and
moves on. That is correct only if something will eventually work the blocker. Three of the four
paths satisfy that; one does not, and it is the one Sigma walks down itself:

    goal #42 hits a dependency
      |
      +-- cross-area hand-off (same_area=False, blocks_goal=True)
      |     issue opened WITH goal_label, ASSIGNED, ledger kind="handoff" to=owner.
      |     autowatch surfaces it. This path is fine.
      |
      +-- same-area, immediately_actionable=False
      |     issue opened with followup_label and NO goal_label; ledger entry is only kind="note",
      |     which autowatch does not watch; goal #42 -> sdlc:blocked.
      |     Nothing can pick the blocker. `auto_unpark` will not resume #42 until the blocker
      |     CLOSES. Neither side can move. Sigma built this, and told nobody.
      |
      +-- a human writes "Blocked by #N" against a plain repo issue
            precheck parks #42; no ledger entry at all. Same deadlock, different origin.

`sdlc:blocking` does not rescue any of it: it RE-RANKS issues that are already eligible.
`_blocking_priority_pending` reuses `_fetch_pending`, whose base query always carries
`--label goal_label`, and `gh issue list --label` ANDs repeated flags -- so a blocker without
`goal_label` is reachable by no queue at all.

THE RULE, and it is the whole module: a block is resolved, routed to whoever owns it, or parked
with a reason from a closed set. There is no fourth option and no silent one.

WHY PROMOTION IS NOT "DEFEATING THE APPROVAL GATE". The gate (#233) exists to stop SPECULATIVE
AI-filed work from consuming the backlog. An issue that real, already-approved work is now stalled
behind is by definition not speculative. And the label pair is decisive: `sdlc:followup` means
Sigma filed it. The confirmation label is legacy-only here (slice 18): a leftover is read through
its provenance label like any other issue, and `_act` still strips the leftover when it grants
membership.

WHAT THIS MODULE WILL NOT DO:
  - un-park anything (`chained`) -- a park is human-owned, and `auto_unpark` is the only sweep
    allowed to reverse one, on evidence this module does not have;
  - adopt an unlabelled third-party issue into `goal_label` (`unmanaged`) -- it was never ours, so
    it cannot be "lost as an orphan", and adopting it would put Sigma to work on somebody
    else's backlog.
"""
import pathlib, sys, importlib.util

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


ledger = _load("ledger")
handoff = _load("handoff")
blocker_scan = _load("blocker_scan")   # #2532: the shared MERGED/CLOSED resolution rule

#: The verdicts, in the order they are TESTED -- the order is load-bearing, not cosmetic. `chained`
#: outranks `promoted` on purpose (plan-review PR-6): a Sigma-filed follow-up that a human later
#: PARKED has had a human decision made on it, and `sdlc:followup` alone cannot know that.
PICKABLE = "pickable"        # already in the queue; blocking_priority_override sorts it first
CHAINED = "chained"          # parked -- a human owns it; never auto-undone
PROMOTED = "promoted"        # LEGACY, never returned by classify now. Sigma's own unruled proposal, and real work waits on it
NEEDS_HUMAN = "needs_human"  # LEGACY, never returned by classify now. a HUMAN filed the proposal; promoting would override their decision
ROUTED = "routed"            # someone else's; grant membership and address it to them
UNMANAGED = "unmanaged"      # not in Sigma's world at all; surface, never adopt
STALE = "stale"              # closed -- not a blocker

#: Labels that PROVE Sigma owns an issue. Deliberately just the two filing-time provenance
#: labels -- NOT "any `sdlc:*` label", which is what this test used to be and which was a
#: self-reinforcing bug found by an adversarial audit:
#:
#:   `auto_unpark.compute_blocking_actions` writes `sdlc:blocking` to a third-party blocker while
#:   DELIBERATELY withholding membership (verdict `unmanaged` -- it was never Sigma's to adopt).
#:   One pass later that same issue carries `sdlc:blocking`, "any sdlc:* label" reads it as proof of
#:   ownership, the verdict flips to `routed`, and membership is granted after all. Sigma's own
#:   annotation became its own evidence, and the withheld decision silently reversed itself -- with
#:   `blocking_priority_override` on, the adopted third-party issue then sorts ahead of every real
#:   goal.
#:
#: Provenance is the only honest evidence here: `sdlc:followup` is stamped on every issue
#: `handoff.create_tracked_issue` files, and `sdlc:dependency` on every cross-area hand-off. Both
#: are written ONLY at filing time, by Sigma, about an issue Sigma created. A derived
#: annotation can never say who owns something.
_PROVENANCE = (handoff.FOLLOWUP_LABEL, handoff.DEPENDENCY_LABEL)

#: The ONLY reasons a block may end in a park. Anything else is an escalation, not a park -- the
#: user's rule, made checkable: "never skip it knowingly until it is sure that this is going to be
#: parked due to insufficient permissions, knowledge, or a known blocker."
PARK_REASONS = ("permissions", "knowledge", "known-blocker")


def classify(state, goal_label, proposed_label, parked_label, followup_label, me):
    """Pure. `state` is `{"labels": set(), "assignees": [logins], "closed": bool}` -> a verdict.

    Deliberately pure and separately tested from anything that writes: every verdict below decides
    whether a label gets written to a real issue, and a classifier that can only be exercised
    through a `gh` fake is a classifier whose edge cases go untested."""
    if state.get("closed"):
        return STALE
    names = set(state.get("labels") or ())
    if parked_label in names:
        return CHAINED                       # BEFORE the promotion test -- see the constant block
    # Legacy label (decision-rubric slice 18): the confirmation label is NO LONGER a classification
    # row. A filed follow-up is already a goal, so nothing new carries the label; a leftover is
    # classified by its OTHER labels (provenance -> ROUTED, which grants membership and, in `_act`,
    # strips the leftover). `proposed_label` stays a parameter only so callers keep one signature.
    if goal_label in names:
        if proposed_label in names:
            return ROUTED                    # {goal, leftover}: the queue refuses it; `_act` strips it
        return PICKABLE
    if _other_assignees(state, me):
        return ROUTED
    if any(n in _PROVENANCE for n in names):
        # Managed by Sigma -- it carries a label only Sigma's own filing path writes -- but
        # no membership label. That is the orphan class, and membership is exactly what it is
        # missing, so granting it is the repair.
        return ROUTED
    return UNMANAGED


def _other_assignees(state, me):
    """The assignees who are NOT the current actor — the ONE definition of "somebody else owns
    this", used by `classify` to decide ROUTED and by `_route`/`_act` to act on that decision.

    Review bug_002: these had drifted. `classify` filtered on `!= me`, while `_route` picked the
    recipient with a bare "first non-empty assignee". On an issue assigned to BOTH Sigma and a
    human — a realistic shape, since Sigma self-assigns same-area follow-ups and a person later
    joins — the verdict was ROUTED *because of* the human, and the ledger note was then addressed
    back to Sigma. `ledger.addressed_to` matches `to` exactly, so the real owner's `ledger.mine`
    never surfaced a blocker routed for them: the routing signal silently went nowhere."""
    return [a for a in (state.get("assignees") or []) if a and a != me]


def verdict_for(source, config, ref, run=None):
    """`(verdict, state)` for ONE blocker — the whole classification, vocabulary resolution included,
    behind one call. `state` is `None` (and the verdict `UNMANAGED`) when the issue cannot be read.

    Exists so a SECOND caller can reuse the policy without also reusing its inputs: `auto_unpark`'s
    `compute_blocking_actions` needs the same "is it safe to grant membership?" answer, but has no
    `handoff`/`ledger` import of its own and should not grow one just to assemble the label
    vocabulary. One policy, one place that resolves what feeds it — a copy of either would drift."""
    state = _state(source, ref)
    if state is None:
        return UNMANAGED, None
    return classify(state, source.goal_label, source.proposed_label, source.parked_label,
                    handoff.FOLLOWUP_LABEL, ledger.actor(config, run)), state


def _state(source, number):
    """`{"labels", "assignees", "closed"}` for one issue, or None when it cannot be read. Refusing
    on an unreadable issue is deliberate: every verdict below decides a real label write.

    #2532: `closed` uses `blocker_scan.closed_state` -- MERGED (a PR) resolves; CLOSED-without-
    merge (also a PR) does NOT; an ordinary closed ISSUE still does. The one shared implementation
    of this rule, also used by `promote.py` and `auto_unpark.py`'s own `_ref_is_open`."""
    try:
        data = source._read_issue(number, ["labels", "assignees", "state", "stateReason"])   # #895: REST first
        if not data.get("state"):
            return None
        return {"labels": {(l.get("name") or "") for l in (data.get("labels") or [])},
                "assignees": [(a.get("login") or "") for a in (data.get("assignees") or [])],
                "closed": blocker_scan.closed_state(str(data.get("state") or "").upper(),
                                                    data.get("stateReason") or "")}
    except Exception:                                   # noqa: BLE001 - unreadable == refuse
        return None


def resolve(sdlc_dir, config, source, goal, refs, run=None, apply=True):
    """Classify and act on every `ref` blocking `goal`. Returns
    `{"results": [{"ref", "verdict", "detail", "acted"}], "resolved": [...], "surfaced": [...]}`.

    Never raises: a blocker that cannot be classified is reported as such and the rest continue.
    The goal's own park/block transition is the CALLER's job -- this only ever touches the blockers.

    THE ALWAYS-ON CHANNEL IS THE ISSUE COMMENT, NOT THE LEDGER. `ledger.enabled` ships FALSE and
    `safe_append` degrades to a no-op, so a resolver whose only output were a ledger entry would be
    silent on every stock adopter -- the same "built on a subsystem that ships disabled" trap this
    branch has already hit three times. The ledger entry is enrichment for teams that have it on;
    the comment and the returned `detail` are what always happen."""
    out = {"results": [], "resolved": [], "surfaced": []}
    if not all(hasattr(source, a) for a in ("_run", "_repo_args", "_swap_labels")):
        out["results"] = [{"ref": str(r), "verdict": UNMANAGED, "acted": False,
                           "detail": "blocker resolution needs github discovery mode"}
                          for r in refs]
        return out
    goal_label = source.goal_label
    proposed_label = source.proposed_label

    for ref in refs:
        r = str(ref)
        verdict, state = verdict_for(source, config, r, run)
        if state is None:
            out["results"].append({"ref": r, "verdict": UNMANAGED, "acted": False,
                                   "detail": "could not read #%s — treat it as unresolved" % r})
            out["surfaced"].append(r)
            continue
        detail, acted = _act(source, goal, r, verdict, state, goal_label, proposed_label,
                             sdlc_dir, config, run, apply)
        out["results"].append({"ref": r, "verdict": verdict, "detail": detail, "acted": acted})
        # Review bug_001: bucket on what ACTUALLY HAPPENED, not on the verdict alone. `_act` returns
        # `acted=False` when `_swap_labels` raises (its documented contract: it retries, then RAISES
        # -- "callers decide what a failed lifecycle write means"), and the verdict is deliberately
        # not downgraded. Bucketing on the verdict therefore filed a blocker whose goal-label write
        # had FAILED as resolved, `park_reason` returned None, and the park claimed "every named
        # blocker is now workable" about an issue no queue can serve. That silently recreates the
        # exact deadlock this module exists to close, on the correlated-outage failure model
        # `_swap_labels` was built for.
        #
        # PICKABLE and STALE legitimately sit in `resolved` with `acted=False` -- they need nothing
        # done -- so they are named explicitly rather than inferred.
        (out["resolved"] if (acted or verdict in (PICKABLE, STALE))
         else out["surfaced"]).append(r)
    return out


def _act(source, goal, ref, verdict, state, goal_label, proposed_label, sdlc_dir, config, run,
         apply):
    """The one action for one verdict. Returns `(detail, acted)`."""
    if verdict == PICKABLE:
        # Say what is actually true of THIS issue. "the loop will pick it" is only right for a bare
        # membership label: an issue that also carries an overlay is not in any queue right now, and
        # the park text is the operator's only window into why their goal is waiting.
        if source.goal_blocked_label in state["labels"]:
            return ("#%s is itself blocked by something else — the sweep resumes it when that "
                    "closes, and this goal follows" % ref), False
        if source.in_progress_label in state["labels"]:
            return "#%s is being worked right now" % ref, False
        return "#%s already carries %s — the loop will pick it" % (ref, goal_label), False
    if verdict == STALE:
        return "#%s is closed — not a blocker" % ref, False
    if verdict == CHAINED:
        return ("#%s is parked — a human owns that decision, so this chain needs one too. "
                "Run /sigma-unpark on #%s." % (ref, ref)), False
    if verdict == UNMANAGED:
        return ("#%s carries no Sigma label and nobody is assigned — it is outside the loop's "
                "world, so it will not be picked up on its own" % ref), False

    # PROMOTED and ROUTED both grant MEMBERSHIP -- that is the resolution.
    add = [goal_label] if goal_label not in state["labels"] else []
    remove = [proposed_label] if proposed_label in state["labels"] else []
    if not apply:
        return ("would grant %s to #%s so it can be picked" % (goal_label, ref)), False
    if add or remove:
        try:
            source._swap_labels(ref, add=add, remove=remove)
        except Exception as exc:                        # noqa: BLE001 - reported, never raised
            return "could not make #%s pickable: %s" % (ref, exc), False
        try:
            source._set_board_status(ref, source.col["ready"])
        except Exception:                               # noqa: BLE001 - the label is what decides
            pass
    _comment(source, ref, "Unblocking #%s: this issue is what it is waiting on, so it now carries "
                          "`%s` and the loop can pick it up." % (goal, goal_label))
    if verdict == ROUTED:
        _route(sdlc_dir, config, goal, ref, state, run)
        others = _other_assignees(state, ledger.actor(config, run))
        if not others:                                  # Sigma-filed (provenance label), nobody else's
            return ("#%s was filed by Sigma and had no membership — granted %s so the loop can "
                    "work it" % (ref, goal_label)), True
        return ("#%s belongs to %s — granted %s and recorded in the ledger for them"
                % (ref, ", ".join("@" + a for a in others) or "another owner",
                   goal_label)), True
    return "#%s was Sigma's own unapproved follow-up — promoted, real work waits on it" % ref, True


def _route(sdlc_dir, config, goal, ref, state, run):
    """Record the blocker against its owner so the team view shows it.

    DELIBERATELY `kind="note"`, not `kind="handoff"`. `handoff` is documented as CROSS-AREA BLOCKING
    only, and `backlog_check._ledger_signals` treats an outstanding one as a confident block settled
    only by an explicit `ack` -- so a resolver-written `handoff` that nobody acks would park the
    filer even after the blocker closed, defeating `auto_unpark`. A routing signal must not carry
    park-the-filer semantics.

    KNOWN LIMITATION, stated rather than skipped: `autowatch` watches `kind="handoff"` only, so a
    routed blocker is visible in `ledger.mine` and the team view but is not auto-nudged. Widening
    autowatch's watch set is a separate change."""
    # Review bug_002: the SAME filter `classify` used to reach ROUTED. Picking the first assignee
    # regardless addressed the note back at Sigma on any co-assigned blocker.
    owner = next(iter(_other_assignees(state, ledger.actor(config, run))), None)
    try:
        ledger.safe_append(sdlc_dir, "note", str(goal), config=config, to=owner,
                           issue=int(ref) if str(ref).isdigit() else None,
                           why="blocks goal #%s — made pickable so it can be worked" % goal)
    except Exception:                                   # noqa: BLE001 - enrichment, never required
        pass


def _comment(source, number, text):
    try:
        source._issue_comment(number, text)             # REST first through gh_api (#895 slice 3a)
    except Exception:                                   # noqa: BLE001 - audit trail is best-effort
        pass


def render(result):
    lines = []
    for r in result["results"]:
        lines.append("  [%s] #%s: %s" % (r["verdict"], r["ref"], r["detail"]))
    if not lines:
        lines.append("  no blockers named")
    return "\n".join(lines)


def park_reason(result):
    """The park text for a goal whose blockers could not all be resolved -- or `None` when every
    blocker IS resolved, which is the whole point: a goal whose blockers are all moving is blocked,
    not parked, and `auto_unpark` will resume it.

    The returned text always names the surviving blockers and what each of them needs, so a park is
    never the bare "blocked" that a human then has to investigate from scratch."""
    if not result["surfaced"]:
        return None
    parts = [r["detail"] for r in result["results"] if r["ref"] in result["surfaced"]]
    return "blocked and not self-resolving — " + "; ".join(parts)
