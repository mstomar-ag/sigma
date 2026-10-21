"""#1924: `dag.py html`, one offline HTML page per chain from the stored dependency map (`dag_html.py`).

Every guard below was run red once against its own deliberate break; the `control:` line in a test's docstring
names the break. Controls: the closure replaced by the weakly connected component; the barycentre sweeps off; the
`http` scrub off; the member cap lifted; the layer rule (a blocker in a column left of what waits on it) dropped;
the edge style taken from nothing; the mode-off early return removed; the ignored-output guard removed.
No test reaches the network; one runs the CLI with a `gh` on PATH that records any call it gets."""
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
S = ROOT / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


dh = _mod("dag_html")
ds = _mod("dag_store")
dag = _mod("dag")
dc = _mod("dag_chain")

NOW = 1_800_000_000.0


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _node(number, life="goal", state="open", kind="issue", prio=None, unit=None):
    n = {"number": number, "kind": kind, "state": state, "life": life, "prio": prio, "in_corpus": True}
    if unit:
        n["unit"] = unit
    return n


def _store(tmp_path, nodes, edges, mode=None, sources=None, **header):
    sdlc = tmp_path / ".sdlc"
    (sdlc / "state").mkdir(parents=True, exist_ok=True)
    h = {"repo": "owner/repo", "built_at": _iso(NOW - 60), "refreshed_at": _iso(NOW - 60),
         "last_ok_at": _iso(NOW - 60), "last_error": None, "partial": False}
    h.update(header)
    sources = sources or {}
    ok, why = ds.save(sdlc, {"header": h, "nodes": nodes,
                             "edges": [{"blocker": a, "blocked": b, "source": sources.get((a, b), "body")}
                                       for a, b in edges]})
    assert ok, why
    if mode:
        (sdlc / "config.json").write_text(json.dumps({"discovery": {"dag": {"mode": mode}}}))
    return sdlc


# Two end goals (10, 20) sharing the diamond's root 9, glued into ONE weak component; a third, apart (30).
# 10 <- 7 <- 8, 9;  8 <- 5;  9 <- 5, 6;  5 <- 4 (closed);  10 <- 11 (open pull request).  20 <- 9, 21.  30 <- 31.
NODES = [
    _node(4, "none", state="closed"), _node(5, "parked", prio="P1"), _node(6, "needs-label"),
    _node(7, prio="P2"), _node(8, prio="P2"), _node(9, prio="P1"), _node(10, prio="P1"),
    _node(11, "in-progress", kind="pr", prio="P3"),
    _node(20, prio="P2"), _node(21), _node(30), _node(31, "in-progress"),
]
EDGES = [(4, 5), (5, 8), (5, 9), (6, 9), (8, 7), (9, 7), (7, 10), (11, 10), (9, 20), (21, 20), (31, 30)]
CLOSURE_10 = {5, 6, 7, 8, 9, 10}
CLOSURE_20 = {5, 6, 9, 20, 21}


@pytest.fixture
def sdlc(tmp_path):
    return _store(tmp_path, NODES, EDGES)


def _run(sdlc, *args, out=None):
    out = out or sdlc / "state" / "dag-html"
    lines, code = dh.run(str(sdlc), out_dir=str(out), now=NOW, **dict(zip(("goal", "all_"), args)))
    return lines, code, out


def _data(page):
    return json.loads(re.search(r'<script type="application/json" id="data">(.*?)</script>', page, re.S).group(1))


def _model(sdlc, goal, titles=None):
    m, _md, config, ok, why = dh._open(str(sdlc), NOW)
    return dh.build(m, config, goal, titles, NOW, (ok, why))


def test_a_page_per_end_goal_and_an_index(sdlc):
    lines, code, out = _run(sdlc, None, True)
    assert code == 0, lines
    assert sorted(p.name for p in out.iterdir()) == ["chain-10.html", "chain-20.html", "chain-30.html", "index.html"]
    index = (out / "index.html").read_text()
    for g in (10, 20, 30):
        assert 'href="chain-%d.html"' % g in index
    assert "<b>3</b> chains" in index


def test_a_chain_is_the_plan_closure_never_the_component(sdlc):
    """control: build the chain from the weakly connected component (dag_core.components) instead of the closure:
    the page of #10 then carries #20, #21 and goes red."""
    got = {n for n, d in _model(sdlc, 10)["nodes"].items() if d["m"] not in ("closed", "outside")}
    assert got == CLOSURE_10
    got20 = {n for n, d in _model(sdlc, 20)["nodes"].items() if d["m"] not in ("closed", "outside")}
    assert got20 == CLOSURE_20
    page = (_run(sdlc, 10, False)[2] / "chain-10.html").read_text()
    assert {int(k) for k in _data(page)["nodes"]} == CLOSURE_10 | {4, 11}
    assert 'data-n="21"' not in page and 'data-n="30"' not in page


