"""Local board mirror — a token-free, gitignored snapshot of the GitHub backlog for the backlog
cross-check (0.9.20). ONE batched `gh issue list` for OPEN `sdlc:goal` issues + ONE for recently-closed
issues, normalized to the fields the cross-check index needs, written as NDJSON under
`.sdlc/state/board-mirror.ndjson` (which `/sigma-setup` gitignores — issue titles/bodies can carry client
strings, so the mirror never rides a PR). Body excerpts are scrubbed too (defense in depth).

ONE field is derived from the FULL body rather than the 500-character excerpt: `blocker_refs`
(#1487). A blocker scan needs an EXACT reference, not a representative sample, and the markers
are written at the END of a body -- see `normalize_issue`. Only the issue number and the
trigger phrase are kept, never surrounding text.

Read-only against GitHub; reaches it only through an injectable `run` (default `sources._run_gh`), so
tests are hermetic — no network, no `gh`. FAIL-OPEN: not github mode / no `gh` / offline / any error =>
no mirror written, returns None; the cross-check then degrades to the ledger + local goal files.

A well-formed but EMPTY result (#1496) is treated the same way — not as an answer. A rate-limited or
scope-reduced `gh` can succeed with zero issues, and that is indistinguishable on the wire from a
board that genuinely has none; writing it would clobber a good, populated mirror with nothing, and a
cleared mirror reads to the cross-check as "checked, found nothing" rather than "could not check".
See `fetch_and_write`.

    python3 pipeline.py mirror .sdlc [--force]      # via the pipeline verb, or run this module directly
"""
import hashlib, importlib.util, json, pathlib, sys, time

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


scrub = _load("scrub").scrub
blocker_scan = _load("blocker_scan")   # #1487: the ONE blocker vocabulary, shared with
                                       # backlog_check.py (which imports THIS module, so the
                                       # vocabulary cannot live there -- see blocker_scan.py)

#: v2 (#1487): records gained `blocker_refs`. The version is part of the meta file and
#: `is_fresh` refuses a mirror whose meta records a DIFFERENT one, so a v1 mirror left on disk
#: by a pre-#1487 install is refetched on the next pick instead of being served from the TTL
#: cache -- a v1 record has no `blocker_refs` at all, and silently gating on nothing for up to
#: a full TTL is the exact failure this issue exists to close.
SCHEMA = "board-mirror/v2"
MIRROR_REL = "state/board-mirror.ndjson"          # under state/ => covered by RUNTIME_IGNORES (gitignored)
META_REL = "state/board-mirror.meta.json"
_EXCERPT_CHARS = 500                              # enough for an optional rerank; never the raw body
                                                  # -- and NEVER the text a blocker scan reads: see
                                                  # `blocker_refs` in `normalize_issue` (#1487)
_OPEN_LIMIT = 200                                 # mirrors next_pending's 200-issue cap
_DEFAULT_CLOSED_LIMIT = 200
_DEFAULT_TTL_MINUTES = 60


def _content_hash(title, excerpt, updated_at):
    """Change-detection key for the incremental index (slice 0.9.21): identical stored content +
    updated_at => identical hash => the engine skips re-tokenizing/re-embedding that issue.

    `blocker_refs` (#1487) is deliberately NOT an input, and the honest reason is not the one an
    earlier draft of this comment gave. Two facts, both checked rather than assumed:

      - NOTHING under `skills/` reads `content_hash` today. It is written here and asserted in
        `tests/test_mirror.py`; the embedding cache that the "skips re-embedding" sentence above
        describes keys on `d["raw"]` instead (`backlog_check._dense_channel`), i.e. on
        `title + "\n" + body_excerpt`. So this is a decision about the shape a FUTURE consumer
        would inherit, not one with a live consumer behind it.
      - For that future consumer the inputs should be exactly what feeds the work being skipped,
        and `blocker_refs` feeds neither tokenizing nor embedding -- it is read by
        `backlog_check._record_blocker_refs` alone. Including it would move the key on records
        whose tokenized content is unchanged.

    The one case where `blocker_refs` can change while title/excerpt/`updated_at` all hold still is
    a change to the EXTRACTION rule itself (this release narrows it). That is a whole-mirror
    concern, not a per-record one, and `SCHEMA`/`is_fresh` already handle it by refusing the entire
    stale mirror -- which is strictly stronger than a per-record hash miss would be."""
    return hashlib.sha256("\x00".join((title or "", excerpt or "", updated_at or "")).encode("utf-8")).hexdigest()[:16]


