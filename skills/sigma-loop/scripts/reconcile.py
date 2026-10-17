"""#1391 step 5a: the CENSUS — a read-only survey of every issue Sigma is responsible for, and
what state each one is actually in.

WHY THIS IS A SEPARATE MODULE, not an extension of `auto_unpark.py`. That module's own docstring
makes its narrowness a permanent promise ("this sweep only ever reverses the ONE park class it can
prove is stale, never guesses at any other"). Folding general drift-detection into it would break
that contract. This module is built to the same SHAPE — pure `compute_*`/`census` functions, a
`sweep_*` orchestrator, a `render_*`, a CLI verb, and a mechanism that is unconditional once called
with the config gate living only at the unattended call site — but owns a different question.

★ THE ABSENCE TRAP, and why this enumerates the POPULATION rather than the corruption.
A label query can only ever return issues that HAVE the label you name. The worst corruption class
in this system is an issue with NO lifecycle label at all — and no label query on earth returns it.
`doctor._multi_state_label_scan` is blind to that class by construction, because it asks four label
queries and flags issues appearing in more than one. So this module never asks "which issues are
broken?". It asks "which issues am I responsible for?", then classifies each one locally. The
difference is the whole design.

★ IT OWNS ITS OWN READS. Deliberately NOT built on:
  - `mirror.fetch_and_write` / `board-mirror.ndjson` — gated on `backlog_check.enabled`, which ships
    FALSE. The "205 records already live" measurement that suggested reusing it was taken on this
    repo's own NON-default config.
  - the ledger — `ledger.enabled` also ships FALSE, and on the one adopter with real in-flight work
    `open_claims()` is `{}` against 7 in-progress issues.
Building the recovery layer on a subsystem that ships disabled is the exact failure shape that sank
two previous attempts at this area. Every read here works on a stock config.

Step 5a is READ-ONLY. It computes and reports; it never writes a label, moves a card, or touches an
issue. The correction tiers (5c AUTOMATIC / 5d PROPOSED) are separate, later, and gated.

    python3 reconcile.py census <sdlc_dir>
"""
import json, pathlib, sys, importlib.util

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


sources = _load("sources")
mirror = _load("mirror")
handoff = _load("handoff")   # dependency_label() -- the adopter-configurable hint, resolved live


#: Classification buckets. `CLEAN` is not an anomaly; the rest are, in descending severity -- except
#: `NEEDS_TRIAGE` (#2364), which is not corruption at all. It is the DESIGNED terminal state of
#: `feature_classify`'s tier-4 (#2363: every automatic classification tier tried and failed), and
#: `mark_needs_triage` keeps `goal_label` when it sets it -- so `sdlc:goal`+`sdlc:needs-triage`
#: together is correct, not drift. It is still reported, separately from CLEAN, because it is a
#: state a human must act on (assign a unit, or the issue never becomes pickable again) and the
#: census's whole job is surfacing exactly that -- see the module docstring's "which issues am I
#: responsible for" framing, not only "which issues are broken".
CLEAN = "clean"
MULTI_LABEL = "multi-label"              # more than one primary lifecycle label at once
ZERO_LABEL = "zero-label"                # managed by Sigma, but carries NO primary label
CLOSED_WITH_STATE = "closed-with-state"  # closed, still carrying in-progress or an overlay
NEEDS_TRIAGE = "needs-triage"            # carries sdlc:needs-triage -- tier-4, waiting on a human

#: Annotations that prove Sigma touched an issue even when no lifecycle label survives. Used
#: ONLY to decide whether a zero-label issue is one of ours — never to gate anything.
_MANAGED_HINTS = ("sdlc:followup", "sdlc:dependency", "sdlc:blocking", "sdlc:decompose",
                  "sdlc:goal-debt")


def _managed_hints(source, config):
    """`_MANAGED_HINTS`, with the two adopter-CONFIGURABLE names resolved live.

    Review bug_002: `sdlc:dependency` and `sdlc:blocking` are both renameable —
    `ledger.handoff.label` and `discovery.github.blocking_label`, and the shipped config template
    explicitly tells adopters to rename them if they collide with a pre-existing label. Hardcoding
    the defaults meant that on a renamed repo the census queried a string nothing carries, and
    `classify`'s fallback then read an issue whose only surviving Sigma trace was the renamed
    hint as CLEAN rather than ZERO_LABEL.

    That is the absence trap this module's own docstring says these hints exist to close, reopened
    by the hints themselves. The literals stay as the DEFAULTS; only the two that can move are
    resolved. (`sdlc:decompose`/`sdlc:goal-debt` have no config key at all, so there is nothing to
    resolve for them.)"""
    return tuple(dict.fromkeys((
        "sdlc:followup",
        handoff.dependency_label(config or {}),
        getattr(source, "blocking_label", None) or "sdlc:blocking",
        "sdlc:decompose",
        "sdlc:goal-debt",
    )))


def _primary_labels(source, config):
    """The three MEMBERSHIP-or-exit labels, in the same sense `doctor._multi_state_label_scan` uses:
    in Sigma's world (`goal_label`), a human's permanent exit from it (`parked_label`), or not
    admitted to it yet (`proposed_label`). Carrying more than one of THOSE is a real contradiction.

    `in_progress_label` is deliberately NOT here: it is an orthogonal, additive marker that
    legitimately co-occurs with the goal label on every actively-worked issue (#1354 documents this,
    and treating it as primary would flag normal work as corruption). It IS fetched separately below
    — an issue carrying it and nothing else is the invisible orphan class this census exists to
    find — but it never participates in the "more than one" test.

    #1393: `goal_blocked_label` is NOT here either, for the same reason and by the same rule.
    `mark_blocked` now KEEPS `goal_label` — `sdlc:blocked` became a temporary OVERLAY rather than an
    exit — so `goal`+`blocked` is the correct, normal shape of a blocked goal. Leaving it in this
    tuple made the census report every properly-blocked goal on the board as multi-label corruption,
    and worse: the PROPOSAL tier would then offer "corrections" against a state the loop creates on
    purpose, i.e. the reconciler proposing to break working goals.

    It is still FETCHED as an overlay below (the same treatment `in_progress_label` gets), so a
    closed issue left carrying it, or one carrying it and no membership label at all, is still
    found — only the "more than one" test changed.

    #1579: this now DELEGATES to `GitHubSource.membership_labels()` instead of restating the tuple.
    The two were byte-identical, and a second copy of this vocabulary is exactly the defect class
    #1393 measured — five of six hand-written copies of the eligibility rule had drifted from each
    other — just not yet triggered. `config` stays in the signature because every caller passes it
    and because `_managed_hints`, the neighbour with the same shape, genuinely needs it; here it is
    deliberately unused, since nothing in the membership set is derived from anything `source` has
    not already resolved."""
    return source.membership_labels()


def _overlay_labels(source):
    """The ADDITIVE overlays: labels that legitimately ride alongside `goal_label` and are never a
    state of their own. `in_progress_label` (#1354), `goal_blocked_label` (#1393), since #1468
    `needs_label_label`, and since #2263 `needs_unit_label`.

    Given a name of its own precisely because removing `goal_blocked_label` from `_primary_labels`
    silently costs TWO detections if the overlay set is left implicit -- both of them orphan classes
    this census exists to find:

      1. an OPEN issue carrying only `sdlc:blocked` (membership lost to a partial write from an
         older plugin) would be enumerated by no query at all, the absence trap again;
      2. a CLOSED issue still carrying `sdlc:blocked` would stop being `CLOSED_WITH_STATE` -- and a
         stale not-eligible label on a closed issue is exactly the "reopen it and a false state
         comes back to life" case that bucket exists for.

    So the overlays are enumerated and classified explicitly, rather than as whatever happens to be
    left over in `primary`.

    #1579: delegates to `GitHubSource.overlay_labels()` for the reason `_primary_labels` does — one
    definition of the vocabulary, not two that happen to agree today."""
    return source.overlay_labels()


