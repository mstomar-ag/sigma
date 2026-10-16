"""#1563: a quota park that names a reset time or retry-after records reset_at on the park and
the ask; with none named, the ask waits the applier window (no fixed day)."""
import datetime
import json

import test_applier as ta
import test_park_kinds_1498 as pk
from test_unpark import _config

AP, AU, DS = ta.AP, ta.AU, ta.DS
T0, MIN = ta.T0, ta.MIN
Q = pk._mod("qkind")
UTC = datetime.timezone.utc


# ---- the parser -------------------------------------------------------------------------------

def test_parse_reset_reads_retry_after_and_relative_and_absolute_forms():
    assert Q.parse_reset("Retry-After: 120", T0) == T0 + 120 * MIN / 60
    assert Q.parse_reset("usage limit hit, resets in 45 minutes", T0) == T0 + 45 * MIN
    assert Q.parse_reset("limit reached; resets in 2 hours", T0) == T0 + 120 * MIN
    assert Q.parse_reset("quota used. resets at 2026-10-10T15:30:00Z", T0) == T0 + 210 * MIN
    epoch = int((T0 + 90 * MIN).timestamp())
    assert Q.parse_reset("x-ratelimit-reset: %d" % epoch, T0) == T0 + 90 * MIN
    assert Q.parse_reset("rate limit exceeded, resets at %d" % epoch, T0) == T0 + 90 * MIN


def test_parse_reset_is_tolerant_and_bounded():
    for text in ("", None, 5, "no time here", "Retry-After: soon", "resets at noonish",
                 "Retry-After: 99999999999999999999", "resets at 2026-13-45T99:00:00Z",
                 "resets in 3 weeks", "resets at 2026-10-09T15:30:00Z",          # past
                 "Retry-After: 0", "Retry-After: 700000",                         # past / over 7 days
                 "x-ratelimit-reset: %d" % int((T0 - MIN).timestamp())):
        assert Q.parse_reset(text, T0) is None, text


def test_reset_line_round_trip_and_garbage():
    t = T0 + 60 * MIN
    assert Q.parse_reset_line("why\n" + Q.render_reset_line(t) + "\nsigma-qkind: quota") == t
    assert Q.parse_reset_line("sigma-reset-at: tomorrow") is None
    assert Q.parse_reset_line(None) is None


# ---- the applier: parsed reset honoured, unknown waits the window ------------------------------

def _prep(tmp_path, reset_at=None):
    cfg = ta._cfg()                                  # window_minutes 60
    sd = ta._sd(tmp_path)
    for _ in range(8):
        DS.append(sd, {"how": "human", "qkind": "quota", "area": "docs", "choice": "wait it out"}, cfg)
        AU.record_watch(sd, "quota", "docs", "wait it out", "wait it out", cfg, now=T0, machine="m1")
    ta._rule(sd, cfg, qkind="quota", option="wait it out")
    aid = AP.open_ask(sd, 7, "quota", "docs", ["wait it out"], cfg, T0, reset_at=reset_at)
    return sd, cfg, aid


def test_a_recorded_reset_holds_past_the_window_then_applies(tmp_path):
    sd, cfg, _ = _prep(tmp_path, reset_at=T0 + 180 * MIN)
    src = ta.Src()
    assert AP.tick(sd, cfg, T0 + 120 * MIN, source=src) == [] and src.swaps == []
    assert [a["result"] for a in AP.tick(sd, cfg, T0 + 181 * MIN, source=src)] == ["done"]


def test_unknown_reset_waits_the_window_not_a_day(tmp_path):
    sd, cfg, _ = _prep(tmp_path)
    src = ta.Src()
    assert AP.tick(sd, cfg, T0 + 59 * MIN, source=src) == [] and src.swaps == []
    assert [a["result"] for a in AP.tick(sd, cfg, T0 + 61 * MIN, source=src)] == ["done"]


def test_unknown_reset_follows_a_longer_configured_window(tmp_path):
    cfg = ta._cfg(applier={"window_minutes": 300})
    sd = ta._sd(tmp_path)
    for _ in range(8):
        DS.append(sd, {"how": "human", "qkind": "quota", "area": "docs", "choice": "wait it out"}, cfg)
        AU.record_watch(sd, "quota", "docs", "wait it out", "wait it out", cfg, now=T0, machine="m1")
    ta._rule(sd, cfg, qkind="quota", option="wait it out")
    AP.open_ask(sd, 7, "quota", "docs", ["wait it out"], cfg, T0)
    src = ta.Src()
    assert AP.tick(sd, cfg, T0 + 299 * MIN, source=src) == []
    assert [a["result"] for a in AP.tick(sd, cfg, T0 + 301 * MIN, source=src)] == ["done"]


# ---- the real verb path ---------------------------------------------------------------------

def _cli(monkeypatch, tmp_path, enabled, labels=("sdlc:goal", "area:docs")):
    repo = pk._Repo(labels=labels)
    cfg = _config()
    cfg.update(ta._cfg(enabled=enabled))
    (tmp_path / "sdlc").mkdir(exist_ok=True)    # the rulebook file sits in the PARENT of the state dir
    (tmp_path / "sdlc" / "config.json").write_text(json.dumps(cfg))
    lp = pk._mod("loop")
    monkeypatch.setattr(lp.sources, "get_source",
                        lambda d, c: lp.sources.GitHubSource(c, run=repo, sdlc_dir=d))
    return lp, repo, cfg


