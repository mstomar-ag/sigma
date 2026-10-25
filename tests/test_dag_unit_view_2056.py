"""#2056: the unit view, `dag.py report --feature <unit>` and `dag.py html --feature <unit>`.

Members are the stored open issues carrying the unit; every OPEN blocker outside it is shown as labelled outside
context, and each member lists its open blockers so a blocked member never reads free. The controls, each run red
once on its own and restored: outside blockers dropped, the open-only filter, the unit filter, the unknown-unit
refusal and the empty-unit message. Fixture issue numbers are generic; nothing here reaches the network."""
import importlib.util
import json
import pathlib
import re
import subprocess
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
S = ROOT / "skills" / "sigma-loop" / "scripts"
DAG = S / "dag.py"
NOW = 1_800_000_000.0


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


rep = _mod("dag_report")
dh = _mod("dag_html")
ds = rep.dag_store


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _node(number, life="goal", state="open", unit=None, kind="issue"):
    n = {"number": number, "kind": kind, "state": state, "life": life, "prio": None, "in_corpus": True}
    if unit:
        n["unit"] = unit
    return n


# unit alpha: fixture issues 1, 2, 8 open, 3 closed. 1 waits on 5 (no unit, open), 7 (no unit, closed) and 2;
# 2 waits on 4 (unit beta, open). 8 waits on nothing. unit gamma has only fixture issue 9, closed.
NODES = [_node(1, unit="alpha"), _node(2, unit="alpha"), _node(3, state="closed", unit="alpha"),
         _node(8, unit="alpha"), _node(4, "parked", unit="beta"), _node(5, "needs-label"),
         _node(7, "none", state="closed"), _node(9, state="closed", unit="gamma")]
EDGES = [(5, 1), (7, 1), (2, 1), (4, 2), (3, 8)]


def _store(tmp_path, nodes=NODES, edges=EDGES, mode="shadow", at=NOW):
    sdlc = tmp_path / ".sdlc"
    (sdlc / "state").mkdir(parents=True)
    h = {"repo": "owner/repo", "built_at": _iso(at - 60), "refreshed_at": _iso(at - 60),
         "last_ok_at": _iso(at - 60), "last_error": None, "partial": False}
    ok, why = ds.save(sdlc, {"header": h, "nodes": nodes,
                             "edges": [{"blocker": a, "blocked": b, "source": "body"} for a, b in edges]})
    assert ok, why
    (sdlc / "config.json").write_text(json.dumps({"discovery": {"dag": {"mode": mode}}}))
    return sdlc


@pytest.fixture
def sdlc(tmp_path):
    return _store(tmp_path)


@pytest.fixture
def live(tmp_path):
    return _store(tmp_path, at=time.time())         # the command line reads the real clock


def _json(sdlc, unit="alpha"):
    text, code = rep.report(sdlc, feature=unit, as_json=True, now=NOW)
    assert code == 0, text
    return json.loads(text)


def _members(doc):
    return {m["number"]: m for m in doc["members"]}


def test_json_members_are_the_open_issues_of_the_unit(sdlc):
    doc = _json(sdlc)
    assert doc["schema"] == "dag-report/1" and doc["feature"] == "alpha"
    assert sorted(_members(doc)) == [1, 2, 8]


def test_outside_open_blockers_are_listed_and_labelled(sdlc):
    doc = _json(sdlc)
    assert sorted(o["number"] for o in doc["outside"]) == [4, 5]
    by = {o["number"]: o for o in doc["outside"]}
    assert by[4]["unit"] == "beta" and by[5]["unit"] is None
    blockers = {b["number"]: b for b in _members(doc)[1]["blockers"]}
    assert blockers[5]["outside"] is True and blockers[2]["outside"] is False


def test_a_blocked_member_never_reads_free(sdlc):
    m = _members(_json(sdlc))
    assert [b["number"] for b in m[2]["blockers"]] == [4]       # blocked only from outside the unit
    assert m[8]["blockers"] == []                               # genuinely free (its blocker is closed)
    md, code = rep.report(sdlc, feature="alpha", now=NOW)
    assert code == 0 and "#4 (outside unit)" in md and "#5 (outside unit)" in md


def test_only_open_blockers_are_shown(sdlc):
    doc = _json(sdlc)
    assert 7 not in {o["number"] for o in doc["outside"]}
    assert 7 not in {b["number"] for b in _members(doc)[1]["blockers"]}
    assert 3 not in {b["number"] for m in doc["members"] for b in m["blockers"]}


def test_markdown_names_the_unit_members_and_outside_section(sdlc):
    md, code = rep.report(sdlc, feature="alpha", now=NOW)
    assert code == 0
    assert md.startswith("# Unit alpha")
    assert "## Outside blockers" in md and "#8" in md
    assert "#9" not in md and "# Dependency map report" not in md


def test_unknown_unit_refuses_in_one_line_naming_known_units(sdlc):
    text, code = rep.report(sdlc, feature="nope", now=NOW)
    assert code == 2 and "\n" not in text.strip()
    assert "nope" in text and "alpha" in text and "beta" in text and "gamma" in text


def test_known_unit_without_open_members_says_so(sdlc):
    text, code = rep.report(sdlc, feature="gamma", now=NOW)
    assert code == 0 and "\n" not in text.strip() and "no open members" in text
    doc, code = rep.report(sdlc, feature="gamma", as_json=True, now=NOW)
    assert code == 0 and json.loads(doc)["members"] == []


def test_unit_with_only_outside_blocker_unit_is_not_a_member_unit(sdlc):
    doc = _json(sdlc, "beta")
    assert sorted(_members(doc)) == [4] and doc["outside"] == []


