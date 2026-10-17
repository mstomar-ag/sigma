"""#1550: the pick-time dependency gate (#1499) in LOCAL-GOALS mode.

A local goal whose body declares a prerequisite (`**Blocked by:** #N`, or a goal-file stem on that
marker line) is not claimed while that prerequisite's file is not `status: done`, and a ref that
names no single local goal HOLDS too, named on stderr (fail-closed: a local ref is checked against
files on disk, not an incomplete mirror). Every pick-level hold test asserts the RESOLVED item and
that `no such local goal` is absent, because under fail-closed a broken resolver still "holds".

`tests/test_pick_dependency_gate.py::test_the_gate_is_inert_outside_github_discovery` now covers a
source that is neither GitHub nor local files (no `goals_dir`, no `fetch_title_body`); its name
predates this file and is left unedited so the GitHub file stays byte-identical.
"""
import importlib.util, json, pathlib, tempfile

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def _refs(text):
    return [(r["kind"], r["ref"]) for r in _mod("blocker_scan").extract_local_refs(text)]


# --- Step 1: the local reader ----------------------------------------------------------------------

def test_local_refs_read_the_same_numeric_markers_as_github():
    assert _refs("**Blocked by:** #0007\ndepends on #3, and depends upon #12") == [
        ("id", "7"), ("id", "3"), ("id", "12")]
    assert _refs("blocked by #7\n**Blocked by:** #0007") == [("id", "7")]


def test_local_refs_accept_a_stem_on_the_canonical_marker_line():
    assert _refs("**Blocked by:** d5-a2-1-cursors") == [("stem", "d5-a2-1-cursors")]
    assert _refs("- **Blocked by:** `auth-a1.md`") == [("stem", "auth-a1")]
    assert _refs("**Blocked by:** 0007-auth-a1.") == [("stem", "0007-auth-a1")]
    assert _refs("**Blocked by:** 0007") == [("id", "7")]
    assert _refs("**Blocked by:** #0007-auth-a1") == [("id", "7")]


def test_a_stem_outside_the_canonical_marker_line_is_never_a_ref():
    for body in ("Blocked by: parser-rework", "see **Blocked by:** parser-rework",
                 "depends on parser-rework", "**Blocked by:** the parser rewrite"):
        assert _refs(body) == [], body


def test_a_marker_saying_none_is_not_a_ref():
    for value in ("none", "None.", "TBD", "nothing", "n/a", "—"):
        assert _refs("**Blocked by:** " + value) == [], value


def test_a_cross_repo_marker_is_foreign_never_a_local_id():
    assert _refs("**Blocked by:** acme/other#12") == [("foreign", "12")]


def test_unpark_qa_spans_are_stripped_before_the_local_scan():
    bs = _mod("blocker_scan")
    assert _refs("%s\n**Blocked by:** parser-rework\n%s\n" % (bs.UNPARK_QA_START, bs.UNPARK_QA_END)) == []


# --- Step 2: the goals index -----------------------------------------------------------------------

def test_goal_index_keys_each_goal_by_stem_leading_id_and_frontmatter_id():
    disc = _mod("discovery")
    with tempfile.TemporaryDirectory() as d:
        g = pathlib.Path(d)
        (g / "0007-auth-a1.md").write_text("---\nid: 0007\nstatus: done\n---\nx\n")
        (g / "d5-a2-1-cursors.md").write_text("---\nstatus: pending\n---\nx\n")
        (g / "x-auth.md").write_text("---\nid: 0042\nstatus: pending\n---\nx\n")
        (g / "README.md").write_text("# goals\n")
        idx = disc.goal_index(d)
        assert idx["7"] == idx["0007-auth-a1"] == [(str(g / "0007-auth-a1.md"), "done")]
        assert idx["d5-a2-1-cursors"] == [(str(g / "d5-a2-1-cursors.md"), "pending")]
        assert idx["42"] == idx["x-auth"] == [(str(g / "x-auth.md"), "pending")]
        assert "README" not in idx
        (g / "0007-b.md").write_text("---\nstatus: pending\n---\nx\n")
        assert len(disc.goal_index(d)["7"]) == 2


