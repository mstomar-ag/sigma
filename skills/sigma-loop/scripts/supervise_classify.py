"""Exit-classifier for the overnight supervisor (zero-dep, pure, hermetically testable).

Reads the TAIL of a finished loop session's output and decides what the supervisor
does next. Four verdicts:
  done     — the loop's own success stop ("backlog-empty" / its stop report): exit.
  relaunch — a per-run budget stop, OR (#2521) a planned context hand-off: both reset per
             invocation BY DESIGN, so relaunch after a short fixed pause (the pause + the
             supervisor's max-runs cap prevent a hot ping-pong on a non-progressing backlog).
             A hand-off is never a problem — it is the orchestrating session retiring itself on
             purpose, before its own accumulated context becomes the next compaction.
  sleep    — usage-limit exhaustion WITH a parseable reset time: sleep until
             reset + jitter, then relaunch (the 12:00 -> 3:00 -> 5:30 scenario).
  backoff  — usage-limit without a parseable time, an unknown crash, or (#1084) a DONE whose
             backlog read was itself degraded (ambiguous, not confirmed empty): capped
             escalating waits (cheap retries, never a hot loop, no permanent stop
             until the supervisor's max-runs cap).

`relaunch` also covers the two endings that are neither success nor crash, and which this
classifier originally had no name for -- so it charged both the escalating CRASH ladder:

  * a session that LANDED GOALS and exited without any stop marker. This is the ordinary
    outcome of a healthy overnight run, not a fault.
  * any clean, self-declared `LOOP STOP: <reason>` -- the loop naming its own blocker, not just
    the literal word `blocked` (widened 2026-08-11: `LOOP STOP: interrupted by operator` and
    `LOOP STOP: worktree corrupt` are declared abnormal stops too, and used to be byte-for-byte
    indistinguishable from a tail with no marker at all). The declared reason is echoed LOUDLY,
    prefixed `ABNORMAL STOP`, so a declared problem is never mistaken for silence in the log.

Measured live on 2026-08-04: consecutive runs that merged a PR and that stopped blocked-on-
permissions were each scored "unclassified exit", walking the ladder to 300s -> 600s -> 1200s
-> 3600s-forever. The loop then sleeps up to an hour BETWEEN SUCCESSFUL GOALS, which inverts
what the backoff is for. Escalation must be reserved for endings we genuinely cannot explain.

Inherited limit (pre-existing, not introduced here -- `_DONE` has the identical weakness):
anchoring stops a MENTION of a marker from firing, but a marker quoted on its OWN LINE still
fires. `_ABNORMAL`'s match surface is far wider than `_DONE`'s or `_BUDGET`'s, so it is likelier
to bite in practice -- not fixed here.

The classifier never talks to any API and adds zero LLM calls — it reads text.
"""
import random, re, sys, time

# done must key on SUCCESS-specific markers only: the loop's stop REPORT ("N done, M
# parked") prints on EVERY stop — budget stops included — so it must never mean done.
# ANCHORED TO LINE START (2026-08-06). A marker is a LINE, not a substring -- SKILL.md already
# says so ("At STOP, print one machine-readable line FIRST"), this only enforces it. Unanchored,
# these patterns matched a session that was being scrupulously HONEST: having hit a blocker it
# refused to emit a stop it had not earned and said so --
#     "I'm deliberately not printing `LOOP STOP: backlog-empty` ...; neither is true, and
#      emitting one would tell your tooling something false."
# -- and the classifier read that disclaimer as the marker, exiting action=done with 28 goals
# queued. Honesty was indistinguishable from success. A mention of a marker, a quote of one, or a
# negation of one must never BE one.
_DONE = re.compile(
    r"^\s*\**\s*(?:stopped:\s*)?(?:LOOP STOP:\s*)?backlog(?:[- ]empty|\s+is\s+empty)\s*[.*]*\s*$"
    r"|^\s*DONE\s*$",
    re.I | re.M)
