"""The upkeep drift measure: how far behind its base a long-lived feature branch is, and how fast its base moves.

LIBRARY ONLY, AND NOTHING CALLS IT YET. A later slice of the same feature (the pass, then the scheduler) imports it behind the
upkeep gate in `feature_upkeep.py`; with that gate closed nothing here runs. It reads no configuration file and never touches
the gate's block: the caller hands it the FLAT settings of `feature_upkeep.read(config)` (the gate stays the one reader), and
`tuning` checks each of the seven keys this module uses with the gate's own `check`. It also writes nothing, anywhere: every
read is a `git log` or `git rev-parse` through a runner the caller injects.

THE RUNNER. `run(cwd, argv) -> str`, raising on a non-zero exit: exactly the default of `feature_sync._run`. Nothing here picks
a runner, so a later slice can hand in one with a timeout and an unattended environment.

WHAT IS COUNTED. An ARRIVAL is a commit the existing classifier (`feature_rebase.arrived_through_a_pull_request`) accepts, read
off the FIRST-PARENT history only (without it every commit inside each merged pull request would count as well). The date of
an arrival is its AUTHOR date, filtered in Python over a bounded walk, never `git log --since`: that filters on the committer
date and stops at the first commit it dislikes, so one old committer date hides everything below it, and a rebase rewrites the
committer date of every commit it replays (the author date survives). The walk is NOT stopped at the first old commit either:
author dates can be out of order, and stopping there loses the in-window commits below it.

UNKNOWN, NEVER ZERO. A count that cannot be trusted is the sentinel `UNKNOWN`, which refuses truthiness and ordering so it can
never be read as zero or compared with a threshold by mistake: the walk hit its cap while its deepest row was still inside the
window; git failed, printed something unparseable or was handed a bad reference; commits exist but none classify (a repository
that rebase-merges); the repository is shallow (git then answers with a wrong count and a success status). Only a walk that
ended before the cap, or whose cap fell on a commit already older than the window, is a count. Partial classification is NOT
detected (a repository that classifies only some of its commits is understated, not unknown); the number of commits that did not
classify is reported in `notes` so a doctor row can show it.

THE THRESHOLD. `triggers.drift_merges` is the text auto, a whole number or null. A number is used as is (not clamped); null
turns the trigger off. Auto is (main rate + unit rate) x target hours, each rate the larger of the window rate and the burst
rate, clamped to the floor and ceiling. It is exact (fractions, never floats). The denominator of a window rate is the
CONFIGURED window, so a repository younger than its window reads a lower rate and therefore a lower threshold (the floor bounds
it): this is deliberate and stated, not hidden.

DRIFT of one unit = the arrivals on the base that the unit lacks + the arrivals on the unit since its last successful pass. The
first has no date filter (the range itself says what the unit lacks); the second is counted from the recorded anchor (the unit
tip after the last success) and, with no usable anchor, over the window.

DORMANT means no arrival reached the unit inside the dormant window. A unit with no commits of its own (level with its base, or
only behind it) has none, so a freshly cut unit reads dormant and waits the longer backstop until its first arrival; the drift
trigger is not affected. A dormancy that cannot be known is UNKNOWN, which the due rule reads as not dormant.

THE FIVE PROPERTIES. Reliability: the counts are exercised on real git fixtures with author and committer dates set apart, and
each guard was seen red against a deliberately broken copy. Scalability: per unit, two ranged reads, plus one `rev-parse` and
one base read that a caller measuring many units shares. Measured on a 30,000-commit synthetic repository with 50 units (one
machine, git 2.49, Python 3.13): units 6 to 300 commits behind cost a median 23 ms and units 40 to 2,000 behind 30 ms with the two
shared reads supplied (68 and 76 ms without them); one 5,000-row walk is about 38 ms; units 500 to 25,000 behind, most of them
beyond the cap and so UNKNOWN, cost 105 ms shared and 151 ms unshared, because `-n` bounds the output and not git's walk. The cost
is linear in units, so a caller that measures 100 units every tick pays about 3 s (shared) to 7 s (unshared) sequentially: an
extrapolation, not a measurement, and a ceiling the scheduler must budget for. Resiliency: any failure is UNKNOWN and UNKNOWN
never makes a unit due by drift (the backstop still applies). Safety: a read-only module; a reference that starts with a dash or
holds whitespace is refused before git sees it; `--no-show-signature` keeps a configured signature display out of the parsed
output; no value is echoed. Liveness: the reason for every UNKNOWN is a word in `notes`.

DEFAULT NUMBERS AND WHERE THEY COME FROM. See `SOURCES`; a test pins that every number this module reads or holds has an entry.
"""
import collections
import fractions
import importlib.util
import pathlib
import re

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


