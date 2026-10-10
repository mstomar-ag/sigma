"""Read-only backlog status: counts by goal status + run cursor + queue state. Zero-dep.
(A 3-line frontmatter read is duplicated from sigma-loop's frontmatter.py on purpose — the two
skills are independently installable units; sharing a lib across them would over-couple them.)"""
import sys, pathlib, re, json, importlib.util, time

_HERE = pathlib.Path(__file__).resolve().parent

_COUNT_CAP = 500   # `_github_counts`'s own per-label fetch ceiling -- unchanged from the old
                   # `gh issue list --limit 500`; a backlog with more than this many issues under
                   # one label silently undercounts, same as before #1833's transport migration.


def _real_run(args):
    import subprocess
    try:
        p = subprocess.run(args, capture_output=True, text=True)
        return p.stdout if p.returncode == 0 else ""
    except Exception:
        return ""


def _load_merge_queue():
    """Same-directory sibling load, not a bare `import merge_queue` -- that only resolves when
    status.py runs as `__main__` (sys.path[0] == this dir); this repo's own tests instead load
    status.py itself via `importlib.util.spec_from_file_location`, under which a plain import of a
    sibling module is not guaranteed to find it. Mirrors doctor.py's `_load_loop_script` idiom.
    Never raises: an unloadable sibling degrades to 'no merge-queue advice', not a broken dashboard —
    the #976 advisor is additive, and a status line that has always worked must keep working even if
    this one file goes missing or fails to import."""
    try:
        spec = importlib.util.spec_from_file_location("merge_queue", _HERE / "merge_queue.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m
    except Exception:
        return None

def _load_handoff():
    """Cross-load the sibling sigma-loop skill's handoff.py, mirroring `_load_merge_queue`'s own
    same-file-location idiom one skill directory over (and doctor.py's `_load_loop_script`, the
    established name for this exact cross-skill pattern). #1348: needed for `proposed_label()` --
    the config-overridable label name a queued follow-up carries -- so this file's own counting
    stays correct for a repo that has renamed it away from the literal default, instead of a
    hardcoded string silently drifting from whatever `handoff.py` actually applies. Fail-open like
    `_load_merge_queue`: an unloadable sibling must not break a status line that has always worked;
    the caller falls back to the literal default name."""
    try:
        spec = importlib.util.spec_from_file_location(
            "handoff", _HERE.parent.parent / "sigma-loop" / "scripts" / "handoff.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m
    except Exception:
        return None


def _load_sources():
    """Cross-load the sibling sigma-loop skill's sources.py, mirroring `_load_handoff`'s own idiom
    one skill directory over. #1833: needed for the shared `fetch_issues_rest`/
    `resolve_assignee_login` REST-fetch helpers `_github_counts` migrates onto below -- every
    `n(*labels)` call there built a `gh issue list --label ...` shape, which #1829's own live
    research confirmed is graphql-search-billed (against the shared 5000/hour `graphql` resource,
    ~2600+ points/call) regardless of `--search`, the same defect that motivated migrating the
    loop's own pick path. Fail-open like every other cross-load in this file: an unloadable
    sibling means `_github_counts` falls back to the pre-#1833 `gh issue list` construction
    instead of zeroing out every count -- a status line that has always worked must keep working
    even if this one file goes missing."""
    try:
        spec = importlib.util.spec_from_file_location(
            "sources", _HERE.parent.parent / "sigma-loop" / "scripts" / "sources.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m
    except Exception:
        return None


_FENCE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)


def _status_of(text):
    m = _FENCE.match(text)
    if not m:
        return None
    s = re.search(r"^status:\s*(\S+)", m.group(1), re.MULTILINE)
    return s.group(1).strip('"') if s else None        # parity with frontmatter.parse (strips quotes)


def _config(base):
    try:
        return json.loads((base / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _loop_heartbeat_segment(sdlc_dir, now=None):
    """Newest loop heartbeat age for the status tail; empty means no managing loop evidence."""
    now = time.time() if now is None else now
    latest = None
    directory = pathlib.Path(sdlc_dir) / "state" / "heartbeat"
    for path in directory.glob("*.json") if directory.is_dir() else ():
        try:
            seen = json.loads(path.read_text()).get("last_seen")
            if isinstance(seen, (int, float)) and not isinstance(seen, bool):
                latest = seen if latest is None else max(latest, seen)
        except (OSError, ValueError, TypeError):
            pass
    if latest is None:
        return ""
    age = max(0, int(now - latest))
    return f"loop heartbeat: {age // 3600:02d}:{(age % 3600) // 60:02d}:{age % 60:02d} ago"


def _github_counts(gh_cfg, run, config=None):
    """Backlog counts from the LIVE board, not the (empty in github mode) .sdlc/goals dir — else a
    github-backed loop reports '0 parked' while N issues sit parked. Scoped to the same assignee the
    loop discovers by, so it shows YOUR queue. Fail-open: an unreachable gh yields zeros, never raises.

    Label bookkeeping is asymmetric and matters here: a claimed goal gets `sdlc:in-progress` (KEEPING
    `sdlc:goal`); parking/failing it adds `sdlc:parked` and drops `sdlc:goal` but LEAVES
    `sdlc:in-progress` behind. So a parked issue still carries the in-progress label. Counting bare
    `sdlc:in-progress` would therefore count parked issues as active (and floor `pending` to 0). Active
    in-progress is the intersection `sdlc:goal AND sdlc:in-progress` (gh ANDs repeated `--label`); a
    parked issue, having lost `sdlc:goal`, is excluded and counted only under `sdlc:parked`.
    pending = open `sdlc:goal` minus those active-in-progress. done/failed don't map to a single
    open-issue label in github mode, so they stay 0.

    LEGACY-ONLY (decision-rubric slice 18): the confirmation label is no longer written -- a filed
    follow-up is already a goal -- so `proposed` counts LEFTOVERS from old boards only, and is 0 on
    a clean repo. The label name is read as a legacy alias (`handoff.proposed_label(config)`).

    #233: `proposed` DOES map now. A retro/hand-off follow-up filed with immediately_actionable=False
    carries the distinct, config-overridable label `handoff.proposed_label(config)` (#1348 renamed
    the shipped default to `sdlc:needs-confirmation`; a repo can still override it back) and
    withholds `sdlc:goal`, so it is filed-but-never-auto-picked. Nothing surfaced that set before, so
    it grew unnoticed six times in one run (#224–#232). Counting it MINUS those a human has since
    promoted (by adding `sdlc:goal`) is the exact mirror of the pending computation above: a promoted
    proposal is already counted under pending/in_progress, so it must not double-count here, and a
    closed one drops out via `--state open`. Assignee-scoped like every other count — YOUR queue.

    `config` (#1348): threaded through so the counted label matches whatever this repo's OWN
    `handoff.proposed_label(config)` actually resolves to, rather than a hardcoded literal that
    could silently drift from what `handoff.py` really applies. `None` (a caller that hasn't been
    updated, or a context with no full config handy) falls back to the shipped default name.

    #1833: every `n(*labels)` call below is now the shared `fetch_issues_rest` REST fetch
    (`sources.py`, cross-loaded by `_load_sources` above) instead of `gh issue list --label ...`
    -- confirmed graphql-search-billed regardless of `--search`, the same defect #1829 fixed on
    the loop's own pick path. `assignee` is resolved to a real login ONCE, up front, and reused
    across every count: the OLD `--assignee @me` shape cost nothing extra (GitHub's search backend
    resolves the `@me` alias server-side), but REST's `assignee=` query param has no such alias
    (a hard 422, confirmed live by #1829's own research) -- resolving it once here, like
    `GitHubSource._fetch_pending` does, means this costs exactly ONE extra `gh api user` call
    total for a whole `/sigma-status` run, not one per count."""
    repo, assignee = gh_cfg.get("repo") or "", gh_cfg.get("assignee") or ""
    # The optional, human-set "do not auto-pick" convention (#1205). Unset by default, in which case
    # it is simply not part of the rule.
    human_hold = gh_cfg.get("blocked_label") or ""
    handoff = _load_handoff()
    if handoff is not None:
        proposed_label = handoff.proposed_label(config or {})
    else:
        proposed_label = "sdlc:needs-confirmation"

    src = _load_sources()
    login = src.resolve_assignee_login(lambda a: run(["gh", *a]), assignee) if (src and assignee) else None

    def n(*labels):
        if src is not None:
            try:
                issues = src.fetch_issues_rest(lambda a: run(["gh", *a]), repo, list(labels),
                                                _COUNT_CAP, assignee=login)
                return len(issues)
            except Exception:
                return 0
        # `sources.py` could not be loaded at all (a broken/partial install) -- fall back to the
        # pre-#1833 shape rather than silently zeroing out every count on this dashboard.
        args = ["gh", "issue", "list", "--state", "open", "--json", "number",
                "--limit", str(_COUNT_CAP)]
        for lb in labels:
            args += ["--label", lb]
        if repo:
            args += ["--repo", repo]
        if assignee:
            args += ["--assignee", assignee]
        try:
            return len(json.loads(run(args) or "[]"))
        except (ValueError, TypeError):
            return 0

    goal = n("sdlc:goal")
    inprog = n("sdlc:goal", "sdlc:in-progress")   # ACTIVE only — a parked issue lost sdlc:goal
    # #1393: a BLOCKED goal keeps `sdlc:goal` (blocked became an overlay, not an exit -- only a
    # park gives up membership), so it is inside `goal` above and must be subtracted out too.
    # Without this, `pending` reports every blocked goal as ready to work -- the single most
    # misleading number this function could produce, since "pending" is exactly the count a human
    # reads to decide whether the loop has anything to do.
    # #1468: both spellings come from config now. `"sdlc:blocked"` was hardcoded here while the
    # label is renameable through `discovery.github.goal_blocked_label`, so a repo that renamed it
    # silently counted zero blocked goals and over-reported `pending` by exactly that many.
    blocked = n("sdlc:goal", gh_cfg.get("goal_blocked_label", "sdlc:blocked"))
    # #1468: the OTHER overlay that keeps `sdlc:goal` and is refused by `_fetch_pending` -- a goal
    # held because the `feature:` label its body declares does not exist yet. Same reason
    # `blocked` is subtracted: `pending` must be the PICKER's rule, not a subset of it. Counted
    # separately rather than folded into `blocked`, because the two need different human actions:
    # one waits for an issue to close, the other waits for somebody to create a label.
    needs_label = n("sdlc:goal", gh_cfg.get("needs_label_label", "sdlc:needs-label"))
    # #2263: the sibling overlay -- same reason `needs_label` is subtracted (it carries `sdlc:goal`
    # and is refused by `_fetch_pending`/`not_eligible_labels`, so leaving it in `goal` would
    # overstate `pending`), a different declaration state: no unit anywhere, rather than one whose
    # label is missing. OFF by default (`no_dangling_goal.enabled` unset) means this label is never
    # written, so this count is 0 on every repo that has not opted in — the subtraction is then a
    # no-op, not a behaviour change.
    needs_unit = n("sdlc:goal", gh_cfg.get("needs_unit_label", "sdlc:needs-unit"))
    # #2427: the THIRD overlay in this same family, missing until now -- #2363's `sdlc:needs-triage`
    # (every tier of automatic classification tried and failed) rides alongside `sdlc:goal` and is
    # refused by `not_eligible_labels()` exactly like `needs_label`/`needs_unit` above, so leaving
    # it out of the subtraction overstated `pending` by exactly this many: a set-aside issue the
    # real picker correctly refuses was still counted as ready. Same "0 on an unopted-in repo, so
    # the subtraction is a no-op there" property the sibling overlays already have.
    needs_triage = n("sdlc:goal", gh_cfg.get("needs_triage_label", "sdlc:needs-triage"))
    # #1393: `pending` is what a human reads to decide whether the loop has anything to do, so it
    # must be the PICKER's eligibility rule and not a subset of it. Both of these carry `sdlc:goal`
    # and are refused by `_fetch_pending`, so counting them as ready overstates the queue: an issue
    # a human half-promoted, and one held by the repo's own `blocked_label` convention.
    half_promoted = n("sdlc:goal", proposed_label)
    held = n("sdlc:goal", human_hold) if human_hold else 0
    parked = n("sdlc:parked")
    proposed = n(proposed_label)
    promoted = n(proposed_label, "sdlc:goal")     # a human promoted it — now a real goal, not pending
    return {"pending": max(0, goal - inprog - blocked - needs_label - needs_unit - needs_triage
                          - half_promoted - held),
            "in_progress": inprog, "parked": parked, "blocked": blocked,
            "needs_label": needs_label, "needs_unit": needs_unit, "needs_triage": needs_triage,
            "done": 0, "failed": 0, "proposed": max(0, proposed - promoted)}


def summary(sdlc_dir, run=None):
    base = pathlib.Path(sdlc_dir)
    counts = {"pending": 0, "in_progress": 0, "done": 0, "parked": 0, "failed": 0, "proposed": 0}
    cfg = _config(base)
    disc = cfg.get("discovery") or {}
    mq_advice = None
    if disc.get("source") == "github":
        gh_run = run or _real_run
        gh_cfg = disc.get("github") or {}
        counts.update(_github_counts(gh_cfg, gh_run, config=cfg))
        # #976: read-only merge-queue detect + recommend. github-mode only -- local mode makes no
        # `gh` calls today and that invariant must not regress. Repo-level evidence (branch
        # protection, PR history, Actions runs), not assignee-scoped like the counts above -- a
        # merge-queue candidate is a property of the REPO, not of any one person's queue.
        mq = _load_merge_queue()
        if mq is not None:
            branch = (cfg.get("work") or {}).get("base") or "main"
            try:
                mq_advice = mq.advise(gh_cfg, gh_run, branch=branch)
            except Exception:                # fail-open: an advisory hiccup must never break /sigma-status
                mq_advice = None
    else:
        for p in sorted((base / "goals").glob("*.md")):
            try:
                s = _status_of(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            if s in counts:
                counts[s] += 1
    cur = base / "state" / "STATE.md"
    it = 0
    if cur.exists():
        try:
            text = cur.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        m = re.search(r"^iteration:\s*(\d+)", text, re.MULTILINE)
        it = int(m.group(1)) if m else 0
    q = base / "state" / "review-queue.md"
    try:
        q_text = q.read_text(encoding="utf-8", errors="replace") if q.exists() else ""
    except OSError:
        q_text = ""
    queue_nonempty = bool(q_text and re.search(r"^## ", q_text, re.MULTILINE))
    return {**counts, "iteration": it, "queue_nonempty": queue_nonempty,
            "ledger_entries": _ledger_entries(base),
            "goals_since_align": _goals_since_align(base, counts["done"], it),
            "goals_since_audit": _goals_since_audit(base, counts["done"], it),
            "merge_queue_advice": mq_advice}


#: Goals shipped before a cumulative-drift audit is worth running. Drift needs a trajectory to be
#: visible at all, and re-reading the same short window just produces noise.
#: ponytail: a flat count, not a cadence — bump it if /sigma-align keeps coming back clean.
ALIGN_EVERY = 5

#: Goals shipped before a whole-repo audit is worth running — a MULTIPLE of ALIGN_EVERY because
#: architecture erodes more slowly than strategy drifts, and an audit that fired as often as the
#: alignment check is one people learn to skip. Skipping is how a periodic check quietly stops
#: being periodic.
#: ponytail: a flat count like its neighbour — bump it if /sigma-audit keeps coming back clean.
AUDIT_EVERY = ALIGN_EVERY * 3


def _goals_since_audit(base, done, iteration):
    """Work not yet covered by a whole-repo audit. Same "the last report IS the state" trick as
    _goals_since_align — the report records the count it reviewed, so there is no bookkeeping file.

    Deliberately NOT gated on a north-star, unlike its neighbour: drift is measured against a stated
    direction and is meaningless without one, but an artifact can rot against its own contracts,
    tests and structure whether or not anyone ever wrote a direction down."""
    reports = sorted((base / "knowledge" / "audit").glob("*.md")) if (base / "knowledge" / "audit").is_dir() else []
    reviewed = 0
    if reports:
        try:                                   # ISO-dated names sort newest-last; unreadable = never ran
            text = reports[-1].read_text(encoding="utf-8", errors="replace")
            m = re.search(r"^goals_reviewed:\s*(\d+)", text, re.MULTILINE)
            reviewed = int(m.group(1)) if m else 0
        except OSError:
            pass
    return max(0, max(done, iteration) - reviewed)


def _goals_since_align(base, done, iteration):
    """Work not yet covered by an alignment audit — None when there's no north-star to drift from.
    The last report IS the state (no extra bookkeeping file): it records the count it reviewed.

    This exists because the trigger would otherwise be circular — "run the drift audit once drift
    becomes a problem" can't fire, since undetected drift is exactly what you can't observe without
    the audit. A count is something the loop can actually see.

    Counts the LARGER of two signals, because neither covers both backlog modes on its own: the local
    `done` tally is blind in github mode (goals are issues there, and .sdlc/goals/ stays empty), while
    the loop's `iteration` cursor is blind to interactive `/sigma-goal` runs (only /sigma-loop advances
    it). Whichever is higher is the honest floor for "how much has happened".
    ponytail: a github-mode backlog driven ONLY interactively advances neither, so it under-counts and
    the offer stays silent — `/sigma-align` still runs fine on demand. Counting closed `sdlc:goal`
    issues would close that hole, at the cost of making this dashboard shell out to `gh`."""
    if not (base / "context" / "north-star.md").exists():
        return None
    reports = sorted((base / "knowledge" / "align").glob("*.md")) if (base / "knowledge" / "align").is_dir() else []
    reviewed = 0
    if reports:
        try:                                   # ISO-dated names sort newest-last; unreadable = never ran
            text = reports[-1].read_text(encoding="utf-8", errors="replace")
            m = re.search(r"^goals_reviewed:\s*(\d+)", text, re.MULTILINE)
            reviewed = int(m.group(1)) if m else 0
        except OSError:
            pass
    return max(0, max(done, iteration) - reviewed)


def _ledger_entries(base):
    """Ledger lines across every author's file. Zero when the ledger is off or absent, which
    keeps the status line byte-identical for a repo that has not opted in. `errors="replace"` is
    load-bearing, not defensive dressing (mirrors doctor.py's `_count_jsonl_lines`): a process
    killed mid-append truncates a multi-byte UTF-8 sequence, and the resulting UnicodeDecodeError
    is a ValueError, NOT an OSError, so it would sail past the catch below and take the whole
    dashboard down. A half-written file is exactly what this is here to survive."""
    total = 0
    entries = base / "ledger" / "entries"
    if not entries.exists():
        return 0
    for path in sorted(entries.glob("*.jsonl")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        total += sum(1 for line in text.splitlines() if line.strip())
    return total


USAGE = "usage: status.py [sdlc_dir]"


def _coexist_warning(sdlc_dir):
    """#240/#314: the one notice line on stderr when the plugin under the previous name is also
    active here (Sigma proceeds; `SIGMA_ALLOW_COEXIST=1` silences it). Fail-open like every
    cross-load in this file."""
    try:
        spec = importlib.util.spec_from_file_location(
            "coexist", _HERE.parent.parent / "sigma-loop" / "scripts" / "coexist.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m.warn_line(sdlc_dir)
    except Exception:
        return None


def _awaiting_merge_segment(sdlc_dir, now=None):
    """#255 LIVENESS: `awaiting merge: N (oldest <goal> PR #n for 3d 04h, last PR read 2m ago)` --
    goals whose PR is open and not merged yet, with the age that tells a stuck wait from a fresh
    one. "" when nothing waits (the line stays as it was). Local reads only; fail-open."""
    try:
        spec = importlib.util.spec_from_file_location(
            "work", _HERE.parent.parent / "sigma-loop" / "scripts" / "work.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m.awaiting_merge_line(sdlc_dir, now=now)
    except Exception:
        return ""


def _legacy_delta_segment(sdlc_dir):
    """#327 LIVENESS: `legacy delta records: N (M would be refused): <repair command>` -- unit
    records the previous plugin wrote after the conversion, which Sigma merges but never applies.
    "" when there are none (the line stays as it was). Local reads only; fail-open."""
    try:
        spec = importlib.util.spec_from_file_location(
            "feature_sync", _HERE.parent.parent / "sigma-loop" / "scripts" / "feature_sync.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m.legacy_delta_segment(sdlc_dir)
    except Exception:
        return ""


def main(argv):
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    warning = _coexist_warning(argv[1] if len(argv) > 1 else ".sdlc")
    if warning:
        print(warning, file=sys.stderr)
    s = summary(argv[1] if len(argv) > 1 else ".sdlc")
    legacy = f"{s['proposed']} legacy-proposed, " if s["proposed"] else ""   # leftovers only
    line = (f"backlog: {legacy}{s['pending']} pending, {s['in_progress']} in-progress, "
            f"{s['done']} done, {s['parked']} parked, {s['failed']} failed | "
            f"iteration {s['iteration']} | "
            f"review-queue: {'NEEDS ATTENTION' if s['queue_nonempty'] else 'empty'}")
    if s["ledger_entries"]:                    # silent when the ledger is off: same line as before
        line += f" | ledger: {s['ledger_entries']} entries"
    if (s["goals_since_align"] or 0) >= ALIGN_EVERY:   # silent without a north-star, or when not due
        line += f" | alignment check due ({s['goals_since_align']} goals since the last): /sigma-align"
    if (s["goals_since_audit"] or 0) >= AUDIT_EVERY:   # silent until the slower audit cadence is hit
        line += f" | repo audit due ({s['goals_since_audit']} goals since the last): /sigma-audit"
    if s.get("merge_queue_advice"):             # #976: silent unless the #408 shape is actually present
        line += f" | {s['merge_queue_advice']}"
    awaiting = _awaiting_merge_segment(argv[1] if len(argv) > 1 else ".sdlc")
    if awaiting:                                # #255: silent unless a goal is awaiting a merge
        line += f" | {awaiting}"
    heartbeat = _loop_heartbeat_segment(argv[1] if len(argv) > 1 else ".sdlc")
    if heartbeat:
        line += f" | {heartbeat}"
    deltas = _legacy_delta_segment(argv[1] if len(argv) > 1 else ".sdlc")
    if deltas:                                  # #327: silent unless a legacy delta record is live
        line += f" | {deltas}"
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