def _fetch_by_label(source, label, state="open"):
    """Every issue carrying `label` — `(issues, ok, truncated)`. FAIL-OPEN per label.

    `ok` is the point: an empty list from a failed query is indistinguishable from a genuinely empty
    one, and the caller MUST be able to tell those apart before it ever reports "nothing is wrong".

    `truncated` (#1579) is that same point one step further out, and it became load-bearing with the
    E3 fix below. A read that came back exactly at the `--limit` ceiling may have silently lost rows,
    and this returned `ok=True` either way — so a partial read presented as a complete census, and
    `sweep_reconcile`'s entire "refuse on an incomplete census" contract rests on that flag. It was
    survivable only while every label E3 queried had a small, BOUNDED closed population.
    `goal_label`-on-closed does not: it is the terminal residue of every goal ever finished where
    `complete()` failed or never ran, and it grows monotonically (24 on this repo; ~350 on `os`).

    Reported SEPARATELY from `ok` rather than folded into it — the shape `triage._fetch` already
    uses, where `"truncated"` is a distinct degradation from `"gh_unavailable"`. The two have
    different remedies (wait for `gh` to recover / raise `GitHubSource._BOARD_ITEM_LIMIT`), and a
    refusal that names the wrong one is the same defect this issue is about."""
    try:
        # #895 slice 2c: REST first, newest first (what `gh issue list` returned, so a board over the
        # ceiling still reads its NEWEST rows), ONE `gh issue list` fallback -- see sources._list_issues
        issues = source._list_issues(["number", "labels", "state", "closedAt"], labels=[label], state=state)
    except Exception:
        return [], False, False
    if not isinstance(issues, list):
        return [], False, False
    # `_warn_truncated` is a no-op below the ceiling, so it is called unconditionally — reusing the
    # repo's one truncation warning rather than writing a second one that could drift from it.
    source._warn_truncated("issues labelled %s (%s)" % (label, state), len(issues))
    truncated = len(issues) >= source._BOARD_ITEM_LIMIT
    return [i for i in issues if isinstance(i, dict) and "number" in i], True, truncated


def _names(issue):
    return {(l.get("name") or "") if isinstance(l, dict) else str(l or "")
            for l in (issue.get("labels") or [])}


def classify(issue, primary, overlays, goal_label, hints=_MANAGED_HINTS, needs_triage_label=None):
    """The bucket for ONE issue, given the label vocabulary. Pure — no I/O, no config reads.

    `primary` is `_primary_labels`' membership-or-exit tuple; `overlays` is `_overlay_labels`' additive
    set (in-progress, blocked); `goal_label` is named separately because it is the MEMBERSHIP label
    and is treated differently from the not-eligible labels on a closed issue.

    #1393 made `overlays` an explicit PARAMETER rather than the single `in_progress_label` it used to
    be: `goal_blocked_label` moved out of `primary`, and passing it implicitly would have silently
    dropped it from both tests below.

    #2364: `needs_triage_label` is named separately from `overlays`, the same way `goal_label` is,
    because — like `goal_label` — it needs DIFFERENT treatment from the rest of that set rather than
    being folded anonymously into it: every other overlay riding alongside `goal_label` on an open
    issue is CLEAN (the normal active-work shape), but `sdlc:needs-triage` is the one overlay that is
    ALSO its own reported bucket, so a human seeing the census actually learns a unit is still owed.
    Defaulted to `None` so every existing caller — including every test above this one — keeps
    classifying exactly as before; only a caller that names the label opts into the new bucket."""
    names = _names(issue)
    overlays = tuple(overlays)
    matched = [p for p in primary if p in names]
    if str(issue.get("state") or "").upper() == "CLOSED":
        # A closed issue keeping its MEMBERSHIP label is the designed terminal state and is inert —
        # every picker query filters `--state open`, so it can never be served. What is NOT inert is
        # a closed issue still carrying in-progress or a not-eligible OVERLAY: reopen it and a false
        # state comes back to life. (Measured: 121 closed sigma issues are pre-loaded this way.)
        # #1393: the OVERLAYS, and byte-identically to `_stale_on_closed`'s set -- which is the whole
        # point of keeping these two in step. #1393's own lesson was a census that reported an
        # anomaly the corrector could not act on; the inverse (a corrector that can act on something
        # the census never reports) is the same bug facing the other way.
        #
        # #1445 adds `goal_label`. A closed issue keeping a HUMAN-DECISION membership label
        # (`sdlc:parked`, `sdlc:needs-confirmation`) is still the designed terminal state and is still
        # NOT flagged here -- that half of #1393 is untouched. But `goal_label` says "the loop may
        # pick this", which on a closed issue is both meaningless and, on reopen, a silent re-entry
        # into the queue nobody re-approved. See `_stale_on_closed` for the full reasoning.
        stale = [l for l in overlays if l in names]
        if goal_label and goal_label in names and goal_label not in stale:
            stale.append(goal_label)
        if stale:
            return CLOSED_WITH_STATE
        # A membership CONTRADICTION survives being closed and is still worth reporting -- but it is
        # not automatically correctable, because deciding which of two membership labels a human
        # meant is a judgment call. Routing it to MULTI_LABEL puts it in front of the evidence-based
        # PROPOSED tier (a human approves) instead of the ungated AUTOMATIC one.
        return MULTI_LABEL if len(matched) > 1 else CLEAN
    if len(matched) > 1:
        return MULTI_LABEL
    # #1393: I2's OTHER half. An overlay is only ever legitimate alongside `goal_label` -- a parked
    # or awaiting-approval issue carrying `sdlc:in-progress` or `sdlc:blocked` is a half-applied
    # transition from before the atomic swap, and the census used to bucket it CLEAN because it
    # tested only for "more than one membership label". That is exactly the drift a census exists to
    # find: the issue looks settled and is not.
    if matched and matched[0] != goal_label and any(o in names for o in overlays):
        return MULTI_LABEL
    # #2364: checked AFTER both MULTI_LABEL tests (a genuine label contradiction is more severe and
    # takes priority) and BEFORE the ZERO_LABEL fallback (this overlay is more informative than the
    # generic orphan class, whether or not membership survived alongside it).
    if needs_triage_label and needs_triage_label in names:
        return NEEDS_TRIAGE
    if not matched:
        # Only OUR issues count. An ordinary issue nobody ever handed to Sigma is not corruption.
        managed = any(o in names for o in overlays) or any(h in names for h in hints)
        return ZERO_LABEL if managed else CLEAN
    return CLEAN


