"""Slice 5: the backup ref NAME. Its shape, its 255-byte limit, and the strict parse that every later step trusts.

PLANNED TESTS of the pull-request red-to-green gate. Each one starts by asking `backup_support.module()` for the
backup module, which asserts the file exists, so before the module is written each fails by AssertionError and
never by ImportError or KeyError. The file is bound by its whole-file hash and is not edited after the red verify.
Nothing here prints, skips or spawns a process."""
import backup_support as bk


def test_ref_name_and_parse():
    """The name is prefix, unit, stamp; the stamp is strict; the parse accepts exactly that and nothing near it."""
    fb = bk.module()
    stamp = fb.stamp_of(bk.CLOCK)
    assert stamp == "20260921T141320Z" and len(stamp) == fb.STAMP_LEN == 16, stamp
    assert fb.PREFIX == "refs/sigma/backup/", fb.PREFIX
    assert fb.ref_name("billing", stamp) == "refs/sigma/backup/billing/20260921T141320Z"
    for epoch in (0, 1, 951782400, bk.CLOCK, 4102444799):
        assert fb.stamp_epoch(fb.stamp_of(epoch)) == epoch, epoch
    wrong = ["", "2026-09-21T14:13:20Z", "20260921T141320", "20260921t141320z", "20260921T141320Zx", "20261321T141320Z",
             "20260230T141320Z", "20260921T246000Z", "20260921T141360Z", " 20260921T141320Z", "20260921T141320Z\n",
             "２０260921T141320Z", None, 5]
    assert [t for t in wrong if fb.stamp_epoch(t) is not None] == []
    good = fb.PREFIX + "billing/" + stamp
    assert fb.parse_ref(good) == (fb.PREFIX, "billing", stamp)
    foreign = ["refs/heads/" + good, "refs/tags/" + good, "refs/other/" + good, "x" + good, good + "/", good + "/x",
               fb.PREFIX + stamp, fb.PREFIX + "/" + stamp, "refs/sigma/backups/billing/" + stamp,
               "refs/sigma/backup-old/billing/" + stamp, fb.PREFIX + "billing/2026", fb.PREFIX + "bill ing/" + stamp,
               fb.PREFIX + "bé/" + stamp, fb.PREFIX + "billing.lock/" + stamp, fb.PREFIX, None, 7]
    assert [r for r in foreign if fb.parse_ref(r) is not None] == []
    former = "refs/former/backup/"
    named = former + "u/" + stamp
    assert fb.parse_ref(named) is None and fb.parse_ref(named, (fb.PREFIX, former)) == (former, "u", stamp)


def test_name_limit_is_220_bytes():
    """220 bytes of unit fit (255 in all), 221 are refused loudly before any git call, and a bad name is not a long one."""
    fb = bk.module()
    assert fb.UNIT_LIMIT == 220 and fb.REF_LIMIT == 255
    stamp = fb.stamp_of(bk.CLOCK)
    fits, long = "a" * 220, "a" * 221
    assert len(fb.ref_name(fits, stamp).encode()) == 255 and fb.unit_problem(fits) is None
    code, why = fb.unit_problem(long)
    assert code == fb.TOO_LONG and "221" in why and "256" in why and "220" in why, (code, why)
    try:
        fb.ref_name(long, stamp)
    except fb.Refused as refused:
        assert refused.code == fb.TOO_LONG, refused.code
    else:
        raise AssertionError("ref_name accepted a 221-byte unit name")
    odd = ["", "a/b", "a b", "..", "a..b", "x.lock", "-a", ".a", "café", "a" * 220 + "é", None, 3]
    assert [(u, fb.unit_problem(u)) for u in odd if not fb.unit_problem(u) or fb.unit_problem(u)[0] != fb.BAD_INPUT] == []
    rec = bk.Recorder()
    out = fb.push_unit(rec, str(bk.ROOT), str(bk.ROOT), "origin", long, bk.OLD, clock=bk.CLOCK)
    assert out["outcome"] == fb.TOO_LONG and "220" in out["why"], out
    assert rec.calls == [], "a refused name must cost no git call: %r" % (rec.calls,)