def test_report_reads_the_stored_map_and_writes_nothing(sdlc, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the unit report must not run a process")
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    before = {p: p.read_bytes() for p in sdlc.rglob("*") if p.is_file()}
    _json(sdlc)
    rep.report(sdlc, feature="alpha", now=NOW)
    assert {p: p.read_bytes() for p in sdlc.rglob("*") if p.is_file()} == before


def test_nothing_to_read_still_gives_the_fix_line(tmp_path):
    sdlc = _store(tmp_path, mode="off")
    text, code = rep.report(sdlc, feature="alpha", now=NOW)
    assert code == 0 and text.startswith("dag: mode is off")


# ---------------------------------------------------------------- the command line

def _cli(sdlc, *args):
    return subprocess.run([sys.executable, str(DAG), *args, "--sdlc", str(sdlc)], capture_output=True, text=True)


def test_cli_feature_conflicts_with_goal_on_report(live):
    r = _cli(live, "report", "--feature", "alpha", "--goal", "1")
    assert r.returncode == 2 and "not allowed" in r.stderr


def test_cli_report_feature_and_unknown_exit_codes(live):
    assert _cli(live, "report", "--feature", "alpha").returncode == 0
    r = _cli(live, "report", "--feature", "nope")
    assert r.returncode == 2 and r.stdout.count("\n") == 1


def test_cli_html_needs_one_of_goal_all_feature(live):
    r = _cli(live, "html")
    assert r.returncode == 2 and "--feature" in r.stderr
    r = _cli(live, "html", "--feature", "alpha", "--goal", "1")
    assert r.returncode == 2 and "not allowed" in r.stderr


# ---------------------------------------------------------------- the page

def _html(sdlc, unit="alpha", out=None):
    out = out or sdlc / "state" / "dag-html"
    lines, code = dh.run(str(sdlc), feature=unit, out_dir=str(out), now=NOW)
    return lines, code, out


def _data(page):
    return json.loads(re.search(r'<script type="application/json" id="data">(.*?)</script>', page, re.S).group(1))


def test_html_writes_one_unit_page_and_prints_its_path(sdlc):
    lines, code, out = _html(sdlc)
    assert code == 0 and (out / "unit-alpha.html").is_file()
    assert str(out / "unit-alpha.html") in lines[0]
    data = _data((out / "unit-alpha.html").read_text())
    assert {"1", "2", "8", "4", "5"} <= set(data["nodes"])
    assert "9" not in data["nodes"]


def test_html_tags_outside_context_and_legends_it(sdlc):
    _l, _c, out = _html(sdlc)
    page = (out / "unit-alpha.html").read_text()
    for n in (4, 5):
        assert re.search(r'class="n c-ctx[^"]*" data-n="%d"' % n, page)
    for n in (1, 2, 8):
        assert not re.search(r'class="n c-ctx[^"]*" data-n="%d"' % n, page)
    assert "outside the unit" in page
    assert "Unit alpha" in page


def test_html_unit_refusals(sdlc):
    lines, code, _o = _html(sdlc, "nope")
    assert code == 2 and len(lines) == 1 and "alpha" in lines[0]
    lines, code, out = _html(sdlc, "gamma")
    assert code == 0 and "no open members" in lines[0] and not (out / "unit-gamma.html").exists()


def test_html_unit_name_is_sanitised_in_the_filename(tmp_path):
    nodes = [_node(1, unit="v1.0..x"), _node(2)]            # the store admits dots, so ".." can reach a file name
    sdlc = _store(tmp_path, nodes, [(2, 1)])
    _l, code, out = _html(sdlc, "v1.0..x")
    assert code == 0
    names = [p.name for p in out.iterdir()]
    assert len(names) == 1 and names[0].startswith("unit-") and ".." not in names[0]
    for hostile in ("a/b ../c", "..", "", "x\\y\x00"):
        safe = dh.unit_name(hostile)
        assert re.fullmatch(r"[A-Za-z0-9_-]+", safe), safe
    assert dh.unit_name("a/b") != dh.unit_name("a b")


def test_chain_pages_are_unchanged_by_the_unit_support(sdlc):
    lines, code = dh.run(str(sdlc), goal=1, out_dir=str(sdlc / "state" / "dag-html"), now=NOW)
    assert code == 0
    page = (sdlc / "state" / "dag-html" / "chain-1.html").read_text()
    assert "c-ctx" not in page.split("</style>")[1] and "outside the unit" not in page.split("</style>")[1]


@pytest.mark.parametrize("state,kind", [("unknown", "issue"), ("closed-unmerged", "pr"), ("open", "pr")])
def test_an_unresolved_blocker_that_is_not_an_open_issue_still_blocks(state, kind):
    """The core counts only a closed issue or a merged pull request as resolved; the unit view must too, or a
    blocked member reads free (the control: filter on state == "open" and this goes red for unknown/closed-unmerged)."""
    m = {"nodes": [_node(1, unit="alpha"), _node(2, state=state, kind=kind, unit="beta")],
         "edges": [{"blocker": 2, "blocked": 1, "source": "body"}]}
    facts, _ = rep.unit_facts(m, "alpha")
    assert [b["number"] for b in facts["members"][0]["blockers"]] == [2]
    assert [o["number"] for o in facts["outside"]] == [2]


def test_a_merged_pull_request_blocker_is_resolved():
    m = {"nodes": [_node(1, unit="alpha"), _node(2, state="merged", kind="pr", unit="beta")],
         "edges": [{"blocker": 2, "blocked": 1, "source": "body"}]}
    facts, _ = rep.unit_facts(m, "alpha")
    assert facts["members"][0]["blockers"] == [] and facts["outside"] == []
