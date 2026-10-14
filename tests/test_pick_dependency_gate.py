"""#1499: a goal whose declared prerequisite is still OPEN must never be CLAIMED.

THE SHAPE THAT WAS BROKEN, measured live on 1.4.1 (`7094e6c0`) against a real board:

    backlog: #1594 (no marker), #1595 and #1596 (both "**Blocked by:** #1594")
    pick 1 -> 1594   correct
    pick 2 -> 1595   #1594 still OPEN and in progress
    pick 3 -> 1595   again
    pick 4 -> 1596   #1594 still OPEN

Every pick after the first returned a goal that could not be worked, so a human had to `--skip`
each one by hand on every call. On a dependency graph the loop did not degrade — it STALLED. The
dependency data was there the whole time: the board mirror had already stored
`{"phrase": "blocked by", "ref": "1594"}` on #1595's record at fetch time. Nothing read it before
the claim. `precheck` reads it, but `precheck` runs AFTER `mark_in_progress`, and on the shipped
`backlog_check.action: "flag"` it only annotates.

WHY THE FIX IS A SKIP AND NOT A PARK. `_next()` leaves a held goal exactly as it found it —
`sdlc:goal` intact, no `sdlc:in-progress`, no claim lock, no comment, no ledger line — and moves to
the next candidate. It becomes pickable by itself the moment its blocker closes; nothing has to
unpark it and no human has to intervene. That is why the assertions below check for the ABSENCE of
writes as hard as they check the pick.

WHAT THE GATE READS, AND WHAT IT COSTS. The board mirror, off disk. `mirror.fetch_and_write` is
TTL-guarded, so a repo whose mirror is fresh pays ZERO `gh` calls for the gate WHENEVER NOTHING IS
HELD (`test_a_fresh_mirror_costs_the_gate_no_gh_calls_when_nothing_is_held`) — which is every pick
on a backlog with no open dependency edge.

TWO THINGS BUY A REFETCH, and this paragraph named only the first until #1650 (it also named the
first test by a name it no longer has, which is why the citations below are checked):

  * the `goal_not_in_corpus` hole — a mirror fresh by TTL but written before the issues being picked
    existed — forces one refresh, at most once per PROCESS
    (`test_the_forced_refresh_happens_at_most_once_per_process`);
  * #1650: reporting a HOLD forces one refresh, at most once per CALL however many goals are held,
    and never when the call already fetched live
    (`test_the_hold_refresh_is_paid_once_per_call_however_many_goals_are_held`,
    `test_the_absence_refresh_and_the_hold_refresh_are_never_both_paid_in_one_call`). It is NOT
    bounded per process: a blocker that closes between two picks has to be seen by the second.

So the mirror is read once per call plus at most one refresh, not "once per call" flat
(`test_the_mirror_is_read_once_per_pick_plus_at_most_one_refresh`).

FAIL OPEN, LOUDLY. Every state the gate cannot determine — no mirror, a goal still absent after the
refresh, a prerequisite the mirror does not carry — CLAIMS, because a wrongly-held goal stalls the
loop, which is the bug this file exists to close. None of them is silent: each prints why.
"""
import importlib.util, json, pathlib, tempfile

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def _loop():
    return _mod("loop")


# --- the trial's own issues ----------------------------------------------------------------------

def _issue(number, body="", state="OPEN"):
    return {"number": number, "title": f"goal {number}", "body": body, "state": state,
            "labels": [{"name": "sdlc:goal"}], "closedAt": None,
            "updatedAt": "2026-08-23T00:00:00Z"}


def _blocked_by(number, ref):
    """The body shape `compile_plan` actually writes, and the shape the trial's issues had: the
    marker is the LAST line, well past `mirror._EXCERPT_CHARS`. Anything reading the 500-character
    excerpt cannot see it; `blocker_refs` (scanned off the whole body at fetch time) can."""
    return _issue(number, body="some context.\n" + "x" * 600 + "\n\n**Blocked by:** #%s\n" % ref)


TRIAL = [_blocked_by(1595, 1594), _blocked_by(1596, 1594), _issue(1594)]


