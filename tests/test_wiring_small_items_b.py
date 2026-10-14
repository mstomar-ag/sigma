"""#1403 items 5-9: small wiring fixes (ack --all scope, work.base remote prefix, board dry-run order,
rebased `why`, reconcile tick limit). One small test per item."""
import importlib.util
import pathlib

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_item5_ack_all_covers_only_the_commits_the_filed_issue_listed(tmp_path):
    import test_feature_rebase as t
    m = t._mod()
    world = t.World(tmp_path).build(feature_commits=(
        ("f.txt", "f", "feat: land a goal (#11)"),
        ("h.txt", "h", "quick fix, straight onto the branch")))
    world.move_integration()
    t._filer(m)
    first = t._upkeep(m, world)
    listed = [d["sha"] for d in first["direct"]]
    t._git(world.local, "checkout", "-q", t.FEATURE)
    t._git(world.local, "pull", "-q", "origin", t.FEATURE)
    t._write(world.local / "k.txt", "k\n")
    t._git(world.local, "add", "k.txt")
    t._git(world.local, "commit", "-q", "-m", "added after the issue was filed")
    t._git(world.local, "push", "-q", "origin", t.FEATURE)
    t._git(world.local, "checkout", "-q", t.INTEGRATION)
    got = m.ack(str(world.sdlc), t._cfg(), t.UNIT, [], all_=True)
    assert got["ok"] is True
    assert sorted(got["acked"]) == sorted(listed), got


def test_item6_work_base_accepts_a_leading_remote_name():
    work = _mod("work")
    assert work.settings({"work": {"base": "origin/main"}})["base"] == "main"
    assert work.settings({"work": {"base": "main"}})["base"] == "main"
    assert work.settings({"work": {"base": "origin/release/1", "remote": "origin"}})["base"] == "release/1"
    assert work.settings({"work": {"base": "upstream/main", "remote": "origin"}})["base"] == "upstream/main"


def test_item7_board_dry_run_lane_order_matches_the_apply_order(capsys):
    import test_board_migrate as t
    m = t._mod()
    opts = t.OPTIONS + [{"id": "o_p", "name": "Parked", "color": "GRAY", "description": ""}]
    run = t._world(options=opts)
    assert m.main(["x", "--owner", "acme", "--project", "8"], run=run) == 0
    out = capsys.readouterr().out
    assert "after:  Backlog | In Progress | Done | Ready | Parked" in out


def test_item8_a_rebased_outcome_carries_no_intermediate_conflict_text(tmp_path):
    import test_feature_rebase as t
    m = t._mod()
    world = t.World(tmp_path).build()
    world.move_integration()
    t._filer(m)
    real = m._rebase_feature

    def noisy(run, cwd, path, branch, base_ref, before, remote, report, **kw):
        out = real(run, cwd, path, branch, base_ref, before, remote, report, **kw)
        report["why"] = "CONFLICT (content): Merge conflict in a.txt"      # left by an earlier stop
        return out

    m._rebase_feature = noisy
    report = t._upkeep(m, world)
    assert report["outcome"] == m.REBASED, report
    assert report.get("why", "") == ""


def test_item9_reconcile_tick_limit_follows_a_long_watcher_interval():
    wd = _mod("watch_daemon")
    assert wd.reconcile_call_timeout("120", 120) == "240"
    assert wd.reconcile_call_timeout("120", 900) == "1800"
    assert wd.reconcile_call_timeout("120", 60) == "120"        # not above 60: unchanged
    assert wd.reconcile_call_timeout("120", None) == "120"
    assert wd.reconcile_call_timeout("500", 120) == "500"       # never lowers an operator's bound


def test_item9_tick_gives_only_the_reconcile_call_the_wider_limit(tmp_path):
    wd = _mod("watch_daemon")
    seen = {}
    wd.rotate_log = lambda *a, **k: None
    wd.log_line = lambda *a, **k: None
    wd.touch_heartbeat = lambda *a, **k: None
    wd.scheduler_step = lambda *a, **k: None
    wd.applier_step = lambda *a, **k: None
    wd.run_call = lambda p, d, timeout, script, subcmd, mode: seen.__setitem__(script, timeout) or ""
    wd.tick(tmp_path, str(tmp_path), "120", 1, {}, interval=120)
    assert seen["reconcile_tick.py"] == "240"
    assert all(v == "120" for k, v in seen.items() if k != "reconcile_tick.py")
