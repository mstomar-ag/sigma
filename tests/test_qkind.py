"""#994 (decision rubric slice 4): the question-kind declaration on a park."""
import importlib.util, json, pathlib, subprocess, sys, tempfile

from test_loop import _loop, _telemetry_backlog, S


def _qkind():
    spec = importlib.util.spec_from_file_location("qkind", S / "qkind.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


class _Capture:
    def __init__(self): self.parked = []
    def complete(self, g): pass
    def fail(self, g, r): pass
    def park(self, g, r): self.parked.append(r)


def _cli(*a):
    return subprocess.run([sys.executable, str(S / "loop.py"), *a], capture_output=True, text=True)


def test_unknown_qkind_is_refused():
    with tempfile.TemporaryDirectory() as d:
        base, goal = _telemetry_backlog(d)
        r = _cli("record", base, goal, "parked", "needs a call", "--qkind", "nonsense")
        assert r.returncode == 2 and "qkind" in r.stderr
        assert "parked" not in (pathlib.Path(base) / "goals" / "0001.md").read_text()


def test_omitted_qkind_keeps_todays_guess():
    lp, sink = _loop(), _Capture()
    with tempfile.TemporaryDirectory() as d:
        base, _ = _telemetry_backlog(d)
        lp._record(base, sink, "g.md", "parked", "changes requested on the API shape")
    assert sink.parked == ["changes requested on the API shape"]
    q = _qkind()
    assert q.from_reason_class(lp._reason_class(sink.parked[0])) == "needs_decision"


def test_every_reason_class_maps_to_a_qkind():
    q, lp = _qkind(), _loop()
    for rc in lp.ledger.REASON_CLASSES:
        assert q.from_reason_class(rc) in q.QKINDS, rc
    assert q.from_reason_class("never-heard-of-it") == "unknown"
    assert {"scope_hold", "owner_hold"} <= set(q.QKINDS)
    assert q.from_reason_class("merge_conflict") == "merge_conflict"


def test_park_comment_roundtrips_the_line():
    q = _qkind()
    for k in q.QKINDS:
        assert q.parse_line("why\n" + q.render_line(k)) == k
    assert q.parse_line("sigma-qkind: bogus") is None
    assert q.parse_line("no line here") is None


def test_declared_kind_reaches_the_park_comment():
    lp, sink, q = _loop(), _Capture(), _qkind()
    with tempfile.TemporaryDirectory() as d:
        base, _ = _telemetry_backlog(d)
        lp._record(base, sink, "g.md", "parked", "owner must pick", qkind="owner_hold")
    assert q.parse_line(sink.parked[0]) == "owner_hold"
    assert sink.parked[0].startswith("owner must pick")


def test_documented_gesture_declares_a_kind():
    skill = (S.parent / "SKILL.md").read_text()
    line = next(l for l in skill.splitlines() if "--qkind" in l)
    assert "parked" in line
    with tempfile.TemporaryDirectory() as d:
        base, goal = _telemetry_backlog(d)
        r = _cli("record", base, goal, "parked", "needs a call", "--qkind", "needs_decision")
        assert r.returncode == 0, r.stderr
