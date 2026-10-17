"""A delete resolution in session mode: which lines may be lost with the file.

Rule 1: the integration side deleted a file the unit changed and the session keeps the delete. Every line the unit added
must be elsewhere in the resolved tree, or be listed in `dropped-by-delete.txt` AND be approved by the independent review.
Rule 2: the unit deleted a file the integration side changed. Every integration-added line must be elsewhere; no list.
Rule 3: the list goes on the upkeep record and in the commit message.
Driven by a fake `claude` through the harness of tests/test_upkeep_session_1573.py. Controls (each broken once and seen
red, then restored): the moved-lines subtraction, the list clause, the reviewer-sees-the-list clause, the rule 2
refusal, and the record/message wiring. Gesture: `python -m pytest tests/test_upkeep_delete_1716.py -q`."""
import sys

import pytest

import test_feature_rebase as base
import test_upkeep_level1 as l1
from test_upkeep_agent_1516 import agent_cfg, wired  # noqa: F401
from test_upkeep_level2 import APP, FEATURE, UNIT, mk, rig  # noqa: F401
from test_upkeep_session_1573 import _delete_in_base, run_session

STUB = '''#!%(python)s
import json, os, pathlib, sys
here = pathlib.Path(%(dir)r)
mode = json.loads((here / "mode.json").read_text())
if sys.argv[1:3] == ["auth", "status"]:
    raise SystemExit(0)
stdin = sys.stdin.read()
reviewer = "independent reviewer" in stdin
calls = json.loads((here / "calls.json").read_text()) if (here / "calls.json").exists() else []
calls.append({"role": "review" if reviewer else "resolve", "stdin": stdin})
(here / "calls.json").write_text(json.dumps(calls))
if reviewer:
    listed = "dropped by delete" in stdin and "feature side" in stdin
    if mode.get("reject_list") and listed:
        print(json.dumps({"verdict": "block", "reasons": ["the list loses real work"]}))
    else:
        print(json.dumps({"verdict": "approve", "reasons": ["ok"]}))
    raise SystemExit(0)
cwd = pathlib.Path(os.getcwd())
for gone in mode.get("rm", []):
    if (cwd / gone).exists():
        (cwd / gone).unlink()
for name, lines in mode.get("declare", {}).items():
    latest = sorted((cwd / ".sigma-context").glob("stop*"))[-1]
    with open(latest / "dropped-by-delete.txt", "a") as out:
        out.write("## %%s\\n%%s\\n" %% (name, "\\n".join(lines)))
print("done")
'''

MOVED = "src/moved.txt"


@pytest.fixture
def drig(rig):  # noqa: F811
    rig.binary.write_text(STUB % {"python": sys.executable, "dir": str(rig.fake)})
    return rig


def _integ_deleted(tmp_path, with_moved):
    """The unit changed APP; the integration side deleted it."""
    feat = {APP: "line1\nfeature side\nline3\n", "src/extra.txt": "unrelated\n"}
    if with_moved:
        feat[MOVED] = "feature side\nmore\n"
    w = l1.world(mk(tmp_path / "w"), feat=feat, integ={"other.txt": "x\n"},
                 base_files={"CHANGELOG.md": l1.LOG % "", APP: "line1\nshared\nline3\n"})
    _delete_in_base(w, APP)
    return w


def _unit_deleted(tmp_path, moved_elsewhere=False):
    """The unit deletes APP; the integration side added a line to it."""
    w = l1.world(mk(tmp_path / "w"), feat={"other.txt": "y\n"}, integ={APP: "line1\nshared\nintegration side\nline3\n"},
                 base_files={"CHANGELOG.md": l1.LOG % "", APP: "line1\nshared\nline3\n"})
    base._git(w.local, "checkout", "-q", FEATURE)
    base._git(w.local, "rm", "-q", APP)
    if moved_elsewhere:
        base._write(w.local / MOVED, "totally different header\nintegration side\nand a long unrelated tail\nmore tail\n")
        base._git(w.local, "add", MOVED)
    l1._commit(w.local, "feat: remove the file (#12)")
    base._git(w.local, "push", "-q", "origin", FEATURE)
    base._git(w.local, "checkout", "-q", l1.MAIN)
    return w


def _message(w):
    base._git(w.local, "fetch", "-q", "origin")
    return base._git(w.local, "log", "-1", "--format=%B", "origin/" + FEATURE)


