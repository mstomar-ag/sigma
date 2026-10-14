"""Pre-work backlog cross-check engine (backlog_check.py, slice 0.9.21): LLM-free TF-IDF retrieval +
explicit `#N` graph + ledger signals over the board mirror / local goals. Hermetic, deterministic, $0."""
import hashlib, json, pathlib, importlib.util, tempfile, calendar, time, subprocess
import gqlfake

from skill_corpus import skill_corpus

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


# #462: reuse test_handoff.py's own FakeSource double (pytest's no-__init__.py "rootdir" import mode
# makes every tests/*.py module importable by its bare filename) rather than a second, divergent copy
# -- the same hardened-sibling-divergence this whole plan item exists to avoid one layer up.
from test_handoff import FakeSource


def _epoch(iso):  # "2026-08-03T00:00:00Z" -> epoch seconds
    return calendar.timegm(time.strptime(iso.replace("Z", "GMT"), "%Y-%m-%dT%H:%M:%S%Z"))


def _rec(number, title, body="", state="open", closed_at=None, updated="2026-08-01T00:00:00Z"):
    return {"number": number, "title": title, "body_excerpt": body, "labels": [],
            "state": state, "closed_at": closed_at, "updated_at": updated, "content_hash": "x"}


def _gh_base(d, records, ledger=None, **bc):
    base = pathlib.Path(d) / ".sdlc"; (base / "state").mkdir(parents=True)
    cfg = {"discovery": {"source": "github"}, "backlog_check": bc}
    if ledger is not None:
        cfg["ledger"] = ledger              # the claim-lease TTL lives here: ledger.lease.ttl_hours
    (base / "config.json").write_text(json.dumps(cfg))
    (base / "state" / "board-mirror.ndjson").write_text("".join(json.dumps(r) + "\n" for r in records))
    return str(base)


# clearly-similar pair (shared rare terms) vs a disjoint issue — neutral fixture, no real stack names
_GOAL = "migrate the widget cache onto the acme storage backend"
_DUP = "move the widget cache to acme storage"
_DISTINCT = "restyle the frontend dashboard button colours"
_LOOSE = {"dup_threshold": 0.4, "obsolete_threshold": 0.4, "closed_window_days": 3650}


# --- tokenization + similarity primitives ---

def test_tokens_lowercase_drop_stopwords_and_short():
    bc = _mod("backlog_check")
    toks = bc._tokens("The Widget Cache is a BIG win, ok")
    assert "widget" in toks and "cache" in toks and "big" in toks
    assert "the" not in toks and "is" not in toks and "a" not in toks   # stopwords gone


def test_cosine_identical_high_disjoint_zero():
    bc = _mod("backlog_check")
    idf = bc._idf([{"tokens": bc._doc_tokens(_GOAL, "")}, {"tokens": bc._doc_tokens(_DISTINCT, "")}])
    a = bc._vector(bc._doc_tokens(_GOAL, ""), idf)
    assert bc._cosine(a, a) > 0.99
    b = bc._vector(bc._doc_tokens(_DISTINCT, ""), idf)
    assert bc._cosine(a, b) == 0.0


# --- #1533: kit-generated issue-template boilerplate must not itself read as similarity ------------
# `handoff.issue_body()` / `handoff._tracked_issue_body()` are the two templates every issue the kit
# opens on its own behalf is filed with; both bodies below are the REAL functions' own output, not a
# hand-typed guess at their prose -- so a future wording change there that the strip in
# `backlog_check._TEMPLATE_STRIP_PATTERNS` falls out of step with re-fails these, per that module's
# own docstring.

def _tracked_issue_corpus(pairs, blocks_goal=True):
    """[(title, body), ...] rendered from `handoff._tracked_issue_body`, one per (goal, area, why),
    with the SAME default title `create_tracked_issue()` falls back to when no caller supplies one
    -- the heaviest single boilerplate contributor (title terms count 3x)."""
    handoff = _mod("handoff")
    out = []
    for goal, area, why in pairs:
        body = handoff._tracked_issue_body(goal, area, why, blocks_goal)
        title = f"[{area}] dependency from {goal}"
        out.append((title, body))
    return out


def test_tracked_issue_boilerplate_alone_scores_below_the_advisory_line():
    # The worst realistic case named in #1533: an overnight batch files several UNRELATED follow-ups
    # from the SAME goal to the SAME area, so every fixed line of the template lines up across the
    # pair, and only the free-text `why` differs. Before the fix this scored 0.93 -- comfortably past
    # even `park_threshold` (0.80) -- on shared scaffolding alone.
    bc = _mod("backlog_check")
    (title_a, body_a), (title_b, body_b) = _tracked_issue_corpus([
        ("2871", "area:loop", "cap the embedder retry count"),
        ("2871", "area:loop", "surface blame in review output"),
    ])
    docs = [{"tokens": bc._doc_tokens(title_a, body_a)}, {"tokens": bc._doc_tokens(title_b, body_b)}]
    idf = bc._idf(docs)
    score = bc._cosine(bc._vector(docs[0]["tokens"], idf), bc._vector(docs[1]["tokens"], idf))
    assert score < 0.45, f"boilerplate-only pair scored {score} -- not below the advisory line"
    # none of the shared scaffolding vocabulary survives into either doc's own token set
    boilerplate_terms = {"raised", "automatically", "sdlc", "reply", "scoped", "declined", "parked"}
    assert not (boilerplate_terms & set(docs[0]["tokens"]))
    assert not (boilerplate_terms & set(docs[1]["tokens"]))


def test_genuine_duplicate_from_real_templates_still_parks():
    # Pinned in the OTHER direction (#1533's own second "done when" bullet): a fix that only lowers
    # every score would pass the test above by breaking real duplicate detection instead. Two issues
    # filed from the SAME template with the SAME `why`, but from DIFFERENT goals/areas (independently
    # discovered, exactly how a real duplicate would arrive) must still park at the shipped defaults
    # -- no threshold override here, unlike the other tests in this file.
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        (title_a, body_a), (title_b, body_b) = _tracked_issue_corpus([
            ("4001", "area:loop", "cap the embedder retry count"),
            ("4002", "area:review", "cap the embedder retry count"),
        ])
        base = _gh_base(d, [_rec(1, title_a, body_a), _rec(2, title_b, body_b)])   # real defaults
        pack = bc.cross_check(base, "2")
        f = next((x for x in pack["findings"] if x["kind"] == "duplicate" and x["ref"] == "1"), None)
        assert f is not None and f["confident"] is True, pack


# --- duplicate detection (github corpus) ---

def test_duplicate_open_issue_flagged_distinct_not():
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, _GOAL), _rec(2, _DUP), _rec(3, _DISTINCT)], **_LOOSE)
        pack = bc.cross_check(base, "1")
        kinds = {(f["kind"], f["ref"]) for f in pack["findings"]}
        assert ("duplicate", "2") in kinds          # the paraphrase is caught
        assert ("duplicate", "3") not in kinds       # the unrelated issue is not
        dup = next(f for f in pack["findings"] if f["ref"] == "2")
        assert dup["score"] >= 0.4 and dup["evidence"]   # carries shared-term evidence
        assert pack["schema"] == "backlog-check/v1" and pack["goal"] == "1"


def test_duplicate_pair_only_the_later_goal_is_park_confident():
    # both #1 and #2 are open duplicates: the EARLIER (#1) survives + is worked; only the LATER (#2)
    # parks. Without this, #1 parks as dup-of-#2 AND #2 parks as dup-of-#1 -> neither ever gets worked.
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, _GOAL), _rec(2, _GOAL)],
                        dup_threshold=0.4, park_threshold=0.8, closed_window_days=3650)
        f1 = next(f for f in bc.cross_check(base, "1")["findings"] if f["ref"] == "2")
        assert f1["kind"] == "duplicate" and f1["confident"] is False   # #1 is earliest -> survives
        f2 = next(f for f in bc.cross_check(base, "2")["findings"] if f["ref"] == "1")
        assert f2["confident"] is True                                  # #2 is later -> parks against #1


def test_earlier_orders_github_numbers_and_local_paths():
    bc = _mod("backlog_check")
    assert bc._earlier("2", "10") is True and bc._earlier("10", "2") is False   # numeric, not lexical
    assert bc._earlier("/a/goals/0001.md", "/b/goals/0002.md") is True          # local: by filename


def test_larger_duplicate_cluster_keeps_earliest_in_top_k_window():
    # a cluster BIGGER than top_k (8) with mixed 1-/2-digit numbers: a lexical candidate tiebreak would
    # sort 10..17 before 2 and crowd #2 out of #3's window, leaving #3 a wrong SECOND survivor. The
    # numeric-aware _ref_key keeps #2 in-window, so #3 parks against the true earliest.
    bc = _mod("backlog_check")
    nums = [2, 3, 10, 11, 12, 13, 14, 15, 16, 17]
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(n, _GOAL) for n in nums],
                        dup_threshold=0.4, park_threshold=0.8, closed_window_days=3650)
        f = [x for x in bc.cross_check(base, "3")["findings"] if x["ref"] == "2"]
        assert f and f[0]["confident"] is True          # #3 parks against the earlier #2, not survives


def test_confidence_gate_park_vs_annotate():
    bc = _mod("backlog_check")
    # cross-check from the LATER goal (#2) so the earlier dup (#1) is the park-confident match
    with tempfile.TemporaryDirectory() as d1:
        # identical -> score ~1.0 >= park_threshold 0.9 -> confident (the loop would park)
        base = _gh_base(d1, [_rec(1, _GOAL), _rec(2, _GOAL)],
                        dup_threshold=0.4, park_threshold=0.9, closed_window_days=3650)
        assert next(f for f in bc.cross_check(base, "2")["findings"] if f["ref"] == "1")["confident"] is True
    with tempfile.TemporaryDirectory() as d2:
        # same score, but an unreachable park_threshold -> a finding, NOT confident (annotate + proceed)
        base = _gh_base(d2, [_rec(1, _GOAL), _rec(2, _GOAL)],
                        dup_threshold=0.4, park_threshold=1.01, closed_window_days=3650)
        assert next(f for f in bc.cross_check(base, "2")["findings"] if f["ref"] == "1")["confident"] is False


# --- obsolescence (closed within the window) ---

def test_obsoleted_by_recent_close_but_not_stale_close():
    bc = _mod("backlog_check")
    now = _epoch("2026-08-03T00:00:00Z")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, _GOAL),
                            _rec(2, _DUP, state="closed", closed_at="2026-08-01T00:00:00Z")],
                        dup_threshold=0.4, obsolete_threshold=0.4, closed_window_days=30)
        assert ("obsoleted-by", "2") in {(f["kind"], f["ref"]) for f in bc.cross_check(base, "1", now=now)["findings"]}
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, _GOAL),
                            _rec(2, _DUP, state="closed", closed_at="2026-01-01T00:00:00Z")],
                        dup_threshold=0.4, obsolete_threshold=0.4, closed_window_days=30)
        assert ("obsoleted-by", "2") not in {(f["kind"], f["ref"]) for f in bc.cross_check(base, "1", now=now)["findings"]}


def test_within_window_helper():
    bc = _mod("backlog_check")
    now = _epoch("2026-08-03T00:00:00Z")
    assert bc._within_window({"closed_at": "2026-08-01T00:00:00Z"}, 30, now) is True
    assert bc._within_window({"closed_at": "2026-01-01T00:00:00Z"}, 30, now) is False
    assert bc._within_window({"closed_at": None}, 30, now) is True        # local done: no date -> keep
    assert bc._within_window({"closed_at": "garbage"}, 30, now) is True   # unparseable -> keep


# --- explicit blocker graph ---

def test_explicit_blocked_by_open_ref_only():
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, "wire the surface", body="blocked by #7 until the contract lands"),
                            _rec(7, "freeze the contract"),
                            _rec(9, "closed dep", state="closed")], **_LOOSE)
        f = [x for x in bc.cross_check(base, "1")["findings"] if x["kind"] == "blocked-by"]
        assert any(x["ref"] == "7" and x["confident"] for x in f)    # open ref -> confident blocker
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, "x", body="depends on #9"), _rec(9, "done dep", state="closed")], **_LOOSE)
        assert not [x for x in bc.cross_check(base, "1")["findings"] if x["kind"] == "blocked-by"]  # closed ref: not a blocker


# --- #389: bounded, scrubbed comment-reading fallback -- a human-authored dependency marker left
# ONLY as a GitHub comment (never the body, so mirror.py's title+body-only fetch never sees it) still
# auto-skips, via a fetch scoped to the ONE goal being considered (never corpus-wide).

def _comment_runner(comments_by_issue):
    """Fake gh runner keyed by the issue number in `issue view <n> ...`: answers with the canned
    comments for that issue (default: none). Records every call, mirroring test_sources.py's own
    _recording_runner shape, scoped to this file's single-goal comment-fetch call."""
    calls = []

    def run(args):
        calls.append(list(args))
        rest = gqlfake.rest_issue(args, lambda n, f: {"comments": comments_by_issue.get(n, [])})   # #895
        if rest is not None:
            return rest
        n = args[2] if len(args) > 2 else None
        return json.dumps({"comments": comments_by_issue.get(n, [])})

    run.calls = calls
    return run


def _raising_runner():
    calls = []

    def run(args):
        calls.append(list(args))
        raise RuntimeError("gh: HTTP 502 Bad Gateway")

    run.calls = calls
    return run


def _gh_comment(body, author="someone", cid="IC_1", created="2026-08-01T00:00:00Z"):
    return {"id": cid, "author": {"login": author}, "body": body, "createdAt": created,
            "authorAssociation": "OWNER"}


def test_explicit_blocked_by_comment_only_no_body_marker():
    # the acceptance criterion, non-vacuous: NO blocker phrase anywhere in the goal's body -- only in
    # a comment `precheck()` would otherwise never see (mirror.py's own corpus fetch is title+body
    # only, by design).
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, "wire the surface"), _rec(7, "freeze the contract")], **_LOOSE)
        run = _comment_runner({"1": [_gh_comment("blocked by #7 until the contract lands")]})
        f = [x for x in bc.cross_check(base, "1", run=run)["findings"] if x["kind"] == "blocked-by"]
        assert any(x["ref"] == "7" and x["confident"] for x in f)
        # the comment-fetch actually ran this call -- fails on today's code, which never calls `run`
        # for comments at all, so this would be an empty list before the fix.
        assert any("comments" in " ".join(c) for c in run.calls)


