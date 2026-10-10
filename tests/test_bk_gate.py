"""Slice 5: the gate around the backup code, the engine's wiring of it, the verbs, and the pins, inventory and prose
that must change with it.

PLANNED TESTS of the pull-request red-to-green gate; see `test_bk_name.py` for how they are built. Two functions here
are NOT planned (`test_legacy_unit_push_is_unchanged`, `test_nothing_else_in_the_engine_changed_shape`): they are green
on an untouched tree and stay green. The pass is driven on the engine suite's own real-git World, which also gets
round the pick-cost helper whose stub cannot reach the push (that helper is neither used nor edited)."""
import ast
import contextlib
import importlib.util
import io
import json
import re
import subprocess
import sys
import time

import backup_support as bk

ROOT = bk.ROOT
UNIT = "u"
BACKUP = "skills/sigma-loop/scripts/feature_backup.py"
OLD_SIX = {
    ("skills/sigma-loop/scripts/work.py", "finish", "branch_dD"),
    ("skills/sigma-loop/scripts/work.py", "_delete_remote_branch", "rest_delete_ref"),
    ("skills/sigma-loop/scripts/gh_api.py", "remove_label", "rest_delete"),
    ("skills/sigma-define/scripts/define.py", "_step_branch", "colon_refspec"),
    ("skills/sigma-loop/scripts/feature_rebase.py", "_pushed", "colon_refspec"),
    ("skills/sigma-loop/scripts/release_manifest.py", "publish_to_ledger_branch", "colon_refspec"),
}


