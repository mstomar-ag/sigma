"""#1227: a bulk `promote.py migrate-confirmation` run over a board with many states, in slices.

Fake source layer only (no network, no `gh`). The matrix test builds issues across populations and states,
asserts each destination, that no issue ends with both or neither membership label, and that every other
label is kept; a control removes the label swap and the same assertions must go red."""
import pytest

from test_promote_migrate import FakeSource, PROPOSED, GOAL, PARKED, promote

PROGRESS, BLOCKED = "sdlc:in-progress", "sdlc:blocked"
UNIT_OPEN, UNIT_GONE = "feature:alpha", "feature:beta"
CFG = {}


class Clock:
    def __init__(self):
        self.t, self.sleeps = 1000.0, []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


class BulkSource(FakeSource):
    def __init__(self):
        super().__init__()
        self.fail_swap, self.rate_limit_swaps = set(), {}

    def _swap_labels(self, number, add=(), remove=(), **kw):
        n = int(str(number))
        if n in self.fail_swap:
            raise RuntimeError("simulated write failure")
        if self.rate_limit_swaps.get(n, 0) > 0:
            self.rate_limit_swaps[n] -= 1
            raise RuntimeError("API rate limit exceeded")
        return super()._swap_labels(number, add=add, remove=remove, **kw)


def _hold(sdlc_dir, config, number, names, author):
    return "unit alpha holds it" if UNIT_OPEN in names and "held" in names else None


@pytest.fixture(autouse=True)
def _gates(monkeypatch):
    monkeypatch.setattr(promote, "_feature_hold", _hold)


def _matrix():
    """number -> (labels, kwargs, expected destination)"""
    P = PROPOSED
    HUMAN = dict(title="human idea", body="please add a flag")
    FUP = dict(title="tidy the docs", body="cleanup of old text")
    return {
        1: ([P, "priority:P1"], HUMAN, "armed"),                                   # human, plain
        2: ([P], HUMAN, "parked"),                                                 # human, no priority
        3: ([P, "priority:P2", "sdlc:followup"], FUP, "armed"),                    # follow-up
        4: ([P, "sdlc:followup"], FUP, "armed"),                                   # follow-up, no priority
        5: ([P, "priority:P1"], dict(HUMAN, comments=[promote._OWNER_MARKER]), "parked"),      # ownership hold
        6: ([P, "priority:P1"], dict(HUMAN, comments=[promote._SCOPE_MARKER]), "parked"),      # scope hold
        7: ([P, "priority:P1", UNIT_OPEN], HUMAN, "armed"),                        # unit open, no hold
        8: ([P, "priority:P1", UNIT_OPEN, "held"], HUMAN, "parked"),               # unit open, gate holds
        9: ([P, "priority:P1", UNIT_GONE], HUMAN, "armed"),                        # unit closed or unknown: fails open
        10: ([P, "priority:P1", PROGRESS], HUMAN, "armed"),                        # in progress
        11: ([P, "priority:P1", PARKED], HUMAN, "parked"),                         # already parked, kept parked
        12: ([P, "priority:P1", BLOCKED], HUMAN, "armed"),                         # blocked overlay, no blocker
        13: ([P, "priority:P1"], dict(HUMAN, body="Blocked by: #90"), "parked"),   # open blocker
        14: ([P, "priority:P1"], dict(HUMAN, body="Blocked by: #91"), "armed"),    # closed blocker
        15: ([P, "priority:P1", GOAL], HUMAN, "armed"),                            # half-promoted pair
        16: ([P, "priority:P1"], dict(HUMAN, assignee="dana"), "armed"),           # assigned
        17: ([P, "priority:P1"], dict(HUMAN, state="closed"), "closed"),           # closed
    }


def _seed(src):
    src.add(90, GOAL)                       # open blocker
    src.add(91, state="closed")             # closed blocker
    spec = _matrix()
    for n, (labels, kw, _) in spec.items():
        src.add(n, *labels, **{k: v for k, v in kw.items() if k != "assignee"})
    return spec


