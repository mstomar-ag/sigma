"""Slice 19 of the decision rubric: the act-and-tell applier (levels 3 and 4)."""
import datetime
import importlib.util
import json
import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
T0 = datetime.datetime(2026, 10, 10, 12, 0, 0, tzinfo=datetime.timezone.utc)
MIN = datetime.timedelta(minutes=1)


def _mod(name, path=None):
    p = path or SCRIPTS / (name + ".py")
    spec = importlib.util.spec_from_file_location(name + "_t19", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


AP = _mod("applier")
AU = _mod("autonomy")
DS = _mod("decision_store")
RB = _mod("rulebook")
WD = None


def _cfg(applier=None, enabled=True, kind_levels=None):
    ap = {"enabled": enabled, "window_minutes": 60, "max_auto_applied_per_day": 3, "spot_check_one_in": 1000000}
    ap.update(applier or {})
    return {"decision_rubric": {
        "records": {"enabled": True},
        "rulebook": {"enabled": True},
        "autonomy": {"enabled": True, "owners": ["boss"], "applier": ap,
                     "levels": {"l1_min_answers": 2, "l2_min_answers": 4, "l2_agreement": 0.75,
                                "l3_min_answers": 8, "l3_agreement": 0.9,
                                "l4_min_answers": 16, "l4_agreement": 0.99}}}}


class Src:
    goal_label, parked_label, goal_blocked_label, in_progress_label = "sdlc:goal", "sdlc:parked", "sdlc:blocked", "sdlc:in-progress"
    project_enabled = False

    def __init__(self):
        self.swaps, self.notes = [], []

    def _swap_labels(self, issue, add, remove):
        self.swaps.append((issue, add, remove))

    def note(self, issue, text):
        self.notes.append((issue, text))


def _sd(tmp_path):
    return str(tmp_path / ".sdlc")


def _train(sd, cfg, qkind="needs_decision", area="docs", level=3):
    n = 16 if level >= 4 else 8
    for _ in range(n):
        DS.append(sd, {"how": "human", "qkind": qkind, "area": area, "choice": "merge"}, cfg)
        AU.record_watch(sd, qkind, area, "merge", "merge", cfg, now=T0, machine="m1")


def _rule(sd, cfg, qkind="needs_decision", area="docs", option="merge"):
    RB.propose(sd, {"qkind": qkind, "area": area, "option": option, "reason": "low risk"}, cfg, now=T0)
    rid = RB.load(sd, cfg)[-1]["id"]
    RB.approve(sd, rid, "boss", cfg, now=T0)
    return rid


def _setup(tmp_path, applier=None, level=3, qkind="needs_decision"):
    cfg = _cfg(applier)
    sd = _sd(tmp_path)
    _train(sd, cfg, qkind=qkind, level=level)
    rid = _rule(sd, cfg, qkind=qkind)
    return sd, cfg, rid


def _ask(sd, cfg, issue=7, qkind="needs_decision", opened=T0):
    return AP.open_ask(sd, issue, qkind, "docs", ["merge", "wait"], cfg, opened)


def test_window_not_elapsed_does_not_apply(tmp_path):
    sd, cfg, _ = _setup(tmp_path)
    _ask(sd, cfg)
    src = Src()
    assert AP.tick(sd, cfg, T0 + 59 * MIN, source=src) == []
    assert src.swaps == [] and src.notes == []


def test_elapsed_window_applies_and_comments_rule_id(tmp_path):
    sd, cfg, rid = _setup(tmp_path)
    _ask(sd, cfg)
    src = Src()
    acts = AP.tick(sd, cfg, T0 + 61 * MIN, source=src)
    assert [a["result"] for a in acts] == ["done"]
    assert src.swaps == [(7, ["sdlc:goal"], ["sdlc:parked", "sdlc:blocked", "sdlc:in-progress"])]
    assert len(src.notes) == 1 and rid in src.notes[0][1]
    assert AP.tick(sd, cfg, T0 + 62 * MIN, source=src) == []  # applied once only


def test_answer_inside_window_wins(tmp_path):
    sd, cfg, _ = _setup(tmp_path)
    aid = _ask(sd, cfg)
    assert AP.answer(sd, aid, "wait", cfg, T0 + 10 * MIN) == "answered"
    src = Src()
    assert AP.tick(sd, cfg, T0 + 600 * MIN, source=src) == []
    assert src.swaps == []


def test_no_after_window_is_a_veto(tmp_path):
    sd, cfg, _ = _setup(tmp_path)
    aid = _ask(sd, cfg)
    src = Src()
    AP.tick(sd, cfg, T0 + 61 * MIN, source=src)
    before = AU.level(sd, "needs_decision", "docs", cfg)
    assert before == 3
    assert AP.answer(sd, aid, "no", cfg, T0 + 70 * MIN) == "veto-pending"
    acts = AP.tick(sd, cfg, T0 + 71 * MIN, source=src)
    assert [(a["kind"], a["result"]) for a in acts] == [("veto", "done")]
    assert AU.level(sd, "needs_decision", "docs", cfg) == before - 1
    assert AP.tick(sd, cfg, T0 + 72 * MIN, source=src) == []  # logged once


def test_level_four_needs_allow_silent(tmp_path):
    sd, cfg, _ = _setup(tmp_path, level=4)
    assert AU.level(sd, "needs_decision", "docs", cfg) == 4
    _ask(sd, cfg)
    src = Src()
    AP.tick(sd, cfg, T0 + 61 * MIN, source=src)
    assert len(src.notes) == 1  # no allow_silent key: never silent
    # the same level with the kind allowed is silent (the rule is an approved sealed rule)
    sd2, cfg2, _ = _setup(tmp_path / "b", {"allow_silent": ["needs_decision"]}, level=4)
    _ask(sd2, cfg2)
    src2 = Src()
    acts = AP.tick(sd2, cfg2, T0 + 61 * MIN, source=src2)
    assert acts[0]["silent"] is True and src2.notes == [] and len(src2.swaps) == 1
    assert AP.silent_allowed("needs_decision", cfg2, {"status": "proposed"}) is False


def test_spot_check_still_tells(tmp_path):
    sd, cfg, _ = _setup(tmp_path, {"allow_silent": ["needs_decision"], "spot_check_one_in": 1}, level=4)
    _ask(sd, cfg)
    src = Src()
    AP.tick(sd, cfg, T0 + 61 * MIN, source=src)
    assert len(src.notes) == 1


def test_hard_stop_kind_never_reaches_the_tick(tmp_path):
    # the rulebook's own matcher only knows the default hard-stop kind, so a CONFIGURED hard-stop kind
    # (and a forced top level) must be stopped by the tick itself
    sd, cfg, _ = _setup(tmp_path)
    cfg["decision_rubric"]["hard_stops"] = {"hardstop_kinds": ["needs_decision"]}
    _ask(sd, cfg)
    AP.level_fn = lambda *a: 4
    try:
        src = Src()
        assert AP.tick(sd, cfg, T0 + 61 * MIN, source=src) == []
        assert src.swaps == [] and src.notes == []
    finally:
        AP.level_fn = None


def test_daily_cap_stops_the_tick(tmp_path):
    sd, cfg, _ = _setup(tmp_path, {"max_auto_applied_per_day": 2})
    for n in (1, 2, 3):
        _ask(sd, cfg, issue=n)
    src = Src()
    acts = AP.tick(sd, cfg, T0 + 61 * MIN, source=src)
    assert len(acts) == 2 and len(src.swaps) == 2
    assert AP.tick(sd, cfg, T0 + 62 * MIN, source=src) == []  # the cap holds on the next tick too
    assert len(AP.tick(sd, cfg, T0 + 61 * MIN + datetime.timedelta(hours=25), source=src)) == 1


def test_dead_tick_is_visible_by_age(tmp_path):
    cfg = _cfg({"stale_after_intervals": 3})
    cfg["ledger"] = {"watch": {"interval_seconds": 600}}
    assert AP.tick_is_dead(T0, cfg, T0 + datetime.timedelta(seconds=1800)) is False
    assert AP.tick_is_dead(T0, cfg, T0 + datetime.timedelta(seconds=1801)) is True
    assert AP.tick_is_dead(None, cfg, T0) is True
    doctor = _mod("doctor", ROOT / "skills" / "sigma-doctor" / "scripts" / "doctor.py")
    sd = _sd(tmp_path)
    os.makedirs(sd + "/autonomy")
    open(sd + "/autonomy/applier-heartbeat.json", "w").write(json.dumps({"time": T0.isoformat()}))
    assert "dead" in doctor._applier_state(cfg, sd, now=T0 + datetime.timedelta(seconds=1801))
    assert "dead" not in doctor._applier_state(cfg, sd, now=T0 + datetime.timedelta(seconds=60))
    assert doctor._applier_state(_cfg(enabled=False), sd, now=T0 + datetime.timedelta(days=9)) == "off (default)"


def test_closed_gate_writes_nothing_and_spawns_nothing(tmp_path):
    sd, cfg, _ = _setup(tmp_path)
    _ask(sd, cfg)
    closed = _cfg(enabled=False)
    before = sorted(os.listdir(sd + "/autonomy"))
    src = Src()
    assert AP.tick(sd, closed, T0 + 600 * MIN, source=src) == []
    assert sorted(os.listdir(sd + "/autonomy")) == before and src.swaps == []
    assert AP.open_ask(sd, 1, "needs_decision", "docs", ["a"], closed, T0) is None
    # the daemon step runs no child when closed
    wd = _mod("watch_daemon")
    calls = []
    wd.run_call = lambda *a, **k: calls.append(a) or ""
    wd.applier_step(None, sd, "120", config=closed)
    assert calls == []


def test_daemon_step_goes_through_the_timeout_runner(tmp_path):
    wd = _mod("watch_daemon")
    seen = []
    wd.run_call = lambda p, sdlc_dir, call_timeout, script, subcmd, mode: seen.append((script, call_timeout, mode)) or ""
    wd.touch_heartbeat = lambda p: None
    wd.applier_step(None, _sd(tmp_path), "120", config=_cfg())
    assert seen == [("applier.py", "120", "summary")]
    argv = wd.call_argv("d", "120", "applier.py", ())
    assert argv[1].endswith("run_with_timeout.py") and argv[3].endswith("applier.py")


def test_template_block_defaults():
    tmpl = (ROOT / "skills" / "sigma-init" / "templates" / "config.json.tmpl").read_text()
    block = json.loads(tmpl)["decision_rubric"]["autonomy"]["applier"]
    assert block == {"enabled": False, "allow_silent": [], "window_minutes": 1440, "spot_check_one_in": 5,
                     "max_auto_applied_per_day": 3, "stale_after_intervals": 3}
    assert AP.settings({}) == {k: v for k, v in block.items() if k != "enabled"}