class _Board:
    """A GitHub-shaped backlog source plus the `gh` transport the mirror's own fetch runs through.

    Deliberately NOT `sources.GitHubSource`: this file is about what happens BEFORE a claim, and a
    real source drags graphql node-id resolution, the feature-label surface and board-status writes
    into every assertion. `marked` is the claim — a goal that reaches `mark_in_progress` has been
    claimed, whatever else did or did not land.

    `live` is what a `gh issue list` would return RIGHT NOW; the mirror on disk may legitimately be
    older than it, which is the `goal_not_in_corpus` case this gate has to handle.
    """
    def __init__(self, order, live=TRIAL, calls=None, single=None):
        self.single = single or {}          # #1420: number -> record | raw string | Exception
        self.order = [str(g) for g in order]
        self.live = list(live)
        self.marked, self.released = [], []
        self.calls = [] if calls is None else calls

    def next_pending(self, skip=()):
        s = {str(x) for x in skip}
        return next((g for g in self.order if g not in s), None)

    def mark_in_progress(self, goal):
        self.marked.append(str(goal))

    def release(self, goal, reason):
        self.released.append((str(goal), reason))

    def _run(self, args):
        """#1833: the mirror's own fetch is `gh api repos/{owner}/{repo}/issues -f state=... ...`
        now, not `gh issue list --state ... --json ...` -- see `mirror.fetch_and_write`."""
        self.calls.append(list(args))
        if len(args) > 1 and args[0] == "api" and "/issues/" in str(args[1]):
            ans = self.single.get(int(str(args[1]).rsplit("/", 1)[1]), "")
            if isinstance(ans, Exception):
                raise ans
            return ans if isinstance(ans, str) else json.dumps(ans)
        if not (len(args) > 1 and args[0] == "api" and str(args[1]).startswith("repos/")
                and str(args[1]).endswith("/issues")):
            return ""
        want = next(v[len("state="):] for v in args
                    if isinstance(v, str) and v.startswith("state="))
        return json.dumps([i for i in self.live if str(i["state"]).lower() == want])

    def live_reads(self):
        """#1420: the single-issue GETs this fake saw."""
        return [c for c in self.calls if len(c) > 1 and "/issues/" in str(c[1])]

    def mirror_fetches(self):
        """Every `gh` call the MIRROR made. #1833: REST has no field-selection concept at all
        (there is no `--json` value left to distinguish a hypothetical differently-shaped caller
        by) -- every issues-listing REST call this fake sees IS a mirror fetch, since `_Board`
        itself answers `next_pending`/`mark_in_progress`/`release` as plain in-memory methods,
        never as `gh` calls."""
        return [c for c in self.calls
                if len(c) > 1 and c[0] == "api" and str(c[1]).startswith("repos/")
                and str(c[1]).endswith("/issues")]


def _base(d, mirror=TRIAL, gh_mode=True, extra=None):
    """A `.sdlc` with a FRESH board mirror already on disk, built by the real `mirror` pipeline from
    real issue bodies — so these tests prove the whole marker -> `blocker_scan` -> record -> gate
    chain, not a hand-written `blocker_refs` field the production path might never produce."""
    base = pathlib.Path(d) / ".sdlc"
    (base / "state").mkdir(parents=True)
    cfg = {"budget": {"max_iterations": 10}}
    if gh_mode:
        cfg["discovery"] = {"source": "github", "github": {"repo": "acme/widget"}}
    for k, v in (extra or {}).items():
        cfg[k] = {**cfg.get(k, {}), **v} if isinstance(v, dict) and isinstance(cfg.get(k), dict) else v
    (base / "config.json").write_text(json.dumps(cfg))
    (base / "state" / "STATE.md").write_text("iteration: 0\nrun_iteration: 0\nlast_run: none\n")
    if mirror is not None:
        seed = _Board([], live=mirror)
        _mod("mirror").fetch_and_write(str(base), config=cfg, run=seed._run)
    return str(base)


def _cfg(lp, base):
    return lp.state.load_config(base)


# --- the defect, and the property that was actually broken ---------------------------------------

def test_the_mirror_already_holds_the_dependency_the_picker_ignored():
    """The premise, pinned. If this ever goes red the gate is reading a field that no longer
    carries the edge, and every other test in this file would pass vacuously."""
    with tempfile.TemporaryDirectory() as d:
        base = _base(d)
        recs = {r["number"]: r["blocker_refs"] for r in _mod("mirror").read_mirror(base)}
    assert recs[1595] == [{"phrase": "blocked by", "ref": "1594"}]
    assert recs[1596] == [{"phrase": "blocked by", "ref": "1594"}]
    assert recs[1594] == []


def test_a_goal_whose_prerequisite_is_still_open_is_not_claimed(capsys):
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _base(d)
        src = _Board(["1595", "1596", "1594"])          # the blocked pair offered FIRST
        kind, goal = lp._next(base, src, _cfg(lp, base))
    assert (kind, goal) == ("goal", "1594")             # reached the pickable goal behind them
    assert src.marked == ["1594"]                       # and claimed nothing else on the way
    err = capsys.readouterr().err
    assert "not claiming #1595" in err and "#1594 (blocked by)" in err   # named, per goal, not silent