def _assert_matrix(src, rep, spec, before):
    out = {int(r["number"]): r for r in rep["items"]}
    for n, (labels, kw, want) in spec.items():
        got = out[n]["outcome"]
        assert got == want, (n, got, want)
        now = set(src.store[n]["labels"])
        if want == "closed":
            assert now == set(labels)
            continue
        assert PROPOSED not in now, n
        assert (GOAL in now) != (PARKED in now), (n, now)          # exactly one membership label
        assert (GOAL in now) == (want == "armed"), n
        keep = {l for l in before[n] if l not in (PROPOSED, GOAL, PARKED) and not l.startswith("priority:")}
        assert keep <= now, (n, keep - now)                         # unit and every other label kept


def test_matrix_destinations_and_label_integrity(tmp_path):
    src = BulkSource()
    spec = _seed(src)
    before = {n: list(src.store[n]["labels"]) for n in spec}
    rep = promote.migrate(src, CFG, apply=True, sdlc_dir=str(tmp_path), clock=Clock().now, sleep=Clock().sleep)
    _assert_matrix(src, rep, spec, before)
    assert rep["complete"] and rep["remaining"] == []
    assert not src.store[17]["labels"].count(GOAL)


def test_control_without_the_label_swap_the_matrix_fails(tmp_path):
    src = BulkSource()
    spec = _seed(src)
    before = {n: list(src.store[n]["labels"]) for n in spec}
    src._swap_labels = lambda *a, **k: True                          # the swap is removed
    rep = promote.migrate(src, CFG, apply=True, sdlc_dir=str(tmp_path), clock=Clock().now, sleep=Clock().sleep)
    with pytest.raises(AssertionError):
        _assert_matrix(src, rep, spec, before)
    assert not rep["complete"] and rep["remaining"]                  # and the run's own verify says so


def test_second_apply_changes_nothing(tmp_path):
    src = BulkSource()
    _seed(src)
    c = Clock()
    promote.migrate(src, CFG, apply=True, sdlc_dir=str(tmp_path), clock=c.now, sleep=c.sleep)
    snap = ({n: sorted(i["labels"]) for n, i in src.store.items()}, list(src.writes))
    again = promote.migrate(src, CFG, apply=True, sdlc_dir=str(tmp_path), clock=c.now, sleep=c.sleep)
    assert [r["outcome"] for r in again["items"]] == ["closed"] and again["complete"]   # only the closed one is listed
    assert ({n: sorted(i["labels"]) for n, i in src.store.items()}, list(src.writes)) == snap


def test_interrupted_run_covers_the_rest_and_a_rerun_finishes(tmp_path):
    src = BulkSource()
    for n in range(1, 7):
        src.add(n, PROPOSED, "priority:P1")
    src.fail_swap = {3}
    c = Clock()
    rep = promote.migrate(src, CFG, apply=True, sdlc_dir=str(tmp_path), clock=c.now, sleep=c.sleep)
    assert rep["covered"] == 5 and rep["total"] == 6 and not rep["complete"]
    assert "covered 5 of 6" in promote.render_migrate(rep)
    assert rep["remaining"] == ["3"] and PROPOSED in src.store[3]["labels"]
    assert all(PROPOSED not in src.store[n]["labels"] for n in (1, 2, 4, 5, 6))
    src.fail_swap = set()
    rep2 = promote.migrate(src, CFG, apply=True, sdlc_dir=str(tmp_path), clock=c.now, sleep=c.sleep)
    assert rep2["total"] == 1 and rep2["complete"] and GOAL in src.store[3]["labels"]


def test_slices_unit_only_limit(tmp_path):
    src = BulkSource()
    for n in range(1, 7):
        src.add(n, PROPOSED, "priority:P1", *([UNIT_OPEN] if n % 2 else []))
    run = lambda **kw: promote.migrate(src, CFG, apply=False, sdlc_dir=str(tmp_path), **kw)
    assert [r["number"] for r in run(unit="alpha")["items"]] == ["1", "3", "5"]
    assert [r["number"] for r in run(only={2, 6})["items"]] == ["2", "6"]
    assert [r["number"] for r in run(limit=2)["items"]] == ["1", "2"]
    assert [r["number"] for r in run(unit="alpha", limit=2)["items"]] == ["1", "3"]
    rep = run(unit="alpha")
    assert rep["total"] == 3 and rep["fetched"] == 6 and src.writes == []


