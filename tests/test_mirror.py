"""Local board mirror (mirror.py, slice 0.9.20): a token-free, gitignored snapshot of the GitHub
backlog for the cross-check. Reaches GitHub only through an injectable runner, so every test here is
hermetic — no network, no `gh`. Deterministic, $0."""
import json, pathlib, importlib.util, tempfile

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def _gh_runner(open_payload, closed_payload, me="me-login"):
    """Fake `gh`: returns the closed payload for a `state=closed` REST fetch, else the open
    payload; a plain login string for the `gh api user` call #1833's assignee resolution makes
    when the configured assignee is the literal "@me"."""
    calls = []
    def run(args):
        calls.append(list(args))
        if len(args) > 1 and args[0] == "api" and args[1] == "user":
            return me
        return json.dumps(closed_payload if "state=closed" in " ".join(args) else open_payload)
    run.calls = calls
    return run


def _is_issues_list_call(a):
    """#1833: true for the `gh api repos/{owner}/{repo}/issues` REST shape mirror.py now builds."""
    return len(a) > 1 and a[0] == "api" and str(a[1]).startswith("repos/") and str(a[1]).endswith("/issues")


def _rest_field(a, key):
    """The value of a `gh api -f key=value` field, or None if `key` was never passed."""
    prefix = key + "="
    return next((v[len(prefix):] for v in a if isinstance(v, str) and v.startswith(prefix)), None)


def _github_cfg(**gh):
    return {"discovery": {"source": "github", "github": gh}}


# --- normalization is pure + secret-safe ---

def test_normalize_issue_shapes_and_scrubs():
    m = _mod("mirror")
    rec = m.normalize_issue({
        "number": 42, "title": "Fix the AK" "IAABCDEFGHIJKLMNOP leak", "state": "OPEN",
        "body": "steps: password: hunter2xyz then ship", "closedAt": None,
        "updatedAt": "2026-08-01T10:00:00Z",
        "labels": [{"name": "sdlc:goal"}, {"name": "area:ui"}, {"other": "x"}]})
    assert rec["number"] == 42 and rec["state"] == "open"
    assert "AK" "IAABCDEFGHIJKLMNOP" not in rec["title"] and "hunter2xyz" not in rec["body_excerpt"]
    assert rec["labels"] == ["sdlc:goal", "area:ui"]          # malformed label object dropped
    assert len(rec["content_hash"]) == 16 and int(rec["content_hash"], 16) >= 0   # 16 hex chars


def test_build_records_dedup_open_wins_and_sorted_and_skips_malformed():
    m = _mod("mirror")
    open_raw = [{"number": 5, "title": "open five", "state": "open"},
                {"number": 3, "title": "open three", "state": "open"}]
    closed_raw = [{"number": 5, "title": "closed five", "state": "closed"},   # clashes with open #5
                  {"title": "no number"}]                                     # malformed -> skipped
    recs = m.build_records(open_raw, closed_raw)
    assert [r["number"] for r in recs] == [3, 5]              # sorted, malformed dropped
    assert next(r for r in recs if r["number"] == 5)["state"] == "open"   # open wins the clash


# --- the fetch orchestrator ---

def test_fetch_writes_ndjson_and_meta_under_gitignored_state():
    m = _mod("mirror")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "state").mkdir(parents=True)
        run = _gh_runner([{"number": 7, "title": "open goal", "state": "open",
                           "labels": [{"name": "sdlc:goal"}], "updatedAt": "2026-08-01T00:00:00Z"}],
                         [{"number": 4, "title": "done work", "state": "closed",
                           "closedAt": "2026-07-30T00:00:00Z", "updatedAt": "2026-07-30T00:00:00Z"}])
        n = m.fetch_and_write(str(base), config=_github_cfg(repo="acme/widget"), run=run, now=1000.0)
        assert n == 2
        recs = m.read_mirror(str(base))
        assert {r["number"] for r in recs} == {4, 7}
        # exactly two issues-listing calls: one open (goal-labelled), one closed (not goal-filtered)
        # -- #1833: repo is embedded in the REST endpoint path itself, no separate --repo flag.
        list_calls = [c for c in run.calls if _is_issues_list_call(c)]
        assert any(_rest_field(c, "labels") == "sdlc:goal" and _rest_field(c, "state") == "open"
                   for c in list_calls)
        assert any(_rest_field(c, "state") == "closed" for c in list_calls)
        assert all(str(c[1]) == "repos/acme/widget/issues" for c in list_calls)
        meta = json.loads((base / m.META_REL).read_text())
        assert meta["count"] == 2 and meta["mirrored_at"] == 1000.0 and meta["schema"] == m.SCHEMA
        assert m.MIRROR_REL.startswith("state/") and m.META_REL.startswith("state/")   # gitignored dir


