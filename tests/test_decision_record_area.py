"""Story 988 / goal 1133: every record writer sets an area and a declared question kind."""
import datetime, json

import test_unpark as tu
import test_promote as tp
import test_auto_unpark as ta
from test_decision_record import _mod, _on, _bodies, _ids

NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


def _recs(tmp_path, cfg):
    return _mod("decision_store").list_by(str(tmp_path), cfg)


def test_area_of_cases():
    d = _mod("decision_record")
    assert d.area_of(["sdlc:goal", "area:docs"]) == "docs"
    assert d.area_of([{"name": "area:web"}]) == "web"
    assert d.area_of(["area:b", "area:a"]) == "b"          # the first one wins
    assert d.area_of(["sdlc:goal", "areas:x", "area:"]) == "repo"
    assert d.area_of([]) == "repo" and d.area_of(None) == "repo"


def test_build_carries_area_only_when_given():
    d = _mod("decision_record")
    assert "area" not in d.build("unpark", 5, "needs_decision", "c", "r", "human")
    assert d.build("unpark", 5, "needs_decision", "c", "r", "human", area="docs")["area"] == "docs"


def test_unpark_record_has_area_and_declared_kind_and_a_rule_matches(tmp_path):
    u, rb = _mod("unpark"), _mod("rulebook")
    cfg = _on(tu._config())
    cfg["decision_rubric"]["rulebook"] = {"enabled": True}
    cfg["decision_rubric"]["autonomy"] = {"owners": ["boss"]}
    park = tu._park("x\nsigma-qkind: dependency")
    run = tu._runner(views={"5": tu._view(labels=("sdlc:parked", "area:docs"), comments=[park])})
    u.resolve(str(tmp_path), cfg, 5, {"decision": "go"}, "unpark", run=run)
    rec = _recs(tmp_path, cfg)[0]
    assert rec["area"] == "docs" and rec["qkind"] == "dependency"
    rb.propose(str(tmp_path), {"qkind": rec["qkind"], "area": "docs", "option": "go", "reason": "r"},
               cfg, now=NOW)
    rid = rb.load(str(tmp_path), cfg)[0]["id"]
    rb.approve(str(tmp_path), rid, "boss", cfg, now=NOW)
    m = rb.match(rec["qkind"], rec["area"], None, rb.load(str(tmp_path), cfg), cfg)
    assert m is not None and m.rule_id == rid


def test_unpark_without_area_label_is_repo_wide(tmp_path):
    u = _mod("unpark")
    cfg = _on(tu._config())
    run = tu._runner(views={"5": tu._view(comments=[tu._park("x")])})
    u.resolve(str(tmp_path), cfg, 5, {"decision": "go"}, "unpark", run=run)
    assert _recs(tmp_path, cfg)[0]["area"] == "repo"


def test_promote_and_demote_record_area_and_a_real_kind(tmp_path):
    p, qk = _mod("promote"), _mod("qkind")
    cfg = _on(tp._config())
    run = tp._runner(views={"5": tp._view("sdlc:needs-confirmation", "area:docs")})
    p.promote(str(tmp_path), cfg, ["5"], run=run)
    run = tp._runner(views={"6": tp._view("sdlc:goal", "area:web")})
    p.promote(str(tmp_path), cfg, ["6"], run=run, demote=True)
    recs = {r["gesture"]: r for r in _recs(tmp_path, cfg)}
    assert recs["promote"]["area"] == "docs" and recs["demote"]["area"] == "web"
    for r in recs.values():
        assert r["qkind"] in qk.QKINDS and r["qkind"] != "unknown"


def test_sweep_record_has_the_issues_area(tmp_path):
    au = _mod("auto_unpark")
    cfg = _on(ta._config())
    issue = ta._blocked_issue(42, body="blocked by #7 until the base lands",
                              labels=("sdlc:blocked", "area:docs"))
    run = ta._label_aware_sweep_runner(
        by_label={"sdlc:parked": "[]", "sdlc:blocked": json.dumps([issue])}, states={"7": "CLOSED"})
    au.sweep_unpark(str(tmp_path), cfg, apply=True, run=run)
    rec = _recs(tmp_path, cfg)[0]
    assert rec["area"] == "docs" and rec["qkind"] == "dependency"


def test_ack_record_has_a_stated_area(tmp_path):
    h = _mod("handoff")
    cfg = _on({})
    h.acknowledge(str(tmp_path), cfg, 9, "taken", why="ok", goal="g")
    assert _recs(tmp_path, cfg)[0]["area"] == "repo"
    h.acknowledge(str(tmp_path), cfg, 9, "taken", why="ok", goal="g", area="docs")
    assert sorted(r["area"] for r in _recs(tmp_path, cfg)) == ["docs", "repo"]


def test_gate_closed_is_byte_identical_and_writes_nothing(tmp_path):
    u, p = _mod("unpark"), _mod("promote")
    closed = tu._config()
    closed["decision_rubric"] = {"records": {"enabled": "yes"}}   # not the JSON boolean true
    for cfg in (tu._config(), closed):
        run = tu._runner(views={"5": tu._view(labels=("sdlc:parked", "area:docs"),
                                              comments=[tu._park("x")])})
        u.resolve(str(tmp_path), cfg, 5, {"decision": "go"}, "unpark", run=run)
        assert _bodies(run, 5) == [u.render_block({"decision": "go"})]
        run = tp._runner(views={"5": tp._view("sdlc:needs-confirmation", "area:docs")})
        p.promote(str(tmp_path), dict(cfg, **tp._config()), ["5"], run=run)
        assert _bodies(run, 5) == [p.PROMOTE_COMMENT.format(goal="sdlc:goal")]
    assert not (tmp_path / "decisions").exists()