def normalize_issue(raw):
    """Pure: one `gh issue list --json` object -> the normalized, scrubbed mirror record. Raises
    (TypeError/ValueError) on a row with no usable number, so build_records can skip it.

    `blocker_refs` (#1487) is scanned off the FULL title+body, not the excerpt. The excerpt cap is
    right for what it was sized for -- "enough for an optional rerank" -- and similarity scoring
    degrades gracefully on a truncated body. A regex for a SPECIFIC `#N` reference does not degrade;
    it just returns nothing. Since `compile_plan` appends `**Blocked by:** #N` at the END of a body,
    every issue longer than 500 characters had its dependency silently unenforced (measured: a real
    18-issue plan where `next-batch` claimed 8 goals, 7 with an open prerequisite). Scanning once
    here costs nothing at check time and keeps the excerpt at the size it was chosen for.

    The scan is NARROWER than the check-time one, deliberately: `blocker_scan.extract_refs` uses
    `EXPLICIT_TRIGGERS` (`blocked by` / `depends on` / `depends upon`), not the full `TRIGGERS` set.
    The weak triggers were calibrated against the 500-character excerpt and 395 of 400 real issues
    exceed it, so pointing them at whole bodies emitted a confident `blocked-by` edge off narrative
    prose ("...a real user hit it after Phase 2 (#1396) went live") -- and a confident edge is not
    advisory, it drives `blockers.resolve` to WRITE to the referenced issue. Measured on this
    repo's own board the narrowing keeps 21 of 21 genuine edges and drops the phantom. See
    `blocker_scan.TRIGGERS` for the full rationale.

    What lands on the record is a decimal issue number plus one phrase from that closed trigger set
    -- never surrounding body text -- so this reads MORE of the body than the mirror stores without
    weakening the secret-safety the excerpt cap and `scrub()` provide. Note the ORDER this depends
    on: `body` is scrubbed BEFORE it is scanned, so the new path never sees unredacted text.

    Deliberately NOT folded into `content_hash` -- see that function's own note for why.

    #1833: `updated_at`/`closed_at` read EITHER key spelling -- `gh issue list --json` normalizes
    these to camelCase (`updatedAt`/`closedAt`, GraphQL-shaped), while `gh api`'s raw REST issues
    endpoint (what `fetch_and_write`/`fetch_dependency_records` now fetch through) returns them
    snake_case (`updated_at`/`closed_at`) instead -- confirmed live, the same naming split
    `_sync_backlog`'s own REST migration ran into with `title`/`body`/`number`/`labels` (those four
    happen to be spelled identically in both shapes, so needed no such fallback). Camelcase is
    tried FIRST so every existing camelCase-fixtured test in this file keeps passing unchanged;
    REST's raw response simply never has that key, so the fallback engages for it and only it."""
    number = int(raw.get("number"))               # raises on missing/garbage -> row dropped upstream
    title = scrub(str(raw.get("title") or ""))
    body = scrub(str(raw.get("body") or ""))
    excerpt = body[:_EXCERPT_CHARS].strip()
    labels = [l.get("name") for l in (raw.get("labels") or [])
              if isinstance(l, dict) and l.get("name")]
    updated_at = raw.get("updatedAt") or raw.get("updated_at") or ""
    return {"number": number,
            "title": title,
            "body_excerpt": excerpt,
            "blocker_refs": blocker_scan.extract_refs(title + "\n" + body, self_ref=number),
            "labels": labels,
            "state": str(raw.get("state") or "").lower(),
            "closed_at": raw.get("closedAt") or raw.get("closed_at") or None,
            "updated_at": updated_at,
            "content_hash": _content_hash(title, excerpt, updated_at)}


def build_records(open_raw, closed_raw):
    """Pure: raw open + closed issue lists -> normalized records, de-duplicated by number (an OPEN
    record wins over a closed one on the rare clash), sorted by number for deterministic output."""
    by_num = {}
    for raw in list(closed_raw or []) + list(open_raw or []):   # open appended last => wins the clash
        try:
            rec = normalize_issue(raw)
        except (TypeError, ValueError, AttributeError):
            continue                        # a non-dict / number-less row (str/None -> AttributeError on
        by_num[rec["number"]] = rec         # .get): skip it, keep the rest
    return [by_num[n] for n in sorted(by_num)]


