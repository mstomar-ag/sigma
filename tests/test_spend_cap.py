"""Goal 1502: one shared rolling weekly and monthly spend cap read by the import classifier and the
drift judge. Stub classifier and judge only; no network, no model."""
import importlib.util
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
DAY = 86400


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


SC = _load(SCRIPTS / "spend_cap.py", "spend_cap_t1502")
DI = _load(SCRIPTS / "decision_import.py", "decision_import_t1502")
DJ = _load(SCRIPTS / "drift_judge.py", "drift_judge_t1502")
DOC = _load(ROOT / "skills" / "sigma-doctor" / "scripts" / "doctor.py", "doctor_t1502")
NOW = 1_800_000_000.0


def _cfg(weekly=None, monthly=None):
    spend = {k: v for k, v in (("weekly_usd", weekly), ("monthly_usd", monthly)) if v is not None}
    return {"rulebook": {"records": {"enabled": True, "repo_visibility": "private"},
                         "import": {"classifier": "model", "max_budget_usd_per_call": 0.5,
                                    "max_budget_usd_per_run": 5.0},
                         "drift": {"enabled": True, "model": "m", "rounds": 1,
                                   "max_budget_usd_per_call": 0.1, "spend_ceiling_usd_per_day": 50.0},
                         "spend": spend}}


def _sd(tmp_path):
    p = tmp_path / ".sdlc"
    p.mkdir(exist_ok=True)
    (p / "categories.json").write_text(json.dumps(
        {"schema": "sigma/categories@1", "categories": [{"id": "money", "areas": ["billing"]}]}))
    return str(p)


def _c(i):
    return {"source": "transcript", "when": "2026-10-01T10:%02d:00Z" % i, "question": "merge the docs change %d" % i,
            "options": ["yes", "no"], "choice": "yes", "reason": "r", "issue": 7, "id": "c%03d" % i,
            "superseded": False}


class Fake:
    def __init__(self, cost, answer):
        self.cost, self.answer, self.calls = cost, answer, []

    def __call__(self, prompt, model, max_budget_usd):
        self.calls.append(prompt)
        return dict(self.answer, cost_usd=self.cost)


CLS = {"kind": "failing_check", "area": "billing"}
JDG = {"kind": "consistent"}
LEDGER = pathlib.Path("state") / SC.LEDGER


def test_caps_unset_changes_nothing_and_creates_no_ledger(tmp_path):
    sd = _sd(tmp_path)
    cfg = _cfg()
    rows, info = DI.classify_model(sd, cfg, [_c(1), _c(2)], Fake(0.2, CLS))
    assert info["sent"] == 2 and "cap_refused" not in info
    f = Fake(0.2, JDG)
    v = DJ.judge("q", [("d", "text")], cfg, sd, call=f, now=NOW)
    assert v.kind == "consistent" and len(f.calls) == 1
    assert not (pathlib.Path(sd) / LEDGER).exists()
    for bad in (0, -1, True, "5", None, [3]):   # wrong type or non-positive reads as unset
        assert SC.caps({"rulebook": {"spend": {"weekly_usd": bad}}}) == {}
    assert SC.caps({"rulebook": {"spend": "x"}}) == {} and SC.caps(None) == {}


def test_shared_total_refuses_the_second_caller_once(tmp_path):
    sd = _sd(tmp_path)
    cfg = _cfg(weekly=0.5)
    cls = Fake(0.3, CLS)
    rows, info = DI.classify_model(sd, cfg, [_c(1), _c(2), _c(3)], cls)
    assert len(cls.calls) == 2 and info["cap_refused"]["window"] == "weekly"   # 0.3, 0.6 >= 0.5, third refused
    assert sum(1 for r in rows if r.get("skipped")) == 1
    judge = Fake(0.01, JDG)
    v = DJ.judge("q", [("d", "text")], cfg, sd, call=judge)
    assert v.kind == "not_judged" and v.reason == "spend_cap_reached" and judge.calls == []
    lines = [x for x in (pathlib.Path(sd) / LEDGER).read_text().splitlines() if x]
    assert [json.loads(x)["source"] for x in lines] == ["import", "import"]


