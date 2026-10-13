"""#1344: five small upkeep fixes. Each test was seen red before its fix (docs wording, Windows refusal, lease word,
the agent-level doctor row, no backup ref for a pure fast-forward)."""
import copy
import json
import pathlib

import backup_support as bk
import test_doctor_resolver_readiness as DR
import test_upkeep_sched as T

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_docs_say_external_merges_wait_for_the_watcher_tick():
    text = (ROOT / "docs" / "upkeep-part-b.md").read_text(encoding="utf-8")
    text = " ".join(text.split())
    assert "next watcher tick" in text and "only `work.py merge` starts" in text


def test_the_job_run_directly_on_windows_refuses_loudly(tmp_path, monkeypatch, capsys):
    mod = T.job()
    monkeypatch.setitem(mod.main.__globals__, "_windows", lambda: True)
    sdlc = T.project(tmp_path)
    code = mod.main(["--sdlc", str(sdlc), "--root", str(tmp_path), "--unit", "u", "--cap", "5"])
    assert code != 0 and "Windows" in capsys.readouterr().err


def test_a_lease_refusal_in_the_unit_pass_is_recorded_as_lease_refused(tmp_path, capsys):
    mod = T.job()
    cfg = copy.deepcopy(T.open_config())
    refused = lambda *a, **k: {"result": "failed", "reason": "lease-refused"}      # noqa: E731
    quiet = lambda *a, **k: {"outcome": "current"}                                 # noqa: E731
    line = mod.run_engine(cfg, T.project(tmp_path), str(tmp_path), "voice", environ=T.ENV, upkeep=quiet, unit_pass=refused)
    assert line["outcome"] == "lease-refused", line
    other = lambda *a, **k: {"result": "failed", "reason": "rewrite-failed"}       # noqa: E731
    assert mod.run_engine(cfg, T.project(tmp_path), str(tmp_path), "voice", environ=T.ENV, upkeep=quiet,
                          unit_pass=other)["outcome"] == "current"
    capsys.readouterr()


def test_the_pass_reports_a_refused_lease_as_its_reason(monkeypatch):
    mod = T.S.script("feature_upkeep_pass")
    a, b = "a" * 40, "b" * 40
    monkeypatch.setattr(mod, "_current", lambda *x: False)
    run = lambda cwd, argv: a if "unit" in argv[-1] else b                          # noqa: E731

    def push(_run, tips):
        raise mod.LeaseRefused("lease-refused")
    report = {"unit_tip": a, "base_tip": b}
    mod._engine({}, ".", "u", "unit", "base", run, ".", report, lambda r, t: {"ok": True}, push, None, None, lambda p: run)
    assert report["result"] == "failed" and report["reason"] == "lease-refused", report


def test_doctor_says_the_agent_level_is_not_available_yet(tmp_path):
    d, cfg = DR.doctor(), DR.config()
    found = DR.rows(d, DR.project(tmp_path, cfg), cfg)
    assert any("not available yet" in (c["name"] + c.get("fix", "")).lower() for c in found), DR.names(found)


def test_a_pure_fast_forward_keeps_no_backup_ref_but_keeps_the_lease(tmp_path):
    fb = bk.module()
    root = tmp_path / "w"
    root.mkdir()
    world = bk.World(root).build()
    old = world.tip()
    world.commit("more.txt", "more")                    # old tip is an ancestor of HEAD
    rec = bk.Recorder()
    outcome, report = bk.pushed(world, old, rec)
    assert outcome == "rebased" and not report.get("backup"), (outcome, report)
    assert world.tip() == world.head() and [r for r in world.refs() if r.startswith("refs/sigma/")] == []
    push = rec.pushes()[-1]
    assert "--force-with-lease=refs/heads/feature/u:" + old in push and not any("backup" in a for a in push), push
    w2 = bk.World(tmp_path / "w2")
    (tmp_path / "w2").mkdir()
    w2.build()
    seed = w2.tip()
    w2.commit("x.txt", "x")
    w2.push_raw(w2.head(), "refs/heads/feature/u")       # somebody else moved the branch: the lease must still refuse
    theirs = w2.tip()
    w2.amend()
    out, _ = bk.pushed(w2, seed, bk.Recorder())
    assert out == "lease-refused" and w2.tip() == theirs, out
    # a rewrite (not a fast-forward) still keeps its backup
    w3 = bk.World(tmp_path / "w3")
    (tmp_path / "w3").mkdir()
    w3.build()
    w3.commit("y.txt", "y")
    w3.push_raw(w3.head(), "refs/heads/feature/u")
    o3 = w3.tip()
    w3.amend()
    out3, rep3 = bk.pushed(w3, o3, bk.Recorder())
    assert out3 == "rebased" and rep3["backup"].startswith("refs/sigma/backup/u/"), rep3