_LOADED = {}


def _sibling(name):
    """A sibling module, loaded once per process (`_load` re-executes on every call and never caches). Lazy, because the
    classifier's module loads four more, and a test substitutes an entry here."""
    if name not in _LOADED:
        _LOADED[name] = _load(name)
    return _LOADED[name]


class _Unknown:
    """The answer to 'how many?' that is not a number. It has no truth value and no order, so `if not drift` and
    `drift >= threshold` raise instead of reading it as zero."""
    __slots__ = ()

    def __repr__(self):
        return "UNKNOWN"

    def __bool__(self):
        raise TypeError("UNKNOWN has no truth value: test `is UNKNOWN`")

    def __hash__(self):
        return 0x554B4E


UNKNOWN = _Unknown()

#: Rows read by one walk. A memory and parse bound, not a time bound (see the module docstring).
WALK_CAP = 5000
#: An author date this far ahead of `now` (or more) is not an arrival: a wrong clock or a forged date must not fill a window.
FUTURE_SLACK_SECONDS = 300

READS = ("triggers.drift_merges", "triggers.dormant_days", "auto.window_days", "auto.burst_window_hours",
         "auto.target_hours", "auto.floor", "auto.ceiling")

_DESIGN = ("design default; derived on the predecessor's history, which this repository does not hold, so it is a starting "
           "point and not a measurement made here")
SOURCES = {   # every number this module reads or holds -> where it comes from
    "triggers.drift_merges": "auto (an integer overrides it, null turns the trigger off): " + _DESIGN,
    "triggers.dormant_days": "14 days without an arrival on the unit: " + _DESIGN,
    "auto.window_days": "21 days of history for the window rate: " + _DESIGN,
    "auto.burst_window_hours": "24 hours for the burst rate: " + _DESIGN,
    "auto.target_hours": "12 hours of expected arrivals tolerated before a pass: " + _DESIGN,
    "auto.floor": "3 arrivals at least: " + _DESIGN,
    "auto.ceiling": "40 arrivals at most: " + _DESIGN,
    "WALK_CAP": ("5000 rows per walk: measured at about 40 ms for one 5,000-row walk on a 30,000-commit synthetic repository "
                 "(one machine, git 2.49, Python 3.13), about 4 microseconds of parsing per row, holding 1.3 MiB (peak 2.6 MiB) "
                 "with 50-character subjects; a bound on rows, so the same on every host and not a machine resource limit; the "
                 "default 21-day window therefore reads a count up to about 240 arrivals a day, and a busier repository reads "
                 "UNKNOWN and relies on the backstop"),
    "FUTURE_SLACK_SECONDS": ("300 seconds: a judgement and not a measurement; more than ordinary clock skew between a laptop "
                             "and its remote, little enough that a wrong clock cannot fill a window"),
}

_HEX = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

Tuning = collections.namedtuple("Tuning", "window_hours burst_hours target_hours floor ceiling drift_merges dormant_hours")
Walk = collections.namedtuple("Walk", "rows truncated reason")
Count = collections.namedtuple("Count", "value reason ignored unclassified")
Drift = collections.namedtuple("Drift", "drift behind threshold dormant notes")


def tuning(settings):
    """-> Tuning, from the flat settings of `feature_upkeep.read(config)`. Each of the seven keys goes through the gate's own
    `check`, and the floor/ceiling rule through the gate's own cross-check, so this module has no second opinion about a valid
    value. A missing or invalid key raises ValueError naming the key (never the value). The caller owns whether the gate is open:
    a closed reading carries the defaults, and this function cannot tell."""
    gate = _sibling("feature_upkeep")
    if not isinstance(settings, dict):
        raise ValueError("settings must be the flat dict of feature_upkeep.read")
    for key in READS:
        if key not in settings:
            raise ValueError("missing setting " + key)
        why = gate.check(key, settings[key])
        if why:
            raise ValueError("%s: %s" % (key, why))
    for keys, fits, why in gate.CROSS_CHECKS:
        if all(k in READS for k in keys) and not fits(*[settings[k] for k in keys]):
            raise ValueError(why)
    return Tuning(settings["auto.window_days"] * 24, settings["auto.burst_window_hours"], settings["auto.target_hours"],
                  settings["auto.floor"], settings["auto.ceiling"], settings["triggers.drift_merges"],
                  settings["triggers.dormant_days"] * 24)


