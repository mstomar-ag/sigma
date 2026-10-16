"""#1360: a pending ask stuck past twice its window is read for owner comments at most once a window."""
import datetime
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
T0 = datetime.datetime(2026, 10, 10, 12, 0, 0, tzinfo=datetime.timezone.utc)
MIN = datetime.timedelta(minutes=1)


def _mod(name):
    spec = importlib.util.spec_from_file_location(name + "_sr", SCRIPTS / (name + ".py"))
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


def _world(tmp_path, issues=(7,)):
    cfg, sd = _cfg(), str(tmp_path / ".sdlc")
    for issue in issues:
        AP.open_ask(sd, issue, "merge_conflict", "docs", ["rebase it", "start fresh"], cfg, T0)
    return sd, cfg


def _comment(body, minutes):
    return {"id": "c%s" % minutes, "author": "boss", "body": body, "created_at": (T0 + minutes * MIN).isoformat()}


def _read(sd, cfg, minutes, comments=(), issue=None):
    """Run read_vetoes alone; returns the issues whose comments were fetched."""
    calls = []

    def fetch(i):
        calls.append(i)
        return list(comments)
    AP.read_vetoes(sd, cfg, AP._asks(sd), T0 + minutes * MIN, fetch)
    return calls


def test_stuck_ask_is_read_once_per_window(tmp_path):
    sd, cfg = _world(tmp_path)
    assert _read(sd, cfg, 130) == [7]            # older than 2 x 60 min: first read
    assert _read(sd, cfg, 150) == []             # inside a window of the last read
    assert _read(sd, cfg, 191) == [7]            # a window later: read again
    assert AP._asks(sd)[0]["veto_read_at"] == (T0 + 191 * MIN).isoformat()


def test_young_asks_are_read_every_tick(tmp_path):
    sd, cfg = _world(tmp_path)
    assert _read(sd, cfg, 30) == [7] and _read(sd, cfg, 31) == [7]
    assert _read(sd, cfg, 100) == [7] and _read(sd, cfg, 101) == [7]   # between 1 and 2 windows
    assert "veto_read_at" not in AP._asks(sd)[0]


def test_owner_no_still_cancels_a_stuck_ask(tmp_path):
    sd, cfg = _world(tmp_path)
    _read(sd, cfg, 130)
    assert _read(sd, cfg, 200, [_comment("no", 10)]) == [7]
    assert AP._asks(sd)[0]["state"] == "answered"


def test_applied_ask_veto_is_still_read(tmp_path):
    sd, cfg = _world(tmp_path)
    ask = AP._asks(sd)[0]
    ask.update(state="applied", applied_at=(T0 + 61 * MIN).isoformat())
    AP._write_atomic(AP._ask_path(sd, ask["id"]), ask)
    assert _read(sd, cfg, 100) == [7] and _read(sd, cfg, 101) == [7]


def test_five_stuck_asks_two_ticks_fetch_once_each(tmp_path):
    sd, cfg = _world(tmp_path, issues=(1, 2, 3, 4, 5))
    calls = []

    def fetch(i):
        calls.append(i)
        return []
    for minutes in (130, 135):
        AP.tick(sd, cfg, T0 + minutes * MIN, source=object(), comments_fn=fetch)
    assert sorted(calls) == [1, 2, 3, 4, 5]


def test_closed_gate_reads_and_writes_nothing(tmp_path):
    sd, cfg = _world(tmp_path)
    cfg["rulebook"]["autonomy"]["applier"]["enabled"] = False
    calls = []
    assert AP.tick(sd, cfg, T0 + 130 * MIN, source=object(), comments_fn=lambda i: calls.append(i) or []) == []
    assert calls == [] and "veto_read_at" not in AP._asks(sd)[0]