def _load_path(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _guard():
    return _load_path("backup_guard_under_test", ROOT / "tests" / "test_no_autonomous_feature_branch_deletion.py")


def _quiet(fn, *args):
    """-> (result, stdout, stderr) with nothing reaching pytest's capture."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        result = fn(*args)
    return result, out.getvalue(), err.getvalue()


def test_verbs_closed_and_footprintless(tmp_path, monkeypatch):
    """The two verbs ask the gate first: under every near-enabled configuration each costs no process, model, network,
    write or file; with that first query removed the trap and the static guard both notice. No function is a decorated
    entry point (that would need registering, which rewrites two slice-1 pins; the pass is registered by the slice
    that builds it)."""
    fb = bk.module()
    support = bk.support
    text = bk.MODULE.read_text(encoding="utf-8")
    assert "@feature_upkeep" not in text and support.entry_point_offenders(text, set()) == []
    assert "feature_backup" not in support.REGISTERED_ENTRY_POINTS, "registration belongs to the slice that builds the pass"
    import test_upkeep_proof as proof
    cases = [(name, config, None) for name, config in proof.project_cases()]
    assert len(cases) >= 20
    drivers = {"restore_backup": lambda c, s, e: fb.restore_backup(c, s, "billing", "20260101T000000Z", "0" * 40, remote="origin"),
               "prune_backups": lambda c, s, e: fb.prune_backups(c, s, remote="origin")}
    for name, driver in drivers.items():
        found = proof.offences(tmp_path / name, monkeypatch, driver, cases)
        assert found == [], (name, found[:3])
    query = "    closed = _closed(config)\n    if closed is not None:\n        return closed\n"
    assert text.count(query) == 2
    loose = bk.variant(query, "")
    _result, trap, _diff, _project = support.run_case(
        tmp_path / "control", monkeypatch, lambda c, s, e: loose.prune_backups(c, s, remote="origin"), {})
    assert trap.processes, "an ungated prune must be seen starting a process"


def test_open_gate_runs_the_verbs():
    """An enabled gate runs each verb; every other reading closes it; the numbers come from the gate's settings."""
    fb = bk.module()
    calls = []

    def run(cwd, argv):
        calls.append([str(a) for a in argv])
        return ""
    closed = [{}, {"upkeep": {"enabled": False}}, {"upkeep": {"enabled": "true"}}, {"upkeep": {"enabled": 1}},
              {"upkeep": {"enabled": True, "auto": {"floor": 99, "ceiling": 3}}}, {"upkeep": []}]
    for config in closed:
        for out in (fb.restore_backup(config, "x/.sdlc", UNIT, None, None, remote="origin", run=run, list_only=True),
                    fb.prune_backups(config, "x/.sdlc", remote="origin", run=run)):
            assert out.get("closed") is True and "outcome" not in out, (config, out)
    assert calls == [], "a closed gate must cost no command: %r" % (calls,)
    assert fb.restore_backup(bk.OPEN, "x/.sdlc", UNIT, None, None, remote="origin", run=run, list_only=True)["outcome"] == fb.LISTED
    assert fb.prune_backups(bk.OPEN, "x/.sdlc", remote="origin", run=run, clock=bk.CLOCK)["outcome"] == fb.NOTHING
    assert calls and [c[:2] for c in calls].count(["git", "push"]) == 0
    refs = {"refs/sigma/backup/u/" + bk.stamp_days_ago(d): "%040x" % (d + 1) for d in (0, 2, 3)}
    default = fb.prune_backups(bk.OPEN, "x/.sdlc", remote="origin", run=bk.FakeRemote(refs), clock=bk.CLOCK)
    assert default["outcome"] == fb.NOTHING and default["kept"] == 3, default
    fake = bk.FakeRemote(refs)
    tuned = fb.prune_backups(bk.config(keep_days=1, keep_last=1), "x/.sdlc", remote="origin", run=fake, clock=bk.CLOCK)
    assert tuned["deleted"] == 2 and sorted(fake.refs) == ["refs/sigma/backup/u/" + bk.stamp_days_ago(0)], tuned
    for clock in (bk.CLOCK, lambda: bk.CLOCK, float(bk.CLOCK)):
        again = bk.FakeRemote(refs)
        assert fb.prune_backups(bk.config(keep_days=1, keep_last=1), "x/.sdlc", remote="origin", run=again,
                                clock=clock)["deleted"] == 2, clock


def test_gate_knows_former_prefixes():
    """The gate reads `backup.former_prefixes` (empty by default); a wrong type closes the block; the template carries it."""
    support = bk.support
    gate = support.gate()
    spec = gate.SCHEMA.get("backup.former_prefixes")
    assert spec == {"kind": "name_list", "min_items": 0}, "the gate has no backup.former_prefixes key: %r" % (spec,)
    assert gate.DEFAULTS["backup.former_prefixes"] == []
    cfg = support.template_cfg()
    assert cfg["upkeep"]["backup"]["former_prefixes"] == [], "the template must ship the key empty"
    assert support.key_gaps(cfg["upkeep"], gate.SCHEMA) == ([], [])
    shipped = gate.read(cfg)
    assert shipped.enabled is False and shipped.problems == () and shipped.settings["backup.former_prefixes"] == []
    on = {"upkeep": {"enabled": True, "backup": {"former_prefixes": ["refs/x/backup/"]}}}
    reading = gate.read(on)
    assert reading.enabled is True and reading.settings["backup.former_prefixes"] == ["refs/x/backup/"], reading
    for bad in ("refs/x/backup/", 5, [1], [""], [None], {"a": 1}, ["a"] * 201):
        got = gate.read({"upkeep": {"enabled": True, "backup": {"former_prefixes": bad}}})
        assert got.enabled is False and any("upkeep.backup.former_prefixes" in p for p in got.problems), (bad, got.problems)
        assert got.settings["backup.former_prefixes"] == [] and "refs/x" not in " ".join(got.problems)


def test_cli_verbs(tmp_path, monkeypatch):
    """`restore` and `prune` parse strictly, exit 3 while the gate is closed, and work end to end on a real remote."""
    fb = bk.module()
    m = bk.support.script("feature_rebase")
    for word in ("restore", "prune", "--list", "--expect", "--dry-run"):
        assert word in m.USAGE, word
    _code, out, _err = _quiet(m.main, ["feature_rebase.py", "--help"])
    assert "restore" in out and "prune" in out
    root = tmp_path / "w"
    root.mkdir()
    world = bk.World(root).build()
    world.commit("second.txt", "second")
    world.push_raw(world.head(), "refs/heads/feature/u")
    old = world.tip()
    world.amend()
    sdlc = str(world.sdlc)
    world.sdlc.mkdir()

    def configure(config):
        (world.sdlc / "config.json").write_text(json.dumps(config), encoding="utf-8")
    spawned = []
    real_run = subprocess.run
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: spawned.append(a) or real_run(*a, **k))
    configure({})
    for argv in (["restore", sdlc, UNIT, "--list"], ["restore", sdlc, UNIT, "20260921T141320Z", "--expect", "1" * 40],
                 ["prune", sdlc], ["prune", sdlc, "--dry-run"]):
        code, out, err = _quiet(m.main, ["feature_rebase.py"] + argv)
        assert code == m.CLOSED_EXIT == 3 and json.loads(out)["closed"] is True, (argv, code, out)
        assert "closed" in err and ":refs/" not in err
    assert spawned == [], "a closed gate must start no process: %r" % (spawned,)
    configure(bk.OPEN)
    for argv in (["restore", sdlc, UNIT], ["restore", sdlc, UNIT, "20260921T141320Z"], ["restore", sdlc, UNIT, "--list", "x"],
                 ["restore", sdlc, UNIT, "x", "--expect"], ["restore", sdlc, UNIT, "--list", "--expect", "1" * 40],
                 ["prune", sdlc, "--force"], ["prune", sdlc, "extra"], ["prune", sdlc, "--dry-run", "--list"]):
        code, out, err = _quiet(m.main, ["feature_rebase.py"] + argv)
        assert code == 2 and out == "" and "usage" in err, (argv, code, out, err)
    assert spawned == [], "a usage error must start no process"
    monkeypatch.setattr(subprocess, "run", real_run)
    long_unit = "a" * 221
    code, out, _err = _quiet(m.main, ["feature_rebase.py", "restore", sdlc, long_unit, "--list"])
    assert code == 1 and json.loads(out)["outcome"] == fb.TOO_LONG, (code, out)
    now = time.time()
    first = now - 5 * bk.DAY
    push = fb.push_unit(bk.runner, str(world.local), str(world.local), "origin", UNIT, old, clock=first)
    assert push["outcome"] == fb.PUSHED, push
    produced, stamp = world.tip(), fb.stamp_of(first)
    code, out, _err = _quiet(m.main, ["feature_rebase.py", "restore", sdlc, UNIT, "--list"])
    listed = json.loads(out)
    assert code == 0 and listed["branch_tip"] == produced and listed["backups"][0]["stamp"] == stamp, (code, listed)
    if m.sync.fcntl is not None:
        held = m._acquire(m.lock_path(sdlc, UNIT), timeout=0.0)
        busy_code, busy_out, _e = _quiet(m.main, ["feature_rebase.py", "restore", sdlc, UNIT, stamp, "--expect", produced])
        m._release(held)
        assert busy_code == 1 and json.loads(busy_out)["outcome"] == fb.BUSY and world.tip() == produced, busy_out
    for days in range(20, 27):
        world.push_raw(old, "refs/sigma/backup/%s/%s" % (UNIT, fb.stamp_of(now - days * bk.DAY)))
    code, out, _err = _quiet(m.main, ["feature_rebase.py", "prune", sdlc, "--dry-run"])
    plan = json.loads(out)
    assert code == 0 and plan["outcome"] == fb.DRY_RUN and len(plan["would_delete"]) == 3, plan
    assert len([r for r in bk.remote_refs(world.local) if r.startswith(fb.PREFIX)]) == 8
    code, out, _err = _quiet(m.main, ["feature_rebase.py", "prune", sdlc])
    done = json.loads(out)
    assert code == 0 and done["outcome"] == fb.PRUNED and done["deleted"] == 3, done
    code, out, _err = _quiet(m.main, ["feature_rebase.py", "restore", sdlc, UNIT, stamp, "--expect", produced])
    result = json.loads(out)
    assert code == 0 and result["outcome"] == fb.RESTORED and world.tip() == old, (code, result)


