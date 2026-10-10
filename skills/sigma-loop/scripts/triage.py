#!/usr/bin/env python3
"""Read-only board survey (1/4) + the dependency-sequenced drain-plan compiler (2/4) + enact (3/4)
-- sigma-triage.

`survey`: nine fail-open buckets over the live GitHub board (or local .sdlc/goals/ in non-github
mode): inbox, active, parked/blocked, enqueued backlog, shadow backlog, epics/needs-decomposition,
hygiene gaps, dependency edges, context. NEVER writes -- no ledger append, no gh mutation, no
label/status change.

Buckets 2-8 derive from ONE retried `gh issue list` fetch (assignee-scoped exactly like
`next_pending`, same eventual-consistency retry posture) rather than one call per bucket -- body
text for up to 200 issues is already the dominant payload; nine separate fetches would be nine
times that cost for no extra signal. (One bounded exception, #706: on a board-enabled repo the
`enqueued` bucket also makes the Ready-lane existence check, at most two further `gh project`
calls -- see `_bucket_enqueued`'s own docstring for the cost accounting; it is not a second issue
fetch and every other repo shape pays nothing extra.)

`plan`: turns a confirmed pick (`--pick n[,n...]`, `--edge BLOCKED:BY`, `--defer n:reason`) into a
dependency-sequenced drain plan in capped waves, written as three artifacts under
`.sdlc/plans/triage/`. Pure compile -- ZERO network, zero ledger writes, under every DEFAULT input
(`--from-survey <file>` optionally supplies titles/priority/model/edges offline). The one
deliberate, opt-in exception (#713): `--resolve-missing N` makes up to N bounded `gh issue view`
calls for picks the survey snapshot doesn't cover (GitHub's search index is only eventually
consistent, so a just-labelled issue can be genuinely absent moments after filing) -- every input
that omits this flag keeps the original, unconditional zero-network guarantee; see `plan_cmd`'s
own docstring for exactly where the exception is scoped.

`enact`: compiles an ALREADY-COMPILED `plan` JSON artifact into the loop's own native primitives --
assignment, `sdlc:goal`/`priority:*`/`model:*` labels (add-only-what's-missing, idempotent), the
v1-lite unpark (drop `sdlc:parked`, re-add `sdlc:goal`), and a `**Blocked by:** #N` body marker per
dependency edge (via `sources.append_to_body`, deduped against what `backlog_check._BLOCK_RE`
already detects) -- so sequencing rides the loop's EXISTING park-on-blocked behavior with zero
changes to loop.py/next_pending. Dry-run by default; `--apply` executes the identical, already-
computed action list. Deferred/dropped issues are never read or written. Zero ledger writes.

    python3 triage.py survey <sdlc_dir> [--json]
    python3 triage.py plan <sdlc_dir> --pick n[,n...] [--edge BLOCKED:BY]... [--defer n:reason]...
        [--cap K] [--slug s] [--from-survey FILE] [--resolve-missing N]
    python3 triage.py enact <sdlc_dir> --plan <path-to-plan.json> [--apply]
"""
import json, os, pathlib, re, sys, tempfile, time, importlib.util

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


ledger = _load("ledger")
sources = _load("sources")
backlog_check = _load("backlog_check")     # _BLOCK_RE reused live -- see _scan_block_edges
watch = _load("watch")                     # read_inbox only, NEVER clear_inbox (read-only contract)
actionlog = _load("actionlog")
discovery = _load("discovery")
frontmatter = _load("frontmatter")
work = _load("work")                       # stem() only -- see _orphaned_worktrees
state = _load("state")                     # unsafe_goal_reason() only -- see _validate_slug (#669 F7:
                                            # stdlib-only, costs nothing comparable to loop.py's own
                                            # 5-sibling-module cascade that justifies copying _DEFAULT_CAP)

SCHEMA = "triage-survey/v1"

# --------------------------------------------------------------------------- config/mode helpers


