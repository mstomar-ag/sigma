"""#1982: the stored map keeps each member's assignees; chain pages show them; the index sorts and filters.

Each guard was run red once against its own deliberate break (the `control:` line in its docstring names it).
The index's sort and filter is one JS function, `applyView`, run here through node over the page's own text
(skipped, with the reason, when node is absent). No test reaches the network."""
import importlib.util
import json
import pathlib
import re
import shutil
import subprocess
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
dc = _mod("dag_collect")
ds = dc.dag_store

NOW = 1_800_000_000.0
REPO = "owner/repo"
CONFIG = {"discovery": {"github": {"repo": REPO}, "dag": {"mode": "on"}}}


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


# ------------------------------------------------------------------ collect and store

def _issue(number, labels, body="", logins=None):
    it = {"number": number, "title": "fixture issue %d" % number, "body": body, "state": "open",
          "labels": [{"name": n} for n in labels], "updated_at": "2026-10-01T00:00:00Z"}
    if logins is not None:
        it["assignees"] = [{"login": x} for x in logins]
    return it


class Fake:
    def __init__(self, issues):
        self.issues = {i["number"]: i for i in issues}
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        fields = {}
        for i, a in enumerate(args):
            if a == "-f":
                k, _, v = args[i + 1].partition("=")
                fields[k] = v
        m = re.search(r"/issues/(\d+)$", args[1])
        if m:
            return json.dumps(self.issues[int(m.group(1))])
        want = [x for x in fields.get("labels", "").split(",") if x]
        rows = [i for i in self.issues.values() if all(w in [l["name"] for l in i["labels"]] for w in want)]
        rows.sort(key=lambda i: i["number"])
        per, page = int(fields["per_page"]), int(fields["page"])
        return json.dumps(rows[(page - 1) * per:page * per])


def _refreshed(tmp_path):
    run = Fake([_issue(1, ["sdlc:goal"], "Blocked by: #2\nBlocked by: #3\nBlocked by: #4", ["alice", "bob"]),
                _issue(2, ["sdlc:parked"], "", []),
                _issue(3, ["sdlc:blocking"], "", ["carol"]),
                _issue(4, ["sdlc:blocking"], "")])        # the listing carried no assignees field at all
    rep = dc.refresh(tmp_path, CONFIG, run, NOW)
    assert rep["status"] == "saved"
    return {n["number"]: n for n in ds.load(tmp_path)[0]["nodes"]}, run


def test_refresh_stores_assignees_and_keeps_empty_distinct_from_absent(tmp_path):
    """control: collect stores no assignees -> node 1 has no key; absent treated as empty -> node 4 gets [].
    """
    nodes, run = _refreshed(tmp_path)
    assert nodes[1]["assignees"] == ["alice", "bob"] and nodes[3]["assignees"] == ["carol"]
    assert nodes[2]["assignees"] == []
    assert "assignees" not in nodes[4]
    assert not any(str(a).startswith("assignee") for c in run.calls for a in c)    # still no assignee argument


def test_store_drops_bad_logins_and_caps_the_list():
    """control: the shape check removed -> the URL login survives; the cap removed -> 25 logins survive."""
    n = ds._clean_node({"number": 5, "assignees": ["alice", "see http://x.test/a", "a b", "", 7, "bot[bot]"]
                        + ["u%d" % i for i in range(30)]})
    assert n["assignees"][:2] == ["alice", "bot[bot]"] and len(n["assignees"]) == ds.MAX_ASSIGNEES
    assert all("/" not in a and " " not in a for a in n["assignees"])
    assert "assignees" not in ds._clean_node({"number": 6})
    assert "assignees" not in ds._clean_node({"number": 6, "assignees": "alice"})     # not a list: unknown
    assert ds._clean_node({"number": 6, "assignees": []})["assignees"] == []


def test_login_with_a_trailing_newline_is_dropped():
    """control: `fullmatch` back to `match` in the store -> `$` admits a trailing newline and this goes red."""
    assert ds._clean_node({"number": 1, "assignees": ["alice\n", "alice"]})["assignees"] == ["alice"]


