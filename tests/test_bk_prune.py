"""Slice 5: the prune of old backup refs, the one deletion this change adds.

PLANNED TESTS of the pull-request red-to-green gate; see `test_bk_name.py` for how they are built. The planner is
pure and is driven by tables and a seeded generator against an independent oracle; the deletion itself is shown on
REAL git remotes (a listing pattern that also returns lookalikes, a ref that changes after it was listed, an
unreadable listing) and, where thousands of refs would be slow, on a stand-in that an unlisted test checks against
real git. Mutation controls rebuild the module with one line broken and require the same checks to go red."""
import datetime
import json
import random
import re

import backup_support as bk

NOW = float(bk.CLOCK)
UNIT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _sha(n):
    return "%040x" % (n + 1)


def _name(fb, unit, days, prefix=None, seconds=0):
    return (prefix or fb.PREFIX) + unit + "/" + bk.stamp_days_ago(days, seconds)


def _plan(fb, entries, keep_days=14, keep_last=5, prefixes=None):
    return fb.plan_prune(entries, prefixes or (fb.PREFIX,), NOW, keep_days, keep_last)


def test_plan_keeps_newest_and_young():
    """Per unit the newest five stay; of the rest only a backup OLDER than 14 days goes; oldest first; a future stamp
    takes no slot and bad names are never planned."""
    fb = bk.module()

    def listing(unit, ages, seconds=0):
        return [(_sha(i), _name(fb, unit, a, seconds=seconds)) for i, a in enumerate(ages)]
    exactly14 = fb.PREFIX + "u/" + fb.stamp_of(NOW - 14 * bk.DAY)
    over14 = fb.PREFIX + "u/" + fb.stamp_of(NOW - 14 * bk.DAY - 1)
    young5 = listing("u", [1, 2, 3, 4, 5])
    table = [
        ("three old ones are all kept", listing("u", [20, 21, 22]), 14, 5, []),
        ("eight young ones are all kept", listing("u", range(1, 9)), 14, 5, []),
        ("eight old: the three oldest go", listing("u", range(20, 28)), 14, 5, [_name(fb, "u", d) for d in (27, 26, 25)]),
        ("mixed: the newest five include old ones", listing("u", [1, 2, 3, 20, 21, 22, 23]), 14, 5,
         [_name(fb, "u", d) for d in (23, 22)]),
        ("exactly 14 days is not older", young5 + [("a" * 40, exactly14)], 14, 5, []),
        ("one second over 14 days is older", young5 + [("a" * 40, over14)], 14, 5, [over14]),
        ("settings are read: one day, last one", listing("u", [0, 1, 2, 3]), 1, 1, [_name(fb, "u", d) for d in (3, 2)]),
        ("units are independent", listing("u", range(20, 27)) + listing("v", [20, 21]) + listing("w", range(30, 36)), 14, 5,
         [_name(fb, "w", 35), _name(fb, "u", 26), _name(fb, "u", 25)]),
        ("nothing listed", [], 14, 5, []),
    ]
    for label, entries, days, last, want in table:
        got = [ref for ref, _ in _plan(fb, entries, days, last)["delete"]]
        assert got == want, (label, got, want)
        again = [ref for ref, _ in _plan(fb, list(reversed(entries)), days, last)["delete"]]
        assert again == want, (label, "input order must not matter", again)
        plan = _plan(fb, entries, days, last)
        assert plan["kept"] + len(plan["delete"]) == len(entries), (label, plan)
    assert [sha for _, sha in _plan(fb, listing("u", range(20, 28)))["delete"]] == [_sha(7), _sha(6), _sha(5)]
    _future_and_malformed(fb)