def test_explicit_blocked_by_comment_closed_ref_is_not_a_blocker():
    # same shape, but #7 is closed -- proves the comment path reuses the SAME open-ref precision
    # guard _explicit_blockers already applies to the body path, not a looser one.
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, "wire the surface"), _rec(7, "freeze the contract", state="closed")], **_LOOSE)
        run = _comment_runner({"1": [_gh_comment("blocked by #7 until the contract lands")]})
        f = [x for x in bc.cross_check(base, "1", run=run)["findings"] if x["kind"] == "blocked-by"]
        assert not any(x["ref"] == "7" for x in f)


def test_comment_fallback_is_a_noop_in_local_mode():
    # no discovery.source: github -> comments aren't a concept at all for local goal files; the
    # fallback must not attempt a network call it has no way to satisfy.
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _local_base(d, [("0001.md", "pending", _GOAL, "do it")], **_LOOSE)
        config = json.loads((pathlib.Path(base) / "config.json").read_text())
        calls = []

        def run(args):
            calls.append(list(args))
            return ""

        goal_doc = {"ref": str(pathlib.Path(base) / "goals" / "0001.md")}
        assert bc._goal_comment_text(base, config, goal_doc, run=run) == ""
        assert calls == []


def test_comment_fallback_fails_open_on_gh_error():
    # a comment-fetch failure must degrade ONLY the comment evidence, never the whole precheck --
    # distinct from cross_check's own outer `except Exception: return _pack(..., ["error"])`, which
    # must never fire for a comment-only failure.
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, "wire the surface", body="blocked by #7 until the contract lands"),
                            _rec(7, "freeze the contract")], **_LOOSE)
        run = _raising_runner()
        pack = bc.cross_check(base, "1", run=run)
        # the BODY-derived finding survives a comment-fetch failure...
        assert any(f["kind"] == "blocked-by" and f["ref"] == "7" for f in pack["findings"])
        # ...and the failure never trips cross_check's own top-level catch-all...
        assert "error" not in pack["degraded"]
        # ...but the comment-fetch was genuinely attempted (and did fail) this run -- 0 calls on
        # today's code, since nothing reads comments yet.
        assert len(run.calls) >= 1


def test_goal_comment_text_scrubs_a_secret_before_returning_it():
    """R5 (plan-review): the plan's original draft of this test asserted a planted secret never
    appears in json.dumps(cross_check(...)) -- but a finding's evidence is only the scrubbed
    *blocking phrase* ("blocked by"), never the comment body itself, so the comment text has no path
    into the pack at all and that assertion would pass identically on UNSCRUBBED code (vacuous:
    cannot fail before, cannot prove anything after). Retargeted at the actual scrub boundary:
    _goal_comment_text's own return value, which is where scrub() is actually called."""
    bc = _mod("backlog_check")
    secret = "AK" "IAABCDEFGHIJKLMNOP"
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, "wire the surface"), _rec(7, "freeze the contract")], **_LOOSE)
        config = json.loads((pathlib.Path(base) / "config.json").read_text())
        run = _comment_runner({"1": [_gh_comment("rotate " + secret + " then close #7")]})
        text = bc._goal_comment_text(base, config, {"ref": "1"}, run=run)
        assert secret not in text
        assert "[REDACTED:" in text


# --- #1186: Sigma's own park/fail comment BOILERPLATE must never itself read as a blocker
# reference. `sources.PARK_COMMENT_PREFIX` ("Parked by Sigma — needs human review: ") contains
# the word "needs" -- itself a `_BLOCK_RE` trigger -- so ANY #N the caller's own free-text `reason`
# happens to mention within the remaining ~25 characters of the 40-char match window reads as a
# phantom blocker with nothing to do with a real dependency.


def test_park_comment_boilerplate_is_itself_a_block_re_trigger():
    # non-vacuity: prove the underlying hazard is real before testing the fix for it -- a park
    # comment built from the SAME fixed prefix `sources.park()` actually posts, with a `#N`
    # mention near the boilerplate's own "needs", really does match _BLOCK_RE raw.
    bc = _mod("backlog_check")
    comment = bc.sources.PARK_COMMENT_PREFIX + "PR #7 is not approved yet (changes requested)"
    m = bc._BLOCK_RE.search(comment)
    assert m and m.group(1).lower() == "needs" and m.group(2) == "7"


def test_strip_offboard_prefixes_removes_the_fixed_prefix_but_keeps_the_reason():
    bc = _mod("backlog_check")
    texts = [bc.sources.PARK_COMMENT_PREFIX + "PR #7 is not approved yet",
            bc.sources.FAIL_COMMENT_PREFIX + "see #7 for the same traceback",
            "an ordinary human comment mentioning #7, untouched"]
    out = bc._strip_offboard_prefixes(texts)
    assert out[0] == "PR #7 is not approved yet"
    assert out[1] == "see #7 for the same traceback"
    assert out[2] == "an ordinary human comment mentioning #7, untouched"     # no prefix -> unchanged


def test_strip_offboard_prefixes_stripped_text_no_longer_triggers_block_re():
    bc = _mod("backlog_check")
    stripped = bc._strip_offboard_prefixes(
        [bc.sources.PARK_COMMENT_PREFIX + "PR #7 is not approved yet (changes requested)"])
    assert not bc._BLOCK_RE.search(stripped[0])


def test_strip_offboard_prefixes_leaves_a_genuine_blocked_by_reason_intact():
    # the fix must not throw the baby out with the bathwater: #1129's own mainstream case is a
    # REAL "blocked by #N" phrase embedded in the park REASON by backlog_check's own automated
    # park -- stripping only the fixed prefix must leave that fully scannable.
    bc = _mod("backlog_check")
    stripped = bc._strip_offboard_prefixes(
        [bc.sources.PARK_COMMENT_PREFIX + "backlog cross-check: blocked by #7 (1.0; shared: widget, cache)"])
    m = bc._BLOCK_RE.search(stripped[0])
    assert m and m.group(2) == "7"


def test_park_comment_needs_boilerplate_does_not_spawn_a_fresh_blocked_by_finding():
    # end-to-end through cross_check(), mirroring test_dismissal_reason_mentioning_a_new_ref_...'s
    # own shape for the sibling #830 bug: a park comment whose free reason coincidentally mentions
    # an open issue near the boilerplate's own "needs" must not spawn a confident blocked-by
    # finding against it.
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, "wire the surface"),
                            _rec(7, "an unrelated open issue")], **_LOOSE)
        comment = bc.sources.PARK_COMMENT_PREFIX + "PR #7 is not approved yet (changes requested)"
        run = _comment_runner({"1": [_gh_comment(comment)]})
        blocked = [x for x in bc.cross_check(base, "1", run=run)["findings"] if x["kind"] == "blocked-by"]
        assert not any(x["ref"] == "7" for x in blocked)


# --- ledger team-wide signals ---

def _write_claim(base, actor, goal, kind="claimed", ts="2026-08-02T00:00:00Z", **extra):
    led = pathlib.Path(base) / "ledger" / "entries"; led.mkdir(parents=True, exist_ok=True)
    row = {"id": f"{actor}:1", "actor": actor, "kind": kind, "goal": str(goal), "ts": ts, **extra}
    with (led / f"{actor}.jsonl").open("a") as f:
        f.write(json.dumps(row) + "\n")


def test_in_flight_elsewhere_from_ledger_claim():
    bc = _mod("backlog_check")
    now = _epoch("2026-08-02T06:00:00Z")          # 6h after the fixture claim — inside any sane lease
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, _GOAL), _rec(2, _DUP)], **_LOOSE)
        _write_claim(base, "bob", "2")           # a teammate is already working the paraphrase #2
        kinds = {(f["kind"], f["ref"]) for f in bc.cross_check(base, "1", now=now)["findings"]}
        assert ("in-flight-elsewhere", "2") in kinds


# --- #535: the in-flight branch honors the claim-lease TTL --------------------------------------
# The similarity channel reads the SAME lease `_next()` does, so these anchor `now` explicitly
# against `_write_claim`'s fixture timestamp instead of the wall clock: a fixed fixture ts plus a
# real clock silently ages past any TTL and the suite starts failing on a calendar date.
_CLAIM_TS = "2026-08-02T00:00:00Z"                # _write_claim's own default, stated for the reader
_FRESH = _epoch("2026-08-02T06:00:00Z")           # +6h  — inside the 12h default lease
_STALE = _epoch("2026-08-04T00:00:00Z")           # +48h — beyond the default and beyond a 24h setting


def test_in_flight_elsewhere_ignores_a_claim_past_its_lease_ttl():
    # an abandoned claim (crashed session, yesterday's run) must stop parking new similar goals the
    # moment the lease system itself would have released it
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, _GOAL), _rec(2, _DUP)],
                        ledger={"lease": {"ttl_hours": 24}}, **_LOOSE)
        _write_claim(base, "bob", "2", ts=_CLAIM_TS)
        kinds = {(f["kind"], f["ref"]) for f in bc.cross_check(base, "1", now=_STALE)["findings"]}
        assert ("in-flight-elsewhere", "2") not in kinds


def test_in_flight_elsewhere_still_fires_for_a_claim_inside_its_lease_ttl():
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, _GOAL), _rec(2, _DUP)],
                        ledger={"lease": {"ttl_hours": 24}}, **_LOOSE)
        _write_claim(base, "bob", "2", ts=_CLAIM_TS)
        kinds = {(f["kind"], f["ref"]) for f in bc.cross_check(base, "1", now=_FRESH)["findings"]}
        assert ("in-flight-elsewhere", "2") in kinds


def test_in_flight_elsewhere_never_expires_when_the_ttl_is_disabled():
    """CONTRACT PIN (green before and after): `ttl_hours: 0`/false means never-expire in
    `lease_ttl_seconds`, and this channel must read that setting with exactly the same meaning as
    every other lease consumer — a disabled TTL keeps an old claim authoritative, by configuration."""
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, _GOAL), _rec(2, _DUP)],
                        ledger={"lease": {"ttl_hours": 0}}, **_LOOSE)
        _write_claim(base, "bob", "2", ts=_CLAIM_TS)
        kinds = {(f["kind"], f["ref"]) for f in bc.cross_check(base, "1", now=_STALE)["findings"]}
        assert ("in-flight-elsewhere", "2") in kinds


def test_in_flight_elsewhere_applies_the_default_ttl_with_no_ledger_config():
    # no `ledger` key at all — the overwhelmingly common shape — must still get DEFAULT_LEASE_TTL_HOURS
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, _GOAL), _rec(2, _DUP)], **_LOOSE)
        _write_claim(base, "bob", "2", ts=_CLAIM_TS)
        kinds = {(f["kind"], f["ref"]) for f in bc.cross_check(base, "1", now=_STALE)["findings"]}
        assert ("in-flight-elsewhere", "2") not in kinds


# --- #532: a hand-off blocks the side that FILED it, never the side it was handed TO -------------
# The ledger `handoff` entry carries goal=<the filing goal> and issue=<the target it opened in the
# owner's area>. Every fixture below therefore keeps those two DISTINCT (filer #5, target #11) --
# the predecessor test used goal==issue==1, which matches under either rule and so could not see
# the inversion at all. Both issues live in the mirror on purpose: `cross_check` short-circuits on
# `goal_not_in_corpus` before any ledger signal runs, which would make the target-side assert
# vacuously green.

def test_ledger_outstanding_handoff_blocks_the_filer_and_names_the_target():
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(5, _GOAL), _rec(11, _DISTINCT)], **_LOOSE)
        _write_claim(base, "amy", "5", kind="handoff", state="open", issue=11, to="bob")
        kinds = {(f["kind"], f["ref"]) for f in bc.cross_check(base, "5")["findings"]}
        # ref is the TARGET that must land first -- the same the-other-item meaning `ref` carries
        # in every other finding, not the self-reference the pre-fix branch emitted.
        assert ("blocked-by", "11") in kinds


def test_ledger_outstanding_handoff_does_not_block_the_handed_off_target():
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(5, _GOAL), _rec(11, _DISTINCT)], **_LOOSE)
        _write_claim(base, "amy", "5", kind="handoff", state="open", issue=11, to="bob")
        kinds = {f["kind"] for f in bc.cross_check(base, "11")["findings"]}
        assert "blocked-by" not in kinds          # #11 IS the work; parking it strands both sides


def test_ledger_handoff_block_on_the_filer_clears_once_the_target_is_acked_resolved():
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(5, _GOAL), _rec(11, _DISTINCT)], **_LOOSE)
        _write_claim(base, "amy", "5", kind="handoff", state="open", issue=11, to="bob")
        assert any(f["kind"] == "blocked-by" for f in bc.cross_check(base, "5")["findings"])
        # the ack pairs on the TARGET issue (`handoff_key`), which this fix deliberately leaves
        # alone -- only the side the block is ASSERTED against moved.
        _write_claim(base, "bob", "5", kind="ack", state="resolved", issue=11,
                     ts="2026-08-02T01:00:00Z")
        assert not any(f["kind"] == "blocked-by" for f in bc.cross_check(base, "5")["findings"])


def test_ledger_handoff_block_on_the_filer_clears_once_the_target_issue_is_closed():
    # An `ack` is skippable -- the recipient's normal path is merge-and-close. Without this release
    # rule the filer would park forever, since `outstanding()` settles only on ack resolved/declined.
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(5, _GOAL),
                            _rec(11, _DISTINCT, state="closed", closed_at="2026-08-01T00:00:00Z")],
                        **_LOOSE)
        _write_claim(base, "amy", "5", kind="handoff", state="open", issue=11, to="bob")
        kinds = {f["kind"] for f in bc.cross_check(base, "5")["findings"]}
        assert "blocked-by" not in kinds


