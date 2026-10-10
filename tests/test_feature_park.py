"""#946 (part B, slice 6): Level 3 -- the park outcome, the capped brief, per-conflict findings, the no-goal filing
path, the filed store with an issue number, closing through `gh_api`, and the controls around them.

Everything runs offline: real git in scratch repositories, a recording stand-in for the issue filer, and a runner that
answers `gh` calls from a list. With the upkeep gate closed every existing path is byte-identical (one test below)."""
import copy
import importlib.util
import json
import pathlib

import pytest

import test_feature_rebase as base

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"
DOCTOR = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-doctor" / "scripts" / "doctor.py"


def _load(name, path=None):
    spec = importlib.util.spec_from_file_location(name, path or SCRIPTS / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


park = _load("feature_park")
legacy = _load("legacy")
handoff = _load("handoff")
ledger = _load("ledger")
OPEN = {"upkeep": {"enabled": True}}


def _cfg(extra=None):
    cfg = base._cfg()
    cfg.update(copy.deepcopy(extra or {}))
    return cfg


def _conflicted_world(tmp_path):
    world = base.World(tmp_path).build(
        feature_commits=(("seed.txt", "feature edit", "feat: touch the seed (#11)"),))
    world.move_integration(name="seed.txt", body="integration edit")
    return world


# --------------------------------------------------------------------------- identity


def test_conflict_id_is_stable_per_conflict_and_differs_per_commit_or_file_set():
    one = park.conflict_id("billing", "abc123", "s", "4", ["b.txt", "a.txt"])
    assert one == park.conflict_id("billing", "abc123", "s", "4", ["a.txt", "b.txt"])   # path order is irrelevant
    assert one == park.conflict_id("BILLING", "abc123", "other", "9", ["a.txt", "b.txt"])  # not the subject, not the tip
    assert one != park.conflict_id("billing", "abc124", "s", "4", ["a.txt", "b.txt"])
    assert one != park.conflict_id("billing", "abc123", "s", "4", ["a.txt"])
    assert one != park.conflict_id("other", "abc123", "s", "4", ["a.txt", "b.txt"])
    merge_a = park.conflict_id("billing", "", "Merge x", "7", ["a.txt"])
    assert merge_a == park.conflict_id("billing", "  ", "Merge x", "7", ["a.txt"])
    assert merge_a != park.conflict_id("billing", "", "Merge x", "8", ["a.txt"])      # a merge: subject AND position


# --------------------------------------------------------------------------- quoted text is untrusted


@pytest.mark.parametrize("marker", sorted(legacy.MARKERS))
def test_neutralise_breaks_every_registered_marker_in_either_spelling(marker):
    for spelled in legacy.spellings(marker):
        quoted = "see %s here" % spelled
        assert legacy.has_marker(quoted, marker)            # the control: the reader honours the raw text
        assert not legacy.has_marker(park.neutralise(quoted), marker), spelled
    assert park.neutralise(park.neutralise("x " + marker)) == park.neutralise("x " + marker)   # idempotent


def test_neutralise_withholds_kit_paths_and_keeps_ordinary_ones():
    text = park.neutralise("edit skills/sigma-loop/scripts/x.py and hooks/h.sh and src/app.py and .claude-plugin/p.json")
    assert "skills/" not in text and "hooks/" not in text and ".claude-plugin/" not in text
    assert "src/app.py" in text
    assert park.safe_path("skills/a/b.py") == park.WITHHELD_PATH and park.safe_path("src/a.py") == "src/a.py"


# --------------------------------------------------------------------------- the brief is capped and offline


def test_brief_caps_files_bytes_and_time_and_runs_no_gh():
    calls = []

    def run(cwd, argv):
        calls.append(list(argv))
        return "\n".join("abc%d subject %d" % (i, i) for i in range(50))
    paths = ["dir/f%02d.txt" % i for i in range(40)] + ["skills/kit/x.py"]
    text = "<<<<<<< a\n" + "x" * 5000 + "\n=======\ny\n>>>>>>> b\n"
    brief = park.build_brief(run, "/w", "origin/main", "billing", "feature/billing", {"sha": "deadbeef0000", "subject": "s"},
                             paths, lambda p: text)
    assert len(brief.encode()) <= park.MAX_BRIEF_BYTES + 64
    assert brief.count("\n- `") <= park.MAX_FILES
    assert "and %d more file(s)" % (41 - park.MAX_FILES) in brief
    assert not any(a[0] == "gh" for a in calls), "the brief never asks the network"
    assert all(len([a for a in c if a == "-n"]) == 1 and c[c.index("-n") + 1] == str(park.MAX_COMMITS_PER_FILE)
               for c in calls), "the commits per file are capped"

    ticks = iter([0, 10] + list(range(40, 1000, 30)))   # the budget runs out after the first file
    short = park.build_brief(run, "/w", "origin/main", "u", "b", {"sha": "", "subject": ""}, ["a", "b", "c"],
                             lambda p: "", now=lambda: next(ticks), budget=20.0)
    assert "time budget reached" in short and short.count("\n- `") == 1


def test_brief_never_cites_a_kit_path_or_a_marker_from_quoted_text():
    marker = "sigma:" + "keep-parked"
    brief = park.build_brief(lambda c, a: "1 touches skills/x/y.py " + marker, "/w", "origin/main", "u", "b",
                             {"sha": "abc", "subject": "see " + marker}, ["skills/x/y.py", "src/a.py"],
                             lambda p: "<<<<<<< a\nuse hooks/z.sh " + marker + "\n=======\n>>>>>>> b\n")
    assert "skills/" not in brief and "hooks/" not in brief and not legacy.has_marker(brief, marker)
    assert "src/a.py" in brief


# --------------------------------------------------------------------------- the filed store and closing


def test_store_values_read_old_strings_as_unknown_issue_and_new_records_fully():
    assert park.fingerprint_of("tipsha") == "tipsha" and park.issue_of("tipsha") is None
    rec = park.record("fp", 12, False)
    assert park.fingerprint_of(rec) == "fp" and park.issue_of(rec) == 12
    assert park.issue_of({"fingerprint": "fp", "issue": "13"}) == 13
    assert park.issue_of({"fingerprint": "fp", "issue": True}) is None and park.issue_of(None) is None


def test_closable_rules():
    store = {"park:a": park.record("a", 5, False), "park:b": park.record("b", 5, False),
             "park:c": park.record("c", 6, True), "park:d": "old", "park:e": park.record("e", None, False),
             "feature-conflict:x": "tip"}
    both = dict(park.closable(store, ["park:a", "park:b"]))
    assert both == {"park:a": 5, "park:b": 5}
    # only one of two slots on the same issue resolved: the issue still tracks a live conflict, so it is not closed
    assert park.closable(store, ["park:a"]) == []
    assert park.closable(store, ["park:c"]) == []                  # reused: someone else's work, never closed
    assert park.closable(store, ["park:d", "park:e", "feature-conflict:x"]) == []   # no number / not a park slot


# --------------------------------------------------------------------------- the no-goal filing path


class _Source:
    def __init__(self):
        self.created = None
        self.notes = []

    def create_dependency(self, title, body, assignee, labels=(), goal_label=True):
        self.created = {"title": title, "body": body, "assignee": assignee, "labels": list(labels)}
        return "71"

    def note(self, goal, text):
        self.notes.append((goal, text))

    def fetch_body_labels(self, goal):
        raise AssertionError("no goal: the unit must not be looked up from one")


def _no_goal(tmp_path, monkeypatch, **kw):
    sdlc = tmp_path / ".sdlc"
    (sdlc / "state").mkdir(parents=True)
    config = {"ledger": {"enabled": True, "actor": "amy"}}
    (sdlc / "config.json").write_text(json.dumps(config))
    for name in ("_auto_classify_unit", "_duplicate_search", "_ownership_verdict"):
        monkeypatch.setattr(handoff, name, lambda *a, _n=name, **k: (_ for _ in ()).throw(AssertionError(_n)))
    source = _Source()
    report = handoff.create_tracked_issue(
        sdlc, config, None, "work", "why", same_area=True, immediately_actionable=False, blocks_goal=False,
        title="t", body="body", source=source, run=lambda *a, **k: "", target_unit="billing",
        idempotency_key="0123456789abcdef", **kw)
    return sdlc, source, report


def test_no_goal_filing_skips_lookup_classifier_ownership_dedup_and_goal_writes(tmp_path, monkeypatch):
    sdlc, source, report = _no_goal(tmp_path, monkeypatch)
    assert report["issue"] == "71" and report["goal"] == "" and report["duplicate_of"] is None
    assert source.notes == [], "no goal issue exists to comment on"
    assert source.created["body"].rstrip().endswith("conflict-id: 0123456789abcdef")
    assert "sdlc:goal" not in source.created["labels"], "filed as a proposal"


def test_no_goal_filing_writes_an_unaddressed_note_keyed_on_the_scoped_key(tmp_path, monkeypatch):
    sdlc, _source, report = _no_goal(tmp_path, monkeypatch)
    entry = report["entry"]
    assert entry["kind"] == "note" and not entry.get("to"), "a finding must not be addressed to its filer"
    assert entry["goal"].startswith("upkeep-0123456789abcdef")
    assert ledger.addressed_to(ledger.read_all(sdlc), "amy") == [], "nobody, the filer included, is told"


def test_no_goal_filing_routes_on_the_scoped_key_not_on_none(tmp_path, monkeypatch):
    seen = []
    real = handoff._upstream()

    class Spy:
        def __getattr__(self, name):
            return getattr(real, name)

        def route(self, sdlc_dir, config, goal, *a, **k):
            seen.append(goal)
            return real.route(sdlc_dir, config, goal, *a, **k)
    monkeypatch.setattr(handoff, "_upstream", lambda: Spy())
    _no_goal(tmp_path, monkeypatch)
    assert seen == ["upkeep-0123456789abcdef"] and "None" not in seen[0]


def test_two_conflicts_do_not_collapse_onto_one_issue_when_the_bodies_are_templated(tmp_path, monkeypatch):
    """CONTROL for dedup=False: a fuzzy search over near-identical templated bodies would reuse conflict 1's issue."""
    sdlc, source, _first = _no_goal(tmp_path, monkeypatch)
    second = handoff.create_tracked_issue(
        sdlc, {"ledger": {"enabled": True, "actor": "amy"}}, None, "work", "why", same_area=True,
        immediately_actionable=False, blocks_goal=False, title="t", body="body", source=source,
        run=lambda *a, **k: "", target_unit="billing", idempotency_key="fedcba9876543210")
    assert second["issue"] == "71" and second["duplicate_of"] is None and second["issue_attempted"]


# --------------------------------------------------------------------------- the engine, real git


def _upkeep(m, world, config, run=None):
    return m.upkeep(str(world.sdlc), config, "7", base.UNIT, run=run)


def test_a_conflict_under_the_gate_is_parked_with_one_per_conflict_finding(tmp_path):
    m = base._mod()
    filed = base._filer(m)
    world = _conflicted_world(tmp_path)
    before = world.dirt()
    tip = base._git(world.local, "rev-parse", "origin/" + base.FEATURE)
    report = _upkeep(m, world, _cfg(OPEN))
    assert report["outcome"] == m.PARKED, report
    assert world.dirt() == before and base._git(world.local, "rev-parse", "origin/" + base.FEATURE) == tip
    assert report["leftovers"] == [] and not (world.local / ".sdlc" / "state" / "rebase" / base.UNIT).exists()
    assert len(filed) == 1
    call = filed[0]
    assert call["goal"] is None and call["target_unit"] == base.UNIT and call["dedup"] is False
    assert call["blocks_goal"] is False and call["immediately_actionable"] is False
    cid = call["idempotency_key"]
    assert len(cid) == 16 and cid in call["title"] or cid[:8] in call["title"]
    assert "seed.txt" in call["body"] and "<<<<<<<" in call["body"], "the brief was rendered before the drop"
    store = json.loads(m.filed_path(str(world.sdlc), base.UNIT).read_text())
    assert store == {"park:" + cid: {"fingerprint": cid, "issue": 999, "reused": False}}
    marker = json.loads(m.parked_path(str(world.sdlc), base.UNIT).read_text())
    assert marker["outcome"] == "parked" and marker["conflict"] == cid and marker["files"] == 1
    assert not m.blocked_path(str(world.sdlc), base.UNIT).exists(), "a park never uses the would-drop marker"
    assert "parked" in report["note"] and "#999" in report["note"]


def test_the_same_conflict_is_filed_once_across_passes_even_when_the_tip_moves(tmp_path):
    m = base._mod()
    filed = base._filer(m)
    world = _conflicted_world(tmp_path)
    first = _upkeep(m, world, _cfg(OPEN))
    second = _upkeep(m, world, _cfg(OPEN))
    assert first["outcome"] == second["outcome"] == m.PARKED
    assert len(filed) == 1 and second["filing"] == m.ALREADY_FILED
    # the unit tip moves with an unrelated commit; the conflicting commit and its files are the same conflict
    base._git(world.local, "checkout", "-q", base.FEATURE)
    base._write(world.local / "extra.txt", "x\n")
    base._git(world.local, "add", "extra.txt")
    base._git(world.local, "commit", "-q", "-m", "feat: extra (#12)")
    base._git(world.local, "push", "-q", "origin", base.FEATURE)
    base._git(world.local, "checkout", "-q", base.INTEGRATION)
    third = _upkeep(m, world, _cfg(OPEN))
    assert third["outcome"] == m.PARKED and len(filed) == 1, "per conflict, not per tip"


def test_closed_gate_is_byte_identical_conflict_per_tip_string_store_no_park_files(tmp_path):
    m = base._mod()
    filed = base._filer(m)
    world = _conflicted_world(tmp_path)
    report = _upkeep(m, world, _cfg())
    assert report["outcome"] == m.CONFLICT and "park" not in report
    assert filed[0]["goal"] == "7" and "target_unit" not in filed[0] and "dedup" not in filed[0]
    store = json.loads(m.filed_path(str(world.sdlc), base.UNIT).read_text())
    assert store == {"feature-conflict:" + base.FEATURE: report["before"]}
    assert not m.parked_path(str(world.sdlc), base.UNIT).exists()


class _GhRecorder:
    def __init__(self, m, fail=False):
        self.m, self.calls, self.fail = m, [], fail

    def __call__(self, cwd, argv):
        if argv and argv[0] == "gh":
            self.calls.append(list(argv[1:]))
            if self.fail:
                raise RuntimeError("gh is down")
            return "{}"
        return self.m._run(cwd, argv)


def _resolve_by_hand(world):
    """A person brings the unit forward: the feature branch now contains the integration branch."""
    local = world.local
    base._git(local, "checkout", "-q", base.FEATURE)
    base._git(local, "reset", "-q", "--hard", "origin/" + base.INTEGRATION)
    base._write(local / "seed.txt", "resolved\n")
    base._git(local, "add", "seed.txt")
    base._git(local, "commit", "-q", "-m", "feat: resolved (#11)")
    base._git(local, "push", "-q", "--force", "origin", base.FEATURE)
    base._git(local, "checkout", "-q", base.INTEGRATION)


def test_a_later_clean_pass_comments_then_closes_the_finding_and_clears_the_park(tmp_path):
    m = base._mod()
    base._filer(m)
    world = _conflicted_world(tmp_path)
    assert _upkeep(m, world, _cfg(OPEN))["outcome"] == m.PARKED
    _resolve_by_hand(world)
    gh = _GhRecorder(m)
    report = _upkeep(m, world, _cfg(OPEN), run=gh)
    assert report["outcome"] == m.CURRENT, report
    kinds = [(c[0], c[1:5]) for c in gh.calls]
    posts = [c for c in gh.calls if "--method" in c and "POST" in c]
    patches = [c for c in gh.calls if "PATCH" in c and "state=closed" in c]
    assert len(posts) == 1 and len(patches) == 1, kinds
    assert gh.calls.index(posts[0]) < gh.calls.index(patches[0]), "the record comment is posted first"
    assert any("issues/999" in a for a in patches[0])
    text = [a for a in posts[0] if a.startswith("body=")][0]
    assert "#" not in text, "a record comment carries no issue reference that could close or re-reference one"
    assert json.loads(m.filed_path(str(world.sdlc), base.UNIT).read_text()) == {}
    assert not m.parked_path(str(world.sdlc), base.UNIT).exists()


def test_a_failed_close_keeps_the_slot_so_the_next_pass_retries(tmp_path):
    m = base._mod()
    base._filer(m)
    world = _conflicted_world(tmp_path)
    _upkeep(m, world, _cfg(OPEN))
    _resolve_by_hand(world)
    assert _upkeep(m, world, _cfg(OPEN), run=_GhRecorder(m, fail=True))["outcome"] == m.CURRENT
    assert len(json.loads(m.filed_path(str(world.sdlc), base.UNIT).read_text())) == 1
    gh = _GhRecorder(m)
    _upkeep(m, world, _cfg(OPEN), run=gh)
    assert any("state=closed" in a for c in gh.calls for a in c)
    assert json.loads(m.filed_path(str(world.sdlc), base.UNIT).read_text()) == {}


def test_a_reused_issue_is_never_closed_but_its_slot_is_dropped(tmp_path):
    m = base._mod()
    world = _conflicted_world(tmp_path)
    filed = []

    def reuse(sdlc_dir, config, goal, area, why, **kw):
        filed.append(kw)
        return {"issue": "42", "warnings": [], "duplicate_of": "42"}
    import types
    m._HANDOFF = types.SimpleNamespace(create_tracked_issue=reuse, DEFAULT_PRIORITY="P1")
    _upkeep(m, world, _cfg(OPEN))
    store = json.loads(m.filed_path(str(world.sdlc), base.UNIT).read_text())
    assert [v["reused"] for v in store.values()] == [True]
    _resolve_by_hand(world)
    gh = _GhRecorder(m)
    _upkeep(m, world, _cfg(OPEN), run=gh)
    assert gh.calls == [] and json.loads(m.filed_path(str(world.sdlc), base.UNIT).read_text()) == {}


def test_closed_gate_never_settles_or_closes(tmp_path):
    m = base._mod()
    base._filer(m)
    world = _conflicted_world(tmp_path)
    m._remember(str(world.sdlc), base.UNIT, "park:abc", park.record("abc", 5, False))
    _resolve_by_hand(world)
    gh = _GhRecorder(m)
    assert _upkeep(m, world, _cfg(), run=gh)["outcome"] == m.CURRENT
    assert gh.calls == [] and "park:abc" in json.loads(m.filed_path(str(world.sdlc), base.UNIT).read_text())


# --------------------------------------------------------------------------- the doctor row


def test_the_doctor_reads_parks_from_their_own_marker_and_stays_silent_otherwise(tmp_path):
    doctor = _load("doctor_under_test", DOCTOR)
    m = base._mod()
    world = base.World(tmp_path).build()          # registers an open unit
    sdlc = world.sdlc
    assert doctor._rebase_parks(sdlc, {}) == []
    marker = m.parked_path(str(sdlc), base.UNIT)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"unit": base.UNIT, "branch": base.FEATURE, "files": 2, "at": "2026-01-01T00:00:00Z"}))
    rows = doctor._rebase_parks(sdlc, {})
    assert len(rows) == 1 and rows[0][:3] == (base.FEATURE, 2, "2026-01-01T00:00:00Z") and rows[0][3].endswith("h")
    assert doctor._rebase_blocks(sdlc, {}) == [], "a park is never rendered as a would-drop block"
    assert doctor._rebase_parks(sdlc, {"rebase_upkeep": "off"}) == []
    assert doctor._age_text("garbage") == "age unknown"


