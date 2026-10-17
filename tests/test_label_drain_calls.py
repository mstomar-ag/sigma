"""#1582: how many GitHub calls a label drain costs, counted through a counting fake transport.

These are COUNTS FROM A FAKE, not timings and not a run against a real repository. The fake is the
stateful one `tests/test_label_drain_arm.py` uses (it serves reads and applies writes), and every call
that reaches it is one counted call. `docs/label-drain.md` carries the table `measure()` returns, and
`test_the_page_table_is_the_measured_table` fails the day the page and the counts disagree.
"""
import pathlib
import re
import tempfile

import pytest

from test_label_drain_arm import DW, Fake, _issue, _mod, _repo
from test_label_scope import LBL, _source

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGE = ROOT / "docs" / "label-drain.md"
GOAL = "sdlc:goal"
PRIORITY = "priority:P2"
OUTSIDE = 9
_PAID = "Paid step: render one clip.\n\n" + DW


def scenario():
    """Six issues. Members of the label: #1 armed and ready, #2 and #3 unarmed (no goal label),
    #4 parked, #5 a paid-step member held by an open prerequisite. #9 is that prerequisite, open
    and outside the label."""
    return [
        _issue(1, GOAL, LBL, PRIORITY),
        _issue(2, LBL),
        _issue(3, LBL),
        _issue(4, GOAL, LBL, PRIORITY, "sdlc:parked"),
        _issue(5, GOAL, LBL, PRIORITY, body="Blocked by #%d\n\n%s" % (OUTSIDE, _PAID)),
        _issue(OUTSIDE, GOAL, PRIORITY),
    ]


def _tally(fake):
    writes = len(fake.writes())
    return {"total": len(fake.calls), "reads": len(fake.calls) - writes, "writes": writes}


def _pick(tmp_path, issues, scope):
    """One `loop.py next` decision on a fresh transport over `issues` -> (result, tally)."""
    lp = _mod("loop")
    base = _repo(tmp_path)
    fk = Fake(issues)
    cfg = lp.state.load_config(base)
    src = lp.sources.GitHubSource(cfg, run=fk, sdlc_dir=base)
    src._BACKLOG_READ_RETRY_BASE = 0
    if scope:
        lp.sources.scope_to_label(src, scope)
    res = lp._next(base, src, cfg)
    return res, _tally(fk)


def measure():
    """-> (rows, notes): rows are (what, total, reads, writes), in the order the page prints them."""
    ld = _mod("label_drain")
    rows, notes = [], {}
    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        for name in ("plan", "arm", "pick-label-unarmed", "pick-label-ready", "pick-plain"):
            (root / name).mkdir()
        base = _repo(root / "plan")
        fk = Fake(scenario())
        plan = ld.plan(base, LBL, run=fk)
        notes["plan_members"] = len(plan["members"])
        rows.append(("plan", *_tally(fk).values()))

        base = _repo(root / "arm")
        fk = Fake(scenario())
        ld.arm(base, LBL, apply=True, run=fk)
        rows.append(("arm --apply", *_tally(fk).values()))
        armed = [dict(i, labels=list(i["labels"])) for i in fk.by.values()]

        res, t = _pick(root / "pick-label-unarmed", scenario(), LBL)
        notes["unarmed_result"] = res
        rows.append(("next --label, before arm", *t.values()))

        state = [dict(i) for i in armed]
        for i in state:                      # the open question answered: the person unparked #4
            i["labels"] = [x for x in i["labels"] if x["name"] != "sdlc:parked"]
        res, t = _pick(root / "pick-label-ready", state, LBL)
        notes["ready_result"] = res
        rows.append(("next --label, after arm and answer", *t.values()))

        res, t = _pick(root / "pick-plain", state, None)
        notes["plain_result"] = res
        rows.append(("next, no label", *t.values()))
    return rows, notes


def test_the_counting_transport_counts_every_call_and_plan_never_writes():
    rows, notes = measure()
    by = {r[0]: r for r in rows}
    assert notes["plan_members"] == 5
    assert by["plan"][1] > 0 and by["plan"][3] == 0
    assert by["arm --apply"][3] > 0
    assert by["next --label, before arm"][3] == 0          # a refused start claims and writes nothing
    assert all(r[1] == r[2] + r[3] for r in rows)


def test_the_picks_are_the_picks_the_page_describes():
    _, notes = measure()
    assert notes["unarmed_result"][0] != "goal"            # not ready: refused before any claim
    assert notes["ready_result"][0] == "goal"
    assert notes["ready_result"][1] in {"1", "2", "3", "5"}   # an armed member; #4 stays parked
    assert notes["plain_result"][0] == "goal"


def _page_rows():
    text = PAGE.read_text(encoding="utf-8")
    out = {}
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 4 and all(c.isdigit() for c in cells[1:]):
            out[cells[0].strip("`")] = tuple(int(c) for c in cells[1:])
    return out


def test_the_page_table_is_the_measured_table():
    rows, _ = measure()
    page = _page_rows()
    for what, total, reads, writes in rows:
        assert page.get(what) == (total, reads, writes), (
            f"docs/label-drain.md row {what!r} is {page.get(what)}, the counting transport measured "
            f"{(total, reads, writes)}")


def test_print_the_measured_rows(capsys):
    """Not an assertion: `pytest -s` shows the table to paste into the page after a deliberate change."""
    rows, notes = measure()
    with capsys.disabled():
        for r in rows:
            print("MEASURED", r)
        print("MEASURED", notes)