def test_guard_pin_and_inventory(tmp_path):
    """The guard sees exactly the two new sites, in the forms it can read; the inventory and the documented gesture agree."""
    bk.module()
    guard = _guard()
    hits = guard._delete_call_sites(ROOT)
    assert hits is not None, "not a git checkout"
    found = {(str(p.relative_to(ROOT)), guard._enclosing_function(p, n), kind) for p, n, kind, _l in hits}
    mine = {t for t in found if t[0] == BACKUP}
    assert mine == {(BACKUP, "_atomic_leased_push", "colon_refspec"), (BACKUP, "_delete_chunk", "push_delete")}, mine
    assert OLD_SIX <= found, sorted(OLD_SIX - found)
    pin = (ROOT / "tests" / "test_no_autonomous_feature_branch_deletion.py").read_text(encoding="utf-8")
    assert all('"%s", "%s", "%s"' % t in pin for t in mine), "the guard's own pin must list the two new sites"
    lines = [(kind, line) for p, _n, kind, line in hits if str(p.relative_to(ROOT)) == BACKUP]
    assert sorted(k for k, _l in lines) == ["colon_refspec", "colon_refspec", "push_delete"], lines
    assert all('"%s:refs/' in l for k, l in lines if k == "colon_refspec"), lines
    assert sum('"--delete"' in l for _k, l in lines) == 1
    text = bk.MODULE.read_text(encoding="utf-8")
    leading = [n.value for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and n.value[:1] in (":", "+")]
    assert leading == [], "no colon-empty or plus refspec may be built in the module: %r" % (leading,)
    assert not [l for l in guard._python_scannable_lines(text) if "--atomic" in l and "--delete" in l]
    surface = _load_path("backup_write_surface", ROOT / "tools" / "readiness" / "write_surface.py")
    inventory = ROOT / "docs" / "launch" / "write-surface.json"
    rows = [e for e in json.loads(inventory.read_text(encoding="utf-8"))["entries"] if e["path"] == BACKUP]
    assert sorted((e["function"], e["rule"], e["count"], e["risk"]) for e in rows) == [
        ("_atomic_leased_push", "git-destructive", 1, "high"), ("_atomic_leased_push", "git-push", 1, "high"),
        ("_delete_chunk", "git-destructive", 1, "high"), ("_delete_chunk", "git-push", 1, "high")], rows
    assert all("upkeep.enabled" in e["gate"] for e in rows), rows
    scanned = [r for r in surface.scan_paths(ROOT, [bk.MODULE])]
    assert sorted((r["function"], r["rule"], r["gate"]) for r in scanned) == sorted(
        (e["function"], e["rule"], e["gate"]) for e in rows), "the module must write nothing locally, and the inventory must equal a scan"
    gesture = [sys.executable, "tools/readiness/write_surface.py", "check", ".", "docs/launch/write-surface.json"]
    done = subprocess.run(gesture, cwd=str(ROOT), capture_output=True, text=True, timeout=300)
    assert done.returncode == 0 and done.stdout.strip() == "", (done.returncode, done.stdout, done.stderr)
    data = json.loads(inventory.read_text(encoding="utf-8"))
    data["entries"] = [e for e in data["entries"] if not (e["path"] == BACKUP and e["function"] == "_delete_chunk")]
    cut = tmp_path / "inventory.json"
    cut.write_text(json.dumps(data), encoding="utf-8")
    broken = subprocess.run(gesture[:-1] + [str(cut)], cwd=str(ROOT), capture_output=True, text=True, timeout=300)
    assert broken.returncode == 1 and "new write site" in broken.stdout and "_delete_chunk" in broken.stdout, broken.stdout