def _load_config(sdlc_dir):
    try:
        return json.loads((pathlib.Path(sdlc_dir) / "config.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def is_github_mode(config):
    return ((config.get("discovery") or {}).get("source")) == "github"


def _paths(sdlc_dir):
    base = pathlib.Path(sdlc_dir)
    return base / MIRROR_REL, base / META_REL


def is_fresh(sdlc_dir, ttl_minutes, now=None):
    """True if a mirror exists, was written by THIS schema version, and is younger than the TTL —
    the loop then skips the refetch.

    The schema check (#1487) is what makes a record-shape change safe to ship. A mirror written by
    an older version is not "stale" by age — it can be minutes old — but it is missing a field the
    current reader depends on, and `read_mirror` degrades per-record rather than raising, so the
    gap is silent. Refusing it here forces exactly ONE refetch on upgrade, after which the TTL
    behaves normally. A meta file with no `schema` key at all (the oldest shape) is refused too."""
    _, meta_path = _paths(sdlc_dir)
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("schema") != SCHEMA:
            return False
        age = (time.time() if now is None else now) - float(meta.get("mirrored_at", 0))
        return 0 <= age < ttl_minutes * 60
    except Exception:
        return False


def age_seconds(sdlc_dir, now=None):
    """How long ago the mirror on disk was written, in seconds — or None when no meta file is
    readable (and None, not a huge number, for a meta file whose timestamp is in the future).

    The same number `is_fresh` compares against a TTL, exposed so a caller can SAY it. #1650: the
    pick gate declines to claim a goal on the strength of a mirror record, and when it cannot
    refresh that record before deciding, the age of the evidence is the difference between a
    reasoned hold and what an operator reads as a stall."""
    _, meta_path = _paths(sdlc_dir)
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        age = (time.time() if now is None else now) - float(meta.get("mirrored_at", 0))
        return age if age >= 0 else None
    except Exception:
        return None


def read_mirror(sdlc_dir):
    """Read the mirror back as a list of records (empty on absent/garbage). For the cross-check engine.
    Fail-open per line: a corrupt line is skipped, the rest survive."""
    mirror_path, _ = _paths(sdlc_dir)
    out = []
    try:
        text = mirror_path.read_text(encoding="utf-8")
    except Exception:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _write(sdlc_dir, records, repo, now=None):
    mirror_path, meta_path = _paths(sdlc_dir)
    mirror_path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records)
    mirror_path.write_text(body, encoding="utf-8")
    meta = {"schema": SCHEMA, "repo": repo, "count": len(records),
            "mirrored_at": (time.time() if now is None else now)}
    meta_path.write_text(json.dumps(meta, sort_keys=True), encoding="utf-8")


def _list_only_run(run):
    """#1833: wrap `run` so a malformed (valid-JSON-but-not-a-list) issues-listing response RAISES
    instead of silently degrading to an empty page inside `sources.fetch_issues_rest` -- that
    helper cannot otherwise tell "genuinely zero issues" from "gh returned an error object"
    (`{"message": "Not Found"}`, a real, live-observed shape from a bad repo/token) the way the
    old `isinstance(..., list)` guard in `fetch_and_write` used to. `fetch_and_write`'s own outer
    `except Exception: return None` turns this raise into the exact same "don't clobber a good
    mirror with junk" fail-open it always had (see `test_fetch_fail_open_on_non_list_gh_payload_
    does_not_clobber`). Deliberately NOT used for `resolve_assignee_login`'s own `gh api user`
    call, whose response is a bare login string, never JSON -- only for an issues-listing fetch."""
    def wrapped(args):
        raw = run(args)
        if not isinstance(json.loads(raw or "[]"), list):
            raise ValueError("gh returned a non-list issues payload")
        return raw
    return wrapped


def fetch_and_write(sdlc_dir, config=None, run=None, now=None, force=False):
    """FAIL-OPEN orchestrator. Returns the record count written, or None when NO mirror was produced —
    not github mode / fresh-and-not-forced / gh missing / offline / bad config / a well-formed fetch
    that came back with zero issues (#1496 — the existing mirror and its timestamp are left exactly as
    they were, so the next pass retries instead of caching the emptiness for a full TTL) / unwritable
    .sdlc / ANY error. The whole body is guarded: this is a read-only pre-flight, so it must never
    raise into the loop's pick path (a later slice calls it there). A None return means "no mirror
    this run", never a crash.

    #1833: both fetches migrated off `gh issue list --label ... --search sort:X` onto the shared
    `sources.fetch_issues_rest` REST helper the pick path (`_fetch_pending`/`list_needs_label`/
    `_sync_backlog`) already uses -- the open query's `--label` made it graphql-search-billed by
    the same mechanism (confirmed live), and the closed query's bare `--search` (no label at all)
    is ALSO always graphql-search-billed, by construction (`--search` literally routes `gh`
    through the `search()` field) -- #1829's own research named both as sharing the defect but out
    of its own scope, being off the `next`/`next-batch` pick path. `state`/`sort`/`direction` are
    REST parameters native to the endpoint, so no `--search "sort:X"` qualifier is needed for
    either query. `assignee` (which may be the kit's own documented `"@me"` convention) is
    resolved to a real login via `resolve_assignee_login` before the open query: REST's
    `assignee=` has no `"@me"` alias of its own (a hard 422, confirmed live by #1829's own
    research), unlike the old `--assignee @me`, which `gh` resolved server-side at no extra cost."""
    try:
        config = config if config is not None else _load_config(sdlc_dir)
        if not is_github_mode(config):
            return None                                   # local-files mode: the goals dir IS the corpus
        gh = (config.get("discovery") or {}).get("github") or {}
        mirror_cfg = ((config.get("backlog_check") or {}).get("mirror")) or {}
        ttl = mirror_cfg.get("ttl_minutes", _DEFAULT_TTL_MINUTES)
        if not force and is_fresh(sdlc_dir, ttl, now=now):
            return None                                   # a fresh mirror amortizes the one API call
        sources_mod = _load("sources")
        run = run or sources_mod._run_gh
        repo = gh.get("repo") or ""
        goal_label = gh.get("goal_label", "sdlc:goal")
        assignee = gh.get("assignee") or None
        closed_limit = int(mirror_cfg.get("closed_limit", _DEFAULT_CLOSED_LIMIT))   # bad value -> caught below
        strict_run = _list_only_run(run)
        login = sources_mod.resolve_assignee_login(run, assignee) if assignee else None
        # The open query does NOT apply next_pending's extra parked-label exclusion. In practice `park`
        # also strips the goal label, so most parked issues fall out of this --label query anyway; one
        # that is parked-but-still-goal-labelled stays in the corpus as a valid dedup candidate.
        # OLDEST-first for the same reason next_pending carries it (F12/#348): a bare `gh issue list`
        # (or an unsorted REST fetch) is created-DESC, so the `_OPEN_LIMIT` cap would take the NEWEST
        # 200 while next_pending picks from the OLDEST. Over the cap those two sets are disjoint — the
        # goal just picked would be missing from its own corpus, and cross_check would degrade to
        # `goal_not_in_corpus` for exactly the goals that had waited longest.
        open_raw = sources_mod.fetch_issues_rest(strict_run, repo, [goal_label], _OPEN_LIMIT,
                                                  assignee=login)
        # Closed issues are NOT goal-filtered: work completed in a prior (often manual) session can
        # obsolete an open goal even if it never carried the goal label. Bounded by closed_limit;
        # `sort=updated&direction=desc` yields most-recently-updated-first (smoke-tested against real
        # gh — `state=closed` alone does NOT sort by recency). The engine applies the window later.
        closed_raw = sources_mod.fetch_issues_rest(strict_run, repo, [], closed_limit,
                                                    state="closed", sort="updated", direction="desc")
        records = build_records(open_raw, closed_raw)
        if not records:
            # #1496: the isinstance guard above catches a `gh` ERROR (a dict, not a list) but a
            # well-formed EMPTY success sails right past it — and a rate-limited or scope-reduced
            # `gh` can succeed with nothing. Writing this would overwrite a good, populated mirror
            # with zero records, and `backlog_check` cannot tell that apart from "checked, this repo
            # really has none" — every dependency/duplicate check then passes on no evidence at all.
            # So: keep whatever is already on disk (nothing, if this is the first fetch) and leave
            # the meta timestamp untouched too, so `is_fresh` stays false and the NEXT pass retries
            # instead of caching the emptiness for a full TTL window. A genuine error above already
            # takes the same "leave it alone" path; this is that same posture for a second shape of
            # non-answer.
            print("mirror: fetch returned zero issues — keeping the mirror already on disk "
                  "instead of overwriting it with nothing (retrying next pass)", file=sys.stderr)
            return None
        _write(sdlc_dir, records, repo, now=now)
        return len(records)
    except Exception:
        return None                                       # fail-open backstop: no mirror, never a raise


def fetch_dependency_records(sdlc_dir, config=None, run=None, labels=(), open_limit=_OPEN_LIMIT,
                              closed_limit=_DEFAULT_CLOSED_LIMIT):
    """A SEPARATE, un-cached corpus fetch for handoff.create_tracked_issue's file-time duplicate
    search (#1204) -- deliberately NOT `fetch_and_write`'s own open query above, and never reads or
    writes the shared `board-mirror.ndjson` that query maintains.

    Two corpus gaps make the regular mirror unusable for this: (1) its open query applies
    `discovery.github.assignee` when configured (`fetch_and_write`, above) -- a follow-up filed by a
    different person/session is invisible to a search that reuses it unchanged, exactly the blind
    spot #1204 exists to close; (2) it filters open issues to ONE label (`discovery.github.
    goal_label`, default `sdlc:goal`) -- a queued follow-up (the legacy confirmation label, withholding
    `sdlc:goal` by design, see handoff.py's PROPOSED_LABEL) never matches it, so follow-ups could
    never dedup against each other. This function fixes both: no assignee filter ever, and one
    `gh issue list --label <L>` call PER label in `labels` (issued separately and merged rather than
    combined into one `--label a,b` query, which `gh` ANDs rather than ORs -- a caller wants "carries
    EITHER label", not "carries both").

    The closed-issue side is unchanged from `fetch_and_write`'s own: no label filter (completed work
    can obsolete/duplicate a follow-up even if it never carried a goal label), no assignee filter
    (already the case above).

    Always live, no TTL cache -- this runs only at the moment the loop is about to file a brand-new
    issue, never on the hot pick path, so one extra `gh` round-trip here is cheap next to the cost of
    a genuine duplicate going unnoticed.

    Returns normalized records in the exact shape `build_records` produces (deduplicated by number,
    an open record winning over a closed one on a clash) -- so a caller (dedup.find_candidates via
    `records=`) can treat this corpus identically to the regular mirror's, whichever path produced
    it. FAIL-OPEN: not github mode / no gh / any error -> None, never a raise -- the caller degrades
    to filing without a duplicate check, exactly as it did before this existed.

    #1833: migrated off `gh issue list --label <L> --search sort:X` (both the per-label open loop
    and the closed query) onto the same shared `sources.fetch_issues_rest` REST helper
    `fetch_and_write` above now uses -- same graphql-search-billing defect, same fix. Unlike that
    function, no `_list_only_run` wrapper is needed here: a malformed per-label or closed response
    degrading to an empty list (`fetch_issues_rest`'s own behavior on a non-list JSON payload) is
    ALREADY this function's own pre-#1833 contract for both (`if isinstance(raw, list):
    open_raw.extend(raw)` skipped a bad label's contribution; `if not isinstance(closed_raw,
    list): closed_raw = []` did the same for closed) -- there is no "don't clobber a good mirror"
    property to preserve here, since this function never writes anything to begin with."""
    try:
        config = config if config is not None else _load_config(sdlc_dir)
        if not is_github_mode(config):
            return None
        gh = (config.get("discovery") or {}).get("github") or {}
        repo = gh.get("repo") or ""
        sources_mod = _load("sources")
        run = run or sources_mod._run_gh

        open_raw = []
        for label in dict.fromkeys(l for l in labels if l):    # de-dup labels, preserve order
            open_raw.extend(sources_mod.fetch_issues_rest(run, repo, [label], open_limit))

        closed_raw = sources_mod.fetch_issues_rest(run, repo, [], int(closed_limit),
                                                    state="closed", sort="updated", direction="desc")

        return build_records(open_raw, closed_raw)
    except Exception:
        return None


USAGE = "usage: mirror.py [sdlc_dir] [--force]"


def main(argv):
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    sdlc_dir = argv[1] if len(argv) > 1 else ".sdlc"
    n = fetch_and_write(sdlc_dir, force=("--force" in argv))
    if n is None:
        print("mirror: skipped (not github mode, fresh, or gh unavailable)")
    else:
        print(f"mirror: wrote {n} issue(s) to {sdlc_dir}/{MIRROR_REL}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv))
