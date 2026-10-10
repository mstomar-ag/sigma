"""#919: the upkeep drift measure (`feature_upkeep_drift.py`).

THE LISTED TESTS (every one red by assertion before the module exists, green after) are silent, unparametrised and each
loops a table, collecting every failing case into ONE assertion message. Their checks are module-level functions that take
the module under test, so the control test can run the very same checks against deliberately broken copies and see them
fail (a guard never seen red is decoration). The tests named test_fixture_* are NOT listed: they pass at baseline because
they use plain git and no new code, and they show that each fixture really has the property it is built for."""
import fractions
import itertools
import types

import upkeep_drift_support as S

EXPECT_NOW = S.at(40)
WINDOW_15_DAYS = 15 * 24


def classifier():
    return S.sibling("feature_rebase").arrived_through_a_pull_request


def count(d, path, now, hours, ref="main", exclude=None, cap=None, runner=None):
    walk = d.walk(runner or S.run, path, ref, exclude, *([cap] if cap else []))
    return d.window_count(walk, now, hours, classifier())


# ------------------------------------------------------------------------------------------ fixtures (built once per test)

def fixtures(tmp):
    f = types.SimpleNamespace()
    f.skew = S.skewed(tmp / "skew")
    f.disorder = S.disordered(tmp / "disorder")
    f.twelve = S.repo(tmp / "twelve", S.linear(12))
    f.plain = S.repo(tmp / "plain", S.linear(4, subject=lambda i: "Tidy up part %d" % i))
    f.mixed = S.repo(tmp / "mixed", S.linear(4, subject=lambda i: "Tidy up part %d" % i if i == 2 else "Change %d (#%d)" % (i, i)))
    now = S.at(40)
    f.future = S.repo(tmp / "future", [
        S.commit("main", 1, "Old (#1)", S.at(38)), S.commit("main", 2, "Recent (#2)", S.at(39)),
        S.commit("main", 3, "Edge (#3)", now + 300), S.commit("main", 4, "Over (#4)", now + 301),
        S.commit("main", 5, "Tomorrow (#5)", now + S.DAY)])
    subjects = [
        S.commit("main", 1, "Release 1.2", S.at(1)),
        S.commit("side", 2, "Inner work (#50)", S.at(2), parents=[]),
        S.commit("main", 3, "Merge pull request #7 from a/b", S.at(3), parents=[1, 2]),
        S.commit("main", 4, "Add thing (#8)", S.at(4)),
        S.commit("main", 5, "Fix (#9) properly", S.at(5)),
        S.commit("main", 6, 'Revert "Add thing (#8)"', S.at(6)),
        S.commit("main", 7, 'Revert "Add thing (#8)" (#10)', S.at(7)),
        S.commit("main", 8, "Bump the version", S.at(8))]
    f.subjects = S.repo(tmp / "subjects", subjects)
    return f


def shallow_clone(tmp):
    """A depth-5 clone of a 40-commit history whose unit branch is 20 commits behind: git then miscounts silently."""
    commits = S.linear(40)
    commits += [S.commit("unit", 100 + i, "Unit work %d (#%d)" % (i, 500 + i), S.at(10 + i), parents=[10] if i == 1 else None)
                for i in range(1, 3)]
    source = S.repo(tmp / "origin", commits)
    dest = tmp / "clone"
    S.run(tmp, ["git", "clone", "-q", "--depth", "5", "--no-single-branch", "file://" + str(source), str(dest)])
    return dest


# ------------------------------------------------------------------------------------------ the checks (each returns a list of failures)

def date_failures(d, f):
    bad = []
    S.expect(bad, "skewed committer date, author-date count", count(d, f.skew, EXPECT_NOW, WINDOW_15_DAYS).value, 16)
    S.expect(bad, "disordered author date, whole bounded walk", count(d, f.disorder, EXPECT_NOW, WINDOW_15_DAYS).value, 15)
    c = count(d, f.future, EXPECT_NOW, 10 * 24)
    S.expect(bad, "future-dated rows: counted, ignored", (c.value, c.ignored), (3, 2))
    return bad