def test_rule_text_and_carve_out():
    """The pinned sentence lives in the branching model and is listed; the carve-out is in the three unmeasured places only."""
    bk.module()
    guard = _guard()
    listed = getattr(guard, "_FILES_STATING_THE_RULE", frozenset())
    assert "docs/branching-model.md" in listed, "list the branching model in _FILES_STATING_THE_RULE"
    found = guard._files_carrying_the_rule_wording(guard.ROOT)
    assert found.get("docs/branching-model.md") == "exact" and not [p for p in found if p not in listed], found
    doc = (ROOT / "docs" / "branching-model.md").read_text(encoding="utf-8")
    for needle in ("### 13c.", "The one exception: backup refs", "_atomic_leased_push", "_delete_chunk", "--expect",
                   "backup.former_prefixes", "220 bytes", "exit 3", "name-too-long", "pick-time rebase pass"):
        assert needle in doc, "docs/branching-model.md lacks %r" % needle
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "lease-protected force-push of a unit branch" in readme and "docs/branching-model.md" in readme
    assert "prune of backup refs" in (ROOT / "docs" / "enforcement.md").read_text(encoding="utf-8")
    table = (ROOT / "skills" / "sigma-doctor" / "scripts" / "enforcement_table.py").read_text(encoding="utf-8")
    assert "backup refs (once upkeep is enabled) are outside it" in table
    budget = _load_path("backup_phase_budget", ROOT / "evals" / "phase_context_budget.py")
    measured = []
    for phase in budget.PHASES.values():
        for rel in phase["files"]:
            body = (ROOT / rel).read_text(encoding="utf-8")
            if "refs/sigma/backup" in body or "feature_backup" in body:
                measured.append(rel)
    assert measured == [], "the carve-out must stay out of the measured phase payloads: %r" % measured
    note = bk.support.template_cfg()["_upkeep"]
    assert "NOTHING RUNS FROM THIS BLOCK" not in note and "no feature calls yet" not in note, "the template note still says nothing runs"
    assert not note.startswith("RESERVED") and "NOTHING ELSE RUNS FROM THIS BLOCK YET" in note
    assert "feature_rebase.py restore" in note and "feature_rebase.py prune" in note and "backup.former_prefixes" in note
    assert "pick-time rebase pass" in note and "too long" in note
    gate = (ROOT / "skills" / "sigma-loop" / "scripts" / "feature_upkeep.py").read_text(encoding="utf-8")
    assert "nothing in the product calls it yet" not in gate and "feature_backup.py" in gate
    entries = json.loads((ROOT / "docs" / "launch" / "dispositions" / "921.json").read_text(encoding="utf-8"))
    assert len(entries) == 1 and entries[0]["pattern"] == "refs/sigma/backup/<unit>/<stamp>", entries
    assert entries[0]["unscanned"] is True and entries[0]["issue"] == "#921" and "feature_backup.py" in entries[0]["evidence"]
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").split("## 1.0.4")[0]
    assert "(#921, upkeep part A, slice 5)" in changelog