def test_the_trial_backlog_drains_to_one_pick_with_no_human_skip():
    """The trial's exact shape, end to end: three goals, two blocked by the first, four slots asked
    for. Pre-fix this returned all three (measured); the two blocked goals were claimed and had to
    be released by hand. The property is that the loop reaches the pickable one BY ITSELF."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _base(d)
        src = _Board(["1595", "1596", "1594"])
        picks = lp.next_batch(base, src, _cfg(lp, base), max_concurrent=4)
    assert picks == [("goal", "1594"), ("DONE", None)]
    assert src.marked == ["1594"]


def test_a_held_goal_is_neither_claimed_nor_released():
    """`not claimed and then released, not claimed and then parked` — the whole point of moving the
    check ahead of the claim. A held goal must leave NO trace: no `mark_in_progress`, no `release`,
    no ledger claim."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, extra={"ledger": {"enabled": True, "actor": "me",
                                          "lease": {"ttl_hours": 0}}})
        src = _Board(["1595", "1596", "1594"])
        lp._next(base, src, _cfg(lp, base))
        claims = lp.ledger.open_claims(lp.ledger.read_all(base))
    assert src.marked == ["1594"] and src.released == []
    assert set(claims) == {"1594"}


def test_a_closed_prerequisite_holds_nothing():
    lp = _loop()
    done = [_blocked_by(1595, 1594), _issue(1594, state="CLOSED")]
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=done)
        src = _Board(["1595"], live=done)
        kind, goal = lp._next(base, src, _cfg(lp, base))
    assert (kind, goal) == ("goal", "1595")


def test_a_goal_with_no_marker_behaves_exactly_as_before():
    lp = _loop()
    plain = [_issue(1594), _issue(1597)]
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=plain)
        src = _Board(["1594", "1597"], live=plain)
        picks = lp.next_batch(base, src, _cfg(lp, base), max_concurrent=3)
    assert picks == [("goal", "1594"), ("goal", "1597"), ("DONE", None)]


def test_a_goal_is_never_held_by_a_reference_to_itself():
    lp = _loop()
    selfref = [_blocked_by(1595, 1595)]
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=selfref)
        src = _Board(["1595"], live=selfref)
        kind, goal = lp._next(base, src, _cfg(lp, base))
    assert (kind, goal) == ("goal", "1595")


def test_a_self_reference_ALREADY_ON_the_record_still_holds_nothing():
    """The test above cannot fail, and saying so is the point: `mirror.normalize_issue` passes
    `self_ref` to `extract_refs`, so a self-reference never reaches the stored field in the first
    place — deleting the gate's own `self_ref=` argument leaves it green (confirmed by mutation).
    The guard is there for a record the CURRENT writer did not produce: a mirror written by an older
    install, or a hand-built one. This rewrites the record to that shape and pins the read side."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=[_issue(1595)])
        path = pathlib.Path(base) / "state" / "board-mirror.ndjson"
        recs = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        for r in recs:
            r["blocker_refs"] = [{"phrase": "blocked by", "ref": "1595"}]      # its own number
        path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in recs))
        src = _Board(["1595"], live=[_issue(1595)])
        kind, goal = lp._next(base, src, _cfg(lp, base))
    assert (kind, goal) == ("goal", "1595")


# --- cost -----------------------------------------------------------------------------------------

def test_a_fresh_mirror_costs_the_gate_no_gh_calls_when_nothing_is_held():
    """The cost claim, pinned. A pick already burns ~35-45 GraphQL calls against a shared 5,000/hr
    bucket; a gate that added a per-candidate remote read would compete with the disease it cures.
    Against a mirror that is fresh by TTL the gate is a local file read and nothing else — three
    slots, three candidates, one of them carrying a dependency edge that is already satisfied, and
    zero `gh`.

    #1650 narrowed the claim from "always" to "whenever nothing is held", and the narrowing is
    worth stating rather than quietly editing: a hold — and only a hold — now buys one forced
    refresh, priced in `test_the_hold_refresh_is_paid_once_per_call_however_many_goals_are_held`.
    What this test is about is the path most picks take, and that path did not move."""
    lp = _loop()
    satisfied = [_issue(1594, state="CLOSED"), _blocked_by(1595, 1594), _issue(1596), _issue(1597)]
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=satisfied)
        src = _Board(["1595", "1596", "1597"], live=satisfied)
        src.calls.clear()                                # the fixture's own seeding fetch
        picks = lp.next_batch(base, src, _cfg(lp, base), max_concurrent=3)
    assert [p[1] for p in picks] == ["1595", "1596", "1597"]
    assert src.mirror_fetches() == []


def test_the_mirror_is_read_once_per_pick_plus_at_most_one_refresh(monkeypatch):
    """The other half of the cost claim. `_next()` walks candidates until one survives every gate,
    so a per-candidate read of `board-mirror.ndjson` would scale with how many goals are held — the
    exact shape of a DAG-heavy backlog. THREE goals are held here, so per-candidate would be four
    reads; what it costs is the mirror as found plus #1650's single forced refresh, and nothing
    more, whatever the call walks past."""
    lp = _loop()
    m = _mod("mirror")
    reads = []
    real_read = m.read_mirror
    m.read_mirror = lambda d: (reads.append(d), real_read(d))[1]
    monkeypatch.setattr(lp, "_load", lambda n, _r=lp._load: m if n == "mirror" else _r(n))
    dag = [_blocked_by(1595, 1594), _blocked_by(1596, 1594), _blocked_by(1598, 1594), _issue(1594)]
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=dag)
        src = _Board(["1595", "1596", "1598", "1594"], live=dag)     # three held before the pick
        kind, goal = lp._next(base, src, _cfg(lp, base))
    assert (kind, goal) == ("goal", "1594")
    assert len(reads) == 2


# --- the goal_not_in_corpus hole -------------------------------------------------------------------

def test_a_goal_absent_from_a_ttl_fresh_mirror_forces_a_refresh_rather_than_claiming_blind():
    """A mirror fresh by TTL but written BEFORE the issues being picked existed reads identically to
    "checked, found nothing". With the default 60-minute TTL that is a coin flip immediately after a
    batch of issues is filed — exactly when a dependency chain is newest and most load-bearing."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=[_issue(1594)])           # predates 1595/1596 entirely
        src = _Board(["1595", "1596", "1594"])           # the live board has all three
        src.calls.clear()
        kind, goal = lp._next(base, src, _cfg(lp, base))
        refreshed = {r["number"] for r in _mod("mirror").read_mirror(base)}
    assert (kind, goal) == ("goal", "1594")              # the refreshed mirror carries the edge
    assert src.mirror_fetches()                          # and it was fetched, not assumed
    assert refreshed == {1594, 1595, 1596}


