"""Slice 5: the atomic leased push that keeps the old tip as a backup, as the engine's unit push now makes it, and the
restore that undoes it.

PLANNED TESTS of the pull-request red-to-green gate; see `test_bk_name.py` for how they are built. These run REAL
git against throwaway bare remotes: both-or-neither, a stale lease, a name collision and a missing --atomic are
properties of git, so only git can show them. The first four drive the engine's own `_pushed` with the descriptor the
pass builds when the upkeep gate is open (the pass itself is driven in `test_bk_gate.py`). Every child process is
captured; nothing prints, skips or sleeps."""
import backup_support as bk

UNIT = "u"
BRANCH = "refs/heads/feature/u"


def _rewritten(root):
    """A world whose remote branch holds a two-commit history and whose local HEAD is an amended copy of its tip:
    -> (world, old tip)."""
    root.mkdir(parents=True)
    world = bk.World(root).build()
    world.commit("second.txt", "second")
    world.push_raw(world.head(), BRANCH)
    old = world.tip()
    world.amend()
    assert world.head() != old and world.tip() == old
    return world, old


def _made(fb, world, old, clock=bk.CLOCK):
    """A backup made the way the pass makes it, for the restore tests."""
    out = fb.push_unit(bk.runner, str(world.local), str(world.local), "origin", UNIT, old, clock=clock)
    assert out["outcome"] == fb.PUSHED, out
    return out


def test_push_makes_both_refs(tmp_path):
    """The engine's unit push, gate open: ONE atomic push moves the branch and creates the backup of the old tip."""
    fb = bk.module()
    world, old = _rewritten(tmp_path / "w")
    rec = bk.Recorder()
    outcome, report = bk.pushed(world, old, rec)
    backup = "refs/sigma/backup/u/" + fb.stamp_of(bk.CLOCK)
    assert outcome == "rebased" and report["backup"] == backup, (outcome, report)
    assert world.tip() == world.head() and world.ref(backup) == old, world.refs()
    assert len(rec.calls) == 1 and len(rec.pushes()) == 1, rec.calls
    assert rec.pushes()[0] == ["git", "push", "--atomic", "--force-with-lease=refs/heads/feature/u:" + old,
                               "--force-with-lease=" + backup + ":", "origin", "HEAD:refs/heads/feature/u",
                               old + ":" + backup], rec.pushes()[0]
    assert not [a for a in rec.pushes()[0] if a.startswith("+") or a in ("--force", "--mirror", "--delete")]
    assert fb.ref_name(UNIT, fb.stamp_of(bk.CLOCK)) == backup


def test_push_stale_lease_makes_neither(tmp_path):
    """Another writer got there first: the branch keeps their commit, no backup name appears, the pass reports a refusal."""
    fb = bk.module()
    world, old = _rewritten(tmp_path / "w")
    theirs = world.other_writer_pushes()
    outcome, report = bk.pushed(world, old, bk.Recorder())
    assert outcome == "lease-refused" and report["tip"] == theirs, (outcome, report)
    assert world.tip() == theirs, "the other writer's commit must survive"
    assert [r for r in world.refs() if r.startswith("refs/sigma/")] == [], world.refs()
    assert "backup" not in report


def test_push_collision_is_refused_loudly(tmp_path):
    """A name already taken by anything but the same commit refuses the WHOLE push in words; nothing is overwritten."""
    fb = bk.module()
    backup = "refs/sigma/backup/u/" + fb.stamp_of(bk.CLOCK)
    for kind in ("unrelated", "ancestor", "descendant", "same"):
        world, old = _rewritten(tmp_path / kind)
        tree = old + "^{tree}"
        occupant = {"unrelated": lambda: bk.git(world.local, "commit-tree", "-m", "stray", tree),
                    "ancestor": lambda: bk.git(world.local, "rev-parse", old + "~1"),
                    "descendant": lambda: bk.git(world.local, "commit-tree", "-p", old, "-m", "child", tree),
                    "same": lambda: old}[kind]()
        world.push_raw(occupant, backup)
        outcome, report = bk.pushed(world, old, bk.Recorder())
        if kind == "same":
            assert outcome == "rebased" and world.tip() == world.head(), (kind, outcome, report)
            continue
        assert outcome == "failed" and "already taken" in report["why"] and backup in report["why"], (kind, outcome, report)
        assert world.tip() == old, "%s: the branch must not move when the backup is refused" % kind
        assert world.ref(backup) == occupant, "%s: the occupant must be left alone" % kind