def _config(sdlc_dir):
    try:
        return json.loads((pathlib.Path(sdlc_dir) / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _is_github(config):
    return ((config.get("discovery") or {}).get("source")) == "github"


def _gh_cfg(config):
    return ((config.get("discovery") or {}).get("github")) or {}


def _me(config, run=None):
    return ledger.actor(config, run)


# --------------------------------------------------------------------------- board fetch + retry


def _list_open_issues(gh_cfg, run):
    """(issues, degraded) for every open issue in scope -- ONE (possibly paginated) REST fetch,
    retried on the same two triggers `sources.GitHubSource.next_pending` established (F447): a
    transient exception, or an empty page (GitHub's search index is only eventually consistent).
    Retry constants AND the fetch ceiling are READ off `sources.GitHubSource` live, never copied
    (a value read fresh every call cannot drift from its source -- a prior drift class here,
    scrub.py/research_capture.py, the two gate scripts). The ceiling is `_BOARD_ITEM_LIMIT`
    (#706 §2c): #692 raised it for the board calls specifically because `gh`'s own `--limit` is a
    maximum, not a page size (a higher ceiling costs nothing on a small board) -- that insight
    applies equally to this broader, unfiltered fetch, so it reads the SAME constant rather than
    keeping the old `200` (which was never actually the ceiling #692 raised -- see
    `_PICKER_WINDOW` below for the ceiling that is still 200, and matters for a different reason).

    #1833: migrated off `gh issue list --state open --search sort:created-asc ...` onto the
    shared `sources.fetch_issues_rest` REST helper `_fetch_pending`/`list_needs_label`/
    `_sync_backlog` already use -- `--search` alone (no `--label` at all here; this fetch is
    UNSCOPED by label, unlike every other migrated call site) is itself always graphql-search-
    billed against the shared 5000/hour `graphql` resource, by construction (`--search` literally
    routes `gh` through the `search()` field) -- confirmed by #1829's own live research, which
    named this exact call site as sharing the defect but out of its own scope (off the
    `next`/`next-batch` pick path; this one only runs behind the human-invoked `/sigma-triage`).
    `state=open` and `sort=created&direction=asc` are REST parameters native to the endpoint, so
    no search-qualifier string is needed for either. `assignee` (which may be the kit's own
    documented `"@me"` convention) is resolved to a real login FRESH EACH ATTEMPT via
    `sources.resolve_assignee_login` -- mirroring `_fetch_pending`'s own per-attempt resolution
    exactly, for the exact same reason: REST's `assignee=` has no `"@me"` alias of its own (a hard
    422, confirmed live), so a resolution failure on one attempt must not permanently unscope
    every later retry.

    The old requested-fields comment (`title+body is the payload cost; createdAt is dropped, no
    bucket consumes it`) no longer applies: REST has no field-selection concept at all, so every
    field comes back on every fetch regardless -- at no extra request cost either way, since it
    was always exactly this one (possibly paginated) fetch. Ordering is NOT baked into this fetch
    (#706 docs sweep: the old comment here claimed a `number` sort "same as next_pending's own
    qualifiers" -- stopped being true the moment #698 made priority the default order) -- the
    `sort=created&direction=asc` pair only fixes which page is returned on a backlog deeper than
    the fetch ceiling; the enqueued bucket's own display order comes from `gh_source._pick_key`
    (or the pre-#706 number sort when `gh_source is None`), applied after this fetch, in
    `_bucket_enqueued`.

    Assignee-scoped exactly like `next_pending`. `degraded` carries `"gh_unavailable"` on exhausted
    retries/non-transient failure, `"gh_unparseable"` when `run()` succeeds but its stdout is not
    valid JSON (a gh upgrade-notice line, truncated output, or an HTML error page -- PR #677
    review finding 1: `json.loads` used to sit OUTSIDE this guard, so any of those crashed the
    whole survey with a raw traceback instead of degrading), or `"truncated"` on a saturated page
    (renamed from the old literal `"truncated_at_200"` -- #706 F3: that string would lie the
    moment the ceiling changes again, the exact drift class #692 was about) -- propagated onto
    every bucket derived from this fetch, see `survey()`."""
    limit = sources.GitHubSource._BOARD_ITEM_LIMIT
    repo = gh_cfg.get("repo") or ""
    retries = sources.GitHubSource._BACKLOG_READ_RETRIES
    base = sources.GitHubSource._BACKLOG_READ_RETRY_BASE
    for attempt in range(retries):
        last = attempt == retries - 1
        login = sources.resolve_assignee_login(run, gh_cfg.get("assignee") or "")
        try:
            issues = sources.fetch_issues_rest(run, repo, [], limit, assignee=login)
        except Exception as exc:
            if last or not sources.GitHubSource._is_transient(exc):
                reason = "gh_unparseable" if isinstance(exc, ValueError) else "gh_unavailable"
                return [], [reason]
            time.sleep(base * (2 ** attempt))
            continue
        if issues:
            return issues, (["truncated"] if len(issues) >= limit else [])
        if last:
            return [], []
        time.sleep(base * (2 ** attempt))
    return [], []   # unreachable -- loop above always returns by its last attempt


def _gh_source(config, run):
    """A `sources.GitHubSource` bound to survey's own `run`, or `None` on any construction failure
    -- never raises. `GitHubSource.__init__` is a pure attribute-resolution pass (#706 §2a,
    confirmed by execution in the plan review: zero gh calls under order=unset/created/bogus
    alike), so this realistically cannot fail, but nothing here should be the first place that
    assumption goes untested. `gh_source is None` is the fallback signal every consumer below
    (`_bucket_enqueued` and its four siblings) already treats as "use the pre-#706 default", so
    a survey against a config `GitHubSource` cannot parse still degrades gracefully rather than
    crashing survey() outright."""
    try:
        return sources.GitHubSource(config, run=run)
    except Exception:
        return None


def _label_names(issue):
    return {l.get("name") for l in (issue.get("labels") or []) if isinstance(l, dict)}


def _label_value(names, prefix):
    """The value portion of the first label starting with `prefix` (e.g. "priority:" -> "P0"), or
    None when no such label exists. `sorted()` first for the same reason `_epic_matched_label`
    already sorts (a bare `set`'s iteration order is not guaranteed across runs/interpreters) --
    shared by the four bucket-item builders below that surface a picked issue's own
    `priority:*`/`model:*` label (#669 F2: additive to G1's own bucket-item shape -- the hygiene
    bucket already parses these same prefixes to flag what is MISSING; this is the same read,
    surfacing what is PRESENT instead, on the buckets that can hold a pickable issue)."""
    return next((n[len(prefix):] for n in sorted(names) if n.startswith(prefix)), None)


# --------------------------------------------------------------------------- buckets 1-5


def _bucket_inbox(sdlc_dir, config, entries, me):
    """Unanswered hand-offs addressed to ME, always first. `ledger.unanswered()` (durable, unlike
    watch_classify's own cursor-gated "ever seen" set -- #421 -- which is why this is PRIMARY, not
    `watch.read_inbox` alone) filtered to `to == me AND actor != me` (a hand-off I filed myself,
    reachable when the CODEOWNERS-resolved owner of the target area is me, is not "a teammate
    blocked on us"). `watch.read_inbox` is read-only supplementary context ONLY -- `clear_inbox` is
    never called; that mutation belongs solely to `loop.py next`."""
    items = []
    # #1574: THE ADDRESSEE IS MATCHED THROUGH `ledger.address_key`, THE ACTOR IS NOT, and the
    # asymmetry is the point. `to` is written from two stores whose spellings differ (a bare login
    # from `actor()`/`owners`, a GitHub `@handle` from the branching-model registry), so comparing
    # it exactly hid every registry-addressed ask from the person it named. `actor` has exactly one
    # writer -- `ledger.actor`, which does not fold case -- so folding only this side keeps the
    # "a hand-off I filed myself is not a teammate blocked on us" test comparing like with like.
    # `me_key` falsy means the actor could not be named at all, which is addressed to nobody rather
    # than to everybody whose entry happens to carry no `to`.
    me_key = ledger.address_key(me)
    for h in ledger.unanswered(entries):
        if me_key and ledger.address_key(h.get("to")) == me_key and h.get("actor") != me:
            items.append({"from": h.get("actor"), "issue": h.get("issue"),
                          "priority": h.get("priority"), "why": h.get("why") or "",
                          "state": "open — no reply"})
    degraded = [] if ledger.enabled(config) else ["ledger_off"]
    return {"items": items, "count": len(items), "degraded": degraded,
            "queued_note": watch.read_inbox(sdlc_dir) or None}


def _bucket_active(issues, entries, gh_cfg, gh_source=None, ttl_seconds=None):
    """`sdlc:goal ∧ sdlc:in-progress` -- the INTERSECTION (status.py's own documented label model:
    parking drops sdlc:goal but LEAVES sdlc:in-progress, so bare in-progress would wrongly count a
    parked issue as active). Cross-referenced against live ledger claims (`ttl_seconds`, fold 10a:
    passed through to `open_claims_detailed` like every sibling consumer, `loop.py`'s `_lease()`
    included -- without it an ancient, never-terminated claim never ages out here either).

    `writer_alive` / `resumable` (fold 8): a claim's own recorded pid is the SHORT-LIVED `loop.py`
    CLI invocation that wrote it -- already exited by the time anyone reads it back, REGARDLESS of
    whether a long-running subagent is actively working the goal right now (loop.py's own
    `next_batch` docstring makes this explicit). That means `pid_alive()` on it reads False for
    essentially every genuinely in-progress goal BY DESIGN -- not a bug in the pid check, but a
    trap for a field NAMED "resumable" if read as "abandoned". `writer_alive` is the raw, literal
    fact; `resumable` mirrors the loop's OWN claim-lease semantics (`ledger.claim_belongs_to_me`'s
    same-actor branch: a dead/unresolvable writer pid is treated as mine to resume) -- crash-resume
    semantics, true for most in-flight goals too, and must never be read as "this was abandoned".

    `gh_source` (#706 §2d): supplies the configured `priority_label_prefix` for the displayed
    `priority` field, same fallback contract as every other bucket below -- `gh_source is None`
    (not supplied, or construction failed) reads the historical `"priority:"` literal."""
    goal_l = gh_cfg.get("goal_label", "sdlc:goal")
    prog_l = gh_cfg.get("in_progress_label", "sdlc:in-progress")
    prefix = gh_source.priority_prefix if gh_source is not None else "priority:"
    claims = ledger.open_claims_detailed(entries, ttl_seconds=ttl_seconds)
    items = []
    for i in issues:
        names = _label_names(i)
        if goal_l not in names or prog_l not in names:
            continue
        claim = claims.get(str(i["number"]))
        claim_actor, writer_alive, resumable = None, None, False
        if claim:
            claim_actor, writer = claim
            pid = ledger.writer_pid(writer)
            writer_alive = ledger.pid_alive(pid) if pid is not None else None
            resumable = pid is None or not writer_alive
        items.append({"number": i["number"], "title": i.get("title", ""),
                      "claim_actor": claim_actor, "writer_alive": writer_alive,
                      "resumable": resumable,
                      "priority": _label_value(names, prefix),
                      "model": _label_value(names, "model:")})
    return {"items": items, "count": len(items), "degraded": []}


def _bucket_parked(sdlc_dir, issues, entries, gh_cfg, gh_source=None):
    """`sdlc:parked` OR `sdlc:blocked` issues (which, per the label model, may ALSO have lost
    `sdlc:goal` already -- still surfaced here regardless) with a recovered reason where possible:
    the ledger's own `parked` ENTRIES-stream `why` field first (always available whenever
    `ledger.enabled`, unlike the EVENTS stream which also needs `journal.enabled`), the local
    action log's last `recorded`/`parked` entry as a fallback. `gh_source` (#706 §2d): same
    configured-prefix contract as `_bucket_active` above -- `gh_source is None` reads the
    historical `"priority:"`.

    #1358 (against #1350's own gap): `goal_blocked_label` (default `sdlc:blocked`) is #1350's
    DISTINCT, always-on, code-managed state `mark_blocked` writes when an in-progress goal
    discovers a genuine blocking dependency -- structurally the same "lost `sdlc:goal`, needs a
    human's eyes on `/sigma-triage`" shape `sdlc:parked` already has (the ledger `why` lookup above
    already works for it too: `loop.py`'s `_record(..., "parked", reason)` runs identically
    regardless of which GitHub label the source actually ends up writing). Before this, an issue
    carrying ONLY `sdlc:blocked` was invisible here -- this bucket's own title is already "Parked/
    blocked" (see `_BUCKET_TITLES`), which only became literally true once this line existed.
    `state` on each item lets a human (or `render`'s own formatter) tell the two apart at a glance."""
    parked_l = gh_cfg.get("parked_label", "sdlc:parked")
    blocked_l = gh_cfg.get("goal_blocked_label", "sdlc:blocked")
    # #1468: the third label that lands a goal in a human's queue. Its reason does NOT come from the
    # ledger recovery below and must not: the reason IS the label, so it renders on a stock adopter
    # (`ledger.enabled` ships false) exactly as it does on a wired one. Every other item here can
    # legitimately show `reason: ''` when neither store has it; this one never can.
    needs_l = gh_cfg.get("needs_label_label", "sdlc:needs-label")
    # #2263: the FOURTH such label -- same "the reason IS the label" property as `needs_l` above,
    # for the sibling overlay (no unit declared at all, rather than one whose label is missing).
    needs_u = gh_cfg.get("needs_unit_label", "sdlc:needs-unit")
    # #2427: the FIFTH -- #2363's own `sdlc:needs-triage` (every tier of automatic classification
    # tried and failed) was never added here, so this bucket -- /sigma-triage's ENTIRE job -- had no
    # awareness of the one label whose whole purpose is surfacing exactly this to a human. UNLIKE
    # `needs_l`/`needs_u` above, this overlay has NO self-healing sweep of its own
    # (`overlay_labels`'s own docstring), so the reason says so rather than promising a resume that
    # will never happen on its own.
    needs_t = gh_cfg.get("needs_triage_label", "sdlc:needs-triage")
    prefix = gh_source.priority_prefix if gh_source is not None else "priority:"
    ledger_why = {str(e.get("goal")): (e.get("why") or "")
                 for e in entries if e.get("kind") == "parked"}
    items = []
    for i in issues:
        names = _label_names(i)
        if (parked_l not in names and blocked_l not in names
                and needs_l not in names and needs_u not in names and needs_t not in names):
            continue
        n = str(i["number"])
        if needs_l in names:
            items.append({"number": i["number"],
                          "reason": "declares a feature: unit whose label does not exist yet — "
                                    "create it and Sigma resumes this goal itself",
                          "reason_source": "label",
                          "priority": _label_value(names, prefix),
                          "model": _label_value(names, "model:"),
                          "state": "needs-label"})
            continue
        if needs_u in names:
            items.append({"number": i["number"],
                          "reason": "declares no unit of work at all — declare one and Sigma "
                                    "resumes this goal itself",
                          "reason_source": "label",
                          "priority": _label_value(names, prefix),
                          "model": _label_value(names, "model:"),
                          "state": "needs-unit"})
            continue
        if needs_t in names:
            items.append({"number": i["number"],
                          "reason": "every tier of automatic unit classification was tried and "
                                    "failed — a human must declare its unit by hand and clear "
                                    "this label; nothing resumes it automatically",
                          "reason_source": "label",
                          "priority": _label_value(names, prefix),
                          "model": _label_value(names, "model:"),
                          "state": "needs-triage"})
            continue
        reason, source = ledger_why.get(n), ("ledger" if n in ledger_why else None)
        if not reason:
            for e in reversed(actionlog.read_goal(sdlc_dir, n)):
                if e.get("kind") == "recorded" and e.get("result") == "parked":
                    reason, source = (e.get("detail") or ""), "actionlog"
                    break
        items.append({"number": i["number"], "reason": reason or "", "reason_source": source,
                      "priority": _label_value(names, prefix),
                      "model": _label_value(names, "model:"),
                      "state": "blocked" if blocked_l in names else "parked"})
    return {"items": items, "count": len(items), "degraded": [],
            "orphaned_worktrees": _orphaned_worktrees(sdlc_dir, entries),
            "review_queue": _review_queue_names(sdlc_dir)}


def _review_queue_names(sdlc_dir):
    """`## <name>` section headings from `.sdlc/state/review-queue.md` -- a `LocalSource`-only
    artifact (`state.park`'s own review-queue append; `GitHubSource.park` never writes it), kept
    here in both modes anyway since a repo can have run in local mode previously, or the file can
    be hand-edited. Degrades to [] on anything unreadable, never raises."""
    p = pathlib.Path(sdlc_dir) / "state" / "review-queue.md"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return re.findall(r"^## (.+)$", text, re.MULTILINE)


def _orphaned_worktrees(sdlc_dir, entries):
    """`.sdlc/work/<stem>/` directories (`work.py start()`, independent of github/local mode --
    left behind whenever `work.enabled`) with no matching OPEN ledger claim -- a crashed or
    abandoned goal's checkout nobody is coming back to on their own.

    Fold 7: a worktree directory is always named `work.stem(goal)` (github: the bare issue number,
    already stem-shaped; local: the goal FILE's stem, e.g. "0002"), but a local-mode ledger claim's
    `goal` field is the FULL FILE PATH LocalSource passes (`.sdlc/goals/0002.md`) -- comparing the
    raw claim key against the bare directory name never matched in local mode, so every genuinely
    claimed local goal with a live worktree read as a false-positive orphan. Both sides go through
    `work.stem()` so github mode (already stem-shaped, a no-op there) and local mode agree."""
    wd = pathlib.Path(sdlc_dir) / "work"
    if not wd.is_dir():
        return []
    claimed = {work.stem(g) for g in ledger.open_claims(entries)}
    return sorted(p.name for p in wd.iterdir() if p.is_dir() and p.name not in claimed)


#: `_pending_by_label`'s own `--limit` (sources.py:482) is a bare string literal, not a class
#: attribute like `_BOARD_ITEM_LIMIT` -- nothing to read live, so this is a copied,
#: lockstep-pinned value (`test_picker_window_stays_lockstep_with_pending_by_labels_own_two_
#: hundred`, source-text pin, same mechanism the `_BLOCK_RE` guard already uses). #706 F1: this is
#: NOT the fetch ceiling `_list_open_issues` reads live off `_BOARD_ITEM_LIMIT` above -- it is the
#: real picker's OWN, still-200, page size, and the reason `_bucket_enqueued` below needs a
#: separate honesty marker even after the fetch ceiling was raised.
_PICKER_WINDOW = 200


def _bucket_enqueued(issues, gh_cfg, gh_source=None):
    """Open `sdlc:goal` issues, in the order `GitHubSource.next_pending` would actually serve them
    -- `gh_source._pick_key` sorts directly (the bound method, zero copy of the ranking/tie-break
    logic: order, priority prefix, and the priority-vs-created branch all live in exactly one
    place, sources.py's own). `gh_source is None` (construction failed, or no gh_source supplied at
    all -- most direct unit tests) falls back to `(0, number)`, byte-identical to the pre-#706
    plain-ascending issue-number sort. Also excludes in-progress issues, which `next_pending`'s
    OWN label filter technically would not (only `sdlc:parked` excludes there -- an abandoned
    in-progress goal is meant to be re-pickable): a genuinely active issue is already shown under
    `active`, and listing it again here would read as "not yet started" for a goal that plainly is.
    A refinement for this human-facing survey, not a literal byte-for-byte mirror of the raw
    picker's own filter.

    Two `degraded` markers this bucket alone can raise (neither is a nicety -- see #706 F1/F6):

    - `"exceeds_picker_window"`: the real picker's own fetch (`_pending_by_label`) is still capped
      at `_PICKER_WINDOW` (200) even though `_list_open_issues` above now reads a much higher
      ceiling live -- raising that ceiling was correct for every OTHER bucket (a broader, unfiltered
      page), but it makes THIS bucket's ordering claim less true, not more: sorting a 5000-issue
      page by priority ranks a P0 sitting at position 3000 first, while the live picker structurally
      cannot see past its own 200-issue window. Past that count, the order shown is only what the
      picker would serve for the first 200 it can reach.
    - `"board_ready_lane_authoritative"`: `GitHubSource.next_pending` calls `_board_queue()` FIRST
      and only falls through to the label-sorted path `_pick_key` implements when that returns
      `None` -- so on a Ready-lane-migrated board, `_pick_key`'s order is not merely an incomplete
      proxy for the real queue, it is order derived from an entirely different source than the one
      the picker will actually use. The marker is therefore structurally mandatory whenever it
      fires, not stylistic. Learned via `gh_source._ready_lane()` alone (the cheap, ≤2-call
      existence check) -- `_board_queue()` itself (paginated, measured 2.45s/268-item board in its
      own docstring) is deliberately never called here; this module's own docstring already commits
      buckets 2-8 to ONE shared fetch, and paying board-read cost on every survey would break that
      for the one bucket that happens to sit on a migrated repo.

    `_ready_lane()`'s failure path also has a user-visible side effect this bucket surfaces rather
    than leaving silent (F5): on a board-enabled repo whose runner lacks the `project` scope, it
    prints a warning to stderr once per process (`GitHubSource._note_scope`, which this module
    cannot suppress without editing sources.py) -- `"project_scope_missing"` mirrors that fact into
    `degraded` too, so a `--json` consumer of survey() learns the board read is blind on this run,
    not only a human watching stderr."""
    goal_l = gh_cfg.get("goal_label", "sdlc:goal")
    prog_l = gh_cfg.get("in_progress_label", "sdlc:in-progress")
    parked_l = gh_cfg.get("parked_label", "sdlc:parked")
    # #1393: `goal_blocked_label` joins the exclusion. It was previously unnecessary here for a
    # structural reason that no longer holds -- `mark_blocked` used to REMOVE `goal_label`, so a
    # blocked goal failed the first test and could never reach this filter at all. Now that
    # `sdlc:blocked` is an overlay riding alongside membership, a blocked goal DOES reach it, and
    # without this line every blocked goal on the board would be reported as ready to pick and a
    # compiled drain plan would happily schedule it. Matches `sources._fetch_pending` and
    # `_card_is_eligible`, which have both always excluded it.
    blocked_l = gh_cfg.get("goal_blocked_label", "sdlc:blocked")
    # #1393: the survey's "ready to pick" filter must be the PICKER's rule, not an approximation of
    # it -- `gh_source.not_eligible_labels()` is the one statement of it. Hand-written copies had
    # drifted in five places; this one was missing `proposed_label`, so a survey listed issues
    # awaiting human approval as ready and a compiled drain plan scheduled them.
    # #1468: `needs_label_label` joins that fallback for the SAME reason `proposed_label` did. The
    # comment above is a warning this branch had already earned once, and adding an overlay to
    # `not_eligible_labels()` alone does not reach it: production passes `gh_source`, so the drift is
    # invisible until `_gh_source` returns None -- and then a HELD goal is reported ready and a
    # compiled drain plan schedules it. Measured before this line: production path count=0, fallback
    # path count=1, on the same held issue.
    # #2263: `needs_unit_label` joins the fallback tuple for the IDENTICAL reason -- it is an
    # overlay `not_eligible_labels()` now includes, so it is already covered when `gh_source` is
    # available, but this hand-written branch is a SEPARATE copy of that rule and would otherwise
    # silently repeat the exact drift #1468's comment above already warns about.
    # #2427: `needs_triage_label` joins it too, for the SAME reason and by the SAME pattern -- #2363
    # added it to `overlay_labels()`/`not_eligible_labels()` but this hand-written fallback copy was
    # never updated, so a `sdlc:needs-triage` issue read as "ready to pick" whenever `gh_source` was
    # unavailable, exactly the drift shape every comment on this line already warns about.
    not_eligible = (tuple(gh_source.not_eligible_labels()) if gh_source is not None
                    else (prog_l, parked_l, blocked_l,
                          gh_cfg.get("needs_label_label") or "sdlc:needs-label",
                          gh_cfg.get("needs_unit_label") or "sdlc:needs-unit",
                          gh_cfg.get("needs_triage_label") or "sdlc:needs-triage",
                          # legacy alias, read-only: a leftover is never ready to pick
                          gh_cfg.get("proposed_label") or "sdlc:needs-confirmation"))
    prefix = gh_source.priority_prefix if gh_source is not None else "priority:"
    eligible = []
    for i in issues:
        names = _label_names(i)
        if goal_l not in names or any(l in names for l in not_eligible):
            continue
        eligible.append((i, names))
    if gh_source is not None:
        eligible.sort(key=lambda pair: gh_source._pick_key(pair[0]))
    else:
        eligible.sort(key=lambda pair: (0, pair[0]["number"]))
    items = [{"number": i["number"], "title": i.get("title", ""),
             "priority": _label_value(names, prefix),
             "model": _label_value(names, "model:")} for i, names in eligible]
    degraded = []
    if len(items) > _PICKER_WINDOW:
        degraded.append("exceeds_picker_window")
    if gh_source is not None:
        if gh_source._ready_lane():
            degraded.append("board_ready_lane_authoritative")
        if gh_source._scope_warned:
            degraded.append("project_scope_missing")
    return {"items": items, "count": len(items), "degraded": degraded}


#: Real, shipped anchor: the label decompose_check's file-mode stamps on a filed "Decompose #N"
#: meta-issue (loop.py / decompose_goal.py). sigma itself has no OTHER fixed epic/needs-
#: decomposition label -- the substring set below is a documented, adopter-convention heuristic,
#: easy to extend if a repo's own vocabulary differs.
_EPIC_LABEL_EXACT = {"sdlc:decompose"}
_EPIC_LABEL_SUBSTR = ("epic", "needs-decomposition")

#: Read as a DISPLAY value only -- `_label_value(names, _MODEL_PREFIX)` fills the `model` column in
#: the bucket items and the plan, and `enact` re-attaches whatever a human put in the plan. It is
#: NOT part of the hygiene check any more (#1602): a `model:*` label is an adopter's own annotation,
#: whose values this kit neither defines nor reads. The tier a goal runs at comes from `predict.py`
#: over the goal's TEXT, gated on `model_selection: "auto"` -- no label value reaches it -- so a
#: goal without one is exactly as enqueue-ready as a goal with one, and demanding it only pushed
#: adopters into inventing incompatible vocabularies (one board grew `daily`/`sonnet`/`haiku`).
_MODEL_PREFIX = "model:"

#: Bucket 8's noise bound (amendment 4): the explicit scan runs over up to 200 full issue bodies,
#: ~200x the single-goal corpus `_BLOCK_RE` was designed for -- ordinary English ("needs #12
#: reviewed", "after #40 lands") WILL false-positive at that scale. Capped per issue, deduped by
#: (blocked, by) pair; this bucket is advisory triage input for a human, never a park/gate signal.
_EDGE_CAP_PER_ISSUE = 5


def _epic_matched_label(issue):
    """The epic-classifying label an issue carries, or None. Shared by `_bucket_epics` AND
    `_bucket_shadow` (PR-review finding 4: an epic has no `sdlc:goal` label by definition, so
    without this shared check it double-counted into shadow too) -- one place, so the two can
    never drift on what "is an epic" means. `sorted()` (fold 9): `_label_names()` returns a bare
    `set`, whose iteration order is not guaranteed across runs/interpreters (a 12-interpreter probe
    varied which of several matching labels won) -- sorted first, so the alphabetically-first match
    is deterministic, always."""
    return next((n for n in sorted(_label_names(issue))
                if n in _EPIC_LABEL_EXACT or any(s in n.lower() for s in _EPIC_LABEL_SUBSTR)), None)


def _bucket_epics(issues):
    """Issues never enqueue-eligible directly -- too big, needs splitting first."""
    items = []
    for i in issues:
        matched = _epic_matched_label(i)
        if matched:
            items.append({"number": i["number"], "title": i.get("title", ""), "matched_label": matched})
    return {"items": items, "count": len(items), "degraded": []}


#: #2296 (BR-19/BR-20/BR-25): the "Priority criteria" rubric (`skills/sigma-triage/SKILL.md`,
#: "Priority criteria" section) was prose-only before this -- read by a human at the question
#: round but applied by nothing. These are the rubric's OWN test-sentence vocabulary, not an
#: invented classifier: P0's anchor is "broken, unsafe, or defeated ... something unsafe ... the
#: mechanism doing the wrong thing, silently"; P1's is "degrade something most runs hit ... leave
#: real output wrong ... blocked outright"; P2's is "real, wanted, planned work ... nothing is
#: broken or blocked by leaving it for now"; P3's is "narrow-blast-radius bugs, tech-debt,
#: follow-up polish"; P4's is "speculative, cosmetic, or pure hygiene". Ordered P0 -> P4 (severest
#: first), the same "ordered high -> low, first/strongest tier wins" shape `sigma-model/scripts/
#: predict.py`'s own `_PATTERNS` already uses for the closest real precedent this codebase has for
#: "classify free text into a discrete tier mechanically" -- `assign.py`/`brainstorm.py`/
#: `dedup.py` (sigma-scope's own scripts) were read in full for this port and carry NO such scoring
#: function; sigma-scope's actual mechanism is the CALLING AGENT applying the rubric prose by
#: judgment at its own step 6, with only a flat P1 default (`compile_plan.DEFAULT_PRIORITY`) coded
#: anywhere. `_priority_hint` below ports predict.py's SHAPE (ordered pattern scan) while adding
#: the one thing neither existing precedent has: a genuine "ambiguous between two ADJACENT tiers"
#: signal, matching sigma-scope's own named example ("soon" -> P1 or P2, SKILL.md's clarifying-
#: questions judgment section) rather than predict.py's always-pick-a-winner resolution.
_PRIORITY_PATTERNS = (
    ("P0", re.compile(r"\b(unsafe|vulnerab\w*|exploit\w*|security hole|bypass(?:es|ed|ing)?\s+"
                      r"(?:the\s+)?review|silently (?:doing|does) the wrong|path traversal|"
                      r"secret(?:s)? leak\w*|deadlock\w*|data corrupt\w*|double.?pick|"
                      r"race condition)\b", re.IGNORECASE)),
    ("P1", re.compile(r"\b(every run|degrades?|blocked outright|no pr (?:can|could) merge|"
                      r"stale code|most runs|silently (?:ignored|executing stale))\b",
                      re.IGNORECASE)),
    ("P2", re.compile(r"\b(planned|roadmap|no clock running|real,? wanted|"
                      r"nothing is broken)\b", re.IGNORECASE)),
    ("P3", re.compile(r"\b(tech.?debt|follow.?up polish|narrow.blast.radius|edge case|cleanup|"
                      r"crash on (?:an )?unusual)\b", re.IGNORECASE)),
    ("P4", re.compile(r"\b(cosmetic|speculative|pure hygiene|stray file|"
                      r"zero functional impact|nice.to.have|someday)\b", re.IGNORECASE)),
)

#: `predict.py`'s `_DEFAULT` counterpart -- but the value ported is `/sigma-scope`'s own
#: zero-signal default, verbatim (its SKILL.md, step 6: "a real `P0`-`P4` priority (default `P1`
#: if nothing about the work argues for a different tier)"; `compile_plan.DEFAULT_PRIORITY` is the
#: same "P1" in code). Applied SILENTLY on zero signal, exactly as sigma-scope already applies it --
#: absence of evidence is not the "genuinely ambiguous between two adjacent tiers" case this module
#: still asks about.
_DEFAULT_PRIORITY_HINT = "P1"

_PRIORITY_TIER_ORDER = tuple(tag for tag, _ in _PRIORITY_PATTERNS)


def _priority_hint(title, body):
    """Mechanically apply the P0-P4 rubric to one issue's own title+body text. Returns
    `{"tier": "P<n>", "ambiguous": False}` when exactly one tier's vocabulary matched, or when
    NOTHING matched (falls back to `_DEFAULT_PRIORITY_HINT`, applied silently -- see that
    constant's own docstring). Returns `{"tier": None, "ambiguous": [<lower>, <higher>]}` only
    when two ADJACENT tiers both matched and neither dominates -- the one case this function
    asks about, the same shape sigma-scope's own SKILL.md names as a real question ("soon" -> P1
    or P2).

    A NON-adjacent multi-match (e.g. P0 and P3 both hit) is NOT this kind of ambiguity: the
    lower-numbered (more severe) tier always wins, mirroring `predict.py`'s own "conflicts resolve
    UPWARD to the more capable tier" rule -- escalating silently is the safe direction; treating a
    genuine P0 signal as a coin-flip with an unrelated P3 mention is not the ambiguity this
    function exists to surface."""
    haystack = f"{title or ''}\n{body or ''}"
    hits = [tag for tag, pattern in _PRIORITY_PATTERNS if pattern.search(haystack)]
    if not hits:
        return {"tier": _DEFAULT_PRIORITY_HINT, "ambiguous": False}
    if len(hits) == 1:
        return {"tier": hits[0], "ambiguous": False}
    ranked = sorted(hits, key=_PRIORITY_TIER_ORDER.index)
    strongest, next_strongest = ranked[0], ranked[1]
    if _PRIORITY_TIER_ORDER.index(next_strongest) - _PRIORITY_TIER_ORDER.index(strongest) == 1:
        return {"tier": None, "ambiguous": [strongest, next_strongest]}
    return {"tier": strongest, "ambiguous": False}


def _bucket_hygiene(issues, gh_cfg, gh_source=None):
    """In-scope issues (open, goal-labelled, not parked -- active ∪ enqueued) missing the
    CONFIGURED priority-prefixed label (#706 §2d: `gh_source.priority_prefix`, or the historical
    `"priority:"` when `gh_source is None`) -- checked against the same prefix every other bucket
    now displays, so a repo that configured `priority_label_prefix` doesn't get flagged as "missing
    priority" for correctly-labelled issues, or silently pass ones that only carry the old default
    spelling.

    #1602: a missing `model:*` USED to be reported here too, and that was the bug. A hygiene item is
    a claim that an issue is not enqueue-ready, and priority earns it -- this module reads priority
    and orders waves by it. Nothing reads a `model:*` value: the enqueue path, the pick path and the
    tier prediction (`predict.py`, over the goal's TEXT, gated on `model_selection`) all behave
    identically with or without one, and `_ensure_labels` seeds no `model:*` label for an adopter to
    use. So the check demanded a label the kit defines nowhere, reads nowhere, and creates nowhere,
    and every adopter met it as "add a `model:*`" with no answer to "which values are legal?" -- one
    audited board had invented `daily`, `sonnet` and `haiku`; this repo's had five values, agreeing
    with each other and with `predict.py`'s `haiku|sonnet|opus|fable` in neither direction. Carrying
    one is still perfectly fine and still displayed; it is only no longer demanded.

    #817: ALSO flags the opposite kind of gap -- an issue carrying BOTH `goal_label` AND
    `parked_label` at once. `sources.py::_offboard` (park/fail) issues three independently
    try/excepted label calls (remove goal, remove in-progress, add parked); a transient failure on
    the first while the third still succeeds leaves exactly this contradiction, invisible until
    now because the missing-model/priority check above explicitly EXCLUDES parked issues from its
    own scope (`parked_l in names: continue`) -- a goal+parked issue was never even looked at.
    Detection only, deliberately (matches this whole module's own "NEVER writes" contract, see the
    file's module docstring) -- repairing it is `triage.py enact`'s job, via its existing
    `remove-label` action, once a plan picks this finding up.

    #2296: a "missing priority" item ALSO carries `priority_hint` -- `_priority_hint`'s mechanical
    read of the rubric against this issue's own title+body, so the question round no longer asks
    unconditionally: a non-ambiguous hint is the answer (apply it, and say so, matching BR-20's
    "let the drafted plan itself carry the decision for the user to see"), and only a genuinely
    ambiguous hint (two adjacent tiers, neither dominant) is still worth asking about. Still
    detection only -- this function computes the hint, it never writes the label; see
    `skills/sigma-triage/SKILL.md`'s "Question round" step for who applies it and how."""
    goal_l = gh_cfg.get("goal_label", "sdlc:goal")
    parked_l = gh_cfg.get("parked_label", "sdlc:parked")
    priority_prefix = gh_source.priority_prefix if gh_source is not None else "priority:"
    checks = (("priority", priority_prefix),)
    items = []
    for i in issues:
        names = _label_names(i)
        if goal_l in names and parked_l in names:
            items.append({"number": i["number"], "title": i.get("title", ""),
                         "contradiction": "goal+parked"})
            continue
        if goal_l not in names or parked_l in names:
            continue
        missing = [tag for tag, p in checks if not any(n.startswith(p) for n in names)]
        if missing:
            item = {"number": i["number"], "title": i.get("title", ""), "missing": missing}
            if "priority" in missing:
                item["priority_hint"] = _priority_hint(i.get("title", ""), i.get("body", ""))
            items.append(item)
    return {"items": items, "count": len(items), "degraded": []}


def _scan_block_edges(docs):
    """[{blocked, by, source:"explicit"}] over `docs` (each `{"ref","title","body"}` -- a bare
    issue number in github mode, a file-path string in local mode). Reads `backlog_check._BLOCK_RE`
    LIVE off the module attribute at call time, never captured into a local/copied pattern -- the
    flagship reuse contract (a re-typed identical pattern would be the SAME cached `re.compile`
    object, so an identity check alone cannot tell reuse from a copy; a monkeypatch of the real
    regex proves live reading instead). Mirrors `_explicit_blockers`'s precision rule in full: a
    match counts only for a real OPEN doc that isn't the referencing doc's own number. Bounded per
    `_EDGE_CAP_PER_ISSUE` and deduped by (blocked, by) pair.

    NOTE: local mode never calls this scanner. In local mode, docs[].ref is a file-path string
    (e.g. '0042-x.md'), but _BLOCK_RE group(2) captures issue numbers (e.g. '42'). The
    `n not in open_refs` membership test never matches, so no explicit edges are produced
    from local-mode bodies — only from the github-mode doc set."""
    open_refs = {d["ref"] for d in docs}
    items, seen = [], set()
    for d in docs:
        haystack = (d.get("title") or "") + "\n" + (d.get("body") or "")
        found = 0
        for m in backlog_check._BLOCK_RE.finditer(haystack):
            if found >= _EDGE_CAP_PER_ISSUE:
                break
            n = m.group(2)
            if n == d["ref"] or n not in open_refs:
                continue
            pair = (d["ref"], n)
            if pair in seen:
                continue
            seen.add(pair); found += 1
            items.append({"blocked": d["ref"], "by": n, "source": "explicit"})
    return items


def _explicit_edges(issues):
    return _scan_block_edges([{"ref": str(i["number"]), "title": i.get("title") or "",
                               "body": i.get("body") or ""} for i in issues])


def _ledger_edges(entries):
    """One edge per outstanding (unanswered-or-not-yet-terminal) hand-off: the filer is blocked on
    the target it opened. Deduped the same way the explicit scan is.

    PR-review finding 2: `ledger.handoff_key()` falls back to the goal itself when `issue` is
    absent -- the default for any source without `create_dependency` (every local-mode hand-off)
    or a failed create (`handoff.py`'s `create_tracked_issue` writes `issue=None` in exactly that
    case) -- which would otherwise emit a meaningless self-edge `{'blocked': N, 'by': N}` on every
    such hand-off. Skipped explicitly rather than relying on the dedupe set (a self-edge is not a
    duplicate of anything else -- it is wrong on its own)."""
    items, seen = [], set()
    for h in ledger.outstanding(entries):
        blocked, by = str(h.get("goal")), ledger.handoff_key(h)
        if blocked == by:
            continue
        pair = (blocked, by)
        if pair in seen:
            continue
        seen.add(pair)
        items.append({"blocked": blocked, "by": by, "source": "ledger"})
    return items


def _bucket_edges(issues, entries):
    items = _explicit_edges(issues) + _ledger_edges(entries)
    return {"items": items, "count": len(items), "degraded": []}


def _bucket_shadow(issues, gh_cfg, me, gh_source=None):
    """Open, assigned to ME, carrying NO `sdlc:goal` label -- invisible to `next_pending`'s own
    `--label` filter (the #623 problem class this bucket exists to surface, and ONLY that class).

    PR-review finding 4: "no goal label" alone is also the guaranteed POST-PARK steady state (the
    label model: parking drops `sdlc:goal`, keeps `sdlc:in-progress`) and true of every EPIC (an
    epic is deliberately never goal-labelled) -- without excluding both, a parked or epic issue
    assigned to me landed in `shadow` AS WELL AS its own bucket (`parked`/`epics`), breaking the
    mutual exclusivity every other bucket boundary in this survey keeps.

    `gh_source` (#706 §2d): same configured-prefix contract as `_bucket_active`/`_bucket_parked`
    above -- `gh_source is None` reads the historical `"priority:"`."""
    goal_l = gh_cfg.get("goal_label", "sdlc:goal")
    parked_l = gh_cfg.get("parked_label", "sdlc:parked")
    prefix = gh_source.priority_prefix if gh_source is not None else "priority:"
    items = []
    for i in issues:
        names = _label_names(i)
        if goal_l in names or parked_l in names or _epic_matched_label(i):
            continue
        # #1442: `sources._logins` is the ONE normaliser for assignee data (#1437), so this bucket
        # cannot drift from what the picker actually does. It also casefolds -- GitHub logins are
        # case-insensitive for identity while the API returns canonical casing, so the old exact
        # comparison silently dropped an issue from this bucket whenever `me` and the assignee
        # differed only in case. `me` is casefolded HERE and not at its source, because its other
        # use (`_bucket_inbox`) compares ledger actor strings, which is a different namespace.
        if me and me.casefold() in sources._logins(i.get("assignees")):
            items.append({"number": i["number"], "title": i.get("title", ""),
                          "priority": _label_value(names, prefix),
                          "model": _label_value(names, "model:")})
    degraded = [] if gh_cfg.get("assignee") else ["no_assignee_configured"]
    return {"items": items, "count": len(items), "degraded": degraded}


# --------------------------------------------------------------------------- bucket 9


#: matches loop.py's own goals_parallel() fallback -- the "no parallel.goals block at all" default.
_DEFAULT_GOALS_MAX_CONCURRENT = 3


def _gate_enabled(gate):
    """`gates.<name>.enabled` (hard_plan_gate / stop_gate) read generously -- the F17/#342
    direction (#416), intentionally duplicated here rather than imported: `hooks/plan_gate.sh`,
    `hooks/completion_gate.sh`, and `skills/sigma-doctor/scripts/doctor.py` each already carry their
    OWN copy of this exact check (a hook cannot reliably source another file across the plugin
    layout, and doctor.py is a standalone diagnostic with no cross-skill import by design) -- this
    bucket's own read is a FIFTH surface describing the SAME two flags, discovered fresh (not in
    #416's original location list, filed before this bucket existed). A strict `is True` here would
    let this survey disagree with the hooks it describes: `gates.hard_plan_gate: false` while the
    real hook is genuinely ON (`enabled: 1`/`"true"`) is exactly the doctor.py-vs-hooks silent
    disagreement `doctor._gate_enabled`'s own docstring warns against, just relocated. Deliberately
    NOT applied to `parallel_goals.enabled` / `backlog_check_enabled` / `goal_decompose_enabled`
    below -- those are FEATURE flags, not hard DENY gates, and strict `is True` is the CORRECT
    direction for them (mirrors `ledger.enabled()`'s own documented intentional strictness: a
    stray truthy value must not silently switch a team/automation surface ON).

    TOTAL SINCE #2116 -- and here the old shape did not merely disagree, it RAISED. `gates.get(name)
    or {}` at the call site hands this function whatever sits at the block path, and
    `ledger.LOCKABLE_KEYS` names that path, so `{"gates": {"hard_plan_gate": true}}` is a legal Org
    policy that arrives as the bare `True`. `True.get("enabled")` is an `AttributeError`, which
    `_run`'s own `_safe` wrapper then swallows by replacing the WHOLE `context` bucket with an error
    stub -- taking `north_star`, `parallel_goals`, `ledger_enabled` and `stop_gate` down with a
    survey line about a flag. Degraded, not off, and for a config `work.py` reads as ON."""
    if not isinstance(gate, dict):
        gate = {"enabled": gate}
    value = gate.get("enabled")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "0", "no", "off")
    return bool(value)


def _bucket_context(sdlc_dir, config):
    """The knobs that shape a drain plan: north-star present?, goal-parallelism cap,
    backlog_check/goal_decompose/ledger on-off, the two plan/stop gates. No config key this bucket
    reads is new -- every one already exists in config.json.tmpl."""
    pg = (config.get("parallel") or {}).get("goals") or {}
    # `or {}` alone is not total (#2116): a scalar `{"gates": true}` sails past it and the `.get`
    # below raises -- which `_safe` then turns into the loss of this whole bucket, not just a wrong
    # gate line.
    gates = config.get("gates")
    gates = gates if isinstance(gates, dict) else {}
    return {
        "north_star": (pathlib.Path(sdlc_dir) / "context" / "north-star.md").exists(),
        "parallel_goals": {"enabled": pg.get("enabled") is True,
                           "max_concurrent": pg.get("max_concurrent", _DEFAULT_GOALS_MAX_CONCURRENT)},
        "backlog_check_enabled": (config.get("backlog_check") or {}).get("enabled") is True,
        "goal_decompose_enabled": (config.get("goal_decompose") or {}).get("enabled") is True,
        "ledger_enabled": ledger.enabled(config),
        # RAW values, not `or {}` (#2116): the block path is what an Org locks, so a scalar there is
        # a legal policy `_gate_enabled` now reads for its plain intent instead of raising.
        "gates": {"hard_plan_gate": _gate_enabled(gates.get("hard_plan_gate")),
                  "stop_gate": _gate_enabled(gates.get("stop_gate"))},
        "degraded": [],
    }


# --------------------------------------------------------------------------- orchestrator

#: The 7 buckets sourced from the ONE `_list_open_issues` fetch -- a saturated/unavailable read
#: propagates its own `degraded` reason onto every one of them (amendment 2), and in local mode
#: these 7 are exactly what `_local_bucket` (lever 1) dispatches for.
_FETCH_DERIVED_BUCKETS = ("active", "parked", "enqueued", "shadow", "epics", "hygiene", "edges")


def survey(sdlc_dir, config=None, run=None, now=None):
    """The whole read-only pass: nine buckets, each fail-open independently -- a bucket whose own
    function raises reports `degraded: ["error: ..."]` and an empty result, never taking the other
    eight down with it (this is what makes `survey()` safe to run against a genuinely broken repo,
    the same posture `status.py.summary` already established for its own smaller surface). NEVER
    writes: no `ledger.append`/`safe_append`, no `gh` mutation, no `watch.clear_inbox`."""
    config = config if config is not None else _config(sdlc_dir)
    run_ = run or sources._run_gh
    entries = ledger.read_all(sdlc_dir)
    me = _me(config, run_)
    mode = "github" if _is_github(config) else "local"
    buckets = {}

    def _safe(name, fn):
        try:
            buckets[name] = fn()
        except Exception as exc:
            buckets[name] = {"items": [], "count": 0, "degraded": [f"error: {exc}"]}

    _safe("inbox", lambda: _bucket_inbox(sdlc_dir, config, entries, me))

    if mode == "github":
        gh_cfg = _gh_cfg(config)
        # Constructed once (#706 §2a) whether or not the fetch below succeeds -- construction is
        # pure attribute resolution, independent of the fetch outcome, so _bucket_enqueued can still
        # report the correct (empty) order and, if reachable, the Ready-lane marker, even when the
        # issue list itself failed. `gh_source is None` on any construction failure; every bucket
        # below already treats that as "fall back to the pre-#706 default".
        gh_source = _gh_source(config, run_)
        # Belt-and-braces beyond _list_open_issues' OWN internal fail-open (finding 1): even an
        # UNEXPECTED failure mode there must degrade the seven fetch-derived buckets, never crash
        # survey() itself -- the same "fail-open per bucket" contract every _safe() call already
        # keeps for everything else.
        try:
            issues, fetch_degraded = _list_open_issues(gh_cfg, run_)
        except Exception as exc:
            issues, fetch_degraded = [], [f"error: {exc}"]
        _safe("active", lambda: _bucket_active(issues, entries, gh_cfg, gh_source,
                                               ttl_seconds=ledger.lease_ttl_seconds(config)))
        _safe("parked", lambda: _bucket_parked(sdlc_dir, issues, entries, gh_cfg, gh_source))
        _safe("enqueued", lambda: _bucket_enqueued(issues, gh_cfg, gh_source))
        _safe("shadow", lambda: _bucket_shadow(issues, gh_cfg, me, gh_source))
        _safe("epics", lambda: _bucket_epics(issues))
        _safe("hygiene", lambda: _bucket_hygiene(issues, gh_cfg, gh_source))
        _safe("edges", lambda: _bucket_edges(issues, entries))
        if fetch_degraded:
            for name in _FETCH_DERIVED_BUCKETS:
                if name in buckets:
                    buckets[name]["degraded"] = list(buckets[name].get("degraded") or []) + fetch_degraded
    else:
        order = sources._order(config)
        aliases = sources._priority_aliases(config)
        docs = _local_goal_docs(sdlc_dir)
        for name in _FETCH_DERIVED_BUCKETS:
            _safe(name, lambda name=name: _local_bucket(name, docs, entries, sdlc_dir, order, aliases))

    _safe("context", lambda: _bucket_context(sdlc_dir, config))

    return {"schema": SCHEMA, "generated_at": _stamp(now), "mode": mode, "buckets": buckets}


def _local_goal_docs(sdlc_dir):
    """The local-files corpus: `.sdlc/goals/*.md`, same reader `discovery.py`/`state.py` already
    use (`frontmatter.parse` for status/title, `frontmatter.strip` for the body). `priority` (#706
    §2a) is the goal's own `priority:` frontmatter key, read the same way `discovery.priority_of`
    does -- needed so `_local_bucket`'s "enqueued" case can mirror `discovery.next_pending`'s own
    `(priority_rank(...), path)` ordering."""
    docs = []
    for p in sorted((pathlib.Path(sdlc_dir) / "goals").glob("*.md")):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        meta = frontmatter.parse(text)
        if not meta:
            continue                                     # a README etc. -- not a goal
        docs.append({"ref": str(p), "title": meta.get("title") or p.stem,
                     "status": meta.get("status") or "", "body": frontmatter.strip(text),
                     "priority": meta.get("priority")})
    return docs


def _local_bucket(kind, docs, entries, sdlc_dir, order=discovery.DEFAULT_ORDER, aliases=None):
    """Local-mode counterpart to the 7 github-fetch-derived buckets -- ONE dispatcher (density
    lever 1) instead of 7 near-duplicate functions. shadow/hygiene/epics have no local equivalent
    (no assignee/label-tier/decompose-meta-issue-label vocabulary exists for goal files) and report
    `not_applicable_local_mode` -- as does `edges` (PR-review finding 3): `_local_goal_docs`'
    `ref` is a full FILE PATH, while `_BLOCK_RE` only ever captures a bare digit string after `#`,
    so `n not in open_refs` can never pass -- the bucket was structurally incapable of finding an
    edge while still reporting `degraded: []` (falsely healthy). Honest rather than clever: no
    numeric local-goal-id convention is established anywhere else in this codebase to build a real
    comparable ref on, so this reports absence rather than a heuristic that could itself mislead.

    `aliases` (#854 follow-up, independent-review finding): passed straight through to
    `discovery.priority_rank` for the enqueued case's sort key. Without it, a goal filed
    `priority: Critical` under a configured `priority_aliases` map sorted UNPRIORITISED here while
    `LocalSource.next_pending` (already alias-aware) would have picked it first -- survey and the
    live picker disagreeing about the same goal's urgency, the same two-places-drift shape #706's
    own docstring already names as the thing a lockstep test guards against.

    `order` (#706 §2a): the enqueued case's counterpart to `_bucket_enqueued`'s `gh_source._pick_key`
    -- `discovery.next_pending` inlines its sort into a single `min()` pass rather than exposing a
    separable key function the way `sources.GitHubSource._pick_key` does for github mode, so there
    is no bound method to reuse here; the `(priority_rank, ref)` tuple below reproduces it directly,
    a copy small enough that a lockstep test is the proportionate fix (see the module's own
    lockstep-pin table), not a `discovery.py` refactor #706 has no reason to make.

    `"priority"` on every emitted item (#839, independent-review finding on #714/PR #835): active/
    parked/enqueued items used to carry only `ref`/`title`/etc, never the goal's own `priority:`
    frontmatter value -- so `plan`'s own wave-ordering sort (`_priority_rank`, fed from a bucket
    item's "priority" field via `_survey_index`/`extra_index`) always saw `None` for a local-mode
    goal, regardless of its real tier, exactly the two-places-drift shape #706/#854 already fixed
    once for the ORDERING half of this same bucket. `docs[]` (built by `_local_goal_docs`, one
    screen up) already reads this value per-goal -- its own docstring documents it as read "the same
    way `discovery.priority_of` does" -- so this is `d["priority"]` reused as-is, not a second
    parse. Deliberately the RAW frontmatter value (`"P0"`, `"Critical"`, or `None`), never
    `discovery.priority_of`'s own return value: that function hands back an already-RANKED int
    (0-4, or `UNPRIORITISED`), and every consumer downstream of a bucket item's "priority" field
    (`_priority_rank`/`discovery.priority_rank`, both here and in every github-mode bucket) expects
    to rank a raw label itself -- handing it a pre-ranked int would silently misrank a P0 goal worst
    of all (`discovery.priority_rank(0)` reads as `UNPRIORITISED`: `str(0 or "")` is `""` via
    Python's own falsy-zero, not `"0"`)."""
    if kind in ("shadow", "hygiene", "epics", "edges"):
        return {"items": [], "count": 0, "degraded": ["not_applicable_local_mode"]}
    if kind == "active":
        items = [{"ref": d["ref"], "title": d["title"], "priority": d["priority"]}
                 for d in docs if d["status"] == "in_progress"]
        return {"items": items, "count": len(items), "degraded": []}
    if kind == "parked":
        ledger_why = {str(e.get("goal")): (e.get("why") or "")
                     for e in entries if e.get("kind") == "parked"}
        items = [{"ref": d["ref"], "reason": ledger_why.get(d["ref"], ""),
                  "reason_source": "ledger" if d["ref"] in ledger_why else None,
                  "priority": d["priority"]}
                 for d in docs if d["status"] == "parked"]
        return {"items": items, "count": len(items), "degraded": [],
                "orphaned_worktrees": _orphaned_worktrees(sdlc_dir, entries),
                "review_queue": _review_queue_names(sdlc_dir)}
    if kind == "enqueued":
        # discovery._SKIP alone (done/parked/failed/proposed) is what next_pending itself excludes
        # -- it does NOT exclude in_progress (a crashed/abandoned goal is meant to be re-picked).
        # This bucket additionally excludes in_progress, same as the github-mode bucket: it is
        # already shown under `active`, and double-listing it here would read as "not yet started"
        # for a goal that plainly is -- a refinement for a human-facing survey, not a literal mirror
        # of the raw picker's own filter.
        open_docs = sorted((d for d in docs
                            if d["status"] not in discovery._SKIP and d["status"] != "in_progress"),
                           key=lambda d: (discovery.priority_rank(d["priority"], aliases)
                                         if order == "priority" else discovery.UNPRIORITISED,
                                         d["ref"]))
        items = [{"ref": d["ref"], "title": d["title"], "priority": d["priority"]} for d in open_docs]
        return {"items": items, "count": len(items), "degraded": []}
    return {"items": [], "count": 0, "degraded": []}


def _stamp(now=None):
    """UTC, whole-second -- mirrors ledger.py's own `_stamp()` exactly, for a consistent read."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now if now is not None else time.time()))


# --------------------------------------------------------------------------- render (human table)

_BUCKET_ORDER = ("inbox", "active", "parked", "enqueued", "shadow", "epics", "hygiene", "edges", "context")
_BUCKET_TITLES = {
    "inbox": "Inbox", "active": "Active", "parked": "Parked/blocked",
    "enqueued": "Enqueued backlog", "shadow": "Shadow backlog",
    "epics": "Epics / needs-decomposition", "hygiene": "Hygiene gaps",
    "edges": "Dependency edges", "context": "Context",
}
_ROW_CAP = 10   # density lever 2: one shared row cap/formatter for every bucket, not nine bespoke ones


def _fmt_row(item):
    """One compact line per item -- shared by every bucket section (lever 2), dispatched by which
    keys the item actually carries rather than one formatter per bucket."""
    if "blocked" in item:
        return f"  - {item['blocked']} blocked by {item['by']} ({item['source']})"
    head = f"#{item['number']}" if "number" in item else item.get("ref", "")
    tail = item.get("title") or item.get("reason") or item.get("why") or ""
    if item.get("missing"):
        tail = "missing: " + ", ".join(item["missing"])
        hint = item.get("priority_hint")
        if hint:
            if hint.get("ambiguous"):
                tail += f" (rubric: ambiguous {'/'.join(hint['ambiguous'])} -- ask)"
            else:
                tail += f" (rubric hint: {hint['tier']})"
    if item.get("contradiction"):
        tail = "contradiction: " + item["contradiction"]
    if item.get("matched_label"):
        tail = f"{tail} [{item['matched_label']}]"
    return f"  - {head} {tail}".rstrip()


def render(pack):
    """The human table: nine sections in spec order, each headed `## <name> (<count>)` with a
    `[degraded: ...]` tag when non-empty (amendment 2's saturation flag surfaces here too), capped
    at `_ROW_CAP` rows with a `... and N more` tail."""
    lines = [f"# Triage survey — {pack['mode']} mode — {pack['generated_at']}", ""]
    for name in _BUCKET_ORDER:
        b = pack["buckets"].get(name, {})
        degraded = b.get("degraded") or []
        tag = f" [degraded: {', '.join(degraded)}]" if degraded else ""
        if name == "context":
            lines.append(f"## {_BUCKET_TITLES[name]}{tag}")
            for k, v in b.items():
                if k != "degraded":
                    lines.append(f"  - {k}: {v}")
            lines.append("")
            continue
        lines.append(f"## {_BUCKET_TITLES[name]} ({b.get('count', 0)}){tag}")
        items = b.get("items") or []
        for item in items[:_ROW_CAP]:
            lines.append(_fmt_row(item))
        if len(items) > _ROW_CAP:
            lines.append(f"  ... and {len(items) - _ROW_CAP} more")
        if name == "inbox" and b.get("queued_note"):
            lines.append(f"  (watch inbox note: {b['queued_note'][:120]})")
        if name == "parked":
            # Fold 6: the issue spec names three parked-bucket surfaces (reasons, review-queue.md,
            # orphaned worktrees) -- all three must reach the table, not just `items`, or the
            # printed count ("(0)") can contradict what the bucket actually knows.
            if b.get("orphaned_worktrees"):
                lines.append(f"  orphaned worktrees: {', '.join(b['orphaned_worktrees'])}")
            if b.get("review_queue"):
                lines.append(f"  review-queue.md: {', '.join(b['review_queue'])}")
        lines.append("")
    return "\n".join(lines)


# ===================================================================================================
# `plan` -- sigma-triage 2/4: the dependency-sequenced drain-plan compiler. Pure: no gh, no ledger,
# no filesystem beyond the three artifact writes at the very end of `plan_cmd` (and the optional
# `--from-survey` read). See the module docstring for the CLI shape.
# ===================================================================================================

PLAN_SCHEMA = "triage-plan/v1"
ACTIVE_SCHEMA = "triage-active/v1"

#: Mirrors loop.DEFAULT_GOALS_MAX_CONCURRENT (parallel.goals.max_concurrent) -- a documented,
#: LOCKSTEP-TESTED copy, not a live read like _list_open_issues' retry constants above: `loop.py`
#: is not a module `triage.py` already loads for other reasons, and `loop.py`'s own module-scope
#: `_load()` calls re-execute FIVE more sibling modules (state, sources, ledger, work, actionlog)
#: just to read one integer -- a real, disproportionate cost for a value this small, unlike the
#: free retry-constant read (see test_default_cap_constant_stays_in_lockstep_with_loop_py).
_DEFAULT_CAP = 3

#: Namespaced away from the per-goal plans directly under .sdlc/plans/ (a free namespace today --
#: confirmed against the live tree, no `triage/` subdirectory exists yet).
_PLANS_DIR = "plans/triage"


# --------------------------------------------------------------------------- CLI parsing (plan-only)

#: Every flag `plan` accepts is a known value-flag -- there is no bare/boolean flag on this verb at
#: all, so `_parse_plan_argv` never needs the "guess from whether the next token starts with --"
#: branch that causes both existing `_flags()` copies' identical repeat-loss bug (#669 review F1:
#: the plan's own claim that `ledger.py`'s copy is uniquely "unfixed" reproduces the OPPOSITE result
#: live -- `ledger._VALUE_FLAGS` is in fact the BROADER, correct list; it is `slices.py`'s copy that
#: mis-parses a dash-leading value. The REAL, verified defect BOTH copies share is a flat `dict`
#: silently dropping a repeated flag -- `--edge`/`--defer` cannot tolerate that, so this parser
#: descends from neither existing copy, not from "the fixed one").
_PLAN_VALUE_FLAGS = frozenset({"pick", "edge", "defer", "cap", "slug", "from-survey",
                               "resolve-missing"})
_PLAN_REPEATABLE = frozenset({"edge", "defer"})


def _parse_plan_argv(argv):
    """{"pick": str|None, "edge": [str], "defer": [str], "cap": str|None, "slug": str|None,
    "from_survey": str|None, "resolve_missing": str|None}. Every token matching a name in
    `_PLAN_VALUE_FLAGS` consumes the very next token as its value UNCONDITIONALLY -- even one that
    itself starts with `--` -- so a value can never be misread as a second, bogus flag (see the
    module-level comment above for why this verb never needs the ambiguous "boolean or value?"
    branch both existing `_flags()` copies carry -- `--resolve-missing` (#713) deliberately follows
    this same value-only rule rather than becoming this verb's first bare/boolean flag: a bare
    `--resolve-missing` immediately followed by another flag would otherwise swallow that flag as
    its own value, exactly the ambiguity this parser exists to avoid). `--edge`/`--defer` append to
    a list (`_PLAN_REPEATABLE`); every other flag keeps last-wins, matching this codebase's
    single-value convention for its other flags. An unrecognized `--foo` is silently skipped, same
    leniency as both existing copies."""
    out = {"pick": None, "edge": [], "defer": [], "cap": None, "slug": None, "from_survey": None,
           "resolve_missing": None}
    i = 0
    while i < len(argv):
        token = argv[i]
        name = token[2:] if token.startswith("--") else None
        if name in _PLAN_VALUE_FLAGS:
            value = argv[i + 1] if i + 1 < len(argv) else ""
            if name in _PLAN_REPEATABLE:
                out[name].append(value)
            elif name == "from-survey":
                out["from_survey"] = value
            elif name == "resolve-missing":
                out["resolve_missing"] = value
            else:
                out[name] = value
            i += 2
            continue
        i += 1
    return out


def _parse_pick(raw):
    """"12,15,20" -> [12,15,20] -- order-preserving, deduped, blank tokens skipped. Raises
    ValueError naming the exact bad token on any non-numeric piece."""
    seen, out = set(), []
    for tok in (raw or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        if not tok.isdigit():
            raise ValueError(f"--pick: not a valid issue number: {tok!r}")
        n = int(tok)
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _parse_edge(raw):
    """"15:12" -> (15, 12) == "15 blocked by 12". Raises ValueError naming the raw input on any
    malformed shape: no colon, more than one colon, a non-digit side, or an empty string."""
    parts = (raw or "").split(":")
    if len(parts) != 2 or not parts[0].strip().isdigit() or not parts[1].strip().isdigit():
        raise ValueError(f"--edge: malformed BLOCKED:BY pair: {raw!r}")
    return int(parts[0].strip()), int(parts[1].strip())


def _parse_defer(raw):
    """"40:needs design: ask rae" -> (40, "needs design: ask rae") -- splits on the FIRST colon
    only. Raises ValueError if there is no colon at all, the issue side is non-digit, or the
    reason is empty/whitespace-only."""
    if ":" not in (raw or ""):
        raise ValueError(f"--defer: malformed N:reason pair: {raw!r}")
    num_part, _, reason = raw.partition(":")
    if not num_part.strip().isdigit():
        raise ValueError(f"--defer: malformed N:reason pair: {raw!r}")
    if not reason.strip():
        raise ValueError(f"--defer: reason must not be empty: {raw!r}")
    return int(num_part.strip()), reason


# --------------------------------------------------------------------------- the algorithmic core


def _priority_rank(priority, aliases=None):
    """Sort rank for a picked node's priority (P0->0 .. P4->4, `discovery.UNPRIORITISED` (5) for
    None/absent, a typo, or anything else unrecognised) -- delegates entirely to
    `discovery.priority_rank` rather than parsing its own copy.

    #714: this used to parse "P0".."P9" itself (regex `^P([0-9])$`, own fallback 999) -- a WIDER
    range than `discovery.priority_rank`'s P0-P4, so a `priority:P7` label ranked 7th in `plan`'s
    own wave order (ahead of every genuinely unprioritised goal) while the live picker and
    `survey`'s enqueued bucket -- both already reading `discovery.priority_rank` -- ranked that same
    label dead last, behind every P0-P4 goal. One board, two disagreeing answers to "how urgent is
    this". Delegating closes the gap at its root -- P5-P9 now fall through `discovery.PRIORITIES`
    exactly like a typo would and collapse to the SAME rank as a truly-unprioritised goal, matching
    the live picker/survey exactly -- one definition, not two that can drift apart again. P0-P4 are
    unaffected: `discovery.priority_rank` ranks them identically to the old regex (the #698
    backward-compatibility hinge), see `test_priority_rank_p0_through_p4_unchanged_by_the_
    delegation_to_discovery`.

    `aliases` (#854 follow-up, independent-review finding): passed straight through to
    `discovery.priority_rank`. Without it, an issue labelled through `priority_aliases` (e.g.
    `priority:critical`) ranked UNPRIORITISED here while the live picker and survey's enqueued
    bucket — both already alias-aware via `sources.py` — ranked it correctly: the exact same
    two-places-drift shape #714 already fixed once for the P5-P9 case, reopened by a feature that
    forgot this was the second of two places. Omitted (the default), behavior is unchanged."""
    return discovery.priority_rank(priority, aliases)


def _cycles(nodes):
    """Every dependency cycle among `nodes`, each as its ordered member ids. DIRECT structural
    mirror of `slices._cycles()` (slices.py :164-195, confirmed live by the #669 review, letter a):
    same DFS-colour (1=on-stack, 2=explored) / back-edge / `frozenset`-deduped shape, `int` ids in
    place of `str`, `node["needs"]` in place of `slice["needs"]` (name unchanged). Reported by
    MEMBERS, never "a cycle exists" -- the fix is to break one edge, and you cannot choose which
    without the names."""
    known = {n["id"] for n in nodes}
    graph = {}
    for n in nodes:
        graph.setdefault(n["id"], [])
        graph[n["id"]] += [x for x in n["needs"] if x in known]
    colour, found, seen = {}, [], set()

    def walk(node, path):
        colour[node] = 1
        path.append(node)
        for nxt in graph.get(node, []):
            if colour.get(nxt) == 1:
                cycle = path[path.index(nxt):]
                key = frozenset(cycle)
                if key not in seen:
                    seen.add(key)
                    found.append(cycle)
            elif colour.get(nxt, 0) == 0:
                walk(nxt, path)
        path.pop()
        colour[node] = 2

    for n in nodes:
        if colour.get(n["id"], 0) == 0:
            walk(n["id"], [])
    return found


def _direct_dependents(blocker_id, remaining):
    """Nodes in `remaining` whose own "needs" names `blocker_id` directly (#900). `blocker_id`
    cannot itself be `done` yet at the point this is ever called: every id `_effective_rank` (below)
    ever calls this with is itself a member of `remaining` for THIS SAME `schedule_waves` pass --
    either the `ready` candidate the pass is currently scoring, or one of THAT candidate's own
    dependents reached by `_effective_rank`'s recursive walk (independent-review fix: a dependent
    two or more hops out is, in general, not itself `ready` -- it is still blocked, possibly by more
    than just the id this call is asking about). Either way, every id passed in is not yet done, so
    every result this returns is, by construction, not itself `ready` right now either (its own
    "needs" still names an undone id, `blocker_id`): scanning `remaining` (not `ready`) is what finds
    it, at any depth."""
    return [d for d in remaining if blocker_id in d["needs"]]


def _effective_rank(node, ready, remaining, aliases, mode, memo=None, in_progress=None):
    """`node`'s sort rank for THIS `schedule_waves` pass (#900) -- its own raw priority rank when
    `mode` is "off"/unrecognised (the default: `discovery.DEFAULT_BLOCKER_PROMOTION_MODE`), or
    PROMOTED to reflect what it blocks otherwise, via `discovery.blocker_promotion_rank` (the
    one-level ranking rule -- see that function's own docstring for full mode semantics; this is its
    per-level caller, walking a whole chain one edge at a time exactly as that docstring's own "ONE
    LEVEL ONLY, BY DESIGN" section prescribes it must).

    TRANSITIVE, not just one hop (independent-review fix -- the original #900 landing computed each
    dependent's contribution from its RAW priority, so a chain of three or more issues only ever
    promoted the innermost link, exactly one level, no matter how urgent the far end was): for each
    direct dependent `d` (`_direct_dependents` above), `d`'s contribution is `d`'s own EFFECTIVE
    rank -- this same function, called recursively on `d` -- never `d`'s raw priority. That
    recursive call resolves `d` exactly the way `node` itself is being resolved: from `d`'s OWN
    direct dependents inward, so a chain C-blocks-B-blocks-A propagates A's urgency into B first,
    then feeds B's already-promoted (not original) rank into C, one edge at a time -- matching
    `discovery.blocker_promotion_rank`'s own documented contract and `sources.py`
    `GitHubSource._promote_blockers`'s own `resolve()` (a different file, read for reference, not
    touched here): the same memo/in-progress/cycle-guard shape, adapted to `schedule_waves`'s own
    remaining/ready state in place of a flat GitHub-issue ref dict.

    `memo` (node id -> resolved rank) and `in_progress` (a set of node ids currently mid-resolution)
    are shared across every node touched in ONE `schedule_waves` wave-iteration -- `schedule_waves`
    creates both fresh at the top of each `while remaining:` pass and threads them into every one of
    that wave's `_effective_rank` calls, so a node reachable from more than one path this wave (a
    diamond dependency, or two `ready` candidates that both transitively lead to the same downstream
    issue) is only ever actually resolved once; every later reference is an O(1) memo hit rather than
    a repeat walk. Both default to None so a caller that does not know about them (any caller written
    before this parameter existed, including every direct `_effective_rank` call this suite does not
    have -- it only ever drives this through `schedule_waves`) still gets fully correct results, just
    a fresh, call-scoped memo instead of a wave-shared one, exactly as if memoization did not exist.

    CYCLE GUARD, defensive: `node["id"]` is added to `in_progress` before recursing into its own
    dependents and discarded once resolved. If a recursive call is asked to resolve an id ALREADY in
    `in_progress` -- a back-edge, whether from a genuine "needs" cycle in the (hand-authored, so
    never fully trusted) blocking graph, or simply because `ready` always includes the very node
    still unwinding higher up this same call stack, so a has-other-work scan (below) can walk
    straight back into it -- that call does not re-enter: it returns the in_progress node's own RAW
    rank, the "no further promotion pressure from this edge" value, mirroring `sources.py`
    `resolve()`'s identical in-progress-set guard. `compile_plan` already runs `_cycles()` over
    `nodes` before ever calling `schedule_waves`, so a genuine cycle should never reach this function
    on that path -- but `schedule_waves` is also callable directly, bypassing that pre-check (several
    tests in this suite already do exactly this, for the stranded-node case below), so this guard
    does not assume the precondition holds; it is unconditional.

    For each direct dependent `d`, `d` "has other unblocked work at its own tier" when `ready` --
    excluding whichever node is CURRENTLY being resolved on this call (the immediate blocker `d` is
    being evaluated as a dependent of, at whatever recursion depth that is -- not necessarily the
    original top-level `node` this whole call started from) -- already holds some OTHER node ranked
    at `d`'s tier: promoting that blocker would not change what gets worked next for `d` regardless,
    since something else at that tier is already queued ahead of it either way. "smart" mode
    excludes such a `d` from consideration entirely (not merely caps its influence); "always"
    promotes on `d`'s account regardless. Both delegate the actual pool/min-selection to
    `blocker_promotion_rank` itself.

    RAW VS EFFECTIVE, decided (independent-review finding, explicitly left open by the fix's own
    brief -- documented here rather than left implicit, since the two now genuinely diverge once a
    dependent has itself been promoted): `d`'s "own tier" for THIS check is `d`'s EFFECTIVE rank --
    the exact same value just resolved above as `d`'s contribution to `blocker_promotion_rank`'s
    `dependent_rank` parameter -- never `d`'s raw priority. Reasoning: `blocker_promotion_rank`'s own
    docstring treats "`dependent_rank`" and "the dependent's own tier" as ONE concept, not two --
    feeding `d`'s raw priority into one and its effective rank into the other would silently split
    that single concept into two different numbers the moment `d` has itself been promoted by a
    deeper chain, which is exactly the scenario this fix exists to handle correctly; mixing them
    would undo the fix by a side door. And the mechanism's own stated purpose -- "would promoting
    this blocker actually change what gets worked next" -- is answered by whatever `schedule_waves`
    actually sorts `ready` by, which is EFFECTIVE rank; checking congestion at `d`'s stale raw tier
    would ask whether a tier `d` no longer really occupies is busy, the wrong question once `d` has
    been promoted elsewhere by its own downstream chain.
    The OTHER side of this same comparison -- the candidate nodes in `ready` being checked for
    occupancy at `d`'s tier -- deliberately STAYS raw, unchanged from before this fix. `ready` always
    includes whichever node is the ORIGINAL top-level call for this whole recursion (that is how it
    came to be in `ready` to begin with), so resolving every candidate's own effective rank here
    would walk this scan straight back into that still-in-progress node on EVERY has-other check, not
    merely on a genuine graph cycle -- silently trading an ordinary linear scan for one permanently
    leaning on the cycle guard's fallback rather than a real answer. Comparing against candidates'
    raw rank avoids that entirely, still answers the question this check needs answered (is some
    other already-known-urgent issue sitting at the tier `d` would compete at), and matches
    `sources.py`'s own reference shape, whose `pickable_by_rank` buckets candidates by raw rank only,
    never a resolved one.

    Deliberately recomputed fresh every wave (never cached across `schedule_waves` iterations, only
    within one): a dependent's own eligibility -- and even the SET of a node's direct dependents
    still outstanding -- changes as earlier waves complete, so a rank computed once at the top of the
    loop would go stale by the second iteration.

    The "off" (default) short-circuit happens BEFORE any dependent scan or memo/in_progress setup,
    not merely inside `blocker_promotion_rank` itself, mirroring that function's own "callers should
    typically skip calling this at all when mode is off" guidance -- so `plan`'s default, unqualified
    wave order pays no extra cost for a feature it never asked for."""
    own_rank = _priority_rank(node["priority"], aliases)
    if mode not in discovery.BLOCKER_PROMOTION_MODES or mode == "off":
        return own_rank
    memo = {} if memo is None else memo
    in_progress = set() if in_progress is None else in_progress
    nid = node["id"]
    if nid in memo:
        return memo[nid]
    if nid in in_progress:
        return own_rank                     # cycle guard: no further promotion pressure from this edge
    in_progress.add(nid)
    dependents = []
    for d in _direct_dependents(nid, remaining):
        d_rank = _effective_rank(d, ready, remaining, aliases, mode, memo, in_progress)
        has_other = any(n["id"] != nid and _priority_rank(n["priority"], aliases) == d_rank
                        for n in ready)
        dependents.append((d_rank, has_other))
    in_progress.discard(nid)
    rank = discovery.blocker_promotion_rank(own_rank, dependents, mode=mode)
    memo[nid] = rank
    return rank


def schedule_waves(nodes, cap, aliases=None, blocker_promotion_mode=discovery.DEFAULT_BLOCKER_PROMOTION_MODE):
    """-> ordered waves (list of node dicts). Mirrors `slices.schedule()`'s own frontier/remaining/
    cap loop shape (slices.py :309-340) MINUS the pairwise `conflicts()` check (no goal-granularity
    analogue -- two goals never share a worktree, `loop.py next_batch`'s own docstring), tie-broken
    by `(priority_rank, id)` within a wave instead of `slices.py`'s widest-fan-out-first heuristic
    (the issue's own acceptance criterion: "within a wave order by priority:* (P0 first) then issue
    number" -- a different ordering philosophy, not a drop-in swap of the same one).

    `aliases` (#854 follow-up) passes straight through to `_priority_rank`, so a node whose
    `priority` came from an alias'd label (raw, unresolved -- `_build_nodes` never resolves it
    itself, ranking is the one place that does) sorts by the tier it actually means. Omitted (the
    default), a node with no literal P0-P4 priority sorts last, unchanged from before this existed.

    `blocker_promotion_mode` (#900): when not "off" (the default), each candidate's sort key is its
    EFFECTIVE rank (`_effective_rank`, above) rather than its raw `_priority_rank` -- a blocker's
    rank promotes to reflect what it blocks, walking the WHOLE transitive chain one edge at a time
    (independent-review fix -- not just the one issue it directly blocks). See `_effective_rank`/
    `discovery.blocker_promotion_rank` for the full mechanics. Omitted, a node's sort key is exactly
    its raw priority rank, byte-identical to every wave order this function produced before #900.

    Raises ValueError naming the stranded id(s) when `remaining` is non-empty once `ready` comes
    back empty (#669 review F3): `slices.schedule()`'s identical branch silently drops such nodes,
    tolerable there only because `validate()` names the reason on a separate path before
    `schedule()` is ever reached for that manifest. `plan` has no second reporter -- the same
    silent drop here would print a cheerful summary and write three artifacts while quietly
    omitting issues the operator explicitly asked to plan. Only reachable when a node's "needs"
    points at an id absent from `nodes` entirely; a genuine cycle is caught earlier, by `_cycles()`,
    before `schedule_waves` is ever called (see `compile_plan`)."""
    cap = max(1, int(cap or 1))
    done = set()
    remaining = list(nodes)
    waves = []
    while remaining:
        ready = [n for n in remaining if all(need in done for need in n["needs"])]
        if not ready:
            stranded = sorted(n["id"] for n in remaining)
            raise ValueError(
                f"stranded node(s) -- unresolvable dependency, never in nodes: {stranded}")
        memo, in_progress = {}, set()       # shared across this WHOLE wave's calls -- see _effective_rank
        effective = {n["id"]: _effective_rank(n, ready, remaining, aliases, blocker_promotion_mode,
                                              memo, in_progress)
                    for n in ready}
        ready.sort(key=lambda n: (effective[n["id"]], n["id"]))
        wave = ready[:cap]
        waves.append(wave)
        taken = {n["id"] for n in wave}
        done |= taken
        remaining = [n for n in remaining if n["id"] not in taken]
    return waves


# --------------------------------------------------------------------------- survey consumption
# Optional input (never required). `--from-survey <file>` reads a prior `survey --json` output
# so `plan` can carry titles/priority/model/edges through offline -- resolved this way rather than
# a live title/edge fetch: edges need body text (`_BLOCK_RE` over issue bodies, bucket 8's own job),
# so a "just fetch titles live" verb could not produce them at all without re-implementing bucket 8
# a second time inside `plan` (the exact copy-and-drift shape amendment 5 already fixed once for
# the retry constants, by reading live instead -- `plan` has no equivalent "read live" option for a
# whole bucket's worth of logic without either a network call from inside a "pure" verb, or
# duplicating it). Zero network either way: `plan` makes ZERO gh calls under any input.


def _load_survey(path):
    """Read+parse a --from-survey file. Returns None on ANY failure (missing file, malformed
    JSON, non-object payload, wrong/absent schema) -- degrade, never raise, never partially trust a
    survey whose shape does not match what `plan` expects. Printing the "no survey data" note is
    `plan_cmd`'s own job (one note per attempt, regardless of which of these three ways it failed)
    -- this function only reads."""
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not str(data.get("schema") or "").startswith("triage-survey/"):
        return None
    return data


#: The only survey buckets that can hold a PICKABLE issue -- an epic is never directly
#: implementable (bucket 6), and hygiene/edges/inbox/context are not issue listings at all.
_SURVEY_PICKABLE_BUCKETS = ("active", "enqueued", "shadow", "parked")


def _survey_index(survey):
    """{issue_number: {"title": str, "priority": str|None, "model": str|None}}, scanned across
    active/enqueued/shadow/parked. Missing survey, a bucket whose own items are not a list, or an
    item missing "number": silently skipped -- degrade, never raise."""
    index = {}
    if not survey:
        return index
    buckets = survey.get("buckets") or {}
    for name in _SURVEY_PICKABLE_BUCKETS:
        items = (buckets.get(name) or {}).get("items") or []
        for item in items:
            if not isinstance(item, dict) or "number" not in item:
                continue
            try:
                n = int(item["number"])
            except (TypeError, ValueError):
                continue
            index[n] = {"title": item.get("title") or "", "priority": item.get("priority"),
                       "model": item.get("model")}
    return index


#: 1:1 from G1's own edges-bucket `source` vocabulary, renamed so a survey-relayed edge can never
#: be confused with the user's OWN --edge (source "cli", see _build_nodes) -- two different
#: meanings of what G1 itself calls "explicit" (a body-text match, bucket 8) vs. what `plan` means
#: by the same English word (a value the user just typed on this invocation).
_SURVEY_EDGE_SOURCE_MAP = {"explicit": "survey-body", "ledger": "survey-ledger"}


def _survey_edges(survey):
    """[(blocked, by, source)] from the survey's own edges bucket. Non-digit blocked/by (a
    local-goals ref) is skipped, not raised -- `plan`'s own domain is GitHub issue numbers only. A
    self-reference is also skipped, defensively (G1's own extraction already excludes these, but a
    survey FILE is untrusted input by the time `plan` reads it back)."""
    edges = []
    if not survey:
        return edges
    for e in ((survey.get("buckets") or {}).get("edges", {}).get("items") or []):
        if not isinstance(e, dict):
            continue
        blocked_raw, by_raw = str(e.get("blocked", "")), str(e.get("by", ""))
        if not blocked_raw.isdigit() or not by_raw.isdigit():
            continue
        blocked, by = int(blocked_raw), int(by_raw)
        if blocked == by:
            continue
        source = _SURVEY_EDGE_SOURCE_MAP.get(e.get("source"))
        if source is None:
            continue
        edges.append((blocked, by, source))
    return edges


# --------------------------------------------------------------------------- #713: opt-in fallback
#
# `plan` stays "pure compile -- ZERO network... under every input" by DEFAULT (module docstring):
# this is the one exception, and it is deliberately never reached unless `--resolve-missing N` is
# passed explicitly. It lives here, not inside compile_plan -- `plan_cmd` calls it BEFORE compile_
# plan, and hands the result in as compile_plan's own `extra_index=` parameter, so the compiler
# itself never performs I/O regardless of whether this ran.


def _resolve_missing_picks(picked, survey_index, gh_source, run, limit):
    """{number: {"title","priority","model"}} for every PICKED issue absent from `survey_index`,
    ONE bounded `gh issue view` call each, up to `limit` total -- #713's opt-in index-lag fallback
    (`gh issue list`'s search-backed index is only eventually consistent; a just-labelled issue can
    be genuinely missing from a survey snapshot taken moments earlier).

    Bounded and targeted, never blind: only picks NOT already in `survey_index` are ever read, in
    `picked`'s own order (deterministic -- which picks get the limited budget when there are more
    misses than `limit` is never a set-iteration accident), and never more than `limit` of them
    regardless of how many are missing.

    Never raises: a single failed read (the issue genuinely does not exist, a transient network
    blip, `gh` unavailable) just leaves that ONE pick unresolved -- `compile_plan` already shows a
    bare "#N" for anything not in its merged index, exactly today's status quo, so a failure here
    costs nothing beyond "no better than before". Deliberately NOT retried the way the hot picker
    path (`_list_open_issues`/`_pending_by_label`) is: this is an explicit, already-bounded,
    best-effort top-up the caller chose to run, not the one read a whole loop tick depends on --
    adding backoff/retry here would only add latency to an opt-in convenience for marginal benefit,
    and the existing manual mitigation (re-run once the index has caught up) still works if it
    misses.

    `gh_source` supplies the configured `priority_label_prefix` (`gh_source is None` -- not
    constructed, e.g. non-github discovery mode -- falls back to the historical `"priority:"`,
    same contract every #706 bucket already uses) and, when present, scopes the read to its own
    `repo`."""
    missing = [n for n in picked if n not in survey_index]
    prefix = gh_source.priority_prefix if gh_source is not None else "priority:"
    resolved = {}
    for n in missing[:limit]:
        try:
            # #895: REST first; no source here, so no breaker/log (no sdlc_dir); `run` is also the fallback.
            item = sources.gh_api.read_issue(run, int(n), ["number", "title", "labels"],
                                             gh_source.repo if gh_source is not None and gh_source.repo else None,
                                             gql_run=run)
        except Exception:
            continue
        if not isinstance(item, dict) or "number" not in item:
            continue
        names = _label_names(item)
        resolved[n] = {"title": item.get("title") or "", "priority": _label_value(names, prefix),
                       "model": _label_value(names, "model:")}
    return resolved


# --------------------------------------------------------------------------- compile core (pure)


def _build_nodes(picked, cli_edges, survey_edges, survey_index):
    """(nodes, edges_out). `nodes`: one {"id","needs","priority","model","title"} dict per picked
    issue -- "needs" holds ONLY blockers that are themselves picked (an edge whose `by` is not
    picked never sequences scheduling; it is surfaced via `edges_out` as "external" instead).
    `edges_out`: every edge (cli-typed ∪ survey-relayed, deduped by (blocked,by) with cli winning
    ties) as {"blocked","by","source","external"} for the JSON artifact -- shown only when its
    `blocked` side is itself picked (an edge about an issue outside this plan entirely has no place
    in ITS edge list; a cli edge's blocked side is already guaranteed picked by compile_plan's own
    validation, so this filter is load-bearing only for survey-relayed noise)."""
    picked_set = set(picked)
    seen, all_edges = set(), []
    for blocked, by in cli_edges:
        key = (blocked, by)
        if key not in seen:
            seen.add(key)
            all_edges.append((blocked, by, "cli"))
    for blocked, by, source in survey_edges:
        key = (blocked, by)
        if key not in seen:
            seen.add(key)
            all_edges.append((blocked, by, source))

    needs = {n: [] for n in picked}
    edges_out = []
    for blocked, by, source in all_edges:
        if blocked not in picked_set:
            continue
        external = by not in picked_set
        edges_out.append({"blocked": blocked, "by": by, "source": source, "external": external})
        if not external and by not in needs[blocked]:
            needs[blocked].append(by)

    nodes = []
    for n in picked:
        info = survey_index.get(n) or {}
        nodes.append({"id": n, "needs": sorted(needs[n]), "priority": info.get("priority"),
                      "model": info.get("model"), "title": info.get("title") or f"#{n}"})
    return nodes, edges_out


def compile_plan(picked, cli_edges, deferred, cap, survey=None, now=None, extra_index=None,
                 aliases=None, blocker_promotion_mode=discovery.DEFAULT_BLOCKER_PROMOTION_MODE):
    """Pure given its args -- no gh, no ledger, no filesystem (the optional survey read already
    happened by the time this is called; so has the optional live-read fallback below, if any --
    `extra_index` is a plain caller-supplied dict, never something this function fetches itself).

    `aliases` (#854 follow-up): passed straight through to `schedule_waves`. `compile_plan` is
    deliberately config-free (see above), so this is the caller's (`plan_cmd`'s) job to compute
    from config and hand in — omitted, a node's raw `priority` (which may itself be an unresolved
    alias spelling, e.g. `"critical"`, if it came from a github-mode label survey never ran through
    `discovery.priority_rank` before landing on the node) sorts last, unchanged from before this
    parameter existed.

    `blocker_promotion_mode` (#900): passed straight through to `schedule_waves`, same config-free
    posture as `aliases` above -- `plan_cmd` resolves it from `discovery.blocker_promotion.mode` via
    `sources._blocker_promotion(config)` and hands it in here. Omitted (the default, "off"), wave
    order is exactly what `schedule_waves` produced before this parameter existed.
    Validates, IN ORDER (an earlier check can never be masked by a later one): self-edge ->
    blocked-not-picked -> defer/pick overlap -> cycle -> schedule. Every validation failure is a
    ValueError naming the exact bad value. A cyclic graph is instead reported as DATA
    (`result["cycle_error"]`, empty waves) -- mirrors `slices.py` `main()`'s own "problems ->
    return before ever calling schedule()" ordering: a cyclic INPUT is a legitimate finding about
    the graph's SHAPE, not a malformed-argument mistake the way self-edge/blocked-not-picked/
    defer-overlap are, so the caller (`plan_cmd`) reports it differently (exit 1, not 2) even
    though both paths write nothing.

    `extra_index` (#713): an optional {number: {"title","priority","model"}} dict -- `plan_cmd`'s
    own opt-in `--resolve-missing` fallback, computed BEFORE this call for picks the survey index
    doesn't cover. Merged UNDER the survey-derived index (survey wins on any overlap, though
    `plan_cmd`'s own construction never produces one by design -- extra_index only ever contains
    picks already confirmed absent from the survey). This keeps `compile_plan` itself exactly as
    pure as it was before #713: whether or not a live read happened is entirely the caller's
    business, this function only ever merges two already-in-memory dicts.

    `now` is the one ambient-clock reading in this whole compiler: `generated_at` is embedded in
    the result so `render_json`/`render_active` (via `plan_cmd`) read ONE single timestamp, never
    recomputing their own."""
    picked_set = set(picked)
    deferred_index = {}
    for n, why in deferred:
        deferred_index[n] = why

    for a, b in cli_edges:
        if a == b:
            raise ValueError(f"--edge: self-reference is not allowed: {a}")
    for a, b in cli_edges:
        if a not in picked_set:
            raise ValueError(f"--edge: blocked issue #{a} is not in --pick")
    overlap = picked_set & set(deferred_index)
    if overlap:
        raise ValueError(f"--defer: issue(s) also in --pick: {sorted(overlap)}")

    survey_idx = {**(extra_index or {}), **_survey_index(survey)}
    survey_edge_list = _survey_edges(survey)
    nodes, edges_out = _build_nodes(picked, cli_edges, survey_edge_list, survey_idx)
    generated_at = _stamp(now)

    cycles = _cycles(nodes)
    if cycles:
        cycle = cycles[0]
        return {"cycle_error": "cycle: " + " -> ".join(f"#{n}" for n in cycle + [cycle[0]]),
                "cap": cap, "waves": [], "edges": [], "deferred": [], "generated_at": generated_at}

    waves = schedule_waves(nodes, cap, aliases=aliases, blocker_promotion_mode=blocker_promotion_mode)
    return {"cycle_error": None, "cap": cap, "waves": waves, "edges": edges_out,
            "deferred": [{"issue": n, "why": why} for n, why in deferred],
            "generated_at": generated_at}


# --------------------------------------------------------------------------- artifact rendering

_SOURCE_LABELS = {"cli": "cli", "survey-body": "survey: issue body", "survey-ledger": "survey: ledger hand-off"}


def render_md(result, slug, date_str):
    """Human drain plan: waves, per-issue priority/model, the dependency map, deferred-with-
    reason, and a how-to-run footer -- "no empty sections": the Dependency map / Deferred headers
    are omitted entirely when there is nothing under them."""
    waves = result["waves"]
    picked_count = sum(len(w) for w in waves)
    lines = [f"# Drain plan — {slug} ({date_str})", "",
             f"Cap: {result['cap']} · Picked: {picked_count} issue(s) in {len(waves)} wave(s) · "
             f"Deferred: {len(result['deferred'])}", ""]

    for i, wave in enumerate(waves, start=1):
        lines.append(f"## Wave {i} — {len(wave)} issue(s)")
        for n in wave:
            priority = n["priority"] or "-"
            model = n["model"] or "-"
            lines.append(f"- #{n['id']} · {priority} · {model} · {n['title']}")
        lines.append("")

    internal = [e for e in result["edges"] if not e["external"]]
    external = [e for e in result["edges"] if e["external"]]
    if internal or external:
        lines.append("## Dependency map")
        for e in internal:
            lines.append(f"- #{e['blocked']} blocked by #{e['by']} "
                         f"({_SOURCE_LABELS.get(e['source'], e['source'])})")
        for e in external:
            lines.append(f"- #{e['blocked']} also depends on #{e['by']} "
                         "(external — not in this plan; drains on its own schedule)")
        lines.append("")

    if result["deferred"]:
        lines.append("## Deferred")
        for d in result["deferred"]:
            lines.append(f"- #{d['issue']} — {d['why']}")
        lines.append("")

    lines += [
        "## How to run",
        "- A wave of width 1 is a strict chain: drain it with plain `next`.",
        "- A wider wave's members have NO edges between each other in this plan, so they are safe",
        "  to work with `next-batch` — but only once every issue in an EARLIER wave has actually",
        "  landed. `next-batch` does not know about this plan's edges: it would happily claim a",
        "  goal from a later wave whose blocker is still open, and the loop's own precheck would",
        "  just park-and-delabel it again. Drain wave-by-wave.",
        "- `next-batch` only returns more than one goal when `parallel.goals.enabled` is `true` in",
        "  config; otherwise it degrades to a single-item batch and hands back one goal at a time,",
        "  same as plain `next` — a wider wave is then simply the safe DRAIN ORDER, not a batch you",
        "  actually get back from one call.",
        f"- Active plan: .sdlc/plans/triage/active.json -> {date_str}-{slug}.json",
    ]
    return "\n".join(lines)


def render_json(result, slug, date_str, cap):
    """{version, schema, generated_at, slug, cap, picked, edges, deferred, waves} -- `picked` is
    ordered (wave, priority_rank, issue), the same order `render_md` renders in, so the two
    artifacts never visually disagree. `json.dumps`'d by the writer (`plan_cmd`), not here."""
    picked = []
    for wave_idx, wave in enumerate(result["waves"], start=1):
        for n in wave:
            picked.append({"issue": n["id"], "priority": n["priority"], "model": n["model"],
                          "wave": wave_idx})
    return {"version": 1, "schema": PLAN_SCHEMA, "generated_at": result["generated_at"],
           "slug": slug, "cap": cap, "picked": picked, "edges": result["edges"],
           "deferred": result["deferred"], "waves": [[n["id"] for n in w] for w in result["waves"]]}


def render_active(json_name, md_name, generated_at):
    """{schema, plan_json, plan_md, generated_at} -- filenames RELATIVE to .sdlc/plans/triage/
    itself (never absolute), so a later `enact` verb or a human opening the directory can resolve
    them directly."""
    return {"schema": ACTIVE_SCHEMA, "plan_json": json_name, "plan_md": md_name,
           "generated_at": generated_at}


# --------------------------------------------------------------------------- atomic write


def _atomic_write_text(path, text):
    """Mirrors `state._patch_cursor`'s own publish tail exactly: `mkstemp` in the SAME directory
    (so `os.replace` stays on one filesystem, the only case it is guaranteed atomic) then
    `os.replace` onto the real path -- a reader only ever observes the fully-old or fully-new file,
    never a truncated one, including under the same-slug-overwrite case (`plan`'s own residual: a
    crash strictly BETWEEN two of the three writes can leave a mixed old/new pair when the
    filenames don't change across a re-run -- ordering the three writes json-then-md-then-active
    protects the cross-slug/cross-date case, not this one; nothing short of a directory-level swap
    would)."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, str(path))


# --------------------------------------------------------------------------- cap + slug plumbing


def _cap_from_config(config):
    """`parallel.goals.max_concurrent`, mirroring `loop.goals_parallel()`'s own try/except + floor
    shape exactly (WITHOUT its enabled-gating: a drain plan's wave WIDTH is a planning-time concern
    independent of whether today's config has opted into live parallel dispatch -- see the module
    docstring's "how to run" footer text, which tells the operator this explicitly). Falls back to
    `_DEFAULT_CAP` on anything non-numeric; ALSO floors any successfully-parsed value at 1 (#669
    review F10): a configured `0` or negative `max_concurrent` is a VALID int, not a
    non-numeric-fallback case, and would otherwise reach `schedule_waves` as a zero/negative cap."""
    block = (config.get("parallel") or {}).get("goals") or {}
    try:
        cap = int(block.get("max_concurrent", _DEFAULT_CAP))
    except (TypeError, ValueError):
        cap = _DEFAULT_CAP
    return max(1, cap)


def _validate_slug(slug):
    """`state.unsafe_goal_reason(slug)` (REUSED, not copied -- #669 review F7 ruling: `state.py`
    imports stdlib only, so `_load("state")` costs nothing comparable to the loop.py-import cost
    that justifies copying `_DEFAULT_CAP`; and `unsafe_goal_reason`'s own docstring is explicit
    that it is THE shared validator for every such path in this plugin, created precisely because
    per-caller copies produced the repeated #486/#487 path-traversal fixes a re-typed character
    class would silently reintroduce) PLUS a guard the shared function does not itself enforce: the
    review's own probe found `''`, `'.'`, and `'  '` all pass `unsafe_goal_reason` unchanged (it
    exists to block path TRAVERSAL via `..`, not to reject a merely degenerate slug) and would
    still produce a useless filename like `2026-08-10-.json`. Stripped of whitespace AND
    leading/trailing dots before the emptiness check, so all three of those are caught by this ONE
    rule, while a slug that merely CONTAINS a dot in the middle (`my.slug`) is untouched. Raises
    ValueError naming the reason on either failure; returns `slug` unchanged on success."""
    reason = state.unsafe_goal_reason(slug)
    if reason:
        raise ValueError(f"--slug {slug!r} is unsafe: {reason}")
    if not (slug or "").strip(" \t\r\n."):
        raise ValueError(f"--slug {slug!r} must not be empty, whitespace-only, or dots-only")
    return slug


# --------------------------------------------------------------------------- CLI orchestration


_PLAN_USAGE = ("usage: triage.py plan <sdlc_dir> --pick n[,n...] [--edge BLOCKED:BY]... "
              "[--defer n:reason]... [--cap K] [--slug s] [--from-survey FILE] "
              "[--resolve-missing N]")


def plan_cmd(sdlc_dir, config, argv_tail, now=None, run=None):
    """parse -> validate --pick present -> compile_plan -> on cycle_error: print, write nothing,
    return 1 -> else: render all three, atomic-write all three (json+md first, active.json LAST --
    see `_atomic_write_text`'s own docstring for what that ordering does and does not protect),
    print a short summary, return 0. Every ValueError from parsing/validation/compile prints its
    message and returns 2, writing nothing -- the same "hard error, nothing written" contract a
    cyclic input gets, just a different exit code (2 for a malformed/contradictory INPUT the
    caller typed, 1 for a genuine finding about an otherwise-valid input's graph SHAPE).

    `run` (#713): ONLY ever touched inside the `--resolve-missing` branch below -- every other
    input path (the overwhelming majority of calls, including every one of this module's own
    pre-#713 tests) never reads this parameter at all, keeping `plan`'s "zero network under every
    input" guarantee intact for its default, unqualified form. `main()` never passes it explicitly
    (defaults to `None`), matching `survey()`'s own `run_ = run or sources._run_gh` fallback
    pattern -- constructed lazily, only when there is an actual missing pick to resolve."""
    parsed = _parse_plan_argv(argv_tail)
    if not parsed["pick"]:
        print(_PLAN_USAGE, file=sys.stderr)
        return 2

    try:
        picked = _parse_pick(parsed["pick"])
        cli_edges = [_parse_edge(e) for e in parsed["edge"]]
        deferred = [_parse_defer(d) for d in parsed["defer"]]
        slug = _validate_slug(parsed["slug"] if parsed["slug"] is not None else "plan")
    except ValueError as exc:
        print(f"triage plan: {exc}", file=sys.stderr)
        return 2

    if not picked:
        print(_PLAN_USAGE, file=sys.stderr)
        return 2

    raw_cap = parsed["cap"]
    if raw_cap and raw_cap.isdigit() and int(raw_cap) > 0:
        cap = int(raw_cap)
    else:
        cap = _cap_from_config(config)
        if raw_cap:
            print(f"triage plan: --cap {raw_cap!r} is not a positive integer — "
                  f"using the config default ({cap})", file=sys.stderr)

    survey_data = None
    if parsed["from_survey"]:
        survey_data = _load_survey(parsed["from_survey"])
        if survey_data is None:
            print("triage plan: no survey data — titles/priority/model/survey-edges unavailable, "
                  "run survey first for a fuller plan", file=sys.stderr)

    # #713: opt-in only -- parsed["resolve_missing"] is None (the overwhelming majority of calls)
    # skips this whole block without reading `run`/`config` for it at all, exactly preserving
    # plan's pre-#713 "zero network under every input" behavior for its default, unqualified form.
    extra_index = None
    raw_resolve = parsed["resolve_missing"]
    if raw_resolve is not None:
        if raw_resolve.isdigit() and int(raw_resolve) > 0:
            if _is_github(config):
                survey_idx_so_far = _survey_index(survey_data)
                missing = [n for n in picked if n not in survey_idx_so_far]
                if missing:
                    run_ = run or sources._run_gh
                    gh_source = _gh_source(config, run_)
                    extra_index = _resolve_missing_picks(picked, survey_idx_so_far, gh_source,
                                                         run_, int(raw_resolve))
                    print(f"triage plan: --resolve-missing resolved {len(extra_index)}/"
                          f"{min(len(missing), int(raw_resolve))} attempted missing pick(s) via "
                          "a live read", file=sys.stderr)
            else:
                print("triage plan: --resolve-missing has no effect outside github discovery "
                      "mode — ignored", file=sys.stderr)
        else:
            print(f"triage plan: --resolve-missing {raw_resolve!r} is not a positive integer — "
                  "ignoring (no live reads attempted)", file=sys.stderr)

    # #900: `blocker_promotion_mode` threads the identical depth `aliases` already does (config ->
    # plan_cmd -> compile_plan -> schedule_waves) -- see schedule_waves'/compile_plan's own
    # docstrings for the mechanics. Thread it the FULL depth here too, not just into
    # schedule_waves' own signature -- that partial-threading gap is exactly what #854 regressed on
    # once already (aliases reached schedule_waves but never plan_cmd/compile_plan, so the live
    # picker and plan's wave order silently disagreed on every real usage).
    try:
        result = compile_plan(picked, cli_edges, deferred, cap, survey=survey_data, now=now,
                              extra_index=extra_index, aliases=sources._priority_aliases(config),
                              blocker_promotion_mode=sources._blocker_promotion(config))
    except ValueError as exc:
        print(f"triage plan: {exc}", file=sys.stderr)
        return 2

    if result["cycle_error"]:
        print(f"triage plan: {result['cycle_error']}", file=sys.stderr)
        return 1

    # #680: derived from the ALREADY-STAMPED generated_at (compile_plan's own single clock read,
    # via _stamp()), not a second, independent time.time() call of its own — the old form here
    # could disagree with generated_at's date whenever the two reads straddled the UTC-midnight
    # second, producing a filename dated the day AFTER the timestamp embedded inside it.
    # generated_at's own format ("%Y-%m-%dT%H:%M:%SZ", see _stamp()) makes the first 10 characters
    # exactly "%Y-%m-%d" -- byte-identical to what this used to compute separately.
    date_str = result["generated_at"][:10]
    plans_dir = pathlib.Path(sdlc_dir) / _PLANS_DIR
    json_name, md_name = f"{date_str}-{slug}.json", f"{date_str}-{slug}.md"
    json_path, md_path, active_path = plans_dir / json_name, plans_dir / md_name, plans_dir / "active.json"

    md_text = render_md(result, slug, date_str)
    json_obj = render_json(result, slug, date_str, cap)
    active_obj = render_active(json_name, md_name, result["generated_at"])

    _atomic_write_text(json_path, json.dumps(json_obj, ensure_ascii=False, indent=2, sort_keys=True))
    _atomic_write_text(md_path, md_text)
    _atomic_write_text(active_path, json.dumps(active_obj, ensure_ascii=False, indent=2, sort_keys=True))

    print(f"triage plan: {len(result['waves'])} wave(s), cap {cap}, "
          f"{len(result['deferred'])} deferred — {json_path} / {md_path}")
    return 0


# ===================================================================================================
# #670: `enact` (sigma-triage 3/4) -- compiles an already-written `plan.json` into native primitives.
# `--plan <path>` is enact's ONLY input, unlike `plan`'s own optional `--from-survey`: a bad/missing
# file is a hard failure (`_load_plan`), never a graceful degrade -- there is nothing to enact
# without it. Reads happen in BOTH dry-run and --apply, always (the issue's own "identical shape
# either way" requirement -- add-only-what's-missing needs the CURRENT state to know what is
# missing); WRITES happen only under --apply. Every gh call funnels through a `sources.GitHubSource`
# instance's own `_run()`/`_repo_args()`/`_ensure_labels()`/`append_to_body()` -- the primitives
# `sources.py` already ships for exactly this (`create_dependency`'s per-label `label create`
# loop, `_offboard`'s one-label-per-call edits, `handoff.py:267`'s own marker string) -- never a new
# `sources.py` method and never a bespoke retry loop (`_run()`'s own retry is `project`-only; no
# existing mutating GitHubSource method retries an `issue` subcommand either).
# ===================================================================================================

#: Closed action vocabulary -- matches this codebase's own taste for small closed sets
#: (ledger.KINDS, actionlog.*_KINDS) over an open string.
ENACT_ACTIONS = ("assign", "add-label", "remove-label", "append-marker", "swap-label")

#: Matches `create_dependency`'s own color for an ad-hoc, non-core label (sources.py :506) --
#: cosmetic, named here for consistency, not load-bearing.
_ARBITRARY_LABEL_COLOR = "d4c5f9"

#: Byte-identical to the ONE existing call site, handoff.py:267 (`source.append_to_body(str(goal),
#: f"**Blocked by:** #{report['issue']}")`) -- no shared constant exists there to import, so this
#: re-types the identical literal rather than inventing new phrasing `_BLOCK_RE` would also match
#: but a human reading both markers side by side would see diverge for no reason.
_MARKER_TMPL = "**Blocked by:** #{n}"

def _swap_detail(add, remove):
    """The human-readable `detail` string for a `swap-label` action: `+a, +b -c`. #1392 -- `detail`
    must stay a plain string because `render_actions`/`auto_unpark.render_sweep` both format it
    directly into an output line; the machine-readable label lists live in the action's own
    `add`/`remove` keys instead. Shared by both producers so the two never render the same swap
    differently."""
    return " ".join([*(f"+{l}" for l in add), *(f"-{l}" for l in remove)])


_ACTION_VERBS = {"assign": "assign to", "add-label": "add label", "remove-label": "remove label",
                 "append-marker": "mark blocked by", "swap-label": "swap labels"}


def _load_plan(path):
    """Read + validate a `--plan` file. Raises `ValueError` naming the concrete problem (unreadable
    file, invalid JSON, non-object payload, wrong/absent schema, or a `picked`/`edges`/`deferred`
    field present but not a list) -- unlike `_load_survey`'s own graceful degrade (optional context
    for a compiler that still produces a valid plan without it), `--plan` is enact's SOLE required
    input: there is nothing to enact without it, so a bad file is a hard failure, never a silent
    empty run. `enact_cmd` (below) is what turns this into exit 2."""
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"could not read {path!r}: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{path!r} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path!r} is not a JSON object")
    if not str(data.get("schema") or "").startswith("triage-plan/"):
        raise ValueError(f"{path!r} has schema {data.get('schema')!r}, expected triage-plan/*")
    for key in ("picked", "edges", "deferred"):
        if key in data and not isinstance(data[key], list):
            raise ValueError(f"{path!r}: {key!r} must be a list")
    return data


def _fetch_issue_state(source, issue):
    """{"labels": set[str], "assignees": set[str], "body": str} for ONE issue -- the fresh, current
    state every add-only-what's-missing check below compares against. `None` on any failure
    (transport error, unparseable JSON, non-object payload) -- the caller turns that into exactly
    ONE "failed" action for the issue (`_picked_actions`), never a guess about what is or isn't
    already there. Mirrors `fetch_title_body`'s own `issue view --json ...` shape, via
    `source._run`/`source._repo_args()` directly -- the same private-but-cross-module-reused
    primitives `_list_open_issues` above already calls (`_is_transient`); no new `sources.py`
    method, one `gh` call per picked issue (this file's own established granularity -- never a
    batched multi-issue fetch, matching `fetch_comments`/`fetch_title_body`/
    `fetch_comments_strict`'s own single-issue shape)."""
    try:
        data = source._read_issue(issue, ["labels", "assignees", "body"])    # #895: REST first
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return {"labels": {l.get("name") for l in (data.get("labels") or []) if isinstance(l, dict)},
           "assignees": {a.get("login") for a in (data.get("assignees") or []) if isinstance(a, dict)},
           "body": data.get("body") or ""}


def _already_marked(body, by):
    """True iff `body` already carries a `_BLOCK_RE`-detectable marker for blocker `by`
    SPECIFICALLY -- per-referenced-number precision (mirrors `_explicit_blockers`'s own `n in
    open_refs` check, and `_scan_block_edges` above), never the coarser "does ANY blocked-by phrase
    exist somewhere". Reads `backlog_check._BLOCK_RE` LIVE off the module attribute, the same reuse
    contract every other consumer in this file already keeps.

    KNOWN, ACCEPTED false-positive class (670-plan-review.md amendment F2, binding): `_BLOCK_RE`'s
    phrase alternation includes ordinary English ("needs", "after", "requires", "waiting on"), so a
    body merely discussing another issue near a `#N` -- "after #12 ships", "this needs #12 landed
    first" -- reads as an EXISTING marker and suppresses the real one `enact` would otherwise
    install. The issue text is unconditional ("anything `_BLOCK_RE` already detects in the body
    counts as existing"), so this is not tightened here -- overriding the spec of record is not
    this module's call, and the alternative failure (occasionally appending a harmless duplicate
    line) is worse in the other direction. Failure direction, stated plainly: `enact` UNDER-acts
    (skips installing a real marker) on this class, never over-acts. A one-line caveat for G4/#671's
    docs pass: "enact treats any `_BLOCK_RE` hit as an existing marker, including an incidental
    prose mention.\""""
    return any(m.group(2) == str(by) for m in backlog_check._BLOCK_RE.finditer(body or ""))


def _label_create_calls(plan):
    """The DEDUPED set of every `priority:*`/`model:*` label this plan's picked issues actually
    reference, in first-seen (picked-array) order -- computed once so a tier shared by several
    issues triggers ONE `gh label create` call, not one per issue. Amortizes
    `create_dependency`'s own per-label loop (sources.py :502-508) across the whole run instead of
    one issue's labels at a time; the underlying gh-call shape is unchanged."""
    seen, out = set(), []
    for item in (plan.get("picked") or []):
        for value, prefix in ((item.get("priority"), "priority:"), (item.get("model"), "model:")):
            if not value:
                continue
            name = f"{prefix}{value}"
            if name not in seen:
                seen.add(name)
                out.append(name)
    return out


def _picked_actions(plan, state_by_issue, assignee, goal_label, parked_label, blocked_label=None,
                    proposed_label=None):
    """One picked issue at a time, in `plan["picked"]`'s own array order; PER issue, in this fixed
    sub-order: assign (if `assignee` is configured and missing) -> add `goal_label` (if missing) ->
    remove `parked_label` (if present) -> remove `blocked_label` (if present) -> add `priority:*`
    (if the plan names one and it's missing) -> add `model:*` (same). Every step is independently
    skipped when already satisfied, so the returned list holds only the GAPS -- what makes
    "idempotent: second apply = zero actions" literal, not merely low-count.

    "Unpark v1-lite" is not a separate branch: per the label model (a PARK drops `goal_label` and
    adds `parked_label`; #1393 -- a BLOCK keeps `goal_label` and only adds the `sdlc:blocked`
    overlay), a parked picked issue is, structurally,
    just one missing `goal_label` + one carrying `parked_label` -- exactly the two ordinary checks
    above, applied together. `sdlc:in-progress` is never read or written here (out of scope, named
    in the module docstring's own enact section -- not in the issue's own action-list enumeration).

    #1358: `blocked_label` is `goal_blocked_label` (#1350's `sdlc:blocked`) -- an OPTIONAL, KEYWORD-
    only, default-`None` parameter, deliberately, so every EXISTING positional caller (none of
    which pass it) is byte-for-byte unchanged. It exists because `_bucket_parked` (survey) now
    ALSO surfaces a `sdlc:blocked`-only picked issue in this same "parked" bucket (#1358) -- without
    this, picking one would add `goal_label` back with `sdlc:blocked` never removed, leaving BOTH
    present at once, the exact double-primary-label state #1350's own `_offboard` fix exists to
    prevent on the write side. `None` (the default, and every pre-#1358 caller) is a genuine no-op:
    the guard below short-circuits before ever comparing against `state["labels"]`.

    A missing/failed state fetch (`state_by_issue[n] is None`) emits exactly ONE "failed" action for
    that issue (never a guess, never one per hypothetical mutation) -- `_edge_actions` below never
    re-reports the identical failure a second time when the same issue is also a `blocked` side."""
    actions = []
    for item in (plan.get("picked") or []):
        n = str(item.get("issue"))
        state = state_by_issue.get(n)
        if state is None:
            actions.append({"action": "add-label", "issue": n, "detail": "(current state unknown)",
                            "result": "failed",
                            "error": f"could not read the current state of #{n}"})
            continue
        if assignee and assignee not in state["assignees"]:
            actions.append({"action": "assign", "issue": n, "detail": assignee,
                            "result": None, "error": None})
        # #1392: the lifecycle trio is now ONE `swap-label` action rather than up to three
        # independent ones -- see `_execute_action`'s own `swap-label` branch for why a partially
        # applied unpark is strictly worse than no unpark at all. The gap-detection is UNCHANGED
        # (each label is still only named when it actually needs changing, so "second apply = zero
        # actions" still holds literally); only the number of writes it takes to close the gap is.
        add = [goal_label] if goal_label not in state["labels"] else []
        # #1393: `proposed_label` joins the removal set. LEGACY-INERT since decision-rubric slice 18:
        # nothing writes the label now, but a picked issue that still carries a leftover is cleaned
        # here, exactly as before. Picking an issue that carries the label used to ADD `sdlc:goal` on top of it, producing exactly the
        # half-promoted state `_fetch_pending`, `_card_is_eligible` and `/sigma-promote`'s `drift`
        # bucket all call drift -- so `enact` was manufacturing the corruption promote.py exists to
        # repair, and the picked issue stayed unpickable afterwards. A human choosing an issue in a
        # triage plan IS the approval, so this makes picking the atomic promote transition.
        remove = [l for l in (parked_label, blocked_label, proposed_label)
                  if l and l in state["labels"]]
        if add or remove:
            actions.append({"action": "swap-label", "issue": n, "detail": _swap_detail(add, remove),
                            "add": add, "remove": remove, "result": None, "error": None})
        for value, prefix in ((item.get("priority"), "priority:"), (item.get("model"), "model:")):
            if not value:
                continue
            label = f"{prefix}{value}"
            if label not in state["labels"]:
                actions.append({"action": "add-label", "issue": n, "detail": label,
                                "result": None, "error": None})
    return actions


def _edge_actions(plan, state_by_issue):
    """One edge at a time, in `plan["edges"]`'s own array order. Three skips, each defensive and
    each named:

    - A self-referential edge (`blocked == by`, string-compared): `plan.json`'s edges are already
      guaranteed self-ref-free by BOTH of G2's own producers (cli-typed: a hard `ValueError` at
      compile time; survey-relayed: dropped in `_survey_edges`) -- but `--plan <path>` is a
      filesystem argument, not necessarily a file `triage.py plan` just wrote, so this guards it
      here too, the same belt-and-suspenders instinct 668-pr-review-findings.md finding 2 already
      established one hop upstream.
    - A REPEATED `(blocked, by)` pair (670-plan-review.md amendment F1, binding): `--edge` is a
      repeatable flag (`_PLAN_REPEATABLE`) and nothing in `_build_nodes` dedupes the `edges_out`
      list it writes to `plan.json` -- `triage.py plan --pick 15 --edge 15:12 --edge 15:12` writes
      the IDENTICAL edge object twice. Without this `seen` set both copies would be evaluated
      against the same pre-write body snapshot, both find no marker, both emit `append-marker`, and
      `apply_actions` would execute both -- `append_to_body` has NO dedup of its own (see
      `_already_marked`'s docstring), so the body would carry the identical line twice. This is the
      corrected version of an earlier plan draft's claim that "two markers for the same issue are
      safe regardless" -- that claim was wrong for the SAME-blocker case; it is only true for
      markers of DIFFERENT blockers on the same issue (two distinct, both-wanted lines).
    - A `blocked` issue outside `state_by_issue` (never fetched -- only picked issues are fetched,
      §`enact`): naturally excludes both a genuine state-fetch failure (already reported once by
      `_picked_actions`, never a second time here) and a hand-edited plan whose edge points at an
      issue this run never picked (out of scope, the same posture as never touching a deferred
      issue)."""
    actions, seen = [], set()
    for edge in (plan.get("edges") or []):
        blocked, by = str(edge.get("blocked")), str(edge.get("by"))
        if blocked == by:
            continue
        pair = (blocked, by)
        if pair in seen:
            continue
        seen.add(pair)
        state = state_by_issue.get(blocked)
        if state is None:
            continue
        if _already_marked(state["body"], by):
            continue
        actions.append({"action": "append-marker", "issue": blocked, "detail": by,
                        "result": None, "error": None})
    return actions


def compute_actions(plan, state_by_issue, assignee, goal_label, parked_label, blocked_label=None,
                    proposed_label=None):
    """The full, ordered action list: every picked-issue action (assign/label/unpark), THEN every
    edge action (markers) -- a two-phase composition mirroring the issue's own two spec bullets
    ("Per picked issue: ..." / "Per dependency edge: ..."), fully deterministic because both halves
    are derived from `plan.json`'s own already-deterministic `picked`/`edges` arrays.

    `blocked_label` (#1358): threaded straight through to `_picked_actions` -- see that function's
    own docstring. Optional/keyword, default `None`, for the identical backward-compat reason."""
    return (_picked_actions(plan, state_by_issue, assignee, goal_label, parked_label,
                            blocked_label=blocked_label, proposed_label=proposed_label)
           + _edge_actions(plan, state_by_issue))


def _label_conflict_notices(plan, state_by_issue):
    """670-plan-review.md amendment F4 (binding, ruling on Open Decision 3): a picked issue that
    already carries a SAME-PREFIX, DIFFERENT-VALUE `priority:*`/`model:*` label is never
    reconciled here -- removal is a different, riskier operation than "add only what's missing",
    the issue's own literal words -- but it must not go SILENT either: `loop.py`'s own priority
    inheritance (`loop.py` :837-838, `next((n[len("priority:"):] for n in names if
    n.startswith("priority:")), None)`) takes the FIRST `priority:*` label in whatever order the
    labels API returns them, so a double tier feeds a downstream field whose value is effectively
    arbitrary -- not cosmetic. Report-only: named here, surfaced in `render_actions()` and in
    `enact()`'s own `result["degraded"]`, NEVER as an executable action -- it cannot affect the
    exit code or the "second apply = zero actions" invariant."""
    notices = []
    for item in (plan.get("picked") or []):
        n = str(item.get("issue"))
        state = state_by_issue.get(n)
        if state is None:
            continue
        for value, prefix in ((item.get("priority"), "priority:"), (item.get("model"), "model:")):
            if not value:
                continue
            current = _label_value(state["labels"], prefix)
            if current and current != value:
                notices.append(f"#{n}: plan wants {prefix}{value} but already carries "
                               f"{prefix}{current} — left both in place (add-only); "
                               "resolve by hand if this is unintended")
    return notices


def _stale_marker_notices(plan, state_by_issue):
    """670-plan-review.md amendment F5 (binding, ruling on Open Decision 8): a picked issue's body
    can already assert a blocker `_BLOCK_RE` would honor that THIS plan's own edge set for that
    issue does not know about -- most often a marker an OLDER plan compile left behind whose edge
    has since been dropped. `enact` never removes a marker (same "add only what's missing, no
    removal" posture F4 already applies to labels), so left silent this stalls the drain: precheck
    keeps parking an issue this plan considers unblocked, for as long as the stale reference happens
    to stay open -- worse than F1's duplicate line because it is invisible. Report-only, same
    constraints as `_label_conflict_notices`. A hit whose `#N` is the issue's OWN number is excluded
    (mirrors `_explicit_blockers`'s self-reference rule); repeated mentions of the same stale `#N`
    within one body produce one notice, not one per occurrence."""
    notices = []
    for item in (plan.get("picked") or []):
        n = str(item.get("issue"))
        state = state_by_issue.get(n)
        if state is None:
            continue
        plan_blockers = {str(e.get("by")) for e in (plan.get("edges") or [])
                         if str(e.get("blocked")) == n}
        already_noted = set()
        for m in backlog_check._BLOCK_RE.finditer(state["body"] or ""):
            by = m.group(2)
            if by == n or by in plan_blockers or by in already_noted:
                continue
            already_noted.add(by)
            notices.append(f"#{n}: body asserts blocker #{by}, not in this plan's edges — "
                           f"precheck will park this issue until #{by} closes")
    return notices


def compute_notices(plan, state_by_issue):
    """Every report-only notice (F4 + F5), picked-issue-scoped then edge-scoped -- same two-phase
    shape as `compute_actions`, for the same reason."""
    return _label_conflict_notices(plan, state_by_issue) + _stale_marker_notices(plan, state_by_issue)


def _ensure_arbitrary_labels(source, names):
    """`gh label create <name>` per DISTINCT name, best-effort -- mirrors `create_dependency`'s
    own per-label loop (sources.py :502-508) exactly ("a missing label must not stop the [edit]
    that follows"), just amortized across the whole run via `_label_create_calls` instead of
    re-issued per issue. Never itself a reported action -- infrastructure for the `add-label`
    actions that reference these names.

    #1917: the mirroring is why this site matters. It carried `--force` because
    `create_dependency` did, and `--force` overwrites an EXISTING label's colour -- so a triage
    plan naming `priority:P1` repainted it. Creation stays idempotent without the flag: `gh`
    refuses a name that exists, and the except below is what absorbs that."""
    for name in names:
        try:
            source._run(["label", "create", name, *source._repo_args(),
                        "--color", _ARBITRARY_LABEL_COLOR])
        except Exception:
            pass


def _execute_action(source, action):
    """The one `gh` call (or `append_to_body` call) for one action -- see the module docstring's
    gh-call-mapping. Raises on failure; `apply_actions` is what catches it."""
    kind, issue, detail = action["action"], action["issue"], action["detail"]
    if kind == "assign":
        source._issue_add_assignees(issue, detail)      # REST first (#895 slice 3a)
    elif kind == "add-label":
        source._issue_add_labels(issue, [detail])         # REST; non-feature labels mint (D3)
    elif kind == "remove-label":
        source._issue_remove_label(issue, detail)
    elif kind == "swap-label":
        # #1392: ONE atomic lifecycle transition, replacing the add-label/remove-label PAIR this
        # module used to emit for an unpark. That pair is the same non-atomic write #1391 step 2
        # removed from every `GitHubSource` transition -- and it survived here, on the RECOVERY
        # path, which is the worst possible place for it: a half-applied unpark adds `sdlc:goal`
        # without removing `sdlc:parked`, and `_fetch_pending` excludes `parked_label`, so the goal
        # stays exactly as stuck as before AND now carries two primary lifecycle labels. The
        # machinery that repairs drift was manufacturing it.
        #
        # `detail` stays a human-readable STRING (`render_actions` formats it straight into its
        # output line); the two label LISTS ride in additive `add`/`remove` keys, which survive
        # `apply_actions`' own `dict(action)` copy untouched. `_swap_labels` raises on final
        # failure, which `apply_actions` already turns into a per-action `failed` -- so a swap that
        # does not land is recorded as not-landed, never as done.
        add, remove = list(action.get("add") or ()), list(action.get("remove") or ())
        if not add and not remove:
            raise ValueError(f"swap-label for #{issue} carries neither add nor remove")
        source._swap_labels(issue, add=add, remove=remove)
    elif kind == "append-marker":
        source.append_to_body(issue, _MARKER_TMPL.format(n=detail))
    elif kind == "set-status":
        # #1391 step 4: move a board card. Until this existed, `_execute_action` had exactly four
        # kinds and NONE of them could touch the board -- so every unpark applied by this module (and
        # by `auto_unpark.py`, which reuses `apply_actions` live) fixed the LABEL and left the CARD
        # where it was. On a board-authoritative repo that manufactures a permanently-unpickable
        # goal: correctly labelled `sdlc:goal`, card still sitting in `Blocked`, invisible to
        # `_board_queue` forever, with no sweep anywhere that moves cards. The recovery machinery was
        # producing the unrecoverable state.
        #
        # `_set_board_status` returns an honest bool as of the same change (it used to return None
        # unconditionally, making success indistinguishable from a swallowed failure). Raising on
        # False is what lets `apply_actions` record this action as `failed` rather than reporting a
        # card move that never happened as `done` -- the whole reason the honest return exists.
        if not source._set_board_status(issue, detail):
            raise RuntimeError(f"board status write did not land for #{issue} -> {detail!r}")
    else:
        raise ValueError(f"unknown enact action kind: {kind!r}")


def apply_actions(source, actions, apply):
    """Returns a NEW list (never mutates `actions`) with `result`/`error` filled in for every
    action whose `result` is not already terminal (a state-fetch-failure sentinel from
    `_picked_actions` is passed through unchanged, in EITHER mode -- it reflects a READ failure
    common to both dry-run and apply, not a write outcome, so it is never re-labelled "would").

    `apply=False`: every other action becomes `"would"`; NO gh call is made for it, ever -- proven
    behaviorally, not just by omission (`test_apply_actions_dry_run_never_calls_gh_for_writes`).
    `apply=True`: `_execute_action`'s one call is wrapped in its own `try/except` -- success ->
    `"done"`; an exception -> `"failed"` + `str(exc)` in `error`, and the loop ALWAYS continues to
    the next action (the issue's own explicit "a failed action... does not abort the remaining
    actions" acceptance criterion)."""
    out = []
    for action in actions:
        if action.get("result") is not None:
            out.append(dict(action))
            continue
        a = dict(action)
        if not apply:
            a["result"] = "would"
            out.append(a)
            continue
        try:
            _execute_action(source, a)
            a["result"], a["error"] = "done", None
        except Exception as exc:
            a["result"], a["error"] = "failed", str(exc)
        out.append(a)
    return out


def enact(sdlc_dir, config, plan, apply=False, run=None):
    """The whole enact pass over an ALREADY-LOADED `plan` dict (see `_load_plan` / `enact_cmd` for
    turning a bad `--plan` file into a hard CLI failure before this is ever called) -- mirrors
    `compile_plan`'s own "takes parsed data, not raw CLI args" shape, just not gh-network-pure the
    way `compile_plan` is network-pure, because reading current issue state IS the whole point.

    Fetches fresh state for every PICKED issue only (never a deferred one, and never an edge's
    `blocked` side that isn't itself picked -- both out of scope, by construction: nothing outside
    `plan["picked"]` is ever named in a `source._run`/`append_to_body` call anywhere in this pass).

    `source._ensure_labels()` runs ONLY under `apply=True`, alongside `_ensure_arbitrary_labels`
    (PR #683 review, BLOCKING, cycle 1/3): it is a `gh label create` WRITE per core label, and
    dry-run's own contract is "change nothing" -- nothing in the READ half above (state-fetch,
    action/notice computation) needs the labels to already exist, so there is no reason for this
    call to run before the `if apply:` gate. Previously unconditional; caught no-op only by
    accident, because this repo's labels already exist -- a FRESH repo would have made a real write
    on every dry-run. (#1917 narrowed which repos that sentence covers, and the narrowing is the
    point: while the call carried `--force` it was a create-OR-RECOLOR, so a repo whose labels
    merely had different colours was written to on every dry-run too. Without the flag `gh` refuses
    a name that exists, so only a genuinely missing label is a write -- and this call still belongs
    behind the gate, because a fresh repo is exactly the one running its first dry-run.)"""
    source = sources.GitHubSource(config, run=run)
    picked = plan.get("picked") or []
    needed = {str(item.get("issue")) for item in picked if item.get("issue") is not None}
    state_by_issue = {n: _fetch_issue_state(source, n) for n in needed}

    assignee = _gh_cfg(config).get("assignee") or None
    # #1358: `source.goal_blocked_label` closes the gap `_bucket_parked` (survey) opened by also
    # surfacing a `sdlc:blocked`-only issue in this same pickable bucket -- see `_picked_actions`'s
    # own docstring for why picking one must also be able to clear that label, not just `sdlc:parked`.
    actions = compute_actions(plan, state_by_issue, assignee, source.goal_label, source.parked_label,
                              blocked_label=source.goal_blocked_label,
                              proposed_label=source.proposed_label)
    notices = compute_notices(plan, state_by_issue)

    if apply:
        source._ensure_labels()
        _ensure_arbitrary_labels(source, _label_create_calls(plan))
    actions = apply_actions(source, actions, apply)

    return {"slug": plan.get("slug"), "cap": plan.get("cap"), "apply": bool(apply),
           "actions": actions, "degraded": notices}


def render_actions(result):
    """One header line (mode + plan slug + action count), one line per action, a notices section
    when non-empty, and a summary line -- the issue's own "(action, issue, done/would-do)" shape,
    human-readable. No `--json` flag: unlike `survey`'s explicit `[--json]`, the issue's own CLI
    signature for `enact` never asks for one, and the structured `result` dict this reads is already
    directly unit-testable without exposing one."""
    mode = "APPLIED" if result["apply"] else "DRY-RUN"
    lines = [f"enact ({mode}): plan {result.get('slug') or '?'!r} — "
            f"{len(result['actions'])} action(s)"]
    for a in result["actions"]:
        verb = _ACTION_VERBS.get(a["action"], a["action"])
        # `detail` is a bare issue number for "append-marker" (the SAME value `_edge_actions`/
        # `_execute_action`/`_MARKER_TMPL.format` need unprefixed) but a label name/login for
        # every other action -- the "#" belongs in the RENDERED text only, never in `detail`
        # itself, so it is added here, not by changing what `detail` stores.
        shown = f"#{a['detail']}" if a["action"] == "append-marker" else a["detail"]
        line = f"  [{a['result']}] #{a['issue']}: {verb} {shown}"
        if a["result"] == "failed" and a.get("error"):
            line += f" — {a['error']}"
        lines.append(line)
    if not result["actions"]:
        lines.append("  no actions needed — already enacted")
    if result["degraded"]:
        lines.append("Notices (report-only — not acted on):")
        for note in result["degraded"]:
            lines.append(f"  - {note}")
    if result["apply"]:
        done = sum(1 for a in result["actions"] if a["result"] == "done")
        failed = sum(1 for a in result["actions"] if a["result"] == "failed")
        lines.append(f"summary: {done} done, {failed} failed")
    else:
        would = sum(1 for a in result["actions"] if a["result"] == "would")
        lines.append(f"summary: {would} would-do (pass --apply to execute)")
    return "\n".join(lines)


_ENACT_USAGE = "usage: triage.py enact <sdlc_dir> --plan <path> [--apply]"


def _plan_flag_value(argv):
    """The value following `--plan` -- the one VALUE-flag `enact` needs. `_flags()` (the shared
    boolean-presence scanner `main()` already uses for `survey`'s own `--json`, reused below for
    `--apply`) has no notion of a flag's OWN value; this is a small, dedicated companion for the one
    exception, not a second general-purpose parser (`plan`'s own `_parse_plan_argv` is left
    untouched -- it was built assuming zero bare/boolean flags exist on `plan` at all, which does
    not hold for `enact`'s `--apply`, so generalizing it would mean editing an already-shipped,
    already-tested sibling verb's parser as part of THIS goal's diff)."""
    for i, token in enumerate(argv):
        if token == "--plan" and i + 1 < len(argv):
            return argv[i + 1]
    return None


def enact_cmd(sdlc_dir, config, argv_tail, run=None):
    """parse -> require `--plan` (usage + exit 2) -> `_load_plan` (a `ValueError` here is ALSO exit
    2, distinct from a runtime partial failure) -> `enact` -> print `render_actions` -> exit 1 iff
    any action's `result == "failed"`, else 0. `--apply`'s presence is read via the shared
    `_flags()` scanner, matching `main()`'s own `"json" in _flags(...)` idiom for `survey`."""
    plan_path = _plan_flag_value(argv_tail)
    if not plan_path:
        print(_ENACT_USAGE, file=sys.stderr)
        return 2
    try:
        plan = _load_plan(plan_path)
    except ValueError as exc:
        print(f"triage enact: {exc}", file=sys.stderr)
        return 2
    apply_ = "apply" in _flags(argv_tail)
    result = enact(sdlc_dir, config, plan, apply=apply_, run=run)
    print(render_actions(result))
    return 1 if any(a["result"] == "failed" for a in result["actions"]) else 0


# --------------------------------------------------------------------------- CLI


def _flags(argv):
    """Local copy of the shared `--flag` scanner idiom (house convention: no cross-import)."""
    out = {}
    for token in argv:
        if token.startswith("--"):
            out[token[2:]] = "true"
    return out


USAGE = ("usage: triage.py survey <sdlc_dir> [--json] | " + _PLAN_USAGE[len("usage: "):]
         + " | " + _ENACT_USAGE[len("usage: "):])


def main(argv):
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    if len(argv) >= 3 and argv[1] == "survey":
        pack = survey(argv[2])
        if "json" in _flags(argv[3:]):
            print(json.dumps(pack, ensure_ascii=False, sort_keys=True))
        else:
            print(render(pack), end="")
        return 0
    if len(argv) >= 3 and argv[1] == "plan":
        return plan_cmd(argv[2], _config(argv[2]), argv[3:])
    if len(argv) >= 3 and argv[1] == "enact":
        return enact_cmd(argv[2], _config(argv[2]), argv[3:])
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
