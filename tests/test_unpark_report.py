"""#1561: `unpark.py report <dir>` -- every parked or blocked issue with its kind, area, matching rule,
the pair's level and whether the applier would act now, later (window) or never, and why.

Driven through `unpark.main(["unpark.py", "report", <dir>])` and `unpark.report` on a temp git repo with a
fake gh; the rulebook, the pair's evidence and the asks are real files made by the real modules.
"""
import datetime, json, subprocess

import test_applier as ta
import test_unpark as tu
from test_applier import AP, AU, DS, RB, T0, MIN

_mod = tu._mod
NOW = T0 + 61 * MIN


def _cfg():
    cfg = ta._cfg()
    cfg["rulebook"]["rules"]["reask_days"] = 100000
    cfg.update(tu._config())
    return cfg


def _issue(n, labels, comments=()):
    return n, {"number": n, "title": "goal %d" % n, "body": "", "labels": [{"name": x} for x in labels]}, \
        tu._view(number=n, title="goal %d" % n, labels=labels, comments=list(comments))


def _declared(kind):
    return tu._park("stuck\n" + _mod("qkind").render_line(kind))


def _train(sd, cfg, kind, area, option):
    for _ in range(8):
        DS.append(sd, {"how": "human", "qkind": kind, "area": area, "choice": option, "answer": option}, cfg)
        AU.record_watch(sd, kind, area, option, option, cfg, now=T0, machine="m1")


def _rule(sd, cfg, kind, area, option):
    RB.propose(sd, {"qkind": kind, "area": area, "option": option, "reason": "low risk"}, cfg, now=T0)
    RB.approve(sd, RB.load(sd, cfg)[-1]["id"], "boss", cfg, now=T0)


def _world(tmp_path, extra_cfg=None):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    sd, cfg = str(tmp_path / ".sdlc"), _cfg()
    (tmp_path / ".sdlc").mkdir()
    for k, v in (extra_cfg or {}).items():
        cfg[k] = v
    _train(sd, cfg, "merge_conflict", "docs", "rebase it")      # level 3, own support, approved rule
    _rule(sd, cfg, "merge_conflict", "docs", "rebase it")
    (tmp_path / ".sdlc" / "config.json").write_text(json.dumps(cfg))
    AP.open_ask(sd, 11, "merge_conflict", "docs", ["rebase it"], cfg, T0)                 # past its window
    AP.open_ask(sd, 12, "merge_conflict", "docs", ["rebase it"], cfg, NOW - 10 * MIN)     # inside its window
    rows = [
        _issue(11, ["sdlc:parked", "area:docs"], [_declared("merge_conflict")]),
        _issue(12, ["sdlc:parked", "area:docs"], [_declared("merge_conflict")]),
        _issue(13, ["sdlc:parked"], [tu._park("waiting on design input")]),
        _issue(14, ["sdlc:parked", "area:docs"], [_declared("failing_check")]),
        _issue(15, ["sdlc:parked", "area:docs"], [_declared("irreversible")]),
        _issue(16, ["sdlc:blocked", "area:docs"], [_declared("merge_conflict")]),
        _issue(17, ["sdlc:parked", "area:web"], [_declared("merge_conflict")]),
    ]
    run = tu._runner(
        views={str(n): view for n, _, view in rows},
        by_label={"sdlc:parked": json.dumps([i for _, i, _ in rows if i["labels"][0]["name"] == "sdlc:parked"]),
                  "sdlc:blocked": json.dumps([i for _, i, _ in rows if i["labels"][0]["name"] == "sdlc:blocked"])})
    return sd, cfg, run


def _report(tmp_path, extra_cfg=None, now=NOW):
    sd, cfg, run = _world(tmp_path, extra_cfg)
    cfg_on_disk = json.loads((tmp_path / ".sdlc" / "config.json").read_text())
    out = _mod("unpark").report(sd, cfg_on_disk, run=run, now=now)
    return sd, run, {r["number"]: r for r in out["issues"]}, out


def test_mixed_parks_get_the_right_verdict_and_reason(tmp_path):
    _, _, rows, out = _report(tmp_path)
    assert rows["11"]["verdict"] == "now" and rows["11"]["gesture"] == "unpark" and rows["11"]["rule"]
    assert rows["11"]["kind"] == "merge_conflict" and rows["11"]["declared"] is True
    assert rows["11"]["area"] == "docs" and rows["11"]["level"] == 3
    assert rows["12"]["verdict"] == "later" and "window" in rows["12"]["why"]
    assert rows["13"]["verdict"] == "never" and rows["13"]["declared"] is False
    assert "not declared" in rows["13"]["why"] and rows["13"]["kind"] == "unknown"
    assert rows["14"]["verdict"] == "never" and "no approved rule" in rows["14"]["why"]
    assert rows["15"]["verdict"] == "never" and "hard-stop" in rows["15"]["why"]
    assert rows["16"]["verdict"] == "later" and "next sync" in rows["16"]["why"] and rows["16"]["state"] == "sdlc:blocked"
    assert rows["17"]["verdict"] == "never" and "no approved rule" in rows["17"]["why"] and rows["17"]["area"] == "web"
    assert out["totals"] == {"now": 1, "later": 2, "never": 4}


