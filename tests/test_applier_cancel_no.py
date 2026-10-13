"""A person's `no` inside the applier window is honoured: it cancels a pending ask, and a veto is
judged by the comment's time, not by when the tick happens to run."""
import datetime
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
T0 = datetime.datetime(2026, 10, 10, 12, 0, 0, tzinfo=datetime.timezone.utc)
MIN = datetime.timedelta(minutes=1)


def _mod(name):
    spec = importlib.util.spec_from_file_location(name + "_cn", SCRIPTS / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


AP, AU, DS, RB = _mod("applier"), _mod("autonomy"), _mod("decision_store"), _mod("rulebook")


def _cfg():
    return {"rulebook": {
        "records": {"enabled": True}, "rules": {"enabled": True},
        "autonomy": {"enabled": True, "owners": ["boss"],
                     "applier": {"enabled": True, "window_minutes": 60, "spot_check_one_in": 1000000,
                                 "max_auto_applied_per_day": 3},
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


def _pending_world(tmp_path):
    cfg, sd = _cfg(), str(tmp_path / ".sdlc")
    for _ in range(8):
        DS.append(sd, {"how": "human", "qkind": "needs_decision", "area": "docs", "choice": "merge"}, cfg)
        AU.record_watch(sd, "needs_decision", "docs", "merge", "merge", cfg, now=T0, machine="m1")
    RB.propose(sd, {"qkind": "needs_decision", "area": "docs", "option": "merge", "reason": "low risk"}, cfg, now=T0)
    RB.approve(sd, RB.load(sd, cfg)[-1]["id"], "boss", cfg, now=T0)
    AP.open_ask(sd, 7, "needs_decision", "docs", ["merge", "wait"], cfg, T0)
    return sd, cfg, Src()


def _comment(author, body, minutes):
    return {"id": "c%s%s" % (author, minutes), "author": author, "body": body,
            "created_at": (T0 + minutes * MIN).isoformat()}


def _state(sd):
    return AP._asks(sd)[0]


def _tick(sd, cfg, src, comments, minutes):
    return AP.tick(sd, cfg, T0 + minutes * MIN, source=src, comments_fn=lambda issue: comments)


def test_owner_no_cancels_a_pending_ask_after_the_window(tmp_path):
    sd, cfg, src = _pending_world(tmp_path)
    assert _tick(sd, cfg, src, [_comment("Boss", " No ", 10)], 61) == []
    assert _state(sd)["state"] == "answered" and src.swaps == [] and src.notes == []


def test_owner_no_cancels_a_pending_ask_inside_the_window(tmp_path):
    sd, cfg, src = _pending_world(tmp_path)
    assert _tick(sd, cfg, src, [_comment("boss", "no", 10)], 30) == []
    assert _state(sd)["state"] == "answered"
    assert _tick(sd, cfg, src, [], 90) == []  # stays cancelled once the window has passed


def test_pending_ask_is_applied_as_before_for_other_text_non_owner_or_pre_open_no(tmp_path):
    for c in ([_comment("boss", "no thanks", 10)], [_comment("stranger", "no", 10)],
              [_comment("boss", "no", -5)]):
        sd, cfg, src = _pending_world(tmp_path / ("w%d" % id(c)))
        assert [a["result"] for a in _tick(sd, cfg, src, c, 61)] == ["done"]
        assert _state(sd)["state"] == "applied"


def test_no_exactly_at_open_time_counts(tmp_path):
    sd, cfg, src = _pending_world(tmp_path)
    assert _tick(sd, cfg, src, [_comment("boss", "no", 0)], 61) == []
    assert _state(sd)["state"] == "answered"


def test_dry_run_does_not_cancel(tmp_path):
    sd, cfg, src = _pending_world(tmp_path)
    AP.tick(sd, cfg, T0 + 61 * MIN, source=src, apply_actions=False,
            comments_fn=lambda i: [_comment("boss", "no", 10)])
    assert _state(sd)["state"] == "pending"


def _applied(tmp_path):
    sd, cfg, src = _pending_world(tmp_path)
    assert [a["result"] for a in AP.tick(sd, cfg, T0 + 61 * MIN, source=src)] == ["done"]
    src.swaps.clear(), src.notes.clear()
    return sd, cfg, src


def test_late_tick_still_records_a_no_made_inside_the_period(tmp_path):
    sd, cfg, src = _applied(tmp_path)  # applied at +61, period ends +121, grace 45 min (3 x 900 s)
    acts = _tick(sd, cfg, src, [_comment("boss", "no", 100)], 150)
    assert [(a["kind"], a["result"]) for a in acts] == [("veto", "done")]


def test_no_made_after_the_period_is_ignored_even_when_read_in_grace(tmp_path):
    sd, cfg, src = _applied(tmp_path)
    assert _tick(sd, cfg, src, [_comment("boss", "no", 130)], 150) == []
    assert _state(sd).get("vetoed_at") is None


def test_reading_stops_after_the_grace(tmp_path):
    sd, cfg, src = _applied(tmp_path)
    called = []
    AP.tick(sd, cfg, T0 + 300 * MIN, source=src, comments_fn=lambda i: called.append(i) or [_comment("boss", "no", 100)])
    assert called == [] and _state(sd).get("vetoed_at") is None


def test_closed_gate_reads_and_writes_nothing(tmp_path):
    sd, cfg, src = _pending_world(tmp_path)
    cfg["rulebook"]["autonomy"]["applier"]["enabled"] = False
    called = []
    assert AP.tick(sd, cfg, T0 + 61 * MIN, source=src,
                   comments_fn=lambda i: called.append(i) or [_comment("boss", "no", 10)]) == []
    assert called == [] and _state(sd)["state"] == "pending"