def test_the_forced_refresh_happens_at_most_once_per_process():
    """A goal that is absent for a reason a refresh cannot fix (past the mirror's 200-issue cap, a
    differing assignee filter) must not buy two `gh` calls on every pick, forever.

    TWO absent goals, not one, and that is load-bearing. The first pick claims #1597 and registers
    it in this session's in-flight set, so a second `_next()` never offers it again — a second call
    over a one-goal board would skip straight past the gate and pass with the guard deleted
    (confirmed: the mutant survived exactly that shape). #1598 is what makes the second call
    actually reach the forced-refresh branch."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=[_issue(1594)])
        # 1597 and 1598 are in NEITHER the mirror nor the live board — a refetch cannot find them
        src = _Board(["1597", "1598", "1594"], live=[_issue(1594)])
        src.calls.clear()
        first = lp._next(base, src, _cfg(lp, base))
        after_first = len(src.mirror_fetches())
        second = lp._next(base, src, _cfg(lp, base))
    assert (first, second) == (("goal", "1597"), ("goal", "1598"))   # both reached the gate
    assert after_first == 2                              # one open query + one closed query
    assert len(src.mirror_fetches()) == 2                # and not one more on the second pick


def test_a_refresh_this_call_already_made_is_not_immediately_repeated():
    """A stock repo's very first pick has no mirror at all, so the TTL fetch at the top of the gate
    genuinely refetches. A goal missing from a mirror written moments ago is missing for a reason a
    second fetch cannot fix (past the 200-issue cap, a differing assignee filter, a board card with
    no matching label) — refetching it back to back would buy two `gh` calls and the same answer."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=None)                     # nothing on disk: the first pick ever
        src = _Board(["1597", "1594"], live=[_issue(1594)])
        kind, goal = lp._next(base, src, _cfg(lp, base))
    assert (kind, goal) == ("goal", "1597")
    assert len(src.mirror_fetches()) == 2                # the TTL fetch only — open + closed


def test_a_goal_still_absent_after_the_refresh_is_claimed_and_says_why(capsys):
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=[_issue(1594)])
        src = _Board(["1597"], live=[_issue(1594)])
        kind, goal = lp._next(base, src, _cfg(lp, base))
    assert (kind, goal) == ("goal", "1597")              # fail OPEN — a held goal stalls the loop
    err = capsys.readouterr().err
    assert "1597" in err and "mirror" in err             # but never silently


def _run_orphan(lp, single, order=("1595",), goals=None, base_kw=None):
    """One `_next` over goals blocked by a prerequisite the mirror does not carry (#4242)."""
    orphan = goals or [_blocked_by(1595, 4242)]
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=orphan)
        src = _Board(list(order), live=orphan, single=single)
        res = lp._next(base, src, _cfg(lp, base))
    return res, src


def test_an_open_prerequisite_the_mirror_does_not_carry_holds_the_goal(capsys):
    """#1420: other-owner / no-goal-label blockers are invisible to the mirror; read live, an OPEN
    one holds the dependent."""
    lp = _loop()
    (kind, goal), src = _run_orphan(lp, {4242: {"number": 4242, "state": "open"}})
    assert src.marked == [] and (kind, goal) != ("goal", "1595")
    assert src.live_reads() == [["api", "repos/acme/widget/issues/4242", "--method", "GET"]]