# #1084: `_print_pick` now emits `DONE (degraded: ...)`-shaped text on stderr when the backlog
# read gave up rather than genuinely finding nothing (GitHubSource.read_degraded()). Checked
# BEFORE `_DONE` below — that bare-`DONE`-stdout alternative would otherwise still match first in
# the merged stdout+stderr tail `supervise_daemon.py` hands this classifier, exactly as it does today
# (the masked-GraphQL-failure bug #1084 exists to fix). Anchored the same way as `_DONE`/`_BUDGET`
# — a marker is a LINE, not a substring.
_DONE_DEGRADED = re.compile(r"^\s*DONE \(degraded[^)]*\)\s*$", re.I | re.M)
# Same anchoring, same reason: the sentence that broke _DONE names `LOOP STOP: budget` in the very
# same breath, so fixing only the done-arm would leave the identical false-positive one line down.
_BUDGET = re.compile(
    r"^\s*BUDGET\s*$"
    r"|^\s*\**\s*LOOP STOP:\s*budget\b"
    r"|^\s*stopped.{0,12}budget\b"
    r"|^\s*budget (cap|stop|hit|reached)\b",
    re.I | re.M)
# Same anchoring doctrine as _DONE/_BUDGET/_ABNORMAL above (module docstring): a marker is a LINE,
# not a substring. #2521: a deliberate, healthy context-bound stop — NEVER a problem, so it must
# never fall into the generic _ABNORMAL bucket ("not a crash" — misleading for something that was
# never a crash-adjacent event in the first place) or take that bucket's longer 300s pause.
# Post-PR review, finding 1 (blocking): mirrors _BUDGET's own two-alternative shape exactly — the
# bare `^\s*HANDOFF\s*$` line is what `loop.py`'s `_print_pick` UNCONDITIONALLY prints to stdout
# (`print(kind)` for kind in ("BUDGET", "DONE", "HANDOFF")), independent of whether the session's
# own prose ever reaches the "LOOP STOP: handoff" sentence SKILL.md instructs it to print. Without
# this alternative, a HANDOFF-triggered session cut off before finishing that sentence (truncation,
# interruption) falls through to the generic ABNORMAL/300s bucket instead of the intended 60s
# "this was healthy" pause — the exact LIVENESS distinction (AGENTS.md: "a component that has DIED
# must be distinguishable from one with nothing to do") this whole feature exists to preserve.
_HANDOFF = re.compile(
    r"^\s*HANDOFF\s*$"
    r"|^\s*\**\s*LOOP STOP:\s*handoff\b",
    re.I | re.M)
# #1600: a resource HOLD is a pause, not a stop. `loop.py` prints the bare `HOLD` line on stdout and
# `HOLD (<reason>; retry ~Ns; ...)` on stderr; the retry figure is the only part of the reason that
# can steer the pause, and it is clamped.
_HOLD = re.compile(r"^\s*HOLD\s*$", re.I | re.M)
_HOLD_DETAIL = re.compile(r"^\s*HOLD \((.*)\)\s*$", re.I | re.M)
_HOLD_RETRY = re.compile(r"retry ~(\d{1,5})s")
_HOLD_RETRY_RANGE = (30, 900)
_LIMIT = re.compile(r"(usage|rate).{0,3}limit|limit (reached|exhausted|hit)|out of (usage|quota)|"
                    r"hit your.{0,12}limit|quota exceeded", re.I)