# --- Steps 3-5: the hold in `_next` --------------------------------------------------------------

def _local(d, goals, extra=None):
    """A local-goals `.sdlc`: `goals` is `(stem, status, body[, extra frontmatter])`."""
    base = pathlib.Path(d) / ".sdlc"
    (base / "state").mkdir(parents=True); (base / "goals").mkdir()
    (base / "config.json").write_text(json.dumps({"budget": {"max_iterations": 10}, **(extra or {})}))
    (base / "state" / "STATE.md").write_text("iteration: 0\nrun_iteration: 0\nlast_run: none\n")
    (base / "state" / "review-queue.md").write_text("# Q\n")
    for stem, status, body, *fm in goals:
        lead = stem.split("-", 1)[0]
        (base / "goals" / (stem + ".md")).write_text("---\n%sstatus: %s\n%s---\n%s\n" % (
            "id: %s\n" % lead if lead.isdigit() else "", status, "".join(fm), body))
    lp = _mod("loop")
    cfg = lp.state.load_config(str(base))
    return lp, str(base), lp.sources.LocalSource(str(base), cfg), cfg


def _g(base, stem):
    return str(pathlib.Path(base) / "goals" / (stem + ".md"))


def _picked(result):
    kind, goal = result
    return kind if goal is None else pathlib.Path(goal).stem


def test_a_local_goal_blocked_by_an_unfinished_id_is_not_claimed(capsys):
    with tempfile.TemporaryDirectory() as d:
        lp, base, src, cfg = _local(d, [("0001-a", "pending", "x"), ("0002-b", "pending", "**Blocked by:** #1")])
        before = pathlib.Path(_g(base, "0002-b")).read_bytes()
        assert _picked(lp._next(base, src, cfg)) == "0001-a"
        assert lp._next(base, src, cfg) == ("DONE", None)
        assert pathlib.Path(_g(base, "0002-b")).read_bytes() == before
    err = capsys.readouterr().err
    assert ("not claiming 0002-b — open prerequisite #1 (blocked by — status: in_progress). Left "
            "untouched (still status: pending, no claim)") in err
    assert "nothing pickable — 1 goal(s)" in err and "0002-b → #1 (blocked by — status: in_progress)" in err
    assert "pickable by themselves when it is done" in err and "no such local goal" not in err


def test_a_local_goal_blocked_by_a_stem_is_held_until_it_is_done(capsys):
    with tempfile.TemporaryDirectory() as d:
        lp, base, src, cfg = _local(d, [("d5-a1-decisions", "pending", "x"),
                                        ("d5-a2-cursors", "pending", "**Blocked by:** d5-a1-decisions", "priority: P0\n")])
        assert _picked(lp._next(base, src, cfg)) == "d5-a1-decisions"
        err = capsys.readouterr().err
        src.complete(_g(base, "d5-a1-decisions"))
        assert _picked(lp._next(base, src, cfg)) == "d5-a2-cursors"
    assert "not claiming d5-a2-cursors — open prerequisite d5-a1-decisions (blocked by — status: pending)" in err
    assert "no such local goal" not in err


def test_the_held_goal_is_picked_on_the_next_pass_after_its_prerequisite_is_done():
    with tempfile.TemporaryDirectory() as d:
        lp, base, src, cfg = _local(d, [("0001-a", "pending", "x"), ("0002-b", "pending", "**Blocked by:** #1")])
        before = pathlib.Path(_g(base, "0002-b")).read_bytes()
        assert _picked(lp._next(base, src, cfg)) == "0001-a"
        assert lp._next(base, src, cfg) == ("DONE", None)
        src.complete(_g(base, "0001-a"))
        assert pathlib.Path(_g(base, "0002-b")).read_bytes() == before
        assert _picked(lp._next(base, src, cfg)) == "0002-b"


