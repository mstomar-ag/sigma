"""#924: one end-to-end run of upkeep part A on a local scratch bare remote, and the record that says so.

The remote is a bare repository made in a temporary directory, the clones are local, and nothing here calls a model or a
hosting service (the rebase pass, the backup refs, the restore and the prune are the shipped code; only the clock is
set). `transcript()` is the single source of the committed record `docs/launch/evidence/upkeep-local-run.md`: a test compares
the record's block to it, so the record cannot drift from what the code does. It says plainly that this ran against a local
bare remote only, never on a hosting service."""
import pathlib

import backup_support as bk
import test_feature_rebase as eng
import upkeep_support as support

ROOT = support.ROOT
RECORD = ROOT / "docs" / "launch" / "evidence" / "upkeep-local-run.md"
BEGIN, END = "<!-- transcript:begin -->", "<!-- transcript:end -->"
STEP = 60
DAY = 86400


def refs_of(world):
    return bk.remote_refs(world.local)


def transcript(tmp):
    """Run the scenario in `tmp` and -> (lines, facts). Shas are labelled by role, stamps are the set clock's."""
    fb = bk.module()
    world = eng.World(pathlib.Path(tmp)).build()
    world.move_integration()
    m = eng._mod()
    config = eng._cfg()
    config["upkeep"] = {"enabled": True, "backup": {"keep_last": 1, "keep_days": 14}}
    sdlc = str(world.sdlc)
    base = lambda: world.tip(eng.INTEGRATION)
    tip0 = world.tip(eng.FEATURE)
    label = {tip0: "tip0"}
    lines = ["remote: a bare repository in a temporary directory; clone: one local checkout; no hosting service",
             "setup: feature/billing has one commit on tip0; main then moved ahead, so the unit is behind"]

    m._WALL = bk.CLOCK
    one = m.upkeep(sdlc, config, "7", eng.UNIT)
    tip1 = world.tip(eng.FEATURE)
    label[tip1] = "tip1"
    stamp1 = fb.stamp_of(bk.CLOCK)
    ref1 = "refs/sigma/backup/billing/" + stamp1
    lines.append("pass 1: outcome=%s; branch %s -> %s; backup %s holds %s" % (
        one["outcome"], label[tip0], label[tip1], ref1, label.get(refs_of(world).get(ref1), "unknown")))

    clone = pathlib.Path(tmp) / "fresh"
    bk.git(tmp, "clone", "-q", "--no-local", str(world.remote), str(clone))
    run = dict(remote="origin", run=bk.runner, cwd=str(clone))
    listing = fb.restore_backup(config, str(clone / ".sdlc"), eng.UNIT, None, None, list_only=True, **run)
    lines.append("restore --list: branch at %s; backups %s" % (
        label.get(listing["branch_tip"], "unknown"),
        ", ".join("%s holds %s" % (b["stamp"], label.get(b["sha"], "unknown")) for b in listing["backups"])))

    stamp2 = fb.stamp_of(bk.CLOCK + STEP)
    back = fb.restore_backup(config, str(clone / ".sdlc"), eng.UNIT, stamp1, tip1, clock=bk.CLOCK + STEP, **run)
    ref2 = "refs/sigma/backup/billing/" + stamp2
    lines.append("restore %s expecting %s: outcome=%s; branch at %s; new backup %s holds %s" % (
        stamp1, label[tip1], back["outcome"], label.get(world.tip(eng.FEATURE), "unknown"), ref2,
        label.get(refs_of(world).get(ref2), "unknown")))

    m = eng._mod()
    m._WALL = bk.CLOCK + 2 * STEP
    two = m.upkeep(sdlc, config, "7", eng.UNIT)
    stamp3 = fb.stamp_of(bk.CLOCK + 2 * STEP)
    ref3 = "refs/sigma/backup/billing/" + stamp3
    lines.append("pass 2 (a restore is not sticky): outcome=%s; branch moved off %s; backup %s holds %s" % (
        two["outcome"], label[tip0], ref3, label.get(refs_of(world).get(ref3), "unknown")))

    later = bk.CLOCK + 30 * DAY
    plan = fb.prune_backups(config, sdlc, remote="origin", run=bk.runner, cwd=str(world.local), clock=later, dry_run=True)
    after_dry = sorted(r for r in refs_of(world) if r.startswith("refs/sigma/backup/"))
    done = fb.prune_backups(config, sdlc, remote="origin", run=bk.runner, cwd=str(world.local), clock=later)
    left = sorted(r for r in refs_of(world) if r.startswith("refs/sigma/backup/"))
    lines.append("prune --dry-run at +30 days: nothing removed, %d backup refs remain" % len(after_dry))
    lines.append("prune at +30 days (keep_last 1, keep_days 14): %d backup refs remain, the newest %s" % (len(left), left[-1].rsplit("/", 1)[-1] == stamp3 and "(" + stamp3 + ")" or "?"))
    facts = dict(one=one, two=two, back=back, plan=plan, done=done, left=left, refs=(ref1, ref2, ref3), tips=(tip0, tip1),
                 world=world, stamps=(stamp1, stamp2, stamp3))
    return lines, facts


def test_one_run_rebases_backs_up_restores_and_prunes(tmp_path):
    lines, f = transcript(tmp_path)
    tip0, tip1 = f["tips"]
    ref1, ref2, ref3 = f["refs"]
    refs = refs_of(f["world"])
    assert f["one"]["outcome"] == "rebased" and f["two"]["outcome"] == "rebased"
    assert f["back"]["outcome"] == "restored" and f["back"]["restored_tip"] == tip0 and f["back"]["backed_up"] == tip1
    assert f["plan"]["outcome"] == "dry-run" and f["plan"]["would_delete"] == [ref1, ref2], f["plan"]
    assert f["done"]["outcome"] == "pruned" and f["done"]["deleted"] == 2 and f["done"]["kept"] == 1, f["done"]
    assert f["left"] == [ref3], f["left"]
    # the prune is the one sanctioned deletion: the unit branch and the base survive it, and nothing else was removed
    assert sorted(refs) == sorted(["refs/heads/main", "refs/heads/feature/billing", ref3]), sorted(refs)
    assert refs["refs/heads/feature/billing"] not in (tip0,), "the second pass must have moved the branch again"
    assert refs[ref3] == tip0


def test_the_committed_record_is_this_run(tmp_path):
    """The evidence record carries exactly this transcript and says plainly that it was a local bare remote only."""
    lines, _facts = transcript(tmp_path)
    text = RECORD.read_text(encoding="utf-8")
    assert BEGIN in text and END in text, "the record must wrap its transcript in the two markers"
    block = text.split(BEGIN)[1].split(END)[0].strip().removeprefix("```text").removesuffix("```").strip()
    assert block == "\n".join(lines), "regenerate the record's block from transcript()"
    prose = " ".join(text.replace(block, "").lower().split())
    assert "local bare remote" in prose and "hosting service" in prose
    assert "not proven" in prose or "no claim" in prose