def census(sdlc_dir, config, run=None, source=None):
    """Survey every issue Sigma is responsible for. READ-ONLY.

    Returns `{"complete": bool, "counts": {bucket: n}, "issues": {bucket: [numbers]},
    "checked": n, "queries": n, "failed": [label], "truncated": [label]}`.

    `complete` is False if ANY underlying query failed OR came back at the `--limit` ceiling. A
    caller must never report a clean board off an incomplete census — "I found nothing" and "I could
    not look" are different answers, and conflating them is how a transient outage becomes a false
    all-clear. `failed` and `truncated` name WHICH queries degraded and how; `_incomplete_reason`
    turns them into a sentence, because those two have different remedies.

    Enumerators, all owning their own reads (see the module docstring for why):
      E1 — one query per PRIMARY lifecycle label (3, since #1393 moved `goal_blocked_label` out).
      E2 — one query per OVERLAY label. This is the only enumerator that can see the invisible
           orphan class (an overlay carried with no primary label at all).
      E2b — one query per MANAGED HINT, for the issue whose only surviving Sigma trace is an
           annotation.
      E3 — one query per PRIMARY **and** OVERLAY label over CLOSED issues, for the
           closed-with-stale-state class.
    Deduped by issue number, so an issue appearing in several queries is classified once."""
    if not mirror.is_github_mode(config):
        return {"complete": True, "counts": {}, "issues": {}, "by_number": {}, "checked": 0,
                "queries": 0, "skipped": "not github discovery mode"}
    source = source or sources.GitHubSource(config, run=run)
    primary = _primary_labels(source, config)
    overlays = _overlay_labels(source)
    hints = _managed_hints(source, config)
    seen, queries = {}, 0
    failed, truncated = [], []      # #1579: WHICH queries degraded, and how — see `_fetch_by_label`

    # E1 + E2 + E2b. The ANNOTATION labels are not optional here, and leaving them out is the
    # absence trap in miniature: an issue that lost its lifecycle label but kept `sdlc:followup` is
    # returned by NONE of the primary queries, so without this the census reports zero zero-label
    # issues on a repo that demonstrably has them (measured: #1242, #888, #1316 on this repo were
    # all invisible to the first version of this loop).
    for label in dict.fromkeys((*primary, *overlays, *hints)):
        issues, ok, cut = _fetch_by_label(source, label, state="open")
        queries += 1
        if not ok:
            failed.append("%s (open)" % label)
        if cut:
            truncated.append("%s (open)" % label)
        for i in issues:
            seen[i["number"]] = i

    # E3 — the same vocabulary, over CLOSED issues.
    #
    # #1579: `primary` was MISSING from this tuple. The docstring four lines above has always said
    # "one query per primary label over CLOSED issues"; the code queried the overlays plus
    # `parked_label` only, so `goal_label` and `proposed_label` were never fetched closed at all.
    # That was not an aspirational comment — it was the spec, and the loop had quietly dropped half
    # of it, which is why the docstring is left standing rather than softened to match the code.
    #
    # The cost was invisible until #1445 taught `classify` to flag `goal_label` on a closed issue and
    # did not teach this loop to go and get one. The only closed goal-labelled issues the census ever
    # saw were the handful that happened to ALSO carry an overlay — measured on this repo, 24 closed
    # issues carry `sdlc:goal` and exactly ONE of them also carries an overlay. `classify`'s own
    # comment predicted this bug facing the other way: "a corrector that can act on something the
    # census never reports is the same bug".
    #
    # `dict.fromkeys` for the reason `_managed_hints` uses it: an adopter who points two label config
    # keys at the same string must not pay for the same query twice.
    #
    # Widening the ENUMERATOR is not widening the CORRECTOR. `_stale_on_closed` takes `primary` and
    # never reads it — it strips the overlays plus `goal_label` and nothing else — so a closed issue
    # carrying `sdlc:parked` or `sdlc:needs-confirmation` still keeps it. That half of #1393 (a
    # HUMAN's decision survives a close) is deliberately untouched here.
    for label in dict.fromkeys((*primary, *overlays)):  # E3
        issues, ok, cut = _fetch_by_label(source, label, state="closed")
        queries += 1
        if not ok:
            failed.append("%s (closed)" % label)
        if cut:
            truncated.append("%s (closed)" % label)
        for i in issues:
            seen.setdefault(i["number"], i)

    counts, issues_by_bucket = {}, {}
    for n, issue in seen.items():
        bucket = classify(issue, primary, overlays, source.goal_label, hints=hints,
                          needs_triage_label=source.needs_triage_label)
        counts[bucket] = counts.get(bucket, 0) + 1
        issues_by_bucket.setdefault(bucket, []).append(n)
    for bucket in issues_by_bucket:
        issues_by_bucket[bucket].sort()
    return {"complete": not failed and not truncated, "counts": counts, "issues": issues_by_bucket,
            "by_number": seen, "checked": len(seen), "queries": queries,
            "failed": failed, "truncated": truncated}


def _incomplete_reason(result):
    """WHY a census is not complete, in the words of the actual cause. #1579.

    Two different degradations clear `complete` and they do not have the same remedy: a query that
    FAILED (transport — re-run when `gh` is healthy) and a query that came back at the `--limit`
    ceiling (the population outgrew the read — raise `GitHubSource._BOARD_ITEM_LIMIT`, or let the
    corrector drain the backlog it is already draining). Reporting either as "at least one query
    failed" would leave a statement of the rule behind a change to the rule, which is the exact
    defect #1579 is about.

    Degrades to the old sentence for a census dict that predates these keys — `_cen_for`-style
    hand-built results in tests, and any caller holding an older payload."""
    failed = result.get("failed") or []
    cut = result.get("truncated") or []
    parts = []
    if failed:
        parts.append("at least one query failed (%s)" % ", ".join(failed))
    if cut:
        parts.append("%s came back at the --limit ceiling (%d) and may be TRUNCATED — raise "
                     "GitHubSource._BOARD_ITEM_LIMIT"
                     % (", ".join(cut), sources.GitHubSource._BOARD_ITEM_LIMIT))
    return "; ".join(parts) or "at least one query failed"


# --- #1391 step 5b: THE TIMELINE ORACLE ----------------------------------------------------------
# Detection is easy; knowing the CORRECT state is the hard part. #1349's own audit found #226
# carrying `sdlc:goal` + `sdlc:parked` and deliberately REFUSED to auto-fix it, because nothing in
# the data model could say which label was true. GitHub's issue timeline can.
#
# MEASURED, live, before this was written:
#   - `timelineItems(itemTypes:[LABELED_EVENT, UNLABELED_EVENT])` returns every label add/remove
#     with `createdAt`, `actor.login` and `label.name`. Replaying it reconstructs the CURRENT label
#     set byte-exactly on 130/130 issues tested, with zero issues missing history. It is lossless.
#   - Cost is ~1 GraphQL point per 25-issue batch (5 points for all 121 legacy cases).
#   - GraphQL, NOT REST: GraphQL resolves label names LIVE, so it self-heals across a rename (the
#     #1348 `sdlc:proposed` -> `sdlc:needs-confirmation` rename would otherwise poison replay).
#     REST returns the historical snapshot and would report the old name.
#   - All 8 live multi-label tangles across both real repos resolve unambiguously by last-add-wins,
#     with a MINIMUM separation of 1.1 HOURS between the competing adds, while a real `_offboard`
#     transaction spans 4 SECONDS. Three orders of magnitude. #226 resolves to `parked` by a
#     45.8-hour margin.
#
# That gap is what makes the window below safe: inside it, two adds are one transaction and cannot
# be told apart, so we REFUSE. Outside it, they are separate intents and the later one is current.

#: Two primary-label adds closer together than this are one transaction, not two intents. Set from
#: the measured gap above with ~4 orders of magnitude of headroom on the transaction side and ~2 on
#: the intent side. Widening it trades false "ambiguous" for false confidence; do not raise it
#: without re-measuring the real separation.
TRANSACTION_WINDOW_SECONDS = 60

AMBIGUOUS = "ambiguous"


def _parse_ts(value):
    """ISO-8601 -> epoch seconds. Returns None on anything unparseable (never raises)."""
    try:
        from datetime import datetime, timezone
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except Exception:
        return None


def fetch_label_history(source, numbers, batch=25):
    """`{number: [(epoch, "add"|"remove", label, actor), ...]}` oldest-first, for each issue number.

    ONE GraphQL document per `batch` issues, using aliased root fields — the whole point of the cost
    measurement above. FAIL-OPEN per batch: a failed batch contributes nothing rather than raising,
    because this oracle informs a PROPOSAL, and a proposal that cannot be evidenced simply is not
    made. `last: 100` is deliberate: label history is short in practice, and truncation would silently
    drop the OLDEST events, which is the safe end to lose (recency is what decides)."""
    owner, name = source._owner_name()
    if not owner:
        return {}
    out = {}
    numbers = [int(n) for n in numbers]
    for start in range(0, len(numbers), batch):
        chunk = numbers[start:start + batch]
        fields = " ".join(
            'i%d: issue(number: %d) { timelineItems(last: 100, itemTypes: [LABELED_EVENT, '
            'UNLABELED_EVENT]) { nodes { __typename ... on LabeledEvent { createdAt actor { login } '
            'label { name } } ... on UnlabeledEvent { createdAt actor { login } label { name } } } } }'
            % (n, n) for n in chunk)
        doc = 'query { repository(owner: "%s", name: "%s") { %s } }' % (owner, name, fields)
        try:
            data = source._graphql(doc)
        except Exception:
            continue
        repo = (data or {}).get("repository") or {}
        for n in chunk:
            node = repo.get("i%d" % n) or {}
            events = []
            for ev in ((node.get("timelineItems") or {}).get("nodes") or []):
                if not isinstance(ev, dict):
                    continue
                ts = _parse_ts(ev.get("createdAt"))
                label = ((ev.get("label") or {}).get("name")) or ""
                if ts is None or not label:
                    continue
                action = "add" if ev.get("__typename") == "LabeledEvent" else "remove"
                events.append((ts, action, label, ((ev.get("actor") or {}).get("login")) or ""))
            events.sort(key=lambda e: e[0])
            out[n] = events
    return out


