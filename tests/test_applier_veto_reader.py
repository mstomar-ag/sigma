"""Wiring of the applier veto: an owner's `no` comment on an applied ask reaches `applier.answer`
through the applier tick itself (no new daemon), and a veto parks the issue again."""
import datetime
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
T0 = datetime.datetime(2026, 10, 10, 12, 0, 0, tzinfo=datetime.timezone.utc)
MIN = datetime.timedelta(minutes=1)


def _mod(name):
    spec = importlib.util.spec_from_file_location(name + "_vr", SCRIPTS / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


AP, AU, DS, RB = _mod("applier"), _mod("autonomy"), _mod("decision_store"), _mod("rulebook")


def _cfg(enabled=True):
    return {"rulebook": {
        "records": {"enabled": True}, "rules": {"enabled": True},
        "autonomy": {"enabled": True, "owners": ["boss"],
                     "applier": {"enabled": enabled, "window_minutes": 60, "spot_check_one_in": 1000000,
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

    def _read_issue(self, goal, fields, comment_limit=None):
        return {"labels": [self.parked_label]}

    def note(self, issue, text):
        self.notes.append((issue, text))


class BoardSrc(Src):
    board_active = True
    col = {"ready": "Ready", "parked": "Parked"}

    def __init__(self):
        super().__init__()
        self.cards = []

    def _set_board_status(self, issue, status, warn=True):
        self.cards.append((issue, status))
        return True


def _applied_world(tmp_path, src=None):
    cfg, sd = _cfg(), str(tmp_path / ".sdlc")
    for _ in range(8):
        DS.append(sd, {"how": "human", "qkind": "needs_decision", "area": "docs", "choice": "merge"}, cfg)
        AU.record_watch(sd, "needs_decision", "docs", "merge", "merge", cfg, now=T0, machine="m1")
    RB.propose(sd, {"qkind": "needs_decision", "area": "docs", "option": "merge", "reason": "low risk"}, cfg, now=T0)
    RB.approve(sd, RB.load(sd, cfg)[-1]["id"], "boss", cfg, now=T0)
    AP.open_ask(sd, 7, "needs_decision", "docs", ["merge", "wait"], cfg, T0)
    src = src or Src()
    assert [a["result"] for a in AP.tick(sd, cfg, T0 + 61 * MIN, source=src)] == ["done"]
    src.swaps.clear(), src.notes.clear()
    getattr(src, "cards", []).clear()
    return sd, cfg, src


def _comment(author, body, minutes):
    return {"id": "c%s%s" % (author, minutes), "author": author, "body": body,
            "created_at": (T0 + minutes * MIN).isoformat()}


def _state(sd):
    return AP._asks(sd)[0]


def _tick(sd, cfg, src, comments, minutes=70):
    return AP.tick(sd, cfg, T0 + minutes * MIN, source=src, comments_fn=lambda issue: comments)


def test_owner_no_vetoes_and_reparks(tmp_path):
    sd, cfg, src = _applied_world(tmp_path)
    before = AU.level(sd, "needs_decision", "docs", cfg)
    acts = _tick(sd, cfg, src, [_comment("Boss", " No ", 65)])
    assert [(a["kind"], a["result"]) for a in acts] == [("veto", "done")]
    assert AU.level(sd, "needs_decision", "docs", cfg) == before - 1
    assert src.swaps == [(7, ["sdlc:parked"], ["sdlc:goal", "sdlc:in-progress"])]
    assert len(src.notes) == 1 and "parked" in src.notes[0][1].lower()
    assert _tick(sd, cfg, src, [_comment("boss", "no", 65)], minutes=80) == []  # once only


def test_veto_on_a_board_active_source_moves_the_card_back_to_parked(tmp_path):
    sd, cfg, src = _applied_world(tmp_path, BoardSrc())
    acts = _tick(sd, cfg, src, [_comment("boss", "no", 65)])
    assert [(a["kind"], a["result"]) for a in acts] == [("veto", "done")]
    assert src.cards == [(7, "Parked")]


def test_a_failed_card_move_does_not_fail_the_veto(tmp_path):
    class Stuck(BoardSrc):
        def _set_board_status(self, issue, status, warn=True):
            return status != "Parked"
    sd, cfg, src = _applied_world(tmp_path, Stuck())
    acts = _tick(sd, cfg, src, [_comment("boss", "no", 65)])
    assert [(a["kind"], a["result"]) for a in acts] == [("veto", "done")]
    assert _state(sd).get("reparked")


def test_other_text_non_owner_or_old_comment_change_nothing(tmp_path):
    sd, cfg, src = _applied_world(tmp_path)
    for c in ([_comment("boss", "no thanks", 65)], [_comment("boss", "yes", 65)],
              [_comment("stranger", "no", 65)], [_comment("boss", "no", 30)]):
        assert _tick(sd, cfg, src, c) == []
    assert _state(sd).get("vetoed_at") is None and src.swaps == [] and src.notes == []


def test_no_after_the_veto_period_changes_nothing(tmp_path):
    sd, cfg, src = _applied_world(tmp_path)
    assert _tick(sd, cfg, src, [_comment("boss", "no", 200)], minutes=300) == []
    assert _state(sd).get("vetoed_at") is None


def test_ask_not_applied_is_not_vetoed(tmp_path):
    cfg, sd = _cfg(), str(tmp_path / ".sdlc")
    AP.open_ask(sd, 7, "needs_decision", "docs", ["merge"], cfg, T0)
    AP.tick(sd, cfg, T0 + 5 * MIN, source=Src(), comments_fn=lambda i: [_comment("boss", "yes", 2)])
    assert _state(sd)["state"] == "pending" and _state(sd).get("vetoed_at") is None


def test_closed_gate_reads_nothing(tmp_path):
    sd, cfg, src = _applied_world(tmp_path)
    cfg["rulebook"]["autonomy"]["applier"]["enabled"] = False
    called = []
    assert AP.tick(sd, cfg, T0 + 70 * MIN, source=src, comments_fn=lambda i: called.append(i) or []) == []
    assert called == [] and _state(sd).get("vetoed_at") is None


def test_dry_run_does_not_record(tmp_path):
    sd, cfg, src = _applied_world(tmp_path)
    AP.tick(sd, cfg, T0 + 70 * MIN, source=src, apply_actions=False,
            comments_fn=lambda i: [_comment("boss", "no", 65)])
    assert _state(sd).get("vetoed_at") is None


def test_comment_read_failure_is_swallowed(tmp_path):
    sd, cfg, src = _applied_world(tmp_path)

    def boom(issue):
        raise RuntimeError("x")
    assert AP.tick(sd, cfg, T0 + 70 * MIN, source=src, comments_fn=boom) == []