def _classifier():
    return _sibling("feature_rebase").arrived_through_a_pull_request


def _ref_ok(ref):
    return isinstance(ref, str) and bool(ref) and not ref.startswith("-") and not any(c.isspace() or ord(c) < 32 for c in ref)


def walk(run, cwd, include, exclude=None, cap=WALK_CAP):
    """The first-parent history of `include` (minus what `exclude` reaches) -> Walk(rows, truncated, reason). A row is
    (sha, author epoch seconds, subject). ONE read of `cap + 1` rows: more than `cap` means history continues past the cap
    (`truncated`), and the extra row is dropped. `reason` is None, or `git-failed` / `bad-argument` with no rows."""
    if not (_ref_ok(include) and (exclude is None or _ref_ok(exclude)) and type(cap) is int and cap >= 1):
        return Walk((), False, "bad-argument")
    argv = ["git", "log", "--first-parent", "--no-show-signature", "-n", str(cap + 1), "--format=%H%x09%at%x09%s", include]
    if exclude is not None:
        argv.append("^" + exclude)
    try:
        out = run(cwd, argv)
    except Exception:                       # noqa: BLE001 - any failure is UNKNOWN; the reason is a word, never the text
        return Walk((), False, "git-failed")
    rows = []
    for line in str(out or "").split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3 or not _HEX.fullmatch(parts[0]) or not (parts[1].isascii() and parts[1].isdigit()):
            return Walk((), False, "git-failed")
        rows.append((parts[0], int(parts[1]), parts[2]))
    if len(rows) > cap:
        return Walk(tuple(rows[:cap]), True, None)
    return Walk(tuple(rows), False, None)


def is_shallow(run, cwd):
    """True / False, or UNKNOWN when git does not answer with exactly true or false (an old git echoes the option back)."""
    try:
        word = str(run(cwd, ["git", "rev-parse", "--is-shallow-repository"]) or "").strip()
    except Exception:                       # noqa: BLE001
        return UNKNOWN
    if word == "true":
        return True
    if word == "false":
        return False
    return UNKNOWN


def window_count(w, now, hours, classify):
    """Arrivals whose AUTHOR date is inside the last `hours` hours of a Walk -> Count(value, reason, ignored, unclassified).
    The whole bounded walk is read; nothing stops at the first old row. A row dated more than FUTURE_SLACK_SECONDS ahead of
    `now` is ignored (counted in `ignored`). `unclassified` is the number of in-window commits that did not classify."""
    if w.reason:
        return Count(UNKNOWN, w.reason, 0, 0)
    start = now - hours * 3600
    arrivals = commits = ignored = 0
    for _sha, at, subject in w.rows:
        if at > now + FUTURE_SLACK_SECONDS:
            ignored += 1
        elif at >= start:
            commits += 1
            arrivals += 1 if classify(subject) else 0
    if w.truncated and w.rows and w.rows[-1][1] >= start:
        return Count(UNKNOWN, "walk-cap", ignored, commits - arrivals)
    if commits and not arrivals:
        return Count(UNKNOWN, "none-classify", ignored, commits)
    return Count(arrivals, None, ignored, commits - arrivals)


def range_count(w, classify):
    """Arrivals among ALL the rows of a Walk (a range with no date filter) -> Count. UNKNOWN when the walk was cut by the cap."""
    if w.reason:
        return Count(UNKNOWN, w.reason, 0, 0)
    if w.truncated:
        return Count(UNKNOWN, "walk-cap", 0, 0)
    arrivals = sum(1 for _sha, _at, subject in w.rows if classify(subject))
    if w.rows and not arrivals:
        return Count(UNKNOWN, "none-classify", 0, len(w.rows))
    return Count(arrivals, None, 0, len(w.rows) - arrivals)


def side_rate(window, burst, tune):
    """Arrivals per hour of one side (the base or the unit): the larger of its window rate and its burst rate, exact.
    UNKNOWN when either count is."""
    if window.value is UNKNOWN or burst.value is UNKNOWN:
        return UNKNOWN
    return max(fractions.Fraction(window.value, tune.window_hours), fractions.Fraction(burst.value, tune.burst_hours))


def derive_threshold(main_rate, unit_rate, tune):
    """(main rate + unit rate) x target hours, clamped to [floor, ceiling] -> a Fraction, or UNKNOWN when a rate is."""
    if main_rate is UNKNOWN or unit_rate is UNKNOWN:
        return UNKNOWN
    raw = (main_rate + unit_rate) * tune.target_hours
    return fractions.Fraction(min(max(raw, tune.floor), tune.ceiling))


