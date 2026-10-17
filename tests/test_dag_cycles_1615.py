"""#1615: a dependency cycle is reported and held, never silently waited on.

Two goals that block each other can never be picked, and before this change the pick summary said they
"become pickable by themselves", which is false for a cycle. The stored map (`dag_store`) already knows
the cycle; `dag_cycles.py` reads it and reports it in the pick output, `/sigma-status` and the doctor,
and (when `discovery.dag.cycle_comment` is true and the mode is not `off`) posts ONE comment per
distinct cycle on its lowest-numbered OPEN member.

No network, no model: `gh` is a scripted fake that records every call. Fixture issues are numbered
10, 11, 20, 21, 30 and nothing here is a real issue.

Each guard was run red once against its own deliberate break (one clause at a time); the `control:`
line in a docstring names the break:
  * remove the marker check (the read of the member's comments)     -> duplicate comment
  * choose the lowest member instead of the lowest OPEN member      -> comment on a closed issue
  * remove the `cycle_comment` gate                                 -> comment posted when switched off
  * remove the mode gate                                            -> comment posted / cycles found in off
"""
import importlib.util
import json
import pathlib
import re
import sys
import tempfile
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

ROOT = pathlib.Path(__file__).resolve().parent.parent
S = ROOT / "skills" / "sigma-loop" / "scripts"
NOW = 1_800_000_000.0


