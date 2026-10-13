"""Goal 1178: the applier's asks are opened at park and closed at answer, through the real verbs."""
import copy
import datetime
import importlib.util
import tempfile

from test_applier import AP, T0, MIN, Src, _cfg, _setup, _sd
from test_loop import _loop, _telemetry_backlog, S
from test_unpark import _runner, _view, _park, _config as _gh_config

HOUR = datetime.timedelta(hours=1)


def _mod(name):
    spec = importlib.util.spec_from_file_location(name + "_w", S / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class ReadSrc(Src):
    """A source that can answer the one label read the open step is allowed to make."""

    def __init__(self, labels=("area:docs",)):
        super().__init__()
        self.reads, self.parked, self.labels = [], [], list(labels)

    def _read_issue(self, goal, fields, comment_limit=None):
        self.reads.append((goal, tuple(fields)))
        return {"labels": [{"name": n} for n in self.labels]}

    def park(self, goal, text):
        self.parked.append((goal, text))

    def complete(self, goal):
        pass

    def fail(self, goal, text):
        pass


def _asks(sd):
    return AP._asks(sd)


def _gh_cfg(**kw):
    cfg = _cfg(**kw)
    cfg.update(_gh_config())
    return cfg


# ----------------------------------------------------------------------------------- open

def test_a_matching_rule_opens_one_ask_with_option_and_area(tmp_path):
    sd, cfg, _ = _setup(tmp_path)
    aid = AP.open_for_park(sd, cfg, 7, "needs_decision", source=ReadSrc(), now=T0)
    asks = _asks(sd)
    assert aid and len(asks) == 1
    a = asks[0]
    assert (a["id"], a["issue"], a["area"], a["options"], a["state"]) == (aid, 7, "docs", ["merge"], "pending")


def test_no_matching_rule_opens_nothing(tmp_path):
    sd, cfg, _ = _setup(tmp_path)
    assert AP.open_for_park(sd, cfg, 7, "needs_decision", source=ReadSrc(("area:ci",)), now=T0) is None
    assert _asks(sd) == []


def test_hard_stop_and_unknown_kinds_open_nothing(tmp_path):
    sd, cfg, _ = _setup(tmp_path)
    for kind in ("irreversible", "bogus", None, ""):
        assert AP.open_for_park(sd, cfg, 7, kind, source=ReadSrc(), now=T0) is None
    assert _asks(sd) == []


def test_a_second_park_reuses_the_pending_ask(tmp_path):
    sd, cfg, _ = _setup(tmp_path)
    first = AP.open_for_park(sd, cfg, 7, "needs_decision", source=ReadSrc(), now=T0)
    again = AP.open_for_park(sd, cfg, 7, "needs_decision", source=ReadSrc(), now=T0 + MIN)
    assert first and again == first and len(_asks(sd)) == 1


def test_a_closed_gate_opens_nothing_and_reads_nothing(tmp_path):
    sd, cfg, _ = _setup(tmp_path)
    cfg["rulebook"]["autonomy"]["applier"]["enabled"] = False
    src = ReadSrc()
    assert AP.open_for_park(sd, cfg, 7, "needs_decision", source=src, now=T0) is None
    assert src.reads == [] and _asks(sd) == []
    assert AP.close_for_issue(sd, cfg, 7, "text", now=T0) is None
    assert src.reads == []


def test_a_failing_source_read_falls_back_to_the_repo_area(tmp_path):
    sd, cfg, _ = _setup(tmp_path)

    class Boom(ReadSrc):
        def _read_issue(self, *a, **k):
            raise RuntimeError("rate limited")
    assert AP.open_for_park(sd, cfg, 7, "needs_decision", source=Boom(), now=T0) is None
    assert _asks(sd) == []


# ----------------------------------------------------------------------------------- close

def _open_plain(sd, cfg, issue=5, gesture="unpark"):
    return AP.open_ask(sd, issue, "needs_decision", "docs", ["merge"], cfg, T0, gesture=gesture)


def test_unpark_closes_the_ask_as_answered(tmp_path):
    sd, cfg, _ = _setup(tmp_path)
    cfg.update(_gh_config())
    _open_plain(sd, cfg)
    run = _runner(views={"5": _view(comments=[_park("needs a call")])})
    res = _mod("unpark").resolve(sd, cfg, 5, {"decision": "start it again"}, "unpark", run=run)
    assert res["outcome"] != "failed", res
    assert [a["state"] for a in _asks(sd)] == ["answered"]


def test_keep_parked_closes_the_ask_as_answered(tmp_path):
    sd, cfg, _ = _setup(tmp_path)
    cfg.update(_gh_config())
    _open_plain(sd, cfg)
    run = _runner(views={"5": _view(comments=[_park("needs a call")])})
    res = _mod("unpark").resolve(sd, cfg, 5, {"decision": "leave it"}, "keep-parked", run=run)
    assert res["outcome"] == "kept-parked"
    assert [a["state"] for a in _asks(sd)] == ["answered"]


def test_promote_closes_the_ask(tmp_path):
    sd, cfg, _ = _setup(tmp_path)
    cfg.update(_gh_config())
    _open_plain(sd, cfg, gesture="promote")
    run = _runner(views={"5": _view(labels=("sdlc:proposed",))})
    res = _mod("promote").promote(sd, cfg, [5], run=run)
    assert [r["outcome"] for r in res["results"]] == ["promoted"], res
    assert [a["state"] for a in _asks(sd)] == ["answered"]


def test_the_appliers_own_promote_does_not_close_its_ask(tmp_path):
    sd, cfg, _ = _setup(tmp_path)
    cfg.update(_gh_config())
    _open_plain(sd, cfg, gesture="promote")
    run = _runner(views={"5": _view(labels=("sdlc:proposed",))})
    src = _mod("sources").GitHubSource(cfg, run=run)
    acts = AP.tick(sd, cfg, T0 + 2 * HOUR, source=src, run=run)
    assert [a["result"] for a in acts] == ["done"], acts
    assert [a["state"] for a in _asks(sd)] == ["applied"]


def test_an_unrelated_issue_ask_is_left_pending(tmp_path):
    sd, cfg, _ = _setup(tmp_path)
    _open_plain(sd, cfg, issue=9)
    AP.close_for_issue(sd, cfg, 5, "text", now=T0)
    assert [a["state"] for a in _asks(sd)] == ["pending"]


# ----------------------------------------------------------------------------------- real path

def _park_for_real(sd, cfg, src, goal="7", kind="needs_decision"):
    lp = _loop()
    if True:
        # the backlog set up under tmp_path IS `sd`: the park and its asks share one state folder
        lp._record(sd, src, goal, "parked", "needs a call", qkind=kind, config=cfg)


def test_record_parked_then_tick_past_the_window_applies_and_names_the_rule(tmp_path):
    _telemetry_backlog(tmp_path)
    sd, cfg, rid = _setup(tmp_path)
    src = ReadSrc()
    _park_for_real(sd, cfg, src)
    assert len(_asks(sd)) == 1
    out = AP.tick(sd, cfg, datetime.datetime.now(datetime.timezone.utc) + 2 * HOUR, source=src)
    assert [a["result"] for a in out] == ["done"]
    assert src.swaps and rid in src.notes[0][1]


def test_a_person_inside_the_window_wins(tmp_path):
    _telemetry_backlog(tmp_path)
    sd, cfg, _ = _setup(tmp_path)
    src = ReadSrc()
    _park_for_real(sd, cfg, src)
    ask = _asks(sd)[0]
    assert AP.answer(sd, ask["id"], "do it differently", cfg, T0) == "answered"
    assert AP.tick(sd, cfg, datetime.datetime.now(datetime.timezone.utc) + 2 * HOUR, source=src) == []
    assert src.swaps == []


def test_no_after_apply_is_a_veto(tmp_path):
    _telemetry_backlog(tmp_path)
    sd, cfg, _ = _setup(tmp_path)
    src = ReadSrc()
    _park_for_real(sd, cfg, src)
    later = datetime.datetime.now(datetime.timezone.utc) + 2 * HOUR
    AP.tick(sd, cfg, later, source=src)
    assert AP.answer(sd, _asks(sd)[0]["id"], "no", cfg, later) == "veto-pending"


def test_level_four_applies_too(tmp_path):
    _telemetry_backlog(tmp_path)
    sd, cfg, _ = _setup(tmp_path, level=4)
    src = ReadSrc()
    _park_for_real(sd, cfg, src)
    out = AP.tick(sd, cfg, datetime.datetime.now(datetime.timezone.utc) + 2 * HOUR, source=src)
    assert [a["result"] for a in out] == ["done"] and src.swaps and out[0]["level"] == 4


# ----------------------------------------------------------------------------------- gates and failure

def test_gate_closed_record_parked_is_byte_identical_and_reads_nothing(tmp_path):
    _telemetry_backlog(tmp_path)
    sd, cfg, _ = _setup(tmp_path)
    cfg["rulebook"]["autonomy"]["applier"]["enabled"] = False
    closed, bare = ReadSrc(), ReadSrc()
    _park_for_real(sd, cfg, closed)
    _loop()._record(sd, bare, "7", "parked", "needs a call", qkind="needs_decision")
    assert closed.parked == bare.parked and closed.reads == [] and _asks(sd) == []


def test_a_raising_applier_never_blocks_the_park(tmp_path):
    _telemetry_backlog(tmp_path)
    sd, cfg, _ = _setup(tmp_path)
    lp, src = _loop(), ReadSrc()
    real = lp._load

    def boom(name):
        if name == "applier":
            raise RuntimeError("broken")
        return real(name)
    lp._load = boom
    lp._record(sd, src, "7", "parked", "needs a call", qkind="needs_decision", config=cfg)
    assert len(src.parked) == 1


def test_a_raising_applier_never_blocks_the_unpark(tmp_path):
    sd, cfg, _ = _setup(tmp_path)
    cfg.update(_gh_config())
    u = _mod("unpark")
    real = u._load

    def boom(name):
        if name == "applier":
            raise RuntimeError("broken")
        return real(name)
    u._load = boom
    run = _runner(views={"5": _view(comments=[_park("needs a call")])})
    res = u.resolve(sd, cfg, 5, {"decision": "start it again"}, "unpark", run=run)
    assert res["outcome"] != "failed", res