def unknown_failures(d, f):
    bad = []
    unknown = d.UNKNOWN
    now, hours = S.at(12), 12 * 24
    S.expect(bad, "history ends exactly at the cap", count(d, f.twelve, now, hours, cap=12).value, 12)
    S.expect(bad, "cap one below the history", (count(d, f.twelve, now, hours, cap=11).value is unknown,
                                                 count(d, f.twelve, now, hours, cap=11).reason), (True, "walk-cap"))
    S.expect(bad, "cap equal to the in-window count", count(d, f.twelve, now, 4 * 24, cap=5).value is unknown, True)
    S.expect(bad, "cap one more than the in-window count", count(d, f.twelve, now, 4 * 24, cap=6).value, 5)
    S.expect(bad, "cap far above", count(d, f.twelve, now, hours, cap=100).value, 12)
    c = count(d, f.plain, S.at(4), 10 * 24)
    S.expect(bad, "commits exist but none classify", (c.value is unknown, c.reason), (True, "none-classify"))
    c = count(d, f.twelve, S.at(100), 24)
    S.expect(bad, "a quiet window is a real zero", (c.value, c.reason), (0, None))
    c = count(d, f.mixed, S.at(4), 10 * 24)
    S.expect(bad, "partial classification is a count with a note", (c.value, c.unclassified), (3, 1))
    for label, trial in (("bool", lambda: bool(unknown)), ("order", lambda: unknown >= 3), ("less", lambda: 3 < unknown),
                         ("add", lambda: unknown + 1)):
        try:
            trial()
        except TypeError:
            continue
        bad.append("UNKNOWN allowed " + label)
    S.expect(bad, "UNKNOWN is not zero", (unknown == 0, unknown is None, repr(unknown)), (False, False, "UNKNOWN"))
    return bad


def git_failures(d, f, tmp):
    bad = []
    unknown = d.UNKNOWN
    tmp.mkdir(parents=True, exist_ok=True)

    def boom(cwd, argv):
        raise RuntimeError("fatal: simulated")

    S.expect(bad, "runner raises", (d.walk(boom, tmp, "main").reason, d.walk(boom, tmp, "main").rows), ("git-failed", ()))
    S.expect(bad, "runner raises, count", count(d, tmp, S.at(1), 24, runner=boom).value is unknown, True)
    S.expect(bad, "garbage output", d.walk(lambda c, a: "not a row at all", tmp, "main").reason, "git-failed")
    S.expect(bad, "bad timestamp", d.walk(lambda c, a: "%s\tsoon\tx" % ("a" * 40), tmp, "main").reason, "git-failed")
    for label, ref in (("dash", "-x"), ("space", "a b"), ("empty", ""), ("none", None), ("number", 5)):
        S.expect(bad, "bad ref " + label, d.walk(boom, tmp, ref).reason, "bad-argument")
    S.expect(bad, "bad exclusion", d.walk(boom, tmp, "main", "--all").reason, "bad-argument")
    S.expect(bad, "bad cap", (d.walk(boom, tmp, "main", None, 0).reason, d.walk(boom, tmp, "main", None, True).reason),
             ("bad-argument", "bad-argument"))
    S.expect(bad, "real git, unknown revision", d.walk(S.run, f.twelve, "nosuch").reason, "git-failed")
    S.expect(bad, "echoed option is not an answer", d.is_shallow(lambda c, a: "--is-shallow-repository", tmp) is unknown, True)
    S.expect(bad, "false", d.is_shallow(lambda c, a: "false", tmp), False)
    tune = d.tuning(S.settings())
    out = d.measure_unit(S.run, tmp, "main", "unit", tune, S.at(1))
    S.expect(bad, "not a repository", (out.drift is unknown, out.behind is unknown, out.notes), (True, True, ("git-failed",)))
    clone = shallow_clone(tmp / "sh")
    S.expect(bad, "real shallow repository", d.is_shallow(S.run, clone), True)
    out = d.measure_unit(S.run, clone, "origin/main", "origin/unit", tune, S.at(40))
    S.expect(bad, "shallow clone is UNKNOWN", (out.drift is unknown, out.behind is unknown, out.dormant is unknown,
                                               out.notes), (True, True, True, ("shallow",)))
    return bad


