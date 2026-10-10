"""Decision rubric slice 15: the bounded counter of AI-filed issues."""
import importlib.util
import json
import pathlib

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _mod():
    spec = importlib.util.spec_from_file_location("ai_filed_counter", S / "ai_filed_counter.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_defaults_and_wrong_types_read_as_defaults():
    m = _mod()
    d = m.limits({})
    assert d["max_per_goal"] == 5 and d["max_depth"] == 3 and d["on_cap"] == "park"
    bad = m.limits({"ai_filed": {"max_per_goal": "5", "max_per_run": True, "max_depth": -1, "on_cap": 3}})
    assert bad == d


def test_over_cap_counts_per_goal_and_per_run():
    m = _mod()
    cfg = {"ai_filed": {"max_per_goal": 2, "max_per_run": 3}}
    c = {"entries": [{"run": "r1", "goal": "g1", "t": 100}, {"run": "r1", "goal": "g1", "t": 101}]}
    assert m.over_cap(c, "g1", "r1", cfg) is True
    assert m.over_cap(c, "g2", "r1", cfg) is False
    c["entries"].append({"run": "r1", "goal": "g3", "t": 102})
    assert m.over_cap(c, "g2", "r1", cfg) is True      # run total reached
    assert m.over_cap(c, "g2", "r2", cfg) is False
    assert m.over_cap(c, "g2", None, cfg) is False     # unattributed run: the run cap is not applied


def test_counter_is_bounded(tmp_path):
    m = _mod()
    p = tmp_path / "state" / "c.json"
    cfg = {"ai_filed": {"counter_retention_days": 1}}
    m.bump(p, "r1", "g1", 1000.0, cfg)
    m.bump(p, "r1", "g1", 1000.0 + 3 * 86400, cfg)
    entries = m.load(p)["entries"]
    assert [e["t"] for e in entries] == [1000.0 + 3 * 86400]          # the old entry was pruned
    for i in range(m.HARD_MAX + 50):
        m.bump(p, "r", "g", 5000000.0 + i, {})
    assert len(m.load(p)["entries"]) <= m.HARD_MAX


def test_corrupt_file_fails_closed_and_is_not_overwritten(tmp_path):
    m = _mod()
    p = tmp_path / "c.json"
    p.write_text("{not json", encoding="utf-8")
    assert m.load(p) is None
    assert m.over_cap(None, "g", "r", {}) is True
    assert m.bump(p, "r", "g", 1.0, {}) is False
    assert p.read_text(encoding="utf-8") == "{not json"
    p.write_text(json.dumps([1, 2]), encoding="utf-8")
    assert m.load(p) is None


def test_missing_file_is_an_empty_counter(tmp_path):
    m = _mod()
    c = m.load(tmp_path / "none.json")
    assert c == {"entries": []} and m.over_cap(c, "g", "r", {}) is False


def test_depth_reads_the_stamp_and_never_raises():
    m = _mod()

    class Src:
        def fetch_title_body(self, goal):
            return ("t", "text\n\nsigma-depth: 2")

    class Bad:
        def fetch_title_body(self, goal):
            raise RuntimeError("x")

    assert m.ancestry_depth("g", Src()) == 2
    assert m.ancestry_depth("g", Bad()) == 0
    assert m.ancestry_depth("g", None) == 0
    assert m.stamp("body", 3).endswith("sigma-depth: 3")