def test_login_shape_does_not_admit_a_range_hole():
    """control: widen the class to admit `:` `<` `@` `^` `\\` -> each is kept as a login."""
    for bad in ("a:b", "a<b", "a@b", "a^b", "a\\b"):
        assert ds._clean_node({"number": 1, "assignees": [bad]})["assignees"] == [], bad


# ------------------------------------------------------------------ chain page

def _node(number, life="goal", prio=None, assignees=None, state="open"):
    n = {"number": number, "kind": "issue", "state": state, "life": life, "prio": prio, "in_corpus": True}
    if assignees is not None:
        n["assignees"] = assignees
    return n


def _store(tmp_path, nodes, edges):
    sdlc = tmp_path / ".sdlc"
    (sdlc / "state").mkdir(parents=True, exist_ok=True)
    h = {"repo": REPO, "built_at": _iso(NOW - 60), "refreshed_at": _iso(NOW - 60), "last_ok_at": _iso(NOW - 60),
         "last_error": None, "partial": False}
    ok, why = ds.save(sdlc, {"header": h, "nodes": nodes,
                             "edges": [{"blocker": a, "blocked": b, "source": "body"} for a, b in edges]})
    assert ok, why
    return sdlc


# Chain 10 <- 7 (alice, bob), 8 (empty), 9 (old map: field absent).  Chain 20 <- 21 (alice).  Chain 30 <- 31 (none).
NODES = [_node(7, prio="P2", assignees=["bob", "alice"]), _node(8, prio="P1", assignees=[]), _node(9),
         _node(10, prio="P3", assignees=["carol"]),
         _node(20, assignees=["alice"]), _node(21, prio="P2", assignees=["alice"]),
         _node(30, assignees=[]), _node(31, assignees=[])]
EDGES = [(7, 10), (8, 10), (9, 10), (21, 20), (31, 30)]


@pytest.fixture
def sdlc(tmp_path):
    return _store(tmp_path, NODES, EDGES)


def _page(sdlc, goal, tmp_path):
    lines, code = dh.run(str(sdlc), goal=goal, out_dir=str(tmp_path / "o"), now=NOW)
    assert code == 0, lines
    return (tmp_path / "o" / ("chain-%d.html" % goal)).read_text()


def _data(page):
    return json.loads(re.search(r'<script type="application/json" id="data">(.*?)</script>', page, re.S).group(1))


def test_chain_page_data_and_node_show_assignees(sdlc, tmp_path):
    """control: panel row unknown -> unassigned: member 9 would read unassigned; the node line removed."""
    page = _page(sdlc, 10, tmp_path)
    d = _data(page)["nodes"]
    assert d["7"]["who"] == ["alice", "bob"] and d["8"]["who"] == [] and d["9"]["who"] is None
    assert "Assignees" in page and "unassigned" in page
    assert "assignee unknown (map predates this field)" in page
    assert "alice, bob" in page                                # the node's own short line


def test_hostile_login_is_text_never_a_link(sdlc, tmp_path):
    """control: scrub removed in `_clean_login` -> the URL and `http` reach the page and its data."""
    m, _md, config, ok, why = dh._open(str(sdlc), NOW)
    for n in m["nodes"]:
        if n["number"] == 7:
            n["assignees"] = ["see https://example.test/x http-ish", "</script><b>x</b>"]
    model = dh.build(m, config, 10, None, NOW, (ok, why))
    page = dh.render_page(model, REPO)
    assert "http" not in page.lower() and "://" not in page
    assert "<b>x</b>" not in page
    assert "<a " not in page.split("<main", 1)[1]              # nothing in the chain page is an anchor


# ------------------------------------------------------------------ index

def _index(sdlc, tmp_path):
    lines, code = dh.run(str(sdlc), all_=True, out_dir=str(tmp_path / "o"), now=NOW)
    assert code == 0, lines
    return (tmp_path / "o" / "index.html").read_text()


def _rows(index):
    out = {}
    for m in re.finditer(r'<tr data-goal="(\d+)" data-open="(\d+)" data-top="([^"]*)" data-who="([^"]*)">', index):
        out[int(m.group(1))] = {"open": int(m.group(2)), "top": m.group(3), "who": m.group(4).split()}
    return out


