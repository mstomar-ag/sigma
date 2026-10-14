"""#1390: `applied_at` is stamped when each ask's own gesture lands, so the veto window of a late ask in a
long tick is not already spent and an owner `no` shortly after its comment is read as a veto."""
import datetime
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
T0 = datetime.datetime(2026, 10, 10, 12, 0, 0, tzinfo=datetime.timezone.utc)
MIN = datetime.timedelta(minutes=1)


def _mod(name):
    spec = importlib.util.spec_from_file_location(name + "_as", SCRIPTS / (name + ".py"))
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

    def _swap_labels(self, issue, add, remove):
        pass

    def note(self, issue, text):
        pass


class Clock:
    """Fake monotonic: every reading is `step` seconds after the previous one."""
    def __init__(self, step):
        self.t, self.step, self.calls = 0.0, step, 0

    def __call__(self):
        self.calls += 1
        self.t += self.step
        return self.t


def _world(tmp_path, issues):
    cfg, sd = _cfg(), str(tmp_path / ".sdlc")
    for _ in range(8):
        DS.append(sd, {"how": "human", "qkind": "needs_decision", "area": "docs", "choice": "merge"}, cfg)
        AU.record_watch(sd, "needs_decision", "docs", "merge", "merge", cfg, now=T0, machine="m1")
    RB.propose(sd, {"qkind": "needs_decision", "area": "docs", "option": "merge", "reason": "low risk"}, cfg, now=T0)
    RB.approve(sd, RB.load(sd, cfg)[-1]["id"], "boss", cfg, now=T0)
    for i in issues:
        AP.open_ask(sd, i, "needs_decision", "docs", ["merge", "wait"], cfg, T0)
    return sd, cfg


def _stamps(sd):
    return {a["issue"]: datetime.datetime.fromisoformat(a["applied_at"]) for a in AP._asks(sd)}


def test_each_ask_is_stamped_from_an_advancing_clock(tmp_path):
    sd, cfg = _world(tmp_path, [7, 8, 9])
    now = T0 + 61 * MIN
    acts = AP.tick(sd, cfg, now, source=Src(), monotonic_fn=Clock(100))
    assert [a["result"] for a in acts] == ["done"] * 3
    got = sorted(_stamps(sd).values())
    assert len(set(got)) == 3 and got == sorted(got)
    assert got[0] > now and got[2] - got[0] == datetime.timedelta(seconds=200)


def test_a_no_after_a_late_stamp_is_a_veto(tmp_path):
    sd, cfg = _world(tmp_path, [7])
    now = T0 + 61 * MIN
    assert [a["result"] for a in AP.tick(sd, cfg, now, source=Src(), monotonic_fn=Clock(90 * 60))] == ["done"]
    stamp = _stamps(sd)[7]
    assert stamp - now >= 90 * MIN
    no = [{"id": "c1", "author": "boss", "body": "no", "created_at": (stamp + datetime.timedelta(seconds=10)).isoformat()}]
    acts = AP.tick(sd, cfg, now + 100 * MIN, source=Src(), comments_fn=lambda i: no)
    assert [(a["kind"], a["result"]) for a in acts] == [("veto", "done")]


def test_closed_gate_never_reads_the_clock(tmp_path):
    sd, cfg = _world(tmp_path, [7])
    cfg["rulebook"]["autonomy"]["applier"]["enabled"] = False
    clock = Clock(1)
    assert AP.tick(sd, cfg, T0 + 61 * MIN, source=Src(), monotonic_fn=clock) == []
    assert clock.calls == 0
