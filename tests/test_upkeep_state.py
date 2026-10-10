"""#919: per-unit upkeep state and the due rule (`feature_upkeep_state.py`).

THE LISTED TESTS (every one red by assertion before the module exists, green after) are silent, unparametrised and each
loops a table, collecting every failing case into ONE assertion message. Their checks are module-level functions that take
the module under test, so the control test can run the very same checks against deliberately broken copies and see them
fail (a guard never seen red is decoration)."""
import fractions
import itertools
import json
import os
import pathlib
import sys
from unittest import mock

import attempt_trap
import upkeep_drift_support as S

NOW = S.at(100)
SECOND_READ = NOW + 10
GOOD_TIPS = {"unit_tip": "a" * 40, "base_tip": "b" * 40}
_RUNS = itertools.count()


def fresh(tmp, label="p"):
    """A new `.sdlc` directory path (not created) in a directory of its own."""
    return tmp / ("%s-%d" % (label, next(_RUNS))) / ".sdlc"


def parts():
    d, s = S.drift(), S.ustate()
    return d, s, s.schedule(S.settings())


def drift_of(d, **kw):
    base = {"drift": 5, "behind": 4, "threshold": 3, "dormant": False, "notes": ()}
    base.update(kw)
    return d.Drift(**base)


def doc_of(s, tmp, name="voice"):
    return json.loads(s.unit_state_path(tmp, name).read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------------------ the checks (each returns a list of failures)

def path_failures(s, tmp):
    bad = []
    sdlc = tmp / ".sdlc"
    registry = S.sibling("feature_registry")
    want = sdlc / "state" / "upkeep" / "units" / "voice.json"
    S.expect(bad, "two casings, one file", (s.unit_state_path(str(sdlc), "Voice"), s.unit_state_path(sdlc, "voice")), (want, want))
    S.expect(bad, "the constant", s.STATE_REL, "state/upkeep/units")
    S.expect(bad, "the constant is not the block name", s.STATE_REL != "upkeep", True)
    S.expect(bad, "folded after the guard", s.unit_state_path(sdlc, "voice.LOCK").name, "voice.lock.json")
    for name in ("Voice", "VOICE", "a.B-c_D", "x-", "123", "index"):
        S.expect(bad, "fold of " + name, s.unit_state_path(sdlc, name).name, registry.unit_key(name) + ".json")
    for name in ("a/b", "..", "../x", "", ".hidden", "x.lock", "has space", "-x", "a..b", 5, None, b"x", ["x"]):
        try:
            s.unit_state_path(sdlc, name)
        except Exception as exc:                     # noqa: BLE001
            if type(exc).__name__ != "InvalidUnitName" or not isinstance(exc, ValueError):
                bad.append("wrong exception for %r: %s" % (name, type(exc).__name__))
        else:
            bad.append("accepted %r" % (name,))
    S.expect(bad, "a refused name makes no directory", sdlc.exists(), False)
    return bad


def atomic_failures(s, tmp):
    bad = []
    sdlc = tmp / ".sdlc"
    now = NOW
    first = s.record_attempt(sdlc, "voice", now, "backstop")
    path = s.unit_state_path(sdlc, "voice")
    S.expect(bad, "first write", tuple(first), (True, None))
    text = path.read_text(encoding="utf-8")
    doc = json.loads(text)
    S.expect(bad, "the fields", sorted(doc), sorted(s.FIELDS))
    S.expect(bad, "schema id", doc["schema"], "upkeep-unit/1")
    S.expect(bad, "compact, one line", (text.endswith("\n"), "\n" in text[:-1], " " in text.replace("upkeep-unit/1", "")), (True, False, False))
    S.expect(bad, "nothing else in the directory", sorted(os.listdir(path.parent)), ["voice.json"])
    before = path.read_bytes()
    for label, target in (("rename fails", "replace"), ("fsync fails", "fsync")):
        with mock.patch.object(os, target, side_effect=OSError("simulated")):
            result = s.record_outcome(sdlc, "voice", now + 5, "rebased", **GOOD_TIPS)
        S.expect(bad, label, tuple(result), (False, "unwritable"))
        S.expect(bad, label + ": the old file is whole", path.read_bytes(), before)
        S.expect(bad, label + ": no temporary file left", sorted(os.listdir(path.parent)), ["voice.json"])
    outside = tmp / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    linked = tmp / "linked" / ".sdlc"
    (linked / "state" / "upkeep" / "units").mkdir(parents=True)
    link = linked / "state" / "upkeep" / "units" / "voice.json"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        link = None
    if link is not None:
        S.expect(bad, "a symlinked file is refused", tuple(s.record_attempt(linked, "voice", now, "backstop")), (False, "unwritable"))
        S.expect(bad, "nothing written through the link", (outside.read_text(encoding="utf-8"), link.is_symlink()), ("keep", True))
        S.expect(bad, "a symlinked file reads unsafe", tuple(s.read_state(linked, "voice", now))[0::2], ("unreadable", "unsafe-path"))
    blocked = tmp / "dir" / ".sdlc"
    (blocked / "state" / "upkeep" / "units" / "voice.json").mkdir(parents=True)
    S.expect(bad, "a directory in the way", tuple(s.record_attempt(blocked, "voice", now, "backstop")), (False, "unwritable"))
    S.expect(bad, "a directory in the way leaves no temporary file", sorted(os.listdir(blocked / "state" / "upkeep" / "units")), ["voice.json"])
    S.expect(bad, "a directory reads unsafe", tuple(s.read_state(blocked, "voice", now))[0::2], ("unreadable", "unsafe-path"))
    flat = tmp / "flat" / ".sdlc"
    flat.mkdir(parents=True)
    (flat / "state").write_text("x", encoding="utf-8")
    S.expect(bad, "a file where a directory belongs", tuple(s.record_attempt(flat, "voice", now, "backstop")), (False, "unwritable"))
    S.expect(bad, "...and it is left alone", (flat / "state").read_text(encoding="utf-8"), "x")
    long = tmp / "long" / ".sdlc"
    S.expect(bad, "a name too long for a file name", tuple(s.record_attempt(long, "a" * 300, now, "backstop")), (False, "unwritable"))
    S.expect(bad, "...reads as unreadable", tuple(s.read_state(long, "a" * 300, now))[0::2], ("unreadable", "io"))
    S.expect(bad, "a bad name is not written", tuple(s.record_attempt(long, "a/b", now, "backstop")), (False, "bad-unit"))
    return bad


def unreadable_failures(s, d, tmp):
    bad = []
    sdlc = tmp / ".sdlc"
    sched = s.schedule(S.settings())
    level = drift_of(d, drift=0, behind=0)
    s.record_attempt(sdlc, "voice", NOW, "drift")
    s.record_outcome(sdlc, "voice", NOW + 5, "rebased", **GOOD_TIPS)
    path = s.unit_state_path(sdlc, "voice")
    good = doc_of(s, sdlc)
    S.expect(bad, "the good document reads", tuple(s.read_state(sdlc, "voice", SECOND_READ))[0::2], ("ok", None))
    dump = json.dumps
    drop = lambda key: {k: v for k, v in good.items() if k != key}           # noqa: E731
    cases = (
        ("garbage", "{not json", "not-json"), ("empty", "", "not-json"), ("array", "[]", "bad-schema"),
        ("string", '"x"', "bad-schema"), ("null", "null", "bad-schema"),
        ("other schema", dump(dict(good, schema="upkeep-unit/2")), "bad-schema"),
        ("no schema", dump(drop("schema")), "bad-schema"),
        ("missing field", dump(drop("outcome")), "bad-field"), ("extra field", dump(dict(good, extra=1)), "bad-field"),
        ("boolean time", dump(dict(good, last_attempt_at=True)), "bad-field"),
        ("float time", dump(dict(good, last_attempt_at=1.5)), "bad-field"),
        ("negative time", dump(dict(good, last_success_at=-1)), "bad-field"),
        ("text time", dump(dict(good, last_attempt_at="5")), "bad-field"),
        ("bad tip", dump(dict(good, unit_tip="XYZ")), "bad-field"), ("upper-case tip", dump(dict(good, base_tip="A" * 40)), "bad-field"),
        ("short tip", dump(dict(good, unit_tip="abc")), "bad-field"),
        ("bad outcome", dump(dict(good, outcome="nonsense")), "bad-field"),
        ("bad reason", dump(dict(good, due_reason="nonsense")), "bad-field"),
        ("failures over the cap", dump(dict(good, consecutive_failures=s.FAILURES_CAP + 1)), "bad-field"),
        ("boolean failures", dump(dict(good, consecutive_failures=True)), "bad-field"),
        ("negative failures", dump(dict(good, consecutive_failures=-1)), "bad-field"),
        ("float failures", dump(dict(good, consecutive_failures=1.0)), "bad-field"),
        ("another unit", dump(dict(good, unit="other")), "wrong-unit"), ("unit not a name", dump(dict(good, unit="a/b")), "wrong-unit"),
        ("unit not text", dump(dict(good, unit=5)), "wrong-unit"),
        ("duplicate key", '{"schema":"upkeep-unit/1","schema":"upkeep-unit/1"}', "not-json"),
        ("NaN", '{"schema":"upkeep-unit/1","last_attempt_at":NaN}', "not-json"),
        ("too big", " " * (s.MAX_STATE_CHARS + 1) + "{}", "too-big"), ("deeply nested", "[" * 3000, "not-json"),
        ("future attempt", dump(dict(good, last_attempt_at=SECOND_READ + s.FUTURE_SLACK_SECONDS + 1)), "future-stamp"),
        ("future success", dump(dict(good, last_success_at=SECOND_READ + s.FUTURE_SLACK_SECONDS + 1)), "future-stamp"),
    )
    for label, text, reason in cases:
        path.write_text(text, encoding="utf-8")
        read = s.read_state(sdlc, "voice", SECOND_READ)
        S.expect(bad, label, (read.kind, read.reason), ("unreadable", reason))
        verdict = s.decide(sched, read, level, SECOND_READ)
        S.expect(bad, label + ": overdue and reported", tuple(verdict)[:2] + (verdict.detail,), (True, "state-unreadable", reason))
    path.write_bytes(b"\xff\xfe{")
    S.expect(bad, "bytes that are not text", tuple(s.read_state(sdlc, "voice", SECOND_READ))[0::2], ("unreadable", "io"))
    edge = SECOND_READ + s.FUTURE_SLACK_SECONDS
    path.write_text(dump(dict(good, last_attempt_at=edge)), encoding="utf-8")
    S.expect(bad, "a stamp exactly at the slack still reads", s.read_state(sdlc, "voice", SECOND_READ).kind, "ok")
    gone = fresh(tmp, "gone")
    read = s.read_state(gone, "voice", NOW)
    S.expect(bad, "no file is missing, not unreadable", (read.kind, read.doc, read.reason), ("missing", None, None))
    S.expect(bad, "missing and behind: the first backstop", tuple(s.decide(sched, read, drift_of(d, drift=0), NOW))[:2], (True, "backstop"))
    S.expect(bad, "missing and level: nothing to do", tuple(s.decide(sched, read, level, NOW))[:2], (False, "not-behind"))
    S.expect(bad, "a bad name reads as unreadable", tuple(s.read_state(gone, "a/b", NOW))[0::2], ("unreadable", "bad-unit"))
    S.expect(bad, "a missing read made no directory", gone.exists(), False)
    return bad


def future_failures(s, d, tmp):
    bad = []
    sdlc = tmp / ".sdlc"
    sched = s.schedule(S.settings())
    behind = drift_of(d, drift=0, threshold=100)
    s.record_attempt(sdlc, "voice", NOW, "backstop")
    path = s.unit_state_path(sdlc, "voice")
    good = doc_of(s, sdlc)
    slack = s.FUTURE_SLACK_SECONDS
    stamp = NOW + slack
    path.write_text(json.dumps(dict(good, last_attempt_at=stamp)), encoding="utf-8")
    for label, at, due in (("inside the slack, ahead of the clock", NOW, False),
                           ("one second before the cooldown ends", stamp + sched.cooldown - 1, False),
                           ("the cooldown ends, measured from the stamp", stamp + sched.cooldown, True)):
        read = s.read_state(sdlc, "voice", at)
        S.expect(bad, label, (read.kind, s.decide(sched, read, behind, at).due), ("ok", due))
    ahead = NOW + slack + 1
    path.write_text(json.dumps(dict(good, last_attempt_at=ahead, last_success_at=ahead)), encoding="utf-8")
    claim = s.claim(sdlc, "voice", sched, behind, NOW)
    S.expect(bad, "a stamp past the slack is overwritten, not waited for", (claim.started, claim.reason, claim.detail),
             (True, "state-unreadable", "future-stamp"))
    healed = doc_of(s, sdlc)
    S.expect(bad, "...and the fresh record is the clock's", (healed["last_attempt_at"], healed["last_success_at"], healed["consecutive_failures"]),
             (NOW, None, 0))
    S.expect(bad, "...so the cooldown now runs from now", s.decide(sched, s.read_state(sdlc, "voice", NOW + 60), behind, NOW + 60).reason, "cooldown")
    return bad


def due_failures(s, d):
    bad = []
    unknown = d.UNKNOWN
    sched = s.schedule(S.settings())
    S.expect(bad, "the default schedule", tuple(sched), (7200, 86400, 259200))
    n = NOW
    day = S.DAY

    def ok(attempt=None, success=None):
        doc = {"schema": s.SCHEMA_ID, "unit": "voice", "last_attempt_at": attempt, "last_success_at": success, "unit_tip": None,
               "base_tip": None, "outcome": None, "due_reason": None, "consecutive_failures": 0}
        return s.StateRead("ok", doc, None)

    rows = (
        ("cooldown holds against a huge drift", ok(n - 7199), drift_of(d, drift=50, behind=9), n, (False, "cooldown")),
        ("cooldown ends at the second", ok(n - 7200), drift_of(d, drift=50, behind=9), n, (True, "drift")),
        ("cooldown beats the backstop", ok(n - 60, n - 5 * day), drift_of(d, drift=0), n, (False, "cooldown")),
        ("a failed pass restarts the cooldown, not the backstop", ok(n - 60, n - 5 * day), drift_of(d, drift=0), n + 7141, (True, "backstop")),
        ("a recent attempt with no success", ok(n - 100), drift_of(d, drift=0), n, (False, "cooldown")),
        ("backstop one second early", ok(None, n - 86399), drift_of(d, drift=0), n, (False, "within-backstop")),
        ("backstop on the second", ok(None, n - 86400), drift_of(d, drift=0), n, (True, "backstop")),
        ("attempt long ago, success recent", ok(n - 8000, n - 8000), drift_of(d, drift=0), n, (False, "within-backstop")),
        ("dormant waits past the ordinary backstop", ok(None, n - 2 * day), drift_of(d, drift=0, dormant=True), n, (False, "within-backstop")),
        ("dormant one second early", ok(None, n - 259199), drift_of(d, drift=0, dormant=True), n, (False, "within-backstop")),
        ("dormant on the second", ok(None, n - 259200), drift_of(d, drift=0, dormant=True), n, (True, "backstop-dormant")),
        ("unknown dormancy is the ordinary backstop", ok(None, n - 86400), drift_of(d, drift=0, dormant=unknown), n, (True, "backstop")),
        ("level with its base", ok(None, None), drift_of(d, drift=99, behind=0), n, (False, "not-behind")),
        ("unknown behind counts as behind", ok(None, None), drift_of(d, drift=0, behind=unknown), n, (True, "backstop")),
        ("drift at the threshold", ok(None, n - 10), drift_of(d, drift=3), n, (True, "drift")),
        ("drift below the threshold", ok(None, n - 10), drift_of(d, drift=2), n, (False, "within-backstop")),
        ("a fractional threshold, below", ok(None, n - 10), drift_of(d, drift=3, threshold=fractions.Fraction(7, 2)), n, (False, "within-backstop")),
        ("a fractional threshold, over", ok(None, n - 10), drift_of(d, drift=4, threshold=fractions.Fraction(7, 2)), n, (True, "drift")),
        ("unknown drift never fires", ok(None, n - 10), drift_of(d, drift=unknown), n, (False, "within-backstop")),
        ("a trigger that is off never fires", ok(None, n - 10), drift_of(d, drift=100, threshold=None), n, (False, "within-backstop")),
        ("an unknown threshold never fires", ok(None, n - 10), drift_of(d, drift=100, threshold=unknown), n, (False, "within-backstop")),
        ("a boolean is not a drift", ok(None, n - 10), drift_of(d, drift=True, threshold=1), n, (False, "within-backstop")),
        ("drift outranks the dormant backstop", ok(None, n - 10), drift_of(d, drift=5, dormant=True), n, (True, "drift")),
        ("never attempted, behind: the first backstop", s.StateRead("missing", None, None), drift_of(d, drift=0), n, (True, "backstop")),
        ("unreadable beats a level unit", s.StateRead("unreadable", None, "io"), drift_of(d, drift=0, behind=0), n, (True, "state-unreadable")),
    )
    for label, read, drift, at, want in rows:
        got = s.decide(sched, read, drift, at)
        S.expect(bad, label, (got.due, got.reason), want)
    S.expect(bad, "dormant is reported", s.decide(sched, ok(), drift_of(d, dormant=True), n).dormant, True)
    custom = s.schedule(S.settings(**{"triggers.min_interval_minutes": 10, "triggers.every_hours": 2, "triggers.dormant_every_hours": 5}))
    S.expect(bad, "settings are used", tuple(custom), (600, 7200, 18000))
    for label, read, drift, want in (("custom cooldown", ok(n - 599), drift_of(d, drift=0), (False, "cooldown")),
                                     ("custom cooldown ends", ok(n - 600), drift_of(d, drift=0), (True, "backstop")),
                                     ("custom backstop", ok(None, n - 7199), drift_of(d, drift=0), (False, "within-backstop")),
                                     ("custom dormant backstop", ok(None, n - 18000), drift_of(d, drift=0, dormant=True), (True, "backstop-dormant"))):
        got = s.decide(custom, read, drift, n)
        S.expect(bad, label, (got.due, got.reason), want)
    for key in s.READS:
        for value in (True, 0, -1, 1.5, "SECRET-TEXT", 10 ** 9):
            try:
                s.schedule(S.settings(**{key: value}))
            except ValueError as exc:
                if key not in str(exc) or "SECRET" in str(exc):
                    bad.append("message for %s=%r: %s" % (key, value, exc))
            except Exception as exc:                     # noqa: BLE001
                bad.append("%s=%r raised %s" % (key, value, type(exc).__name__))
            else:
                bad.append("accepted %s=%r" % (key, value))
    for label, over in (("cooldown above the backstop", {"triggers.min_interval_minutes": 1441}),
                        ("dormant backstop below the backstop", {"triggers.dormant_every_hours": 23})):
        try:
            s.schedule(S.settings(**over))
        except ValueError:
            continue
        bad.append("accepted " + label)
    missing = dict(S.settings())
    del missing["triggers.every_hours"]
    for label, settings in (("missing key", missing), ("not a dict", [])):
        try:
            s.schedule(settings)
        except ValueError:
            continue
        bad.append("accepted " + label)
    return bad


def outcome_failures(s, d, tmp):
    bad = []
    engine = S.sibling("feature_rebase")
    S.expect(bad, "the engine's outcomes, all of them", (set(s.OUTCOMES) == set(engine.OUTCOMES), len(s.OUTCOMES)), (True, 17))
    S.expect(bad, "each outcome in one class", len(set(s.SUCCESS) | set(s.FAILURE) | set(s.NEUTRAL)), 17)
    S.expect(bad, "the classes", (s.SUCCESS, s.FAILURE, s.NEUTRAL), (("rebased", "current"), ("occupied", "unverifiable", "direct-commits",
             "conflict", "lease-refused", "failed", "would-drop", "name-too-long"), ("disabled", "no-unit", "not-adopted", "no-base", "no-branch",
             "remote-unreadable", "busy")))
    for outcome in s.OUTCOMES:
        sdlc = fresh(tmp, "outcome")
        s.record_attempt(sdlc, "voice", NOW, "backstop")
        result = s.record_outcome(sdlc, "voice", NOW + 60, outcome, **GOOD_TIPS)
        doc = doc_of(s, sdlc)
        if outcome in s.SUCCESS:
            want = (NOW + 60, 0, "a" * 40, "b" * 40)
        elif outcome in s.FAILURE:
            want = (None, 1, None, None)
        else:
            want = (None, 0, None, None)
        S.expect(bad, outcome, (result.ok, doc["last_attempt_at"], doc["outcome"], doc["last_success_at"], doc["consecutive_failures"],
                                doc["unit_tip"], doc["base_tip"]), (True, NOW, outcome) + want)
    sdlc = fresh(tmp, "sequence")
    s.record_attempt(sdlc, "voice", NOW, "backstop")
    for i in range(3):
        s.record_outcome(sdlc, "voice", NOW + 10 + i, "conflict")
    S.expect(bad, "three failures in a row", doc_of(s, sdlc)["consecutive_failures"], 3)
    s.record_outcome(sdlc, "voice", NOW + 100, "rebased", **GOOD_TIPS)
    s.record_outcome(sdlc, "voice", NOW + 200, "failed")
    after = doc_of(s, sdlc)
    S.expect(bad, "a failure after a success keeps the success clock", (after["last_success_at"], after["consecutive_failures"],
             after["unit_tip"]), (NOW + 100, 1, "a" * 40))
    s.record_outcome(sdlc, "voice", NOW + 300, "current")
    S.expect(bad, "a success with no tips keeps the old ones", (doc_of(s, sdlc)["unit_tip"], doc_of(s, sdlc)["last_success_at"]),
             ("a" * 40, NOW + 300))
    s.record_outcome(sdlc, "voice", NOW + 400, "rebased", unit_tip="c" * 64)
    S.expect(bad, "a 64-character object name is a tip", doc_of(s, sdlc)["unit_tip"], "c" * 64)
    mono = fresh(tmp, "mono")
    s.record_attempt(mono, "voice", NOW + 100, "backstop")
    s.record_outcome(mono, "voice", NOW + 150, "rebased")
    s.record_attempt(mono, "voice", NOW + 50, "drift")
    s.record_outcome(mono, "voice", NOW + 60, "rebased")
    older = doc_of(s, mono)
    S.expect(bad, "time never moves backwards", (older["last_attempt_at"], older["last_success_at"], older["due_reason"]),
             (NOW + 100, NOW + 150, "drift"))
    capped = S.variant(S.STATE, S.NO_STATE, [("FAILURES_CAP = 1000", "FAILURES_CAP = 3")])
    cap_dir = fresh(tmp, "cap")
    for i in range(5):
        capped.record_outcome(cap_dir, "voice", NOW + i, "failed")
    S.expect(bad, "the failure count is bounded", doc_of(s, cap_dir)["consecutive_failures"], 3)
    folded = fresh(tmp, "fold")
    s.record_attempt(folded, "Voice", NOW, "backstop")
    got = s.read_state(folded, "voice", NOW)
    S.expect(bad, "one file for two casings", (got.kind, got.doc and got.doc["unit"]), ("ok", "Voice"))
    sched = s.schedule(S.settings())
    pair = fresh(tmp, "pair")
    s.record_attempt(pair, "voice", NOW, "backstop")
    s.record_outcome(pair, "voice", NOW + 30, "conflict")
    held = s.decide(sched, s.read_state(pair, "voice", NOW + 60), drift_of(d, drift=0), NOW + 60)
    free = s.decide(sched, s.read_state(pair, "voice", NOW + 7200), drift_of(d, drift=0), NOW + 7200)
    S.expect(bad, "after a failure: cooldown, then the backstop is still overdue", (held.reason, free.reason), ("cooldown", "backstop"))
    for label, trial in (("unknown outcome", lambda: s.record_outcome(pair, "voice", NOW, "nonsense")),
                         ("bad tip", lambda: s.record_outcome(pair, "voice", NOW, "rebased", unit_tip="XYZ")),
                         ("upper-case tip", lambda: s.record_outcome(pair, "voice", NOW, "rebased", base_tip="A" * 40)),
                         ("tip not text", lambda: s.record_outcome(pair, "voice", NOW, "rebased", unit_tip=5)),
                         ("unknown reason", lambda: s.record_attempt(pair, "voice", NOW, "nonsense")),
                         ("boolean time", lambda: s.record_attempt(pair, "voice", True, "drift")),
                         ("float time", lambda: s.record_outcome(pair, "voice", 1.5, "failed")),
                         ("negative time", lambda: s.read_state(pair, "voice", -1))):
        try:
            trial()
        except ValueError:
            continue
        bad.append("accepted " + label)
    return bad


def claim_failures(s, d, tmp):
    bad = []
    sched = s.schedule(S.settings())
    due = drift_of(d, drift=0)
    steady = drift_of(d, drift=1, threshold=3)

    def row(c):
        return (c.started, c.due, c.reason, c.detail)

    blocked = fresh(tmp, "blocked")
    blocked.mkdir(parents=True)
    (blocked / "state").write_text("x", encoding="utf-8")
    ticks = [row(s.claim(blocked, "voice", sched, due, NOW + 60 * i)) for i in range(5)]
    S.expect(bad, "five ticks with a state directory that cannot be written start nothing", ticks, [(False, True, "state-unwritable", "unwritable")] * 5)
    (blocked / "state").unlink()
    S.expect(bad, "writable again: it starts once", row(s.claim(blocked, "voice", sched, due, NOW + 400)), (True, True, "backstop", None))
    S.expect(bad, "...then the cooldown holds", row(s.claim(blocked, "voice", sched, due, NOW + 460)), (False, False, "cooldown", None))
    S.expect(bad, "...one second before it ends", row(s.claim(blocked, "voice", sched, due, NOW + 400 + 7199)), (False, False, "cooldown", None))
    S.expect(bad, "...and it starts again when it ends", row(s.claim(blocked, "voice", sched, due, NOW + 400 + 7200)), (True, True, "backstop", None))
    corrupt = fresh(tmp, "corrupt")
    S.expect(bad, "the first claim records the attempt before any work", row(s.claim(corrupt, "voice", sched, due, NOW)), (True, True, "backstop", None))
    first = doc_of(s, corrupt)
    S.expect(bad, "...with its time and its reason", (first["last_attempt_at"], first["due_reason"], first["last_success_at"]), (NOW, "backstop", None))
    s.unit_state_path(corrupt, "voice").write_text("{not json", encoding="utf-8")
    S.expect(bad, "a corrupt file is overwritten by the same claim", row(s.claim(corrupt, "voice", sched, due, NOW + 50)),
             (True, True, "state-unreadable", "not-json"))
    S.expect(bad, "...and then reads", (s.read_state(corrupt, "voice", NOW + 50).kind, doc_of(s, corrupt)["last_attempt_at"]), ("ok", NOW + 50))
    quiet = fresh(tmp, "quiet")
    S.expect(bad, "a unit that is not due writes nothing", (row(s.claim(quiet, "voice", sched, drift_of(d, drift=0, behind=0), NOW)),
             quiet.exists()), ((False, False, "not-behind", None), False))
    S.expect(bad, "a bad unit name starts nothing and writes nothing", (row(s.claim(quiet, "a/b", sched, due, NOW)), quiet.exists()),
             ((False, True, "state-unwritable", "bad-unit"), False))
    run = fresh(tmp, "run")
    s.claim(run, "voice", sched, steady, NOW)
    s.record_outcome(run, "voice", NOW + 30, "conflict")
    S.expect(bad, "a failed pass: still cooling down", row(s.claim(run, "voice", sched, steady, NOW + 60)), (False, False, "cooldown", None))
    S.expect(bad, "a failed pass: the backstop was never reset, so it starts when the cooldown ends",
             row(s.claim(run, "voice", sched, steady, NOW + 7200)), (True, True, "backstop", None))
    s.record_outcome(run, "voice", NOW + 7260, "rebased", **GOOD_TIPS)
    S.expect(bad, "a success: inside the backstop nothing starts",
             row(s.claim(run, "voice", sched, steady, NOW + 7260 + 86399)), (False, False, "within-backstop", None))
    S.expect(bad, "a success: the backstop runs from the success", row(s.claim(run, "voice", sched, steady, NOW + 7260 + 86400)),
             (True, True, "backstop", None))
    return bad


def read_failures(s, d, tmp):
    bad = []
    sched = s.schedule(S.settings())
    unadopted = tmp / "unadopted"
    unadopted.mkdir(parents=True)
    adopted = tmp / "adopted"
    (adopted / ".sdlc" / "features").mkdir(parents=True)
    written = fresh(tmp, "written")
    s.record_attempt(written, "voice", NOW, "drift")
    raw = fresh(tmp, "raw")
    s.record_attempt(raw, "voice", NOW, "drift")
    s.unit_state_path(raw, "voice").write_text("{not json", encoding="utf-8")
    roots = (unadopted, adopted, written.parent, raw.parent)
    before = attempt_trap.snapshot(*roots)
    with mock.patch.object(sys, "dont_write_bytecode", True), attempt_trap.AttemptTrap() as trap:
        reads = [s.read_state(unadopted / ".sdlc", "voice", NOW), s.read_state(adopted / ".sdlc", "voice", NOW),
                 s.read_state(written, "voice", NOW), s.read_state(raw, "voice", NOW), s.read_state(written, "a/b", NOW)]
        for read in reads:
            s.decide(sched, read, drift_of(d), NOW)
    S.expect(bad, "the reads", [r.kind for r in reads], ["missing", "missing", "ok", "unreadable", "unreadable"])
    S.expect(bad, "no process, network, model or write was attempted", (trap.processes, trap.models, trap.network, trap.writes), ([], [], [], []))
    S.expect(bad, "no file or directory was created, changed or removed", attempt_trap.tree_diff(before, attempt_trap.snapshot(*roots)), ([], [], []))
    S.expect(bad, "an unadopted project stays untouched", (unadopted / ".sdlc").exists() or (adopted / ".sdlc" / "state").exists(), False)
    return bad


# ------------------------------------------------------------------------------------------ the listed tests

def test_state_path_fold(tmp_path):
    s = S.ustate()
    bad = path_failures(s, tmp_path)
    assert not bad, "; ".join(bad)


def test_write_is_atomic(tmp_path):
    s = S.ustate()
    bad = atomic_failures(s, tmp_path)
    assert not bad, "; ".join(bad)


def test_unreadable_states(tmp_path):
    d, s, _ = parts()
    bad = unreadable_failures(s, d, tmp_path) + future_failures(s, d, tmp_path / "future")
    assert not bad, "; ".join(bad)


def test_due_cases():
    d, s, _ = parts()
    bad = due_failures(s, d)
    assert not bad, "; ".join(bad)


def test_outcomes_and_merge(tmp_path):
    d, s, _ = parts()
    bad = outcome_failures(s, d, tmp_path)
    assert not bad, "; ".join(bad)


def test_claim_protocol(tmp_path):
    d, s, _ = parts()
    bad = claim_failures(s, d, tmp_path)
    assert not bad, "; ".join(bad)


def test_reads_never_write(tmp_path):
    d, s, _ = parts()
    bad = read_failures(S.build(S.STATE), d, tmp_path)
    assert not bad, "; ".join(bad)


MUTANTS = (   # (what the broken copy does, the check that must see it, source edits)
    ("the cooldown is ignored", "due", [('    if attempt is not None and max(0, now - attempt) < sched.cooldown:\n        return Decision(False, "cooldown", dormant, None)\n', "")]),
    ("unreadable state is not due", "unreadable", [('return Decision(True, "state-unreadable", dormant, read.reason)', 'return Decision(False, "within-backstop", dormant, read.reason)')]),
    ("the backstop runs from the last attempt", "due", [('attempt, success = doc["last_attempt_at"], doc["last_success_at"]', 'attempt, success = doc["last_attempt_at"], doc["last_attempt_at"]')]),
    ("a dormant unit waits no longer", "due", [("backstop = sched.dormant_backstop if dormant else sched.backstop", "backstop = sched.backstop")]),
    ("a level unit is still due", "due", [("    if type(drift.behind) is int and drift.behind == 0:\n        return Decision(False, \"not-behind\", dormant, None)\n", "")]),
    ("an unknown drift fires", "due", [("if type(drift.drift) is int and _number(drift.threshold) and drift.drift >= drift.threshold:", "if _number(drift.threshold) and not (drift.drift < drift.threshold if type(drift.drift) is int else False):")]),
    ("a stamp in the future is trusted and wedges", "future", [('        if value is not None and value > now + FUTURE_SLACK_SECONDS:\n            return "future-stamp"\n', ""), ("max(0, now - attempt) < sched.cooldown", "now - attempt < sched.cooldown")]),
    ("the write is in place, not atomic", "atomic", [('state.atomic_write_text(path, json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\\n")', 'path.write_text(json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\\n")')]),
    ("a symlink is followed", "atomic", [("        state.refuse_symlinks(sdlc_dir, path.relative_to(pathlib.Path(sdlc_dir)), create_parents=True)\n", "        path.parent.mkdir(parents=True, exist_ok=True)\n")]),
    ("an unwritable record still starts the pass", "claim", [('        return Claim(False, True, "state-unwritable", wrote.reason)', "        return Claim(True, True, verdict.reason, verdict.detail)")]),
    ("a failure restarts the backstop", "outcomes", [("    elif outcome in FAILURE:\n", "    if outcome in FAILURE:\n        doc[\"last_success_at\"] = _later(doc[\"last_success_at\"], now)\n    if outcome in FAILURE:\n")]),
    ("time moves backwards", "outcomes", [("    return new if old is None else max(old, new)", "    return new")]),
    ("a read makes the directory", "reads", [('state.safe_state_open(sdlc_dir, path.relative_to(pathlib.Path(sdlc_dir)), "r")', '(state.refuse_symlinks(sdlc_dir, path.relative_to(pathlib.Path(sdlc_dir)), create_parents=True), state.safe_state_open(sdlc_dir, path.relative_to(pathlib.Path(sdlc_dir)), "r"))[1]')]),
    ("two casings are two files", "path", [("registry.unit_key(name) + SUFFIX", "name + SUFFIX")]),
    ("a bad name is accepted", "path", [("    if not (isinstance(name, str) and registry.is_unit_name(name)):\n        raise registry.InvalidUnitName(\"%r is not a unit name, so it has no upkeep record\" % (name,))\n", "")]),
)


def run_check(s, kind, tmp):
    d = S.drift()
    here = tmp / ("%s-%d" % (kind, next(_RUNS)))
    if kind == "path":
        return path_failures(s, here)
    if kind == "atomic":
        return atomic_failures(s, here)
    if kind == "unreadable":
        return unreadable_failures(s, d, here)
    if kind == "future":
        return future_failures(s, d, here)
    if kind == "due":
        return due_failures(s, d)
    if kind == "outcomes":
        return outcome_failures(s, d, here)
    if kind == "claim":
        return claim_failures(s, d, here)
    return read_failures(s, d, here)


def test_state_controls(tmp_path):
    """Each guard is broken once, in a copy of the module, and the check that guards it must see it."""
    real = S.ustate()
    insane = [kind for kind in ("path", "atomic", "unreadable", "future", "due", "outcomes", "claim", "reads")
              if run_check(real, kind, tmp_path / "real")]
    assert insane == [], "the real module fails its own checks: %s" % insane
    unseen = []
    for label, kind, edits in MUTANTS:
        mutant = S.variant(S.STATE, S.NO_STATE, edits)
        try:
            seen = bool(run_check(mutant, kind, tmp_path / "mutant"))
        except Exception:                      # noqa: BLE001 - a broken copy that makes its check raise has been seen
            seen = True
        if not seen:
            unseen.append(label)
    assert unseen == [], "a broken copy passed its check: %s" % unseen


# ------------------------------------------------------------------------------------------ unlisted: facts about the platform

def test_fixture_atomic_helper_leaves_old_file(tmp_path):
    """Plain `state.py`, no new code: the shared helper this slice relies on leaves the old file whole and no temporary
    file behind when the rename fails, and it follows a symlink (which is why the module refuses one first)."""
    state = S.sibling("state")
    target = tmp_path / "a.json"
    target.write_text("old", encoding="utf-8")
    with mock.patch.object(os, "replace", side_effect=OSError("simulated")):
        try:
            state.atomic_write_text(target, "new")
        except OSError:
            pass
    assert target.read_text(encoding="utf-8") == "old"
    assert sorted(os.listdir(tmp_path)) == ["a.json"]
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    link = tmp_path / "link.json"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        return
    state.atomic_write_text(link, "through")
    assert outside.read_text(encoding="utf-8") == "through" and link.is_symlink()


def test_fixture_symlink_helper_refuses(tmp_path):
    """Plain `state.py`: `refuse_symlinks` raises for a symlink on the path and creates missing parents one level at a time."""
    state = S.sibling("state")
    base = tmp_path / ".sdlc"
    made = state.refuse_symlinks(base, pathlib.Path("state/upkeep/units/x.json"), create_parents=True)
    assert made == base / "state" / "upkeep" / "units" / "x.json" and (base / "state" / "upkeep" / "units").is_dir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (base / "state" / "link").symlink_to(outside)
    except (OSError, NotImplementedError):
        return
    try:
        state.refuse_symlinks(base, pathlib.Path("state/link/x.json"), create_parents=True)
    except OSError:
        return
    raise AssertionError("a symlinked directory was not refused")