def resolve_primary(events, primary, window=TRANSACTION_WINDOW_SECONDS):
    """Which primary lifecycle label is authoritative for an issue, from its label history.

    Returns `(label, reason)` when it can be decided, or `(AMBIGUOUS, reason)` when it genuinely
    cannot — and REFUSING is a first-class outcome here, not a failure. Guessing on a state a human
    may have set deliberately is exactly what #1349 declined to do, and this must inherit that.

    Method: replay the events to find which primary labels are live NOW; if exactly one is, there is
    nothing to resolve. If several are, compare the most recent ADD of each — separated by more than
    `window` they are distinct intents and the LATER one is current; inside it they are one
    transaction whose ordering carries no meaning, so the answer is AMBIGUOUS."""
    live, last_add = set(), {}
    for ts, action, label, _actor in events:
        if label not in primary:
            continue
        if action == "add":
            live.add(label)
            last_add[label] = ts
        else:
            live.discard(label)
    if not live:
        return None, "no primary label is live"
    if len(live) == 1:
        only = next(iter(live))
        return only, "the only live primary label"
    ranked = sorted(live, key=lambda l: last_add.get(l, 0), reverse=True)
    top, second = ranked[0], ranked[1]
    gap = last_add.get(top, 0) - last_add.get(second, 0)
    if gap <= window:
        return AMBIGUOUS, ("%s and %s were added %.0fs apart — inside the %ds transaction window, "
                           "so their order carries no intent" % (top, second, gap, window))
    return top, ("%s was added %.1fh after %s" % (top, gap / 3600.0, second))


# --- #1391 step 5c: the AUTOMATIC tier -----------------------------------------------------------
# The ONLY class corrected without a human, and it is deliberately the narrowest one that is
# provably safe: a CLOSED issue still carrying `in_progress_label` or a not-eligible OVERLAY.
#
# WHY THIS CLASS AND NOTHING ELSE. An adversarial review of the corrector design found that its
# worst realistic harm is releasing a goal whose worker is still live — worktree paths are
# deterministic, and `_release()` clears both the claim and the liveness marker, so the next picker
# attaches to a LIVE agent's directory and runs `git add -A` there. That harm requires an OPEN goal
# a picker can serve. It cannot occur here: every picker query filters `--state open`, so a closed
# issue is unreachable by the picker by construction, has no claim, and has no worker. Stripping a
# stale label from it can wake nothing up.
#
# What it fixes is real and pre-loaded: 137 closed issues on this repo alone carry stale state.
# Each is inert TODAY and becomes a live orphan the moment someone reopens it.
#
# The membership label is NEVER touched — a closed issue keeping `sdlc:goal` is the designed
# terminal state (and the reason the 334 such issues here need no migration).

#: An issue closed more sec ago than this is not mid-transition. A real `_offboard` spans ~4 seconds;
#: this is ~5 orders of magnitude of headroom, and it also means a human who closes an issue by hand
#: has a full day to fix its labels before anything automatic touches them.
CLOSED_SETTLE_SECONDS = 24 * 3600

#: Never correct more than this many issues in one sweep. A corrector that suddenly rewrites
#: hundreds of issues is indistinguishable from a runaway, and the backlog it is fixing is not
#: urgent — 137 stale issues drain in a few sweeps and nothing is worse for waiting.
MAX_CORRECTIONS_PER_SWEEP = 25


def _stale_on_closed(issue, primary, overlays, goal_label):
    """The labels that should not survive on a CLOSED issue: the OVERLAYS, and only those.

    #1393 corrects two bugs here that an adversarial audit found, and they pull in opposite
    directions — one made this strip too much, the other too little:

      TOO MUCH: it used to compute the stale set as "every primary label except `goal_label`",
      which since the membership refactor means `sdlc:parked` and `sdlc:needs-confirmation`. Those
      are MEMBERSHIP labels, not overlays — a human's park and a human's withheld approval. Feeding
      them to the AUTOMATIC (ungated, no-human) correction tier meant a closed-and-parked issue
      would have its park silently stripped, and a closed proposal its approval gate. A closed issue
      keeping a membership label is the designed terminal state; only the overlays are stale.

      TOO LITTLE: `goal_blocked_label` was not in the set at all once it left `primary`, so a closed
      issue carrying `sdlc:blocked` was flagged by `classify` as CLOSED_WITH_STATE and then never
      corrected — the census reporting an anomaly the corrector could not act on, forever.

    Both disappear by naming the OVERLAYS explicitly, which is what "stale on a closed issue" always
    meant: reopen it and a false ACTIVITY state comes back to life.

    #1445 ADDS `goal_label` to that set, which REVISES the sentence above ("a closed issue keeping a
    membership label is the designed terminal state"). Stated plainly rather than quietly, because it
    narrows an explicit prior decision:

      * #1393's actual bug was `sdlc:parked` and `sdlc:needs-confirmation` being stripped -- those
        record a HUMAN's decision (a park, a withheld approval) and must survive a close. That half
        is UNCHANGED and still deliberate.
      * `goal_label` is not a human decision; it is "the loop may pick this". On a closed issue it
        asserts membership of a queue the issue cannot be in, and it contradicts the documented
        terminal state ("a Done issue carries no `sdlc:*` label"). It also fails this docstring's
        OWN test: reopen the issue and it silently re-enters the queue without anyone re-approving
        it, which is exactly the false state this function exists to prevent.

    So the rule is now: on a closed issue the OVERLAYS are stale, and so is MEMBERSHIP-BY-GOAL;
    membership that encodes a human decision is not."""
    names = _names(issue)
    stale = [l for l in overlays if l in names]
    if goal_label and goal_label in names and goal_label not in stale:
        stale.append(goal_label)
    return stale


def compute_closed_state_actions(source, census_result, primary, now=None,
                                 cap=MAX_CORRECTIONS_PER_SWEEP):
    """`[{issue, remove: [...], reason}]` for every closed issue eligible for automatic correction.

    Pure — no I/O. Eligibility is deliberately conservative: the issue must be CLOSED, must have
    been closed longer than `CLOSED_SETTLE_SECONDS`, and must actually carry something stale. An
    issue whose `closedAt` cannot be parsed is SKIPPED, not assumed old."""
    import time as _time
    now = now if now is not None else _time.time()
    out = []
    for number in sorted(census_result["issues"].get(CLOSED_WITH_STATE, [])):
        issue = census_result["by_number"].get(number)
        if not issue:
            continue
        closed_at = _parse_ts(issue.get("closedAt"))
        if closed_at is None or (now - closed_at) < CLOSED_SETTLE_SECONDS:
            continue                          # mid-transition, or a timestamp we cannot trust
        stale = _stale_on_closed(issue, primary, _overlay_labels(source), source.goal_label)
        if not stale:
            continue
        out.append({"issue": str(number), "remove": stale,
                    "reason": "closed %.1fh ago, still carrying %s"
                              % ((now - closed_at) / 3600.0, ", ".join(stale))})
        if len(out) >= cap:
            break
    return out


#: #1579 — BLOCKING half of the E3 fix, and it had to ship in the same commit. This sentence used
#: to end "the goal label is left untouched", which #1445 made false when it added `goal_label` to
#: `_stale_on_closed`. It stayed harmless only because the enumerator never fetched a closed
#: goal-labelled issue, so `{stale}` in practice never contained `sdlc:goal`. Widening E3 makes it
#: routine — the first sweep would have posted "Removing sdlc:goal, sdlc:in-progress; the goal label
#: is left untouched" to GitHub, on 19 issues here and hundreds on `os`. A change that leaves an
#: old statement of the rule standing IS this issue's defect, so fixing it here and not fixing it
#: there would have been the same bug shipped twice.
#:
#: What replaces it says the rule that is actually true now (`_stale_on_closed`'s closing line): on a
#: closed issue the overlays are stale and so is membership-BY-GOAL; membership that encodes a
#: HUMAN's decision is not.
_AUDIT_COMMENT = ("Reconciled by Sigma — this issue is closed but still carried {stale}, which "
                  "would have come back to life as a false state if it were ever reopened. "
                  "Removing {stale}. A label that records a human's decision — `sdlc:parked`, "
                  "`sdlc:needs-confirmation` — is never removed here.")