def test_a_parked_or_failed_prerequisite_still_holds(capsys):
    for status in ("parked", "failed"):
        with tempfile.TemporaryDirectory() as d:
            lp, base, src, cfg = _local(d, [("0001-a", status, "x"), ("0002-b", "pending", "**Blocked by:** #1")])
            assert lp._next(base, src, cfg) == ("DONE", None), status
        err = capsys.readouterr().err
        assert "0002-b → #1 (blocked by — status: %s)" % status in err and "no such local goal" not in err
        assert "by themselves" not in err and "needs a human" in err


def test_an_in_progress_dependent_is_reported_with_its_real_status(capsys):
    with tempfile.TemporaryDirectory() as d:
        lp, base, src, cfg = _local(d, [("0001-a", "pending", "x"),
                                        ("0002-b", "in_progress", "**Blocked by:** #1", "priority: P0\n")])
        assert _picked(lp._next(base, src, cfg)) == "0001-a"
    err = capsys.readouterr().err
    assert "#1 (blocked by — status: pending). Left untouched (still status: in_progress, no claim)" in err
    assert "still status: pending" not in err


def test_next_batch_and_the_next_cli_verb_both_hold_in_local_mode(capsys):
    with tempfile.TemporaryDirectory() as d:
        lp, base, src, cfg = _local(d, [("0001-a", "pending", "x"), ("0002-b", "pending", "**Blocked by:** #1"),
                                        ("0003-c", "pending", "x")])
        picks = lp.next_batch(base, src, cfg, max_concurrent=3)
        assert sorted(_picked(p) for p in picks) == ["0001-a", "0003-c", "DONE"]
        assert "status: pending" in pathlib.Path(_g(base, "0002-b")).read_text()
        capsys.readouterr()
        assert lp.main(["loop.py", "next", base]) == 0
        assert "0002-b" not in capsys.readouterr().out


def test_a_goal_whose_only_ref_is_itself_is_not_held():
    with tempfile.TemporaryDirectory() as d:
        lp, base, src, cfg = _local(d, [("0003-c", "pending", "**Blocked by:** #3")])
        assert _picked(lp._next(base, src, cfg)) == "0003-c"


def _count_index(monkeypatch, lp):
    calls, real = [], lp.sources.discovery.goal_index
    monkeypatch.setattr(lp.sources.discovery, "goal_index", lambda g: calls.append(g) or real(g))
    return calls


def test_local_mode_costs_no_gh_and_no_index_when_nothing_is_declared(monkeypatch, capsys):
    with tempfile.TemporaryDirectory() as d:
        lp, base, src, cfg = _local(d, [("0001-a", "pending", "x"), ("0002-b", "pending", "y")])
        calls = _count_index(monkeypatch, lp)

        def no_gh(*a, **k):
            raise AssertionError("gh called in local mode")
        monkeypatch.setattr(lp.sources, "_run_gh", no_gh)
        assert _picked(lp._next(base, src, cfg)) == "0001-a"
    assert calls == [] and capsys.readouterr().err == ""