_RESET_AT = re.compile(r"reset[s]?\s*(?:at\s*)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.I)
# A clean, self-declared stop -- the loop naming its own blocker, whatever it is. NOT a crash.
# Anchored to line start, same doctrine as _DONE/_BUDGET above: a marker is a LINE, not a
# substring, so a MENTION or a QUOTE of one must never BE one (see the module docstring for the
# incident that forced this). `(.*)$`, NOT `(\S.*)$` -- a bare `LOOP STOP:` line with nothing
# after the colon must still match, or it silently falls into the generic "unrecognised" bucket
# and loses the loud reason (an empty/whitespace capture is defaulted at the call site instead).
_ABNORMAL = re.compile(r"^\s*\**\s*LOOP STOP:\s*(.*)$", re.I | re.M)
# The stop report, matched on its FULL shape ("N done, M parked") rather than a bare "N done",
# which prose like "step 1 done" would trip. The captured count is checked > 0 at the call site:
# the report's mere presence is not progress, and treating it as such would reset the supervisor's
# attempt counter every cycle and hot-loop on a genuinely stuck backlog.
_PROGRESS = re.compile(r"(\d+)\s+done,\s*\d+\s+parked", re.I)
# The ONLY endings that earn the escalating ladder. Everything else that produced coherent output
# gets a flat pause -- see the `classify` fall-through for why the default had to be inverted.
_CRASH_SIG = re.compile(
    r"traceback \(most recent call last\)|^killed\b|segmentation fault|out of memory|"
    r"\boom\b(?!er)|fatal error|panic:|command not found|core dumped|"
    r"connection reset|broken pipe|SIGKILL|SIGSEGV",
    re.I | re.M)

_BUDGET_PAUSE = 60
_HANDOFF_PAUSE = 60   # same short pause BUDGET gets — a hand-off is not a problem either
_LIMIT_FALLBACK = [1800, 3600, 3600, 3600]      # unparseable limit: retry cheaply, capped waits
_CRASH_BACKOFF = [300, 600, 1200, 3600]         # unknown crash: escalate, cap at 1h
_DEGRADED_BACKOFF = [300, 600, 1200, 3600]      # unconfirmed empty: same escalating shape as a
                                                 # crash — we genuinely don't know if it's real
_ABNORMAL_PAUSE = 300   # a fresh session often clears the cause; MAX_RUNS still bounds the retries
_PROGRESS_PAUSE = 60    # it worked -- get the next goal started
_UNKNOWN_PAUSE = 120    # named nothing, broke nothing: retry steadily, never escalate
_JITTER = (120, 300)                            # never thunder exactly on the reset minute


def _seconds_until(hour, minute, ampm, now):
    """Seconds from `now` (epoch) to the NEXT local occurrence of hour:minute."""
    lt = time.localtime(now)
    h = hour % 12 + (12 if (ampm or "").lower() == "pm" else 0) if ampm else hour
    target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, minute, 0,
                          lt.tm_wday, lt.tm_yday, -1))
    if target <= now:
        target += 24 * 3600
    return int(target - now)