# --- #533: area-qualified settlement cross-guard with #532 --------------------------------------
# #533 only changes WHICH hand-offs ledger.outstanding() reports as live; _ledger_signals() (#532,
# above) blocks the FILER goal on any such entry. An issue-less goal handed off to two areas must
# therefore stay blocked once only ONE area's ack lands -- the wrongly-settle-both bug #533 fixes,
# now proven at the level a real park decision is made from, not just against the raw ledger helpers.

def test_ledger_area_qualified_settlement_keeps_the_filer_blocked_until_every_area_is_acked():
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(5, _GOAL)], **_LOOSE)
        _write_claim(base, "amy", "5", kind="handoff", state="open", area="engine", to="bob",
                     ts="2026-08-02T00:00:00Z")
        _write_claim(base, "amy", "5", kind="handoff", state="open", area="ui", to="cara",
                     ts="2026-08-02T00:01:00Z")
        _write_claim(base, "bob", "5", kind="ack", state="resolved", area="engine",
                     ts="2026-08-02T01:00:00Z")
        kinds = {f["kind"] for f in bc.cross_check(base, "5")["findings"]}
        assert "blocked-by" in kinds          # the ui hand-off is still outstanding


# --- #521: decomposition-marker dedup exemption -------------------------------------------------
# A goal whose BODY's first line carries `sigma:decomposed-from=`/`sigma:decompose-of=` (the
# same markers loop.py's decompose_check already exempts, single-sourced in goal_size.py) is a
# deliberately authored decomposition child/meta-goal. The duplicate path's _earlier() rule always
# parks the NEWER of a similar pair, and a freshly created child is always the newest -- so it would
# always park against its own parent/siblings; obsoleted-by and in-flight-elsewhere-similarity share
# the same false-positive shape. Exemption downgrades `confident` only: the finding is still EMITTED
# (module contract: evidence, never a verdict) -- every test below asserts PRESENT + confident False,
# never absent. Explicit blockers and recorded hand-offs are NEVER exempt.
#
# Non-vacuity device (matching :75-85/:107-119 above): identical-title records score ~0.94-0.98 with
# park_threshold=0.8, so each "exempts" case is genuinely confident=True on today's (pre-fix) code --
# a realistic ~0.77 paraphrase would already be non-confident and prove nothing.

def test_duplicate_exemption_for_decomposition_child_marked_first_line():
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, _GOAL),
                            _rec(2, _GOAL, body="sigma:decomposed-from=1\n\nchild details")],
                        dup_threshold=0.4, park_threshold=0.8, closed_window_days=3650)
        f = next(f for f in bc.cross_check(base, "2")["findings"] if f["ref"] == "1")
        assert f["kind"] == "duplicate"
        assert f["score"] >= 0.8                # confirms this pair clears park_threshold at all
        assert f["confident"] is False           # PRESENT, downgraded -- on unfixed code this is True


def test_obsoleted_by_exemption_for_decomposition_child_marked_first_line():
    bc = _mod("backlog_check")
    now = _epoch("2026-08-03T00:00:00Z")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, _GOAL, state="closed", closed_at="2026-08-01T00:00:00Z"),
                            _rec(2, _GOAL, body="sigma:decomposed-from=1\n\nchild details")],
                        dup_threshold=0.4, obsolete_threshold=0.4, park_threshold=0.8,
                        closed_window_days=30)
        f = next(f for f in bc.cross_check(base, "2", now=now)["findings"] if f["ref"] == "1")
        assert f["kind"] == "obsoleted-by"
        assert f["score"] >= 0.8
        assert f["confident"] is False           # PRESENT, downgraded -- on unfixed code this is True


def test_in_flight_elsewhere_exemption_for_decomposition_child_marked_first_line():
    # the child being checked (#2) is marked; a teammate is claiming its SIBLING (#3) -- parallel
    # sibling execution is exactly what decomposition creates, so this is the same false-positive
    # family as duplicate/obsoleted-by, just at ledger similarity's lower (unconditional) bar.
    bc = _mod("backlog_check")
    now = _epoch("2026-08-02T06:00:00Z")          # 6h after the fixture claim — inside any sane lease
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, _GOAL),
                            _rec(2, _GOAL, body="sigma:decomposed-from=1"),
                            _rec(3, _GOAL)],
                        dup_threshold=0.4, park_threshold=0.8, closed_window_days=3650)
        _write_claim(base, "bob", "3")
        f = next(f for f in bc.cross_check(base, "2", now=now)["findings"]
                 if f["kind"] == "in-flight-elsewhere" and f["ref"] == "3")
        assert f["score"] >= 0.4
        assert f["confident"] is False           # PRESENT, downgraded -- on unfixed code this is True


def test_ledger_handoff_blocker_stays_confident_for_a_marked_child():
    # the never-exempt pin: a RECORDED hand-off the marked child itself FILED is a real, explicit
    # blocker -- must stay confident True regardless of the child's own marker. Filer and target
    # are distinct (#532) so this pins the filer side; the target (#11) is absent from the mirror
    # on purpose, the fail-closed case of the closed-target release rule.
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(2, _GOAL, body="sigma:decomposed-from=1")], **_LOOSE)
        _write_claim(base, "amy", "2", kind="handoff", state="open", issue=11, to="bob")
        f = next(f for f in bc.cross_check(base, "2")["findings"] if f["kind"] == "blocked-by")
        assert f["confident"] is True


def test_explicit_blocker_stays_confident_and_named_for_a_marked_child():
    # a marked child with an explicit "Blocked by #N" in its own body: the blocker finding stays
    # confident True, and decide()'s park reason actually NAMES the blocker (#7).
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [
            _rec(1, _GOAL, body="sigma:decomposed-from=99\n\nblocked by #7 until the base lands"),
            _rec(7, "freeze the base contract"),
        ], dup_threshold=0.72, obsolete_threshold=0.72, park_threshold=0.80, closed_window_days=3650)
        pack = bc.cross_check(base, "1")
        blocked = [f for f in pack["findings"] if f["kind"] == "blocked-by"]
        assert any(f["ref"] == "7" and f["confident"] is True for f in blocked)
        decision = bc.decide(pack, {})
        assert decision["action"] == "park"
        assert "#7" in decision["reason"]


def test_duplicate_not_exempt_without_a_marker_matched_pair():
    # dedup-not-weakened pin: the SAME shape as the marked test above (identical title, similar body
    # length) but with no marker at all -- must stay confident True, proving `exempt` is correctly
    # False by default and never accidentally suppresses a genuine duplicate.
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, _GOAL),
                            _rec(2, _GOAL, body="child details, no marker here")],
                        dup_threshold=0.4, park_threshold=0.8, closed_window_days=3650)
        f = next(f for f in bc.cross_check(base, "2")["findings"] if f["ref"] == "1")
        assert f["confident"] is True


def test_marker_in_title_only_does_not_exempt():
    # the one negative a sloppy `raw.splitlines()[0]` implementation would pass without: raw's own
    # first line IS the title, so a title-only marker must NOT exempt -- only a first-line BODY marker
    # does. Lower park_threshold (0.75, still well above the 0.4 dup_threshold) because the extra
    # marker tokens IN the title (3x title weight) drag the score down to ~0.80 -- still confident at
    # a realistic threshold, so this stays non-vacuous instead of failing on score alone.
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, _GOAL), _rec(2, _GOAL + " sigma:decomposed-from=1")],
                        dup_threshold=0.4, park_threshold=0.75, closed_window_days=3650)
        f = next(f for f in bc.cross_check(base, "2")["findings"] if f["ref"] == "1")
        assert f["confident"] is True


def test_marker_mid_body_does_not_exempt():
    # first-line anchoring: a marker that shows up later in the body (not the first line) must not
    # exempt -- a goal merely discussing decomposition in passing isn't exempted by accident.
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        body = "some preamble text here.\nsigma:decomposed-from=1\nmore text"
        base = _gh_base(d, [_rec(1, _GOAL), _rec(2, _GOAL, body=body)],
                        dup_threshold=0.4, park_threshold=0.8, closed_window_days=3650)
        f = next(f for f in bc.cross_check(base, "2")["findings"] if f["ref"] == "1")
        assert f["confident"] is True


def test_crlf_body_first_line_does_exempt():
    # CRLF-tolerant, matching loop.py's own guard: a `\r\n`-terminated first line still exempts.
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        body = "sigma:decomposed-from=1\r\n\r\nchild details"
        base = _gh_base(d, [_rec(1, _GOAL), _rec(2, _GOAL, body=body)],
                        dup_threshold=0.4, park_threshold=0.8, closed_window_days=3650)
        f = next(f for f in bc.cross_check(base, "2")["findings"] if f["ref"] == "1")
        assert f["confident"] is False           # on unfixed code this is True


def test_decompose_of_marker_also_exempts_duplicate_path():
    # symmetry: `sigma:decompose-of=` (a meta-goal decomposing #N) exempts exactly like
    # `sigma:decomposed-from=` does.
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, _GOAL),
                            _rec(2, _GOAL, body="sigma:decompose-of=1\n\nmeta goal details")],
                        dup_threshold=0.4, park_threshold=0.8, closed_window_days=3650)
        f = next(f for f in bc.cross_check(base, "2")["findings"] if f["ref"] == "1")
        assert f["confident"] is False           # on unfixed code this is True


def test_local_mode_lstrip_pin_marker_after_leading_blank_line_exempts():
    # local-mode bodies do NOT always start with "\n" right after the frontmatter delimiter (#544
    # fixed `_build_corpus` to strip the fence itself via frontmatter.strip(), not leave an artifact
    # newline behind) -- but a goal file AUTHORED with a blank line right after the closing fence (a
    # common, legitimate markdown style) still produces one, since that blank line is real content the
    # fence-stripping never touches. The explicit leading "\n" in 0002's body below reproduces exactly
    # that authored-blank-line shape -- without lstrip(), the "first line" would be "" and this marker
    # would never be seen. local-mode ref ordering is by filename ("0001.md" < "0002.md"), so the LATER
    # goal (0002) is the one that would park.
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _local_base(d, [
            ("0001.md", "pending", _GOAL, "already shipped equivalent"),
            ("0002.md", "pending", _GOAL, "\nsigma:decomposed-from=0001\n\nchild details"),
        ], dup_threshold=0.4, park_threshold=0.8, closed_window_days=3650)
        goal = str(pathlib.Path(base) / "goals" / "0002.md")
        f = next(f for f in bc.cross_check(base, goal)["findings"]
                 if pathlib.Path(f["ref"]).name == "0001.md")
        assert f["confident"] is False           # on unfixed code this is True


def test_local_mode_a_bare_triple_dash_inside_a_frontmatter_value_does_not_pollute_the_body():
    """#544: `text.split("---", 2)[-1]` splits on the first TWO bare '---' SUBSTRINGS, not the
    line-anchored fence `frontmatter.parse()`/`frontmatter.strip()` already implement -- a
    frontmatter VALUE containing a literal '---' (the issue's own repro shape: a free-text field
    like "Fix the A---B connector bug") is itself counted as a split point ahead of the real
    closing fence, so the body ends up under-stripped: a fragment of that value plus the real
    closing fence get prepended to it. That pollutes the dedup token vector, and (#521) can
    silently revoke a decomposition child's marker exemption because the corrupted body's first
    line is no longer the marker.

    Written directly (not via `_local_base`, which only lets title/body vary) so the corrupting
    `note:` field can carry the '---' while `title` stays IDENTICAL between the two goals -- that
    similarity is what makes 0001/0002 a real 'duplicate' finding in the first place, the same way
    every other exemption test in this file relies on `_GOAL` for both sides."""
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"
        (base / "goals").mkdir(parents=True)
        (base / "config.json").write_text(json.dumps(
            {"backlog_check": {"dup_threshold": 0.4, "park_threshold": 0.8, "closed_window_days": 3650}}))
        (base / "goals" / "0001.md").write_text(
            f"---\nid: 0001\nstatus: pending\ntitle: {_GOAL}\n---\nalready shipped equivalent\n")
        (base / "goals" / "0002.md").write_text(
            f"---\nid: 0002\nstatus: pending\ntitle: {_GOAL}\nnote: Fix the A---B connector bug\n"
            "---\nsigma:decomposed-from=0001\n\nchild details\n")

        docs, _ = bc._build_corpus(str(base), {})
        doc = next(doc for doc in docs if pathlib.Path(doc["ref"]).name == "0002.md")
        assert doc["body"].lstrip().startswith("sigma:decomposed-from=0001")
        assert "---" not in doc["body"]              # no leftover closing-fence fragment
        assert "connector bug" not in doc["body"]     # no leftover note-field fragment either

        goal = str(base / "goals" / "0002.md")
        f = next(f for f in bc.cross_check(str(base), goal)["findings"]
                 if pathlib.Path(f["ref"]).name == "0001.md")
        assert f["confident"] is False           # the #521 exemption survives -- on unfixed code this is True


def test_marker_constants_identical_across_modules():
    # #521 change 4: single source of truth in goal_size.py -- both backlog_check's own module-level
    # `goal_size` reference and loop.py's own `_load("goal_size")` call resolve to the SAME constant
    # values, not independently-typed literals that could silently drift apart.
    gs = _mod("goal_size")
    bc = _mod("backlog_check")
    lp = _mod("loop")
    assert gs.DECOMPOSED_FROM_MARKER == "sigma:decomposed-from="
    assert gs.DECOMPOSE_OF_MARKER == "sigma:decompose-of="
    assert bc.goal_size.DECOMPOSED_FROM_MARKER == gs.DECOMPOSED_FROM_MARKER
    assert bc.goal_size.DECOMPOSE_OF_MARKER == gs.DECOMPOSE_OF_MARKER
    assert lp._load("goal_size").DECOMPOSED_FROM_MARKER == gs.DECOMPOSED_FROM_MARKER
    assert lp._load("goal_size").DECOMPOSE_OF_MARKER == gs.DECOMPOSE_OF_MARKER