def test_a_closed_prerequisite_the_mirror_does_not_carry_releases_the_goal(capsys):
    lp = _loop()
    (kind, goal), src = _run_orphan(lp, {4242: {"number": 4242, "state": "closed"}})
    assert (kind, goal) == ("goal", "1595")
    assert "#4242" not in capsys.readouterr().err


def test_an_unset_repo_still_reads_live_through_the_placeholder_endpoint():
    """#1420 review: `discovery.github.repo` unset is a supported setup; `gh` resolves the
    `{owner}/{repo}` placeholder from the checkout, so the live read must still happen."""
    lp = _loop()
    seen = []

    def run(args):
        seen.append(list(args))
        return json.dumps({"number": 4242, "state": "open"})
    got = lp._live_blocker_states(["4242"], {"discovery": {"source": "github"}}, run, {})
    assert got == {"4242": "open"}
    assert seen == [["api", "repos/{owner}/{repo}/issues/4242", "--method", "GET"]]


def test_an_unknown_prerequisite_is_read_once_per_call_however_many_goals_name_it():
    lp = _loop()
    goals = [_blocked_by(1595, 4242), _blocked_by(1596, 4242), _issue(1594)]
    _, src = _run_orphan(lp, {4242: {"number": 4242, "state": "open"}},
                         order=("1595", "1596", "1594"), goals=goals)
    assert len(src.live_reads()) == 1


def test_a_prerequisite_that_cannot_be_read_live_is_claimed_and_says_why(capsys):
    """Fail open: a read error, a malformed answer or a pull request stays undeterminable."""
    lp = _loop()
    for answer in (RuntimeError("boom"), "not json", "[1, 2]", "",
                   {"number": 4242, "state": "open", "pull_request": {"url": "x"}},
                   {"number": 4242, "state": "weird"}):
        (kind, goal), src = _run_orphan(lp, {4242: answer})
        assert (kind, goal) == ("goal", "1595"), answer
        assert "#4242" in capsys.readouterr().err, answer


# --- gates, and never breaking the pick ------------------------------------------------------------

def test_the_gate_is_inert_outside_github_discovery(capsys):
    """Local-files mode has no mirror at all. Byte-identical to before: same pick, no stray `gh` —
    and SILENT. Without the mode guard the gate still costs nothing (every path below it is itself
    github-gated and returns None) but every local-mode pick gains a "not in the board mirror"
    warning about a mirror that is not supposed to exist. A permanent false alarm on a config that
    was never in scope is a regression even though the pick is unchanged, which is why this asserts
    on stderr and not only on the pick."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=None, gh_mode=False)
        src = _Board(["1595", "1594"])
        kind, goal = lp._next(base, src, _cfg(lp, base))
    assert (kind, goal) == ("goal", "1595") and src.calls == []
    assert "mirror" not in capsys.readouterr().err


def test_the_gate_can_be_switched_off():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, extra={"discovery": {"dependency_gate": {"mode": "off"}}})
        src = _Board(["1595", "1596", "1594"])
        kind, goal = lp._next(base, src, _cfg(lp, base))
    assert (kind, goal) == ("goal", "1595")              # the pre-#1499 behaviour, on request


def test_an_unrecognised_mode_reads_as_on():
    """The opposite default from `_reconcile_mode`, deliberately: a typo there could switch on a
    mechanism that WRITES, while a typo here can only make the loop decline to claim something."""
    lp = _loop()
    assert lp._dependency_gate_mode({}) == "on"
    assert lp._dependency_gate_mode({"discovery": {"dependency_gate": {"mode": "nope"}}}) == "on"
    assert lp._dependency_gate_mode({"discovery": {"dependency_gate": "off"}}) == "on"
    assert lp._dependency_gate_mode({"discovery": {"dependency_gate": {"mode": "off"}}}) == "off"


def test_the_gate_fails_open_when_the_mirror_read_raises(monkeypatch, capsys):
    lp = _loop()
    real = lp._load

    def boom(name):
        if name == "mirror":
            raise RuntimeError("simulated mirror failure")
        return real(name)

    monkeypatch.setattr(lp, "_load", boom)
    with tempfile.TemporaryDirectory() as d:
        base = _base(d)
        src = _Board(["1595", "1594"])
        kind, goal = lp._next(base, src, _cfg(lp, base))
    assert (kind, goal) == ("goal", "1595")              # a broken gate must never stop a pick
    assert "dependency gate" in capsys.readouterr().err


def test_a_batch_that_holds_everything_says_so_rather_than_reporting_a_bare_drain(capsys):
    """`("DONE", None)` on a backlog where every remaining goal is waiting on an open prerequisite
    is technically true and practically misleading — it is what made the trial look like a silent
    stall. The terminal tuple is unchanged; the operator is told."""
    lp = _loop()
    held = [_blocked_by(1595, 1594), _blocked_by(1596, 1594)]
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=held + [_issue(1594)])
        src = _Board(["1595", "1596"], live=held + [_issue(1594)])
        picks = lp.next_batch(base, src, _cfg(lp, base), max_concurrent=2)
    assert picks == [("DONE", None)]
    err = capsys.readouterr().err
    # the SUMMARY's own words: "#1595"/"#1596" alone are already in the per-goal lines above it,
    # so asserting on those would pass with this line deleted (confirmed by mutation).
    assert "nothing pickable" in err and "2 goal(s)" in err
    assert "#1595 → #1594" in err and "#1596 → #1594" in err


# --- one vocabulary, one reader --------------------------------------------------------------------

def test_the_pick_gate_declares_no_blocker_vocabulary_of_its_own():
    """`blocker_scan` is the ONE definition of what a dependency marker looks like AND of how the
    stored `blocker_refs` record reads back. A gate that re-typed either would drift from the scan
    that produced the field — the exact failure #1487 factored that module out to prevent."""
    import re
    src = (S / "loop.py").read_text(encoding="utf-8")
    assert "blocker_refs" not in src, "loop.py reads the stored field itself instead of via blocker_scan"
    vocab = re.compile(r"\b(blocked by|depends on|depends upon)\b")
    for call in re.findall(r"re\.compile\((.*?)\)", src, re.DOTALL):
        assert not vocab.search(call), f"loop.py re-declares the block vocabulary: {call!r}"


