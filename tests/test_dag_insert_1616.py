"""#1616: a goal Sigma files enters the stored map at once, and a `Blocked by` that closes a cycle is refused.

The stored map (`dag_store`) is rebuilt by a refresh once per TTL. `dag_insert.py` adds a freshly filed
issue and its edges to it immediately, and `GitHubSource.append_to_body` asks it, BEFORE writing, whether
a `Blocked by #N` line would close a cycle; if so it raises `CycleRefused` naming every member and writes
nothing. With no usable map the answer comes from a bounded live walk (25 reads); over the cap the write
is allowed and the pair is recorded as `unchecked` for the next refresh to examine.

No network, no model: `gh` is a scripted fake that keeps its own issues and records every call. Fixture
issues are numbered 7, 8, 9, 20, 21 and 100 upward; nothing here is a real issue.

Each guard was run red once against its own deliberate break (one clause at a time); the `control:` line
in a docstring names the break:
  * skip the insertion in `create_dependency`                       -> test 1 / compile-plan test red
  * skip the insertion in `append_to_body`                          -> the edge test red
  * remove the cycle guard in `append_to_body`                      -> the refusal test red
  * live walk cap `>` instead of `>=` (one read too many)           -> the cap test red
  * live walk cap off by one the other way (one read too few)       -> the within-cap test red
  * do not record `unchecked`                                       -> the over-the-cap test red
  * remove the mode gate in `GitHubSource` / `insert`               -> the mode-off tests red
"""
import importlib.util
import json
import pathlib
import re
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

ROOT = pathlib.Path(__file__).resolve().parent.parent
S = ROOT / "skills" / "sigma-loop" / "scripts"
SCOPE = ROOT / "skills" / "sigma-scope" / "scripts"