def test_a_pair_below_level_three_never_acts(tmp_path):
    sd, run, rows, _ = _report(tmp_path)
    cfg = json.loads((tmp_path / ".sdlc" / "config.json").read_text())
    _rule(sd, cfg, "failing_check", "docs", "fix it here")
    out = _mod("unpark").report(sd, cfg, run=run, now=NOW)
    r14 = {r["number"]: r for r in out["issues"]}["14"]
    assert r14["verdict"] == "never" and "level" in r14["why"] and r14["level"] < 3


def test_applier_off_means_never_everywhere(tmp_path):
    sd, run, _, _ = _report(tmp_path)
    cfg = json.loads((tmp_path / ".sdlc" / "config.json").read_text())
    cfg["rulebook"]["autonomy"]["applier"]["enabled"] = False
    out = _mod("unpark").report(sd, cfg, run=run, now=NOW)
    assert {r["verdict"] for r in out["issues"]} == {"never"}
    assert all("applier is off" in r["why"] for r in out["issues"])


def test_cli_prints_rows_and_one_total_per_outcome_and_json_agrees(tmp_path, capsys):
    sd, cfg, run = _world(tmp_path)
    u = _mod("unpark")
    assert u.main(["unpark.py", "report", sd], run=run) == 0
    text = capsys.readouterr().out
    lines = text.strip().splitlines()
    assert [l.split(":")[0] for l in lines[-3:]] == ["now", "later", "never"]
    assert sum(1 for l in lines if l.startswith("#")) == 7 and "#13" in text and "not declared (suggested: unknown)" in text
    assert u.main(["unpark.py", "report", sd, "--json"], run=run) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["issues"]) == 7 and sum(data["totals"].values()) == 7


def test_report_is_read_only(tmp_path):
    sd, cfg, run = _world(tmp_path)
    before = sorted((str(p), p.stat().st_mtime_ns) for p in (tmp_path / ".sdlc").rglob("*") if p.is_file())
    out = _mod("unpark").report(sd, json.loads((tmp_path / ".sdlc" / "config.json").read_text()), run=run, now=NOW)
    assert out["issues"]
    after = sorted((str(p), p.stat().st_mtime_ns) for p in (tmp_path / ".sdlc").rglob("*") if p.is_file())
    assert before == after
    writes = [c for c in run.calls if c[:2] in (["issue", "comment"], ["issue", "edit"])]
    assert writes == [] and run.rest_writes == [] and run.gql_calls == []


def test_usage_names_the_verb(capsys):
    u = _mod("unpark")
    assert u.main(["unpark.py", "--help"]) == 0
    assert "unpark.py report <sdlc_dir>" in capsys.readouterr().out


def _auto_world(tmp_path, train):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    sd, cfg = str(tmp_path / ".sdlc"), _cfg()
    (tmp_path / ".sdlc").mkdir()
    cfg["rulebook"]["rules"]["auto_approve"] = True
    cfg["rulebook"]["autonomy"]["owners"] = ["boss"]
    if train:
        _train(sd, cfg, "merge_conflict", "docs", "rebase it")
    RB.propose(sd, {"qkind": "merge_conflict", "area": "docs", "option": "rebase it", "reason": "low"}, cfg, now=T0)
    RB.approve(sd, RB.load(sd, cfg)[-1]["id"], "boss", cfg, now=T0, auto=True)
    AP.open_ask(sd, 11, "merge_conflict", "docs", ["rebase it"], cfg, T0)
    row = _issue(11, ["sdlc:parked", "area:docs"], [_declared("merge_conflict")])
    run = tu._runner(views={"11": row[2]}, by_label={"sdlc:parked": json.dumps([row[1]]), "sdlc:blocked": "[]"})
    return sd, cfg, run


def test_an_auto_approved_rule_below_level_three_without_own_support_would_act(tmp_path):
    sd, cfg, run = _auto_world(tmp_path, train=False)
    assert AP._level(sd, "merge_conflict", "docs", cfg) < 3
    r = _mod("unpark").report(sd, cfg, run=run, now=NOW)["issues"][0]
    assert r["verdict"] == "now", r


def test_a_vetoed_auto_rule_is_reported_held(tmp_path):
    sd, cfg, run = _auto_world(tmp_path, train=False)
    rid = DS.append(sd, {"how": "autonomous", "qkind": "merge_conflict", "area": "docs", "choice": "rebase it"}, cfg)
    AU.veto(sd, rid, cfg, now=T0 + MIN)
    r = _mod("unpark").report(sd, cfg, run=run, now=NOW)["issues"][0]
    assert r["verdict"] == "never" and "veto" in r["why"], r