# --------------------------------------------------------------------------- #1104: one issue per parked conflict id

REPO_CFG = {"discovery": {"github": {"repo": "acme/widgets"}}}
DAY = 24 * 3600


class _Board:
    """A fake `gh`: serves the open-issue list and records every comment/close."""

    def __init__(self, m, rows=()):
        self.m, self.rows, self.comments, self.closed, self.lists = m, list(rows), [], [], 0

    def __call__(self, cwd, argv):
        if not argv or argv[0] != "gh":
            return self.m._run(cwd, argv)
        args = argv[1:]
        if "--method" in args and "GET" in args and args[1].endswith("/issues"):
            self.lists += 1
            return json.dumps(self.rows if "page=1" in " ".join(args) else [])
        if "POST" in args and args[1].endswith("/comments"):
            self.comments.append((args[1], [a for a in args if a.startswith("body=")][0]))
        if "state=closed" in args:
            self.closed.append(args[1])
        return "{}"


def _opened(m, tmp_path, clock):
    m._now = lambda: clock[0]
    filed = base._filer(m)
    return filed, _conflicted_world(tmp_path)


def _forget_store(world, m):
    m.filed_path(str(world.sdlc), base.UNIT).unlink()


def test_second_pass_against_an_open_issue_of_the_same_conflict_files_nothing_and_comments_once_a_day(tmp_path):
    m, clock = base._mod(), [1_000_000.0]
    filed, world = _opened(m, tmp_path, clock)
    cfg = _cfg(dict(OPEN, **REPO_CFG))
    board = _Board(m)
    assert _upkeep(m, world, cfg, run=board)["outcome"] == m.PARKED and len(filed) == 1
    cid = filed[0]["idempotency_key"]
    board.rows = [{"number": 999, "title": filed[0]["title"]}]
    _forget_store(world, m)                       # another clone, or a lost store: only GitHub remembers
    second = _upkeep(m, world, cfg, run=board)
    assert len(filed) == 1 and second["filing"] == m.ALREADY_FILED, "exactly one issue after the second run"
    assert len(board.comments) == 1 and "issues/999/comments" in board.comments[0][0]
    assert "pass 1" in board.comments[0][1] and "#" not in board.comments[0][1]
    _upkeep(m, world, cfg, run=board)             # same day: silent
    assert len(board.comments) == 1 and board.lists == 2, "one bounded read per lookup"
    clock[0] += DAY + 5
    _upkeep(m, world, cfg, run=board)             # a day later: one comment, with the counter
    assert len(board.comments) == 2 and "pass 2" in board.comments[1][1]
    assert cid in json.dumps(json.loads(m.filed_path(str(world.sdlc), base.UNIT).read_text()))