def test_members_equal_the_dag_chain_query(sdlc):
    for goal in (10, 20, 30):
        want = {m["number"] for m in dc.chain(sdlc, goal, include_resolved=True, now=NOW)["members"]}
        got = {int(k) for k in _data(dh.render_page(_model(sdlc, goal)))["nodes"]}
        assert got == want, goal


def test_goal_in_the_middle_of_a_larger_chain_is_its_own_page(sdlc):
    got = {n for n, d in _model(sdlc, 9)["nodes"].items() if d["m"] not in ("closed", "outside")}
    assert got == {5, 6, 9}


def test_states_by_marker(sdlc):
    nodes = _model(sdlc, 10)["nodes"]
    assert nodes[5]["m"] == "needs decision" and nodes[6]["m"] == "needs decision"
    assert nodes[8]["m"] == "after" and nodes[8]["after"] == [5]
    assert nodes[4]["m"] == "closed" and nodes[11]["m"] == "outside"
    other = _model(sdlc, 30)["nodes"]
    assert other[31]["m"] == "someone else's"
    ready = _model(sdlc, 20)["nodes"][21]
    assert ready["m"] == "ready now"


def test_every_marker_has_a_colour_class_and_a_legend_entry():
    chain_forecast = dh.chain_forecast
    assert set(chain_forecast.MARKERS) | {"closed", "outside", "cycle"} | set(dh.PLAN_WORDS) == set(dh.CLASS)
    assert {k for k, _v in dh.LEGEND} == set(dh.CLASS)


def test_header_facts_and_the_forecast_line(sdlc):
    page = dh.render_page(_model(sdlc, 10), "owner/repo")
    assert "<b>6</b> open members" in page and "depth <b>3</b>" in page and "<b>1</b> closed prerequisites" in page
    assert "can finish 0 of 6 before it stops on decision" in page
    assert "Held by: #5 needs decision (parked)" in page
    assert "the assignee filter is not applied" in page


def test_no_http_in_any_page_even_with_hostile_titles(sdlc, tmp_path):
    """control: skip the scrub in `clean_title`: a title with a URL or the letters `http` reaches the page."""
    titles = tmp_path / "t.json"
    titles.write_text(json.dumps({"10": "see https://example.test/x and http-ish", "5": "</script><b>x</b>",
                                  "7": "HTTP://UPPER.test", "8": "ftp://files.test/a"}))
    lines, code = dh.run(str(sdlc), all_=True, out_dir=str(tmp_path / "o"), titles_path=str(titles), now=NOW)
    assert code == 0, lines
    for p in (tmp_path / "o").iterdir():
        text = p.read_text()
        assert "http" not in text.lower(), p.name
        assert "://" not in text, p.name
        assert "<b>x</b>" not in text            # escaped, never markup


def test_script_in_a_title_cannot_close_the_data_block(sdlc, tmp_path):
    titles = tmp_path / "t.json"
    titles.write_text(json.dumps({"5": "</script><script>alert(1)</script>"}))
    page = dh.render_page(_model(sdlc, 10, dh.read_titles(str(titles))))
    assert page.count("</script>") == 2 and page.count("<script") == 2
    assert _data(page)["nodes"]["5"]["t"].startswith("</script>")      # JSON-decoded intact, still inert in the page


def test_no_body_text_is_read_or_written(sdlc, tmp_path):
    titles = tmp_path / "t.json"
    titles.write_text(json.dumps([{"number": 5, "title": "short", "body": "SECRET BODY TEXT", "labels": ["x"]}]))
    page = dh.render_page(_model(sdlc, 10, dh.read_titles(str(titles))))
    assert "SECRET" not in page and "short" in page


def test_titles_file_is_read_with_a_bound(monkeypatch):
    """control: `fh.read()` with no size: the recorded request is no longer MAX + 1."""
    asked = []

    class Fake:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            asked.append(n)
            return b"{}"
    monkeypatch.setattr(dh, "open", lambda *a, **k: Fake(), raising=False)
    assert dh.read_titles("x") == {}
    assert asked == [dh.TITLES_MAX_BYTES + 1]