def _pass(tmp, name, block, wall=None, setup=None, race=None):
    """One real pass over the engine suite's own World: the feature is behind, so the pass replays and pushes.
    `setup(world, tip)` runs before the pass; `race(world, argv)` runs just before the first push.
    -> (world, report, every command run, the tip before, engine module, engine tests)."""
    import test_feature_rebase as eng
    root = tmp / name
    root.mkdir(parents=True)
    world = eng.World(root).build()
    world.move_integration()
    m = eng._mod()
    m._WALL = wall
    config = eng._cfg()
    if block is not None:
        config["upkeep"] = block
    calls, real, raced = [], m._run, []

    def watch(cwd, argv):
        argv = [str(a) for a in argv]
        if race is not None and argv[:2] == ["git", "push"] and not raced:
            raced.append(race(world, argv))
        calls.append(argv)
        return real(cwd, argv)
    m._run = watch
    before = world.tip(eng.FEATURE)
    if setup is not None:
        setup(world, before)
    return world, m.upkeep(str(world.sdlc), config, "7", eng.UNIT), calls, before, m, eng


def test_pass_pushes_legacy_or_atomic(tmp_path):
    """The pass reaches the push for real. Gate closed in six ways: the argv is the legacy bytes, no backup ref, no report
    key. Gate open: the same commands with only the push different, and that push is ONE atomic push of the backup and the
    leased update; a branch that moves under it refuses both, and a taken backup name refuses loudly."""
    fb = bk.module()
    subcommands = None
    for n, block in enumerate((None, {"enabled": False}, {"enabled": "true"}, {"enabled": 1}, 7,
                               {"enabled": True, "auto": {"floor": 99, "ceiling": 3}})):
        world, report, calls, before, m, eng = _pass(tmp_path, "closed%d" % n, block)
        pushes = [c for c in calls if c[:2] == ["git", "push"]]
        assert report["outcome"] == m.REBASED, (block, report)
        assert pushes == [["git", "push", "--force-with-lease=feature/billing:" + before, "origin",
                           "HEAD:refs/heads/feature/billing"]], (block, pushes)
        assert "backup" not in report and [r for r in bk.remote_refs(world.local) if r.startswith("refs/sigma/")] == [], (block, report)
        subcommands = [c[1] for c in calls]
    world, report, calls, before, m, eng = _pass(tmp_path, "open", {"enabled": True}, wall=bk.CLOCK)
    backup = "refs/sigma/backup/billing/" + fb.stamp_of(bk.CLOCK)
    pushes = [c for c in calls if c[:2] == ["git", "push"]]
    assert report["outcome"] == m.REBASED and report["backup"] == backup, report
    assert pushes == [["git", "push", "--atomic", "--force-with-lease=refs/heads/feature/billing:" + before,
                       "--force-with-lease=" + backup + ":", "origin", "HEAD:refs/heads/feature/billing",
                       before + ":" + backup]], pushes
    refs = bk.remote_refs(world.local)
    assert refs[backup] == before and refs["refs/heads/feature/billing"] == report["after"] != before, refs
    assert [c[1] for c in calls] == subcommands, "the open path must run the same commands as the closed one"

    def other_writer(world, argv):
        racer = world.root / "racer"
        bk.git(world.root, "clone", "-q", "--no-local", str(world.remote), str(racer))
        bk.git(racer, "checkout", "-q", "-b", "w", "origin/feature/billing")
        (racer / "racer.txt").write_text("racer\n", encoding="utf-8")
        bk.git(racer, "add", "racer.txt")
        bk.git(racer, "commit", "-q", "-m", "racer")
        bk.git(racer, "push", "-q", "origin", "w:refs/heads/feature/billing")
        return bk.git(racer, "rev-parse", "HEAD")
    world, report, calls, before, m, eng = _pass(tmp_path, "stale", {"enabled": True}, wall=bk.CLOCK, race=other_writer)
    refs = bk.remote_refs(world.local)
    assert report["outcome"] == m.LEASE_REFUSED and refs["refs/heads/feature/billing"] == report["tip"] != before, report
    assert [r for r in refs if r.startswith("refs/sigma/")] == [], "a refused lease must leave no backup"

    def occupy(world, tip):
        stray = bk.git(world.local, "commit-tree", "-m", "stray", tip + "^{tree}")
        bk.git(world.local, "push", "-q", "origin", "%s:%s" % (stray, backup))
    world, report, calls, before, m, eng = _pass(tmp_path, "taken", {"enabled": True}, wall=bk.CLOCK, setup=occupy)
    assert report["outcome"] == m.FAILED and "already taken" in report["why"] and backup in report["why"], report
    assert "already taken" in report["note"] and world.tip(eng.FEATURE) == before, "the branch must not move"
    assert bk.remote_refs(world.local)[backup] != before, "the occupant must be left alone"