def subject_failures(d, f):
    bad = []
    walk = d.walk(S.run, f.subjects, "main")
    S.expect(bad, "first-parent rows only", len(walk.rows), 7)
    S.expect(bad, "the side commit is not walked", [r for r in walk.rows if "Inner work" in r[2]], [])
    c = d.window_count(walk, S.at(8), 10 * 24, classifier())
    S.expect(bad, "arrivals: merge (#7), squash (#8), revert ending (#10)", c.value, 3)
    S.expect(bad, "first-parent commits with no pull-request number", c.unclassified, 4)
    return bad


def math_failures(d):
    bad = []
    unknown = d.UNKNOWN
    tune = d.tuning(S.settings())
    count_ = lambda value: d.Count(value, None, 0, 0)          # noqa: E731
    cases = (
        ("148 in 21 days, 8 in 24 hours, quiet unit (the default numbers on a real history)", 148, 8, 0, 0, fractions.Fraction(4)),
        ("the window rate wins", 300, 5, 0, 0, fractions.Fraction(50, 7)),
        ("the unit adds its own rate", 148, 8, 63, 0, fractions.Fraction(11, 2)),
        ("floor", 0, 0, 0, 0, fractions.Fraction(3)),
        ("ceiling", 10000, 10000, 10000, 10000, fractions.Fraction(40)),
        ("burst wins", 0, 24, 0, 0, fractions.Fraction(12)),
    )
    for label, mw, mb, uw, ub, want in cases:
        got = d.derive_threshold(d.side_rate(count_(mw), count_(mb), tune), d.side_rate(count_(uw), count_(ub), tune), tune)
        S.expect(bad, label, (got, type(got).__name__), (want, "Fraction"))
    per_day = fractions.Fraction(148, 21)
    S.expect(bad, "per-day form of the same rule", max(per_day, fractions.Fraction(8, 1)) * 12 / 24, fractions.Fraction(4))
    S.expect(bad, "an unknown window", d.side_rate(d.Count(unknown, "walk-cap", 0, 0), count_(1), tune) is unknown, True)
    S.expect(bad, "an unknown burst", d.side_rate(count_(1), d.Count(unknown, "walk-cap", 0, 0), tune) is unknown, True)
    S.expect(bad, "an unknown rate", d.derive_threshold(unknown, fractions.Fraction(1), tune) is unknown, True)
    other = d.tuning(S.settings(**{"auto.window_days": 1, "auto.burst_window_hours": 1, "auto.target_hours": 24,
                                   "auto.floor": 1, "auto.ceiling": 1000}))
    got = d.derive_threshold(d.side_rate(count_(24), count_(0), other), d.side_rate(count_(0), count_(0), other), other)
    S.expect(bad, "another tuning", got, fractions.Fraction(24))
    return bad


def tuning_failures(d):
    bad = []
    gate = S.sibling("feature_upkeep")
    S.expect(bad, "defaults", tuple(d.tuning(gate.DEFAULTS)), (504, 24, 12, 3, 40, "auto", 336))
    S.expect(bad, "through the reader", d.tuning(gate.read({"upkeep": {"enabled": True, "auto": {"floor": 5}}}).settings).floor, 5)
    S.expect(bad, "a closed reading carries the defaults", tuple(d.tuning(gate.read({}).settings)), (504, 24, 12, 3, 40, "auto", 336))
    for key in d.READS:
        for value in (True, 0, -1, 1.5, "SECRET-TEXT", 10 ** 9, [1], {}):
            try:
                d.tuning(S.settings(**{key: value}))
            except ValueError as exc:
                if key not in str(exc) or "SECRET" in str(exc):
                    bad.append("message for %s=%r: %s" % (key, value, exc))
            except Exception as exc:                     # noqa: BLE001
                bad.append("%s=%r raised %s" % (key, value, type(exc).__name__))
            else:
                bad.append("accepted %s=%r" % (key, value))
    for value in (None, "auto", 1, 1000):
        try:
            d.tuning(S.settings(**{"triggers.drift_merges": value}))
        except ValueError:
            bad.append("refused drift_merges=%r" % (value,))
    missing = dict(gate.DEFAULTS)
    del missing["auto.floor"]
    for label, trial in (("missing key", lambda: d.tuning(missing)), ("not a dict", lambda: d.tuning([])),
                         ("floor over ceiling", lambda: d.tuning(S.settings(**{"auto.floor": 41})))):
        try:
            trial()
        except ValueError:
            continue
        bad.append("accepted " + label)
    return bad