def test_backlog_check_exempt_reads_the_constant_live_not_a_hardcoded_copy():
    # stronger than value-equality: mutate the ACTUAL module object backlog_check.py holds and prove
    # cross_check's exemption follows it -- this fails if backlog_check ever hardcodes its own copy
    # of the marker string instead of reading goal_size.DECOMPOSED_FROM_MARKER live.
    bc = _mod("backlog_check")
    original = bc.goal_size.DECOMPOSED_FROM_MARKER
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, _GOAL),
                            _rec(2, _GOAL, body="totally-renamed-marker=1\n\nchild details")],
                        dup_threshold=0.4, park_threshold=0.8, closed_window_days=3650)
        before = next(f for f in bc.cross_check(base, "2")["findings"] if f["ref"] == "1")
        assert before["confident"] is True       # an unrecognized marker string doesn't exempt yet
        bc.goal_size.DECOMPOSED_FROM_MARKER = "totally-renamed-marker="
        try:
            after = next(f for f in bc.cross_check(base, "2")["findings"] if f["ref"] == "1")
            assert after["confident"] is False   # now exempt, purely from the constant changing
        finally:
            bc.goal_size.DECOMPOSED_FROM_MARKER = original


# --- #830: human-dismissed-finding marker ---------------------------------------------------------
# A human who reviews a parked, confident finding and decides it's a false positive can post
# `dismiss_comment(kind, ref)` as a COMMENT on the goal issue; a later cross_check reads it back
# (bare substring/regex over the SAME `comment_text` already fetched for _explicit_blockers -- zero
# new gh calls) and downgrades that EXACT (kind, ref) finding to confident=False. Same "evidence,
# never a verdict" contract as the #521 exemption block above: the finding stays PRESENT, only
# `confident` changes -- every test below asserts PRESENT + confident False, never absent.

def test_dismiss_comment_round_trips_through_dismissed_findings():
    bc = _mod("backlog_check")
    text = bc.dismiss_comment("blocked-by", "821", "reviewed directly, not a real dependency")
    assert bc._dismissed_findings(text) == {("blocked-by", "821")}


def test_dismiss_comment_tolerates_a_stray_leading_hash_on_ref():
    # a human copying "#821" straight off the park comment (_KIND_PHRASE renders it WITH the hash)
    # must still round-trip -- every finding's own `ref` is stored bare, never "#821".
    bc = _mod("backlog_check")
    text = bc.dismiss_comment("blocked-by", "#821")
    assert bc._dismissed_findings(text) == {("blocked-by", "821")}


def test_dismiss_comment_text_does_not_itself_trigger_a_fresh_explicit_blocker():
    # the self-defeat trap: a naive template reusing _KIND_PHRASE's "blocked by #{ref}" wording would
    # make the DISMISSAL comment itself look like a fresh explicit blocker on the very finding it
    # dismisses -- the next cross_check would then re-derive a brand-new confident "blocked-by"
    # finding from this comment's own text and silently undo the dismissal.
    bc = _mod("backlog_check")
    text = bc.dismiss_comment("blocked-by", "821", "reviewed directly, not a real dependency")
    assert not bc._BLOCK_RE.search(text)


def test_dismissed_explicit_blocker_is_not_confident():
    # non-vacuity: test_explicit_blocked_by_open_ref_only (above) already proves this exact fixture
    # shape is confident True with NO dismissal comment present -- this is the same shape, PLUS the
    # marker comment.
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, "wire the surface", body="blocked by #7 until the contract lands"),
                            _rec(7, "freeze the contract")], **_LOOSE)
        marker = bc.dismiss_comment("blocked-by", "7", "reviewed -- different function, no real dep")
        run = _comment_runner({"1": [_gh_comment(marker)]})
        f = next(x for x in bc.cross_check(base, "1", run=run)["findings"] if x["kind"] == "blocked-by")
        assert f["ref"] == "7"
        assert f["confident"] is False           # PRESENT, downgraded -- on unfixed code this is True


def test_dismissed_finding_does_not_flip_decide_to_park():
    # the actual acceptance bar (#830): a dismissed finding must not re-park on the very next
    # precheck -- on unfixed code this asserts "park", i.e. the exact re-park-forever bug reported.
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, "wire the surface", body="blocked by #7 until the contract lands"),
                            _rec(7, "freeze the contract")], **_LOOSE)
        marker = bc.dismiss_comment("blocked-by", "7")
        run = _comment_runner({"1": [_gh_comment(marker)]})
        pack = bc.cross_check(base, "1", run=run)
        decision = bc.decide(pack, {})
        assert decision["action"] == "proceed"
        assert "advisory" in decision["note"]


def test_dismissal_is_scoped_to_the_exact_kind_and_ref():
    # precision pin: a dismissal for (blocked-by, 7) must not suppress a DIFFERENT finding on the
    # same goal -- same kind, a different ref, must stay untouched.
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [
            _rec(1, _GOAL, body="blocked by #7 until the contract lands\nalso blocked by #9"),
            _rec(7, "freeze the contract"),
            _rec(9, "a second, real blocker"),
        ], **_LOOSE)
        marker = bc.dismiss_comment("blocked-by", "7", "false positive")
        run = _comment_runner({"1": [_gh_comment(marker)]})
        findings = bc.cross_check(base, "1", run=run)["findings"]
        f7 = next(x for x in findings if x["kind"] == "blocked-by" and x["ref"] == "7")
        f9 = next(x for x in findings if x["kind"] == "blocked-by" and x["ref"] == "9")
        assert f7["confident"] is False           # dismissed
        assert f9["confident"] is True            # untouched -- different ref, same kind


def test_dismissal_survives_alongside_unrelated_comments():
    # the marker scan runs over the FULL concatenated comment text (mirrors _explicit_blockers' own
    # comment fallback) -- an unrelated comment before or after the marker must not hide it.
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, "wire the surface", body="blocked by #7 until the contract lands"),
                            _rec(7, "freeze the contract")], **_LOOSE)
        marker = bc.dismiss_comment("blocked-by", "7")
        run = _comment_runner({"1": [
            _gh_comment("looks good, will review soon", cid="IC_1"),
            _gh_comment(marker, cid="IC_2"),
            _gh_comment("thanks!", cid="IC_3"),
        ]})
        f = next(x for x in bc.cross_check(base, "1", run=run)["findings"] if x["kind"] == "blocked-by")
        assert f["confident"] is False


def test_dismiss_cli_prints_the_marker_text(capsys):
    bc = _mod("backlog_check")
    assert bc.main(["backlog_check.py", "dismiss-text", "blocked-by", "821", "reviewed", "directly"]) == 0
    text = capsys.readouterr().out.strip()
    assert bc._dismissed_findings(text) == {("blocked-by", "821")}
    assert "reviewed directly" in text


# --- PR #1109 review fixes for #830 -----------------------------------------------------------

def test_dismissal_reason_mentioning_a_new_ref_does_not_spawn_a_fresh_finding():
    # finding 3 (blocking, the serious one): `dismiss_comment`'s free-text `reason` used to be
    # embedded verbatim into the posted comment with no check against _BLOCK_RE's trigger
    # vocabulary. A perfectly ordinary, honest dismissal reason mentioning "needs #9" while
    # explaining why #7 is NOT a real dependency made _BLOCK_RE match "needs #9" INSIDE the
    # dismissal comment itself, so cross_check correctly downgraded #7 but ALSO emitted a
    # brand-new confident blocked-by finding against the unrelated #9 -- decide() parked again.
    # The exact re-park-forever bug #830 was filed to fix, just relocated via a different field
    # than the one that got a regression test until now.
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, "wire the surface", body="blocked by #7 until the contract lands"),
                            _rec(7, "freeze the contract"),
                            _rec(9, "an unrelated open issue")], **_LOOSE)
        marker = bc.dismiss_comment(
            "blocked-by", "7",
            "not a real dependency -- this actually needs #9 to land first, unrelated to the contract")
        # non-vacuity: confirm _BLOCK_RE really does match inside the marker text, so a failure to
        # filter it out of the blocker-phrase corpus would really produce the false #9 finding.
        assert bc._BLOCK_RE.search(marker) and bc._BLOCK_RE.search(marker).group(2) == "9"
        run = _comment_runner({"1": [_gh_comment(marker)]})
        pack = bc.cross_check(base, "1", run=run)
        blocked = [x for x in pack["findings"] if x["kind"] == "blocked-by"]
        assert not any(x["ref"] == "9" for x in blocked)    # no fresh false finding from the reason text
        f7 = next(x for x in blocked if x["ref"] == "7")
        assert f7["confident"] is False                     # #7 still correctly dismissed
        # the actual acceptance bar: this must not re-park either.
        assert bc.decide(pack, {})["action"] == "proceed"


def test_dismissal_filter_still_lets_the_same_comment_be_read_for_its_own_marker():
    # the fix must not throw the baby out with the bathwater: excluding a marker-bearing comment
    # from the blocker-PHRASE scan must not also blind the MARKER scan itself to that same comment
    # -- they read the same raw text through two different lenses.
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, "wire the surface", body="blocked by #7 until the contract lands"),
                            _rec(7, "freeze the contract")], **_LOOSE)
        marker = bc.dismiss_comment("blocked-by", "7", "needs nothing else, just a clean dismissal")
        run = _comment_runner({"1": [_gh_comment(marker)]})
        pack = bc.cross_check(base, "1", run=run)
        f7 = next(x for x in pack["findings"] if x["kind"] == "blocked-by" and x["ref"] == "7")
        assert f7["confident"] is False


def test_dismiss_marker_survives_a_long_reason_past_the_excerpt_cap():
    # finding 4 (blocking): the marker sits at the END of dismiss_comment()'s narrative, but the
    # old read-back path capped each comment body at mirror._EXCERPT_CHARS (500 -- a constant sized
    # for TF-IDF corpus excerpting, a completely different use case) BEFORE scanning for the
    # marker. A `reason` long enough to push the marker past that cap silently truncated it off
    # entirely, so the finding never got recognized as dismissed and re-parked on the very next
    # run -- #830 all over again, just triggered by an ordinary detailed reason instead of a stale
    # label.
    bc = _mod("backlog_check")
    long_reason = ("this was investigated directly against the actual contract surface and the "
                   "dependency does not exist in practice -- the two modules were split apart in "
                   "the refactor that landed last sprint, and the remaining reference is a stale "
                   "docstring comment nobody updated, not a real runtime or build-time coupling "
                   "between the two components, confirmed by reading the actual call graph end to "
                   "end and finding no shared import, no shared queue, and no shared schema anywhere "
                   "in the current codebase as of this review")
    assert len(long_reason) > 450
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, "wire the surface", body="blocked by #7 until the contract lands"),
                            _rec(7, "freeze the contract")], **_LOOSE)
        marker = bc.dismiss_comment("blocked-by", "7", long_reason)
        # non-vacuity: prove the marker genuinely sits past the old cap, so this is a real repro of
        # the truncation, not a fixture that happens to pass either way.
        assert bc.DISMISS_MARKER not in marker[:500]
        assert bc.DISMISS_MARKER in marker
        run = _comment_runner({"1": [_gh_comment(marker)]})
        pack = bc.cross_check(base, "1", run=run)
        f7 = next(x for x in pack["findings"] if x["kind"] == "blocked-by" and x["ref"] == "7")
        assert f7["confident"] is False    # marker still recognized -- on unfixed code this is True


def test_dismiss_cli_rejects_too_few_args_instead_of_falling_through(capsys):
    # finding 5 (nitpick): `if len(argv) >= 4 and argv[1] == "dismiss-text":` let a call with too
    # few args (e.g. missing `ref`) fall through and get silently misinterpreted as a normal
    # `cross_check(sdlc_dir=argv[1], goal=argv[2])` call instead of producing a usage error.
    bc = _mod("backlog_check")
    assert bc.main(["backlog_check.py", "dismiss-text", "blocked-by"]) == 2
    assert "usage" in capsys.readouterr().err.lower()
    assert bc.main(["backlog_check.py", "dismiss-text"]) == 2
    assert "usage" in capsys.readouterr().err.lower()
    # the valid shape (exactly kind + ref, no reason) must still work unchanged.
    assert bc.main(["backlog_check.py", "dismiss-text", "blocked-by", "821"]) == 0
    text = capsys.readouterr().out.strip()
    assert bc._dismissed_findings(text) == {("blocked-by", "821")}


def test_local_mode_dismissal_is_a_loud_signal_not_a_silent_trap():
    # finding 1 (blocking): the dismissal-reading path is built entirely around GitHub issue
    # comments -- local-mode discovery has none, so a local-mode operator who ran the documented
    # remediation (`loop.py note` -> LocalSource.note(), which already worked correctly as a
    # WRITE) got no error, no warning: the finding just silently stayed confident and would
    # re-park on the very next run, reproducing the exact bug #830 exists to fix. After the fix:
    # local mode still cannot APPLY the dismissal (a local finding's `ref` is a full goal-file
    # path, not the bare issue number the marker format requires -- true dismissal stays
    # GitHub-mode only, see SKILL.md), but it is no longer SILENT about the attempt.
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        # only the LATER goal of a duplicate pair is park-confident (`_earlier`) -- check 0002
        # (later) against 0001 (earlier), not the other way round. park_threshold lowered too
        # (unlike _LOOSE's default 0.80): _GOAL vs _DUP's real cosine (~0.50) needs it to register
        # as CONFIDENT, not just present, so the "before" half of this test is non-vacuous.
        base = _local_base(d, [("0001.md", "pending", _GOAL, "do it"),
                               ("0002.md", "pending", _DUP, "same thing")],
                           **dict(_LOOSE, park_threshold=0.4))
        config = json.loads((pathlib.Path(base) / "config.json").read_text())
        goal = str(pathlib.Path(base) / "goals" / "0002.md")

        # non-vacuity: confident True and no flag, with nothing dismissed yet.
        before = bc.cross_check(base, goal)
        assert "dismissal_local_mode_unsupported" not in before["degraded"]
        dup_before = next(f for f in before["findings"] if f["kind"] == "duplicate")
        assert dup_before["confident"] is True

        # exactly the documented SKILL.md remediation: dismiss-text's output posted via the same
        # `source.note()` `loop.py note` itself dispatches to.
        source = bc.sources.get_source(base, config)
        marker = bc.dismiss_comment("duplicate", "0001", "reviewed directly, not a real duplicate")
        source.note(goal, marker)
        assert bc.DISMISS_MARKER in (pathlib.Path(base) / "journey" / "0002.md").read_text()

        after = bc.cross_check(base, goal)
        assert "dismissal_local_mode_unsupported" in after["degraded"]   # LOUD now, not silent
        dup_after = next(f for f in after["findings"] if f["kind"] == "duplicate")
        assert dup_after["confident"] is True   # still not applied (github-only) -- but visibly flagged

        decision = bc.decide(after, {})
        assert decision["action"] == "park"
        assert "NOT applied" in decision["reason"]