def test_assignee_scopes_the_open_query_only():
    """#1833: REST's `assignee=` has no `"@me"` alias of its own (a hard 422, confirmed live by
    #1829's own research) -- the configured "@me" is resolved to a real login (`_gh_runner`'s own
    `gh api user` response) via `resolve_assignee_login` before it ever reaches the query."""
    m = _mod("mirror")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "state").mkdir(parents=True)
        run = _gh_runner([], [])
        m.fetch_and_write(str(base), config=_github_cfg(assignee="@me"), run=run, now=1.0)
        list_calls = [c for c in run.calls if _is_issues_list_call(c)]
        open_calls = [c for c in list_calls if _rest_field(c, "state") == "open"]
        assert open_calls and all(_rest_field(c, "assignee") == "me-login" for c in open_calls)
        closed_calls = [c for c in list_calls if _rest_field(c, "state") == "closed"]
        assert closed_calls and not any(_rest_field(c, "assignee") for c in closed_calls)   # closed net is wide


def test_open_query_pages_from_the_oldest_end_like_next_pending():
    # F12/#348 one layer down: a plain `gh issue list --limit 200` is created-DESC, so on a backlog
    # over the cap the mirror held the NEWEST 200 while `next_pending` picks the OLDEST — the goal
    # just picked was systematically ABSENT from its own corpus and the cross-check silently no-opped
    # on exactly the goals that had waited longest. Same qualifier, same end of the queue.
    m = _mod("mirror")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "state").mkdir(parents=True)
        run = _gh_runner([], [])
        m.fetch_and_write(str(base), config=_github_cfg(), run=run, now=1.0)
        list_calls = [c for c in run.calls if _is_issues_list_call(c)]
        assert any(_rest_field(c, "state") == "open" and _rest_field(c, "sort") == "created"
                   and _rest_field(c, "direction") == "asc" for c in list_calls)
        # the closed net keeps its OWN, deliberately different recency sort — not collateral damage
        assert any(_rest_field(c, "state") == "closed" and _rest_field(c, "sort") == "updated"
                   and _rest_field(c, "direction") == "desc" for c in list_calls)


def test_local_mode_writes_no_mirror():
    m = _mod("mirror")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "state").mkdir(parents=True)
        called = _gh_runner([], [])
        assert m.fetch_and_write(str(base), config={"discovery": {"source": "local-goals"}},
                                 run=called, now=1.0) is None
        assert called.calls == [] and not (base / m.MIRROR_REL).exists()


def test_fetch_and_write_never_uses_the_graphql_search_field():
    """#1833: `fetch_and_write` was the second call site #1829's own research explicitly
    enumerated as graphql-search-billed by the same mechanism as the pick path (the open query's
    `--label`, AND the closed query's bare `--search`, which routes there by construction with no
    label at all) but left out of scope for being off the loop's own pick path. Must never
    construct that shape again -- both queries are now the same REST fetch the pick path uses."""
    m = _mod("mirror")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "state").mkdir(parents=True)
        run = _gh_runner([], [])
        m.fetch_and_write(str(base), config=_github_cfg(repo="acme/widget"), run=run, now=1.0)
        assert not any(len(c) > 1 and c[0] == "issue" and c[1] == "list" for c in run.calls)
        list_calls = [c for c in run.calls if _is_issues_list_call(c)]
        assert len(list_calls) == 2   # exactly one open, one closed
        assert not any("--search" in c or "--label" in c for c in list_calls)


def test_fetch_fail_open_on_gh_error():
    m = _mod("mirror")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "state").mkdir(parents=True)
        def boom(args):
            raise RuntimeError("gh not authenticated")
        assert m.fetch_and_write(str(base), config=_github_cfg(), run=boom, now=1.0) is None
        assert not (base / m.MIRROR_REL).exists()            # nothing half-written


def test_fetch_fail_open_on_bad_closed_limit():
    # a hand-edited config typo must not crash the pick path: "closed_limit": "all" -> None, not ValueError
    m = _mod("mirror")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "state").mkdir(parents=True)
        run = _gh_runner([{"number": 1, "title": "a", "state": "open"}], [])
        cfg = _github_cfg(); cfg["backlog_check"] = {"mirror": {"closed_limit": "all"}}
        assert m.fetch_and_write(str(base), config=cfg, run=run, now=1.0) is None
        assert not (base / m.MIRROR_REL).exists()


def test_fetch_fail_open_on_non_list_gh_payload_does_not_clobber():
    # a gh error object ({"message": "Not Found"}) is not a backlog: return None, leave any prior mirror intact
    m = _mod("mirror")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "state").mkdir(parents=True)
        good = _gh_runner([{"number": 1, "title": "keep me", "state": "open"}], [])
        m.fetch_and_write(str(base), config=_github_cfg(), run=good, now=1.0, force=True)
        before = (base / m.MIRROR_REL).read_text()
        def err(args):
            return json.dumps({"message": "Not Found"})          # valid JSON, not a list
        assert m.fetch_and_write(str(base), config=_github_cfg(), run=err, now=2.0, force=True) is None
        assert (base / m.MIRROR_REL).read_text() == before        # prior good mirror untouched