def test_push_without_atomic_changes_nothing(tmp_path):
    """A server that cannot do an atomic push is a loud failure with one push attempt, never a weaker push."""
    fb = bk.module()
    world, old = _rewritten(tmp_path / "w")
    bk.git(world.remote, "config", "receive.advertiseAtomic", "false")
    rec = bk.Recorder()
    outcome, report = bk.pushed(world, old, rec)
    assert outcome == "failed" and "atomic" in report["why"].lower(), (outcome, report)
    assert len(rec.pushes()) == 1, "no fallback push may follow: %r" % (rec.pushes(),)
    assert world.tip() == old and [r for r in world.refs() if r.startswith("refs/sigma/")] == []


def test_push_helper_refuses_bad_input():
    """A bad source, tip, unit or remote reaches no command; with the source check removed an empty source WOULD
    build the delete shape (the control)."""
    fb = bk.module()
    rec = bk.Recorder()
    cwd = str(bk.ROOT)

    def push(unit=UNIT, tip=bk.OLD, remote="origin", **kw):
        return fb.push_unit(rec, cwd, cwd, remote, unit, tip, clock=bk.CLOCK, **kw)
    for source in ("", None, "HEAD~1", " HEAD", "refs/heads/x", ":x", "A" * 40, "abc"):
        assert push(source=source)["outcome"] == fb.BAD_INPUT, source
    for tip in ("", None, "abc", "A" * 40, "0" * 39, "0" * 41):
        assert push(tip=tip)["outcome"] == fb.BAD_INPUT, tip
    for unit in ("", "a/b", "x.lock", None):
        assert push(unit=unit)["outcome"] == fb.BAD_INPUT, unit
    for remote in ("", "-x", "a b", None):
        assert push(remote=remote)["outcome"] == fb.BAD_INPUT, remote
    assert rec.calls == [], "bad input must cost no git call: %r" % (rec.calls,)
    loose = bk.variant('if not (isinstance(source, str) and (source == "HEAD" or _SHA_RE.fullmatch(source))):',
                       "if False:")
    seen = bk.Recorder(inner=lambda cwd, argv: "")
    loose.push_unit(seen, cwd, cwd, "origin", UNIT, bk.OLD, source="", clock=bk.CLOCK)
    assert any(arg == ":refs/heads/feature/u" for call in seen.calls for arg in call), seen.calls


def test_restore_puts_the_old_tip_back(tmp_path):
    """From a clone that never had the old commit: the branch returns to it and the produced tip becomes a backup."""
    fb = bk.module()
    world, old = _rewritten(tmp_path / "w")
    _made(fb, world, old)
    produced, stamp = world.tip(), fb.stamp_of(bk.CLOCK)
    clone = world.clone("fresh")
    assert bk.git(clone, "cat-file", "-t", old, check=False) == "", "the clone must not have the old commit yet"
    sdlc, rec = str(clone / ".sdlc"), bk.Recorder()
    local = lambda: [r for r in world.local_refs(clone) if not r.startswith("refs/remotes/")]
    before = local()
    listing = fb.restore_backup(bk.OPEN, sdlc, UNIT, None, None, remote="origin", run=rec, cwd=str(clone),
                                list_only=True)
    assert listing["outcome"] == fb.LISTED and listing["branch_tip"] == produced, listing
    assert listing["backups"] == [{"stamp": stamp, "sha": old}], listing
    later = bk.CLOCK + 60
    out = fb.restore_backup(bk.OPEN, sdlc, UNIT, stamp, produced, remote="origin", run=rec, cwd=str(clone),
                            clock=later)
    fresh = "refs/sigma/backup/u/" + fb.stamp_of(later)
    assert out["outcome"] == fb.RESTORED and out["backup"] == fresh, out
    assert world.tip() == old, "the branch must be back at the old tip"
    assert world.ref(fresh) == produced and world.ref("refs/sigma/backup/u/" + stamp) == old, world.refs()
    assert rec.pushes()[-1] == ["git", "push", "--atomic", "--force-with-lease=refs/heads/feature/u:" + produced,
                                "--force-with-lease=" + fresh + ":", "origin", old + ":refs/heads/feature/u",
                                produced + ":" + fresh], rec.pushes()[-1]
    fetches = [c for c in rec.calls if c[:2] == ["git", "fetch"]]
    assert fetches and all("--no-write-fetch-head" in c for c in fetches), fetches
    assert not (clone / ".git" / "FETCH_HEAD").exists(), "a restore must not rewrite FETCH_HEAD"
    assert local() == before, "a restore must create no local branch or ref: %r -> %r" % (before, local())


