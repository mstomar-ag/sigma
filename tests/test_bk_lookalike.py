"""Slice 5 addendum: the prune never deletes a ref that is not the exact backup ref.

Not one of the 25 planned tests (those files are byte-pinned); added after plan review found that `git push
--delete <name>` resolves a short name by tail-matching (`refs/heads/<name>`, `refs/tags/<name>`, `refs/<name>`,
`refs/remotes/<name>`), so a lookalike can be deleted when the exact ref has vanished, or can stall the whole chunk when
both exist. Every scenario runs on a REAL local bare remote, with no network; the race tests include an in-test control
that disarms the new check and requires the same scenario to delete the lookalike."""
import pytest

import backup_support as bk

AGES = (0, 1, 2, 3, 4, 30)          # six backups of unit u: the 30-day-old one is the single prune candidate
FORMS = ("refs/heads/%s", "refs/tags/%s", "refs/%s", "refs/remotes/%s", "refs/remotes/%s/HEAD")


def _name(fb, days):
    return fb.PREFIX + "u/" + bk.stamp_days_ago(days)


def _world(fb, root):
    world = bk.World(root)
    root.mkdir(parents=True)
    world.build()
    for age in AGES:
        world.push_raw(world.head(), _name(fb, age))
    return world, _name(fb, 30)


def _prune(fb, world, run=None):
    return fb.prune_backups(bk.OPEN, str(world.sdlc), remote="origin", run=run or bk.Recorder(),
                            cwd=str(world.local), clock=bk.CLOCK)


@pytest.mark.parametrize("form", FORMS)
def test_planted_lookalike_keeps_the_candidate(tmp_path, form):
    """The candidate and a lookalike coexist: nothing is deleted, no delete push is even attempted, and it is counted."""
    fb = bk.module()
    world, victim = _world(fb, tmp_path / "w")
    lookalike = form % victim
    world.push_raw(world.head(), lookalike)
    before, rec = world.refs(), bk.Recorder()
    out = _prune(fb, world, rec)
    assert out["outcome"] == fb.NOTHING and out["skipped_ambiguous"] == 1 and out["deleted"] == 0, out
    assert world.refs() == before and lookalike in before and victim in before
    assert rec.pushes() == [], rec.pushes()


def _race(fb, world, victim, plant, at_push=False):
    """Between the listing and the delete the exact ref vanishes (and, if `plant`, a lookalike branch appears)."""
    state = []

    def vanish(argv):
        hit = argv[:3] == ["git", "push", "--delete"] if at_push else (argv[:2] == ["git", "ls-remote"] and "*" not in argv[-1])
        if hit and not state:
            state.append(1)
            bk.git(world.local, "push", "-q", "origin", "--delete", victim)
            if plant:
                world.push_raw(world.head(), "refs/heads/" + victim)
    return bk.Recorder(before=vanish), state


def test_vanished_exact_ref_does_not_delete_a_lookalike(tmp_path):
    fb = bk.module()
    world, victim = _world(fb, tmp_path / "w")
    rec, state = _race(fb, world, victim, plant=True)
    out = _prune(fb, world, rec)
    after = world.refs()
    assert state and "refs/heads/" + victim in after, "the lookalike branch was deleted"
    assert rec.pushes() == [] and out["skipped_ambiguous"] + out["skipped_vanished"] == 1 and out["deleted"] == 0, out
    # control: with the re-read disarmed, the same race (landing in the push window) deletes the branch; that window is
    # the named residual ceiling, so the control proves the test can fail
    world2, victim2 = _world(fb, tmp_path / "c")
    rec2, state2 = _race(fb, world2, victim2, plant=True, at_push=True)
    broken = bk.support.script("feature_backup", edit=("checked = _recheck(run, cwd, remote, group)",
                                                       "checked = (group, 0, 0)"))
    broken.prune_backups(bk.OPEN, str(world2.sdlc), remote="origin", run=rec2, cwd=str(world2.local), clock=bk.CLOCK)
    assert state2 and "refs/heads/" + victim2 not in world2.refs(), "control: the disarmed check should lose the branch"


def test_vanished_exact_ref_without_lookalike_is_skipped_and_not_counted(tmp_path):
    fb = bk.module()
    world, victim = _world(fb, tmp_path / "w")
    rec, state = _race(fb, world, victim, plant=False)
    out = _prune(fb, world, rec)
    assert state and rec.pushes() == [], rec.pushes()
    assert out["skipped_vanished"] == 1 and out["deleted"] == 0 and out["confirmed"] is True, out


def test_unreadable_reread_deletes_nothing(tmp_path):
    fb = bk.module()
    world, victim = _world(fb, tmp_path / "w")

    def inner(cwd, argv):
        if argv[:2] == ["git", "ls-remote"] and "*" not in argv[-1]:
            raise RuntimeError("no route to host")
        return bk.runner(cwd, argv)
    rec = bk.Recorder(inner=inner)
    before = world.refs()
    out = _prune(fb, world, rec)
    assert rec.pushes() == [] and world.refs() == before, out
    assert out["failed_chunks"] == 1 and out["outcome"] == fb.PARTIAL, out


def test_lookalike_appearing_after_the_listing_skips_only_its_own_ref(tmp_path):
    """Exact ref still present, a lookalike tag appears after the listing: git would refuse the whole chunk, so that one
    ref is skipped (counted) and its chunk-mate is still deleted."""
    fb = bk.module()
    world, victim = _world(fb, tmp_path / "w")
    mate = _name(fb, 31)
    world.push_raw(world.head(), mate)
    state = []

    def plant(argv):
        if argv[:2] == ["git", "ls-remote"] and "*" not in argv[-1] and not state:
            state.append(1)
            world.push_raw(world.head(), "refs/tags/" + victim)
    out = _prune(fb, world, bk.Recorder(before=plant))
    after = world.refs()
    assert state and victim in after and "refs/tags/" + victim in after and mate not in after, sorted(after)
    assert out["outcome"] == fb.PRUNED and out["deleted"] == 1 and out["skipped_ambiguous"] == 1, out