def test_local_mode_dismissal_flag_absent_in_github_mode():
    # `_local_dismissal_flag` must never fire for github-mode packs, even if a journey dir happens
    # to exist on disk (e.g. a repo that switched discovery modes) -- github mode has its own real
    # dismissal support and must not layer a spurious "unsupported" note on top of it.
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, "wire the surface", body="blocked by #7 until the contract lands"),
                            _rec(7, "freeze the contract")], **_LOOSE)
        jdir = pathlib.Path(base) / "journey"; jdir.mkdir(parents=True)
        (jdir / "1.md").write_text(f"\n## note\n{bc.DISMISS_MARKER} kind=blocked-by ref=7\n")
        pack = bc.cross_check(base, "1")
        assert "dismissal_local_mode_unsupported" not in pack["degraded"]


# --- recommended coverage (PR #1109 review, "also worth doing") -------------------------------

def test_dismissal_is_scoped_to_kind_not_just_ref():
    # precision pin, the other axis from test_dismissal_is_scoped_to_the_exact_kind_and_ref: a
    # dismissal for (duplicate, 7) must not suppress a DIFFERENT finding sharing the SAME ref but
    # a DIFFERENT kind on the same goal.
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [
            _rec(9, _GOAL, body="blocked by #7 until the contract lands"),
            _rec(7, _GOAL),   # same title as #9 -> also a duplicate match, AND the blocker target
        ], **_LOOSE)
        marker = bc.dismiss_comment("duplicate", "7", "not really a duplicate, just a shared template")
        run = _comment_runner({"9": [_gh_comment(marker)]})
        findings = bc.cross_check(base, "9", run=run)["findings"]
        dup7 = next(x for x in findings if x["kind"] == "duplicate" and x["ref"] == "7")
        blk7 = next(x for x in findings if x["kind"] == "blocked-by" and x["ref"] == "7")
        assert dup7["confident"] is False    # dismissed
        assert blk7["confident"] is True     # untouched -- different kind, same ref


def test_two_dismissal_markers_in_one_thread_both_take_effect():
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [
            _rec(1, "wire the surface", body="blocked by #7 until the contract lands\nalso blocked by #9"),
            _rec(7, "freeze the contract"),
            _rec(9, "a second, real blocker"),
        ], **_LOOSE)
        m7 = bc.dismiss_comment("blocked-by", "7", "false positive one")
        m9 = bc.dismiss_comment("blocked-by", "9", "false positive two")
        run = _comment_runner({"1": [_gh_comment(m7, cid="IC_1"), _gh_comment(m9, cid="IC_2")]})
        findings = bc.cross_check(base, "1", run=run)["findings"]
        f7 = next(x for x in findings if x["kind"] == "blocked-by" and x["ref"] == "7")
        f9 = next(x for x in findings if x["kind"] == "blocked-by" and x["ref"] == "9")
        assert f7["confident"] is False
        assert f9["confident"] is False


def test_malformed_dismiss_marker_syntax_fails_safe():
    # uppercase kind, underscore instead of hyphen, a "#" where a bare digit is required -- none of
    # these are the exact marker syntax _DISMISS_RE requires; must fail safe (no crash, simply no
    # match) rather than being entirely untested.
    bc = _mod("backlog_check")
    assert bc._dismissed_findings("<!-- sigma:dismissed-finding kind=BLOCKED-BY ref=7 -->") == set()
    assert bc._dismissed_findings("<!-- sigma:dismissed-finding kind=blocked_by ref=7 -->") == set()
    assert bc._dismissed_findings("<!-- sigma:dismissed-finding kind=blocked-by ref=#7 -->") == set()
    assert bc._dismissed_findings("garbage text with no marker at all") == set()
    assert bc._dismissed_findings("") == set()
    assert bc._dismissed_findings(None) == set()


def test_skill_md_dismiss_kind_doc_matches_the_real_internal_identifiers():
    # finding 2: SKILL.md used to claim <kind> "come[s] straight from the park reason" -- but the
    # posted park comment renders via _KIND_PHRASE's human phrasing ("blocked by #821", a space),
    # not the hyphenated identifier the regex actually requires ("blocked-by"). Pin the corrected
    # doc: every real internal kind identifier must be named (hyphenated) in the dismiss
    # instructions, and the old misleading claim must be gone.
    bc = _mod("backlog_check")
    # #1611 split this skill into SKILL.md + references/*.md; the drift claim is about the
    # SKILL, not which of its files holds the line. See tests/skill_corpus.py.
    text = skill_corpus("sigma-loop")
    for kind in bc._KIND_PHRASE:
        assert f"`{kind}`" in text, f"SKILL.md dismiss docs never name kind {kind!r}"
    assert "come straight from the park reason" not in text


# --- secret-safety, fail-open, determinism ---

def test_secret_shaped_token_never_reaches_the_pack():
    bc = _mod("backlog_check")
    secret = "AK" "IAABCDEFGHIJKLMNOP"
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, _GOAL, body="rotate " + secret),
                            _rec(2, _DUP, body="rotate " + secret)], **_LOOSE)
        pack = bc.cross_check(base, "1")
        blob = json.dumps(pack)
        # CASE-INSENSITIVE: the tokenizer lowercases, so a case-sensitive check would pass even on
        # reverted (leaky) code where the secret reaches evidence as `akiaabcdefghijklmnop`.
        assert secret.lower() not in blob.lower()
        assert any(f["ref"] == "2" for f in pack["findings"])   # the dup WAS found — the evidence path ran


def test_fail_open_empty_corpus_and_missing_goal():
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [], **_LOOSE)                     # empty mirror
        pack = bc.cross_check(base, "1")
        assert pack["findings"] == [] and "no_mirror" in pack["degraded"]
        assert "goal_not_in_corpus" in pack["degraded"]


def test_fail_open_never_raises_on_garbage():
    bc = _mod("backlog_check")
    pack = bc.cross_check("/nonexistent/nope", "1")
    assert pack["schema"] == "backlog-check/v1" and pack["findings"] == []


def test_github_corpus_skips_a_non_dict_record_not_the_whole_check():
    # a garbage mirror line (valid JSON, not an object) must degrade ONE record, not zero the check
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "state").mkdir(parents=True)
        (base / "config.json").write_text(json.dumps({"discovery": {"source": "github"},
                                                       "backlog_check": _LOOSE}))
        (base / "state" / "board-mirror.ndjson").write_text(
            "\n".join([json.dumps(_rec(1, _GOAL)), "null", "42", json.dumps(_rec(2, _DUP))]) + "\n")
        pack = bc.cross_check(str(base), "1")
        assert "error" not in pack["degraded"]                       # one bad line didn't zero it
        assert ("duplicate", "2") in {(f["kind"], f["ref"]) for f in pack["findings"]}


def test_bad_config_numeric_falls_back_to_default_not_disabled():
    # a hand-edited config typo (top_k: "all", dup_threshold: "high") degrades to the DEFAULTS, not an
    # error. Identical titles (cosine ~1.0) clear the default 0.72 dup_threshold, proving it was applied.
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, _GOAL), _rec(2, _GOAL)],
                        dup_threshold="high", top_k="all", closed_window_days=3650)
        pack = bc.cross_check(base, "1")
        assert "error" not in pack["degraded"]
        assert ("duplicate", "2") in {(f["kind"], f["ref"]) for f in pack["findings"]}   # default 0.72 applied


def test_candidate_gen_skips_zero_overlap_goal():
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, "totally unique wombat zeppelin"), _rec(2, _DUP)], **_LOOSE)
        assert bc.cross_check(base, "1")["findings"] == []   # nothing shares a term with the goal


def test_determinism():
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, _GOAL), _rec(2, _DUP), _rec(3, _GOAL + " variant")], **_LOOSE)
        assert bc.cross_check(base, "1") == bc.cross_check(base, "1")


# --- local-files mode ---

def _local_base(d, goals, **bc):
    base = pathlib.Path(d) / ".sdlc"; (base / "goals").mkdir(parents=True)
    (base / "config.json").write_text(json.dumps({"backlog_check": bc}))     # source defaults to local
    for name, status, title, body in goals:
        (base / "goals" / name).write_text(
            f"---\nid: {name[:-3]}\nstatus: {status}\ntitle: {title}\n---\n{body}\n")
    return str(base)


def test_local_mode_duplicate_and_obsolete():
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _local_base(d, [("0001.md", "pending", _GOAL, "do it"),
                               ("0002.md", "pending", _DUP, "same thing"),
                               ("0003.md", "done", _GOAL + " earlier", "already shipped")], **_LOOSE)
        (pathlib.Path(base) / "goals" / "README.md").write_text("just notes, no frontmatter")  # not a goal
        goal = str(pathlib.Path(base) / "goals" / "0001.md")
        kinds = {(f["kind"], pathlib.Path(f["ref"]).name) for f in bc.cross_check(base, goal)["findings"]}
        assert ("duplicate", "0002.md") in kinds
        assert ("obsoleted-by", "0003.md") in kinds          # a `done` goal obsoletes
        assert not any("README" in f["ref"] for f in bc.cross_check(base, goal)["findings"])  # README skipped


# --- velocity-scaled window ---

def test_closed_window_days_pinned_and_auto_and_fallback():
    bc = _mod("backlog_check")
    assert bc._closed_window_days({"backlog_check": {"closed_window_days": 45}}) == 45
    assert bc._closed_window_days({"backlog_check": {"closed_window_days": "60"}}) == 60
    # auto: invert an injected velocity (50 target / 5 prs_per_day = 10 days)
    fast = lambda days=30, run=None: {"prs_per_day": 5.0, "commits_per_day": 9.0}
    assert bc._closed_window_days({"backlog_check": {"closed_window_days": "auto"}}, velocity_measure=fast) == 10
    # rate 0 (fresh/non-git) -> fallback 90
    dead = lambda days=30, run=None: {"prs_per_day": 0, "commits_per_day": 0}
    assert bc._closed_window_days({"backlog_check": {"closed_window_days": "auto"}}, velocity_measure=dead) == 90
    # bool is an int subclass: True must be treated as unset ("auto"), NOT a 1-day window
    assert bc._closed_window_days({"backlog_check": {"closed_window_days": True}}, velocity_measure=dead) == 90


def test_closed_window_days_loads_the_real_velocity_module():
    # exercise the cross-skill _load_velocity() path (no velocity_measure injected) with a fake git
    # runner so it stays hermetic: 5 merges / 30 days = 0.17 prs/day -> 50/0.17 ≈ 294 -> clamped to 180
    bc = _mod("backlog_check")
    fake_git = lambda args: "\n".join(["h"] * (5 if "--merges" in args else 60))
    assert bc._closed_window_days({"backlog_check": {"closed_window_days": "auto"}}, run=fake_git) == 180


# --- CLI verb ---

# --- similarity: bm25 + hybrid embedding layer (0.9.23) ---

def test_bm25_similarity_still_finds_the_duplicate_not_the_distinct():
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, _GOAL), _rec(2, _DUP), _rec(3, _DISTINCT)],
                        similarity="bm25", dup_threshold=0.4, closed_window_days=3650)
        kinds = {(f["kind"], f["ref"]) for f in bc.cross_check(base, "1")["findings"]}
        assert ("duplicate", "2") in kinds and ("duplicate", "3") not in kinds


def test_embed_disabled_is_byte_identical_even_with_an_embedder_passed():
    # off by default: an embed_fn is ignored unless the config opts in -> identical pack
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, _GOAL), _rec(2, _DUP), _rec(3, _DISTINCT)], **_LOOSE)
        assert bc.cross_check(base, "1") == bc.cross_check(base, "1", embed_fn=lambda t: [1.0, 0.0])


def test_embed_catches_a_zero_lexical_overlap_paraphrase():
    bc = _mod("backlog_check")
    para = "relocate persistence subsystem for the gadget"          # shares NO token with _GOAL
    emb = lambda t: [1.0, 0.0] if ("widget" in t or "gadget" in t) else [0.0, 1.0]
    with tempfile.TemporaryDirectory() as d1:
        base = _gh_base(d1, [_rec(1, _GOAL), _rec(2, para)], dup_threshold=0.4, closed_window_days=3650)
        assert not [f for f in bc.cross_check(base, "1")["findings"] if f["ref"] == "2"]   # lexical misses it
    with tempfile.TemporaryDirectory() as d2:
        base = _gh_base(d2, [_rec(1, _GOAL), _rec(2, para)],
                        embed={"enabled": True, "weight": 1.0}, dup_threshold=0.4, closed_window_days=3650)
        assert any(f["ref"] == "2" for f in bc.cross_check(base, "1", embed_fn=emb)["findings"])   # dense catches it


def test_embed_cache_skips_reembedding_unchanged_texts():
    bc = _mod("backlog_check")
    calls = []
    def emb(text):
        calls.append(text)
        return [1.0, 0.0] if "widget" in text else [0.0, 1.0]
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, _GOAL), _rec(2, _DUP)],
                        embed={"enabled": True, "weight": 1.0}, dup_threshold=0.4, closed_window_days=3650)
        bc.cross_check(base, "1", embed_fn=emb)
        first = len(calls)
        assert first == 2                                  # both docs embedded on the first run
        bc.cross_check(base, "1", embed_fn=emb)
        assert len(calls) == first                         # second run reads the gitignored cache — no re-embed
        assert (pathlib.Path(base) / "state" / "embeddings.json").exists()