def test_rule1a_unit_lines_moved_elsewhere_let_the_delete_stand_with_one_push_and_a_backup(drig, tmp_path):
    drig.mode({"rm": [APP]})
    w = _integ_deleted(tmp_path, with_moved=True)
    m, w, report, spy = run_session(drig, tmp_path, world=w)
    assert report["outcome"] == m.REBASED, report.get("why")
    assert len(spy.pushes()) == 1 and report["backup"]
    assert "dropped_by_delete" not in report["resolved"]["extra"]
    assert "dropped by delete" not in _message(w)


def test_rule1b_listed_lines_with_reviewer_approval_are_dropped_and_recorded(drig, tmp_path):
    drig.mode({"rm": [APP], "declare": {APP: ["feature side"]}})
    w = _integ_deleted(tmp_path, with_moved=False)
    m, w, report, spy = run_session(drig, tmp_path, world=w)
    assert report["outcome"] == m.REBASED, report.get("why")
    assert len(spy.pushes()) == 1 and report["backup"]
    assert report["resolved"]["extra"]["dropped_by_delete"] == {APP: ["feature side"]}
    message = _message(w)
    assert "dropped by delete" in message and "feature side" in message
    reviews = [c for c in drig.calls() if c["role"] == "review"]
    assert reviews and "dropped by delete" in reviews[0]["stdin"]


def test_rule1b_the_reviewer_rejecting_the_list_parks_and_pushes_nothing(drig, tmp_path):
    drig.mode({"rm": [APP], "declare": {APP: ["feature side"]}, "reject_list": True})
    w = _integ_deleted(tmp_path, with_moved=False)
    before = w.tip(FEATURE)
    m, w, report, spy = run_session(drig, tmp_path, world=w)
    assert report["outcome"] != m.REBASED and not spy.pushes() and w.tip(FEATURE) == before
    assert "reviewer" in report["why"]


def test_rule1_lines_neither_moved_nor_listed_park_naming_them(drig, tmp_path):
    drig.mode({"rm": [APP], "declare": {APP: ["some other line"]}})
    w = _integ_deleted(tmp_path, with_moved=False)
    m, w, report, spy = run_session(drig, tmp_path, world=w)
    assert not spy.pushes() and "unit-line-dropped" in report["why"] and "feature side" in report["why"], report["why"]


def test_rule2_a_unit_delete_that_loses_integration_lines_is_refused_and_a_list_cannot_save_it(drig, tmp_path):
    drig.mode({"rm": [APP], "declare": {APP: ["integration side"]}})
    w = _unit_deleted(tmp_path)
    before = w.tip(FEATURE)
    m, w, report, spy = run_session(drig, tmp_path, world=w)
    assert not spy.pushes() and w.tip(FEATURE) == before
    assert "base-line-dropped" in report["why"] and "integration side" in report["why"], report["why"]


def test_rule2_integration_lines_found_elsewhere_in_the_resolved_tree_let_the_delete_stand(drig, tmp_path):
    drig.mode({"rm": [APP]})
    w = _unit_deleted(tmp_path, moved_elsewhere=True)
    m, w, report, spy = run_session(drig, tmp_path, world=w)
    assert report["outcome"] == m.REBASED, report.get("why")
    assert len(spy.pushes()) == 1 and report["backup"]


def test_rule1b_an_oversize_dropped_list_is_truncated_so_the_review_cannot_approve_it_and_nothing_is_pushed(drig, tmp_path):
    """Each deleted file stays under the per-side cap, so only the dropped-by-delete block (both files, ~30k) is over it."""
    second = "src/second.txt"
    a = ["alpha line %05d %s" % (i, "x" * 40) for i in range(250)]
    b = ["beta line %05d %s" % (i, "y" * 40) for i in range(250)]
    feat = {APP: "line1\n" + "\n".join(a) + "\nline3\n", second: "line1\n" + "\n".join(b) + "\nline3\n",
            "src/extra.txt": "unrelated\n"}
    w = l1.world(mk(tmp_path / "w"), feat=feat, integ={"other.txt": "x\n"},
                 base_files={"CHANGELOG.md": l1.LOG % "", APP: "line1\nshared\nline3\n", second: "line1\nshared\nline3\n"})
    _delete_in_base(w, APP)
    _delete_in_base(w, second)
    drig.mode({"rm": [APP, second], "declare": {APP: a, second: b}})
    before = w.tip(FEATURE)
    m, w, report, spy = run_session(drig, tmp_path, world=w)
    assert report["outcome"] != m.REBASED and not spy.pushes() and w.tip(FEATURE) == before
    assert "truncated" in report["why"] and "dropped-by-delete" in report["why"], report["why"]
    assert not [c for c in drig.calls() if c["role"] == "review"]