def test_fetch_fail_open_on_well_formed_empty_result_does_not_clobber(capsys):
    # #1496: unlike the error-object case above, a rate-limited or scope-reduced `gh` can SUCCEED
    # with an empty list -- valid JSON, valid list, zero issues. The isinstance guard alone lets that
    # straight through to `_write`, so a genuine "no answer" clobbers a good, populated mirror with
    # nothing, and a cleared mirror reads to `backlog_check` as "checked, found nothing" rather than
    # "could not check". This pins that a well-formed empty result is refused exactly like the error
    # object is: prior mirror kept byte-identical, meta's `mirrored_at` left untouched so the next
    # pass retries instead of caching the emptiness for a full TTL window, and the reason lands on
    # stderr. Breaking the `if not records:` guard (e.g. deleting it, or writing unconditionally)
    # turns this red: the mirror would come back empty and the meta timestamp would advance to 2.0.
    m = _mod("mirror")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "state").mkdir(parents=True)
        good = _gh_runner([{"number": 1, "title": "keep me", "state": "open"}], [])
        assert m.fetch_and_write(str(base), config=_github_cfg(), run=good, now=1.0, force=True) == 1
        before_mirror = (base / m.MIRROR_REL).read_text()
        before_meta = (base / m.META_REL).read_text()
        capsys.readouterr()   # discard output from the seeding fetch above
        empty = _gh_runner([], [])
        assert m.fetch_and_write(str(base), config=_github_cfg(), run=empty, now=2.0, force=True) is None
        assert (base / m.MIRROR_REL).read_text() == before_mirror   # prior good mirror untouched
        assert (base / m.META_REL).read_text() == before_meta       # timestamp untouched -> next pass retries
        assert "zero issues" in capsys.readouterr().err


def test_first_ever_fetch_returning_empty_writes_no_mirror_at_all():
    # The same guard with no prior mirror to protect: a brand-new install whose first fetch comes
    # back with zero issues (transient or a genuinely empty board) must not plant an empty mirror +
    # fresh meta, which would look identical to a checked-and-confirmed-empty board for a full TTL.
    m = _mod("mirror")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "state").mkdir(parents=True)
        empty = _gh_runner([], [])
        assert m.fetch_and_write(str(base), config=_github_cfg(), run=empty, now=1.0) is None
        assert not (base / m.MIRROR_REL).exists()
        assert not (base / m.META_REL).exists()


def test_build_records_skips_non_dict_rows_without_raising():
    # a stray null / string row inside an otherwise-valid list is skipped, the good rows survive
    m = _mod("mirror")
    recs = m.build_records([{"number": 2, "title": "ok", "state": "open"}, None, "garbage"], [])
    assert [r["number"] for r in recs] == [2]


def test_fetch_fail_open_on_unreadable_config():
    # config=None + a garbage config.json on disk -> _load_config returns {} -> not github mode -> None
    m = _mod("mirror")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "state").mkdir(parents=True)
        (base / "config.json").write_text("{ not json")
        assert m.fetch_and_write(str(base), now=1.0) is None


def test_the_backlog_read_itself_never_runs_the_mirror():
    """The half of the 0.9.22 guarantee that has not moved: `sources.next_pending` -- the query the
    picker runs against GitHub -- is untouched by the mirror. Whatever reads the mirror does so from
    `loop.py`, at a point of its own choosing, never inside the backlog read."""
    assert "fetch_and_write" not in (S / "sources.py").read_text()


def test_every_mirror_call_site_in_the_loop_is_behind_its_own_gate():
    """0.9.22's OTHER half said the mirror is reached from exactly one place -- `precheck` -- and
    therefore "a default config never mirrors". #1499 DELIBERATELY ENDS THAT, and the change is
    worth stating rather than quietly editing: the pick-time dependency gate reads the mirror on a
    STOCK config, because `backlog_check.enabled` ships FALSE and a correctness guard built on a
    subsystem that ships disabled is not a guard (`reconcile.py`'s module docstring records the same
    lesson from two earlier attempts at that mistake).

    What survives is the part that actually protected adopters: no call site may be unguarded. Each
    is preceded by its own cheap, local gate -- `backlog_check.enabled is True` for the cross-check,
    `_dependency_gate_mode` + `mirror.is_github_mode` for the pick gate -- and `fetch_and_write` is
    itself TTL-guarded, so the steady-state cost of the new site is a stat, not a fetch (pinned
    against the real picker by `test_a_fresh_mirror_costs_the_gate_no_gh_calls_when_nothing_is_held`).

    FOUR sites since #1650, not three: the pick gate now FORCES a refresh before it reports a hold,
    because `ttl_minutes` ships at 60 and a blocker an operator had just closed by hand went on
    holding its dependents for up to an hour. It sits behind the same `_dependency_gate_mode` gate
    as the other two, and behind a narrower one of its own -- it is reached only when the answer is
    a hold, which is the only case where a stale record changes the outcome."""
    loop_txt = (S / "loop.py").read_text()
    sites = [i for i in range(len(loop_txt)) if loop_txt.startswith(".fetch_and_write(", i)]
    assert len(sites) == 4, "a new mirror call site appeared in loop.py — gate it and pin it here"
    cross_check_gate = loop_txt.index('backlog_check") or {}).get("enabled") is not True')
    pick_gate = loop_txt.index('if _dependency_gate_mode(config) == "off":')
    for site in sites:
        assert cross_check_gate < site or pick_gate < site, \
            "a mirror fetch that no gate precedes"