def test_embed_cache_is_keyed_by_embedder_identity_not_just_text():
    """#542: the embeddings cache used to key SOLELY on sha256(text) -- nothing tied a cached vector
    to the `embed.command` that produced it. Swapping the embedder (a provider/model change) while
    the corpus text stays exactly the same left the SECOND embedder silently served the FIRST
    embedder's stale vectors from the shared gitignored cache -- a meaningless cross-space cosine.
    The issue's own probe, reproduced directly: swap the injected embedder between two calls over
    the same unchanged corpus and confirm the second embedder is actually invoked, not skipped."""
    bc = _mod("backlog_check")
    calls_a, calls_b = [], []
    def emb_a(text):
        calls_a.append(text)
        return [1.0, 0.0]
    def emb_b(text):
        calls_b.append(text)
        return [0.0, 1.0]
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, _GOAL), _rec(2, _DUP)],
                        embed={"enabled": True, "command": "embedder-a", "weight": 1.0},
                        dup_threshold=0.4, closed_window_days=3650)
        bc.cross_check(base, "1", embed_fn=emb_a)
        assert len(calls_a) == 2                           # both docs embedded by embedder A

        # SAME corpus text, SAME .sdlc/state/ (so the gitignored cache persists), only the
        # embedder identity changes -- exactly a provider/model swap, config.json alone rewritten.
        (pathlib.Path(base) / "config.json").write_text(json.dumps(
            {"discovery": {"source": "github"},
             "backlog_check": {"embed": {"enabled": True, "command": "embedder-b", "weight": 1.0},
                               "dup_threshold": 0.4, "closed_window_days": 3650}}))
        bc.cross_check(base, "1", embed_fn=emb_b)
        # pre-fix: calls_b stays [] -- the cache (keyed on text alone) already has both docs from
        # embedder A and silently serves those stale vectors to embedder B's run instead.
        assert len(calls_b) == 2                           # embedder B is genuinely re-invoked, not skipped


def test_embed_cache_drops_superseded_identity_entries_on_write():
    """#624: a probe found superseded-identity entries persist in the cache FOREVER -- #542 made a
    swap self-invalidating (a new identity always re-embeds, never serves a stale cross-space
    vector), but never removed the old identity's now-dead entries, so the file only ever grows
    across embedder swaps. They are never looked up again (their hash key was computed under the
    old identity; a new identity computes a different key for the same text), so keeping them is
    pure dead weight. A write under a new identity now drops the superseded generation wholesale."""
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, _GOAL), _rec(2, _DUP)],
                        embed={"enabled": True, "command": "embedder-a", "weight": 1.0},
                        dup_threshold=0.4, closed_window_days=3650)
        bc.cross_check(base, "1", embed_fn=lambda t: [1.0, 0.0])
        cache_path = pathlib.Path(base) / "state" / "embeddings.json"
        before = json.loads(cache_path.read_text())["vectors"]
        assert len(before) == 2                                # both docs embedded under identity A

        # same corpus text, same .sdlc/state/ (gitignored cache persists) -- only the embedder
        # identity changes, exactly a provider/model swap.
        (pathlib.Path(base) / "config.json").write_text(json.dumps(
            {"discovery": {"source": "github"},
             "backlog_check": {"embed": {"enabled": True, "command": "embedder-b", "weight": 1.0},
                               "dup_threshold": 0.4, "closed_window_days": 3650}}))
        bc.cross_check(base, "1", embed_fn=lambda t: [0.0, 1.0])
        after = json.loads(cache_path.read_text())
        assert after["identity"] == "embedder-b"
        assert len(after["vectors"]) == 2                      # only B's 2 entries -- A's are gone, not appended
        assert not (set(before) & set(after["vectors"]))       # genuinely different keys (identity is in the hash)


def test_evict_oldest_drops_earliest_inserted_keys_first():
    """#624: pins the "oldest" in evict-oldest to INSERTION order specifically, not e.g. alphabetical
    key order -- chosen so the two would disagree (inserted first, sorts LAST alphabetically) if the
    implementation ever iterated a re-sorted view instead of the dict's own insertion order."""
    bc = _mod("backlog_check")
    cache = {}
    cache["zebra"] = 1                          # inserted first -> must be evicted first
    cache["apple"] = 2                          # inserted second, sorts first alphabetically
    cache["mango"] = 3                          # inserted third
    bc._evict_oldest(cache, 2)
    assert cache == {"apple": 2, "mango": 3}    # zebra (oldest) gone; alphabetical-first (apple) stays

    # a cache HIT (reassigning an EXISTING key) must not renew it -- FIFO, not LRU
    cache2 = {}
    cache2["first"], cache2["second"], cache2["third"] = 1, 2, 3
    cache2["first"] = 99                        # re-touch the oldest key; does not move it
    bc._evict_oldest(cache2, 2)
    assert cache2 == {"second": 2, "third": 3}  # "first" still evicted despite the re-touch


def test_embed_cache_caps_entries_and_evicts_oldest(monkeypatch):
    """#624: even within ONE identity, growth is otherwise unbounded (measured in the #584 review:
    ~1.45MB per 200 docs) -- a large or heavily-edited corpus (each edit mints a new hash key; the
    stale text's old entry is never looked up again either) never shrinks. Capped at a small N via
    monkeypatch for a fast, deterministic test. `test_evict_oldest_drops_earliest_inserted_keys_first`
    above pins the "oldest, not alphabetical" semantic directly; this test proves the cap is actually
    wired up through the real hash/read/write pipeline, across two separate cross_check calls (so it
    also exercises that the on-disk round-trip doesn't lose the ordering evict-oldest depends on)."""
    bc = _mod("backlog_check")
    monkeypatch.setattr(bc, "_EMBED_CACHE_MAX_ENTRIES", 2)
    identity = "embedder-a"
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, _GOAL), _rec(2, _DUP)],
                        embed={"enabled": True, "command": identity, "weight": 1.0},
                        dup_threshold=0.4, closed_window_days=3650)
        bc.cross_check(base, "1", embed_fn=lambda t: [1.0, 0.0])
        cache_path = pathlib.Path(base) / "state" / "embeddings.json"
        assert len(json.loads(cache_path.read_text())["vectors"]) == 2   # exactly at the cap, no eviction yet

        # #1's own hash key, computed exactly as _dense_channel does -- independent of whatever key
        # order the persisted file happens to expose, so this pins the doc embedded FIRST specifically.
        raw1 = bc.scrub(_GOAL) + "\n" + bc.scrub("")
        oldest_key = hashlib.sha256((identity + "\x00" + raw1).encode("utf-8")).hexdigest()[:16]

        # a genuinely new third doc pushes the cache past the cap
        (pathlib.Path(base) / "state" / "board-mirror.ndjson").write_text(
            "".join(json.dumps(r) + "\n" for r in [_rec(1, _GOAL), _rec(2, _DUP), _rec(3, _DISTINCT)]))
        bc.cross_check(base, "1", embed_fn=lambda t: [1.0, 0.0])
        vectors = json.loads(cache_path.read_text())["vectors"]
        assert len(vectors) == 2                                # capped, not 3
        assert oldest_key not in vectors                        # #1 (embedded first) was evicted, not #2's or #3's


def test_embed_enabled_without_command_falls_back_to_lexical_with_degraded():
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, _GOAL), _rec(2, _DUP)],
                        embed={"enabled": True}, dup_threshold=0.4, closed_window_days=3650)
        pack = bc.cross_check(base, "1")                   # no embed_fn, no command -> dense off
        assert "no_embedder" in pack["degraded"]
        assert ("duplicate", "2") in {(f["kind"], f["ref"]) for f in pack["findings"]}   # lexical still works


def test_embed_via_real_subprocess_command():
    # exercise _run_embedder + _embedder_from_config end-to-end (no injected embed_fn): a tiny embedder
    # that reads stdin and prints a JSON vector varying by content
    bc = _mod("backlog_check")
    cmd = ("python3 -c \"import sys,json; t=sys.stdin.read(); "
           "print(json.dumps([1.0,0.0] if 'widget' in t else [0.0,1.0]))\"")
    para = "relocate persistence subsystem for the widget engine"   # shares no token but embeds same
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["git", "init", "-q", d], check=True)
        subprocess.run(["git", "-C", d, "config", "--local",
                        "sigma.allowRepositoryShellCommands", "true"], check=True)
        base = _gh_base(d, [_rec(1, _GOAL), _rec(2, para)],
                        embed={"enabled": True, "command": cmd, "weight": 1.0},
                        dup_threshold=0.4, closed_window_days=3650)
        assert any(f["ref"] == "2" for f in bc.cross_check(base, "1")["findings"])


def test_embed_fusion_is_deterministic():
    bc = _mod("backlog_check")
    emb = lambda t: [1.0, 0.0] if "widget" in t else [0.5, 0.5]
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, _GOAL), _rec(2, _DUP)],
                        embed={"enabled": True, "weight": 0.5}, dup_threshold=0.4, closed_window_days=3650)
        assert bc.cross_check(base, "1", embed_fn=emb) == bc.cross_check(base, "1", embed_fn=emb)


# --- decide(): pure pack -> loop-hook action ---

def _mkpack(findings):
    return {"schema": "backlog-check/v1", "goal": "1", "findings": findings, "degraded": []}


def _f(kind, ref, score, confident, ev=("widget", "cache")):
    return {"kind": kind, "ref": ref, "score": score, "source": "mirror",
            "evidence": list(ev), "confident": confident}


def test_decide_parks_a_confident_finding_by_default():
    bc = _mod("backlog_check")
    d = bc.decide(_mkpack([_f("duplicate", "42", 0.85, True)]), {})
    assert d["action"] == "park" and "duplicate of #42" in d["reason"] and "widget" in d["reason"]
    assert d["note"] == ""


def test_decide_annotates_a_weak_finding():
    bc = _mod("backlog_check")
    d = bc.decide(_mkpack([_f("duplicate", "42", 0.6, False)]), {})
    assert d["action"] == "proceed" and "advisory" in d["note"] and "duplicate of #42" in d["note"]


def test_decide_proceeds_on_no_findings():
    bc = _mod("backlog_check")
    assert bc.decide(_mkpack([]), {}) == {"action": "proceed", "reason": "", "note": ""}


def test_decide_surfaces_a_goal_that_was_missing_from_the_corpus():
    # `goal_not_in_corpus` means NO finding could be computed for this goal at all — decide() never
    # read `degraded`, so that came back as a bare proceed with an empty note, indistinguishable from
    # a check that ran and found nothing. The no-op has to be visible, in the same advisory channel.
    bc = _mod("backlog_check")
    pack = {"schema": "backlog-check/v1", "goal": "42", "findings": [],
            "degraded": ["goal_not_in_corpus"]}
    d = bc.decide(pack, {})
    assert d["action"] == "proceed" and d["reason"] == ""     # never park: there is no evidence to park on
    assert "42" in d["note"] and "advisory" in d["note"]
    # ...and park mode does not turn a missing goal into a park either
    assert bc.decide(pack, {"backlog_check": {"action": "park"}})["action"] == "proceed"


def test_decide_flag_mode_never_parks_even_a_confident_hit():
    bc = _mod("backlog_check")
    d = bc.decide(_mkpack([_f("duplicate", "42", 0.95, True)]), {"backlog_check": {"action": "flag"}})
    assert d["action"] == "proceed" and "advisory" in d["note"]


def test_decide_summary_covers_multiple_kinds_secret_safe():
    bc = _mod("backlog_check")
    d = bc.decide(_mkpack([_f("obsoleted-by", "5", 0.8, True), _f("blocked-by", "7", 1.0, True, ev=[])]), {})
    assert "obsoleted by #5" in d["reason"] and "blocked by #7" in d["reason"]
    # a local-mode ref is a path; summary shows the stem, not the whole path
    d2 = bc.decide(_mkpack([_f("duplicate", "/tmp/x/.sdlc/goals/0002.md", 0.9, True)]), {})
    assert "duplicate of #0002.md" in d2["reason"] and "/tmp/x" not in d2["reason"]