def classify(tail_text, attempt=0, now=None, rng=None):
    """(action, sleep_seconds, reason). `now`/`rng` are injectable for tests."""
    now = time.time() if now is None else now
    jitter = (rng or random).randint(*_JITTER)
    text = tail_text or ""
    if _DONE_DEGRADED.search(text):
        wait = _DEGRADED_BACKOFF[min(attempt, len(_DEGRADED_BACKOFF) - 1)]
        return ("backoff", wait, "backlog read degraded — could not confirm the backlog is "
                                  "actually empty, treating as a transient failure")
    if _DONE.search(text):
        return ("done", 0, "the loop reported its own success stop")
    if _LIMIT.search(text):
        m = _RESET_AT.search(text)
        if m:
            secs = _seconds_until(int(m.group(1)), int(m.group(2) or 0), m.group(3), now)
            return ("sleep", secs + jitter, f"usage limit — resuming after the stated reset (+{jitter}s jitter)")
        wait = _LIMIT_FALLBACK[min(attempt, len(_LIMIT_FALLBACK) - 1)]
        return ("backoff", wait, "usage limit with no parseable reset time — capped retry")
    if _HOLD.search(text):
        detail = _HOLD_DETAIL.search(text)
        body = detail.group(1) if detail else ""
        found = _HOLD_RETRY.search(body)
        wait = int(found.group(1)) if found else _HOLD_RETRY_RANGE[0]
        wait = min(max(wait, _HOLD_RETRY_RANGE[0]), _HOLD_RETRY_RANGE[1])
        body = body.replace("sleep=", "sleep-").replace("action=", "action-")
        return ("sleep", wait, "resource hold: " + (body or "no reason given") + " — not a stop; waiting for room")
    if _BUDGET.search(text):
        return ("relaunch", _BUDGET_PAUSE, "per-run budget stop — budgets reset on relaunch by design")
    if _HANDOFF.search(text):
        return ("relaunch", _HANDOFF_PAUSE, "planned context hand-off — resuming with a fresh session")
    # Below the usage-limit and budget checks ON PURPOSE. A session that lands a goal and THEN
    # hits a limit must sleep until the reset -- relaunching into a closed door would burn the
    # remaining runs for nothing. ABOVE _PROGRESS ON PURPOSE too: a session that landed goals and
    # THEN declared an abnormal stop must surface the declared problem, not be absorbed into
    # "landed N goal(s)" -- this ordering is load-bearing, do not move this branch.
    # A declared `LOOP STOP:` must NOT mask a co-occurring REAL crash (#746). Old `_BLOCKED` matched
    # only the literal word 'blocked' (narrow); `_ABNORMAL` matches ANY `LOOP STOP:` line (wide), so
    # a crash that ALSO emitted a declared stop used to classify ('relaunch', 300) and escape the
    # escalating `_CRASH_BACKOFF` ladder, in either text order. The crash probe here runs against the
    # tail with the declared-stop LINE(S) STRIPPED, so an INDEPENDENT crash signature (a traceback on
    # its own lines) outranks the declared stop and falls through to the crash ladder below -- while a
    # CLEAN declared stop whose REASON merely MENTIONS a crashy phrase ("LOOP STOP: connection reset
    # by peer") still relaunches, because that phrase lives only inside the stripped stop line. The
    # ordering above (_DONE/_LIMIT/_BUDGET/_PROGRESS unchanged) is untouched: only the declared-stop
    # vs crash precedence is settled here.
    #
    # `crash_present` is computed ONCE here and reused below by _PROGRESS too (#941, sibling of
    # #746): #746 only gated _ABNORMAL against a co-occurring real crash: _PROGRESS ("N done, M
    # parked", count>0) sat below it with NO crash check of its own, so a progress-shaped tail with
    # an independent traceback -- whether or not a LOOP STOP line was ALSO present -- still fell
    # into "progress, not a crash" and relaunched on a flat 60s pause, masking the crash exactly
    # the way _ABNORMAL used to. Stripping the declared-stop line(s) before the crash probe (same
    # as _ABNORMAL's own check) keeps the counterweight intact: a declared stop whose REASON merely
    # MENTIONS a crashy phrase ("LOOP STOP: connection reset by peer") must not itself trip this
    # gate, since that phrase would otherwise only ever live inside the stripped line.
    crash_present = bool(_CRASH_SIG.search(_ABNORMAL.sub("", text)))
    hit = _ABNORMAL.search(text)
    if hit and not crash_present:
        # `reason_text` is session-authored free text -- the ONLY place in this module where such
        # text enters `reason` (see module docstring). It crosses into supervise_daemon.py's
        # `extract_sleep_seconds`, which extracts `sleep=<n>` from the printed verdict line: an
        # unsanitised `sleep=999999` (or `action=...`) inside a declared reason could otherwise be
        # read as the verdict's OWN field downstream (PR #744 blocking finding, #176).
        # supervise_daemon.py's own extraction is anchored against this too (its own ANCHORED
        # regex; that anchor is the PRIMARY defence and is regression-pinned by
        # test_the_shell_anchor_alone_resists_an_unsanitised_reason),
        # so this boundary scrub is defence-in-depth against any accidental reuse of `reason`
        # elsewhere. It therefore neutralises ONLY the two literal tokens a downstream key=value
        # reader could misattribute to the verdict's own fields -- `sleep=` and `action=` -- NOT
        # every `=`. #745: the old blanket `.replace("=", "-")` also mangled legitimate diagnostics a
        # real stop reason carries (`threshold=5 exceeded` -> `threshold-5 exceeded`, config dumps,
        # env vars), silently corrupting operator-facing detail; narrowing keeps the guard while
        # preserving that fidelity. `.rstrip("* \t")` also strips a bold-wrapped marker's trailing
        # `**` (a session writing `**LOOP STOP: worktree corrupt**`), consistent with `_DONE`'s
        # existing handling of trailing decoration.
        reason_text = (hit.group(1) or "").strip().rstrip("* \t")
        reason_text = reason_text.replace("sleep=", "sleep-").replace("action=", "action-")
        reason_text = reason_text or "no reason given"
        return ("relaunch", _ABNORMAL_PAUSE,
                f"ABNORMAL STOP: {reason_text} — not a crash; a fresh session often clears the cause")
    # Gated on the SAME crash-detection check as _ABNORMAL just above (#941): a progress-shaped
    # tail ("N done, M parked") is not immune to co-occurring with a genuine, independent crash
    # signature -- the report prints on ordinary exits, but nothing stops a session from also
    # dying mid-write with a traceback right after landing a goal. Without this gate that traceback
    # was silently downgraded to "progress, not a crash" and never reached the escalating ladder.
    landed = _PROGRESS.search(text)
    if landed and int(landed.group(1)) > 0 and not crash_present:
        return ("relaunch", _PROGRESS_PAUSE,
                f"session landed {landed.group(1)} goal(s) — progress, not a crash")
    # DEFAULT INVERTED (2026-08-04). This used to escalate on anything it could not name, which
    # made the common case pathological: a headless `claude -p` session prints only its final
    # message, so a run that did real work and exited mid-goal waiting on CI left a tail like
    # "Polling in the background; I'll report when the gate clears" -- no marker, no report. That
    # was scored a crash, and three of them in a row put the supervisor to sleep for an hour
    # between SUCCESSFUL goals.
    #
    # An ending we cannot name is not evidence of a crash. Escalation is now reserved for endings
    # that look genuinely broken, plus an empty tail (which tells us nothing at all and is the one
    # silence worth treating as failure). Anything else relaunches on a flat pause; the
    # supervisor's MAX_RUNS cap remains the backstop against a pathological loop.
    if (not text.strip()) or _CRASH_SIG.search(text):
        wait = _CRASH_BACKOFF[min(attempt, len(_CRASH_BACKOFF) - 1)]
        return ("backoff", wait, "crash signature — escalating backoff")
    return ("relaunch", _UNKNOWN_PAUSE,
            "unrecognised but clean exit — relaunching without escalation")


USAGE = "usage: supervise_classify.py <tail-file> [attempt]"


def main(argv):
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    if len(argv) < 2:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        text = open(argv[1], encoding="utf-8", errors="ignore").read()
    except OSError:
        text = ""
    action, secs, reason = classify(text, int(argv[2]) if len(argv) > 2 else 0)
    # COUPLED to supervise_daemon.py's anchored `extract_sleep_seconds` regex: that regex depends on
    # this EXACT shape -- `action=<...> sleep=<...> reason=<...>`, in this field order, with these
    # literal spaces. Reorder, rename, or insert a field here and the regex silently stops matching
    # -- the daemon's `${secs:-1800}`-equivalent fallback then substitutes a SILENT 30-minute pause,
    # with no error anywhere. Change this shape and supervise_daemon.py's extractor together, or
    # not at all.
    print(f"action={action} sleep={secs} reason={reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
