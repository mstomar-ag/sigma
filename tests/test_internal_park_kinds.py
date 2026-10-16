"""#1524: the two internal park writers that passed no kind now declare one.

(1) `loop._record`'s source-error downgrade: `quota` for a quota/budget failure, else `needs_decision`.
(2) `work._stale_resume_refusal`'s escalating park: `needs_decision`.
Fake source only; the park comment text is read back for the kind line.
"""
from test_unpark import _mod

class _Src:
    def __init__(self, exc):
        self.exc = exc
        self.parks = []

    def complete(self, goal):
        if self.exc is not None:
            raise self.exc

    def park(self, goal, text):
        self.parks.append(text)
        return True


def _kind_lines(text):
    return [ln for ln in text.splitlines() if ln.startswith(_mod("qkind").LINE_PREFIX)]


def _expected(kind):
    return _mod("qkind").render_line(kind)


def test_downgrade_quota_failure_declares_quota(tmp_path):
    lp = _mod("loop")
    src = _Src(RuntimeError("API rate limit exceeded for GraphQL"))
    lp._record(str(tmp_path), src, "5", "done", "shipped")
    assert _kind_lines(src.parks[0]) == [_expected("quota")]


def test_downgrade_secondary_rate_limit_declares_quota(tmp_path):
    lp = _mod("loop")
    src = _Src(RuntimeError("You have exceeded a secondary rate limit"))
    lp._record(str(tmp_path), src, "5", "done", "shipped")
    assert _kind_lines(src.parks[0]) == [_expected("quota")]


def test_downgrade_other_failure_declares_needs_decision(tmp_path):
    lp = _mod("loop")
    src = _Src(RuntimeError("issue is locked"))
    lp._record(str(tmp_path), src, "5", "done", "shipped")
    assert _kind_lines(src.parks[0]) == [_expected("needs_decision")]


def test_ordinary_park_without_failure_is_byte_identical(tmp_path):
    lp = _mod("loop")
    src = _Src(None)
    lp._record(str(tmp_path), src, "5", "parked", "waiting on a person")
    assert src.parks == ["waiting on a person"]


def test_stale_resume_park_declares_needs_decision(monkeypatch, tmp_path):
    w = _mod("work")
    lp = _mod("loop")
    seen = {}
    monkeypatch.setattr(w, "ensure_fresh",
                        lambda *a, **k: "merge conflict " + w._PARK_STALE_RESUME_CONFLICT)
    monkeypatch.setattr(w, "_record", lambda d, g: {})
    monkeypatch.setattr(w, "_load", lambda name: lp)   # `_load` re-executes the module each call
    monkeypatch.setattr(lp.sources, "get_source", lambda d, c: object())
    monkeypatch.setattr(lp, "_record", lambda *a, **k: seen.update(args=a, kw=k) or "parked")
    out = w._stale_resume_refusal(str(tmp_path), {}, "5")
    assert out.startswith(w._STALE_RESUME_REFUSAL_PREFIX)
    assert seen["args"][3] == "parked"
    assert seen["kw"].get("qkind") == "needs_decision"
