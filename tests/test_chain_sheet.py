"""#1585: `chain.py sheet` and `chain.py answer` -- every decision a chain needs, in front of the person
together, before anything is changed.

The sheet reads only the local chain record. `answer` writes only that record, and hands a parked member's
answer to the existing unpark resolve path (a stateful fake `gh`; no network). Controls (each clause alone,
then restored): give the cap a default; let approval ignore one question kind; drop the unanswered check."""
import json
import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import resource_mode  # noqa: E402,F401  (a test building its own subprocess env carries the mode from here)
import test_label_answer as tla  # noqa: E402
from test_label_drain_arm import DW, _mod, _repo  # noqa: E402

NOW = 1_800_000_000.0
KIND_DEP = "Parked by Sigma: waiting on the other issue.\nsigma-qkind: dependency"


def _chain():
    return _mod("chain")


def _unpark():
    return _mod("unpark")


def _backlog(answered=False):
    """Fixture backlog: goal 1 waits on parked 2 (dependency), unarmed 3 and closed 4."""
    body2 = DW
    if answered:
        body2 = DW + "\n" + _unpark().render_block({"route": "wait for it"})
    return [tla.issue(1, "sdlc:goal", body="Blocked by #2\nBlocked by #3\nBlocked by #4\n\n" + DW),
            tla.issue(2, "sdlc:goal", "sdlc:parked", body=body2, comments=[KIND_DEP]),
            tla.issue(3, body=DW),
            tla.issue(4, "sdlc:goal", state="closed")]


def _scene(tmp_path, answered=False, issues=None):
    base = _repo(tmp_path, "")
    cp = pathlib.Path(base) / "config.json"
    cfg = json.loads(cp.read_text())
    cfg["discovery"]["dag"] = {"mode": "off"}
    cp.write_text(json.dumps(cfg))
    fk = tla.Fake(issues if issues is not None else _backlog(answered))
    _chain().plan(base, 1, run=fk, now=NOW)
    return base, fk


def _rec_path(base):
    return pathlib.Path(base) / "state" / "chains" / "c1.json"


def _rec(base):
    return json.loads(_rec_path(base).read_text())


def _put(base, rec):
    _rec_path(base).write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n")


def _answers(tmp_path, d):
    f = tmp_path / "answers.json"
    f.write_text(json.dumps(d))
    return str(f)


def _tree(base):
    root = pathlib.Path(base)
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


GOLDEN = """Chain sheet for #1
End goal: #1 (armed)
Open members: 3, depth 1, units: none
Source: live (map: off); planned 5m ago; plan complete
State: planned

Order, deepest first:
  1. #2 parked (kind dependency)
  2. #3 unarmed
  3. #1 armed; waits on #2, #3
Done prerequisites: #4
Runs ahead of: 0 higher-priority open goal(s)

Decisions (3 open of 3):
  1. [dependency:2] A blocking issue is still open. Do you want to wait for it, work around it, or drop this?
     options: wait for it (recommended) / work around it / drop this
  2. [label_set:c1] Arm the unarmed issues in this set?
     options: arm them (recommended) / arm only the ones that can start / leave them
  3. [cap:c1] What cap applies to this chain? (a positive whole number; no estimate exists yet, so there is no default)
     options: none, answer with a positive whole number

Writes that follow approval:
  - record each answer in .sdlc/state/chains/c1.json (numbers and labels only)
  - #2: the Q&A block and one comment through the unpark resolve path; its labels change only if the answer starts it again
  - answers to an undeclared-kind or spend question are recorded here only: declaring the kind and writing the spend marker belong to a later slice
  - after approval, `chain.py arm` creates the label chain:1 if it is missing
  - #2: add label chain:1
  - #3: add label chain:1
  - #1: add label chain:1
  - arming, per member below: add sdlc:goal, priority P2 only if it has none, assign you only if nobody is assigned, never edit the body; one comment with issue numbers only
  - #3: arm it and add one comment
  - nothing else: an upper-tier, parked or assigned-elsewhere member is labelled, never armed; starting the members belongs to a later slice

Nothing has been changed yet.
"""