def test_pass_refuses_a_long_name_loudly(tmp_path):
    """Gate open and a unit name too long for a backup ref: the pass says so on the pick line, runs nothing after the
    reads, and pushes nothing; gate closed, the same pass goes on to replay. The new outcome is in the vocabulary."""
    fb = bk.module()
    import test_feature_rebase as eng
    m = eng._mod()
    for word in (m.NAME_TOO_LONG,):
        assert word in m.OUTCOMES and word in m.IN_CLAUSE and word in m._WORDING, word
    unit, sha = "a" * 221, "9" * 40
    sdlc = tmp_path / ".sdlc"

    def run_pass(block):
        calls = []

        def stub(cwd, argv):
            argv = [str(a) for a in argv]
            calls.append(argv)
            if argv[:3] == ["git", "ls-remote", "--heads"]:
                return "%s\trefs/heads/feature/%s" % (sha, unit)
            if argv[:2] == ["git", "rev-parse"]:
                return sha
            if argv[:3] == ["git", "rev-list", "--count"]:
                return "3"
            return ""
        (sdlc / "features").mkdir(parents=True, exist_ok=True)
        config = eng._cfg()
        if block is not None:
            config["upkeep"] = block
        return m.upkeep(str(sdlc), config, "7", unit, run=stub, cwd=str(tmp_path), remote="origin"), calls
    report, calls = run_pass({"enabled": True})
    assert report["outcome"] == m.NAME_TOO_LONG and "221" in report["why"] and "220" in report["why"], report
    assert report["note"].startswith(" \u2014 upkeep: ") and "NOT rebased" in report["note"], report["note"]
    assert not [c for c in calls if c[1] in ("worktree", "push", "rebase")], calls
    legacy, legacy_calls = run_pass(None)
    assert legacy["outcome"] != m.NAME_TOO_LONG and [c for c in legacy_calls if c[1] == "worktree"], (legacy, legacy_calls)
    assert fb.UNIT_LIMIT == 220


def test_legacy_unit_push_is_unchanged():
    """Green before and after: the engine's unit push is byte for byte what it was (the half no new test can show)."""
    m = bk.support.script("feature_rebase")
    calls = []

    def run(cwd, argv):
        calls.append((cwd, [str(a) for a in argv]))
        return ""
    sha, report = "a" * 40, {}
    assert m._pushed(run, "root", "wt", "feature/billing", sha, "origin", report) == m.REBASED
    assert calls == [("wt", ["git", "push", "--force-with-lease=feature/billing:" + sha, "origin",
                             "HEAD:refs/heads/feature/billing"])], calls


def test_nothing_else_in_the_engine_changed_shape():
    """Green before and after: the existing verbs keep their exit codes and the usage line still leads with them."""
    m = bk.support.script("feature_rebase")
    assert m.USAGE.startswith("usage: feature_rebase.py upkeep <sdlc_dir> <unit> [goal] | feature_rebase.py show")
    code, out, err = _quiet(m.main, ["feature_rebase.py"])
    assert code == 2 and out == "" and "usage" in err
    code, out, err = _quiet(m.main, ["feature_rebase.py", "ack", "x"])
    assert code == 2