def test_read_refs_is_the_inverse_of_extract_refs():
    bs = _mod("blocker_scan")
    body = "context\n\n**Blocked by:** #1594\n"
    rec = {"number": 1595, "blocker_refs": bs.extract_refs(body, self_ref=1595)}
    assert bs.read_refs(rec) == [("1594", "blocked by")]


def test_read_refs_tolerates_every_shape_a_real_mirror_can_hold():
    bs = _mod("blocker_scan")
    rec = {"number": 1, "blocker_refs": [
        {"phrase": "blocked by", "ref": "7"}, {"ref": "12"}, "42", 1, None, [], {"ref": "x"}]}
    assert bs.read_refs(rec, self_ref=1) == [("7", "blocked by"), ("12", ""), ("42", "")]
    assert bs.read_refs({"blocker_refs": "blocked by #7"}) == []      # a bare str never iterates
    assert bs.read_refs({}) == []                                     # a pre-#1487 record
    assert bs.read_refs(None) == []


def test_the_check_time_reader_delegates_to_the_one_definition():
    """`backlog_check._record_blocker_refs` keeps its name and its contract — every existing caller
    and test reads it unchanged — but it must not be a SECOND walk of the same field."""
    bc, bs = _mod("backlog_check"), _mod("blocker_scan")
    doc = {"ref": "1", "blocker_refs": [{"phrase": "blocked by", "ref": "7"},
                                        {"phrase": "blocked by", "ref": "1"}]}
    assert bc._record_blocker_refs(doc) == bs.read_refs(doc, self_ref="1") == [("7", "blocked by")]


# --- #1650: a blocker that has just closed ---------------------------------------------------------

#: The live branching trial's own pair. The mirror on disk says the blocker is OPEN; the board has
#: moved on. The mirror is TTL-FRESH in every test below — staleness here is not an expired cache,
#: it is a cache that is young and wrong, which is the only interesting case.
TRIAL_1650 = [_blocked_by(1643, 1642), _issue(1642)]
CLEARED = [_blocked_by(1643, 1642), _issue(1642, state="CLOSED")]


def test_a_blocker_closed_since_the_mirror_was_written_releases_its_dependent_on_the_same_pick(capsys):
    """#1650, the measured sequence. An operator closes the blocker by hand — precisely so the next
    goal can run — and immediately runs the loop. Pre-fix the gate read `open` off the mirror and
    held, and kept holding until the TTL expired: 60 minutes on the shipped default, during which
    the loop reports `DONE, nothing pickable` and looks exactly like a stall. Measured live with
    `ttl_minutes: 1` — DONE, then the identical command 75 seconds later returned #1643."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=TRIAL_1650)                  # fresh by TTL, and wrong
        src = _Board(["1643"], live=CLEARED)
        src.calls.clear()
        kind, goal = lp._next(base, src, _cfg(lp, base))
    assert (kind, goal) == ("goal", "1643")
    assert src.marked == ["1643"]
    assert len(src.mirror_fetches()) == 2                   # it asked the board rather than guessing
    assert "not claiming" not in capsys.readouterr().err


def test_a_blocker_that_is_genuinely_still_open_holds_after_the_refresh(capsys):
    """The half that must not move. Latency is the defect; the hold itself was always right, and a
    refresh that released a goal whose prerequisite is genuinely open would trade the correctness
    #1499 bought for the responsiveness #1650 wants."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=TRIAL_1650)
        src = _Board(["1643"], live=TRIAL_1650)             # the board agrees: still open
        src.calls.clear()
        kind, goal = lp._next(base, src, _cfg(lp, base))
    assert (kind, goal) == ("DONE", None) and src.marked == []
    assert len(src.mirror_fetches()) == 2
    assert "not claiming #1643" in capsys.readouterr().err


