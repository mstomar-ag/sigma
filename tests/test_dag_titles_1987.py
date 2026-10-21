"""#1987: `dag.py titles` lists open issue titles (REST) into `<sdlc>/state/dag-titles.json`; `dag.py html` reads that
file and stays network-free. A scripted repository, no network.

Controls, each clause broken alone and seen red: prefer the file over `--titles` (test_titles_flag_wins); the stale
compare (test_stale_file_is_named_and_fresh_is_not, both directions); keep a closed member's earlier title
(test_closed_member_title_kept_and_foreign_number_dropped); the scrub (test_titles_are_scrubbed_like_titles_input);
the listing cap (test_listing_is_capped_and_says_so); the atomic write (test_failed_listing_keeps_the_old_file)."""
import importlib.util
import json
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
S = ROOT / "skills" / "sigma-loop" / "scripts"
sys.path.insert(0, str(ROOT / "tests"))


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


dt = _mod("dag_titles")
dh = _mod("dag_html")
dag = _mod("dag")

NOW = 1_800_000_000.0
from test_dag_html import NODES, EDGES, _store, _iso   # noqa: E402  (the shared fixture map: members 4..31)


@pytest.fixture
def sdlc(tmp_path):
    return _store(tmp_path, NODES, EDGES)


class Fake:
    def __init__(self, titles, fail=False):
        self.titles, self.fail, self.calls = titles, fail, []

    def __call__(self, args):
        self.calls.append(list(args))
        if self.fail:
            raise RuntimeError("boom")
        f = {}
        for i, a in enumerate(args):
            if a == "-f":
                k, _, v = args[i + 1].partition("=")
                f[k] = v
        rows = [{"number": n, "title": t, "state": "open", "labels": [], "body": "x"}
                for n, t in sorted(self.titles.items())]
        per, page = int(f["per_page"]), int(f["page"])
        return json.dumps(rows[(page - 1) * per:page * per])


def _file(sdlc):
    return sdlc / "state" / "dag-titles.json"


def _page(sdlc, now=NOW):
    lines, code = dh.run(str(sdlc), goal=10, now=now)
    assert code == 0, lines
    return (sdlc / "state" / "dag-html" / "chain-10.html").read_text()


def test_listing_writes_the_file_one_call_per_page(sdlc):
    fake = Fake({n: "title %d" % n for n in (5, 6, 7, 8, 9, 10)})
    lines, code = dt.run(str(sdlc), now=NOW, run=fake)
    assert code == 0, lines
    doc = json.loads(_file(sdlc).read_text())
    assert doc["schema"] == "dag-titles/1" and doc["read_at"] == _iso(NOW)
    assert doc["titles"]["7"] == "title 7"
    assert len(fake.calls) == 1                           # 6 open issues: one page


def test_many_pages_are_that_many_calls(sdlc):
    fake = Fake({n: "t" for n in range(1, 251)})
    dt.run(str(sdlc), now=NOW, run=fake)
    assert len(fake.calls) == 3                           # ceil(250 / 100)


def test_closed_member_title_kept_and_foreign_number_dropped(sdlc):
    _file(sdlc).write_text(json.dumps({"schema": "dag-titles/1", "read_at": _iso(NOW - 99),
                                       "titles": {"4": "was open once", "777": "not in the map"}}))
    dt.run(str(sdlc), now=NOW, run=Fake({7: "seven", 999: "outside the map"}))
    t = json.loads(_file(sdlc).read_text())["titles"]
    assert t == {"4": "was open once", "7": "seven"}      # 4 is a closed map member (kept); 777 and 999 are no node


def test_titles_are_scrubbed_like_titles_input(sdlc, tmp_path):
    raw = "see https://example.test/x and http-ish " + "y" * 200
    dt.run(str(sdlc), now=NOW, run=Fake({7: raw}))
    got = json.loads(_file(sdlc).read_text())["titles"]["7"]
    assert got == dh.clean_title(raw)
    assert "http" not in got.replace("ht​tp", "") and len(got) <= dh.TITLE_CHARS


def test_html_uses_the_file_and_the_flag_wins(sdlc, tmp_path):
    """control: read the file first and ignore `--titles`: the flag title is then missing and this is red."""
    dt.run(str(sdlc), now=NOW, run=Fake({7: "from the listing"}))
    page = _page(sdlc)
    assert "from the listing" in page and "No titles were given" not in page
    flag = tmp_path / "t.json"
    flag.write_text(json.dumps({"7": "from the flag"}))
    lines, code = dh.run(str(sdlc), goal=10, titles_path=str(flag), now=NOW)
    page = (sdlc / "state" / "dag-html" / "chain-10.html").read_text()
    assert code == 0 and "from the flag" in page and "from the listing" not in page


def test_no_file_keeps_the_no_titles_notice(sdlc):
    assert "No titles were given" in _page(sdlc)
    assert "dag.py titles" in _page(sdlc)                 # and it names the verb that makes them


def test_stale_file_is_named_and_fresh_is_not(sdlc):
    """control: flip the compare (older -> newer) or drop it: one of the two halves goes red."""
    dt.run(str(sdlc), now=NOW - 3600, run=Fake({7: "seven"}))        # map built at NOW - 60: file is older
    assert "older than the map" in _page(sdlc)
    dt.run(str(sdlc), now=NOW, run=Fake({7: "seven"}))
    assert "older than the map" not in _page(sdlc)


def test_html_makes_no_github_call(sdlc, monkeypatch):
    dt.run(str(sdlc), now=NOW, run=Fake({7: "seven"}))
    boom = Fake({}, fail=True)
    monkeypatch.setattr(_mod("gh_api"), "_default_run", boom)
    assert dh.run(str(sdlc), all_=True, now=NOW)[1] == 0
    assert boom.calls == []
    assert not hasattr(dh, "dag_titles")                 # the html module does not even load the network verb


def test_failed_listing_keeps_the_old_file(sdlc):
    """control: write the file before listing (or non-atomically): the old file is lost and this is red."""
    dt.run(str(sdlc), now=NOW, run=Fake({7: "old"}))
    before = _file(sdlc).read_bytes()
    lines, code = dt.run(str(sdlc), now=NOW + 5, run=Fake({}, fail=True))
    assert code == 1 and _file(sdlc).read_bytes() == before
    assert not list(_file(sdlc).parent.glob(".dag-titles*"))


def test_listing_is_capped_and_says_so(sdlc):
    """control: remove the cap: 50 calls become more and the line stops naming what was left out."""
    fake = Fake({n: "t" for n in range(1, 5301)})
    lines, code = dt.run(str(sdlc), now=NOW, run=fake)
    assert len(fake.calls) == dt.TITLES_LIST_CAP // 100 == 50
    assert any("left out" in l for l in lines)


def test_mode_off_touches_nothing(tmp_path):
    sdlc = _store(tmp_path, NODES, EDGES, mode="off")
    fake = Fake({7: "x"})
    lines, code = dt.run(str(sdlc), now=NOW, run=fake)
    assert code == 4 and fake.calls == [] and not _file(sdlc).exists()


def test_cli_verb_exists():
    assert dag._load("dag_titles") is not None
    assert re.search(r"^\s+dag\.py titles", dag.__doc__, re.M)