def test_ttl_skips_refetch_until_forced():
    m = _mod("mirror")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "state").mkdir(parents=True)
        run = _gh_runner([{"number": 1, "title": "a", "state": "open"}], [])
        assert m.fetch_and_write(str(base), config=_github_cfg(), run=run, now=1000.0) == 1
        first = len(run.calls)
        # 30s later, TTL 60min default -> still fresh -> skip (no new gh calls)
        assert m.fetch_and_write(str(base), config=_github_cfg(), run=run, now=1030.0) is None
        assert len(run.calls) == first
        # force bypasses freshness
        assert m.fetch_and_write(str(base), config=_github_cfg(), run=run, now=1030.0, force=True) == 1
        assert len(run.calls) > first


def test_secret_never_lands_in_the_mirror_file():
    m = _mod("mirror")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "state").mkdir(parents=True)
        run = _gh_runner([{"number": 9, "title": "rotate creds", "state": "open",
                           "body": "aws AK" "IAABCDEFGHIJKLMNOP and ghp_" + "z" * 30,
                           "updatedAt": "2026-08-01T00:00:00Z"}], [])
        m.fetch_and_write(str(base), config=_github_cfg(), run=run, now=1.0)
        raw = (base / m.MIRROR_REL).read_text()
        assert "AK" "IAABCDEFGHIJKLMNOP" not in raw and "ghp_zzz" not in raw and "REDACTED" in raw


def test_read_mirror_roundtrip_and_fail_open():
    m = _mod("mirror")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "state").mkdir(parents=True)
        assert m.read_mirror(str(base)) == []                # absent -> empty
        (base / m.MIRROR_REL).write_text(
            json.dumps({"number": 1, "title": "ok"}) + "\n" + "{not json}\n" + "\n")
        recs = m.read_mirror(str(base))
        assert [r["number"] for r in recs] == [1]            # garbage line skipped


def test_records_and_file_are_deterministic():
    m = _mod("mirror")
    raw = [{"number": 2, "title": "b", "state": "open", "updatedAt": "t"},
           {"number": 1, "title": "a", "state": "open", "updatedAt": "t"}]
    assert m.build_records(raw, []) == m.build_records(raw, [])
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "state").mkdir(parents=True)
        run = _gh_runner(raw, [])
        m.fetch_and_write(str(base), config=_github_cfg(), run=run, now=5.0, force=True)
        first = (base / m.MIRROR_REL).read_text()
        m.fetch_and_write(str(base), config=_github_cfg(), run=run, now=5.0, force=True)
        assert (base / m.MIRROR_REL).read_text() == first    # byte-identical for the same snapshot


def test_mirror_location_is_covered_by_runtime_ignores():
    m = _mod("mirror")
    setup_path = (pathlib.Path(__file__).resolve().parent.parent
                  / "skills" / "sigma-setup" / "scripts" / "setup.py")
    spec = importlib.util.spec_from_file_location("setup_mod", setup_path)
    setup = importlib.util.module_from_spec(spec); spec.loader.exec_module(setup)
    # RUNTIME_IGNORES entries are like ".sdlc/state/"; the mirror rel path (state/…) must fall under one
    rels = [ig.split(".sdlc/", 1)[-1] for ig in setup.RUNTIME_IGNORES]
    assert any(m.MIRROR_REL.startswith(r) for r in rels)


# --- #1204: fetch_dependency_records -- the file-time dedup corpus, deliberately UNSCOPED by
# discovery.github.assignee and covering every label a caller asks for (goal_label AND
# sdlc:needs-confirmation), unlike fetch_and_write's own open query above. Independent of, and never
# touches, the shared board-mirror.ndjson.

def test_fetch_dependency_records_never_scopes_by_assignee():
    m = _mod("mirror")
    run = _gh_runner([{"number": 1, "title": "a", "state": "open"}], [])
    recs = m.fetch_dependency_records(
        "irrelevant", config=_github_cfg(assignee="@me"), run=run, labels=["sdlc:goal"])
    list_calls = [c for c in run.calls if _is_issues_list_call(c)]
    assert any(_rest_field(c, "labels") == "sdlc:goal" for c in list_calls)
    assert not any(_rest_field(c, "assignee") for c in list_calls)   # the whole point of #1204
    assert not any(len(c) > 1 and c[0] == "api" and c[1] == "user" for c in run.calls)   # never resolved
    assert [r["number"] for r in recs] == [1]


def test_fetch_dependency_records_queries_every_label_and_merges_dedupes():
    m = _mod("mirror")
    calls = []

    def run(args):
        calls.append(list(args))
        if not _is_issues_list_call(args):
            return "[]"
        if _rest_field(args, "state") == "closed":
            return "[]"
        if _rest_field(args, "labels") == "sdlc:goal":
            return json.dumps([{"number": 1, "title": "goal issue", "state": "open"},
                               {"number": 2, "title": "shared issue", "state": "open"}])
        if _rest_field(args, "labels") == "sdlc:needs-confirmation":
            return json.dumps([{"number": 2, "title": "shared issue", "state": "open"},
                               {"number": 3, "title": "queued follow-up", "state": "open"}])
        return "[]"

    recs = m.fetch_dependency_records(
        "irrelevant", config=_github_cfg(), run=run, labels=["sdlc:goal", "sdlc:needs-confirmation"])
    assert {r["number"] for r in recs} == {1, 2, 3}            # merged, #2 (both labels) not duplicated
    label_calls = [c for c in calls if _is_issues_list_call(c) and _rest_field(c, "state") == "open"]
    assert any(_rest_field(c, "labels") == "sdlc:goal" for c in label_calls)
    assert any(_rest_field(c, "labels") == "sdlc:needs-confirmation" for c in label_calls)