def test_restore_refuses_when_remote_moved(tmp_path):
    """A tip that is not the one named, or one that moves during the push, refuses and leaves the other writer's work."""
    fb = bk.module()
    world, old = _rewritten(tmp_path / "a")
    _made(fb, world, old)
    produced, stamp = world.tip(), fb.stamp_of(bk.CLOCK)
    theirs = world.other_writer_pushes()
    rec = bk.Recorder()
    out = fb.restore_backup(bk.OPEN, str(world.sdlc), UNIT, stamp, produced, remote="origin", run=rec,
                            cwd=str(world.local), clock=bk.CLOCK + 60)
    assert out["outcome"] == fb.MOVED and out["tip"] == theirs, out
    assert rec.pushes() == [] and world.tip() == theirs, "a moved remote must cost no push"
    world, old = _rewritten(tmp_path / "b")
    _made(fb, world, old)
    produced, stamp = world.tip(), fb.stamp_of(bk.CLOCK)
    moved = []

    def race(argv):
        if argv[:2] == ["git", "push"] and not moved:
            moved.append(world.other_writer_pushes(name="racer"))
    out = fb.restore_backup(bk.OPEN, str(world.sdlc), UNIT, stamp, produced, remote="origin",
                            run=bk.Recorder(before=race), cwd=str(world.local), clock=bk.CLOCK + 60)
    assert moved and out["outcome"] == fb.LEASE_REFUSED and out["tip"] == moved[0], out
    assert world.tip() == moved[0], "the lease must keep the racing writer's commit"
    assert world.ref("refs/sigma/backup/u/" + fb.stamp_of(bk.CLOCK + 60)) == "", "no backup without the update"
    world, old = _rewritten(tmp_path / "c")
    _made(fb, world, old)
    produced, stamp, later = world.tip(), fb.stamp_of(bk.CLOCK), bk.CLOCK + 60
    taken = "refs/sigma/backup/u/" + fb.stamp_of(later)
    stray = bk.git(world.local, "commit-tree", "-m", "stray", produced + "^{tree}")
    world.push_raw(stray, taken)
    out = fb.restore_backup(bk.OPEN, str(world.sdlc), UNIT, stamp, produced, remote="origin", run=bk.Recorder(),
                            cwd=str(world.local), clock=later)
    assert out["outcome"] == fb.COLLISION and out["holds"] == stray, out
    assert world.tip() == produced and world.ref(taken) == stray, "a same-second restore must change nothing"


def test_restore_input_and_lock_refusals():
    """Every refusal happens before a push: bad tip or stamp, unknown stamp, busy unit, blank listing, no change."""
    fb = bk.module()
    stamp = fb.stamp_of(bk.CLOCK)
    name = "refs/sigma/backup/u/" + stamp
    tip, other = "1" * 40, "2" * 40

    def remote(listing, head=tip):
        def run(cwd, argv):
            if argv[:2] == ["git", "ls-remote"] and any("backup" in a for a in argv):
                return listing
            if argv[:2] == ["git", "ls-remote"]:
                return "%s\t%s" % (head, BRANCH)
            return ""
        return run
    base = dict(remote="origin", cwd=str(bk.ROOT), clock=bk.CLOCK)
    sdlc = str(bk.ROOT / "none" / ".sdlc")
    good = "%s\t%s" % (other, name)
    cases = [("no tip", stamp, None, good, fb.BAD_INPUT), ("short tip", stamp, "abc", good, fb.BAD_INPUT),
             ("upper tip", stamp, "A" * 40, good, fb.BAD_INPUT), ("bad stamp", "soon", tip, good, fb.BAD_INPUT),
             ("unknown stamp", "20200101T000000Z", tip, good, fb.NO_BACKUP),
             ("blank sha", stamp, tip, "\t" + name, fb.NO_BACKUP),
             ("no change", stamp, tip, "%s\t%s" % (tip, name), fb.NO_CHANGE),
             ("empty listing", stamp, tip, "", fb.NO_BACKUP)]
    for label, st, expect, listing, want in cases:
        rec = bk.Recorder(inner=remote(listing))
        out = fb.restore_backup(bk.OPEN, sdlc, UNIT, st, expect, run=rec, **base)
        assert out["outcome"] == want, (label, out)
        assert rec.pushes() == [], "%s: a refusal must not push" % label
    rec = bk.Recorder(inner=remote(good))
    busy = fb.restore_backup(bk.OPEN, sdlc, UNIT, stamp, tip, run=rec, hold=lambda unit: None, **base)
    assert busy["outcome"] == fb.BUSY and rec.calls == [], (busy, rec.calls)
    held = []
    fb.restore_backup(bk.OPEN, sdlc, UNIT, stamp, tip, run=bk.Recorder(inner=remote(good)),
                      hold=lambda unit: held.append(unit) or (lambda: held.append("released")), **base)
    assert held == [UNIT, "released"], "the lock must be taken for the unit and released afterwards: %r" % held

    def unreadable(cwd, argv):
        raise RuntimeError("no route to host")
    out = fb.restore_backup(bk.OPEN, sdlc, UNIT, stamp, tip, run=unreadable, **base)
    assert out["outcome"] == fb.UNREADABLE, out