def apply_closed_state_actions(source, actions, apply=False):
    """Execute (or dry-run) the automatic corrections. Returns the actions with `result` filled in.

    Three safeguards worth naming, all from the corrector-harm review:
      1. RE-READ IMMEDIATELY BEFORE THE WRITE. The census may be seconds or minutes old; an issue
         reopened in between must not be corrected. There is no compare-and-set on GitHub labels, so
         a fresh read is the closest available thing and it closes the realistic window.
      2. AUDIT COMMENT FIRST, then the label write. If the write then fails, the record of what was
         attempted still exists — the reverse order can lose it silently.
      3. ONE swap per issue, through the same atomic primitive every lifecycle transition uses."""
    done = []
    for action in actions:
        record = dict(action, result=None, error=None)
        if not apply:
            record["result"] = "would"
            done.append(record)
            continue
        try:
            fresh = source._read_issue(action["issue"], ["state", "labels"])    # #895: REST first
            names = _names(fresh)
            if str(fresh.get("state") or "").upper() != "CLOSED":
                record["result"], record["error"] = "skipped", "reopened since the census"
                done.append(record)
                continue
            still = [l for l in action["remove"] if l in names]
            if not still:
                record["result"], record["error"] = "skipped", "already clean"
                done.append(record)
                continue
        except Exception as exc:
            record["result"], record["error"] = "skipped", "could not re-read: %s" % exc
            done.append(record)
            continue
        try:
            source.note(action["issue"], _AUDIT_COMMENT.format(stale=", ".join(still)))
        except Exception:
            pass                              # best-effort audit trail; never blocks the fix
        try:
            source._swap_labels(action["issue"], remove=still)
            record["result"] = "done"
        except Exception as exc:
            record["result"], record["error"] = "failed", str(exc)
        done.append(record)
    return done


_BUCKET_BLURB = {
    MULTI_LABEL: "carry more than one primary lifecycle label",
    ZERO_LABEL: "are managed by Sigma but carry NO primary lifecycle label (invisible to the picker)",
    # #1579 audited all four statements of the closed-issue rule when E3 was widened. This one was
    # already correct — #1445 updated it when it changed the rule — so it is unchanged, and this
    # comment exists so the next reader does not have to re-derive that it was checked.
    CLOSED_WITH_STATE: ("are closed but still carry the goal label, in-progress, or a "
                        "not-eligible overlay"),
    # #2364: not corruption -- see NEEDS_TRIAGE's own comment above the bucket constants. Reported
    # anyway because it names something a human still owes the issue: a unit assignment.
    NEEDS_TRIAGE: ("carry sdlc:needs-triage -- every automatic classification tier failed, "
                  "waiting on a human to assign a unit"),
}


def render_census(result):
    if result.get("skipped"):
        return "census: skipped — %s" % result["skipped"]
    lines = ["census: %d issue(s) checked over %d quer%s%s"
             % (result["checked"], result["queries"], "y" if result["queries"] == 1 else "ies",
                "" if result["complete"]
                else "  ** INCOMPLETE — %s **" % _incomplete_reason(result))]
    anomalies = [(b, ns) for b, ns in sorted(result["issues"].items()) if b != CLEAN]
    if not anomalies:
        lines.append("  no anomalies found" if result["complete"]
                     else "  no anomalies found IN WHAT COULD BE READ")
        return "\n".join(lines)
    for bucket, numbers in anomalies:
        shown = ", ".join("#%s" % n for n in numbers[:10])
        more = " (+%d more)" % (len(numbers) - 10) if len(numbers) > 10 else ""
        lines.append("  %-18s %d — %s: %s%s"
                     % (bucket, len(numbers), _BUCKET_BLURB.get(bucket, ""), shown, more))
    return "\n".join(lines)


# --- #1391 step 5d: the PROPOSED tier ------------------------------------------------------------
# Corrections that the timeline can EVIDENCE but that must not be applied unattended, because they
# touch OPEN issues — where a wrong write can collide with live work, and where a human may have set
# the state deliberately. Each proposal carries the evidence that produced it, so approving one is a
# judgement on facts rather than on trust.
#
# Reuses `triage`'s artifact convention deliberately (atomic write, a schema key, an `active.json`
# pointer) rather than inventing a second one — that chain is the repo's established
# survey -> artifact -> human-approves -> apply shape, and this is the same shape.

PROPOSAL_SCHEMA = "reconcile-proposal/1"
_PROPOSALS_DIR = "plans/reconcile"


def compute_proposals(source, census_result, primary, history, overlays=None, goal_label=None):
    """`[{issue, add, remove, evidence, klass, winner}]` — one entry per anomaly the timeline can
    decide.

    Pure: `history` is already-fetched label history (see `fetch_label_history`). Anything the
    oracle calls AMBIGUOUS is deliberately NOT proposed — it is left for a human, which is the whole
    reason the oracle has that answer at all.

    #2295: `winner` is carried on every entry — for MULTI_LABEL it is `resolve_primary`'s own return
    value; for ZERO_LABEL it is the primary label being restored. This is additive (every existing
    caller/test that reads `issue`/`add`/`remove`/`evidence`/`klass` is unaffected) and it is what
    `sweep_reconcile`'s open-issue promotion step (design #2287 detailed design section 2)
    filters on: `winner == goal_label`, full stop — never a re-parse of the `evidence` string."""
    # Defaulted so every existing caller/test keeps working unchanged; resolved from `source` when
    # not supplied, which is what every real caller has available.
    overlays = tuple(overlays if overlays is not None else _overlay_labels(source))
    goal_label = goal_label if goal_label is not None else source.goal_label
    proposals, unresolved = [], []
    for number in sorted(census_result["issues"].get(MULTI_LABEL, [])):
        events = history.get(number)
        if not events:
            unresolved.append({"issue": str(number), "why": "no label history available"})
            continue
        winner, why = resolve_primary(events, primary)
        if winner == AMBIGUOUS or winner is None:
            unresolved.append({"issue": str(number), "why": why})
            continue
        names = _names(census_result["by_number"].get(number, {}))
        present = [p for p in primary if p in names]
        losers = [p for p in present if p != winner]
        # #1393: when the winning membership label is an EXIT (`sdlc:parked` / awaiting approval),
        # the overlays have to go with it or applying the approved proposal produces
        # `sdlc:parked` + `sdlc:in-progress` -- the reconciler leaving behind a state one step worse
        # than the one it was asked to repair. `goal_label` winning keeps them: they are legitimate
        # alongside membership, which is the whole point of an overlay.
        if winner != goal_label:
            losers += [o for o in overlays if o in names]
        if losers:
            proposals.append({"issue": str(number), "klass": MULTI_LABEL,
                              "add": [], "remove": losers, "winner": winner,
                              "evidence": "%s is authoritative: %s" % (winner, why)})

    for number in sorted(census_result["issues"].get(ZERO_LABEL, [])):
        events = history.get(number)
        if not events:
            unresolved.append({"issue": str(number), "why": "no label history available"})
            continue
        # What was the last PRIMARY label this issue carried before it lost them all? A label that
        # was explicitly REMOVED is recoverable evidence; an issue that never had one is not, and is
        # left to a human (per the correction-evidence ruling for that class).
        removed = [(ts, label) for ts, action, label, _a in events
                   if action == "remove" and label in primary]
        if not removed:
            unresolved.append({"issue": str(number),
                               "why": "never carried a primary label — nothing to restore"})
            continue
        ts, label = max(removed, key=lambda r: r[0])
        proposals.append({"issue": str(number), "klass": ZERO_LABEL,
                          "add": [label], "remove": [], "winner": label,
                          "evidence": "%s was the last primary label, removed at %s" % (
                              label, _iso(ts))})
    return proposals, unresolved