def test_titles_file_is_bounded_and_checked(tmp_path):
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (dh.TITLES_MAX_BYTES + 1))
    with pytest.raises(dh.HtmlError) as e:
        dh.read_titles(str(big))
    assert e.value.code == 2 and "over" in str(e.value)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(dh.HtmlError):
        dh.read_titles(str(bad))
    scalar = tmp_path / "s.json"
    scalar.write_text("3")
    with pytest.raises(dh.HtmlError):
        dh.read_titles(str(scalar))
    ok = tmp_path / "ok.json"
    ok.write_text(json.dumps({"#7": "seven", "x": "ignored", "8": ""}))
    assert dh.read_titles(str(ok)) == {7: "seven"}
    long_t = dh.clean_title("w" * 500)
    assert len(long_t) == dh.TITLE_CHARS and long_t.endswith("…")


def _chain_of(n, width=1):
    """A synthetic chain of n open goals: n-1 prerequisites in `width`-wide layers, the last waits on all of them."""
    nodes = [_node(i) for i in range(1, n + 1)]
    edges = []
    for i in range(1, n):
        edges.append((i, n) if i % width == 0 or i > n - width else (i, i + 1))
    return nodes, edges


def test_cap_is_stated_and_exceeded_says_so(tmp_path):
    """control: set MEMBER_CAP huge: the 81-member chain then carries no notice and goes red."""
    assert dh.MEMBER_CAP == 80                                      # the stated cap; a change is a decision
    nodes, edges = _chain_of(80)
    at = _store(tmp_path / "a", nodes, edges)
    page = dh.render_page(_model(at, 80))
    assert "over the page cap" not in page and _model(at, 80)["over_cap"] is False
    nodes, edges = _chain_of(81)
    over = _store(tmp_path / "b", nodes, edges)
    model = _model(over, 81)
    page = dh.render_page(model)
    assert model["over_cap"] and "over the page cap of 80" in page
    assert len(_data(page)["nodes"]) == 81                          # written in full, not truncated
    lines, code = dh.run(str(over), goal=81, out_dir=str(tmp_path / "o"), now=NOW)
    assert code == 0 and any("over the page cap" in ln for ln in lines)


def test_size_and_time_are_bounded(tmp_path):
    nodes, edges = _chain_of(400, width=4)
    big = _store(tmp_path, nodes, edges)
    t0 = time.time()
    page = dh.render_page(_model(big, 400))
    took = time.time() - t0
    assert len(page.encode()) < 1_500_000, len(page)
    assert took < 30, took
    small = _store(tmp_path / "s", *_chain_of(80, width=4))
    assert len(dh.render_page(_model(small, 80)).encode()) < 250_000


def test_layout_puts_blockers_left_of_what_waits(sdlc):
    """control: give every node column 0 (drop the depth): the assertion on columns goes red."""
    page = dh.render_page(_model(sdlc, 10))
    x = {int(m.group(1)): float(m.group(2)) for m in re.finditer(r'data-n="(\d+)"[^>]*>(?:<title>[^<]*</title>)?'
                                                                 r'<rect x="([-\d.]+)"', page)}
    for a, b in ((5, 8), (5, 9), (6, 9), (8, 7), (9, 7), (7, 10)):
        assert x[a] < x[b], (a, b)
    assert x[11] < x[10]                                    # the outside pull request is a blocker too
    assert x[4] < min(x[n] for n in CLOSURE_10)           # closed prerequisites sit in the leftmost column


def test_closed_prerequisites_stay_out_of_the_layered_graph(sdlc, monkeypatch):
    """control: lay the closed nodes out with the open ones (`shown = dict(nodes)`): the layout then receives them
    (and slots for their long edges), which the recorded `cols` shows."""
    seen = {}
    real = dh.layout
    monkeypatch.setattr(dh, "layout", lambda cols, edges, rank, sweeps=None: seen.update(cols=cols, edges=edges)
                        or real(cols, edges, rank, sweeps))
    dh.render_page(_model(sdlc, 20))
    assert 4 not in seen["cols"] and not any(4 in e for e in seen["edges"])
    monkeypatch.setattr(dh, "layout", real)
    page = dh.render_page(_model(sdlc, 20))
    width = int(re.search(r'<svg id="g" viewBox="0 0 (\d+) ', page).group(1))
    cols = _model(sdlc, 20)["depth"] + 1
    assert width == 2 * dh.MARGIN + cols * dh.NODE_W + (cols - 1) * dh.COL_GAP
    closed = re.search(r'<g class="n c-closed cl" data-n="4"[^>]*>.*?<rect x="([-\d.]+)"', page, re.S)
    assert float(closed.group(1)) == -(dh.NODE_W + dh.COL_GAP)
    assert 'class="e explicit ce" data-a="4" data-b="5"' in page      # its edge is hidden until it is shown