def test_fetch_dependency_records_includes_closed_issues_with_no_label_restriction():
    m = _mod("mirror")
    run = _gh_runner([{"number": 1, "title": "open one", "state": "open"}],
                     [{"number": 9, "title": "closed nine", "state": "closed"}])
    recs = m.fetch_dependency_records(
        "irrelevant", config=_github_cfg(), run=run, labels=["sdlc:goal"])
    assert {r["number"] for r in recs} == {1, 9}
    closed_calls = [c for c in run.calls if _is_issues_list_call(c) and _rest_field(c, "state") == "closed"]
    assert closed_calls and not any(_rest_field(c, "labels") for c in closed_calls)


def test_fetch_dependency_records_not_github_mode_returns_none():
    m = _mod("mirror")

    def boom(args):
        raise AssertionError("must never call gh in local mode")

    assert m.fetch_dependency_records(
        "irrelevant", config={"discovery": {"source": "local-goals"}}, run=boom,
        labels=["sdlc:goal"]) is None


def test_fetch_dependency_records_fail_open_on_gh_error():
    m = _mod("mirror")

    def boom(args):
        raise RuntimeError("gh not authenticated")

    assert m.fetch_dependency_records(
        "irrelevant", config=_github_cfg(), run=boom, labels=["sdlc:goal"]) is None


def test_fetch_dependency_records_shape_matches_build_records():
    """The returned records must be normalize_issue-shaped -- the same shape build_records()
    produces -- so a downstream consumer (backlog_check._docs_from_records) can treat either
    source identically without knowing which path it took."""
    m = _mod("mirror")
    run = _gh_runner([{"number": 5, "title": "t", "state": "open", "body": "b",
                       "updatedAt": "2026-08-01T00:00:00Z"}], [])
    recs = m.fetch_dependency_records(
        "irrelevant", config=_github_cfg(), run=run, labels=["sdlc:goal"])
    assert recs[0].keys() == {"number", "title", "body_excerpt", "blocker_refs", "labels", "state",
                              "closed_at", "updated_at", "content_hash"}


def test_mirror_cli_in_process_local_mode_is_noop(capsys):
    m = _mod("mirror")
    pl = _mod("pipeline")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "state").mkdir(parents=True)
        (base / "config.json").write_text(json.dumps({"discovery": {"source": "local-goals"}}))
        assert pl.main(["pipeline.py", "mirror", str(base)]) == 0
        assert "skipped" in capsys.readouterr().out
        assert m.main(["mirror.py", str(base)]) == 0
        assert "skipped" in capsys.readouterr().out


# --- #1487: blocker refs are scanned off the FULL body, not the 500-char excerpt -------------------
# The excerpt is sized for "enough for an optional rerank" and similarity scoring degrades gracefully
# when it truncates. A regex for a SPECIFIC `#N` does not degrade -- it returns nothing -- and
# `compile_plan` writes `**Blocked by:** #N` at the END of a body, so every issue over the cap had
# its dependency silently unenforced.

_PAD = "padding sentence about the widget cache and the acme storage backend. " * 12   # >500 chars


def test_normalize_issue_extracts_blocker_refs_from_the_full_body_not_the_excerpt():
    m = _mod("mirror")
    body = "Context.\n\n" + _PAD + "\n\n**Blocked by:** #7"
    assert len(body) > m._EXCERPT_CHARS
    rec = m.normalize_issue({"number": 3, "title": "a sub-issue", "state": "OPEN", "body": body,
                             "updatedAt": "2026-08-01T00:00:00Z"})
    assert "#7" not in rec["body_excerpt"]                    # the excerpt still truncates, unchanged
    assert rec["blocker_refs"] == [{"phrase": "blocked by", "ref": "7"}]


def test_normalize_issue_blocker_refs_are_empty_without_a_marker():
    m = _mod("mirror")
    rec = m.normalize_issue({"number": 3, "title": "t", "state": "OPEN",
                             "body": "an ordinary body mentioning #9 with no dependency phrasing",
                             "updatedAt": "2026-08-01T00:00:00Z"})
    assert rec["blocker_refs"] == []


def test_normalize_issue_blocker_refs_drops_a_self_reference():
    m = _mod("mirror")
    rec = m.normalize_issue({"number": 9, "title": "t", "state": "OPEN", "body": "blocked by #9",
                             "updatedAt": "2026-08-01T00:00:00Z"})
    assert rec["blocker_refs"] == []