def _asks(tmp_path):
    folder = tmp_path / "sdlc" / "autonomy"
    return [json.loads(p.read_text()) for p in sorted(folder.glob("ask-*.json"))] if folder.is_dir() else []


def test_scenario_record_parked_quota_with_retry_after_then_tick(monkeypatch, tmp_path):
    lp, repo, cfg = _cli(monkeypatch, tmp_path, True)
    sd = str(tmp_path / "sdlc")
    for _ in range(8):
        DS.append(sd, {"how": "human", "qkind": "quota", "area": "docs", "choice": "wait it out"}, cfg)
        AU.record_watch(sd, "quota", "docs", "wait it out", "wait it out", cfg, now=T0, machine="m1")
    ta._rule(sd, cfg, qkind="quota", option="wait it out")
    before = datetime.datetime.now(UTC)
    assert lp.main(["loop.py", "record", sd, "5", "parked", "API rate limit hit. Retry-After: 7200",
                    "--qkind", "quota"]) == 0
    park = next(b for b in repo.bodies() if "Retry-After" in b)
    lines = park.splitlines()
    assert lines[-1] == "sigma-qkind: quota" and lines[-2].startswith("sigma-reset-at: ")
    asks = _asks(tmp_path)
    assert len(asks) == 1
    reset = datetime.datetime.fromisoformat(asks[0]["reset_at"])
    assert before + 119 * MIN <= reset <= datetime.datetime.now(UTC) + 121 * MIN
    assert Q.parse_reset_line(park) == reset
    opened = datetime.datetime.fromisoformat(asks[0]["opened"])
    src = ta.Src()
    assert AP.tick(sd, cfg, opened + 61 * MIN, source=src) == [] and src.swaps == []
    assert [a["result"] for a in AP.tick(sd, cfg, reset + MIN, source=src)] == ["done"]


def test_scenario_quota_park_without_a_reset_gets_no_reset_line_or_field(monkeypatch, tmp_path):
    lp, repo, cfg = _cli(monkeypatch, tmp_path, True)
    sd = str(tmp_path / "sdlc")
    for _ in range(8):
        DS.append(sd, {"how": "human", "qkind": "quota", "area": "docs", "choice": "wait it out"}, cfg)
        AU.record_watch(sd, "quota", "docs", "wait it out", "wait it out", cfg, now=T0, machine="m1")
    ta._rule(sd, cfg, qkind="quota", option="wait it out")
    assert lp.main(["loop.py", "record", sd, "5", "parked", "API rate limit hit", "--qkind", "quota"]) == 0
    assert all("sigma-reset-at" not in b for b in repo.bodies())
    asks = _asks(tmp_path)
    assert len(asks) == 1 and "reset_at" not in asks[0]


def test_closed_gate_and_other_kinds_keep_the_park_text_byte_identical(monkeypatch, tmp_path):
    text = "API rate limit hit. Retry-After: 7200"
    lp, repo, _ = _cli(monkeypatch, tmp_path, False)
    assert lp.main(["loop.py", "record", str(tmp_path / "sdlc"), "5", "parked", text, "--qkind", "quota"]) == 0
    closed = [b for b in repo.bodies() if "Retry-After" in b]
    assert len(closed) == 1 and closed[0].endswith(text + "\nsigma-qkind: quota")
    assert _asks(tmp_path) == []
    lp2, repo2, _ = _cli(monkeypatch, tmp_path, True)
    assert lp2.main(["loop.py", "record", str(tmp_path / "sdlc"), "5", "parked", text, "--qkind", "needs_decision"]) == 0
    other = [b for b in repo2.bodies() if "Retry-After" in b]
    assert len(other) == 1 and other[0].endswith(text + "\nsigma-qkind: needs_decision")
    assert all("sigma-reset-at" not in b for b in closed + other)


# ---- #1641: the sync read path applies the parser's far-future bound ----------------------------

class _ReadSrc:
    def __init__(self, body, created):
        self._c = [{"body": body, "createdAt": created}]

    def _read_issue(self, issue, fields):
        return {"labels": [], "comments": self._c}


def _park_body(reset):
    return "%s rate limited\n%s\nsigma-qkind: quota" % (AU.sources.PARK_COMMENT_PREFIX if hasattr(AU, "sources")
                                                          else AP._load("unpark").sources.PARK_COMMENT_PREFIX,
                                                          Q.render_reset_line(reset))


def test_sync_read_keeps_a_near_reset_and_drops_a_far_future_one():
    created = T0.isoformat()
    near = AP._park_info(_ReadSrc(_park_body(T0 + 120 * MIN), created), 5)
    far = AP._park_info(_ReadSrc(_park_body(T0 + 30 * 24 * 60 * MIN), created), 5)
    assert near["reset_at"] == T0 + 120 * MIN
    assert far["qkind"] == "quota" and far["reset_at"] is None