def flow_failures(d, tmp):
    bad = []
    unknown = d.UNKNOWN
    commits = S.linear(20, subject=lambda i: "Main change %d (#%d)" % (i, i))
    commits += [S.commit("unit", 101, "Unit work 1 (#101)", S.at(12), parents=[10]),
                S.commit("unit", 102, "Unit work 2 (#102)", S.at(20)), S.commit("unit", 103, "Unit work 3 (#103)", S.at(30))]
    path = S.repo(tmp / "flow", commits)
    now = S.at(40)
    tune = d.tuning(S.settings())
    anchor = S.rev(path, "unit~1")

    def measure(**kw):
        return d.measure_unit(S.run, path, "main", "unit", kw.pop("tune", tune), kw.pop("now", now), **kw)

    out = measure()
    S.expect(bad, "no anchor: window count of the unit side", (out.drift, out.behind, out.threshold, out.dormant, out.notes),
             (12, 10, 3, False, ()))
    S.expect(bad, "the derived threshold is exact", type(out.threshold).__name__, "Fraction")
    out = measure(anchor=anchor)
    S.expect(bad, "anchored: lacks 10 + one arrival since the anchor", (out.drift, out.notes), (11, ()))
    out = measure(anchor=S.rev(path, "unit"))
    S.expect(bad, "anchored at the tip: nothing since", out.drift, 10)
    out = measure(anchor="0" * 40)
    S.expect(bad, "unknown anchor falls back to the window", (out.drift, out.notes), (12, ("anchor-fallback",)))
    out = measure(anchor="not-an-object-name")
    S.expect(bad, "an anchor that is not an object name is ignored, and says so", (out.drift, out.notes), (12, ("anchor-ignored",)))
    out = measure(anchor=anchor.upper())
    S.expect(bad, "an upper-case anchor is the same object", out.drift, 11)
    out = measure(tune=d.tuning(S.settings(**{"triggers.drift_merges": 5})))
    S.expect(bad, "integer override, not clamped", out.threshold, 5)
    out = measure(tune=d.tuning(S.settings(**{"triggers.drift_merges": 1000})))
    S.expect(bad, "integer override above the ceiling", out.threshold, 1000)
    out = measure(tune=d.tuning(S.settings(**{"triggers.drift_merges": None})))
    S.expect(bad, "null turns the trigger off", (out.threshold, out.notes), (None, ("threshold-off",)))
    out = measure(cap=5)
    S.expect(bad, "cap hit on the lacks walk", (out.drift is unknown, out.behind, out.notes[:2]), (True, 5, ("behind-lower-bound", "walk-cap")))
    out = d.measure_unit(S.run, path, "main", "main", tune, now)
    S.expect(bad, "a unit level with its base", (out.drift, out.behind, out.dormant), (0, 0, True))
    out = measure(now=S.at(60))
    S.expect(bad, "dormant: no arrival on the unit for the dormant window", out.dormant, True)
    out = measure(now=S.at(44))
    S.expect(bad, "not dormant with an arrival inside the window", out.dormant, False)
    wip = S.repo(tmp / "wip", commits[:20] + [S.commit("unit", 101, "work in progress", S.at(38), parents=[10])])
    out = d.measure_unit(S.run, wip, "main", "unit", tune, now)
    S.expect(bad, "unclassified unit commits: dormancy unknown", (out.dormant is unknown, out.drift is unknown, "dormancy-unknown" in out.notes),
             (True, True, True))
    rec = S.Recorder()
    d.measure_unit(rec, path, "main", "unit", tune, now)
    S.expect(bad, "reads per unit in auto mode (shallow, lacks, unit, base)", len(rec.calls), 4)
    rec = S.Recorder()
    d.measure_unit(rec, path, "main", "unit", tune, now, base_walk=d.walk(S.run, path, "main"), shallow=False)
    S.expect(bad, "shared reads are not repeated", len(rec.calls), 2)
    rec = S.Recorder()
    d.measure_unit(rec, path, "main", "unit", d.tuning(S.settings(**{"triggers.drift_merges": 7})), now)
    S.expect(bad, "no base read when the threshold is fixed", len(rec.calls), 3)
    return bad