def _future_and_malformed(fb):
    """A stamp in the future takes no keep slot; a stamp equal to now is not future; a name or object name that is not
    well formed is never planned."""
    old = [(_sha(i), _name(fb, "u", 20 + i)) for i in range(6)]
    future = [(_sha(10 + i), _name(fb, "u", -(2 + i))) for i in range(3)]
    plan = _plan(fb, old + future)
    assert [ref for ref, _ in plan["delete"]] == [_name(fb, "u", 25)], plan
    assert plan["future"] == 3 and plan["kept"] == 8, plan
    edge = [(_sha(0), fb.PREFIX + "u/" + fb.stamp_of(NOW))]
    assert _plan(fb, edge + old[:4])["delete"] == [] and _plan(fb, edge + old[:4])["future"] == 0, "a stamp equal to now is not future"
    stamp = bk.stamp_days_ago(30)
    bad = [("zz", fb.PREFIX + "u/" + stamp), ("", fb.PREFIX + "u/" + stamp), (_sha(1), fb.PREFIX + "u/" + stamp + "x"),
           (_sha(1), fb.PREFIX + "u/latest"), (_sha(1), fb.PREFIX + "u/20260230T000000Z"), (_sha(1), fb.PREFIX + stamp),
           (_sha(1), "refs/heads/" + fb.PREFIX + "u/" + stamp), (_sha(1), "refs/tags/x")]
    plan = _plan(fb, bad, 1, 1)
    assert plan["delete"] == [] and plan["foreign"] + plan["malformed"] == len(bad), plan
    assert plan["foreign"] == 2 and plan["malformed"] == 6, plan