def _unknown_all(note, behind=UNKNOWN):
    return Drift(UNKNOWN, behind, UNKNOWN, UNKNOWN, (note,))


def _since_anchor(unit, anchor, now, tune, classify, notes):
    """Arrivals on the unit since its anchor: the rows ahead of the anchor in the unit-only walk. With no anchor, or one that
    is not on the unit (rewritten elsewhere, or already in the base), the window count of the same rows, and `anchor-fallback`."""
    if anchor is not None:
        for index, row in enumerate(unit.rows):
            if row[0] == anchor:
                return range_count(Walk(unit.rows[:index], False, None), classify)
        if unit.truncated:
            return Count(UNKNOWN, "walk-cap", 0, 0)
        notes.append("anchor-fallback")
    return window_count(unit, now, tune.window_hours, classify)


def measure_unit(run, cwd, base_ref, unit_ref, tune, now, anchor=None, cap=WALK_CAP, classify=None, base_walk=None,
                 shallow=None):
    """Everything the due rule needs about one unit -> Drift(drift, behind, threshold, dormant, notes).

    drift      arrivals the unit lacks + arrivals on the unit since `anchor`, an int, or UNKNOWN (never zero)
    behind     first-parent commits on the base the unit lacks, an int (a lower bound when `behind-lower-bound` is in
               notes), or UNKNOWN
    threshold  a number, None when the trigger is off (null), or UNKNOWN
    dormant    True when no arrival reached the unit inside the dormant window, False when one did, UNKNOWN when not known
    notes      words, for a doctor row: the reason for every UNKNOWN, `anchor-ignored` (not an object name), `anchor-fallback`
               (not on the unit), `future-dated:N`, `unclassified:N`
    `now` is whole epoch seconds, passed in. `base_walk` and `shallow` let a caller that measures many units share the two
    reads that do not depend on the unit."""
    classify = classify or _classifier()
    if shallow is None:
        shallow = is_shallow(run, cwd)
    if shallow is UNKNOWN:
        return _unknown_all("git-failed")
    if shallow:
        return _unknown_all("shallow")
    notes = []
    if anchor is not None:
        anchor = anchor.lower() if isinstance(anchor, str) and _HEX.fullmatch(anchor.lower()) else None
        if anchor is None:
            notes.append("anchor-ignored")
    lacks = walk(run, cwd, base_ref, unit_ref, cap)
    if lacks.reason:
        return _unknown_all(lacks.reason)
    behind = len(lacks.rows)
    if lacks.truncated:
        notes.append("behind-lower-bound")
    unit = walk(run, cwd, unit_ref, base_ref, cap)
    if unit.reason:
        return _unknown_all(unit.reason, behind)
    lack = range_count(lacks, classify)
    since = _since_anchor(unit, anchor, now, tune, classify, notes)
    dormancy = window_count(unit, now, tune.dormant_hours, classify)
    dormant = UNKNOWN if dormancy.value is UNKNOWN else dormancy.value == 0
    counts = [lack, since, dormancy]
    drift = UNKNOWN if lack.value is UNKNOWN or since.value is UNKNOWN else lack.value + since.value
    if drift is UNKNOWN:
        notes.extend(c.reason for c in (lack, since) if c.value is UNKNOWN)
    mode = tune.drift_merges
    if mode is None:
        threshold = None
        notes.append("threshold-off")
    elif mode != "auto":
        threshold = mode
    else:
        base = base_walk if base_walk is not None else walk(run, cwd, base_ref, None, cap)
        windows = [window_count(base, now, tune.window_hours, classify), window_count(base, now, tune.burst_hours, classify),
                   window_count(unit, now, tune.window_hours, classify), window_count(unit, now, tune.burst_hours, classify)]
        counts.extend(windows)
        threshold = derive_threshold(side_rate(windows[0], windows[1], tune), side_rate(windows[2], windows[3], tune), tune)
        if threshold is UNKNOWN:
            notes.append("threshold-unknown")
            notes.extend(sorted({c.reason for c in windows if c.value is UNKNOWN}))
        elif windows[0].unclassified:
            notes.append("unclassified:%d" % windows[0].unclassified)
    if dormant is UNKNOWN:
        notes.append("dormancy-unknown")
    ignored = max(c.ignored for c in counts)
    if ignored:
        notes.append("future-dated:%d" % ignored)
    return Drift(drift, behind, threshold, dormant, tuple(dict.fromkeys(notes)))
