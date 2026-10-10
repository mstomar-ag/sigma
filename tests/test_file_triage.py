"""Slice 14 of the decision rubric: the pure file triage core."""
import ast
import importlib.util
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "skills" / "sigma-loop" / "scripts" / "file_triage.py"


def _mod():
    spec = importlib.util.spec_from_file_location("file_triage", SRC)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _cfg(**over):
    block = {"triage": {"enabled": True}, "buckets": {"urgent": "P2", "routine": "P3", "unclear": "P4"}}
    block.update(over)
    return {"ai_filed": block}


def test_bucket_at_or_above_human_default_closes():
    m = _mod()
    for bad in ("P1", "P0"):
        cfg = _cfg(buckets={"urgent": bad, "routine": "P3", "unclear": "P4"})
        v = m.validate_buckets(cfg)
        assert v["ok"] is False and "urgent" in v["reason"]
        d = m.decide("tidy a comment", "cleanup", {}, cfg)
        assert d.kind == "park" and "urgent" in d.reason
    assert m.validate_buckets(_cfg())["ok"] is True


def test_hard_stop_word_parks_never_arms():
    m = _mod()
    for text in ("run rm -rf on the cache", "this is irreversible", "drop table users"):
        d = m.decide("cleanup", text, {}, _cfg())
        assert d.kind == "park", text
    assert m.decide("cleanup", "tidy a comment", {}, _cfg()).kind == "arm"
    assert m.decide("cleanup", "rm -rf x", {"human_confirmed": True, "priority": "P1"}, _cfg()).kind == "park"


def test_marker_first_line_parks():
    m = _mod()
    marker = "sigma:spend-" + "approved=lab1"
    assert m.decide("cleanup", marker + "\nbody", {}, _cfg()).kind == "park"
    assert m.decide("cleanup", "body\n" + marker, {}, _cfg()).kind == "arm"


def test_never_returns_blocking():
    m = _mod()
    texts = ["", "exploit found", "cosmetic", "cleanup", "every run degrades", None, 5]
    for t in texts:
        for flags in ({}, {"bucket": "urgent"}, {"human_confirmed": True, "priority": "blocking"}, None):
            d = m.decide(t, t, flags, _cfg())
            assert "block" not in str(d).lower()
            assert d.kind in ("arm", "park", "queue")
    assert m.validate_buckets(_cfg(buckets={"urgent": "blocking", "routine": "P3", "unclear": "P4"}))["ok"] is False


def test_triage_off_returns_queue():
    m = _mod()
    d = m.decide("cleanup", "x", {}, _cfg(triage={"enabled": False}))
    assert d.kind == "queue" and d.priority is None
    assert m.decide("cleanup", "x", {}, {}).kind == "arm"  # absent block: on by default
    assert m.decide("cleanup", "x", {}, _cfg(triage={"enabled": "no"})).kind == "park"


def test_decision_is_login_free():
    m = _mod()
    a = m.decide("exploit in parser", "b", {"login": "one"}, _cfg())
    b = m.decide("exploit in parser", "b", {"login": "two"}, _cfg())
    assert a == b and a.kind == "arm" and a.priority == "P2"
    assert "login" not in SRC.read_text(encoding="utf-8").replace("login-free", "")


def test_human_confirmed_keeps_plan_priority():
    m = _mod()
    d = m.decide("t", "b", {"human_confirmed": True, "priority": "P1"}, _cfg())
    assert (d.kind, d.priority) == ("arm", "P1")
    assert m.decide("t", "b", {"human_confirmed": True, "priority": "high"}, _cfg()).kind == "park"


def test_pure_no_process_or_network_imports():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    names = {n.name.split(".")[0] for x in ast.walk(tree) if isinstance(x, ast.Import) for n in x.names}
    names |= {x.module.split(".")[0] for x in ast.walk(tree) if isinstance(x, ast.ImportFrom) and x.module}
    assert not names & {"subprocess", "socket", "urllib", "http", "requests", "os"}


def test_template_defaults_match_code():
    m = _mod()
    tmpl = json.loads((ROOT / "skills" / "sigma-init" / "templates" / "config.json.tmpl").read_text(encoding="utf-8"))
    assert "_ai_filed" in tmpl
    block = tmpl["ai_filed"]
    assert block["triage"]["enabled"] is True
    assert block["buckets"] == m.DEFAULT_BUCKETS
    assert m.validate_buckets(tmpl)["ok"] is True


_PROSE_BODIES = (
    "Found while building slice 15. It's a follow-up: the user's \"quoted\" text isn't a command. cleanup.",
    "Hand-off from the engine goal: don't re-run the doctor; it's done. The plan's step 3 is open.",
    "Decompose child: split the parser's loop. Done when the 'edge case' doesn't recur. Unbalanced \" quote.",
    "Design child: the writer's contract isn't stated; one apostrophe ' alone. follow-up polish.",
)


def test_ordinary_prose_with_odd_quotes_arms_not_parks():
    m = _mod()
    for body in _PROSE_BODIES:
        d = m.decide("Tidy the writer's docs", body, {}, _cfg())
        assert d.kind == "arm" and d.priority in ("P2", "P3", "P4"), (body, d)


def test_dangerous_command_in_prose_with_odd_apostrophe_still_parks():
    m = _mod()
    for cmd in ("git push --force origin main", "kubectl delete pod web", "git reset --hard HEAD~3"):
        d = m.decide("cleanup", "It's odd, don't run: " + cmd, {}, _cfg())
        assert d.kind == "park", cmd


def test_non_parse_cannot_tell_still_parks(monkeypatch):
    m = _mod()
    real = m._load

    def fake(name):
        if name != "hard_stop":
            return real(name)

        class H:
            @staticmethod
            def classify(text, ctx, cfg):
                return real("hard_stop").Result("cannot-tell", "detector-error", "")
        return H
    monkeypatch.setattr(m, "_load", fake)
    assert m.decide("cleanup", "plain words", {}, _cfg()).kind == "park"