def argv_failures(d, tmp):
    bad = []
    rec = S.Recorder(inner=lambda cwd, argv: "")
    walk = d.walk(rec, tmp, "main", "unit", 7)
    S.expect(bad, "the walk reads", (walk.rows, walk.truncated, walk.reason), ((), False, None))
    S.expect(bad, "the exact command", rec.calls, [["git", "log", "--first-parent", "--no-show-signature", "-n", "8",
                                                    "--format=%H%x09%at%x09%s", "main", "^unit"]])
    rec.calls.clear()
    d.walk(rec, tmp, "main")
    S.expect(bad, "no exclusion, default cap", rec.calls, [["git", "log", "--first-parent", "--no-show-signature", "-n",
                                                            str(d.WALK_CAP + 1), "--format=%H%x09%at%x09%s", "main"]])
    banned = [a for call in rec.calls for a in call if a.startswith(("--since", "--after", "--until", "--max-age", "--min-age"))]
    S.expect(bad, "no committer-date filter", banned, [])
    rec.calls.clear()
    d.is_shallow(rec, tmp)
    S.expect(bad, "the shallow question", rec.calls, [["git", "rev-parse", "--is-shallow-repository"]])
    rec.calls.clear()
    d.walk(rec, tmp, "-x")
    d.walk(rec, tmp, "main", "a\nb")
    S.expect(bad, "a refused reference never reaches git", rec.calls, [])
    return bad


# ------------------------------------------------------------------------------------------ the listed tests

def test_author_dates(tmp_path):
    d = S.drift()
    bad = date_failures(d, fixtures(tmp_path))
    assert not bad, "; ".join(bad)


def test_cap_and_unknown(tmp_path):
    d = S.drift()
    bad = unknown_failures(d, fixtures(tmp_path))
    assert not bad, "; ".join(bad)


def test_git_failures(tmp_path):
    d = S.drift()
    bad = git_failures(d, fixtures(tmp_path), tmp_path)
    assert not bad, "; ".join(bad)


def test_subject_table(tmp_path):
    d = S.drift()
    bad = subject_failures(d, fixtures(tmp_path))
    assert not bad, "; ".join(bad)


def test_threshold_math():
    d = S.drift()
    bad = math_failures(d)
    assert not bad, "; ".join(bad)


def test_settings_via_gate():
    d = S.drift()
    bad = tuning_failures(d)
    assert not bad, "; ".join(bad)


def test_measure_unit_flow(tmp_path):
    d = S.drift()
    bad = flow_failures(d, tmp_path)
    assert not bad, "; ".join(bad)


def test_walk_argv_shape(tmp_path):
    d = S.drift()
    bad = argv_failures(d, tmp_path)
    assert not bad, "; ".join(bad)


MUTANTS = (   # (what the broken copy does, the check that must see it, source edits)
    ("committer date instead of author date", "dates", [("%H%x09%at%x09%s", "%H%x09%ct%x09%s")]),
    ("stops at the first old row", "dates", [("        elif at >= start:\n            commits += 1\n",
                                              "        elif at < start:\n            break\n        else:\n            commits += 1\n")]),
    ("future-dated rows counted", "dates", [("if at > now + FUTURE_SLACK_SECONDS:", "if False:")]),
    ("cap hit reads as a number", "unknown", [('    if w.truncated and w.rows and w.rows[-1][1] >= start:\n        return Count(UNKNOWN, "walk-cap", ignored, commits - arrivals)\n', "")]),
    ("none classified reads as zero", "unknown", [('    if commits and not arrivals:\n        return Count(UNKNOWN, "none-classify", ignored, commits)\n', "")]),
    ("an unknown reads as zero", "git", [("        return Count(UNKNOWN, w.reason, 0, 0)\n    start = now", "        return Count(0, None, 0, 0)\n    start = now")]),
    ("a git failure reads as an empty history", "git", [('        return Walk((), False, "git-failed")\n    rows = []', "        return Walk((), False, None)\n    rows = []")]),
    ("a shallow clone is trusted", "git", [('    if shallow:\n        return _unknown_all("shallow")\n', "")]),
    ("second-parent commits are walked", "subjects", [('"--first-parent", "--no-show-signature", ', '"--no-show-signature", ')]),
    ("the threshold is not clamped", "math", [("    return fractions.Fraction(min(max(raw, tune.floor), tune.ceiling))", "    return fractions.Fraction(raw)")]),
    ("a settings value is not checked", "tuning", [("        why = gate.check(key, settings[key])\n        if why:\n            raise ValueError(\"%s: %s\" % (key, why))\n", "")]),
    ("an anchor is ignored", "flow", [("            if row[0] == anchor:", "            if False:")]),
    ("a committer-date filter is passed to git", "argv", [('"-n", str(cap + 1),', '"-n", str(cap + 1), "--since=1.day",')]),
)


