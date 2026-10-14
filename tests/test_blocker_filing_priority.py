"""#1408: a blocker is filed at the blocked goal's priority (never lowered), a blocker of an open
edge never keeps the legacy confirmation label, and doctor names a blocker that still does or has
no priority. Driven through the real verbs with the fake-gh runner of test_blocker_rank_writers."""
import json, pathlib, importlib.util

import test_blocker_rank_writers as w
import test_handoff as th
import gqlfake

D = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-doctor" / "scripts" / "doctor.py"
LEGACY = "sdlc:needs-confirmation"


def _file(tmp_path, views, mode=None, **kw):
    """Open a blocker through the real hand_off, the source's own filing-priority method wired in."""
    run = w._runner(views)
    real = w._src(run, mode)

    class Src(th.FakeSource):
        def blocker_filing_priority(self, goal, own):
            return real.blocker_filing_priority(goal, own)
    src = Src()
    report = th.handoff.hand_off(th._project(tmp_path), th.ONL, "9", "engine", "needs a flag",
                                 source=src, **kw)
    return run, src, report


def _prio(src):
    return [l for l in src.created["labels"] if l.startswith("priority:")]


def test_a_blocker_is_filed_at_the_blocked_goals_priority_when_its_own_is_lower_or_unknown(tmp_path):
    for i, own in enumerate(("P3", "P9", "P4")):
        run, src, _ = _file(tmp_path / str(i), {"9": {"labels": ["priority:P0"]}}, priority=own)
        assert _prio(src) == ["priority:P0"], own
        assert len(run.calls) <= 3                  # one bounded read of the goal, nothing written
        assert w._writes(run) == []


def test_a_blocker_is_never_lowered_and_an_unreadable_goal_files_as_before(tmp_path, capsys):
    _, src, _ = _file(tmp_path / "a", {"9": {"labels": ["priority:P3"]}}, priority="P1")
    assert _prio(src) == ["priority:P1"]
    _, src2, rep = _file(tmp_path / "b", {"9": {"labels": ["priority:P0"]}}, priority="P2")
    assert _prio(src2) == ["priority:P0"]
    run = w._runner({"9": {"labels": ["priority:P0"]}}, fail_reads=("9",))
    assert w._src(run).blocker_filing_priority("9", "P2") is None
    assert "could not be read" in capsys.readouterr().err


def test_mode_off_files_byte_identically_with_no_read(tmp_path):
    run, src, _ = _file(tmp_path, {"9": {"labels": ["priority:P0"]}}, mode="off", priority="P3")
    assert _prio(src) == ["priority:P3"]
    assert run.calls == []


def test_the_confirmation_label_is_stripped_once_with_one_comment_and_a_second_pass_is_silent():
    run = w._runner({"7": {"labels": [LEGACY, "priority:P0"]}})
    assert w._src(run).strip_blocker_confirmation("7") == "stripped"
    removed = [c for c in run.calls if c[:3] == ["issue", "edit", "7"] and "--remove-label" in c and LEGACY in c]
    assert len(removed) == 1
    assert len([c for c in run.calls if c[:3] == ["issue", "comment", "7"]]) == 1
    clean = w._runner({"7": {"labels": ["priority:P0"]}, "9": {"labels": ["priority:P0"]}})
    assert w._src(clean).strip_blocker_confirmation("7") == "unchanged"
    assert [c for c in clean.calls if "--remove-label" in c] == []
    assert w._writes(clean) == []


def test_strip_is_off_with_mode_off_and_through_the_sweep_it_fires():
    off = w._runner({"7": {"labels": [LEGACY]}})
    assert w._src(off, "off").strip_blocker_confirmation("7") == "skipped: off"
    assert off.calls == []
    au = w._mod("auto_unpark")
    blocked = {"number": 9, "title": "", "body": "blocked by #7 until the base lands",
               "labels": [{"name": "sdlc:blocked"}, {"name": "priority:P0"}]}
    run = w._runner({"7": {"labels": [LEGACY, "priority:P3"]}, "9": {"labels": ["sdlc:blocked", "priority:P0"]}},
                    lists={"sdlc:blocked": [blocked]})
    au.sweep_unpark(".sdlc", w._config(), apply=True, run=run)
    assert any(c[:3] == ["issue", "edit", "7"] and "--remove-label" in c and LEGACY in c for c in run.calls)


def _doc():
    spec = importlib.util.spec_from_file_location("doctor", D)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


_NAME = "every sdlc:blocking issue carries a priority and no legacy confirmation label"


def _doctor_names(tmp_path, issues, mode=None):
    import test_doctor as td
    disc = {"source": "github", "github": {"repo": "acme/widget"}}
    if mode:
        disc["blocker_promotion"] = {"mode": mode}
    base = td._sdlc(tmp_path, {"discovery": disc})

    def run(args):
        if args[:3] == ["gh", "auth", "status"]:
            return "Logged in."
        if td._is_list(args):
            return json.dumps(issues if td._list_label(args) == "sdlc:blocking" else [])
        return ""
    return {c["name"]: c for c in _doc().check(base, run=run)}


def test_doctor_names_a_blocker_with_the_legacy_label_or_no_priority(tmp_path):
    issues = [{"number": 7, "labels": [{"name": "sdlc:blocking"}, {"name": "sdlc:goal"},
                                        {"name": "priority:P1"}, {"name": LEGACY}]},
              {"number": 8, "labels": [{"name": "sdlc:blocking"}, {"name": "sdlc:goal"}]},
              {"number": 10, "labels": [{"name": "sdlc:blocking"}, {"name": "sdlc:goal"},
                                         {"name": "priority:P2"}]}]
    hit = _doctor_names(tmp_path / "a", issues)[_NAME]
    assert hit["ok"] is False and "#7" in hit["fix"] and "#8" in hit["fix"] and "#10" not in hit["fix"]
    assert "Advisory" in hit["fix"]
    assert _NAME not in _doctor_names(tmp_path / "b", issues, mode="off")
    assert _NAME not in _doctor_names(tmp_path / "c", [issues[2]])