def test_sheet_golden(tmp_path):
    base, _fk = _scene(tmp_path)
    assert _chain().sheet(base, 1, now=NOW + 300) == GOLDEN


def test_answered_not_asked(tmp_path):
    base, _fk = _scene(tmp_path, answered=True)
    assert next(r for r in _rec(base)["members"] if r["number"] == 2)["answered"] is True
    text = _chain().sheet(base, 1, now=NOW)
    assert "[dependency:2]" not in text and "Decisions (2 open of 2)" in text
    # and it does not block approval: only the other two are needed
    res = _chain().answer(base, 1, {"label_set:c1": "leave them", "cap:c1": "5"})
    assert res["state"] == "approved" and res["open"] == []


def test_defaults_leave_cap_open(tmp_path):
    base, _fk = _scene(tmp_path)
    res = _chain().answer(base, 1, defaults=True, run=_fk)
    rec = _rec(base)
    assert res["open"] == ["cap:c1"] and "cap:c1" not in rec["answers"]
    assert rec["state"] == "awaiting-answers"
    assert rec["answers"]["label_set:c1"] == "arm them"        # the recommended (first) option
    assert rec["answers"]["dependency:2"] == "wait for it"


def _blocks(fk, n):
    return [c for c in fk.stored_comments(n)] + [fk.body(n)]


def test_parked_answer_recorded(tmp_path):
    base, fk = _scene(tmp_path)
    res = _chain().answer(base, 1, {"dependency:2": "wait for it"}, run=fk)
    assert res["results"][0]["outcome"] == "resolved"
    # the same Q&A block the unpark flow writes, from a second, untouched fixture
    other = tmp_path / "other"
    other.mkdir()
    base2, fk2 = _scene(other)
    cfg = json.loads((pathlib.Path(base2) / "config.json").read_text())
    up = _unpark()
    src = _mod("sources").GitHubSource(cfg, run=fk2)
    r = up.resolve(base2, cfg, 2, {"route": "wait for it"}, "keep-parked", source=src, apply=True)
    assert r["outcome"] == "kept-parked"
    assert fk.body(2) == fk2.body(2) and "wait for it" in fk.body(2)
    assert fk.stored_comments(2)[1:] == fk2.stored_comments(2)[1:] and len(fk.stored_comments(2)) == 2
    assert _rec(base)["answers"]["dependency:2"] == "wait for it"
    # nothing was written to the other members
    assert fk.stored_comments(1) == [] and fk.stored_comments(3) == []


def _full_record(base):
    """Add the three kinds a small live fixture cannot produce on its own: a cross-unit edge, a spend step,
    and an end goal that cannot be armed."""
    rec = _rec(base)
    rec["cross_unit"] = [{"from": 1, "to": 3, "units": ["alpha", "beta"]}]
    next(r for r in rec["members"] if r["number"] == 3)["spend"] = True
    next(r for r in rec["members"] if r["number"] == 1)["verdict"] = "upper-tier"
    _put(base, rec)
    return rec


KINDS = ["dependency", "label_set", "irreversible", "cross_unit", "upper_tier", "cap"]


@pytest.mark.parametrize("alone", KINDS)
def test_approval_requires_all(tmp_path, alone):
    base, fk = _scene(tmp_path)
    _full_record(base)
    ch = _chain()
    qs, openq = ch.open_questions(_rec(base))
    assert {q["kind"] for q in openq} == set(KINDS)
    pick = {"dependency": "wait for it", "label_set": "leave them", "irreversible": "leave it",
            "cross_unit": "keep going", "upper_tier": "leave it", "cap": "7"}
    for q in qs:
        if q["kind"] == "irreversible":
            pick["irreversible"] = q["options"][-1]
    rest = {q["id"]: pick[q["kind"]] for q in openq if q["kind"] != alone}
    res = ch.answer(base, 1, rest, run=fk)
    assert res["errors"] == [], res
    assert res["state"] == "awaiting-answers" and [i.split(":")[0] for i in res["open"]] == [alone]
    assert _rec(base)["state"] == "awaiting-answers"
    last = next(q["id"] for q in openq if q["kind"] == alone)
    res = ch.answer(base, 1, {last: pick[alone]}, run=fk)
    assert res["state"] == "approved" and res["open"] == [] and _rec(base)["state"] == "approved"