def _iso(epoch):
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return "?"


def write_proposal(sdlc_dir, proposals, unresolved, generated_at=None):
    """Atomically write the proposal artifact + an `active.json` pointer. Returns the json path."""
    import tempfile, os
    from datetime import datetime, timezone
    generated_at = generated_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    base = pathlib.Path(sdlc_dir) / _PROPOSALS_DIR
    # Review bug_003: the stem used to be `generated_at[:10]` -- the DATE only -- so two `propose`
    # runs on the same UTC day produced the same filename and the atomic `os.replace` below silently
    # rewrote the first. The README tells operators to apply by explicit path, and `propose_cmd`
    # prints that path, so a human who reviewed proposal A and then re-ran `propose` (after a `gh`
    # hiccup, say) would apply proposal B's contents from the path they had reviewed A at: a
    # different issue set, different label changes, different evidence. `apply_proposal`'s per-issue
    # re-read cannot catch it — an entry that is new in B has no drift to trip on.
    #
    # The whole safety property of the PROPOSED tier is that a human applies exactly what they
    # approved, so the filename carries the time.
    name = "%s-reconcile.json" % generated_at[:19].replace(":", "")
    payload = {"schema": PROPOSAL_SCHEMA, "generated_at": generated_at,
               "proposals": proposals, "unresolved": unresolved}

    def _write(path, text):
        path = pathlib.Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, str(path))

    _write(base / name, json.dumps(payload, indent=2))
    _write(base / "active.json", json.dumps(
        {"schema": PROPOSAL_SCHEMA, "proposal_json": name, "generated_at": generated_at}, indent=2))
    prune_proposals(base, keep_name=name)
    return str(base / name)


#: Cap (#1267): a proposal is removed only when it is BOTH beyond the newest PROPOSAL_KEEP (names sort by time)
#: AND older than PROPOSAL_MAX_AGE_DAYS, so a proposal a human is still reviewing by explicit path is not pulled
#: away; ceiling = 20 files plus whatever was written in the last 30 days. A `tmp*` file of a crashed write
#: (mkstemp, no suffix) older than a day is removed too. Nothing else in the directory is touched.
PROPOSAL_KEEP = 20
PROPOSAL_MAX_AGE_DAYS = 30
_TMP_MAX_AGE_SECONDS = 86400


def prune_proposals(base, keep_name=None, now=None):
    import os, stat, time
    now = time.time() if now is None else now
    base = pathlib.Path(base)

    def remove_if_old(p, max_age):      # a regular file only (lstat: a link is never followed or removed); per-entry errors skip
        try:
            st = os.lstat(p)
            if stat.S_ISREG(st.st_mode) and now - st.st_mtime > max_age:
                p.unlink()
        except OSError:
            pass

    try:
        names = sorted(p.name for p in base.glob("*-reconcile.json"))
        for name in names[:-PROPOSAL_KEEP] if len(names) > PROPOSAL_KEEP else []:
            if name != keep_name:
                remove_if_old(base / name, PROPOSAL_MAX_AGE_DAYS * 86400)
        for p in list(base.glob("tmp*")):
            remove_if_old(p, _TMP_MAX_AGE_SECONDS)
    except OSError:
        pass


def load_proposal(path):
    """Read + validate a proposal artifact. RAISES `ValueError` naming the concrete problem — the
    artifact is the sole input to an apply, so a bad file must be a hard failure, never a silent
    empty run. Mirrors `triage._load_plan`'s own posture exactly."""
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError("could not read %r: %s" % (path, exc)) from exc
    except ValueError as exc:
        raise ValueError("%r is not valid JSON: %s" % (path, exc)) from exc
    if not isinstance(data, dict):
        raise ValueError("%r is not a JSON object" % path)
    if not str(data.get("schema") or "").startswith("reconcile-proposal/"):
        raise ValueError("%r has schema %r, expected reconcile-proposal/*"
                         % (path, data.get("schema")))
    if not isinstance(data.get("proposals"), list):
        raise ValueError("%r: 'proposals' must be a list" % path)
    return data


_PROPOSAL_COMMENT = ("Reconciled by Sigma (human-approved) — {evidence}. Applying: {change}.")


def apply_proposal(source, proposal, apply=False):
    """Execute an approved proposal. Same three safeguards as the automatic tier: re-read
    immediately before the write, audit comment first, one atomic swap per issue."""
    out = []
    for item in proposal.get("proposals", []):
        record = dict(item, result=None, error=None)
        add, remove = item.get("add") or [], item.get("remove") or []
        if not apply:
            record["result"] = "would"
            out.append(record)
            continue
        try:
            names = _names(source._read_issue(item["issue"], ["state", "labels"]))    # #895: REST first
        except Exception as exc:
            record["result"], record["error"] = "skipped", "could not re-read: %s" % exc
            out.append(record)
            continue
        still_remove = [l for l in remove if l in names]
        still_add = [l for l in add if l not in names]
        if not still_remove and not still_add:
            record["result"], record["error"] = "skipped", "already in the proposed state"
            out.append(record)
            continue
        change = ", ".join(["+%s" % l for l in still_add] + ["-%s" % l for l in still_remove])
        try:
            source.note(item["issue"], _PROPOSAL_COMMENT.format(
                evidence=item.get("evidence", "reconciliation"), change=change))
        except Exception:
            pass
        try:
            source._swap_labels(item["issue"], add=still_add, remove=still_remove)
            record["result"] = "done"
        except Exception as exc:
            record["result"], record["error"] = "failed", str(exc)
        out.append(record)
    return out


# --- #2295 (design #2287 detailed design section 2): promote DECISIVE open-issue --------
# corrections from the PROPOSED tier to AUTOMATIC.
#
# B-1 (the design's own blocker): this is a NEW, not-yet-adversarially-reviewed write mechanism on
# OPEN issues — a different risk class from the closed-issue AUTOMATIC tier above, whose own
# corrector-harm review is BR-4's cited precedent. So unlike that tier (deliberately UNGATED once
# `sweep_reconcile` is called — module docstring, "a direct call IS the opt-in"), this one carries
# its OWN config gate, checked INSIDE `sweep_reconcile` itself rather than only at loop.py's
# unattended call site. That is a deliberate departure from this module's usual gating philosophy,
# and it is deliberate for exactly one reason: `discovery.reconcile.mode` already means something
# else (BR-4's reviewed, closed-issue-only tier) on every repo that has already turned it on, and a
# repo with `mode: "on"` must not silently start auto-applying to OPEN issues the moment this code
# ships. `open_issue_mode` is therefore a SEPARATE, independently-off-by-default sub-key of the same
# `discovery.reconcile` block, not a second meaning bolted onto `mode` — the conservative reading of
# an ambiguous design instruction, recorded here rather than assumed silently.

_OPEN_ISSUE_MODES = ("off", "on")


def _open_issue_promotion_mode(config):
    """`discovery.reconcile.open_issue_mode` -> 'off' (default) | 'on'. Same defensive shape as
    `loop.py`'s `_reconcile_mode`/`sources._auto_unpark`: an unrecognised or missing value reads as
    off, because a typo must never switch on a mechanism that WRITES. Deliberately INDEPENDENT of
    `discovery.reconcile.mode` — see the block comment above for why the two are not one gate."""
    block = ((config or {}).get("discovery") or {}).get("reconcile")
    value = block.get("open_issue_mode") if isinstance(block, dict) else None
    return value if value in _OPEN_ISSUE_MODES else "off"