def _mod(name, base=S):
    spec = importlib.util.spec_from_file_location(name, base / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


dag_insert = _mod("dag_insert")
dag_store = _mod("dag_store")
dag_refresh = _mod("dag_refresh")
sources = _mod("sources")
handoff = _mod("handoff")
compile_plan = _mod("compile_plan", SCOPE)


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _node(n, state="open"):
    return {"number": n, "kind": "issue", "state": state, "life": "goal", "prio": None, "in_corpus": True}


def _edge(blocker, blocked):
    return {"blocker": blocker, "blocked": blocked, "source": "body"}


def _store(sdlc, nodes, edges, age=60):
    h = {"repo": "o/r", "built_at": _iso(time.time() - age), "refreshed_at": _iso(time.time() - age),
         "last_ok_at": _iso(time.time() - age), "last_error": None, "partial": False}
    ok, why = dag_store.save(sdlc, {"header": h, "nodes": nodes, "edges": edges}, config={})
    assert ok, why


def _read_map(sdlc):
    m, why = dag_store.load(sdlc, {})
    assert m is not None, why
    return m


def _edges(sdlc):
    return {(e["blocker"], e["blocked"]) for e in _read_map(sdlc)["edges"]}


def _numbers(sdlc):
    return {n["number"] for n in _read_map(sdlc)["nodes"]}


class Gh:
    """A scripted `gh`: keeps issues by number, records every call. A POST to /issues files number 100+."""

    def __init__(self, issues=None, fail_reads=()):
        self.issues = {n: {"body": b, "state": "open"} for n, b in (issues or {}).items()}
        self.calls = []
        self.next = 100
        self.fail_reads = set(fail_reads)

    def __call__(self, args):
        self.calls.append(list(args))
        if args[0] != "api":
            return ""
        ep = args[1]
        method = args[args.index("--method") + 1] if "--method" in args else "GET"
        fields = {a.split("=", 1)[0]: a.split("=", 1)[1] for a in args if "=" in a and not a.startswith("-")}
        m = re.search(r"/issues/(\d+)$", ep)
        if m:
            n = int(m.group(1))
            if method == "GET":
                if n in self.fail_reads or n not in self.issues:
                    raise RuntimeError("HTTP 404 Not Found")
                i = self.issues[n]
                return json.dumps({"number": n, "title": "t", "state": i["state"], "body": i["body"],
                                   "comments": 0, "labels": [], "user": {"login": "x"}})
            if method == "PATCH" and "body" in fields:
                self.issues[n]["body"] = fields["body"]
            return json.dumps({"number": n})
        if ep.endswith("/issues") and method == "POST":
            n, self.next = self.next, self.next + 1
            self.issues[n] = {"body": fields.get("body", ""), "state": "open"}
            return json.dumps({"number": n})
        return "{}"

    def reads(self):
        return [c for c in self.calls if c[0] == "api" and re.search(r"/issues/\d+$", c[1])
                and "--method" in c and c[c.index("--method") + 1] == "GET"]

    def writes_to(self, n):
        return [c for c in self.calls if c[0] == "api" and c[1].endswith("/issues/%d" % n)
                and "PATCH" in c]


def _cfg(mode="shadow", **extra):
    return {"discovery": {"source": "github", "github": {"repo": "o/r"}, "dag": {"mode": mode}},
            "ledger": {"enabled": True, "actor": "amy"}, **extra}


def _setup(tmp_path, issues=None, nodes=None, edges=None, mode="shadow", age=60, **gh_kw):
    sdlc = tmp_path / ".sdlc"
    (sdlc / "state").mkdir(parents=True)
    cfg = _cfg(mode)
    (sdlc / "config.json").write_text(json.dumps(cfg))
    if nodes is not None:
        _store(sdlc, nodes, edges or [], age=age)
    gh = Gh(issues, **gh_kw)
    src = sources.GitHubSource(cfg, run=gh, sdlc_dir=str(sdlc))
    src._RETRY_BASE = 0
    return sdlc, cfg, gh, src


class _refused:
    """`pytest.raises` for `CycleRefused` by NAME: `sources` loads its own copy of `dag_insert`, so the class
    object differs from this module's and an `isinstance` match would be wrong however the code behaves."""

    def __enter__(self):
        self.value = None
        return self

    def __exit__(self, typ, val, tb):
        assert typ is not None, "expected CycleRefused, nothing was raised"
        assert typ.__name__ == "CycleRefused", repr(val)
        self.value = val
        return True


def _blocked_by(n):
    return "context.\n\n**Blocked by:** #%d\n" % n


# a map with 7 <- 8 <- 9 (9 waits on 8, 8 waits on 7) and an unrelated 20
CHAIN_NODES = [_node(n) for n in (7, 8, 9, 20)]
CHAIN_EDGES = [_edge(7, 8), _edge(8, 9)]


# ----------------------------------------------------------------- 1. a filed follow-up is in the map at once

def test_followup_filed_through_create_tracked_issue_is_in_the_map_with_its_edge(tmp_path):
    """control: skip the insertion in `create_dependency` (node 100 absent) and in `append_to_body`
    (edge (100, 7) absent) -> red, each on its own."""
    sdlc, cfg, gh, src = _setup(tmp_path, issues={7: "goal text"}, nodes=[_node(7)], edges=[])
    rep = handoff.create_tracked_issue(str(sdlc), cfg, "7", "engine", "found a thing", same_area=True,
                                       immediately_actionable=True, blocks_goal=True, source=src,
                                       dedup=False)
    assert rep["issue"] == "100" and not [w for w in rep["warnings"] if "blocker" in w]
    assert 100 in _numbers(sdlc)                      # the node, before any refresh
    assert (100, 7) in _edges(sdlc)                   # the Blocked by marker's edge
    node = next(n for n in _read_map(sdlc)["nodes"] if n["number"] == 100)
    assert node["state"] == "open" and node["in_corpus"] is True and node["kind"] == "issue"


def test_create_dependency_parses_blocked_by_lines_of_the_body_it_files(tmp_path):
    sdlc, cfg, gh, src = _setup(tmp_path, issues={7: ""}, nodes=[_node(7)], edges=[])
    n = src.create_dependency("t", "text\n\n**Blocked by:** #7\n", None)
    assert n == "100"
    assert (7, 100) in _edges(sdlc) and 100 in _numbers(sdlc)


# ----------------------------------------------------------------- 2. the plan compiler

def _plan(*issues):
    return {"epic": None, "issues": list(issues)}


def _pi(key, title, blocked_by=()):
    return {"key": key, "title": title, "body": "body text", "priority": "P2", "blocked_by": list(blocked_by)}


def test_compile_plan_inserts_every_issue_and_edge_in_one_pass(tmp_path):
    """control: skip the insertion in `create_dependency` -> no node and no edge, red."""
    sdlc, cfg, gh, src = _setup(tmp_path, nodes=[_node(7)], edges=[])
    rep = compile_plan.compile_plan(str(sdlc), cfg, _plan(_pi("a", "First"), _pi("b", "Second", ["a"]),
                                                          _pi("c", "Third", ["a", "b"])), source=src)
    a, b, c = (int(rep["issues"][k]) for k in "abc")
    assert {a, b, c} <= _numbers(sdlc)
    assert {(a, b), (a, c), (b, c)} <= _edges(sdlc)


def test_compile_plan_in_plan_cycle_still_fails_closed_with_zero_side_effects(tmp_path):
    sdlc, cfg, gh, src = _setup(tmp_path, nodes=[_node(7)], edges=[])
    before = (sdlc / "state" / "dag.json").read_bytes()
    with pytest.raises(ValueError, match="cycle"):
        compile_plan.compile_plan(str(sdlc), cfg, _plan(_pi("a", "A", ["b"]), _pi("b", "B", ["a"])),
                                  source=src)
    assert gh.calls == []                                           # nothing created, nothing read
    assert (sdlc / "state" / "dag.json").read_bytes() == before     # the map is untouched
    assert not (sdlc / "state" / dag_insert.UNCHECKED_FILE).exists()


# ----------------------------------------------------------------- 3. the cycle guard on append_to_body

def test_append_to_body_refuses_a_blocked_by_that_closes_a_cycle_and_names_the_members(tmp_path):
    """9 waits on 8 waits on 7; writing `7 Blocked by 9` closes 7 -> 8 -> 9 -> 7.
    control: remove the guard in `append_to_body` -> the write happens and no error is raised, red."""
    sdlc, cfg, gh, src = _setup(tmp_path, issues={7: "goal 7", 9: "goal 9"}, nodes=CHAIN_NODES,
                                edges=CHAIN_EDGES)
    with _refused() as ei:
        src.append_to_body("7", "**Blocked by:** #9")
    assert ei.value.members == [7, 8, 9]
    assert all("#%d" % n in str(ei.value) for n in (7, 8, 9)) and "#20" not in str(ei.value)
    assert gh.writes_to(7) == [] and gh.issues[7]["body"] == "goal 7"        # nothing written
    assert (9, 7) not in _edges(sdlc)


def test_append_to_body_writes_a_harmless_blocked_by_and_inserts_its_edge(tmp_path):
    sdlc, cfg, gh, src = _setup(tmp_path, issues={7: "goal 7", 20: "x"}, nodes=CHAIN_NODES, edges=CHAIN_EDGES)
    src.append_to_body("20", "**Blocked by:** #9")
    assert gh.issues[20]["body"].rstrip().endswith("**Blocked by:** #9")
    assert (9, 20) in _edges(sdlc)


def test_a_marker_without_blocked_by_is_written_as_before_and_the_map_is_not_touched(tmp_path):
    sdlc, cfg, gh, src = _setup(tmp_path, issues={7: "goal 7"}, nodes=CHAIN_NODES, edges=CHAIN_EDGES)
    before = (sdlc / "state" / "dag.json").read_bytes()
    src.append_to_body("7", "Part of epic #50.")
    assert gh.issues[7]["body"] == "goal 7\n\nPart of epic #50.\n"
    assert (sdlc / "state" / "dag.json").read_bytes() == before
    assert len(gh.reads()) == 1                                            # the body read, nothing else


def test_closing_members_stored_map_clauses_each_on_their_own(tmp_path):
    """control: (a) test `blocker in down` against the wrong end; (b) leave out the blocker's upstream
    intersection (members include the unrelated branch); (c) walk resolved nodes (a closed middle goal
    still reads as a cycle)."""
    sdlc, cfg, gh, src = _setup(tmp_path, nodes=CHAIN_NODES + [_node(30), _node(31)],
                                edges=CHAIN_EDGES + [_edge(8, 30), _edge(30, 31)])
    cm = lambda a, b: dag_insert.closing_members(str(sdlc), cfg, a, b)         # noqa: E731
    assert cm(9, 7) == [7, 8, 9]                                               # members: only the paths
    assert cm(7, 9) == []                                                      # the other direction is fine
    assert cm(20, 7) == []
    assert cm(31, 7) == [7, 8, 30, 31]                                         # a branch on a path counts
    _store(sdlc, [_node(7), _node(8, "closed"), _node(9)], CHAIN_EDGES)
    assert cm(9, 7) == []                                                      # a closed middle cuts it


# ----------------------------------------------------------------- 4. no usable map: the live walk

def _chain_issues(length, closing=True):
    """Issues 200.. where 200 + i is blocked by 200 + i + 1; the last one is blocked by 7 when `closing`."""
    d = {7: "goal 7"}
    for i in range(length):
        d[200 + i] = _blocked_by(200 + i + 1) if i < length - 1 else (_blocked_by(7) if closing else "end")
    return d


def test_no_map_the_live_walk_finds_a_cycle_within_the_cap_and_refuses(tmp_path):
    """The goal is 7; adding `7 Blocked by 200` with 200 -> 201 -> 7 closes 7 -> 201 -> 200 -> 7... here
    200 is blocked by 201 and 201 by 7, so 7 would wait (transitively) on itself.
    control: cap `>` instead of `>=` or one read too few -> the within-cap / over-cap tests go red."""
    sdlc, cfg, gh, src = _setup(tmp_path, issues=_chain_issues(2))
    with _refused() as ei:
        src.append_to_body("7", "**Blocked by:** #200")
    assert ei.value.members == [7, 200, 201]
    assert gh.writes_to(7) == []
    assert [c[1].rsplit("/", 1)[1] for c in gh.reads()] == ["200", "201"]        # the walk, nothing else
    assert not (sdlc / "state" / dag_insert.UNCHECKED_FILE).exists()


def test_live_walk_with_exactly_the_cap_of_reads_still_decides():
    """25 issues in a row, the last one blocked by the goal: the 25th read finds it. One read fewer in the
    budget (a cap of 24) cannot. control: `>=` -> `>` lets a 26th read happen; `cap - 1` loses this one."""
    gh = Gh(_chain_issues(25))
    reader = dag_insert.default_reader(gh, "o/r")
    got = dag_insert._live(200, 7, lambda n: reader(n), 25)
    assert got is not None and got[0] == 7 and 200 in got and 224 in got
    assert len(gh.reads()) == 25
    gh2 = Gh(_chain_issues(25))
    assert dag_insert._live(200, 7, dag_insert.default_reader(gh2, "o/r"), 24) is None
    assert len(gh2.reads()) == 24                                      # and never a 25th


def test_over_the_cap_the_write_is_allowed_and_recorded_as_unchecked(tmp_path):
    """A chain of 40 never reaches the goal inside 25 reads. control: do not record `unchecked` -> red;
    refuse on a cap hit instead of allowing -> red."""
    sdlc, cfg, gh, src = _setup(tmp_path, issues=_chain_issues(40, closing=False))
    src.append_to_body("7", "**Blocked by:** #200")
    assert "Blocked by:** #200" in gh.issues[7]["body"]                # allowed
    assert len([c for c in gh.reads() if not c[1].endswith("/issues/7")]) == dag_insert.LIVE_READ_CAP
    assert dag_insert.unchecked(str(sdlc)) == [[200, 7]]


def test_a_failed_live_read_is_undecided_not_a_cycle_and_not_a_crash(tmp_path):
    sdlc, cfg, gh, src = _setup(tmp_path, issues=_chain_issues(3), fail_reads={201})
    src.append_to_body("7", "**Blocked by:** #200")
    assert dag_insert.unchecked(str(sdlc)) == [[200, 7]]


def test_a_stale_map_is_not_trusted_the_live_walk_decides(tmp_path):
    sdlc, cfg, gh, src = _setup(tmp_path, issues=_chain_issues(2), nodes=[_node(7)], edges=[],
                                age=10 * 24 * 3600)
    with _refused():
        src.append_to_body("7", "**Blocked by:** #200")


def test_the_unchecked_list_is_bounded_numbers_only_and_cleared_by_a_saved_refresh(tmp_path):
    sdlc = tmp_path / ".sdlc"
    (sdlc / "state").mkdir(parents=True)
    cfg = _cfg()
    for i in range(dag_insert.UNCHECKED_CAP + 25):
        assert dag_insert.record_unchecked(str(sdlc), 1000 + i, 5, cfg)
    pairs = dag_insert.unchecked(str(sdlc))
    assert len(pairs) == dag_insert.UNCHECKED_CAP and pairs[-1] == [1000 + dag_insert.UNCHECKED_CAP + 24, 5]
    assert re.fullmatch(r"[\[\],0-9\n]+", (sdlc / "state" / dag_insert.UNCHECKED_FILE).read_text())
    assert dag_insert.take_unchecked(str(sdlc)) == pairs and dag_insert.unchecked(str(sdlc)) == []


def test_dag_refresh_clears_the_unchecked_list_after_a_saved_refresh_and_only_then(tmp_path, monkeypatch):
    """control: do not clear in `refresh_if_due` -> red; clear on a kept (failed) refresh too -> red."""
    sdlc, cfg, gh, src = _setup(tmp_path)
    dag_insert.record_unchecked(str(sdlc), 200, 7, cfg)
    for status, left in (("kept", [[200, 7]]), ("saved", [])):
        monkeypatch.setattr(dag_refresh.dag_collect, "refresh",
                            lambda *a, _s=status, **k: {"status": _s, "last_error": None})
        monkeypatch.setattr(dag_refresh, "_post_cycles", lambda *a, **k: None)
        dag_refresh.refresh_if_due(str(sdlc), cfg, run=gh, force=True)
        assert dag_insert.unchecked(str(sdlc)) == left


# ----------------------------------------------------------------- 5. the map never fails a filing

def test_an_insertion_failure_never_fails_the_filing_and_prints_one_line(tmp_path, monkeypatch, capsys):
    """control: let the exception out of `insert` / out of the `create_dependency` hook -> red."""
    sdlc, cfg, gh, src = _setup(tmp_path, nodes=[_node(7)], edges=[])
    monkeypatch.setattr(dag_store, "save", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    real = sources._load

    def patched(name):
        m = real(name)
        if name == "dag_insert":
            m.dag_store = dag_store
        return m
    monkeypatch.setattr(sources, "_load", patched)
    n = src.create_dependency("t", "body", None)
    assert n == "100" and 100 in gh.issues                              # the issue was still created
    err = capsys.readouterr().err
    assert len([ln for ln in err.splitlines() if "dependency map insert skipped" in ln]) == 1


def test_insert_itself_never_raises_and_says_error(tmp_path, monkeypatch, capsys):
    sdlc, cfg, gh, src = _setup(tmp_path, nodes=[_node(7)], edges=[])
    monkeypatch.setattr(dag_insert.dag_core, "edges_from", lambda *a: (_ for _ in ()).throw(KeyError("x")))
    assert dag_insert.insert(str(sdlc), cfg, 50, body="Blocked by #7") == "error"
    assert capsys.readouterr().err.count("\n") == 1


# ----------------------------------------------------------------- 6. mode off

def test_mode_off_touches_no_map_file_loads_no_dag_insert_and_files_as_before(tmp_path, monkeypatch):
    """control: remove the mode gate in `GitHubSource` -> dag_insert gets loaded, red; remove it in
    `insert` -> a file is created, red."""
    sdlc, cfg, gh, src = _setup(tmp_path, issues={7: "goal 7", 9: "g"}, mode="off")
    loaded = []
    real = sources._load
    monkeypatch.setattr(sources, "_load", lambda name: (loaded.append(name), real(name))[1])
    n = src.create_dependency("t", "body\n\n**Blocked by:** #9\n", None)
    src.append_to_body("7", "**Blocked by:** #100")
    assert n == "100" and "dag_insert" not in loaded
    assert sorted(p.name for p in (sdlc / "state").iterdir()) == []
    assert gh.issues[7]["body"].endswith("**Blocked by:** #100\n")


def test_mode_off_library_calls_do_nothing(tmp_path):
    sdlc = tmp_path / ".sdlc"
    cfg = _cfg("off")
    assert dag_insert.insert(str(sdlc), cfg, 5, body="Blocked by #4") == "off"
    assert dag_insert.closing_members(str(sdlc), cfg, 9, 7, reader=lambda n: 1 / 0) == []
    assert dag_insert.record_unchecked(str(sdlc), 9, 7, cfg) is False
    assert not sdlc.exists()


# ----------------------------------------------------------------- 7. busy lock and no map

def test_a_busy_lock_and_a_missing_map_skip_quietly(tmp_path, capsys):
    sdlc, cfg, gh, src = _setup(tmp_path)
    assert dag_insert.insert(str(sdlc), cfg, 5) == "nomap"
    assert not (sdlc / "state" / "dag.json").exists()
    _store(sdlc, [_node(7)], [])
    with dag_store.refresh_lock(str(sdlc), cfg) as held:
        assert held in ("held", "open")
    before = (sdlc / "state" / "dag.json").read_bytes()
    import fcntl
    fd = open(sdlc / "state" / dag_store.LOCK, "a+")
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert dag_insert.insert(str(sdlc), cfg, 5) == "busy"
    finally:
        fd.close()
    assert (sdlc / "state" / "dag.json").read_bytes() == before
    assert capsys.readouterr().err == ""


def test_insert_refreshes_an_existing_node_and_does_not_duplicate_edges(tmp_path):
    sdlc, cfg, gh, src = _setup(tmp_path, nodes=[_node(7), _node(8, "closed")], edges=[_edge(7, 8)])
    assert dag_insert.insert(str(sdlc), cfg, 8, body="**Blocked by:** #7") == "inserted"
    m = _read_map(sdlc)
    assert [n["state"] for n in m["nodes"] if n["number"] == 8] == ["open"]
    assert m["edges"] == [_edge(7, 8)]
    assert dag_insert.insert(str(sdlc), cfg, 9, blockers=[8], blocked=[20]) == "inserted"
    assert {(8, 9), (9, 20)} <= _edges(sdlc)


def test_a_cycle_refusal_surfaces_as_a_hand_off_warning_and_the_goal_is_not_marked_blocked(tmp_path):
    """`handoff.create_tracked_issue` turns the exception into a warning; the blocker marker never lands."""
    nodes = [_node(7), _node(9)]
    # the new issue (100) will be created by this call; make 7 already wait on it through an existing edge
    # so that writing `7 Blocked by 100` closes 7 -> ... : model it as 100 already waiting on 7.
    sdlc, cfg, gh, src = _setup(tmp_path, issues={7: "goal 7"}, nodes=nodes, edges=[])
    orig = src.create_dependency

    def filing(title, body, assignee, **kw):
        n = orig(title, body + "\n\n**Blocked by:** #7\n", assignee, **kw)        # 100 waits on 7
        return n
    src.create_dependency = filing
    rep = handoff.create_tracked_issue(str(sdlc), cfg, "7", "engine", "why", same_area=True,
                                       immediately_actionable=True, blocks_goal=True, source=src, dedup=False)
    assert any("dependency cycle" in w for w in rep["warnings"])
    assert gh.writes_to(7) == [] and "Blocked by" not in gh.issues[7]["body"]