def test_approval_refuses_an_incomplete_plan_and_bad_answers(tmp_path):
    base, fk = _scene(tmp_path)
    ch = _chain()
    before = _tree(base)
    for bad in ({"cap:c1": "lots"}, {"cap:c1": "0"}, {"dependency:2": "maybe"}, {"nope:1": "x"}):
        assert ch.answer(base, 1, bad, run=fk)["errors"]
        assert _tree(base) == before and fk.stored_comments(2) == [KIND_DEP]
    rec = _rec(base)
    rec["complete"] = False
    _put(base, rec)
    res = ch.answer(base, 1, {"label_set:c1": "leave them", "cap:c1": "5", "dependency:2": "work around it"},
                    run=fk)
    assert res["open"] == [] and res["state"] == "awaiting-answers"
    assert ch.main(["chain.py", "answer", base, "1", "--defaults", "--partial"]) == 2


def _many(base, n):
    rec = _rec(base)
    rec["members"] = [{"number": 100 + i, "verdict": "parked", "kind": "dependency", "labels": [],
                       "waits_on": [], "depth": 0, "prio": None} for i in range(n)]
    rec["cross_unit"], rec["done"] = [], []
    _put(base, rec)


def test_grouping(tmp_path):
    base, fk = _scene(tmp_path)
    ch = _chain()
    _many(base, 24)                                   # 24 + the cap = 25: not over the line
    text = ch.sheet(base, 1, now=NOW)
    assert "grouped" not in text and "[dependency:100]" in text
    _many(base, 25)                                   # 25 + the cap = 26
    text = ch.sheet(base, 1, now=NOW)
    assert "grouped by kind" in text and "[group:dependency]" in text
    assert "[dependency:100]" not in text and "applies to 25 questions" in text
    assert "wait for it (recommended)" in text
    res = ch.answer(base, 1, {"group:dependency": "work around it", "cap:c1": "3"}, run=fk)
    assert res["errors"] == [] and res["state"] == "approved"
    answers = _rec(base)["answers"]
    assert len([k for k in answers if k.startswith("dependency:")]) == 25
    assert fk.writes() == []                          # "work around it" resolves nothing on GitHub
    assert ch.answer(base, 1, {"group:upper_tier": "x"}, run=fk)["errors"]


def test_sheet_read_only(tmp_path, monkeypatch, capsys):
    base, fk = _scene(tmp_path)
    ch = _chain()
    before, calls = _tree(base), len(fk.calls)

    def boom(*a, **k):
        raise AssertionError("the sheet ran a subprocess")
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    assert ch.main(["chain.py", "sheet", base, "c1"], now=NOW, run=boom) == 0
    assert "Nothing has been changed yet." in capsys.readouterr().out
    assert _tree(base) == before and len(fk.calls) == calls and fk.writes() == []
    assert ch.main(["chain.py", "sheet", base, "99"], now=NOW) == 2          # no record: refused, still no write


def test_no_free_text_is_posted(tmp_path):
    base, fk = _scene(tmp_path)
    secret = "pay the invoice at the usual place"
    assert _chain().answer(base, 1, {"dependency:2": secret}, run=fk)["errors"]
    res = _chain().answer(base, 1, {"dependency:2": "wait for it"}, run=fk)
    assert res["errors"] == []
    posted = "\n".join(fk.stored_comments(2)[1:] + [fk.body(2)])
    assert secret not in posted and "issue 2" not in posted       # only the bank option, never a title or free text