def test_rolling_window_and_reset_time(tmp_path):
    sd = _sd(tmp_path)
    SC.append(sd, "import", 0.4, NOW - 8 * DAY)       # outside 7 days
    SC.append(sd, "judge", 0.3, NOW - 6 * DAY)
    SC.append(sd, "judge", 0.3, NOW - 1 * DAY)
    cfg = _cfg(weekly=0.5)
    r = SC.check(sd, cfg, NOW)
    assert not r["ok"] and abs(r["spent"] - 0.6) < 1e-9
    assert r["reset_at"] == NOW - 6 * DAY + 7 * DAY    # oldest in-window record ages out first
    assert SC.check(sd, _cfg(weekly=0.7), NOW)["ok"]
    assert SC.check(sd, _cfg(weekly=5, monthly=0.9), NOW)["window"] == "monthly"   # 1.0 spent in 30 days
    assert SC.check(sd, cfg, NOW + 2 * DAY)["ok"]      # 6-day-old record has aged out


def test_ledger_bounded_torn_line_ignored_and_unreadable_fails_closed(tmp_path):
    sd = _sd(tmp_path)
    path = pathlib.Path(sd) / LEDGER
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ts": NOW - 40 * DAY, "usd": 1.0, "source": "x"}) + "\n" * 1 +
                    "garbage\n" + json.dumps({"ts": NOW - DAY, "usd": 0.2, "source": "x"}) + "\n" + '{"ts": 17')
    recs, ok = SC.read(sd)
    assert ok and len(recs) == 2                       # garbage and the torn last line skipped
    assert abs(SC.status(sd, _cfg(weekly=1), NOW)[0]["spent"] - 0.2) < 1e-9
    old_bound = SC.PRUNE_BYTES
    SC.PRUNE_BYTES = 200
    try:
        for _ in range(5):
            SC.append(sd, "import", 0.01, NOW)
    finally:
        SC.PRUNE_BYTES = old_bound
    after, _ok = SC.read(sd)
    assert all(r["ts"] > NOW - 30 * DAY for r in after) and len(after) == 6   # the 40-day record is gone
    path.unlink()
    path.mkdir()                                       # a directory where the ledger should be: unreadable
    r = SC.check(sd, _cfg(weekly=1), NOW)
    assert not r["ok"] and r["reason"] == "unreadable"
    assert "cannot be read" in SC.refusal_line(r)
    assert SC.check(sd, _cfg(), NOW)["ok"]             # no cap set: never consults the ledger
    f = Fake(0.01, JDG)
    assert DJ.judge("q", [("d", "t")], _cfg(weekly=1), sd, call=f).reason == "spend_cap_reached" and f.calls == []


def test_doctor_row_shows_cap_spend_reset_and_is_absent_when_unset(tmp_path):
    sd = _sd(tmp_path)
    import time
    now = time.time()
    SC.append(sd, "import", 0.6, now - DAY)
    out = DOC._rulebook_state_core(_cfg(weekly=1.0), sd)
    assert "spend cap weekly" in out and "$0.60 of $1.00" in out and "resets" not in out   # under cap: no reset time yet
    SC.append(sd, "import", 0.6, now - 2 * DAY)
    out = DOC._rulebook_state_core(_cfg(weekly=1.0), sd)
    assert "$1.20 of $1.00" in out and "resets" in out
    assert "spend cap" not in DOC._rulebook_state_core(_cfg(), sd)


def test_apply_verb_then_judge_share_the_cap(tmp_path, capsys, monkeypatch):
    """The gesture the docs give: `decision_import.py apply <sdlc>`, a stubbed classifier, then a judge call."""
    sdlc = tmp_path / "repo" / ".sdlc"
    sdlc.mkdir(parents=True)
    (sdlc / "categories.json").write_text(json.dumps(
        {"schema": "sigma/categories@1", "categories": [{"id": "money", "areas": ["billing"]}]}))
    (sdlc / "config.json").write_text(json.dumps(_cfg(weekly=0.5)))
    cls = Fake(0.3, CLS)
    monkeypatch.setattr(DI, "scan", lambda *a, **k: {"candidates": [_c(1), _c(2), _c(3)]})
    monkeypatch.setattr(DI, "default_call", lambda p, m, b, timeout_s=60: cls(p, m, b))
    rc = DI.main(["apply", str(sdlc), "--profile", str(tmp_path / "none")])
    out = capsys.readouterr().out
    assert rc == 0 and len(cls.calls) == 2, out
    assert out.count("spend cap reached (weekly $0.60 of $0.50, resets ") == 1
    judge = Fake(0.01, JDG)
    v = DJ.judge("q", [("d", "text")], json.loads((sdlc / "config.json").read_text()), str(sdlc), call=judge)
    assert v.reason == "spend_cap_reached" and judge.calls == []