def test_a_changed_conflict_id_files_a_new_issue_naming_the_one_it_supersedes(tmp_path):
    m, clock = base._mod(), [1_000_000.0]
    filed, world = _opened(m, tmp_path, clock)
    board = _Board(m, [{"number": 41, "title": "Rebase parked: %s onto main (conflict deadbeef)" % base.FEATURE}])
    _upkeep(m, world, _cfg(dict(OPEN, **REPO_CFG)), run=board)
    assert len(filed) == 1 and "supersedes the earlier finding #41" in filed[0]["body"]
    assert board.comments == [] and board.closed == [], "the old issue is neither commented on nor closed"


def test_a_later_clean_pass_comments_on_the_found_open_issue_and_never_closes_it(tmp_path):
    m, clock = base._mod(), [1_000_000.0]
    filed, world = _opened(m, tmp_path, clock)
    cfg = _cfg(dict(OPEN, **REPO_CFG))
    board = _Board(m)
    _upkeep(m, world, cfg, run=board)
    board.rows = [{"number": 999, "title": filed[0]["title"]}]
    _forget_store(world, m)
    _upkeep(m, world, cfg, run=board)
    board.comments.clear()
    _resolve_by_hand(world)
    assert _upkeep(m, world, cfg, run=board)["outcome"] == m.CURRENT
    assert len(board.comments) == 1 and "resolved" in board.comments[0][1] and board.closed == []
    assert json.loads(m.filed_path(str(world.sdlc), base.UNIT).read_text()) == {}


def test_closed_gate_never_looks_up_open_issues(tmp_path):
    m, clock = base._mod(), [1_000_000.0]
    filed, world = _opened(m, tmp_path, clock)
    board = _Board(m, [{"number": 5, "title": "Rebase parked: x onto y (conflict 00000000)"}])
    assert _upkeep(m, world, _cfg(REPO_CFG), run=board)["outcome"] == m.CONFLICT
    assert board.lists == 0 and board.comments == [] and len(filed) == 1