def _oracle(entries, prefixes, now, keep_days, keep_last):
    """An independent brute-force statement of the rule, sharing no code with the module."""
    groups = {}
    for sha, ref in entries:
        for prefix in prefixes:
            if not ref.startswith(prefix):
                continue
            unit, _, leaf = ref[len(prefix):].partition("/")
            ok = (UNIT_RE.fullmatch(unit) and not unit.endswith((".lock", ".")) and ".." not in unit
                  and re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", leaf) and re.fullmatch(r"[0-9a-f]{40}", sha))
            if ok:
                try:
                    when = datetime.datetime.strptime(leaf, "%Y%m%dT%H%M%SZ").replace(tzinfo=datetime.timezone.utc)
                except ValueError:
                    break
                groups.setdefault((prefix, unit), []).append((leaf, ref, when.timestamp()))
            break
    doomed = []
    for rows in groups.values():
        rows = sorted((r for r in rows if r[2] <= now), reverse=True)
        doomed += [(t, ref) for _leaf, ref, t in rows[keep_last:] if t < now - keep_days * bk.DAY]
    return [ref for _t, ref in sorted(doomed)]


def _noise(rng, fb):
    """A random listing: ours (old, young, future), lookalikes, bad stamps, bad object names."""
    prefixes = (fb.PREFIX, "refs/former/backup/")
    entries = []
    for _ in range(rng.randint(0, 40)):
        kind = rng.choice(["ours", "ours", "ours", "ours", "future", "foreign", "leaf", "sha", "date"])
        unit = rng.choice(["a", "b", "c.d", "e-f"])
        prefix = rng.choice(prefixes)
        stamp = fb.stamp_of(NOW - rng.randint(0, 80 * bk.DAY))
        sha = _sha(rng.randint(0, 1 << 30))
        if kind == "future":
            stamp = fb.stamp_of(NOW + rng.randint(1, 5 * bk.DAY))
        ref = prefix + unit + "/" + stamp
        if kind == "foreign":
            ref = rng.choice(["refs/heads/", "refs/tags/", "refs/x/", "xrefs/", "refs/sigma/backups/"]) + ref
        elif kind == "leaf":
            ref = prefix + unit + "/" + rng.choice(["latest", stamp + "x", "2026", stamp[:-1], stamp + "/y"])
        elif kind == "sha":
            sha = rng.choice(["", "zz", sha.upper(), sha[:-1]])
        elif kind == "date":
            ref = prefix + unit + "/" + rng.choice(["20261340T000000Z", "20260230T000000Z", "20260921T256000Z"])
        entries.append((sha, ref))
    return entries, prefixes


def _mismatches(fb, rounds=120):
    rng = random.Random(921)
    wrong = 0
    for _ in range(rounds):
        entries, prefixes = _noise(rng, fb)
        days, last = rng.choice([(14, 5), (1, 1), (30, 3), (14, 1)])
        want = _oracle(entries, prefixes, NOW, days, last)
        got = [ref for ref, _ in fb.plan_prune(entries, prefixes, NOW, days, last)["delete"]]
        shuffled = entries[:]
        rng.shuffle(shuffled)
        again = [ref for ref, _ in fb.plan_prune(shuffled, prefixes, NOW, days, last)["delete"]]
        wrong += (got != want) + (again != want)
    return wrong


def test_plan_invariants_hold_on_noise():
    """The planner equals an independent oracle on 120 seeded noisy listings, and each of four broken planners differs."""
    fb = bk.module()
    assert _mismatches(fb) == 0, "the planner disagrees with the oracle on seeded noise"
    broken = [("rows[keep_last:]", "rows[keep_last - 1:]"), ("if epoch < cutoff:", "if epoch <= now:"),
              ("if epoch > now:", "if False:"), ("rows.sort(reverse=True)", "rows.sort()")]
    caught = [old for old, new in broken if _mismatches(bk.variant(old, new)) == 0]
    assert caught == [], "these broken planners passed the oracle check: %r" % (caught,)


def _make_remote(fb, root):
    """A real remote holding: unit u with 6 backups (ages 0-5) and 2 old (20, 21), the unit branch, and lookalikes
    that all carry an OLD stamp so any mistake would delete them. -> (world, names of the lookalikes, doomed names)."""
    world = bk.World(root)
    root.mkdir(parents=True)
    world.build()
    sha = world.head()
    for age in (0, 1, 2, 3, 4, 5, 20, 21):
        world.push_raw(sha, _name(fb, "u", age))
    old = bk.stamp_days_ago(40)
    lookalikes = ["refs/heads/refs/sigma/backup/u/" + old, "refs/tags/refs/sigma/backup/u/" + old,
                  "refs/other/refs/sigma/backup/u/" + old, "refs/sigma/backups/u/" + old,
                  "refs/sigma/backup-old/u/" + old, fb.PREFIX + "u/x/" + old, fb.PREFIX + old, fb.PREFIX + "u/latest"]
    for ref in lookalikes:
        world.push_raw(sha, ref)
    return world, lookalikes, [_name(fb, "u", 20), _name(fb, "u", 21)]


def _scenario(fb, root):
    """Prune the planted remote; -> the list of broken promises (empty when the prune is right)."""
    world, lookalikes, doomed = _make_remote(fb, root)
    before = world.refs()
    broken = []
    glob = bk.runner(str(world.local), ["git", "ls-remote", "origin", fb.PREFIX + "*"])
    returned = [line.partition("\t")[2] for line in glob.splitlines()]
    if not all(ref in returned for ref in lookalikes[:3]):
        broken.append("the listing pattern did not return the branch, tag and namespace lookalikes")
    if any(ref in returned for ref in lookalikes[3:5]):
        broken.append("the listing pattern returned a prefix lookalike it should not")
    rec = bk.Recorder()
    out = fb.prune_backups(bk.OPEN, str(world.sdlc), remote="origin", run=rec, cwd=str(world.local), clock=bk.CLOCK)
    after = world.refs()
    if set(before) - set(after) != set(doomed):
        broken.append("the removed set is %r, not the two old backups" % sorted(set(before) - set(after)))
    if any(after.get(ref) != before[ref] for ref in lookalikes + ["refs/heads/feature/u"]):
        broken.append("a lookalike or the unit branch changed")
    if out.get("foreign") != 3 or out.get("malformed") != 3:
        broken.append("foreign/malformed counts are %r/%r" % (out.get("foreign"), out.get("malformed")))
    named = [a for call in rec.pushes() for a in call if a.startswith("refs/")]
    if any(fb.parse_ref(a) is None for a in named):
        broken.append("a delete named a ref that is not one of ours")
    return broken


def test_plan_trusts_only_the_exact_prefix(tmp_path):
    """Real remote: the listing also returns a branch, a tag and another namespace; none of them, and no lookalike, is touched."""
    fb = bk.module()
    assert _scenario(fb, tmp_path / "real") == []
    spy = bk.Recorder(inner=lambda cwd, argv: "")
    for ref in ("refs/heads/feature/u", "refs/tags/x", fb.PREFIX + "u/latest", "refs/sigma/backups/u/" + bk.stamp_days_ago(40)):
        try:
            fb._delete_chunk(spy, ".", "origin", [(ref, bk.OLD)], (fb.PREFIX,))
        except fb.Refused:
            continue
        raise AssertionError("the delete step accepted a name that is not one of ours: %r" % ref)
    assert spy.calls == [], "the delete step must refuse before it runs anything: %r" % (spy.calls,)
    fb._delete_chunk(spy, ".", "origin", [(_name(fb, "u", 40), bk.OLD)], (fb.PREFIX,))
    assert len(spy.calls) == 1 and spy.calls[0][:3] == ["git", "push", "--delete"], spy.calls
    open_sink = bk.variant("        if parse_ref(ref, prefixes) is None or _SHA_RE.fullmatch(sha) is None:", "        if False:")
    leak = bk.Recorder(inner=lambda cwd, argv: "")
    open_sink._delete_chunk(leak, ".", "origin", [("refs/heads/feature/u", bk.OLD)], (fb.PREFIX,))
    assert leak.calls, "with the check removed the delete step must reach the runner (the control)"
    tail = bk.variant("        if ref.startswith(prefix):\n            unit, sep, stamp = ref[len(prefix):].partition(\"/\")",
                      "        if prefix in ref:\n            unit, sep, stamp = ref.partition(prefix)[2].partition(\"/\")")
    assert _scenario(tail, tmp_path / "mutant") != [], "a parser that trusts the listing must be caught"


def test_plan_age_comes_from_the_name(tmp_path):
    """A backup of a very old commit made today is young; a backup of a new commit with an old name is old."""
    fb = bk.module()
    world = bk.World(tmp_path / "w")
    world.root.mkdir()
    world.build()
    ancient = world.commit("ancient.txt", "a", date="2026-05-01T00:00:00Z")
    fresh = world.commit("fresh.txt", "f", date="2026-09-21T14:00:00Z")
    given = {_name(fb, "u", 0): fresh, _name(fb, "u", 1): ancient, _name(fb, "u", 30): fresh, _name(fb, "u", 40): fresh}
    for ref, sha in given.items():
        world.push_raw(sha, ref)
    out = fb.prune_backups(bk.config(keep_last=1), str(world.sdlc), remote="origin", run=bk.runner,
                           cwd=str(world.local), clock=bk.CLOCK)
    left = [ref for ref in world.refs() if ref.startswith(fb.PREFIX)]
    assert out["outcome"] == fb.PRUNED and out["deleted"] == 2, out
    assert sorted(left) == sorted([_name(fb, "u", 0), _name(fb, "u", 1)]), left


def test_prune_deletes_by_flag_with_leases(monkeypatch):
    """Every delete is `git push --delete` with one lease per ref, in chunks sized by bytes (derived from the machine's
    limit), capped per run, resumable."""
    fb = bk.module()
    refs = {"refs/heads/feature/u%d" % i: _sha(i) for i in range(9)}
    for i in range(9):
        for k in range(50):
            refs[_name(fb, "u%d" % i, 20 + k)] = _sha(100 * i + k)
    fake = bk.FakeRemote(refs)
    budget, runs, total = 6000, 0, 0
    while True:
        out = fb.prune_backups(bk.OPEN, "x/.sdlc", remote="origin", run=fake, cwd=".", clock=bk.CLOCK, budget=budget)
        runs += 1
        if out["outcome"] == fb.NOTHING:
            break
        assert out["outcome"] == fb.PRUNED and out["confirmed"] is True, out
        total += out["deleted"]
        assert out["deleted"] == min(fb.MAX_DELETES, out["candidates"]) and runs < 6, out
    assert runs == 4 and total == 9 * 45, (runs, total)
    assert len([r for r in fake.refs if r.startswith(fb.PREFIX)]) == 9 * 5 and len([r for r in fake.refs if r.startswith("refs/heads/")]) == 9
    pushes = fake.deletes()
    assert len(pushes) > 3
    for argv in pushes:
        assert argv[:3] == ["git", "push", "--delete"] and "origin" in argv, argv
        rest = argv[3:]
        leases = [a for a in rest if a.startswith("--force-with-lease=")]
        targets = rest[len(leases) + 1:]
        assert rest[:len(leases)] == leases and rest[len(leases)] == "origin" and len(targets) == len(leases), argv
        assert not [a for a in argv if a in ("--atomic", "--force", "--mirror", "-d") or a.startswith((":", "+"))], argv
        assert all(fb.parse_ref(t) is not None for t in targets), targets
        assert sorted(l.partition("=")[2].rpartition(":")[0] for l in leases) == sorted(targets), argv
        assert sum(len(a) + 1 for a in argv) <= budget + 32, "a delete exceeded its byte budget"
    dry = bk.FakeRemote({_name(fb, "u", 20 + k): _sha(k) for k in range(9)})
    out = fb.prune_backups(bk.OPEN, "x/.sdlc", remote="origin", run=dry, cwd=".", clock=bk.CLOCK, dry_run=True)
    assert out["outcome"] == fb.DRY_RUN and out["would_delete"] and dry.deletes() == [], out
    _budget(fb, monkeypatch)


def _budget(fb, monkeypatch):
    """The byte budget is a fraction of the machine's own limit, clamped, and survives a platform with no limit."""
    seen = {}
    for limit, want in ((1048576, 64000), (100000, 12500), (1000, 4096), (-1, 4096)):
        monkeypatch.setattr(fb.os, "sysconf", lambda name, limit=limit: limit)
        seen[limit] = fb.arg_budget()
        assert seen[limit] == want, (limit, seen[limit], want)

    def missing(name):
        raise ValueError(name)
    monkeypatch.setattr(fb.os, "sysconf", missing)
    assert fb.arg_budget() == 4096
    rows = [("refs/sigma/backup/u/%s" % bk.stamp_days_ago(20 + k), _sha(k)) for k in range(300)]
    groups = fb.chunks(rows, 3000)
    assert sum(len(g) for g in groups) == 300 and len(groups) > 2 and max(len(g) for g in groups) <= fb.CHUNK_REFS
    assert all(sum(fb._cost(r, s) for r, s in g) <= 3000 for g in groups)
    assert [len(g) for g in fb.chunks(rows[:250], 10 ** 9)] == [100, 100, 50]


def test_prune_real_remote_end_to_end(tmp_path):
    """Real git: the right refs go, a ref changed after the listing survives, a second run is quiet, an unreadable listing deletes nothing."""
    fb = bk.module()
    ages = {"u": [1, 2, 3, 20, 21, 22, 23, 24], "v": [20, 25, 30], "w": [30, 31, 32, 33, 34, 35]}
    want = {_name(fb, "u", d) for d in (22, 23, 24)} | {_name(fb, "w", 35)}

    def plant(root):
        world = bk.World(root)
        root.mkdir()
        world.build()
        for unit, days in ages.items():
            for d in days:
                world.push_raw(world.head(), _name(fb, unit, d))
        world.push_raw(world.head(), "refs/tags/keep-me")
        return world
    world = plant(tmp_path / "a")
    before, rec = world.refs(), bk.Recorder()
    out = fb.prune_backups(bk.OPEN, str(world.sdlc), remote="origin", run=rec, cwd=str(world.local), clock=bk.CLOCK)
    after = world.refs()
    assert out["outcome"] == fb.PRUNED and out["deleted"] == 4 and out["confirmed"] is True, out
    assert set(before) - set(after) == want and all(after[r] == before[r] for r in after), (set(before) - set(after))
    rec2 = bk.Recorder()
    quiet = fb.prune_backups(bk.OPEN, str(world.sdlc), remote="origin", run=rec2, cwd=str(world.local), clock=bk.CLOCK)
    assert quiet["outcome"] == fb.NOTHING and rec2.pushes() == [] and world.refs() == after, quiet
    world = plant(tmp_path / "b")
    other = world.commit("later.txt", "later")
    victim = sorted(want)[0]
    moved = []

    def race(argv):
        if argv[:3] == ["git", "push", "--delete"] and not moved:
            world.push_raw(other, victim, force=True)
            moved.append(victim)
    out = fb.prune_backups(bk.OPEN, str(world.sdlc), remote="origin", run=bk.Recorder(before=race),
                           cwd=str(world.local), clock=bk.CLOCK)
    survivors = world.refs()
    assert moved and out["outcome"] == fb.PARTIAL and out["deleted"] == 3 and out["failed_chunks"] == 1, out
    assert survivors.get(victim) == other, "a ref that changed after it was listed must survive"
    assert not [r for r in want if r != victim and r in survivors]
    world = plant(tmp_path / "c")
    rec3 = bk.Recorder(inner=lambda cwd, argv: (_ for _ in ()).throw(RuntimeError("no route to host")))
    down = fb.prune_backups(bk.OPEN, str(world.sdlc), remote="origin", run=rec3, cwd=str(world.local), clock=bk.CLOCK)
    assert down["outcome"] == fb.UNREADABLE and down["deleted"] == 0 and rec3.pushes() == [], down


def test_former_prefix_planted_and_empty():
    """The list ships empty; a planted prefix is read and pruned like our own; every other namespace is left alone."""
    fb = bk.module()
    own = {_name(fb, "u", d): _sha(d) for d in range(20, 27)}
    former = {_name(fb, "u", d, prefix="refs/former/backup/"): _sha(100 + d) for d in range(20, 27)}
    unlisted = {_name(fb, "u", d, prefix="refs/unlisted/backup/"): _sha(200 + d) for d in range(20, 27)}
    base = dict(own, **former, **unlisted)

    def run(config):
        fake = bk.FakeRemote(base)
        out = fb.prune_backups(config, "x/.sdlc", remote="origin", run=fake, cwd=".", clock=bk.CLOCK)
        patterns = [c[3:] for c in fake.calls if c[:2] == ["git", "ls-remote"]][0]
        return out, patterns, set(base) - set(fake.refs)
    out, patterns, gone = run(bk.OPEN)
    assert patterns == [fb.PREFIX + "*"] and gone == {_name(fb, "u", 25), _name(fb, "u", 26)}, (patterns, gone)
    out, patterns, gone = run(bk.config(former_prefixes=["refs/former/backup/"]))
    assert patterns == [fb.PREFIX + "*", "refs/former/backup/*"], patterns
    assert gone == {_name(fb, "u", d, p) for d in (25, 26) for p in (None, "refs/former/backup/")}, gone
    assert out["prefixes"] == 2 and out["rejected_prefixes"] == [], out
    bad = ["refs/heads/", "refs/tags/x/backup/", "bogus-value", "refs/sigma/backup/", "refs/Sigma/backup/",
           "refs/notes/backup/", "refs/ok/backup/", "refs/ok/backup/"]
    out, patterns, gone = run(bk.config(former_prefixes=bad))
    assert patterns == [fb.PREFIX + "*", "refs/ok/backup/*"] and [r["position"] for r in out["rejected_prefixes"]] == [1, 2, 3, 4, 5, 6, 8], (patterns, out)
    assert not [b for b in bad[:6] if b in json.dumps(out)], "a rejected value must not be echoed"
    many = ["refs/p%d/backup/" % i for i in range(10)]
    out, patterns, gone = run(bk.config(former_prefixes=many))
    assert len(patterns) == 9 and [r["position"] for r in out["rejected_prefixes"]] == [9, 10], (patterns, out)
    for odd in ([""], [7], "refs/x/backup/", [None]):
        fake = bk.FakeRemote(base)
        closed = fb.prune_backups(bk.config(former_prefixes=odd), "x/.sdlc", remote="origin", run=fake, cwd=".", clock=bk.CLOCK)
        assert closed.get("closed") is True and fake.calls == [], (odd, closed)
    needle = bk.support.script("legacy").RETIRED.lower()
    paths = [bk.MODULE, bk.ROOT / "docs" / "branching-model.md", bk.ROOT / "README.md", bk.support.TEMPLATE]
    paths += sorted((bk.ROOT / "tests").glob("*bk_*.py")) + [bk.ROOT / "tests" / "backup_support.py"]
    assert [p.name for p in paths if needle in p.read_text(encoding="utf-8").lower()] == []


def test_fake_remote_matches_real_git(tmp_path):
    """Green before and after: the stand-in used above lists and deletes the way real git does."""
    world = bk.World(tmp_path / "w")
    world.root.mkdir()
    world.build()
    names = ["refs/sigma/backup/u/20260101T000000Z", "refs/sigma/backup/u/20260102T000000Z", "refs/heads/refs/sigma/backup/u/x",
             "refs/tags/refs/sigma/backup/u/y", "refs/other/refs/sigma/backup/u/z", "refs/sigma/backups/u/q", "refs/sigma/backup-old/u/r"]
    for name in names:
        world.push_raw(world.head(), name)
    fake = bk.FakeRemote(world.refs())
    for pattern in (["refs/sigma/backup/*"], ["refs/sigma/backup/u/*"], ["refs/heads/feature/u"], ["refs/sigma/backup/*", "refs/other/*"]):
        real = bk.runner(str(world.local), ["git", "ls-remote", "origin", *pattern])
        assert fake(".", ["git", "ls-remote", "origin", *pattern]) == real, pattern
    sha = world.head()
    good, stale = names[0], names[1]
    argv = ["git", "push", "--delete", "--force-with-lease=%s:%s" % (good, sha), "--force-with-lease=%s:%s" % (stale, "1" * 40),
            "origin", good, stale]
    try:
        bk.runner(str(world.local), argv)
        raised = False
    except RuntimeError:
        raised = True
    try:
        fake(".", argv)
        fake_raised = False
    except RuntimeError:
        fake_raised = True
    assert raised and fake_raised and set(world.refs()) == set(fake.refs), (raised, fake_raised)