def _recompute_winner(klass, primary, fresh_names, fresh_events):
    """Re-derive the winner for ONE issue from a FRESH read, mirroring `compute_proposals`'s own
    per-class logic exactly. Used only by `apply_open_issue_promotions`'s pre-write eligibility
    re-check — never by the human-reviewed propose/apply flow, which always re-derives everything
    from a fresh `propose` run instead.

    Returns the recomputed winner label, or `None` when it can no longer be decided (including the
    AMBIGUOUS case — `resolve_primary` may return `AMBIGUOUS`, and that is correctly not
    `goal_label`, so the caller's plain `winner != goal_label` test aborts on it without having to
    special-case it).

    ZERO_LABEL is checked two ways, per round-2 goal-review's own flagged ambiguity (the design
    stated this precisely for MULTI_LABEL but only "re-fetch current labels" for ZERO_LABEL):
      1. the issue must STILL carry no primary label at all — if one is already present, something
         else already changed it, and re-adding a label now would be a second, unreviewed write on
         top of whatever already happened;
      2. the most-recently-removed primary label, by the FRESH history, must still be the same one
         the census-time proposal named — if a different label was removed more recently since, that
         is the correct restore candidate now, not the stale one."""
    if klass == MULTI_LABEL:
        winner, _why = resolve_primary(fresh_events, primary)
        return winner if winner != AMBIGUOUS else None
    if klass == ZERO_LABEL:
        if any(p in fresh_names for p in primary):
            return None                      # no longer zero-label -- something already changed
        removed = [(ts, label) for ts, action, label, _a in fresh_events
                   if action == "remove" and label in primary]
        if not removed:
            return None
        _ts, label = max(removed, key=lambda r: r[0])
        return label
    return None


_AUTO_OPEN_PROPOSAL_COMMENT = (
    "Reconciled by Sigma (auto-applied — decisive timeline evidence, no human approval) — "
    "{evidence}. Applying: {change}. This tier only ever restores or confirms `sdlc:goal` "
    "membership; it never removes `sdlc:in-progress`, `sdlc:blocked`, or any other overlay label.")


def apply_open_issue_promotions(source, proposals, primary, goal_label, apply=False):
    """Apply the PROMOTED subset of `compute_proposals`'s output — entries where `winner ==
    goal_label` — unattended, the same way `apply_closed_state_actions` already does for the
    closed-issue tier. `proposals` must already be filtered to that promotable subset and capped by
    the caller (`sweep_reconcile`); this function re-verifies eligibility per item regardless, on
    the premise that census time and write time are different moments.

    THE NEW SAFEGUARD (design #2287 detailed design section 2, round-2 goal-review):
    `apply_proposal`'s existing re-read (`reconcile.py:794-833` at review time) only re-verifies the
    SPECIFIC labels already queued for change are still stale — never the eligibility CONDITION
    itself. A human can act on the same issue in the window between `census()` and this write (most
    concretely: deliberately re-parking an issue the census saw as MULTI_LABEL
    `{goal_label, parked_label}` by removing `goal_label` themselves, leaving only `parked_label`) —
    `apply_proposal`'s check would still see the queued `remove: [parked_label]` present in a fresh
    read and still remove it, silently undoing the human's own re-park and leaving the issue with
    NEITHER label. So this function re-fetches the issue fresh, re-fetches its label HISTORY fresh,
    and re-derives the winner from that fresh evidence (`_recompute_winner`) BEFORE ever computing a
    label diff — mirroring `apply_closed_state_actions`'s own shape (a fresh re-check of the
    eligibility condition, not just of the specific labels) at `reconcile.py:579-624`.

    Same three safeguards `apply_proposal` already gives every write: re-read immediately before
    the write, audit comment first, one atomic swap — plus this one, new, additional re-check."""
    out = []
    for item in proposals:
        record = dict(item, result=None, error=None)
        if not apply:
            record["result"] = "would"
            out.append(record)
            continue
        issue_ref = item["issue"]
        try:
            number = int(issue_ref)
        except (TypeError, ValueError):
            record["result"], record["error"] = "skipped", "not a valid issue number"
            out.append(record)
            continue
        try:
            fresh = source._read_issue(issue_ref, ["state", "labels"])    # #895: REST first
        except Exception as exc:
            record["result"], record["error"] = "skipped", "could not re-read: %s" % exc
            out.append(record)
            continue
        if str(fresh.get("state") or "").upper() != "OPEN":
            record["result"], record["error"] = "skipped", "no longer open since the census"
            out.append(record)
            continue
        fresh_names = _names(fresh)
        history = fetch_label_history(source, [number])
        events = history.get(number) or []
        if not events:
            record["result"], record["error"] = "skipped", "no label history available on re-check"
            out.append(record)
            continue
        winner = _recompute_winner(item.get("klass"), primary, fresh_names, events)
        if winner != goal_label:
            record["result"] = "skipped"
            record["error"] = ("recomputed winner is %r, no longer %r — leaving to a later sweep's "
                               "PROPOSED tier" % (winner, goal_label))
            out.append(record)
            continue
        add, remove = item.get("add") or [], item.get("remove") or []
        still_remove = [l for l in remove if l in fresh_names]
        still_add = [l for l in add if l not in fresh_names]
        if not still_remove and not still_add:
            record["result"], record["error"] = "skipped", "already in the proposed state"
            out.append(record)
            continue
        change = ", ".join(["+%s" % l for l in still_add] + ["-%s" % l for l in still_remove])
        try:
            source.note(issue_ref, _AUTO_OPEN_PROPOSAL_COMMENT.format(
                evidence=item.get("evidence", "reconciliation"), change=change))
        except Exception:
            pass                              # best-effort audit trail; never blocks the fix
        try:
            source._swap_labels(issue_ref, add=still_add, remove=still_remove)
            record["result"] = "done"
        except Exception as exc:
            record["result"], record["error"] = "failed", str(exc)
        out.append(record)
    return out


def sweep_reconcile(sdlc_dir, config, apply=False, run=None):
    """One reconciliation pass: census, the closed-issue AUTOMATIC tier, and — only when
    `discovery.reconcile.open_issue_mode` is explicitly `"on"` (#2295, default `"off"`) — the
    open-issue promotion tier.

    The closed-issue tier is UNGATED once called, exactly like `auto_unpark.sweep_unpark` and
    `triage.enact` — a direct call (this module's CLI, or a human) IS the opt-in. The
    `discovery.reconcile.mode` config gate belongs at the unattended call site (`loop.py`), not
    here. The open-issue promotion tier is DIFFERENT and DELIBERATELY NOT UNGATED — see the block
    comment above `_open_issue_promotion_mode` for why it carries its own gate checked right here,
    regardless of caller.

    REFUSES on an incomplete census. Correction is a diff against what the census saw, so acting on
    a partial read risks "correcting" from data that was never fully gathered. A skipped pass costs
    nothing; the corruption the closed-issue tier fixes is inert by construction (closed issues) and
    waits happily.

    #1579 brings TRUNCATION under that same refusal, and the tradeoff is named rather than assumed.
    "Waits happily" stops being true at the ceiling: the population this corrector shrinks is the
    same one whose size caused the truncation, so refusing forever means it never shrinks. That
    deadlock was accepted over the alternative (correct from a knowingly partial read) for three
    reasons — it is far away (5000 closed issues on ONE label; 24 here, ~350 on `os`), it cannot be
    reached silently (`_warn_truncated` on stderr, `render_census`, and doctor's row all say so),
    and `_incomplete_reason` prints the one-line remedy in the refusal itself. A refusal that tells
    you how to clear it is not a deadlock in practice; a partial read that presents as complete is
    the bug this whole flag exists to prevent. The same refusal covers the open-issue tier too — it
    is a diff against the same census."""
    result = {"apply": bool(apply), "census": None, "actions": [], "open_actions": [],
              "refused": None}
    cen = census(sdlc_dir, config, run=run)
    result["census"] = cen
    if cen.get("skipped"):
        result["refused"] = cen["skipped"]
        return result
    if not cen.get("complete"):
        result["refused"] = ("census incomplete — %s; refusing to correct from a partial read"
                             % _incomplete_reason(cen))
        return result
    source = sources.GitHubSource(config, run=run)
    primary = _primary_labels(source, config)
    actions = compute_closed_state_actions(source, cen, primary)
    result["actions"] = apply_closed_state_actions(source, actions, apply=apply)

    # #2295: the open-issue promotion tier. Gated FIRST on `_open_issue_promotion_mode`, mirroring
    # `loop.py`'s own "gated first" precedent — an operator who never opts in pays literally nothing
    # here, not even the history-fetch GraphQL call, regardless of `discovery.reconcile.mode` or of
    # which entry point called `sweep_reconcile` (byte-identical-when-off).
    if _open_issue_promotion_mode(config) == "on":
        targets = sorted({*cen["issues"].get(MULTI_LABEL, []), *cen["issues"].get(ZERO_LABEL, [])})
        history = fetch_label_history(source, targets) if targets else {}
        proposals, _unresolved = compute_proposals(source, cen, primary, history)
        # Condition 2, full stop: `winner == goal_label`. See `## Detailed design` section 2 of
        # design #2287 for why this is narrower than "only adds or restores membership",
        # and `test_a_promoted_multi_label_correction_can_never_remove_an_overlay_by_construction`
        # for the structural proof that this alone makes overlay removal impossible here.
        promotable = [p for p in proposals if p.get("winner") == source.goal_label]
        result["open_actions"] = apply_open_issue_promotions(
            source, promotable[:MAX_CORRECTIONS_PER_SWEEP], primary, source.goal_label, apply=apply)
    return result