def test_normalize_issue_blocker_refs_ignores_an_unpark_qa_span():
    """#1392's phantom-blocker trap, at the new scan site. The Q&A span a human's `/sigma-unpark`
    answers land in is ordinary English; scanning the FULL body reaches spans the truncated excerpt
    never even contained, so the strip has to apply here too."""
    bs = _mod("blocker_scan")
    m = _mod("mirror")
    body = ("real content\n" + bs.UNPARK_QA_START + "\nwaiting on the design sign-off see #1234\n"
            + bs.UNPARK_QA_END + "\n**Blocked by:** #7")
    rec = m.normalize_issue({"number": 3, "title": "t", "state": "OPEN", "body": body,
                             "updatedAt": "2026-08-01T00:00:00Z"})
    assert rec["blocker_refs"] == [{"phrase": "blocked by", "ref": "7"}]


def test_blocker_refs_never_carry_body_text_past_the_excerpt_cap():
    """The mirror deliberately refuses to store a raw body. `blocker_refs` reads more of the body
    than the record stores, so it must persist ONLY a number and a closed-set trigger phrase."""
    m = _mod("mirror")
    body = ("x" * m._EXCERPT_CHARS) + "\npassword: hunter2xyz aws AK" "IAABCDEFGHIJKLMNOP\nblocked by #7"
    rec = m.normalize_issue({"number": 3, "title": "t", "state": "OPEN", "body": body,
                             "updatedAt": "2026-08-01T00:00:00Z"})
    blob = json.dumps(rec["blocker_refs"])
    assert "hunter2xyz" not in blob and "AKIA" not in blob
    assert rec["blocker_refs"] == [{"phrase": "blocked by", "ref": "7"}]


def test_is_fresh_refuses_a_mirror_written_by_an_older_schema():
    """A pre-#1487 mirror is not stale by AGE -- it can be seconds old -- but its records have no
    `blocker_refs`, and the reader degrades silently rather than raising. Refusing it forces exactly
    one refetch on upgrade instead of gating on nothing for a full TTL."""
    m = _mod("mirror")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "state").mkdir(parents=True)
        meta = base / m.META_REL
        meta.write_text(json.dumps({"schema": m.SCHEMA, "mirrored_at": 1000.0}))
        assert m.is_fresh(str(base), 60, now=1030.0) is True
        meta.write_text(json.dumps({"schema": "board-mirror/v1", "mirrored_at": 1000.0}))
        assert m.is_fresh(str(base), 60, now=1030.0) is False
        meta.write_text(json.dumps({"mirrored_at": 1000.0}))          # oldest shape: no schema key
        assert m.is_fresh(str(base), 60, now=1030.0) is False


def test_an_older_schema_mirror_is_actually_refetched_not_just_reported_stale():
    """The behavioural half: `fetch_and_write` must go back to `gh` and REWRITE the file, so the
    stale, blocker_refs-less records are replaced rather than left in place."""
    m = _mod("mirror")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "state").mkdir(parents=True)
        (base / m.MIRROR_REL).write_text(
            json.dumps({"number": 1, "title": "old", "body_excerpt": "", "labels": [],
                        "state": "open", "closed_at": None, "updated_at": "", "content_hash": "x"})
            + "\n")
        (base / m.META_REL).write_text(json.dumps({"schema": "board-mirror/v1",
                                                   "mirrored_at": 1000.0}))
        run = _gh_runner([{"number": 1, "title": "old", "state": "open", "body": "blocked by #7",
                           "updatedAt": "2026-08-01T00:00:00Z"}], [])
        assert m.fetch_and_write(str(base), config=_github_cfg(), run=run, now=1030.0) == 1
        assert m.read_mirror(str(base))[0]["blocker_refs"] == [{"phrase": "blocked by", "ref": "7"}]


# --- #1487 review (blocking): the fetch-time scan is NARROWER than the check-time one --------------
# `_BLOCK_RE`'s trigger set and its 40-char window were calibrated (#1393) against a 500-character
# excerpt. 395 of 400 real issues exceed that cap, so pointing the same vocabulary at whole bodies
# grew the scanned surface ~10x and turned the documented prose residue into live confident edges:
# 22 new edges on a production-realistic corpus, 21 genuine `blocked by` markers and one phantom off
# `after`. A confident `blocked-by` is not advisory -- `loop.py`'s `_resolve_blockers_for_park` hands
# it to `blockers.resolve`, which relabels, moves and comments on the REFERENCED issue. So fetch time
# takes `EXPLICIT_TRIGGERS` only; check time keeps the full set unchanged.

_REAL_FALSE_POSITIVE = "that scope boundary wasn't surfaced until a real user hit it after Phase 2 (#1396) went live"


def _past_the_cap(tail, lead="Context for this goal.\n\n"):
    """`tail` placed past `_EXCERPT_CHARS`, so only a full-body scan can ever reach it."""
    body = lead + _PAD + "\n\n" + tail
    assert len(lead + _PAD) > _mod("mirror")._EXCERPT_CHARS, "fixture: the tail is inside the excerpt"
    return body


def _refs_at_fetch_time(tail, number=3):
    m = _mod("mirror")
    return m.normalize_issue({"number": number, "title": "a goal", "state": "OPEN",
                              "body": _past_the_cap(tail),
                              "updatedAt": "2026-08-01T00:00:00Z"})["blocker_refs"]