def _mod(name, base=S):
    spec = importlib.util.spec_from_file_location(name, base / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _cycles():
    return _mod("dag_cycles")


_gate = _mod("test_pick_dependency_gate", pathlib.Path(__file__).resolve().parent)
_Board, _base, _issue, _cfg, _loop = _gate._Board, _gate._base, _gate._issue, _gate._cfg, _gate._loop
doctor = _mod("doctor", ROOT / "skills" / "sigma-doctor" / "scripts")
status = _mod("status", ROOT / "skills" / "sigma-status" / "scripts")


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _node(n, state="open"):
    return {"number": n, "kind": "issue", "state": state, "life": "goal", "prio": None, "in_corpus": True}


def _edge(blocker, blocked):
    return {"blocker": blocker, "blocked": blocked, "source": "body"}


def _store(sdlc, nodes, edges):
    """Write the stored map directly (mode must not be `off` in the config on disk)."""
    ds = _mod("dag_store")
    h = {"repo": "acme/widget", "built_at": _iso(time.time() - 60), "refreshed_at": _iso(time.time() - 60),
         "last_ok_at": _iso(time.time() - 60), "last_error": None, "partial": False}
    ok, why = ds.save(sdlc, {"header": h, "nodes": nodes, "edges": edges}, config={})
    assert ok, why


def _write_cfg(sdlc, **dag):
    p = pathlib.Path(sdlc) / "config.json"
    cfg = json.loads(p.read_text())
    cfg.setdefault("discovery", {})["dag"] = dag
    p.write_text(json.dumps(cfg))
    return cfg


def _blocked(number, ref):
    return _issue(number, body="context.\n\n**Blocked by:** #%d\n" % ref)


# two cycles (10<->11, and 20->21->22->20) plus one independent goal, 30
CYC_NODES = [_node(n) for n in (10, 11, 20, 21, 22, 30)]
CYC_EDGES = [_edge(11, 10), _edge(10, 11), _edge(22, 20), _edge(20, 21), _edge(21, 22)]
MIRROR = [_blocked(10, 11), _blocked(11, 10), _blocked(20, 22), _blocked(21, 20), _blocked(22, 21),
          _issue(30)]


class Gh:
    """A scripted `gh api`: issue comments are kept per issue; every call is recorded."""

    def __init__(self, existing=None, states=None):
        self.calls = []
        self.comments = {k: list(v) for k, v in (existing or {}).items()}

    def __call__(self, args):
        self.calls.append(list(args))
        ep, method = args[1], args[args.index("--method") + 1]
        m = re.search(r"issues/(\d+)(/comments)?$", ep)
        n = int(m.group(1))
        if method == "GET" and not m.group(2):
            return json.dumps({"number": n, "comments": len(self.comments.get(n, []))})
        if method == "GET":
            return json.dumps(self.comments.get(n, []))
        assert method == "POST" and m.group(2), args
        body = next(a[len("body="):] for a in args if a.startswith("body="))
        self.comments.setdefault(n, []).append({"body": body, "created_at": "2026-01-01T00:00:00Z"})
        return json.dumps({"id": 1, "html_url": "https://example.invalid/c"})

    def posts(self):
        return [c for c in self.calls if "POST" in c]


def _sdlc(d, nodes=CYC_NODES, edges=CYC_EDGES, mirror=MIRROR, **dag):
    base = _base(d, mirror=mirror)
    _write_cfg(base, **({"mode": "shadow"} | dag))
    if dag.get("mode") == "off":
        nodes = None
    if nodes is not None:
        _store(base, nodes, edges)
    return base


# ----------------------------------------------------------------- 1. the characterization

def test_characterization_a_two_goal_cycle_is_held_and_said_to_release_by_itself(capsys):
    """Today's deadlock, pinned (mode `off`, which is byte-for-byte today's output): two goals blocking each other are both held and
    the summary promises they become pickable by themselves. Passes on the unchanged code; the next
    test is its updated twin once a stored map holds the cycle."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _sdlc(d, nodes=None, mode="off")
        src = _Board(["10", "11"], live=MIRROR)
        assert lp._next(base, src, _cfg(lp, base)) == ("DONE", None)
    err = capsys.readouterr().err
    assert src.marked == []
    assert "nothing pickable" in err and "#10" in err and "#11" in err
    assert "become pickable by themselves" in err


def test_with_a_stored_cycle_the_summary_no_longer_promises_self_release(capsys):
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _sdlc(d)
        src = _Board(["10", "11"], live=MIRROR)
        assert lp._next(base, src, _cfg(lp, base)) == ("DONE", None)
    err = capsys.readouterr().err
    assert src.marked == [] and src.released == []
    assert "become pickable by themselves" not in err
    assert "loop.py: stuck: #10, #11 wait on each other" in err      # the root-cause line (#1614) names both


# ----------------------------------------------------------------- 2. every member, three surfaces

def test_pick_output_names_every_member_of_each_distinct_cycle(capsys):
    """Every held goal waits on a cycle: the root-cause lines (#1614) name every member of each cycle."""
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _sdlc(d)
        src = _Board(["10", "11", "20", "21", "22"], live=MIRROR)
        lp._next(base, src, _cfg(lp, base))
    err = capsys.readouterr().err
    assert "become pickable by themselves" not in err
    assert "#10, #11 wait on each other" in err and "#20, #21, #22 wait on each other" in err


def test_pick_output_names_every_cycle_member_when_another_held_goal_waits_on_plain_work(capsys):
    """A held goal that waits on ordinary open work is not a stuck root, so the #1614 lines do not apply;
    the cycle lines of this change still name every member, and the old promise is not made for them."""
    nodes = CYC_NODES + [_node(40), _node(41)]
    edges = CYC_EDGES + [_edge(41, 40)]
    mirror = MIRROR + [_blocked(40, 41), _issue(41)]
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _sdlc(d, nodes=nodes, edges=edges, mirror=mirror)
        src = _Board(["10", "11", "20", "21", "22", "40"], live=mirror)
        lp._next(base, src, _cfg(lp, base))
    err = capsys.readouterr().err
    assert "loop.py: dependency cycle: #10, #11" in err
    assert "loop.py: dependency cycle: #20, #21, #22" in err
    assert err.count("loop.py: dependency cycle") == 2
    assert "never releases by itself" in err and "become pickable by themselves" not in err


def test_status_names_every_member_of_each_distinct_cycle():
    with tempfile.TemporaryDirectory() as d:
        base = _sdlc(d)
        seg = status._dag_cycles_segment(base)
    assert "dependency cycle: #10, #11 -> remove one Blocked by line" in seg
    assert "dependency cycle: #20, #21, #22 -> remove one Blocked by line" in seg


def test_doctor_names_every_member_of_each_distinct_cycle():
    with tempfile.TemporaryDirectory() as d:
        base = _sdlc(d)
        row = doctor._dag_cycles_row(json.loads((pathlib.Path(base) / "config.json").read_text()), base)
    assert row["ok"] is False
    for n in (10, 11, 20, 21, 22):
        assert "#%d" % n in row["name"]
    assert row["name"].startswith("dag cycles")
    assert "Blocked by" in row["fix"]
    assert "#30" not in row["name"]


# ----------------------------------------------------------------- 3. one comment per distinct cycle

def test_one_comment_per_cycle_on_the_lowest_open_member_and_a_second_refresh_posts_nothing():
    """control: remove the marker check (never read the member's comments) -> the second call posts
    a duplicate and the `posts == 2` assertion goes red."""
    dc = _cycles()
    with tempfile.TemporaryDirectory() as d:
        base = _sdlc(d)
        cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
        gh = Gh()
        dc.post(base, cfg, gh)
        assert sorted(gh.comments) == [10, 20]
        assert len(gh.posts()) == 2
        dc.post(base, cfg, gh)
        assert len(gh.posts()) == 2
        assert all(len(v) == 1 for v in gh.comments.values())


def test_a_changed_member_set_posts_exactly_one_new_comment():
    dc = _cycles()
    with tempfile.TemporaryDirectory() as d:
        base = _sdlc(d)
        cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
        gh = Gh()
        dc.post(base, cfg, gh)
        # cycle 10<->11 grows to 10,11,12
        _store(base, CYC_NODES + [_node(12)], CYC_EDGES + [_edge(11, 12), _edge(12, 11)])
        dc.post(base, cfg, gh)
        assert len(gh.posts()) == 3
        assert len(gh.comments[10]) == 2 and len(gh.comments[20]) == 1


def test_the_comment_goes_on_the_lowest_open_member_not_the_lowest_member():
    """control: pick the lowest member instead -> the comment lands on closed #10 and this is red.
    Member #10 is `unknown` (unresolved, so in the cycle, but not open); #9 is closed and outside it."""
    dc = _cycles()
    with tempfile.TemporaryDirectory() as d:
        base = _sdlc(d, nodes=[_node(9, "closed"), _node(10, "unknown"), _node(11), _node(12)],
                     edges=[_edge(11, 12), _edge(12, 10), _edge(10, 11), _edge(9, 11)])
        cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
        gh = Gh()
        dc.post(base, cfg, gh)
    assert sorted(gh.comments) == [11]


def test_a_hundred_comments_before_the_marker_do_not_hide_it():
    """The marker check reads the whole comment list (all pages), not the newest few."""
    dc = _cycles()
    with tempfile.TemporaryDirectory() as d:
        base = _sdlc(d)
        cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
        marker = dc.marker((10, 11))
        gh = Gh({10: [{"body": marker, "created_at": "2026-01-01T00:00:00Z"}] + [
            {"body": "x", "created_at": "2026-01-02T00:00:00Z"}] * 120})
        dc.post(base, cfg, gh)
    assert 10 not in [int(re.search(r"issues/(\d+)/comments$", c[1]).group(1)) for c in gh.posts()]


def test_the_poster_is_bounded_and_never_raises():
    dc = _cycles()
    with tempfile.TemporaryDirectory() as d:
        pairs = [(100 + 2 * i, 101 + 2 * i) for i in range(30)]
        nodes = [_node(n) for p in pairs for n in p]
        edges = [e for a, b in pairs for e in (_edge(a, b), _edge(b, a))]
        base = _sdlc(d, nodes=nodes, edges=edges)
        cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
        gh = Gh()
        dc.post(base, cfg, gh)
        assert len(gh.posts()) == dc.MAX_CYCLES == 20

        def boom(args):
            raise RuntimeError("scripted outage")
        assert dc.post(base, cfg, boom) is not None or True      # returns, does not raise


# ----------------------------------------------------------------- 4. the two switches

def test_cycle_comment_false_posts_nothing():
    """control: remove the `cycle_comment` gate -> posts happen and this is red."""
    dc = _cycles()
    with tempfile.TemporaryDirectory() as d:
        base = _sdlc(d, cycle_comment=False)
        cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
        gh = Gh()
        dc.post(base, cfg, gh)
        assert gh.calls == []
        assert dc.find(base, cfg) == [(10, 11), (20, 21, 22)]      # the report is not switched off


def test_mode_off_posts_nothing_and_finds_nothing():
    """control: remove the mode gate -> find() returns the cycles and/or a comment is posted."""
    dc = _cycles()
    with tempfile.TemporaryDirectory() as d:
        base = _sdlc(d)
        cfg = _write_cfg(base, mode="off")
        gh = Gh()
        dc.post(base, cfg, gh)
        assert gh.calls == []
        assert dc.find(base, cfg) == []
        assert dc.lines(base, cfg) == []


def test_find_is_empty_without_a_usable_map():
    dc = _cycles()
    with tempfile.TemporaryDirectory() as d:
        base = _sdlc(d, nodes=None)
        cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
        assert dc.find(base, cfg) == []
        (pathlib.Path(base) / "state" / "dag.json").write_text("{not json")
        assert dc.find(base, cfg) == []


# ----------------------------------------------------------------- 5. never claims, never labels

def test_members_are_never_claimed_and_the_poster_writes_only_comments():
    lp = _loop()
    dc = _cycles()
    with tempfile.TemporaryDirectory() as d:
        base = _sdlc(d)
        cfg = _cfg(lp, base)
        src = _Board(["10", "11", "20", "21", "22"], live=MIRROR)
        lp._next(base, src, cfg)
        assert src.marked == [] and src.released == []
        gh = Gh()
        dc.post(base, cfg, gh)
    for c in gh.calls:
        if "POST" in c:
            assert re.search(r"issues/\d+/comments$", c[1]), c
        else:
            assert "GET" in c, c
        assert not any("label" in str(a) for a in c)


# ----------------------------------------------------------------- 6. the text is numbers only

def test_the_comment_text_is_numbers_and_the_fixed_instruction_only():
    dc = _cycles()
    body = dc.comment_body((10, 11, 20))
    assert body.startswith("<!-- sigma:dag-cycle 10,11,20 -->")
    rest = body.split("-->", 1)[1].replace(dc.INSTRUCTION, "")
    # no title or name can enter: the only variable part is `#N, #N` numbers
    assert re.fullmatch(r"\s*These goals block each other: #10, #11, #20\.\s*", rest)
    assert "Blocked by" in dc.INSTRUCTION
    assert not re.search(r"[A-Za-z]", re.sub(r"These goals block each other|Remove one .Blocked by. line.*picked|sigma:dag-cycle",
                                            "", body))


# ----------------------------------------------------------------- 7. independent goal is picked

def test_an_independent_goal_is_picked_beside_a_cycle():
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _sdlc(d)
        src = _Board(["10", "11", "30"], live=MIRROR)
        assert lp._next(base, src, _cfg(lp, base)) == ("goal", "30")
    assert src.marked == ["30"]


# ----------------------------------------------------------------- 8. off-mode byte identity

def test_mode_off_pick_status_and_doctor_are_unchanged(capsys):
    lp = _loop()
    with tempfile.TemporaryDirectory() as d:
        base = _sdlc(d)
        cfg = _write_cfg(base, mode="off")
        src = _Board(["10", "11"], live=MIRROR)
        lp._next(base, src, _cfg(lp, base))
        err = capsys.readouterr().err
        assert "become pickable by themselves" in err and "dependency cycle" not in err
        assert status._dag_cycles_segment(base) == ""
        assert doctor._dag_cycles_row(cfg, base) is None


def test_the_off_pick_path_does_not_load_dag_cycles(capsys):
    """control: remove the mode gate in `loop._held_cycles` -> `dag_cycles` is loaded and this is red."""
    lp = _loop()
    loaded = []
    real = lp._load
    lp._load = lambda name: (loaded.append(name), real(name))[1]
    try:
        with tempfile.TemporaryDirectory() as d:
            base = _sdlc(d, mode="off")
            src = _Board(["10", "11"], live=MIRROR)
            lp._next(base, src, _cfg(lp, base))
    finally:
        lp._load = real
    assert "dag_cycles" not in loaded


# ----------------------------------------------------------------- the refresh hook

def test_a_saved_refresh_posts_the_cycle_comment_once():
    dr = _mod("dag_refresh")
    calls = []

    class FakeCycles:
        @staticmethod
        def post(sdlc, config, run):
            calls.append(config)

    with tempfile.TemporaryDirectory() as d:
        base = _sdlc(d, nodes=None)
        cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
        dr.dag_cycles = FakeCycles
        dr.dag_collect.refresh = lambda *a, **k: {"status": "saved"}
        out = dr.refresh_if_due(base, cfg, run=Gh(), now=NOW, force=True)
        assert out["status"] == "saved" and len(calls) == 1
        dr.dag_collect.refresh = lambda *a, **k: {"status": "kept"}
        dr.refresh_if_due(base, cfg, run=Gh(), now=NOW, force=True)
        assert len(calls) == 1                                   # only a saved refresh posts

        class Boom:
            @staticmethod
            def post(*a):
                raise RuntimeError("x")
        dr.dag_cycles = Boom
        dr.dag_collect.refresh = lambda *a, **k: {"status": "saved"}
        assert dr.refresh_if_due(base, cfg, run=Gh(), now=NOW, force=True)["status"] == "saved"