def test_a_refresh_that_loses_the_blocker_record_does_not_release_the_hold(capsys):
    """The refresh answers with a corpus that no longer carries the blocker at all — its goal label
    was removed, it fell past `mirror._OPEN_LIMIT`, an assignee filter now excludes it. That is LESS
    evidence than the mirror already held, not more: the last thing anyone actually observed about
    #1642 is that it was OPEN. The refreshed index is therefore MERGED over the old one rather than
    replacing it, so a record the refresh dropped keeps its last known state. Replacing would turn
    "no evidence" into a claim — fail-open is right for a ref never seen, and wrong for one seen
    open a minute ago."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=TRIAL_1650)
        src = _Board(["1643"], live=[_blocked_by(1643, 1642)])   # 1642 in neither live query
        src.calls.clear()
        kind, goal = lp._next(base, src, _cfg(lp, base))
    assert (kind, goal) == ("DONE", None) and src.marked == []
    assert "not claiming #1643" in capsys.readouterr().err


def test_a_hold_the_refresh_could_not_confirm_says_how_old_its_evidence_is(monkeypatch, capsys):
    """The degradation path — `gh` missing, offline, a `gh` error object instead of a backlog. The
    hold stands (see the test above), but a decision resting on an hour-old mirror must not read
    the same as one taken seconds after asking the board. This is the weaker half of #1650's own
    "done when", kept as the fallback for exactly the case where the stronger half cannot run."""
    lp = _loop()
    m = _mod("mirror")
    monkeypatch.setattr(lp, "_load", lambda n, _r=lp._load: m if n == "mirror" else _r(n))
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=TRIAL_1650)
        meta = pathlib.Path(base) / "state" / "board-mirror.meta.json"
        written = json.loads(meta.read_text())
        written["mirrored_at"] -= 42 * 60                   # the mirror is 42 minutes old
        meta.write_text(json.dumps(written, sort_keys=True))
        monkeypatch.setattr(m, "fetch_and_write", lambda *a, **k: None)      # no mirror, ever
        src = _Board(["1643"], live=CLEARED)
        kind, goal = lp._next(base, src, _cfg(lp, base))
    assert (kind, goal) == ("DONE", None)
    err = capsys.readouterr().err
    assert "42 minute(s) ago" in err and "#1643" in err


# --- #1650: what the refresh costs -----------------------------------------------------------------

def test_the_hold_refresh_is_paid_once_per_call_however_many_goals_are_held():
    """The cost claim for the new fetch. Three goals held behind one blocker, and the refresh is
    bought ONCE for the whole `_next()` call — per-candidate would be six `gh` calls here and would
    scale with exactly the DAG-heavy backlog this gate exists to serve."""
    lp = _loop()
    many = [_blocked_by(1643, 1642), _blocked_by(1644, 1642), _blocked_by(1645, 1642), _issue(1642)]
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=many)
        src = _Board(["1643", "1644", "1645", "1642"], live=many)
        src.calls.clear()
        kind, goal = lp._next(base, src, _cfg(lp, base))
    assert (kind, goal) == ("goal", "1642")
    assert len(src.mirror_fetches()) == 2                   # one open query + one closed query


def test_a_hold_computed_on_a_mirror_this_call_just_fetched_is_not_refreshed_again():
    """A stock repo's first pick has no mirror, so the TTL fetch at the top of the gate genuinely
    goes to the board. A hold computed off THAT is as fresh as a hold can be; forcing a second
    fetch would buy two more `gh` calls and the identical answer."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=None)                        # nothing on disk: the TTL fetch runs
        src = _Board(["1643", "1642"], live=TRIAL_1650)
        kind, goal = lp._next(base, src, _cfg(lp, base))
    assert (kind, goal) == ("goal", "1642")                 # 1643 held on genuinely live evidence
    assert len(src.mirror_fetches()) == 2                   # the TTL fetch only