def test_crosscheck_cli_in_process(capsys):
    bc, pl = _mod("backlog_check"), _mod("pipeline")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, _GOAL), _rec(2, _DUP)], **_LOOSE)
        assert pl.main(["pipeline.py", "crosscheck", base, "1"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["schema"] == "backlog-check/v1" and out["goal"] == "1"
        assert bc.main(["backlog_check.py", base, "1"]) == 0
        assert bc.main(["backlog_check.py", base]) == 2       # missing goal -> usage error
        capsys.readouterr()


# --------------------------------------------------------------------------- #462: create_tracked_issue's
# blocks_goal axis -- "dependency actually honored", not just recorded, proven in BOTH directions


def test_create_tracked_issue_dependency_is_honored_by_backlog_check_in_both_directions():
    """#462: create_tracked_issue's blocks_goal=True body marker isn't just WRITTEN -- a separate,
    realistic backlog_check run actually HONORS it (parks goal "10" while the tracked issue "11" is
    open) and correctly RELEASES it once "11" closes. Feeds the marker handoff.create_tracked_issue
    itself produced into the mirror fixture -- not a hand-typed guess at the format -- so this test
    would break if the two ever drifted (test_handoff.py's own
    test_handoff_narrative_wording_actually_matches_the_auto_skip_regex "import the real thing, don't
    hand-copy it" discipline, applied here to the fixture data instead of a regex)."""
    handoff = _mod("handoff")
    bc = _mod("backlog_check")

    # step 1: the real helper produces the marker + the new issue number -- captured, not guessed.
    with tempfile.TemporaryDirectory() as scratch:
        src = FakeSource(number="11")
        report = handoff.create_tracked_issue(
            scratch, {}, "10", "engine", "needs the widget cache migrated first",
            same_area=False, immediately_actionable=True, blocks_goal=True, source=src)
        assert report["issue"] == "11"
        body_goal, marker = src.body_appends[0]
        assert body_goal == "10" and marker == "**Blocked by:** #11"

    # step 2+3: feed the ACTUAL captured marker into the mirror fixture for goal "10", while "11" is
    # open -- a separate, realistic backlog-check run concludes "10" is blocked.
    with tempfile.TemporaryDirectory() as d1:
        base = _gh_base(d1, [
            _rec(10, _GOAL, body="do the migration\n\n" + marker),
            _rec(11, "the tracked issue", state="open"),
        ], **_LOOSE)
        pack = bc.cross_check(base, "10")
        blocked = [f for f in pack["findings"] if f["kind"] == "blocked-by" and f["ref"] == "11"]
        assert blocked and blocked[0]["confident"] is True
        assert bc.decide(pack, {})["action"] == "park"

    # step 4: flip "11" to closed, same goal body unchanged -- the block is correctly released, closing
    # the loop in the other direction too.
    with tempfile.TemporaryDirectory() as d2:
        base2 = _gh_base(d2, [
            _rec(10, _GOAL, body="do the migration\n\n" + marker),
            _rec(11, "the tracked issue", state="closed"),
        ], **_LOOSE)
        pack2 = bc.cross_check(base2, "10")
        assert not [f for f in pack2["findings"] if f["kind"] == "blocked-by" and f["ref"] == "11"]
        assert bc.decide(pack2, {})["action"] == "proceed"


def test_create_tracked_issue_with_blocks_goal_false_never_writes_a_body_marker():
    """#462, step 5, unit-level (not through the mirror fixture): the regression test for the
    false-blocking bug the blocks_goal axis exists to prevent. A non-blocking, merely-related finding
    (blocks_goal=False) must NEVER write the **Blocked by:** marker onto the current goal's body --
    otherwise backlog_check would incorrectly park unrelated work behind a finding that was never
    meant to gate it."""
    handoff = _mod("handoff")
    with tempfile.TemporaryDirectory() as scratch:
        src = FakeSource(number="12")
        report = handoff.create_tracked_issue(
            scratch, {}, "10", "engine", "just a related finding, not a blocker",
            same_area=False, immediately_actionable=False, blocks_goal=False, source=src)
        assert report["issue"] == "12"
        assert src.body_appends == []


def test_create_tracked_issue_non_blocking_cross_area_finding_never_parks_the_filing_goal_via_the_ledger():
    """PR #466 independent review: backlog_check._ledger_signals() is a SECOND, independent blocking
    mechanism from the body-marker/_explicit_blockers() channel the test above covers.
    _ledger_signals() treats any ledger.outstanding() entry (kind="handoff", not yet acked) as a
    confident block against the entry's own goal -- the side that filed it (#532). That makes this
    gate MORE load-bearing than when the block landed on handoff_key(): every kind="handoff" row now
    blocks the goal it names, so nothing softens a wrongly-written one. same_area=False,
    blocks_goal=False is a fully sanctioned "cross-area FYI, not a blocker" combination; before the
    fix it still wrote kind="handoff", so the ledger confident-blocked the FILING goal against its
    own unresolved entry -- exactly the false-blocking bug blocks_goal exists to prevent, reproduced
    here end-to-end with the real ledger and real backlog_check (not mocked)."""
    handoff = _mod("handoff")
    bc = _mod("backlog_check")

    class Degraded:
        """No create_dependency at all -- mirrors LocalSource, the kit's actual default source,
        which can never return a real issue number."""

    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(10, _GOAL, state="open")], **_LOOSE)
        cfg = {"ledger": {"enabled": True, "actor": "amy"}}
        report = handoff.create_tracked_issue(
            base, cfg, "10", "engine", "spotted while working this goal, not a blocker",
            same_area=False, immediately_actionable=True, blocks_goal=False, source=Degraded())
        assert report["issue"] is None            # the repro precondition: no real issue number

        pack = bc.cross_check(base, "10")
        blocked = [f for f in pack["findings"] if f["kind"] == "blocked-by"]
        assert blocked == [], f"the filing goal was incorrectly blocked by its own entry: {blocked}"
        assert bc.decide(pack, {})["action"] == "proceed"


# --- _referenced_blocker_refs (#1129: the auto-unpark sweep's "was this ever blocked-by anything at
# all" superset of _explicit_blockers' OPEN-only view) -------------------------------------------------


def test_referenced_blocker_refs_finds_a_ref_regardless_of_open_state():
    # unlike _explicit_blockers, this needs no `docs` corpus at all -- it never checks open/closed.
    bc = _mod("backlog_check")
    goal_doc = {"ref": "1", "raw": "some title\nblocked by #7 until the base lands"}
    assert bc._referenced_blocker_refs(goal_doc) == {"7"}


def test_referenced_blocker_refs_empty_when_no_marker_present():
    bc = _mod("backlog_check")
    goal_doc = {"ref": "1", "raw": "an ordinary goal with no dependency language at all"}
    assert bc._referenced_blocker_refs(goal_doc) == set()


def test_referenced_blocker_refs_scans_extra_text_too():
    # the auto-unpark sweep's primary real-world signal: the AUTOMATED park comment, not the body --
    # `_goal_comment_text`'s output arrives here as `extra_text`, same contract `_explicit_blockers`
    # already has.
    bc = _mod("backlog_check")
    goal_doc = {"ref": "1", "raw": "an otherwise unremarkable goal body"}
    extra = "Parked by Sigma — needs human review: backlog cross-check: blocked by #42 (1.0)"
    assert bc._referenced_blocker_refs(goal_doc, extra) == {"42"}


def test_referenced_blocker_refs_dedupes_a_ref_mentioned_twice():
    bc = _mod("backlog_check")
    goal_doc = {"ref": "1", "raw": "blocked by #7, and also depends on #7 landing first"}
    assert bc._referenced_blocker_refs(goal_doc) == {"7"}


def test_referenced_blocker_refs_collects_multiple_distinct_blockers():
    # two DISTINCT trigger phrases -- _BLOCK_RE matches one #N per trigger-phrase occurrence, so a
    # single "blocked by #7 and #12" phrase only ever yields one match.
    bc = _mod("backlog_check")
    goal_doc = {"ref": "1", "raw": "blocked by #7, depends on #12"}
    assert bc._referenced_blocker_refs(goal_doc) == {"7", "12"}


def test_referenced_blocker_refs_excludes_a_self_reference():
    bc = _mod("backlog_check")
    goal_doc = {"ref": "9", "raw": "blocked by #9"}     # malformed/self-referential input, defensive
    assert bc._referenced_blocker_refs(goal_doc) == set()


def test_referenced_blocker_refs_is_the_superset_explicit_blockers_narrows_from():
    # the exact distinction the auto-unpark sweep is built on: _explicit_blockers (OPEN-only) finds
    # nothing once the one referenced blocker is closed, while _referenced_blocker_refs still reports
    # it was referenced at all -- that gap is what tells the sweep "eligible", not "never blocked".
    bc = _mod("backlog_check")
    goal_doc = {"ref": "1", "raw": "blocked by #7"}
    docs = [{"ref": "7", "open": False}]                # #7 has since closed
    assert bc._referenced_blocker_refs(goal_doc) == {"7"}
    assert bc._explicit_blockers(goal_doc, docs) == []


# --- #1204: _docs_from_records -- the per-record normalizer factored out of _build_corpus's own
# github branch, so a caller with its OWN differently-scoped record set (mirror.
# fetch_dependency_records, unrestricted by assignee/label unlike the cached board-mirror.ndjson
# _build_corpus reads) can reuse the identical normalization without re-deriving it.

def test_docs_from_records_matches_what_build_corpus_derives_from_an_equivalent_mirror():
    bc = _mod("backlog_check")
    records = [_rec(1, _GOAL, "body one"), _rec(2, _DUP, "body two", state="closed",
                                                closed_at="2026-08-01T00:00:00Z")]
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, records, closed_window_days=3650)
        via_corpus, _ = bc._build_corpus(base, {"discovery": {"source": "github"}})
    via_records = bc._docs_from_records(records)
    assert via_records == via_corpus


def test_docs_from_records_skips_non_dict_rows_without_raising():
    bc = _mod("backlog_check")
    docs = bc._docs_from_records([_rec(1, _GOAL), None, "garbage", {"no": "number"}])
    # a dict with no usable "number" key still normalizes (str(None) -> "None") -- only a
    # non-dict row is dropped outright, same tolerance _build_corpus's own loop already has
    assert [d["ref"] for d in docs if d["ref"] != "None"] == ["1"]


def test_build_corpus_github_branch_delegates_to_docs_from_records():
    """Not just equal OUTPUT (the test above already proves that) -- this pins that _build_corpus
    actually CALLS the shared helper, so the two can never independently drift apart again."""
    bc = _mod("backlog_check")
    calls = []
    real = bc._docs_from_records

    def spy(recs):
        calls.append(list(recs))
        return real(recs)

    bc._docs_from_records = spy
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, _GOAL)], closed_window_days=3650)
        bc._build_corpus(base, {"discovery": {"source": "github"}})
    assert len(calls) == 1


# --- #1393: what `_BLOCK_RE` matches, and -- just as importantly -- what it still gets wrong ------


def test_block_re_boundary_table():
    """The pinned, honest behaviour of the blocker regex, including its RESIDUE.

    A dependency is inferred from prose, so both error directions exist and neither is solvable
    with a pattern. #1393 removed the clause-boundary class of false positive (a trigger word and a
    `#N` on opposite sides of a comma or full stop are a separate remark, not a dependency) and
    left the rest, which is shape-indistinguishable from a real edge.

    This table is the documentation as much as the test: a future tightening that breaks a REAL row
    is caught here, and a future loosening that revives a FALSE row is too."""
    bc = _mod("backlog_check")
    real = [
        "**Blocked by:** #123",          # the canonical marker every Sigma writer emits
        "Blocked by #99",
        "this depends on #7",
        "- #12 blocked by #7",
        "requires the new parser in #7",
        "waiting on #7",
    ]
    fixed = [                            # were false positives before #1393, now correctly ignored
        "waiting on the design sign-off, see #1234",
        "we need this after the release. see #77",
    ]
    residue = [                          # STILL false positives -- documented, not pretended away
        "after #40 lands",
        "not a real dependency -- this actually needs #9 to land first",
    ]
    for text in real:
        assert bc._BLOCK_RE.search(text), f"real dependency no longer detected: {text!r}"
    for text in fixed:
        assert not bc._BLOCK_RE.search(text), f"clause-boundary false positive is back: {text!r}"
    for text in residue:
        assert bc._BLOCK_RE.search(text), (
            f"residue row {text!r} stopped matching -- if that was deliberate, move it to `fixed` "
            "and update the caveat in the SKILL docs, which currently tells adopters it matches")


def test_the_canonical_marker_survives_every_writer_that_emits_it():
    """Three modules independently emit `**Blocked by:** #N` (handoff, triage, compile_plan) with no
    shared constant between them. The tightened class must not break any of them -- and the colon
    and asterisks it contains are exactly the kind of punctuation a careless tightening would."""
    bc = _mod("backlog_check")
    for n in ("1", "42", "1234"):
        m = bc._BLOCK_RE.search(f"**Blocked by:** #{n}")
        assert m and m.group(2) == n


# --- #1487: a dependency marker sitting PAST the mirror's 500-char body excerpt -------------------
# `compile_plan._append_extra` writes `**Blocked by:** #N` at the END of a body. `mirror.py` stores
# only the first `_EXCERPT_CHARS` of it, and `_docs_from_records` builds `raw` from that excerpt --
# so on any issue with a body longer than the cap, `_BLOCK_RE` had nothing left to match and the
# dependency was silently unenforced. Measured on the real repo: `next-batch` claimed 8 goals, 7 of
# which had an open, unstarted prerequisite.

_PAD = "padding sentence about the widget cache and the acme storage backend. " * 12   # >500 chars


def _long_body_with_trailing_marker(blocker, prefix="Context for this goal.\n\n"):
    """A body shaped exactly like a `compile_plan`-created sub-issue: real content first, the
    canonical `**Blocked by:** #N` marker LAST, total length comfortably past `_EXCERPT_CHARS`."""
    body = prefix + _PAD + "\n\n**Blocked by:** #" + str(blocker)
    assert len(body) > _mod("mirror")._EXCERPT_CHARS, "fixture no longer exceeds the excerpt cap"
    return body


def _gh_row(number, title, body="", state="OPEN", updated="2026-08-01T00:00:00Z"):
    """A raw `gh issue list --json` row -- fed through the REAL `mirror.build_records`, never a
    hand-built record, so the truncation this test is about is the one production performs."""
    return {"number": number, "title": title, "body": body, "labels": [], "state": state,
            "closedAt": None, "updatedAt": updated}


def _mirror_records_for_1487():
    mirror = _mod("mirror")
    recs = mirror.build_records(
        [_gh_row(1, _GOAL, _long_body_with_trailing_marker(7)),
         _gh_row(7, "build the acme storage adapter", "the prerequisite, still open")], [])
    goal_rec = next(r for r in recs if r["number"] == 1)
    # the precondition the whole bug rests on: the marker is genuinely gone from the excerpt
    assert "#7" not in goal_rec["body_excerpt"], "fixture: the marker survived truncation"
    return recs


def test_explicit_blocker_survives_the_mirror_excerpt_truncation():
    """THE repro. A goal whose `**Blocked by:** #7` sits past character 500 must still be reported
    as blocked by the open #7."""
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, _mirror_records_for_1487(), **_LOOSE)
        pack = bc.cross_check(base, "1", run=lambda *a, **k: "{}")
        blocked = [f for f in pack["findings"] if f["kind"] == "blocked-by"]
        assert [f["ref"] for f in blocked] == ["7"], pack
        assert blocked[0]["confident"] is True


def test_referenced_blocker_refs_survives_the_mirror_excerpt_truncation():
    """The superset view `auto_unpark`/`unpark` read must see the truncated-away ref too -- else a
    goal parked on it looks like it was "never blocked by anything" and the sweep skips it."""
    bc = _mod("backlog_check")
    goal_doc = next(d for d in bc._docs_from_records(_mirror_records_for_1487()) if d["ref"] == "1")
    assert bc._referenced_blocker_refs(goal_doc) == {"7"}