def test_barycentre_reduces_crossings():
    """control: sweeps=0 keeps the starting order: the strict inequality goes red. A ladder wired so that the
    numeric order of the second column is the reverse of its blockers' order."""
    cols = {1: 0, 2: 0, 3: 0, 4: 0, 11: 1, 12: 1, 13: 1, 14: 1}
    edges = [(1, 14), (2, 13), (3, 12), (4, 11)]
    plain = dh.layout(cols, edges, {}, sweeps=0)
    swept = dh.layout(cols, edges, {})
    assert plain["crossings"] == 6
    assert swept["crossings"] == 0


def test_crossing_counter_ignores_shared_ends():
    layers = [[1, 2], [3, 4]]
    assert dh.crossings(layers, [(1, 3), (2, 4)]) == 0
    assert dh.crossings(layers, [(1, 4), (2, 3)]) == 1
    assert dh.crossings(layers, [(1, 3), (1, 4)]) == 0


def test_long_edges_run_through_a_slot_in_every_column_between():
    cols = {1: 0, 2: 1, 3: 2, 4: 3}
    lay = dh.layout(cols, [(1, 2), (2, 3), (3, 4), (1, 4)], {})
    assert lay["paths"][(1, 4)] == [1, ("d", 1, 4, 1), ("d", 1, 4, 2), 4]
    assert lay["direct"] == []


def test_layout_is_deterministic(sdlc):
    a = dh.render_page(_model(sdlc, 10), "r")
    b = dh.render_page(_model(sdlc, 10), "r")
    assert a == b


def test_edge_style_by_source(tmp_path):
    """control: class every edge `explicit`: the ledger edge goes red."""
    sdlc = _store(tmp_path, [_node(1), _node(2), _node(3)], [(1, 3), (2, 3)], sources={(2, 3): "comment"})
    model = _model(sdlc, 3)
    assert {(a, b): t for a, b, t in model["edges"]} == {(1, 3): "explicit", (2, 3): "ledger"}
    page = dh.render_page(model)
    assert re.search(r'class="e explicit" data-a="1" data-b="3"', page)
    assert re.search(r'class="e ledger" data-a="2" data-b="3"', page)
    model["edges"][0] = (1, 3, "unconfirmed")
    assert 'class="e unconfirmed" data-a="1"' in dh.render_page(model)
    assert "stroke-dasharray:6 4" in page and "stroke-dasharray:2 4" in page


def test_page_is_self_contained_and_themed(sdlc):
    page = dh.render_page(_model(sdlc, 10))
    for banned in ("<link", "src=", "@import", "url(http", "fetch(", "XMLHttpRequest", "import("):
        assert banned not in page, banned
    assert "prefers-color-scheme:dark" in page and "data-theme=dark" in page and "--ready-f" in page
    assert "viewBox" in page and 'id="zfit"' in page and 'id="zin"' in page
    assert page.count("<svg") == 1 and page.startswith("<!doctype html>")


def test_panel_data_names_waits_and_frees(sdlc):
    d = _data(dh.render_page(_model(sdlc, 10)))
    n9 = d["nodes"]["9"]
    assert n9["waits"] == [5, 6] and n9["frees"] == [7]          # #20 is another chain: not a "frees" of this page
    assert d["nodes"]["10"]["waits"] == [7, 11] and d["nodes"]["10"]["why"] == "waits on #7"
    assert d["goal"] == 10


def test_cycle_is_drawn_and_said(tmp_path):
    sdlc = _store(tmp_path, [_node(1), _node(2), _node(3)], [(1, 2), (2, 1), (2, 3)])
    model = _model(sdlc, 3)
    page = dh.render_page(model)
    assert model["cycles"] and "Dependency cycle" in page and "c-cycle" in page


def test_stale_map_is_drawn_with_the_reason(tmp_path):
    sdlc = _store(tmp_path, NODES, EDGES, built_at=_iso(NOW - 10 * 86400), last_ok_at=_iso(NOW - 10 * 86400))
    lines, code = dh.run(str(sdlc), goal=10, out_dir=str(tmp_path / "o"), now=NOW)
    assert code == 0, lines
    assert "The map is not fresh (stale)" in (tmp_path / "o" / "chain-10.html").read_text()