def test_the_absence_refresh_and_the_hold_refresh_are_never_both_paid_in_one_call():
    """A goal that is BOTH missing from the mirror and blocked hits two refresh branches in a row.
    They share one "this call already has live data" flag, so the second one is free — without it
    the same pick bought four `gh` calls for two identical fetches moments apart."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=[_issue(1642)])              # predates #1643 entirely
        src = _Board(["1643", "1642"], live=TRIAL_1650)
        src.calls.clear()
        kind, goal = lp._next(base, src, _cfg(lp, base))
    assert (kind, goal) == ("goal", "1642")
    assert len(src.mirror_fetches()) == 2


def test_a_goal_whose_declared_prerequisites_are_all_closed_pays_nothing():
    """The boundary of the new cost. The refresh is bought by a HOLD, not by the presence of a
    dependency marker: a goal that declares a prerequisite the mirror already records as closed is
    released off the local file alone, exactly as before."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=CLEARED)
        src = _Board(["1643"], live=CLEARED)
        src.calls.clear()
        kind, goal = lp._next(base, src, _cfg(lp, base))
    assert (kind, goal) == ("goal", "1643")
    assert src.mirror_fetches() == []


def test_editing_the_marker_out_of_the_body_also_releases_the_goal_on_the_next_pick():
    """The other operator gesture with the same staleness problem. `_dependency_gate_mode` names
    editing the body as the per-goal escape from a false positive — but the marker the gate reads
    lives on the MIRROR's copy of that body, so pre-fix the edit took a TTL window to be believed
    too. The refresh re-reads the GOAL's own record, not only its blockers', so one gesture is
    enough either way."""
    lp = _loop()
    edited = [_issue(1643, body="no marker any more"), _issue(1642)]
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=TRIAL_1650)
        src = _Board(["1643"], live=edited)
        src.calls.clear()
        kind, goal = lp._next(base, src, _cfg(lp, base))
    assert (kind, goal) == ("goal", "1643")
    assert src.marked == ["1643"]


def test_a_refresh_that_cannot_be_made_is_attempted_once_not_once_per_held_goal(monkeypatch, capsys):
    """The guard the successful path cannot pin. A refresh that SUCCEEDS marks the mirror live and
    every later candidate skips the branch on that alone; a refresh that FAILS leaves the flag
    false, so without a separate once-per-call latch every one of ten held goals would re-run a
    failing `gh` — ten subprocess spawns, ten network timeouts, ten identical warnings — on a
    backlog whose whole problem is that gh is unreachable."""
    lp = _loop()
    m = _mod("mirror")
    fetches = []
    monkeypatch.setattr(lp, "_load", lambda n, _r=lp._load: m if n == "mirror" else _r(n))
    dag = [_blocked_by(1643, 1642), _blocked_by(1644, 1642), _blocked_by(1645, 1642), _issue(1642)]
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=dag)
        monkeypatch.setattr(m, "fetch_and_write", lambda *a, **k: fetches.append(k) or None)
        src = _Board(["1643", "1644", "1645"], live=CLEARED)
        kind, goal = lp._next(base, src, _cfg(lp, base))
    assert (kind, goal) == ("DONE", None)                   # all three still held, correctly
    assert len(fetches) == 2                                # the TTL attempt + ONE forced attempt
    assert capsys.readouterr().err.count("could not refresh the board mirror") == 1


def test_a_goal_absent_after_a_hold_refresh_does_not_buy_a_second_fetch():
    """The two refresh branches share one flag in BOTH directions. A hold refresh has just gone to
    the board; a later candidate that is missing from the result is missing for a reason another
    fetch moments later cannot fix, so the absence branch must read the hold refresh as live
    evidence exactly as the hold branch reads the absence refresh."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _base(d, mirror=TRIAL_1650)
        # #1646 is in neither the mirror nor the live board — a refetch can never find it
        src = _Board(["1643", "1646"], live=TRIAL_1650)
        src.calls.clear()
        kind, goal = lp._next(base, src, _cfg(lp, base))
    assert (kind, goal) == ("goal", "1646")                 # held #1643, then failed open on #1646
    assert len(src.mirror_fetches()) == 2                   # the hold refresh, and nothing after it


def test_a_sub_minute_mirror_is_described_in_seconds_not_as_zero_minutes():
    """`_mirror_age_phrase` exists to make a stale hold legible; "0 minute(s) ago" is the one output
    that would make a fresh one look stale instead."""
    lp = _loop()
    m = _mod("mirror")

    class _Age:
        def __init__(self, secs):
            self.secs = secs

        def age_seconds(self, _d):
            return self.secs

    assert lp._mirror_age_phrase(_Age(12), "x") == "seconds ago"
    assert lp._mirror_age_phrase(_Age(59.9), "x") == "seconds ago"
    assert lp._mirror_age_phrase(_Age(60), "x") == "1 minute(s) ago"
    assert lp._mirror_age_phrase(_Age(None), "x") == "at an unknown time"
    assert m.age_seconds("/nonexistent/.sdlc") is None       # the real source of that None