def test_narrative_prose_past_the_cap_produces_no_fetch_time_edge():
    """THE blocking case, verbatim from the review's measurement against issue #1397. Both
    directions in one test: the full vocabulary DOES match this sentence -- so the fixture is live
    and the narrowing is what drops it, not a typo that stopped matching anything."""
    bs = _mod("blocker_scan")
    assert bs._BLOCK_RE.search(_REAL_FALSE_POSITIVE), "fixture no longer trips the full vocabulary"
    assert bs._BLOCK_RE.search(_REAL_FALSE_POSITIVE).group(2) == "1396"
    assert _refs_at_fetch_time(_REAL_FALSE_POSITIVE) == []


def test_every_weak_trigger_is_dropped_at_fetch_time_and_still_matches_at_check_time():
    bs = _mod("blocker_scan")
    for phrase in ("needs", "after", "requires", "waiting on"):
        text = f"the rollout {phrase} #7 to be looked at some day"
        assert bs._BLOCK_RE.search(text), f"check-time vocabulary lost {phrase!r}"
        assert _refs_at_fetch_time(text) == [], f"{phrase!r} still produces a fetch-time edge"


def test_every_explicit_trigger_still_produces_a_fetch_time_edge():
    """The other direction: the narrowing must not have cost a single genuine marker. Includes the
    canonical `**Blocked by:** #N` literal that `compile_plan`, `handoff` and `triage` all write."""
    for tail, phrase in (("**Blocked by:** #7", "blocked by"),
                         ("Blocked by #7", "blocked by"),
                         ("this depends on #7", "depends on"),
                         ("this depends upon #7", "depends upon")):
        assert _refs_at_fetch_time(tail) == [{"phrase": phrase, "ref": "7"}], tail


def test_the_narrow_trigger_set_is_a_prefix_subset_of_the_full_one():
    """Structural, so the two can never be edited apart: the narrow pattern is the full pattern with
    its alternation restricted. Same character class, same proximity window, same capture groups --
    which is what makes a fetch-time match always a check-time match too."""
    bs = _mod("blocker_scan")
    assert set(bs.EXPLICIT_TRIGGERS) < set(bs.TRIGGERS)
    assert bs.EXPLICIT_TRIGGERS == bs.TRIGGERS[:len(bs.EXPLICIT_TRIGGERS)]
    assert bs._EXPLICIT_BLOCK_RE.pattern == bs._BLOCK_RE.pattern.replace(
        "|".join(bs.TRIGGERS), "|".join(bs.EXPLICIT_TRIGGERS))
    assert bs._EXPLICIT_BLOCK_RE.flags == bs._BLOCK_RE.flags


def test_the_check_time_pattern_is_byte_identical_to_the_calibrated_literal():
    """#1393 calibrated this exact string, and #1487 both moved it to another module and rebuilt it
    from a tuple. Pinned against the literal so neither step could have altered check-time
    behaviour -- a stronger claim than "the tests still pass"."""
    bs = _mod("blocker_scan")
    assert bs._BLOCK_RE.pattern == (
        r"(?i)\b(blocked by|depends on|depends upon|needs|after|requires|waiting on)"
        r"\b[^\n#,;.!?]{0,40}?#(\d+)")


def test_a_fetch_time_edge_is_always_a_check_time_edge_too():
    """The invariant the prefix-subset construction buys: the narrow scan can never assert a
    dependency the full vocabulary would not also see. Run over the whole #1393 boundary table plus
    the explicit forms, so it covers the punctuation and proximity edges, not just happy paths."""
    bs = _mod("blocker_scan")
    corpus = [
        "**Blocked by:** #123", "Blocked by #99", "this depends on #7", "this depends upon #7",
        "requires the new parser in #7", "waiting on #7", "- #12 blocked by #7",
        "waiting on the design sign-off, see #1234", "we need this after the release. see #77",
        "after #40 lands", "not a real dependency -- this actually needs #9 to land first",
        _REAL_FALSE_POSITIVE, "BLOCKED BY #5", "blocked by #7 and depends on #12",
        "blocked by the parser, see #7", "unblocked by #7", "blocked by #  7", "#7 blocked by #8",
    ]
    for text in corpus:
        narrow = {r["ref"] for r in bs.extract_refs(text)}
        full = {m.group(2) for m in bs._BLOCK_RE.finditer(text)}
        assert narrow <= full, f"fetch-time scan invented an edge the full vocabulary rejects: {text!r}"


def test_extract_refs_dedupes_a_ref_named_by_two_phrases():
    """F2: `extract_refs`'s docstring promises an "ORDERED, ref-deduplicated list", and 6 real
    issues on this board carry two dependency phrasings for the same ref. Without the guard the
    mirror stores duplicate records; downstream absorbs it, so nothing else would ever catch it."""
    bs = _mod("blocker_scan")
    assert bs.extract_refs("blocked by #7, and this also depends on #7 landing first") == [
        {"phrase": "blocked by", "ref": "7"}]
    # ordered, and only the DUPLICATE is collapsed -- a second distinct ref still lands, after it
    assert bs.extract_refs("depends on #12 and blocked by #7 and depends upon #12") == [
        {"phrase": "depends on", "ref": "12"}, {"phrase": "blocked by", "ref": "7"}]