def render_sweep(result):
    lines = [render_census(result["census"] or {})]
    if result.get("refused"):
        lines.append("reconcile: REFUSED — %s" % result["refused"])
        return "\n".join(lines)
    mode = "APPLIED" if result["apply"] else "DRY-RUN"
    acts = result["actions"]
    lines.append("reconcile (%s): %d automatic correction(s)" % (mode, len(acts)))
    for a in acts:
        lines.append("  [%s] #%s: remove %s — %s%s"
                     % (a.get("result"), a["issue"], ", ".join(a["remove"]), a["reason"],
                        (" (%s)" % a["error"]) if a.get("error") else ""))
    if not acts:
        lines.append("  nothing eligible")
    # #2295: only printed when there is something to say — always empty when the gate is off, so
    # this line never appears and the rendered text stays byte-identical to before this slice.
    open_acts = result.get("open_actions") or []
    if open_acts:
        lines.append("reconcile open-issue promotion (%s): %d decisive correction(s)"
                     % (mode, len(open_acts)))
        for a in open_acts:
            change = ", ".join(["+%s" % l for l in a.get("add", [])]
                               + ["-%s" % l for l in a.get("remove", [])])
            lines.append("  [%s] #%s %s%s"
                         % (a.get("result"), a["issue"], change,
                            (" — %s" % a["error"]) if a.get("error") else ""))
    return "\n".join(lines)


_USAGE = ("usage: reconcile.py census|sweep|propose <sdlc_dir> [--apply]\n"
          "       reconcile.py apply <sdlc_dir> --plan <path> [--apply]\n"
          "       reconcile.py blockers <sdlc_dir>   (read-only preview of the blocker priority repair)")


def census_cmd(sdlc_dir, config, run=None):
    result = census(sdlc_dir, config, run=run)
    print(render_census(result))
    return 0


def propose_cmd(sdlc_dir, config, argv_tail, run=None):
    """Compute the PROPOSED tier and write the artifact. Read-only against GitHub — the only write
    is the local artifact, which a human then reviews and applies with `apply --plan <path>`."""
    cen = census(sdlc_dir, config, run=run)
    if cen.get("skipped"):
        print("propose: skipped — %s" % cen["skipped"])
        return 0
    if not cen.get("complete"):
        print("propose: REFUSED — census incomplete (%s); a partial read must not drive proposals"
              % _incomplete_reason(cen))
        return 1
    source = sources.GitHubSource(config, run=run)
    primary = _primary_labels(source, config)
    targets = sorted({*cen["issues"].get(MULTI_LABEL, []), *cen["issues"].get(ZERO_LABEL, [])})
    history = fetch_label_history(source, targets) if targets else {}
    proposals, unresolved = compute_proposals(source, cen, primary, history)
    path = write_proposal(sdlc_dir, proposals, unresolved)
    print(render_census(cen))
    print("propose: %d proposal(s), %d unresolved -> %s" % (len(proposals), len(unresolved), path))
    for pr in proposals:
        change = ", ".join(["+%s" % l for l in pr["add"]] + ["-%s" % l for l in pr["remove"]])
        print("  #%s  %s   (%s)" % (pr["issue"], change, pr["evidence"]))
    for u in unresolved:
        print("  #%s  NEEDS A HUMAN — %s" % (u["issue"], u["why"]))
    return 0


def apply_cmd(sdlc_dir, config, argv_tail, run=None):
    """Apply a previously-written proposal artifact. `--apply` is required; without it this is a
    dry-run, matching every other apply-shaped verb in this repo."""
    if "--plan" not in argv_tail:
        print("apply: --plan <path> is required", file=sys.stderr)
        return 2
    path = argv_tail[argv_tail.index("--plan") + 1]
    try:
        proposal = load_proposal(path)
    except ValueError as exc:
        print("apply: %s" % exc, file=sys.stderr)
        return 2
    source = sources.GitHubSource(config, run=run)
    results = apply_proposal(source, proposal, apply=("--apply" in argv_tail))
    mode = "APPLIED" if "--apply" in argv_tail else "DRY-RUN"
    print("apply (%s): %d proposal(s)" % (mode, len(results)))
    for r in results:
        change = ", ".join(["+%s" % l for l in r.get("add", [])]
                           + ["-%s" % l for l in r.get("remove", [])])
        print("  [%s] #%s %s%s" % (r["result"], r["issue"], change,
                                    (" — %s" % r["error"]) if r.get("error") else ""))
    return 1 if any(r["result"] == "failed" for r in results) else 0


def sweep_cmd(sdlc_dir, config, argv_tail, run=None):
    result = sweep_reconcile(sdlc_dir, config, apply=("--apply" in argv_tail), run=run)
    print(render_sweep(result))
    return 1 if any(a.get("result") == "failed" for a in result["actions"]) else 0


def blockers_cmd(sdlc_dir, config, run=None):
    """#1560: preview, read-only, every blocker the priority repair would raise. Writes nothing."""
    if (config.get("discovery") or {}).get("source") != "github":
        print("blockers: needs discovery.source github", file=sys.stderr)
        return 2
    src = sources.GitHubSource(config, run=run, sdlc_dir=sdlc_dir)
    res = src.repair_blocker_ranks(preview=True)
    print("blockers: blocker_promotion mode: %s (preview ignores it; nothing is written)" % src.blocker_promotion_mode)
    if res.get("skipped"):
        print("blockers: preview not available (%s)" % res["skipped"])
        return 1
    for p in res.get("plan", []):
        print("  #%s  %s -> %s  (blocks #%s)" % (p["blocker"], p["current"], p["new"], p["cause"]))
    if not res.get("plan"):
        print("  no blocker ranks below what it blocks")
    if res.get("foreign"):      # #1707: the repair refuses these (hard stops on), so they are not "to raise"
        print("  would be skipped: not a Sigma issue: #%s" % ", #".join(res["foreign"]))
    outside = len(res.get("unwritable", [])) + int(res.get("unread", 0))
    print("blockers: %d to raise, cycles: %d%s, outside or unreadable blockers: %d"
          % (len(res.get("plan", [])), len(res.get("cycles", [])),
             (" (#%s)" % ", #".join(res["cycles"])) if res.get("cycles") else "", outside))
    return 0


def main(argv, run=None):
    if argv[1:] in (["-h"], ["--help"]):
        print(_USAGE)
        return 0
    if len(argv) >= 3 and argv[1] == "census":
        triage = _load("triage")
        return census_cmd(argv[2], triage._config(argv[2]))
    if len(argv) >= 3 and argv[1] == "sweep":
        triage = _load("triage")
        return sweep_cmd(argv[2], triage._config(argv[2]), argv[3:])
    if len(argv) >= 3 and argv[1] == "propose":
        triage = _load("triage")
        return propose_cmd(argv[2], triage._config(argv[2]), argv[3:])
    if len(argv) >= 3 and argv[1] == "apply":
        triage = _load("triage")
        return apply_cmd(argv[2], triage._config(argv[2]), argv[3:])
    if len(argv) >= 3 and argv[1] == "blockers":
        triage = _load("triage")
        return blockers_cmd(argv[2], triage._config(argv[2]), run=run)
    print(_USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