def test_the_goals_index_is_built_once_per_pick_however_many_goals_are_held(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        goals = [("000%d-x" % i, "pending", "**Blocked by:** #5") for i in range(1, 5)]
        lp, base, src, cfg = _local(d, goals + [("0005-e", "pending", "x")])
        calls = _count_index(monkeypatch, lp)
        assert lp._next(base, src, cfg, extra_skip=[_g(base, "0005-e")]) == ("DONE", None)
    assert len(calls) == 1


def test_the_gate_switched_off_also_switches_off_the_local_hold():
    with tempfile.TemporaryDirectory() as d:
        lp, base, src, cfg = _local(d, [("0001-b", "pending", "**Blocked by:** #2"), ("0002-a", "pending", "x")],
                                    extra={"discovery": {"dependency_gate": {"mode": "off"}}})
        assert _picked(lp._next(base, src, cfg)) == "0001-b"


class _Bare:
    """A source that is neither GitHub nor local files; `attrs` adds one half of the local shape."""
    def __init__(self, **attrs):
        self.__dict__.update(attrs)

    def next_pending(self, skip=()):
        return None if "1" in {str(s) for s in skip} else "1"

    def mark_in_progress(self, goal):
        pass

    def release(self, goal, reason):
        pass


def test_a_source_that_is_neither_github_nor_local_files_stays_silent(capsys):
    for attrs in ({}, {"goals_dir": "goals"}, {"fetch_title_body": lambda g: {"body": "**Blocked by:** #9"}}):
        with tempfile.TemporaryDirectory() as d:
            lp, base, _src, cfg = _local(d, [])
            assert lp._next(base, _Bare(**attrs), cfg) == ("goal", "1"), attrs
        assert capsys.readouterr().err == "", attrs


def test_a_local_read_that_raises_fails_open_and_says_so(monkeypatch, capsys):
    with tempfile.TemporaryDirectory() as d:
        lp, base, src, cfg = _local(d, [("0001-a", "pending", "x")])

        def boom(goal):
            raise RuntimeError("simulated read failure")
        monkeypatch.setattr(src, "fetch_title_body", boom)
        assert _picked(lp._next(base, src, cfg)) == "0001-a"
    assert "dependency gate failed non-fatally" in capsys.readouterr().err


# --- Step 4: unresolved, ambiguous, foreign --------------------------------------------------------

def test_a_marker_naming_no_local_goal_holds_and_names_it(capsys):
    with tempfile.TemporaryDirectory() as d:
        lp, base, src, cfg = _local(d, [("0002-b", "pending", "**Blocked by:** #99"),
                                        ("0003-c", "pending", "**Blocked by:** ghost-goal")])
        assert lp._next(base, src, cfg) == ("DONE", None)
    err = capsys.readouterr().err
    assert "#99 (blocked by — no such local goal)" in err and "ghost-goal (blocked by — no such local goal)" in err
    lines = [ln for ln in err.splitlines() if "resolve to no single local goal" in ln]
    assert any("0002-b" in ln and "#99" in ln for ln in lines)
    assert any("0003-c" in ln and "ghost-goal" in ln for ln in lines)
    assert "by themselves" not in err


def test_an_ambiguous_id_holds_and_names_it(capsys):
    with tempfile.TemporaryDirectory() as d:
        lp, base, src, cfg = _local(d, [("0001-x", "pending", "**Blocked by:** #7"),
                                        ("0007-a", "done", "x"), ("0007-b", "pending", "x")])
        assert _picked(lp._next(base, src, cfg)) == "0007-b"
    assert "#7 (blocked by — matches 2 local goals)" in capsys.readouterr().err


def test_a_cross_repo_marker_holds_and_never_resolves_to_a_local_goal(capsys):
    with tempfile.TemporaryDirectory() as d:
        lp, base, src, cfg = _local(d, [("0001-b", "pending", "**Blocked by:** acme/other#12"),
                                        ("0012-x", "done", "x")])
        assert lp._next(base, src, cfg) == ("DONE", None)
    assert "#12 (blocked by — another repository, not a local goal)" in capsys.readouterr().err


# --- Step 5: proposed ------------------------------------------------------------------------------

def test_a_proposed_goal_is_never_picked_even_when_its_prerequisite_is_done():
    with tempfile.TemporaryDirectory() as d:
        lp, base, src, cfg = _local(d, [("0001-a", "done", "x"), ("0002-b", "proposed", "**Blocked by:** #1")])
        assert lp._next(base, src, cfg) == ("DONE", None)
        assert src.release(_g(base, "0002-b"), "") is False
        assert "status: proposed" in pathlib.Path(_g(base, "0002-b")).read_text()