_RUNS = itertools.count()


def run_check(d, kind, tmp, f):
    """One named check against module `d`, in a directory of its own (a repository is never built twice in one place)."""
    here = tmp / ("%s-%d" % (kind, next(_RUNS)))
    if kind == "dates":
        return date_failures(d, f)
    if kind == "unknown":
        return unknown_failures(d, f)
    if kind == "git":
        return git_failures(d, f, here)
    if kind == "subjects":
        return subject_failures(d, f)
    if kind == "math":
        return math_failures(d)
    if kind == "tuning":
        return tuning_failures(d)
    if kind == "flow":
        return flow_failures(d, here)
    return argv_failures(d, here)


def test_drift_controls(tmp_path):
    """Each guard is broken once, in a copy of the module, and the check that guards it must see it."""
    real = S.drift()
    f = fixtures(tmp_path / "fx")
    sane = [kind for kind in ("dates", "unknown", "git", "subjects", "math", "tuning", "flow", "argv")
            if run_check(real, kind, tmp_path / "real", f)]
    assert sane == [], "the real module fails its own checks: %s" % sane
    unseen = []
    for label, kind, edits in MUTANTS:
        mutant = S.variant(S.DRIFT, S.NO_DRIFT, edits)
        try:
            seen = bool(run_check(mutant, kind, tmp_path / "mutant", f))
        except Exception:                      # noqa: BLE001 - a broken copy that makes its check raise has been seen
            seen = True
        if not seen:
            unseen.append(label)
    assert unseen == [], "a broken copy passed its check: %s" % unseen


# ------------------------------------------------------------------------------------------ unlisted: the fixtures are what they claim

def test_fixture_skew_is_real(tmp_path):
    """Plain git, no new code: commit 30 keeps its author day but not its committer day, so `--since` (committer date) stops
    there and reports 10 where the author-date count is 16."""
    path = S.skewed(tmp_path / "skew")
    rows = [line.split() for line in S.run(path, ["git", "log", "--first-parent", "--format=%at %ct"]).splitlines()]
    assert len(rows) == 40 and sum(1 for a, c in rows if a != c) == 1
    start = EXPECT_NOW - 15 * S.DAY
    assert sum(1 for a, _c in rows if int(a) >= start) == 16
    since = S.run(path, ["git", "log", "--first-parent", "--since=@%d" % start, "--format=%h"]).splitlines()
    assert len(since) == 10


def test_fixture_disorder_is_real(tmp_path):
    """Plain git: one old AUTHOR date inside the history. A walk that stops at it counts 10, the whole walk 15, and
    `--since` (committer date) counts the old-authored commit as recent: 16."""
    path = S.disordered(tmp_path / "disorder")
    authors = [int(x) for x in S.run(path, ["git", "log", "--first-parent", "--format=%at"]).splitlines()]
    start = EXPECT_NOW - 15 * S.DAY
    assert sum(1 for a in authors if a >= start) == 15
    early = 0
    for a in authors:
        if a < start:
            break
        early += 1
    assert early == 10
    since = S.run(path, ["git", "log", "--first-parent", "--since=@%d" % start, "--format=%h"]).splitlines()
    assert len(since) == 16


def test_fixture_shallow_is_real(tmp_path):
    """Plain git: in a depth-5 clone `rev-list --count` answers with a wrong number and a success status, which is why a
    shallow repository must be UNKNOWN."""
    clone = shallow_clone(tmp_path)
    assert S.run(clone, ["git", "rev-parse", "--is-shallow-repository"]) == "true"
    assert S.run(clone, ["git", "rev-list", "--count", "origin/unit..origin/main"]) != "30"