def test_a_pre_1487_mirror_record_degrades_instead_of_raising():
    """A `board-mirror.ndjson` written before this change has NO `blocker_refs` key at all. The
    reader must fall back to exactly the pre-#1487 behaviour -- the excerpt scan alone -- never
    raise, and never invent an edge. (`mirror.is_fresh` bounds that degradation to one pick by
    refusing an older-schema mirror outright; this pins the reader half.)"""
    bc = _mod("backlog_check")
    old_rec = _rec(1, _GOAL, body="blocked by #7")          # `_rec` predates the field, deliberately
    assert "blocker_refs" not in old_rec
    doc = bc._docs_from_records([old_rec])[0]
    assert bc._record_blocker_refs(doc) == []
    assert bc._referenced_blocker_refs(doc) == {"7"}        # the excerpt scan still works, unchanged
    assert [f["ref"] for f in bc._explicit_blockers(doc, [{"ref": "7", "open": True}])] == ["7"]


def test_record_blocker_refs_tolerates_every_malformed_shape():
    """Defensive: a hand-edited or hand-built mirror. A bare ref keeps its edge with no evidence
    phrase; garbage, non-numeric refs and a self-reference are dropped."""
    bc = _mod("backlog_check")
    doc = {"ref": "1", "raw": "", "blocker_refs": [
        {"phrase": "blocked by", "ref": "7"},   # the real shape
        "12",                                   # a bare string ref (an older/hand-built writer)
        42,                                     # a bare int ref
        {"ref": "1"},                           # self-reference -> dropped
        {"ref": "abc"},                         # non-numeric -> dropped
        {"phrase": "needs"},                    # no ref at all -> dropped
        None, ["nope"],                         # not a record at all -> dropped
    ]}
    assert bc._record_blocker_refs(doc) == [("7", "blocked by"), ("12", ""), ("42", "")]


def test_record_blocker_refs_survives_a_non_list_field():
    bc = _mod("backlog_check")
    assert bc._record_blocker_refs({"ref": "1", "blocker_refs": "blocked by #7"}) == []
    assert bc._record_blocker_refs({"ref": "1", "blocker_refs": None}) == []
    assert bc._record_blocker_refs({"ref": "1"}) == []


def test_a_marker_inside_the_excerpt_is_reported_exactly_once():
    """The no-regression half of #1487: when the marker DOES fit inside the excerpt, both the
    haystack scan and the record refs name it. The record path must add nothing -- one finding, one
    piece of evidence, byte-identical to before."""
    bc = _mod("backlog_check")
    doc = {"ref": "1", "raw": "a title\n**Blocked by:** #7", "body": "**Blocked by:** #7",
           "blocker_refs": [{"phrase": "blocked by", "ref": "7"}]}
    found = bc._explicit_blockers(doc, [{"ref": "7", "open": True}])
    assert len(found) == 1 and found[0]["ref"] == "7" and found[0]["evidence"] == ["blocked by"]


def test_a_record_ref_that_has_since_closed_is_not_a_blocker():
    """The open-only precision rule applies to the record path identically -- a closed ref is not a
    blocker -- while `_referenced_blocker_refs` still reports it was referenced at all. That gap is
    exactly what `auto_unpark` reads, so the superset relationship must survive the new channel."""
    bc = _mod("backlog_check")
    doc = {"ref": "1", "raw": "a title\nno marker in the excerpt at all",
           "blocker_refs": [{"phrase": "blocked by", "ref": "7"}]}
    assert bc._explicit_blockers(doc, [{"ref": "7", "open": False}]) == []
    assert bc._referenced_blocker_refs(doc) == {"7"}
    assert [f["ref"] for f in bc._explicit_blockers(doc, [{"ref": "7", "open": True}])] == ["7"]


def test_record_refs_never_bypass_the_self_reference_guard():
    bc = _mod("backlog_check")
    doc = {"ref": "9", "raw": "a title", "blocker_refs": [{"phrase": "blocked by", "ref": "9"}]}
    assert bc._explicit_blockers(doc, [{"ref": "9", "open": True}]) == []
    assert bc._referenced_blocker_refs(doc) == set()


def test_record_refs_do_not_leak_into_the_similarity_channel():
    """`blocker_refs` rides ALONGSIDE `raw`, never inside it -- `raw` feeds TF-IDF and the embedding
    channel for every doc in the corpus, so folding refs back in as synthetic text would silently
    move dedup/obsolescence scores."""
    bc = _mod("backlog_check")
    recs = _mirror_records_for_1487()
    goal = next(d for d in bc._docs_from_records(recs) if d["ref"] == "1")
    assert "#7" not in goal["raw"] and "#7" not in goal["body"]
    assert goal["tokens"] == bc._doc_tokens(goal["title"], goal["body"])


def test_the_block_vocabulary_is_declared_only_in_blocker_scan():
    """#1487 moved `_BLOCK_RE` into a leaf module so `mirror.py`'s fetch-time scan and this
    module's check-time scan can never diverge -- `mirror` is the LOWER module and cannot import
    this one, so a shared home was the only alternative to a second copy.

    Identity is vacuous as a pin (`re.compile` caches, so a hand-retyped copy IS the same object --
    the lesson test_triage.py already learned), so pin it at the SOURCE level with that module's own
    paren-balanced scanner: neither reader may carry the block vocabulary in a `re.compile()` call
    of its own."""
    from test_triage import _re_compile_call_bodies
    import re as _re
    vocab = _re.compile(r"\b(blocked by|depends on|depends upon|requires|waiting on)\b")
    for name in ("backlog_check.py", "mirror.py"):
        for call in _re_compile_call_bodies((S / name).read_text(encoding="utf-8")):
            assert not vocab.search(call), f"{name} re-declares the block vocabulary: {call!r}"


def test_the_unpark_qa_markers_are_declared_only_in_blocker_scan():
    """Same seam, the other half: #1392's fenced span must be stripped identically by both scans,
    so the two literals live in `blocker_scan.py` and each reader re-exports them."""
    bs = _mod("blocker_scan")
    for name in ("backlog_check.py", "mirror.py"):
        src = (S / name).read_text(encoding="utf-8")
        assert bs.UNPARK_QA_START not in src and bs.UNPARK_QA_END not in src, name


def test_the_check_time_scan_keeps_the_full_trigger_vocabulary():
    """#1487 review: the fetch-time scan narrowed to the explicit phrases; the CHECK-time one must
    not have moved with it. A weak trigger inside the excerpt is found today and must still be
    found -- narrowing both would have been a silent regression on every issue under 500 chars."""
    bc = _mod("backlog_check")
    for phrase in ("needs", "after", "requires", "waiting on", "depends on", "blocked by"):
        doc = {"ref": "1", "raw": f"a title\nthe rollout {phrase} #7 to land", "blocker_refs": []}
        assert [f["ref"] for f in bc._explicit_blockers(doc, [{"ref": "7", "open": True}])] == ["7"], phrase
        assert bc._referenced_blocker_refs(doc) == {"7"}, phrase


def test_check_time_confidence_splits_on_the_matched_phrase():
    """#1497: presence is unchanged (the test above), but a weak trigger ("needs", "after",
    "requires", "waiting on") must never be `confident` on its own -- only a canonical marker
    (`blocker_scan.EXPLICIT_TRIGGERS`: "blocked by" / "depends on" / "depends upon") is. A confident
    check-time finding reaches `_resolve_blockers_for_park` -> `blockers.resolve`, which WRITES to
    the referenced issue; a weak trigger inside the excerpt is prose, not a declaration."""
    bc = _mod("backlog_check")
    for phrase in ("needs", "after", "requires", "waiting on"):
        doc = {"ref": "1", "raw": f"a title\nthe rollout {phrase} #7 to land", "blocker_refs": []}
        f = next(x for x in bc._explicit_blockers(doc, [{"ref": "7", "open": True}]) if x["ref"] == "7")
        assert f["confident"] is False, phrase       # PRESENT, but no longer confident
    for phrase in ("blocked by", "depends on", "depends upon"):
        doc = {"ref": "1", "raw": f"a title\nthe rollout {phrase} #7 to land", "blocker_refs": []}
        f = next(x for x in bc._explicit_blockers(doc, [{"ref": "7", "open": True}]) if x["ref"] == "7")
        assert f["confident"] is True, phrase         # canonical marker: unchanged


def test_check_time_weak_trigger_does_not_park_but_stays_advisory():
    """The actual acceptance bar: `decide()` must not park on a prose-shaped match alone, and the
    finding must still surface -- as the documented `(advisory)` note -- rather than being silently
    dropped. Mirrors `test_dismissed_finding_does_not_flip_decide_to_park`'s shape for the new case."""
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, "wire the surface", body="this actually needs #7 to land first"),
                            _rec(7, "freeze the contract")], **_LOOSE)
        pack = bc.cross_check(base, "1")
        f = next(x for x in pack["findings"] if x["kind"] == "blocked-by" and x["ref"] == "7")
        assert f["confident"] is False
        decision = bc.decide(pack, {})
        assert decision["action"] == "proceed"
        assert "advisory" in decision["note"] and "#7" in decision["note"]


def test_a_ref_confirmed_by_a_canonical_marker_elsewhere_stays_confident():
    """A ref matched TWICE in the same body -- once by a weak trigger, once by a canonical marker --
    is confident: an unrelated prose mention of an issue must not weaken a genuine declaration of the
    same issue sitting elsewhere in the same text."""
    bc = _mod("backlog_check")
    doc = {"ref": "1", "raw": "mentioned again after #7 shipped, but this work is Blocked by #7",
           "blocker_refs": []}
    f = next(x for x in bc._explicit_blockers(doc, [{"ref": "7", "open": True}]) if x["ref"] == "7")
    assert f["confident"] is True


# --- #1497 sub-classes, pinned from the real board text that produced them (measured on this
# repo's own backlog while confirming the issue) -----------------------------------------------

def test_label_vocabulary_sub_class_is_not_confident():
    """Sub-class 1: the label vocabulary itself is a trigger. `sdlc:needs-confirmation` contains the
    weak trigger "needs", and a body naming the label near a reference (the shape of two real
    issues on this board, both auditing `sdlc:*` label combinations) reads as a dependency
    assertion purely off that literal string -- nothing about it expresses a real blocker."""
    bc = _mod("backlog_check")
    doc = {"ref": "1",
           "raw": ("a title\ncarries sdlc:needs-confirmation (or sdlc:proposed if #7 "
                   "hasn't merged yet when you pick this up)"),
           "blocker_refs": []}
    f = next(x for x in bc._explicit_blockers(doc, [{"ref": "7", "open": True}]) if x["ref"] == "7")
    assert f["confident"] is False


def test_quoted_park_boilerplate_sub_class_is_not_confident():
    """Sub-class 2: a body that QUOTES Sigma's own park-comment boilerplate -- e.g. to document
    the #1186-class phantom-blocker bug in a table -- re-asserts whatever the quoted example
    referenced. The boilerplate itself contains "needs" (`sources.py`'s park-comment prefix); a body
    merely quoting it to explain the bug must not spawn a confident blocker off the quoted example."""
    bc = _mod("backlog_check")
    doc = {"ref": "1",
           "raw": ("a title\n| park comment | result |\n|---|---|\n"
                   "| `Parked by Sigma -- needs human review: PR #7 is not approved yet` | "
                   "phantom blocker `#7` |"),
           "blocker_refs": []}
    found = [x for x in bc._explicit_blockers(doc, [{"ref": "7", "open": True}]) if x["ref"] == "7"]
    assert found and found[0]["confident"] is False


def test_a_weak_trigger_past_the_excerpt_cap_is_the_one_case_that_changed():
    """The whole behaviour delta of the review fix, stated once, end to end through the real mirror:
    the same weak-trigger sentence produces a finding INSIDE the excerpt and none past it. That
    asymmetry is deliberate -- see blocker_scan.TRIGGERS -- and this is what pins it."""
    bc, m = _mod("backlog_check"), _mod("mirror")
    prose = "and a real user hit it after Phase 2 (#7) went live"
    inside = m.normalize_issue({"number": 1, "title": _GOAL, "state": "OPEN", "body": prose,
                                "updatedAt": "2026-08-01T00:00:00Z"})
    past = m.normalize_issue({"number": 1, "title": _GOAL, "state": "OPEN",
                              "body": _long_body_with_trailing_marker(7).replace(
                                  "**Blocked by:** #7", prose),
                              "updatedAt": "2026-08-01T00:00:00Z"})
    corpus = [{"ref": "7", "open": True}]
    assert [f["ref"] for f in bc._explicit_blockers(bc._docs_from_records([inside])[0], corpus)] == ["7"]
    assert bc._explicit_blockers(bc._docs_from_records([past])[0], corpus) == []


def test_a_declared_blocker_absent_from_the_corpus_is_reported_unresolved():
    """#1420: a blocker the corpus cannot see is named in `degraded`, not skipped silently; a goal
    with no marker gets no such entry."""
    bc = _mod("backlog_check")
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, "goal one", body="x\n\n**Blocked by:** #4242\n"),
                            _rec(2, "goal two", body="plain")])
        assert "blocker_unresolved:4242" in bc.cross_check(base, "1")["degraded"]
        assert not [x for x in bc.cross_check(base, "2")["degraded"]
                    if x.startswith("blocker_unresolved")]


def _issue_run(states):
    """A fake `gh` transport serving single-issue GETs from {ref: state}; anything else raises."""
    def run(args):
        ref = args[1].rsplit("/", 1)[-1]
        if ref not in states:
            raise RuntimeError("no such issue")
        return json.dumps({"number": int(ref), "state": states[ref]})
    return run


def test_a_declared_blocker_closed_outside_the_mirror_window_is_not_reported_unresolved():
    """#1422: a blocker the corpus cannot see but GitHub says is closed is dropped from `degraded`;
    an open one and an unreadable one stay reported; with no transport nothing is read."""
    bc = _mod("backlog_check")
    body = "x\n\n**Blocked by:** #4242\n**Blocked by:** #4243\n**Blocked by:** #4244\n"
    with tempfile.TemporaryDirectory() as d:
        base = _gh_base(d, [_rec(1, "goal one", body=body)])
        run = _issue_run({"4242": "closed", "4243": "open"})
        got = [x for x in bc.cross_check(base, "1", run=run)["degraded"]
               if x.startswith("blocker_unresolved")]
        assert got == ["blocker_unresolved:4243", "blocker_unresolved:4244"]
        bare = [x for x in bc.cross_check(base, "1")["degraded"]
                if x.startswith("blocker_unresolved")]
        assert len(bare) == 3