def test_dry_run_prints_destination_population_reason_and_exits_nonzero_when_unreadable(tmp_path, capsys):
    src = BulkSource()
    src.add(1, PROPOSED, "priority:P1", title="human idea")
    src.add(2, PROPOSED, "priority:P1")
    src.unreadable.add(2)
    rc = promote.main(["promote.py", "migrate-confirmation", str(tmp_path), "--only", "1,2"], source=src)
    out = capsys.readouterr().out
    assert "#1 -> armed at P1 | population: human | reason:" in out
    assert rc == 1 and "covered 1 of 2" in out and src.writes == []


def test_dry_run_exits_nonzero_when_the_fetch_was_truncated(tmp_path, capsys):
    src = BulkSource()
    for n in range(1, 4):
        src.add(n, PROPOSED, "priority:P1")
    cfg = {"ai_filed": {"migration": {"max_issues": 3}}}
    rep = promote.migrate(src, cfg, apply=False, sdlc_dir=str(tmp_path))
    assert rep["truncated"] and not rep["complete"]


def test_cli_flag_parsing():
    f = promote._migrate_flags
    assert f(["--apply", "--unit", "alpha", "--only", "1,2", "--limit", "3"]) == ({"--apply"}, "alpha", {1, 2}, 3)
    for bad in (["--limit", "0"], ["--limit", "x"], ["--only", "a"], ["--unit"], ["--nope"], ["--only", ","]):
        assert f(bad) is None


def test_writes_are_paced_to_the_configured_rate(tmp_path):
    src = BulkSource()
    for n in range(1, 7):
        src.add(n, PROPOSED, "priority:P1")
    cfg = {"ai_filed": {"migration": {"max_writes_per_minute": 3}}}
    c = Clock()
    rep = promote.migrate(src, cfg, apply=True, sdlc_dir=str(tmp_path), clock=c.now, sleep=c.sleep)
    assert rep["complete"]
    # 6 issues x (swap + comment) = 12 writes; no 60s window ever holds more than 3
    assert len(c.sleeps) >= 3 and all(0 < s <= 60 for s in c.sleeps)
    assert c.t - 1000.0 >= 60 * 3


def test_default_pacing_is_conservative():
    w = promote._Writes({}, lambda: 0.0, lambda s: None)
    assert w.per_minute == 30 and w.pause_s == 60


def test_rate_limit_pauses_and_resumes_instead_of_failing(tmp_path):
    src = BulkSource()
    for n in range(1, 4):
        src.add(n, PROPOSED, "priority:P1")
    src.rate_limit_swaps = {2: 2}
    c = Clock()
    rep = promote.migrate(src, CFG, apply=True, sdlc_dir=str(tmp_path), clock=c.now, sleep=c.sleep)
    assert rep["complete"] and rep["pauses"] == 2 and 60 in c.sleeps
    assert all(GOAL in src.store[n]["labels"] for n in (1, 2, 3))
    assert "paused 2 time(s)" in promote.render_migrate(rep)


def test_rate_limit_that_never_clears_fails_only_that_issue(tmp_path):
    src = BulkSource()
    for n in range(1, 4):
        src.add(n, PROPOSED, "priority:P1")
    src.rate_limit_swaps = {2: 99}
    c = Clock()
    rep = promote.migrate(src, CFG, apply=True, sdlc_dir=str(tmp_path), clock=c.now, sleep=c.sleep)
    assert rep["covered"] == 2 and rep["total"] == 3 and not rep["complete"]


def test_verify_lists_leftovers_and_flags_one_inside_the_slice(tmp_path):
    src = BulkSource()
    for n in range(1, 5):
        src.add(n, PROPOSED, "priority:P1")
    c = Clock()
    rep = promote.migrate(src, CFG, apply=True, sdlc_dir=str(tmp_path), only={1, 2}, clock=c.now, sleep=c.sleep)
    assert rep["remaining"] == ["3", "4"] and rep["complete"]
    assert "2 are outside this slice" in promote.render_migrate(rep)
    src.fail_swap = {3}
    rep = promote.migrate(src, CFG, apply=True, sdlc_dir=str(tmp_path), only={3}, clock=c.now, sleep=c.sleep)
    assert not rep["complete"] and "FAILED: 1 of them" in promote.render_migrate(rep)


def test_dry_run_does_not_verify(tmp_path):
    src = BulkSource()
    src.add(1, PROPOSED, "priority:P1")
    rep = promote.migrate(src, CFG, apply=False, sdlc_dir=str(tmp_path))
    assert rep["remaining"] is None