def test_refusals_write_nothing(tmp_path, sdlc):
    out = tmp_path / "o"
    assert dh.run(str(sdlc), goal=999, out_dir=str(out), now=NOW)[1] == 2
    assert dh.run(str(sdlc), goal=4, out_dir=str(out), now=NOW)[1] == 2             # closed
    assert dh.run(str(sdlc), goal=11, out_dir=str(out), now=NOW)[1] == 2            # a pull request
    assert dh.run(str(sdlc), out_dir=str(out), now=NOW)[1] == 2                     # neither --goal nor --all
    assert dh.run(str(sdlc), goal=10, all_=True, out_dir=str(out), now=NOW)[1] == 2
    assert not out.exists()
    empty = tmp_path / "e" / ".sdlc"
    assert dh.run(str(empty), goal=10, out_dir=str(out), now=NOW)[1] == 3
    assert not out.exists()


def test_mode_off_touches_nothing(tmp_path):
    """control: delete the off check in `_open`: a page is written in mode off and the test goes red."""
    sdlc = _store(tmp_path, NODES, EDGES, mode="off")
    lines, code = dh.run(str(sdlc), all_=True, now=NOW)
    assert (lines, code) == (["dag: off"], 4)
    assert not (sdlc / "state" / "dag-html").exists()


def test_default_output_is_under_state(sdlc):
    lines, code = dh.run(str(sdlc), goal=10, now=NOW)
    assert code == 0 and (sdlc / "state" / "dag-html" / "chain-10.html").is_file()


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def test_output_inside_a_checkout_must_be_ignored(tmp_path):
    """control: make `check_out` return at once: the tracked-directory output is then written and the test is red."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / ".gitignore").write_text(".sdlc/state/\n")
    sdlc = _store(repo, NODES, EDGES)
    assert dh.run(str(sdlc), goal=10, now=NOW)[1] == 0                      # the default is ignored
    lines, code = dh.run(str(sdlc), goal=10, out_dir=str(repo / "docs" / "maps"), now=NOW)
    assert code == 2 and "not ignored" in lines[0]
    assert not (repo / "docs" / "maps").exists()
    assert dh.run(str(sdlc), goal=10, out_dir=str(tmp_path / "elsewhere"), now=NOW)[1] == 0


def test_cli_documented_gesture_makes_no_github_call(sdlc, tmp_path):
    """The documented gesture, run as a subprocess with a `gh` first on PATH that records any call."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    mark = tmp_path / "gh-called"
    (bindir / "gh").write_text("#!/bin/sh\necho called >> %s\nexit 1\n" % mark)
    (bindir / "gh").chmod(0o755)
    env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ["PATH"], GH_TOKEN="invalid")
    r = subprocess.run([sys.executable, str(S / "dag.py"), "html", "--all", "--sdlc", str(sdlc)], env=env,
                       capture_output=True, text=True, cwd=str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert (sdlc / "state" / "dag-html" / "index.html").is_file()
    assert not mark.exists()
    r = subprocess.run([sys.executable, str(S / "dag.py"), "html", "--sdlc", str(sdlc)], env=env,
                       capture_output=True, text=True, cwd=str(tmp_path))
    assert r.returncode == 2 and "--goal" in r.stderr           # one of --goal / --all is required


def test_index_lists_members_depth_holders_and_decisions(sdlc):
    out = _run(sdlc, None, True)[2]
    index = (out / "index.html").read_text()
    row = re.search(r'<tr[^>]*><td><a href="chain-10.html">#10</a>.*?</tr>', index, re.S).group(0)
    assert ">6<" in row and ">3<" in row and "#5 needs decision" in row and "0 of 6" in row
    assert index.index("chain-10.html") < index.index("chain-20.html") < index.index("chain-30.html")


def test_end_goals_match_dag_chains(sdlc):
    m = ds.load(str(sdlc))[0]
    listed = sorted({n for c in dc.chains(sdlc, now=NOW) for n in c["ends"]})
    assert dh.end_goals(m) == listed == [10, 20, 30]


def test_the_plan_still_reads_the_same_closure(sdlc):
    """`chain.map_closure` is shared with the plan's map half: the plan's rows are the page's members."""
    chain = dh.chain
    m = ds.load(str(sdlc))[0]
    out, _g, _esrc, _body = chain.map_closure(m, 10, 100, True, NOW)
    assert {r["number"] for r in out["members"]} == CLOSURE_10 and out["done"] == [4]
    assert [e["ref"] for e in out["externals"]] == ["#11"]
    with pytest.raises(chain.ChainRefused):
        chain.map_closure(m, 10, 3, True, NOW)                  # strict: over the bound is refused, not truncated