def test_normalize_issue_scans_the_title_as_well_as_the_body():
    """F3-minor: the scanned text is `title + "\\n" + body`, mirroring `_docs_from_records`' own
    `raw`. Unpinned until now -- dropping the title left the suite green."""
    m = _mod("mirror")
    rec = m.normalize_issue({"number": 3, "title": "port the adapter, blocked by #7", "state": "OPEN",
                             "body": "no marker down here", "updatedAt": "2026-08-01T00:00:00Z"})
    assert rec["blocker_refs"] == [{"phrase": "blocked by", "ref": "7"}]
    # and the join is a NEWLINE, so the title's last word can never pair with the body's first `#N`
    # ([^\n...] cannot cross it) -- the reason `_docs_from_records` builds `raw` the same way
    spanning = m.normalize_issue({"number": 3, "title": "this depends on", "state": "OPEN",
                                  "body": "#7 is unrelated", "updatedAt": "2026-08-01T00:00:00Z"})
    assert spanning["blocker_refs"] == []


def test_content_hash_ignores_blocker_refs():
    """F3: the decision not to fold `blocker_refs` into `content_hash`, pinned. Two issues with the
    same title, the same first 500 characters and the same `updated_at`, differing only in a marker
    past the cap: different refs, SAME hash. Nothing under `skills/` reads `content_hash` today, so
    without this assert either choice was green -- and the field's shape is what a future consumer
    inherits."""
    m = _mod("mirror")
    def rec(tail):
        return m.normalize_issue({"number": 3, "title": "a goal", "state": "OPEN",
                                  "body": _past_the_cap(tail), "updatedAt": "2026-08-01T00:00:00Z"})
    a, b = rec("**Blocked by:** #7"), rec("**Blocked by:** #8")
    assert a["blocker_refs"] != b["blocker_refs"]
    assert a["content_hash"] == b["content_hash"]
    assert a["body_excerpt"] == b["body_excerpt"]          # the reason the hash is allowed to match


def test_nothing_under_skills_reads_content_hash():
    """The load-bearing half of the corrected rationale above: the previous comment named a consumer
    ("the embedding/tokenizing layer") that does not exist. If one ever appears, this fails and the
    `content_hash` decision has to be re-argued rather than inherited."""
    root = S.parent.parent.parent
    hits = [str(f.relative_to(root)) for f in (root / "skills").rglob("*.py")
            if "content_hash" in f.read_text(encoding="utf-8") and f.name != "mirror.py"]
    assert hits == [], f"content_hash gained a reader: {hits}"


def test_age_seconds_reports_the_mirror_age_and_None_when_there_is_none():
    """#1650: the pick gate declines to claim on the strength of a mirror record, and when it cannot
    refresh that record first it has to be able to SAY how old the evidence is. `is_fresh` already
    computes this number and throws it away against a TTL; this exposes it.

    None, never a number, for the three shapes a real `.sdlc` can hold: no meta file at all, an
    unreadable one, and a timestamp in the FUTURE (a clock adjustment, a copied `.sdlc`) — a
    negative age printed as "-93 minute(s) ago" is worse than admitting the age is unknown."""
    m = _mod("mirror")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "state").mkdir(parents=True)
        assert m.age_seconds(str(base)) is None                  # nothing written yet
        run = _gh_runner([{"number": 1, "title": "a", "state": "open"}], [])
        m.fetch_and_write(str(base), config=_github_cfg(), run=run, now=1000.0)
        assert m.age_seconds(str(base), now=1000.0) == 0
        assert m.age_seconds(str(base), now=3520.0) == 2520.0    # 42 minutes
        assert m.age_seconds(str(base), now=900.0) is None       # a future timestamp is not an age
        (base / "state" / "board-mirror.meta.json").write_text("{not json")
        assert m.age_seconds(str(base)) is None


# --- #1608: an other-repository reference is not a local blocker -----------------------------------

def test_extract_refs_skips_an_other_repository_reference():
    bs = _mod("blocker_scan")
    both = "Blocked by: other-org/other-repo#41\nBlocked by: #42\n"
    assert bs.extract_refs("Blocked by: other-org/other-repo#41") == []
    assert [r["ref"] for r in bs.extract_refs("Blocked by: #42")] == ["42"]
    assert [r["ref"] for r in bs.extract_refs(both)] == ["42"]


import pytest


@pytest.mark.parametrize("text,refs", [
    ("blocked by repo#7", []),
    ("blocked by other-org/other-repo#7", []),
    ("blocked by my_repo#7", []),
    ("blocked by #7", ["7"]),
    ("blocked by repo#7 and depends on #9", ["9"]),
])
def test_extract_refs_cross_repo_forms(text, refs):
    bs = _mod("blocker_scan")
    assert [r["ref"] for r in bs.extract_refs(text)] == refs


def test_a_v2_schema_mirror_is_refused_once_and_refetched():
    m = _mod("mirror")
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d) / ".sdlc"; (base / "state").mkdir(parents=True)
        meta = base / m.META_REL
        meta.write_text(json.dumps({"schema": "board-mirror/v2", "mirrored_at": 1000.0}))
        assert m.is_fresh(str(base), 60, now=1030.0) is False
        meta.write_text(json.dumps({"schema": m.SCHEMA, "mirrored_at": 1000.0}))
        assert m.is_fresh(str(base), 60, now=1030.0) is True