def test_index_rows_carry_top_priority_and_the_assignee_set(sdlc, tmp_path):
    """control: top uses max over ranks -> chain 10 reads P3 instead of P1."""
    r = _rows(_index(sdlc, tmp_path))
    assert r[10]["top"] == "P1" and r[10]["open"] == 4
    assert r[10]["who"] == sorted(["alice", "bob", "carol", "(unassigned)", "(unknown)"])
    assert r[20]["top"] == "P2" and r[20]["who"] == ["alice"]
    assert r[30]["top"] == "" and r[30]["who"] == ["(unassigned)"]


def test_index_controls_and_no_external_reference(sdlc, tmp_path):
    index = _index(sdlc, tmp_path)
    for needle in ('id="sort"', 'id="who"', 'id="prio"', 'value="priority"', 'value="open"', 'value="goal"',
                   'value="(unassigned)"', 'value="alice"', 'value="P1"'):
        assert needle in index, needle
    assert "http" not in index.lower() and "://" not in index
    assert not re.search(r"<script[^>]*\bsrc=|<link\b|@import|\burl\(", index)
    assert "<a " not in re.sub(r'<td><a href="chain-\d+\.html">#\d+</a>', "", index.split("<tbody>", 1)[1])


def test_index_logins_are_escaped_in_data_and_options(sdlc, tmp_path):
    m, _md, config, ok, why = dh._open(str(sdlc), NOW)
    for n in m["nodes"]:
        if n["number"] == 21:
            n["assignees"] = ['"><img src=x onerror=1> http://u.test']
    models = [dh.build(m, config, g, None, NOW, (ok, why)) for g in (10, 20)]
    rows = [{"file": "c.html", "goal": x["goal"], "title": None, "open": x["open"], "depth": x["depth"],
             "stops": x["stops"], "decisions": x["decisions"], "finish": x["finish"], "over_cap": False,
             "top": x["top"], "who": x["who"]} for x in models]
    page = dh.render_index(rows, REPO, "now", "1m", (True, "ok"), False)
    assert "<img" not in page and "http" not in page.lower() and "://" not in page


# ------------------------------------------------------------------ the sort and filter function

node = shutil.which("node")


def _apply(index, rows, sort, who, prio):
    fn = re.search(r"function applyView[\s\S]*?\n}\n", index)
    assert fn, "applyView missing from the page"
    prog = fn.group(0) + "\nconsole.log(JSON.stringify(applyView(%s,%s,%s,%s).map(function(r){return r.goal;})));" % (
        json.dumps(rows), json.dumps(sort), json.dumps(who), json.dumps(prio))
    r = subprocess.run([node, "-e", prog], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


ROWS = [{"goal": 30, "open": 2, "top": "", "who": ["(unassigned)"]},
        {"goal": 10, "open": 4, "top": "P1", "who": ["alice", "(unknown)"]},
        {"goal": 20, "open": 4, "top": "P0", "who": ["alice"]},
        {"goal": 40, "open": 9, "top": "P1", "who": ["bob", "(unassigned)"]}]


@pytest.mark.skipif(node is None, reason="node is not installed: the page script cannot be executed here")
def test_apply_view_sorts_and_filters(sdlc, tmp_path):
    """control: comparator reversed -> priority order flips; filter ignores the unassigned marker -> 30 absent;
    each clause alone."""
    index = _index(sdlc, tmp_path)
    assert _apply(index, ROWS, "priority", "", "") == [20, 10, 40, 30]       # P0, then P1 by goal, none last
    assert _apply(index, ROWS, "open", "", "") == [40, 10, 20, 30]           # most open first, ties by goal
    assert _apply(index, ROWS, "goal", "", "") == [10, 20, 30, 40]
    assert _apply(index, ROWS, "goal", "alice", "") == [10, 20]
    assert _apply(index, ROWS, "goal", "(unassigned)", "") == [30, 40]
    assert _apply(index, ROWS, "goal", "(unknown)", "") == [10]
    assert _apply(index, ROWS, "goal", "", "P1") == [10, 40]
    assert _apply(index, ROWS, "goal", "alice", "P1") == [10]
    assert _apply(index, ROWS, "goal", "nobody", "") == []
    assert ROWS[0]["goal"] == 30                                             # the input is not reordered in place
