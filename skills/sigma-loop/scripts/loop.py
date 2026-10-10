"""Park-&-continue loop driver. run_loop ties the backlog source + run_goal + state; start/next/record are
the agent's CLI hooks into the same primitives. Budgets (all per-run, reset each invocation):
max_iterations bounds admitted goals in one loop session; max_minutes enforces by wall-clock from the run's
start; max_tokens enforces against the host-REPORTED spend counter (`loop.py spend <dir> <n>` — the
loop never measures spend itself; no reports == no enforcement). An absent/zero key enforces nothing
for any of the three, so a config without it behaves exactly as before. The irreversible-action gate
is enforced by /sigma-loop SKILL.md prose. Claim and outcome are mirrored to the team ledger
(ledger.py) when `ledger.enabled` is on — every such call is fail-open, so a ledger problem can never
stop a run."""
import os, sys, pathlib, importlib.util, time, subprocess, inspect, json, re, tempfile, hashlib, uuid
import contextlib
import errno

try:
    import fcntl                    # POSIX only — see _try_acquire_claim_lock's docstring
except ImportError:
    fcntl = None

try:                    # portable output: force UTF-8 so the plugin's own non-ASCII (arrows, em-dashes)
    import sys as _sys  # doesn't garble to '?' or crash on a non-UTF-8 console (the Windows cp1252
    _sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")   # default); a stream without
    _sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")   # reconfigure is left as-is
except Exception:
    pass

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def _host_model_config(config):
    """Use the Codex-only model gate without mutating shared repo config."""
    effective = dict(config)
    env = os.environ
    codex = (env.get("SIGMA_HOST") == "codex" or
             (env.get("SIGMA_HOST") != "claude" and
              (env.get("CODEX_THREAD_ID") or env.get("CODEX_SESSION_ID")) and
              not (env.get("CLAUDECODE") or env.get("CLAUDE_CODE_SESSION_ID"))))
    overrides = config.get("model_selection_host_overrides")
    if codex and isinstance(overrides, dict) and overrides.get("codex") in ("auto", "off"):
        effective["model_selection"] = overrides["codex"]
    return effective


state = _load("state")
sources = _load("sources")          # backlog source: local files or GitHub issues (config-selected)
legacy = _load("legacy")            # #239: markers written under the plugin's previous name
ledger = _load("ledger")            # team record (config-gated, default OFF; every call is fail-open)
work = _load("work")                # per-goal worktree/branch/PR (config-gated, default OFF)
tamper_scan = _load("tamper_scan")  # #1937: diff parser; #1933 reuses changed_test_files()
flake_check = _load("flake_check")  # #1933: 3x varied-order re-run of a goal's changed tests
witness = _load("witness")          # #1934: red-before-green as data (+ #1935's vocabulary)
diff_revert = _load("diff_revert")  # #2240: mutmut-free kill control -- writes #1935's `mutation`
                                     # witness kind, which nothing else in this repo ever produces
retention = _load("retention")      # #457: bounded prune of closed goals' action-log + witness streams
actionlog = _load("actionlog")      # local-only action trace (config-gated, default OFF; never the ledger)
timing_store = _load("timing_store")  # working time; the ONLY store here that is never config-gated
decision_tier = _load("decision_tier")   # #953: needs_decision-park tier classifier (config-gated, #952)
feature_labels = _load("feature_labels") # #1468: attach a declared unit's label at pick, never create one
shell_policy = _load("shell_policy")


def _coexist_notice(sdlc_dir, surface, once=False):
    """#240/#314: the plugin under the previous name also active here is a NOTICE, never a
    refusal -- `coexist.gate` prints its one line on stderr (silenced by `SIGMA_ALLOW_COEXIST=1`).
    `once`: a per-verb surface (the watcher spawn, claim, record) says it once per run, not per
    verb (#251). Fail-open: a detector that cannot run must never stop the verb."""
    try:
        _load("coexist").gate(sdlc_dir, surface, once=once)
    except Exception:                # noqa: BLE001 - see the docstring
        pass
    try:
        # #514: observe the registry sheet at every gated verb: take Sigma's copy of it, or say loudly
        # that the previous plugin's fold emptied it. Writes nothing but that copy. One stat when the
        # repository has no registry. Not silenced by SIGMA_ALLOW_COEXIST: it is protection, not a notice.
        if (pathlib.Path(sdlc_dir) / "features" / "index.json").is_file():
            _load("feature_registry").guard_sheet(sdlc_dir)
    except Exception:                # noqa: BLE001 - fail-open, like the notice above
        pass


def _ensure_watcher(sdlc_dir, config, spawn=None):
    """Start the ledger watcher for this repo if it isn't already, so a loop trigger shares its trail
    without a separate step. Otherwise entries only ever get pushed when someone remembers to run the
    watcher — the exact way a busy team's ledger goes silent. Safe to call on every trigger:
    `watch_daemon.py` self-guards against a second copy (a live watch.pid). Only when the ledger is
    enabled AND actually a worktree (nothing to publish otherwise), and fail-open — a watcher we
    cannot start must never stop a run. `spawn` is injectable for tests."""
    try:
        if not ledger.enabled(config):
            return
        sync = _load("sync")
        if not sync.is_worktree(sdlc_dir):
            return
        _coexist_notice(sdlc_dir, "watcher start", once=True)   # #314: the shared lock decides
        launch = spawn or (lambda: subprocess.Popen(
            [sys.executable, str(_HERE / "watch_daemon.py"), str(sdlc_dir)],
            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        launch()
    except Exception:                # noqa: BLE001 - fail-open by design; the run matters, the watcher is a convenience
        pass


def _ledger_delivery_cooldown_path(sdlc_dir):
    """Mirrors `sync.heartbeat_path`'s own mtime-based shape. Touched once per real escalation
    attempt (a self-publish, OR the live-watcher-still-behind warning below) so
    `_ensure_ledger_delivery` can bound BOTH a synchronous network attempt (plan-review finding
    2: no timeout anywhere in the git layer, up to 5 retries -- calling it on every trigger would
    turn a silent freshness bug into a repeatedly-hanging loop) and its own stderr line to once
    per `cooldown_s`, independent of how often a loop trigger fires."""
    return pathlib.Path(sdlc_dir) / "state" / "ledger-delivery-attempt"


def _ensure_ledger_delivery(sdlc_dir, config, threshold=15, publish_fn=None, cooldown_s=300):
    """A loop trigger checks whether this clone's ledger backlog has grown past `threshold` real
    entries and, if so, tries once (bounded) to clear it -- one level up from `_ensure_watcher`'s
    own "a loop trigger keeps the ledger flowing on its own": that starts the watcher that
    publishes on its OWN tick; this notices when publishing has quietly STOPPED working despite a
    live watcher, or has no watcher to rely on at all (#2392's sibling incident shape).

    Config-gated and fail-open exactly like `_ensure_watcher` (`ledger.enabled`,
    `sync.is_worktree` -- nothing to escalate without a ledger worktree to escalate from), and
    every internal exception is swallowed: a diagnostic must never cost a run.

    THE COMMON CASE COSTS ONE LOCAL `git status`. `sync.pending_entry_count` is the only call made
    once the backlog is at or below `threshold`, and this returns immediately after it.

    THE COOLDOWN, once above threshold. `_ledger_delivery_cooldown_path`'s mtime bounds every real
    attempt below -- a self-publish, or the live-watcher warning -- to once per `cooldown_s`, so a
    run dispatching many triggers cannot turn this into a repeatedly-hanging synchronous publish or
    a flooded stderr (plan-review finding 2). An absent marker (never attempted) proceeds
    immediately, the same "no heartbeat yet" reading `watcher_liveness` gives its own marker.

    THE LIVENESS GATE, once past cooldown. Defers to a genuinely live watcher rather than racing
    it for the ledger worktree's git index lock -- the SAME reason `publish_after_write` already
    gives for the identical check -- but a live watcher is not automatically reported as benign:
    plan-review finding 3 is exactly a live, ticking watcher whose publishes keep failing, so a
    backlog still above threshold once the cooldown elapses is reported even then, naming the
    watcher as context rather than as an all-clear.

    ONE CLEAR STDERR LINE on any non-benign outcome, naming the real entry count and pointing at
    `/sigma-doctor` -- including `sync.publish`'s own `"nothing to publish"`, which plan-review
    finding 1 established is not always benign: `publish()` only ever commits+pushes files newly
    staged by THIS call, so a clone already holding an earlier commit that failed to push reports
    "nothing to publish" (nothing NEW to stage) while that earlier commit sits there unpushed --
    re-measured here via a second `pending_entry_count` call, never inferred from the string alone.

    `publish_fn` is `sync.publish` by default, injectable for tests."""
    try:
        if not ledger.enabled(config):
            return
        sync = _load("sync")
        if not sync.is_worktree(sdlc_dir):
            return
        count = sync.pending_entry_count(sdlc_dir, config)
        if count is None or count <= threshold:
            return
        marker = _ledger_delivery_cooldown_path(sdlc_dir)
        try:
            age = time.time() - marker.stat().st_mtime
        except OSError:
            age = None
        if age is not None and age < cooldown_s:
            return                       # bounded: at most one real attempt per cooldown window
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        except OSError:
            pass                         # a marker we cannot write must not stop the check itself
        state_now, watcher_age = sync.watcher_liveness(sdlc_dir, config)
        if state_now == "live":
            print("loop: ledger backlog is %d entries -- the ledger watcher is live (last tick "
                  "%s) but has not cleared it; run /sigma-doctor to check why"
                  % (count, sync._ago(watcher_age)), file=sys.stderr)
            return
        try:
            result = (publish_fn or sync.publish)(sdlc_dir, config)
        except Exception as exc:             # noqa: BLE001 - defense in depth: publish_fn itself
            print("loop: ledger backlog is %d entries and could not be published (%s); run "
                  "/sigma-doctor" % (count, exc), file=sys.stderr)
            return
        if result == "published":
            return
        if result == "nothing to publish":
            still = sync.pending_entry_count(sdlc_dir, config)
            if still is None or still <= threshold:
                return
            print("loop: ledger backlog is %d entries -- publish had nothing new to stage but the "
                  "backlog persists (an earlier commit likely failed to push); run /sigma-doctor"
                  % still, file=sys.stderr)
            return
        print("loop: ledger backlog is %d entries and could not be published (%s); run "
              "/sigma-doctor" % (count, result), file=sys.stderr)
    except Exception:                # noqa: BLE001 - fail-open by design, same as _ensure_watcher
        pass


#: THE ENFORCEMENT REGISTRY (#2740). Every gate this module implements, one entry each, read (never
#: imported) by skills/sigma-doctor/scripts/enforcement_table.py to render docs/enforcement.md.
#: A module-level function whose name ends `_refusal`/`_gate`/`_hold`/`_guard`, is `gate`, or
#: contains `blocked_by` must be listed here or in ENFORCEMENT_EXEMPT, or
#: tests/test_enforcement_table.py fails. After editing: regenerate the doc (command in its header).
#: Pure literals only (str/tuple/bool/None): the reader is `ast.literal_eval`, so it cannot follow a
#: name or a call. Text fields carry no `|` and no newline -- they land in a Markdown table cell.
ENFORCEMENT_GATES = (
    {"control": "Dependency hold at pick", "function": "_pick_dependency_hold",
     "kind": "python-gate", "hosts": "all", "enabled_by": ("discovery.dependency_gate.mode",),
     "settings": (),
     "mechanism": "leaves a goal unclaimed (no label, no lock, no comment) while a prerequisite it "
                  "declares is still open, instead of claiming it and parking",
     "condition": "any `mode` other than `off` reads as on, including an absent key or a typo "
                  "(`_dependency_gate_mode`)"},
    {"control": "Per-run budget: wall-clock and token ceilings", "function": "_budget_resource_reason",
     "kind": "python-gate", "hosts": "all", "enabled_by": ("budget.max_minutes",),
     "settings": ("budget.max_tokens", "budget.max_codex_raw_tokens"),
     "mechanism": "stops admitting goals at a phase boundary once the session's minutes, priced "
                  "Claude-equivalent tokens or measured Codex raw tokens reach a ceiling; each "
                  "ceiling enforces only when set to a positive number; there is no currency cap",
     "condition": "the on/off cell keys off `budget.max_minutes` alone -- the token ceilings still "
                  "enforce with minutes zeroed, and vice versa"},
    {"control": "Per-run budget: goals per session", "function": "_session_count_stop",
     "kind": "python-gate", "hosts": "all", "enabled_by": ("budget.max_iterations",),
     "settings": ("handoff.after_goals",),
     "mechanism": "stops admitting goals once this session's settled plus in-flight admissions "
                  "reach the ceiling",
     "condition": "`handoff.after_goals` is checked first and wins a tie: at the shipped 20/20 the "
                  "stop reads HANDOFF, not BUDGET"},
)
ENFORCEMENT_EXEMPT = ()


def _budget_reason(cursor, budget):
    """None when nothing is spent; else a short, human-readable string naming the FIRST tripped
    ceiling (checked in the same iterations -> minutes -> tokens priority `_budget_spent` has
    always used) and its observed-vs-configured numbers, e.g. "elapsed 720min >= max_minutes 480"
    (#411) — so a caller that already prints the bare `BUDGET` token can also say WHICH ceiling
    tripped, instead of leaving a human (or an orchestrating session) to hand-compare
    `state.load_cursor()` against `config.json`'s own `budget` block to find out. Only the FIRST
    tripped ceiling is named, matching `_budget_spent`'s own short-circuit order — still a genuine
    answer even on the rare tick where more than one ceiling trips at once."""
    max_iterations = budget.get("max_iterations")
    if max_iterations and cursor["run_iteration"] >= max_iterations:
        return f"{cursor['run_iteration']} iterations >= max_iterations {max_iterations}"
    minutes = budget.get("max_minutes")
    if minutes and cursor["run_started_at"]:
        elapsed = (time.time() - cursor["run_started_at"]) / 60.0
        if elapsed >= minutes:
            return f"elapsed {int(elapsed)}min >= max_minutes {minutes}"
    tokens = budget.get("max_tokens")
    if tokens and cursor["run_tokens"] >= tokens:
        return f"{cursor['run_tokens']} tokens >= max_tokens {tokens}"
    codex_tokens = budget.get("max_codex_raw_tokens")
    if codex_tokens and cursor.get("run_codex_raw_tokens", 0) >= codex_tokens:
        return (f"{cursor['run_codex_raw_tokens']} Codex raw tokens >= "
                f"max_codex_raw_tokens {codex_tokens}")
    return None


def _budget_spent(cursor, budget):
    """True when ANY configured ceiling is reached. Absent/zero keys never enforce —
    a config without them behaves exactly as before this check existed. A thin wrapper over
    `_budget_reason` (#411) so the two can never independently drift on WHICH three checks, or
    their order. Production admission now uses the session counter for max_iterations and
    `_budget_resource_reason` for minutes/tokens. Retained for its own test matrix, in
    particular the F18/#349 hardcoded-20 regression pin
    (`test_budget_spent_max_iterations_absent_or_zero_enforces_nothing`), not for any caller."""
    return _budget_reason(cursor, budget) is not None


def _budget_resource_reason(cursor, budget):
    """A real resource ceiling — wall-clock (max_minutes) or spend (max_tokens, now genuinely fed
    by `phase_report.py cmd_end`, #2515) — checked INDEPENDENTLY of max_iterations, unlike
    `_budget_reason`'s own iterations-first short-circuit. #2521: this is the ONE budget case
    checked before HANDOFF in `_next()` (see that function's own comment for why) — deliberately
    NOT derived from `_budget_reason` by elimination (checking "did iterations alone explain the
    reason"), because on the SHIPPED DEFAULT config `handoff.after_goals` and `budget.max_iterations`
    are the identical number (20) watching the identical counter, so max_iterations tripping at the
    exact same tick as a genuine max_tokens/max_minutes breach is the COMMON case, not a rare corner
    one (a `large`-lane drain can plausibly burn `max_tokens`'s own 500k-token shipped default
    within 20 goals, given a fresh subagent's own ~70-77k-token starting cost alone). Eliminating on
    "iterations already explains it" would silently mask that live breach behind HANDOFF or the
    iterations reason, forever, one hand-off at a time — precisely the bug this function exists to
    close. The minutes/tokens conditions below intentionally mirror `_budget_reason`'s own two
    branches byte-for-byte (never re-derived independently), so the message text is identical
    whichever function reports it; `test_budget_resource_reason_message_shape_matches_budget_
    reasons_own` (tests/test_loop.py) is the drift guard for that coupling — re-copy this pair if
    `_budget_reason`'s own minutes/tokens branches ever change, don't let them silently diverge."""
    minutes = budget.get("max_minutes")
    if minutes and cursor["run_started_at"]:
        elapsed = (time.time() - cursor["run_started_at"]) / 60.0
        if elapsed >= minutes:
            return f"elapsed {int(elapsed)}min >= max_minutes {minutes}"
    tokens = budget.get("max_tokens")
    if tokens and cursor["run_tokens"] >= tokens:
        return f"{cursor['run_tokens']} tokens >= max_tokens {tokens}"
    codex_tokens = budget.get("max_codex_raw_tokens")
    if codex_tokens and cursor.get("run_codex_raw_tokens", 0) >= codex_tokens:
        return (f"{cursor['run_codex_raw_tokens']} Codex raw tokens >= "
                f"max_codex_raw_tokens {codex_tokens}")
    return None


#: Same order of magnitude as `budget.max_iterations`'s own already-shipped default (20) —
#: deliberately NOT a freshly measured number. #2521's own research flagged the real ratio (how
#: many goals before an orchestrator's accumulated context exceeds a fresh subagent's own ~70-77k
#: starting cost) as not measured; this is a reasonable, honestly-labelled starting point, not a
#: derived optimum. Real tuning is named as follow-up once a real multi-goal run is observed. It
#: is DELIBERATELY equal to `max_iterations`'s own default — see the precedence note in `_next()`
#: (Task 2) and Top Risk 5 for why that tie is intentional, not accidental.
#:
#: #2531 measured this for real (plan #2531's own "Real-run results" section): three
#: fresh goal-slot subagents (#2543, #2544, #2531) started at 71,055 / 71,226 / 71,635 tokens
#: respectively (matching the ~70-77k estimate above), and the orchestrator's own CLEAN
#: dispatch-window peak — windowed to exactly this 3-goal burst, excluding 51 unrelated calls
#: from other work on the same long-lived session that would otherwise contaminate the number —
#: stayed at 603,142 tokens across those 3 real goals, well under the 930k-998k auto-compaction
#: ceiling #2521 was built to avoid (about 65% of its low end). That sample is small (one
#: session, 3 goals, none of which had finished its own SDLC lifecycle yet as of the
#: measurement) and not a clean multi-day trend, so it is evidence 20 is not yet CONTRADICTED,
#: not proof it is optimal — left unchanged rather than retuned from too few points. A larger,
#: multi-day sample (this tool makes that free to collect on every future pick) is the right
#: basis for ever changing this number.
DEFAULT_HANDOFF_AFTER_GOALS = 20


def _handoff_ceiling(handoff_config):
    """The effective `after_goals` ceiling, or `None` when hand-off is disabled/unlimited (explicit
    `enabled: false`, or explicit `after_goals: 0`) — the ONE definition both `_handoff_reason`
    below and `next_batch`'s own pre-loop shrink (Task 3, plan-review round 1 finding 3) read, so
    the two can never independently drift on what "the ceiling" means — mirrors `_budget_spent`'s
    own documented relationship to `_budget_reason` exactly (loop.py:415-423, "a thin wrapper...
    so the two can never independently drift").

    UNLIKE every other opt-in block `state.load_config` returns, and unlike `_budget_reason`'s own
    "absent/zero enforces nothing" convention for max_iterations, the ABSENT-KEY case here is NOT
    "off" — it is enabled at `DEFAULT_HANDOFF_AFTER_GOALS`. #2521: the defect this closes (an
    orchestrating session growing unbounded across goals) is present under the config every existing
    install ALREADY has on disk, gitignored, predating this key by construction — config templates
    are applied once, at `/sigma-init` time, never re-synced onto an existing `.sdlc/config.json`. An
    "absent means off" reading would leave every install that already exists exhibiting the exact
    bug this goal exists to close, forever, with no key to even know to add. `enabled: false` is the
    explicit, documented escape hatch for an operator who has measured their own repo and wants the
    old, unbounded single-session behaviour back — see `skills/sigma-init/templates/config.json.tmpl`.

    Explicit `after_goals: 0` (the key present, the value zero) still means "unlimited", matching
    `_budget_reason`'s own convention for `max_iterations` exactly — only the ABSENCE of the whole
    `handoff` block (or block present, `after_goals` key itself absent) falls back to the default."""
    handoff_config = handoff_config if isinstance(handoff_config, dict) else {}
    if handoff_config.get("enabled") is False:
        return None
    after_goals = handoff_config.get("after_goals", DEFAULT_HANDOFF_AFTER_GOALS)
    return after_goals or None


def _handoff_reason(cursor, handoff_config):
    """None when the hand-off ceiling has not tripped; else a short, human-readable string naming
    the goal count and the configured ceiling, e.g. "12 goals >= handoff.after_goals 12" — the
    HANDOFF sibling of `_budget_reason` above. Checked in `_next()` AFTER a real resource ceiling
    (`_budget_resource_reason`, max_minutes/max_tokens — see that gate's own comment for why) but
    BEFORE the remaining, max_iterations-only BUDGET case: it is the smaller, EXPECTED-to-trip-first
    ceiling on the shared default, and a deliberate, healthy context refresh for the orchestrating
    session. Production admission now calls `_session_count_stop` against a per-session counter;
    this cursor helper remains for direct diagnostics and compatibility tests.

    NAMING NOTE (code review cycle 1, finding 4): "handoff" already names an UNRELATED, pre-existing
    mechanism in this same script family — `handoff.py`/`ledger.KINDS`'s `"handoff"` entries-kind, a
    goal-level escalation needing a human ack (see `HANDOFF-trust-goals.md`, `tests/test_handoff.py`).
    This function's own "hand-off" is a DIFFERENT concept: the orchestrating SESSION retiring itself
    on a goal-count ceiling. No functional collision — different modules, different ledger streams —
    but grep "handoff" in this codebase later and expect to find both."""
    ceiling = _handoff_ceiling(handoff_config)
    if ceiling and cursor["run_iteration"] >= ceiling:
        return f"{cursor['run_iteration']} goals >= handoff.after_goals {ceiling}"
    return None


def _lease(sdlc_dir, config):
    """(me, my_writer, {goal: (actor, writer)}) — who I am (as both a bare actor and this
    process's own writer identity), and the claim lease read once per selection, writer-detailed
    (F10.5/#374) so `_next()` can tell a DIFFERENT, still-live process of MY OWN actor apart from a
    genuinely resumable claim — not just tell my actor apart from someone else's (see
    `ledger.claim_belongs_to_me`). A goal another actor holds an OPEN claim on (claimed, not yet
    done/parked/failed) belongs to their loop, so this loop skips it instead of starting the same
    work twice. A goal I hold — my own CURRENT process, or a legacy/dead writer of mine — is still
    mine to resume; a goal held by a DIFFERENT, still-live writer of mine is not.

    (None, None, {}) when the ledger is off or unreadable — selection is then byte-identical to a
    repo that never enabled the ledger. Fail-open by design: a lease we cannot read must never stop
    the loop. A claim older than the TTL (config `ledger.lease.ttl_hours`, default 12h; 0/false =
    never expire) is treated as released, so one crashed run cannot block a teammate on that goal
    forever."""
    try:
        if not ledger.enabled(config):
            return (None, None, {})
        ttl = ledger.lease_ttl_seconds(config)
        me = ledger.actor(config)
        lease = ledger.open_claims_detailed(ledger.read_all(sdlc_dir), ttl_seconds=ttl)
        return (me, ledger.my_writer(config), lease)
    except Exception:               # noqa: BLE001 - fail-open; an unreadable lease leaves selection unlocked
        return (None, None, {})


_LOCK_UNAVAILABLE = -1   # fail-open sentinel from _try_acquire_claim_lock: no real fd was ever
# opened (no fcntl, or an OS error), so proceed as if the lock was acquired — os.open() itself never
# returns a negative fd on success, so this can never collide with a genuine, held lock's fd.


def _claim_lock_path(sdlc_dir, goal):
    stem = work.stem(goal)
    reason = state.unsafe_goal_reason(stem)
    if reason:
        raise ValueError(f"unsafe goal {goal!r} for the claim lock: {reason}")
    return pathlib.Path(sdlc_dir) / "state" / "claims" / f"{stem}.lock"


def _try_acquire_claim_lock(sdlc_dir, goal):
    """The open file descriptor THIS process holds an exclusive, kernel-mediated `flock` on, iff it
    is the exclusive winner, right now, of the local race to claim `goal` (F10.5-2/#387) — `None` if
    a live sibling already holds it (caller should skip this goal), or `_LOCK_UNAVAILABLE` if the
    mechanism itself could not be used at all (fail-open; see below). Closes what the ledger-claim
    check (`_lease`/`ledger.claim_belongs_to_me`, #374) structurally cannot: #374 is entirely about
    correctly INTERPRETING a claim that already exists — it has no answer for "nothing exists yet and
    two readers look at the same instant." Two processes sharing one `.sdlc` directory (two tabs on
    one machine) racing `_next()` close enough together both read the same pre-claim ledger state and
    could otherwise both proceed.

    `flock(fd, LOCK_EX | LOCK_NB)` is kernel-mediated, not built out of ordinary file operations — it
    has NO read-then-act gap of its own for a second caller to land in. Two prior schemes here, each
    built from ordinary create/rename/unlink calls plus a staleness timeout to recover a crashed
    holder's lock, were independently broken across two review cycles: `unlink()`-then-recreate let a
    second racer silently clobber the first's fresh lock; a rename-then-verify-then-restore
    refinement closed that gap but opened a narrower one, where a THIRD caller could land in the
    restore step's own window and also win. `flock` has no such window to begin with — the kernel
    either grants exclusive access to this open file description or it doesn't, atomically — and,
    the property that also retires the staleness-timeout idea those schemes needed entirely: it is
    released the INSTANT the holding process ends for ANY reason, crash included, because the kernel
    closes every fd a dying process held. A lock file surviving on disk after a crash is therefore
    never ambiguous — nothing is still flocking it, so the very next attempt against it succeeds
    immediately. No age to guess at, no eviction to arbitrate, no restore step to have its own gap.

    Deliberately LOCAL-only (this file, this machine) — cross-machine claims are the ledger's own,
    already-correct job; a lock file two different machines don't share cannot and need not arbitrate
    between them. POSIX-only (`fcntl.flock`): on a platform without `fcntl` (Windows), this fails
    open unconditionally, exactly as if this whole file didn't exist — no narrower than before #387,
    just not ALSO narrowed by it there; a `msvcrt`-based Windows equivalent is not attempted here, it
    would need its own from-scratch verification this change cannot give it.

    Fail-open beyond that: any OS error this function cannot interpret (permissions, a read-only
    filesystem, a directory it cannot create) returns `_LOCK_UNAVAILABLE` — a lock this call cannot
    manage must never be what stops the loop; `_lease`/`claim_belongs_to_me` remains the primary,
    always-on defense regardless of whether this local, best-effort narrowing is available at all.

    #494: on a WINNING acquisition, also stamps the file with this process's own pid (see
    `_write_claim_lock_liveness`) — the file used to stay at 0 bytes forever, giving a reader with
    no way to re-attempt the flock themselves (a human, a different tool) nothing to tell "claimed
    and abandoned" from "claimed and working." The stamp never affects whether this call itself
    succeeds. THIS process's own pid is always the short-lived `next`/`next-batch` CLI invocation
    doing the claiming, not any long-lived worker — see `claim_lock_alive` for why the read side
    deliberately does not treat that pid's own liveness as the signal for whether the GOAL is
    still being worked, and `reclaim_stale_claim_lock` for the read side's deletion half."""
    if fcntl is None:
        return _LOCK_UNAVAILABLE
    try:
        path = _claim_lock_path(sdlc_dir, goal)
    except ValueError:
        return _LOCK_UNAVAILABLE             # unsafe goal — same fail-open posture as an OSError
    # #464: the liveness sweep (and `reclaim_stale_claim_lock`) unlink stale lock files. A lock won on
    # an inode that was unlinked between our `open` and our `flock` would let a second process create
    # a fresh file and win too, so after winning, the fd must still be the inode AT the path (checked
    # BEFORE the liveness stamp). A swapped file is retried, bounded; exhausted retries read as
    # contended (`None`), never as a win.
    for _attempt in range(3):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(path), os.O_CREAT | os.O_RDWR)
        except OSError:
            return _LOCK_UNAVAILABLE         # can't even open it — fail open, see docstring
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return None                      # a live sibling holds it right now
        try:
            held, here = os.fstat(fd), os.lstat(str(path))
            same = (held.st_dev, held.st_ino) == (here.st_dev, here.st_ino)
        except OSError:
            same = False                     # unlinked under us
        if not same:
            os.close(fd)
            continue
        try:
            _write_claim_lock_liveness(fd)
        except OSError:
            os.close(fd)
            return None
        return fd
    return None


def _write_claim_lock_liveness(fd):
    """#494: stamp a just-WON claim lock with this process's own pid, `f"{pid}\\n"` — the same
    bare-pid-content convention `session_start`/`agent_start` established for "a marker file a
    reader can inspect" (no hostname: like those two, and unlike the ledger's own writer id —
    #540 — this marker is read ONLY on the machine that wrote it, `_try_acquire_claim_lock`'s
    docstring already says so, so a host component would be pure noise here).

    Unlike `session_start`/`agent_start`'s own pid parameters — the CALLER's own long-lived
    process id, captured once and handed in explicitly — THIS pid is whatever `os.getpid()`
    resolves to for the process executing `_try_acquire_claim_lock` right now: always the
    short-lived `next`/`next-batch` CLI invocation itself, which (per `session_start`'s own
    docstring, and SKILL.md's dispatch flow) exits within moments of returning, long before the
    goal's real multi-hour work even starts. So this stamp is forensic-only (which invocation
    last won the acquisition, and when, via the file's own mtime) — `claim_lock_alive`
    deliberately does NOT treat this pid's own liveness as evidence of whether the GOAL is still
    being worked; see that function's docstring for why (a same-process check made this look
    fine in tests — real cross-process use proved otherwise, under a second after a winning
    subprocess exits normally).

    Truncate before writing: the fd may be reused from a PRE-#494 empty file, or from an earlier
    acquisition's now-stale (possibly LONGER) pid string — a bare `write()` with no truncate could
    leave old trailing bytes behind a shorter new one. Best-effort and silent on failure: the flock
    itself is the real prize this call must not put at risk — a lock this helper cannot annotate is
    simply as uninformative to a later reader as every lock was before #494, never worse, and never
    a reason to lose an otherwise-genuine acquisition."""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
    except OSError:
        pass


def _release_claim_lock(fd):
    """Best-effort: releasing must never raise into `_next()`'s caller. `None` (denied, nothing was
    ever acquired) and `_LOCK_UNAVAILABLE` (fail-open, no real fd) are both safe no-ops. Closing the
    real fd is what releases the kernel-held `flock` — there is nothing else to clean up: the lock
    FILE itself is deliberately left on disk (now carrying the acquiring pid, #494 — see
    `_write_claim_lock_liveness`), ready for the next `flock` attempt whether that is an ordinary
    release or a crash recovery; deleting it here would gain nothing `flock` doesn't already give
    for free, and would only reopen a create/delete race for no benefit. See
    `reclaim_stale_claim_lock` for the one place this codebase DOES delete the file, deliberately,
    off the hot path and only once a lock is confirmed both pid-dead and genuinely unheld."""
    if fd is None or fd == _LOCK_UNAVAILABLE:
        return
    try:
        os.close(fd)
    except OSError:
        pass


#: #1472: `cross_repo` is loaded on FIRST PICK, not at import, and the accessor exists so a test can
#: substitute it by assigning `loop._CROSS_REPO`. Lazy for a real reason rather than for style: that
#: module pulls `features`, `feature_registry`, `work`, `ledger` and `state` behind it, and every
#: other `loop.py` entry point (`verify`, `record`, `log`, the hooks that import this file) would pay
#: for a chain none of them can reach, on behalf of a branching model most projects never adopt.
_CROSS_REPO = None


def _cross_repo():
    global _CROSS_REPO
    if _CROSS_REPO is None:
        _CROSS_REPO = _load("cross_repo")
    return _CROSS_REPO


#: #1477: `feature_propagate` is loaded on FIRST PICK OF A UNIT, not at import, and the accessor
#: exists so a test can substitute it by assigning `loop._FEATURE_PROPAGATE`. Lazy for exactly
#: `_cross_repo()`'s reason: it pulls `feature_registry`, `feature_sync`, `feature_doc`, `features`
#: and `ledger` behind it, and every other entry point in this file would pay for a chain none of
#: them can reach, on behalf of a branching model most projects never adopt.
_FEATURE_PROPAGATE = None


def _feature_propagate():
    global _FEATURE_PROPAGATE
    if _FEATURE_PROPAGATE is None:
        _FEATURE_PROPAGATE = _load("feature_propagate")
    return _FEATURE_PROPAGATE


def _scope_ok_at_pick(sdlc_dir, source, goal, config, unit):
    """#1477: may this goal's REPO carry work for the unit it declares? -> True to proceed.

    §7.1's one-directional rule: propagation reaches the repos `repos` already names, and a goal in a
    repo that is NOT named is a scope EXPANSION -- the unit owner's decision, never a silent registry
    edit. Refused here, inside the claim lock and beside the unit-label attach, because this is the
    last moment at which nothing has acted on the goal: no worktree, no branch, no registry write.

    FAIL-OPEN, and it costs nothing to be. `gate_at_pick` already resolves every internal failure to
    "proceed", and this guard covers the case where the module cannot even be loaded -- because the
    field the rule protects is ALSO defended one layer down, in `feature_sync._record_goal`, which
    refuses to widen `repos` whatever reaches it. A gate that cannot answer must not stop a queue
    when the answer it would have given changes nothing about the registry."""
    try:
        return _feature_propagate().gate_at_pick(sdlc_dir, source, goal, config, unit).proceed
    except Exception as exc:                        # noqa: BLE001 - never break a pick over a check
        print(f"sigma: unit scope check skipped (non-fatal): {exc}", file=sys.stderr)
        return True


#: #1479: `feature_owner` is loaded on FIRST PICK OF A UNIT, not at import, and the accessor exists
#: so a test can substitute it by assigning `loop._FEATURE_OWNER`. Lazy for `_feature_propagate()`'s
#: reason, and a SEPARATE accessor from it rather than a reach through that module: the two gates
#: answer different questions, and a module loaded as somebody else's attribute is a module nobody
#: can substitute on its own.
_FEATURE_OWNER = None


def _feature_owner():
    global _FEATURE_OWNER
    if _FEATURE_OWNER is None:
        _FEATURE_OWNER = _load("feature_owner")
    return _FEATURE_OWNER


def _owner_ok_at_pick(sdlc_dir, source, goal, config, unit):
    """#1479: was this issue FILED by somebody entitled to file against the unit it carries?
    -> True to proceed.

    §12's rule: an issue carrying `feature:<name>` may be created directly only by the unit's owner
    or by the owner of the board it lands on, and where those two disagree the board owner wins. An
    issue somebody else opened is not blocked from existing -- it is held here, inert, with a ledger
    entry to its owner, because an agent may otherwise pick it up and start executing work on a
    board nobody agreed to put it on.

    RUNS AFTER `_scope_ok_at_pick`, and the ordering is the reason the two are separate calls rather
    than one: Python short-circuits the `and` in `_next` below, so a goal already refused for scope
    never also pays for this check and never collects two refusals for one pick.

    FAIL-OPEN, and it costs nothing to be. `gate_at_pick` already resolves every internal failure to
    "proceed", and this guard covers the case where the module cannot even be loaded. The other half
    of the enforcement -- `handoff.create_tracked_issue`'s filing gate -- is unaffected by anything
    that happens here, so a gate that cannot answer must not stop a queue."""
    try:
        return _feature_owner().gate_at_pick(sdlc_dir, source, goal, config, unit).proceed
    except Exception as exc:                        # noqa: BLE001 - never break a pick over a check
        print(f"sigma: unit ownership check skipped (non-fatal): {exc}", file=sys.stderr)
        return True


#: #1567: `feature_registry` is loaded on the ONE path that needs it -- a pick whose declaration
#: read failed on an adopted project -- and never at import. Lazy for `_cross_repo()`'s reason, and
#: its own accessor rather than a reach through that module: a module loaded as somebody else's
#: attribute is a module nobody can substitute on its own.
_FEATURE_REGISTRY = None


def _feature_registry():
    global _FEATURE_REGISTRY
    if _FEATURE_REGISTRY is None:
        _FEATURE_REGISTRY = _load("feature_registry")
    return _FEATURE_REGISTRY


def _unit_at_pick(sdlc_dir, goal, config, decision):
    """#1567: the unit the pick-time gates are handed -> a unit name, or None.

    ONE DECLARATION HAD TWO READERS ON TWO TRANSPORTS, and that is the whole defect. `attach_at_pick`
    resolves the unit over GRAPHQL (`gh issue view --json body,labels`); three lines further down
    `work._declared_unit` resolves THE SAME DECLARATION over REST and bases the goal on
    `feature/<unit>`. A failed GraphQL read degrades to `UNREADABLE`/`unit=None` -- correctly, since
    a read we could not make is not evidence of anything -- but that None was then handed to the
    gates as if it were an ANSWER. So one un-retried `gh issue view` cut the goal from the unit's
    branch while the gate governing work against that unit had been told there is no unit.

    WHAT MAKES IT SHARP, AND WHAT THIS FIXES, IS NOT THE FALLBACK ITSELF: the scope gate's entire
    decision is LOCAL -- the registry on disk plus this repo's own slug -- and it was being switched
    off by a REMOTE failure, purely because its input happened to travel the failing transport. A
    gate may fail open on its own inputs; it must not fail open on somebody else's.

    ONLY `UNREADABLE`, and that boundary is the load-bearing one. Every other outcome carries an
    ANSWER: `NOTHING_TO_DO` means the read succeeded and found no declaration, `CONFLICT_DECLARED`
    means the body won and the body is what base resolution reads too, and a refusal never reaches a
    gate at all. Re-reading on any of those would tax a project that declares nothing -- the exact
    adoption promise `feature_labels`' failure table makes -- to answer a question already answered.

    ONLY WHEN `.sdlc/features/` EXISTS, for the same reason. Without it BOTH gates and #1472's
    landing check return `not-adopted` without ever looking at the unit, so resolving one would be
    pure cost paid by every project that never adopted the branching model. That check is a `stat`,
    and it is what keeps a non-adopter's pick byte-identical -- same calls, same order, same output.

    WHAT IT COSTS, MEASURED RATHER THAN ASSERTED. Nothing on the ordinary pick: a read that answered
    consults nobody. On the one whose read failed, one `gh api repos/<slug>/issues/<n>` -- REST,
    which is the point. GitHub meters REST on a SEPARATE hourly budget from GraphQL (`work.py`'s own
    measurement, and #1209's outage), so the transport that just failed is not the transport being
    asked, which is what makes a second attempt worth making at all rather than a retry of the same
    call under a different name. It is also the read `_check_cross_repo_at_pick` makes moments later
    on this same pick, so on a REFUSED pick the total is unchanged and on a proceeding one it is one
    more than before -- never per candidate, only per candidate whose declaration could not be read.

    THE RESIDUE, NAMED RATHER THAN LEFT TO BE FOUND: if the REST read fails too, there is no unit to
    hand anybody and the gates degrade exactly as they did before this existed. That is TWO
    transport failures, not one, and the alternative -- refusing the pick -- would hand every
    adopter a hard dependency on a network call that a queue must be able to survive. It stays
    fail-open, and says so on stderr.

    NEVER RAISES. This runs inside `_next`'s claim lock, above the `try/finally` that releases it, so
    an exception escaping here would leak the lock and leave the goal unpickable by anyone. The
    `getattr` on `outcome` is part of that totality rather than defensive noise: a `Decision` is a
    namedtuple, but a partial stand-in is exactly the shape a caller substitutes, and an
    `AttributeError` raised in here is not a failed check -- it is a stuck queue."""
    try:
        if getattr(decision, "outcome", None) != feature_labels.UNREADABLE:
            return decision.unit
        if not _feature_registry().registry_dir(sdlc_dir).is_dir():
            return decision.unit
        unit, why = _cross_repo().unit_of(sdlc_dir, config, goal, None)
    except Exception as exc:                        # noqa: BLE001 - never break a pick over a check
        print(f"sigma: the unit re-read for #{goal} did not run (non-fatal): {exc}",
              file=sys.stderr)
        return getattr(decision, "unit", None)
    if why:
        print(f"sigma: features: #{goal}'s declaration could not be read on either transport "
              f"({why}) — the pick carries on with no unit, exactly as before, so a gate that "
              f"depends on one cannot answer for it", file=sys.stderr)
        return decision.unit
    return unit


def _check_cross_repo_at_pick(sdlc_dir, goal, config):
    """#1472: decide the goal's cross-repo landing tier HERE, at the pick, and record it.

    This call site IS the definition of done -- "the check runs at pick time, not at merge time".
    A permissions failure found at merge is found after the worktree, the branch, the code and the
    PR already exist; found here it is found before any of them do. The merge gate (#1474) reads the
    recorded decision rather than re-asking, so there is exactly one measurement per goal and it is
    taken at the cheapest moment.

    ADVISORY, NEVER BLOCKING. Neither tier is a refusal -- tier 2's entire promise is that nothing
    is lost when access is missing, so a goal whose sibling repo is unreachable still proceeds,
    contract-first, with the unavailable half raised to its owner through the ledger. And a check
    that fails outright must not cost the pick its goal: `check_at_pick` already resolves every
    internal failure to `flagged`, and this guard covers the case where it cannot even be loaded.

    Takes no `source`. It used to, back when the unit was read from the issue BODY through a backlog
    source's `fetch_title_body`; it now resolves through `work._declared_unit` (#1467), the same dual
    read `work.start()` bases the worktree on, so the base a goal is cut from and the tier it is
    checked under cannot disagree. See `cross_repo.unit_of`."""
    try:
        _cross_repo().check_at_pick(sdlc_dir, goal, config)
    except Exception as exc:                        # noqa: BLE001 - never break a pick over a check
        print(f"sigma: cross-repo landing check skipped (non-fatal): {exc}", file=sys.stderr)


#: `predict.py why`'s line. #2827 added an optional `in=<title|body>` BEFORE `signal=`, so the
#: signal stays the free-text tail (signals contain spaces: `dead code`). The location is parsed
#: and not recorded -- the ledger's `model_choice` fields are unchanged.
_WHY_LINE = re.compile(
    r"^model=(?P<model>\S+)(?: in=(?P<where>title|body))? signal=(?P<signal>.*)$")

# Keep the pick-time recording boundary strict even if a resolver process is stale or corrupted.
# This mirrors the versioned Codex catalog in sigma-model without importing that skill's Python.
_CODEX_HOST_MODELS = frozenset((
    "gpt-5.5",
    "gpt-5.6-luna",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-6-astra",
))
_CODEX_REASONING_EFFORTS = frozenset(("low", "medium", "high"))


def _parse_codex_host_model(stdout):
    """Return a strictly valid `host-model` result, or reject it before an event is written."""
    pairs = {}
    for token in (stdout or "").strip().split():
        key, separator, value = token.partition("=")
        if not separator or not key or not value or key in pairs:
            raise ValueError("host-model resolver returned an invalid result")
        pairs[key] = value
    if (set(pairs) != {"model", "effort"}
            or pairs["model"] not in _CODEX_HOST_MODELS
            or pairs["effort"] not in _CODEX_REASONING_EFFORTS):
        raise ValueError("host-model resolver returned an invalid result")
    return pairs


def _predict_model_choice_at_pick(sdlc_dir, source, goal, config):
    """#1627: resolve + record this goal's model tier from CODE, at pick time -- the invocation
    half issue #1627 measured as never happening (predict.py's only prior caller anywhere in this
    repo was SKILL.md prose telling an agent to run it by hand; a real `model_choice` event was
    recorded exactly once, ever, across 1,102 ledger event files). Mirrors
    `_check_cross_repo_at_pick`'s exact call site and fail-open contract: advisory, best-effort,
    never blocks or fails a pick.

    Shells out to `predict.py`'s own `why` verb -- pure, ungated, writes nothing -- rather than
    importing `predict.py`'s Python: the north-star's own architecture rule ("skills do not import
    each other's Python... the convention doctor.py and predict.py already state for themselves")
    is symmetric, so this mirrors `predict.py`'s own `_emit_model_choice` shelling out to `loop.py`
    in the OTHER direction. The recording half is then ONE literal, same-skill
    `ledger.safe_append(sdlc_dir, "model_choice", ...)` call made directly in THIS function
    (`ledger` is already `_load()`ed at module scope, both live in `skills/sigma-loop/scripts/`) --
    a genuine `ast.Call` site `tests/test_vocabulary_coverage.py`'s checker can see, unlike
    `predict.py resolve`'s own subprocess-shells-to-`loop.py-emit` path, which is why `model_choice`
    no longer needs `_PROSE_ONLY_ALLOWLIST` once this call exists (verified against that checker
    before this landed).

    Gated FIRST on `model_selection == "auto"` -- a repo that has not opted in must not pay for a
    real `gh` API call (`fetch_title_body`, in github mode) or a subprocess spawn on every single
    pick, matching this project's own SAFETY property ("nothing spawns background processes, sends
    data, or consumes quota without the operator opting in"). ALSO gated on `ledger.journal_on(
    sdlc_dir, config)` before `fetch_title_body` runs at all -- the EVENTS stream `ledger.append`
    writes to is gated on exactly that switch (see `ledger.append`'s own docstring, #2574/S1-G3),
    so without it the write would be silently dropped anyway; no point paying for the API call and
    subprocess spawn first only to discard the result. IT CALLS THE SWITCH, it does not re-derive
    it: this is the one place that duplicated the old, pre-rename config read, and a
    managed-settings lock that turns the journal ON must not leave this pre-gate closed, or the
    `model_choice` emit below would be skipped on exactly the installs org policy just enabled.
    The cost of the switch here is one `stat` (~19 µs, this machine) per pick, three orders below
    the `gh` call it is protecting. Then on
    non-empty fetched text -- a degraded/malformed `fetch_title_body` read (e.g. a `GitHubSource`
    payload that didn't parse) must never classify nothing into a confident-looking, entirely
    fabricated `sonnet` entry, matching the north-star's own "never a fabricated reading" non-goal.
    Measured cost when both gates are open: ~28ms median for the `why` subprocess alone (this
    machine) -- a per-goal-pick cost, not a hot-loop one.

    Reads the goal's REAL text through `source.fetch_title_body` -- the same source-agnostic,
    already-tested seam `decompose_check` and `review_context.py` both use -- rather than passing
    the bare identifier as text. In github mode `goal` is just an issue number ("1627"), which
    `predict.py`'s own `_read()` returns UNCHANGED (it only resolves local file paths), so a
    goal-identifier-as-text call would always land the unsignalled `sonnet` default; classifying
    the actual title+body instead is what fixes that second, compounding bug.

    PRINTS THE RESOLVED TIER TO STDERR ON SUCCESS, not only on failure (a code-review finding on an
    earlier draft): SKILL.md still tells an agent to separately run `predict.py resolve "$goal"
    .sdlc` by hand to learn the tier for dispatching phase subagents, and in github mode that
    manual re-run shares the exact same bug this function exists to fix -- `$goal` there is the
    bare issue number, not real text, so a manual recomputation lands the wrong, unsignalled
    default even after THIS call has already recorded the correct one. Printing the tier this
    function already computed is what lets an agent read the right answer instead of recomputing a
    wrong one; see the SKILL.md paragraph this function is cited from for the corrected guidance."""
    try:
        if (_host_model_config(config).get("model_selection") or "off") != "auto":
            return
        if not ledger.journal_on(sdlc_dir, config):
            return
        title_body = source.fetch_title_body(goal) or {}
        # #2827: the title must stay the FIRST LINE, where predict.py reads haiku signals -- so
        # never `.strip()` this: with an empty title, stripping promoted the body's first line.
        text = f"{title_body.get('title') or ''}\n\n{title_body.get('body') or ''}"
        if not text.strip():
            return
        predict_py = _HERE.parent.parent / "sigma-model" / "scripts" / "predict.py"
        r = subprocess.run([sys.executable, str(predict_py), "why", text, str(sdlc_dir)],
                            capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            err_tail = (r.stderr or "").strip().splitlines()[-1:] or [f"exit {r.returncode}"]
            print(f"sigma: model-tier prediction failed (non-fatal): {err_tail[0]}",
                  file=sys.stderr)
            return
        m = _WHY_LINE.match((r.stdout or "").strip())
        if not m:
            print(f"sigma: model-tier prediction returned an unexpected shape "
                  f"(non-fatal): {r.stdout!r}", file=sys.stderr)
            return
        tier, signal = m.group("model"), m.group("signal")
        # `why` is deliberately pure and permissive so it can explain a candidate tier. Before
        # that candidate becomes a durable advisory event, run the strict Codex resolver too. This
        # is a sibling process rather than an import: Sigma skill modules deliberately do not
        # import one another's Python. A broken override must leave neither a runnable host command
        # nor a misleading model_choice entry in the ledger.
        host = subprocess.run([sys.executable, str(predict_py), "host-model", "codex", tier,
                               str(sdlc_dir)], capture_output=True, text=True, timeout=15)
        if host.returncode != 0:
            err_tail = (host.stderr or "").strip().splitlines()[-1:] or [f"exit {host.returncode}"]
            print(f"sigma: model-tier prediction skipped (non-fatal): Codex host mapping refused: "
                  f"{err_tail[0]}", file=sys.stderr)
            return
        try:
            _parse_codex_host_model(host.stdout)
        except ValueError as exc:
            print(f"sigma: model-tier prediction skipped (non-fatal): Codex host mapping refused: "
                  f"{exc}", file=sys.stderr)
            return
        fields = {"model": tier}
        if signal:
            fields["signal"] = signal
        ledger.safe_append(sdlc_dir, "model_choice", goal, config=config,
                            stream=ledger.EVENTS, **fields)
        print(f"sigma: model tier for {goal} resolved to {tier}"
              f"{f' (signal={signal})' if signal else ''} — read this, do not recompute "
              f"from \"$goal\" alone in github mode", file=sys.stderr)
    except Exception as exc:                      # noqa: BLE001 - never break a pick over this
        print(f"sigma: model-tier prediction skipped (non-fatal): {exc}", file=sys.stderr)


#: #1478: `unit_completion` is loaded on the FIRST `done`, not at import, and the accessor exists so
#: a test can substitute it. Lazy for the reason `_cross_repo()` is lazy: it pulls `feature_registry`,
#: `feature_labels`, `features`, `ledger` -- and, on the one goal that closes a unit, `work` and
#: `feature_sync` -- behind it, on behalf of a branching model most projects never adopt.
_UNIT_COMPLETION = None


def _unit_completion():
    global _UNIT_COMPLETION
    if _UNIT_COMPLETION is None:
        _UNIT_COMPLETION = _load("unit_completion")
    return _UNIT_COMPLETION


def _signal_unit_completion(sdlc_dir, goal, run=None, sleep=None):
    """#1478: has this goal's unit of work just become complete, and should anyone be told?

    THIS CALL SITE IS THE DEFINITION OF DONE for "the default config SURFACES the signal": three
    lines above, `source.complete(goal)` closed the issue, so this is the one moment "zero open
    issues under `feature:<unit>`" can newly become true. `work.finish()` was the alternative and is
    wrong -- it refuses outright on the documented `auto_merge: off` / fork / read-only paths, so on
    exactly those repos the signal would never fire at all.

    IT DECIDES NOTHING, WHICH IS THE POINT OF THE WHOLE FEATURE. The return value is ignored here
    and `_record` is unchanged by it: no outcome flips, no goal parks, nothing merges. The report
    exists for the CLI verb and for tests. See `unit_completion`'s own docstring for why the merge
    that would land a unit deliberately still has no owner.

    NOT ON A HOT PATH, and that is what makes calling `work.sibling_gate` from inside it affordable
    -- that check can wait up to 450s on a sibling repo's own CI. Nothing waits on this answer: the
    goal is over, its outcome is already recorded, and the loop's next pick is a separate call. The
    wait is bounded to at most once per unit ever, because the sibling is only consulted after the
    board says zero open issues.

    TOTAL, for the same reason `_check_cross_repo_at_pick` is: `signal()` already resolves every
    internal failure to a report, and this covers the two things outside it -- an unreadable config
    (`state.load_config` RAISES on a missing or non-object `config.json`) and a module that cannot
    be loaded at all. A goal's terminal record must never be lost over a courtesy."""
    try:
        kwargs = {}
        if run is not None:                 # injection points for tests; the real path passes neither
            kwargs["run"] = run
        if sleep is not None:
            kwargs["sleep"] = sleep
        return _unit_completion().signal(sdlc_dir, state.load_config(sdlc_dir), goal, **kwargs)
    except Exception as exc:                # noqa: BLE001 - never lose a record over a signal
        print(f"sigma: unit-completion signal skipped (non-fatal): {exc}", file=sys.stderr)
        return None


def _goal_has_registered_worker(sdlc_dir, goal, config, exclude_pids=None):
    """True iff `agent_alive` reports "alive" for `goal`, on some registered thread whose pid is
    NOT one of `exclude_pids` (so intra-goal slice parallelism still counts, per thread) — the
    PER-GOAL half of `_claimed_goal_has_live_worker` below, and, as of the correction described
    there, the ONLY signal this codebase's reclaim-refusal logic is allowed to trust. `agent_alive`'s
    own marker is unconditional as of #1197 (see `agent_start`) — this helper invents no new state
    of its own, it just asks the one question a reclaim decision for THIS SPECIFIC `goal` actually
    needs answered.

    `exclude_pids` (#2394) is who is ASKING, not evidence about the goal — the same `{os.getpid(),
    os.getppid(), session_pid}` set `work.py`'s `_calling_session_pids` already builds for the
    sibling `_blocked_by_a_live_foreign_agent` check one call site up. Without it, this function
    cannot tell "a genuinely different, still-live process is working this goal" apart from "the
    CALLER's own agent-start marker, written moments ago by this very session" — and SKILL.md step
    3a writes exactly that marker immediately before either caller below ever asks this question, so
    every ordinary resume/pick found its own registration and read it as foreign (#2394:
    `work.py start` refusing to resume the worktree it had itself just cut).
    Omitted (every pre-#2394 call site, and any caller with no identity to offer), this degrades to
    exactly the old behaviour — purely additive, byte-identical default; it only NARROWS what counts
    as foreign, never widens it.

    The two callers that need exactly this, both via `ledger.claim_belongs_to_me`'s
    `live_worker_check`: this module's own `_next()` (a picker deciding whether a
    dead-picker-pid claim is safe to reclaim) and `work.py`'s `_resume_blocked_by_a_live_sibling`
    (deciding whether to resume into an existing local worktree) — the same "is THIS goal's worker
    genuinely still alive?" question either way, so one implementation, not two. `_auto_reclaim_
    stale_claims`'s own sweep call site deliberately passes none: that pass is a genuinely different
    actor's process reclaiming ANOTHER session's stale claim, not a session asking about its own
    just-written marker, so nothing there should ever be excluded."""
    exclude = exclude_pids or ()
    try:
        codex_thread = _session_codex_thread() if exclude_pids is not None else None
    except ValueError:
        codex_thread = "invalid-codex-thread"  # never exclude a live marker on uncertain identity
    for thread in agent_threads(sdlc_dir, goal):
        state_, pid = agent_alive(sdlc_dir, goal, config, thread=thread)
        if state_ == "unknown":
            try:
                marker = _agent_marker_path(sdlc_dir, goal, thread)
                if marker.exists() and not _agent_marker_expired(marker, config):
                    return True
            except (OSError, ValueError):
                return True  # unreadable ownership fails toward an active worker
        if state_ != "alive":
            continue
        if pid not in exclude:
            return True
        try:
            marker_owner = _agent_marker_identity(sdlc_dir, goal, thread)[1]
        except (OSError, TypeError, ValueError, KeyError):
            return True
        if marker_owner != codex_thread:
            return True
    return False


def _claimed_goal_has_live_worker(sdlc_dir, goal, config):
    """True iff either of the two markers that ALREADY correctly track a genuine long-lived
    worker pid — `session_active` (the whole managing session driving `.sdlc`, GOAL-AGNOSTIC: one
    marker per `.sdlc`, true if ANY managing session is registered, regardless of which goal it is
    working) or `_goal_has_registered_worker` (`agent_alive` scoped to this SPECIFIC `goal`) —
    currently reports one alive. `session_active` stays opt-in (`--session-pid` at `start`, a
    hand-adopted routine prompt) and fails open to "no marker" when never armed; this helper just
    OR's the two together, it invents no new state of its own.

    CORRECTION (post-#1237-review): this OR'd, goal-agnostic combination is right for
    `claim_lock_alive` below (its own, older, #494 consumer — "is `.sdlc` under active management
    at all" is exactly the question that call site needs) but was WRONG for the #1197 dead-picker
    reclaim-refusal check that briefly reused it here too. Reviewer-reproduced failure mode: with a
    long-lived managing session registered (`session_start`, the project's own documented
    overnight/routine shape), a DIFFERENT goal that has crashed and never even registered its own
    `agent_alive` marker was judged "still has a live worker" purely because `session_active` was
    True — permanently blocking that goal's reclaim for as long as the unrelated session ran
    anything at all, violating #1197's own acceptance criterion 4 ("a crash must not leave a goal
    permanently unpickable"). `_next()` and `_resume_blocked_by_a_live_sibling` now pass
    `_goal_has_registered_worker` — never this function — as `live_worker_check`, so a reclaim
    decision for `goal` is corroborated ONLY by a marker that is actually about `goal`.
    `_auto_reclaim_stale_claims`'s own release-side check followed suit under #1284 (it had
    merged before this correction existed and briefly lagged behind it — see that function's own
    "#1284 FIX" docstring paragraph). This function itself is UNCHANGED and still correct for
    `claim_lock_alive`'s own, different question; only the reclaim-refusal call sites moved off
    it — as of #1284, ALL of them."""
    if session_active(sdlc_dir, config):
        return True
    return _goal_has_registered_worker(sdlc_dir, goal, config)


def claim_lock_alive(sdlc_dir, goal, config):
    """(state, pid) — #494's reader half; mirrors `agent_alive`'s exact contract (state in
    "alive" | "dead" | "unknown"; pid is the recorded int if the lock file parsed, else None).

    Revised post-#494 review: this used to apply the SAME two-signal combination
    `session_active`/`agent_alive` use on THEIR OWN markers — `ledger.pid_alive()` on the
    recorded pid, or the file's mtime past `ledger.lease_ttl_seconds(config)` as a fallback — to
    the claim lock's own stamped pid. That was wrong, not just imprecise: unlike THOSE two
    markers, whose recorded pid IS the long-lived thing being asked about, the claim lock's
    stamped pid (`_write_claim_lock_liveness`) is always the short-lived `next`/`next-batch` CLI
    invocation that won the acquisition — which exits within moments of returning, by design,
    regardless of whether a subagent goes on to work the goal for hours (see `session_start`'s
    own docstring, and SKILL.md's dispatch flow). So `ledger.pid_alive()` on THAT pid answers a
    question nobody asked: it reads "dead" within a fraction of a second of EVERY acquisition,
    whether the goal is genuinely abandoned or being actively, successfully worked — empirically
    confirmed with a real subprocess winning the lock and exiting normally, checked from a
    separate process a moment later. The 13 tests that shipped with #494 never caught this
    because every one of them checked liveness from the SAME still-running process that won the
    lock — a case that cannot occur in production, where `next`/`next-batch` is always a fresh,
    short-lived subprocess per call.

    Fixed by cross-checking `_claimed_goal_has_live_worker` FIRST — the two markers that DO
    correctly track a genuine long-lived pid for this exact question — before ever consulting the
    claim lock's own stamp. Only once neither shows activity does this fall back to the lock
    file's OWN mtime against the TTL (the same age-based backstop `session_active`/`agent_alive`
    already apply to their own markers) as a coarse "nobody has touched this in a very long time"
    signal — never to the stamped pid's liveness, which the paragraph above already disqualifies.
    A lock past the TTL with no corroborating trace either way is "dead"; the same lock still
    within the TTL, with no corroborating trace either way, is "unknown" — not enough evidence to
    call it either way yet, matching `reclaim_stale_claim_lock`'s own bias toward never deleting
    on a guess.

    "unknown" — no lock file at all (nothing has ever claimed `goal` locally, or a `_LOCK_UNAVAILABLE`
    fail-open path never wrote one), or one that PARSES as nothing (a pre-#494 lock, forever 0 bytes
    under the code that used to ship) — is deliberately NOT "dead": nobody recorded a pid here in any
    interpretable way, which is a strictly weaker claim than "recorded, and confirmed gone." An
    unsafe `goal` (see `_claim_lock_path`) degrades to "unknown" too, via the same except clause —
    correct either way, since no lock this call can even address was ever meaningfully read."""
    try:
        path = _claim_lock_path(sdlc_dir, goal)
        pid = int(path.read_text().strip())
    except (OSError, ValueError):
        return "unknown", None
    if _claimed_goal_has_live_worker(sdlc_dir, goal, config):
        return "alive", pid
    ttl = ledger.lease_ttl_seconds(config)
    if ttl is not None:
        try:
            if (time.time() - path.stat().st_mtime) >= ttl:
                return "dead", pid
        except OSError:
            pass
    return "unknown", pid


def reclaim_stale_claim_lock(sdlc_dir, goal, config):
    """True iff a confirmed-stale claim lock FILE for `goal` was removed from disk — #494's
    "safely reclaim" half, the automated stand-in for the hand-clearing that was the only recourse
    in the incident that opened this issue. Intended for a hygiene pass (mirrors `agent_watch.py`'s
    own periodic-tick shape) or ad-hoc operator use — nothing on the hot `_next()` path calls this;
    `_try_acquire_claim_lock`'s flock already needs no staleness window of its own (#387) to let a
    genuinely dead holder's lock be won again immediately, lock FILE untouched either way.

    Never touches a lock `claim_lock_alive` reports "alive" (a live holder must never be raced) or
    "unknown" (nothing this call can interpret — could be a fresh acquisition mid-flight, a
    pre-#494 empty file, or simply a lock still within the TTL with no corroborating
    `session_active`/`agent_alive` trace either way; leaving any of those alone is always safe).
    "dead" is a DIAGNOSIS (the lock has sat past the TTL with no live worker corroborating it —
    see `claim_lock_alive`), not proof of safety to delete on its own — the goal could in
    principle still be genuinely, slowly in progress with neither corroborating marker enabled.
    The deletion itself is therefore gated on genuinely WINNING the flock first
    (`_try_acquire_claim_lock` — the same kernel-mediated exclusivity #387 already established has
    no TOCTOU gap of its own): a lock some OTHER open file description still holds, right now, is
    never removed out from under it, regardless of what the diagnosis said. The diagnosis only
    decides whether this call bothers trying at all."""
    state, _pid = claim_lock_alive(sdlc_dir, goal, config)
    if state != "dead":
        return False
    fd = _try_acquire_claim_lock(sdlc_dir, goal)
    if fd is None or fd == _LOCK_UNAVAILABLE:
        return False                          # still genuinely held, or the mechanism is unavailable
    try:
        _claim_lock_path(sdlc_dir, goal).unlink(missing_ok=True)
    finally:
        _release_claim_lock(fd)
    return True


def _auto_unpark_sweep(sdlc_dir, config, run=None):
    """#1129/#1394: `discovery.auto_unpark.mode` (default 'on' since #1394), fail-open per-pick
    reconciliation -- re-examines `sdlc:blocked` GitHub issues whose recorded `blocked by #N` targets
    have all closed, and lifts the overlay so a LATER pick can pick one back up. An `sdlc:parked`
    issue is never resumed (#1394). See `auto_unpark.py`'s module docstring for the full mechanism and why it's scoped as
    narrowly as it is.

    Gated FIRST on `sources._auto_unpark(config)` — mirrors `discovery.blocker_promotion`'s own
    "Gated FIRST" precedent (sources.py `_promote_blockers`): an adopter who never sets
    `discovery.auto_unpark.mode` pays literally nothing here, not even the cost of importing
    `auto_unpark.py` (and transitively `triage.py`, a large module) — the `_load` call sits INSIDE
    this guard, not at loop.py's own module level alongside `sources`/`ledger`/etc.

    FAIL-OPEN, deliberately looser than most of this file's other guards: ANY exception (a bad
    config shape the gate itself can't evaluate, a transient `gh` failure inside the sweep despite
    its own internal fail-open handling, anything) is swallowed here with one stderr line. A
    background reconciliation sweep must never be what stops an unattended pick — that failure
    mode would be strictly worse than the bug #1129 exists to fix.

    Returns a `frozenset` of the goal refs THIS call actually flipped `sdlc:parked` ->
    `sdlc:goal` (`auto_unpark.sweep_unpark`'s own `result["unparked"]`, never `None` even from a
    stub/fake `sweep_unpark` that returns nothing) -- always empty on the 'off' gate and on the
    fail-open exception path. #1129-followup cooldown: `_next()` folds this into its own pick-loop
    `skip` set so the goal(s) this call just unparked cannot ALSO be the one it picks up in the
    SAME call — see `_next()`'s docstring for why."""
    try:
        if sources._auto_unpark(config) == "off":
            return frozenset()
        result = _load("auto_unpark").sweep_unpark(sdlc_dir, config, apply=True, run=run)
        return frozenset((result or {}).get("unparked") or ())
    except Exception as exc:
        print(f"loop.py: auto-unpark sweep failed non-fatally ({exc}) — continuing", file=sys.stderr)
        return frozenset()


def _reconcile_mode(config):
    """`discovery.reconcile.mode` -> 'off' (default) | 'on'. An unrecognised or missing value reads
    as off, because a typo must never switch on a mechanism that WRITES. (`sources._auto_unpark` is
    the opposite: it defaults to 'on' since #1394.)"""
    block = ((config or {}).get("discovery") or {}).get("reconcile")
    value = block.get("mode") if isinstance(block, dict) else None
    return value if value in ("off", "on") else "off"


def _reconcile_ttl_minutes(config):
    block = ((config or {}).get("discovery") or {}).get("reconcile")
    try:
        return max(1, int((block or {}).get("ttl_minutes", 60)))
    except (TypeError, ValueError):
        return 60


def _reconcile_sweep(sdlc_dir, config, run=None, now=None):
    """#1391 step 5e: the throttled, opt-in reconciliation pass. Returns the swept issue refs (or an
    empty frozenset), and NEVER raises.

    THREE GATES, in cost order, so an adopter who never opts in pays literally nothing -- not even
    the import of `reconcile.py` (which transitively pulls `sources`/`mirror`), mirroring
    `_auto_unpark_sweep`'s own "Gated FIRST" precedent:
      1. `discovery.reconcile.mode` must be 'on' (default 'off').
      2. A TTL watermark at `.sdlc/state/reconcile.meta.json` -- the census costs ~13 `gh` calls, and
         a backlog's label state does not drift meaningfully between two picks minutes apart.
         `discovery.reconcile.ttl_minutes`, default 60.
      3. `sweep_reconcile` itself refuses on an incomplete census.

    FAIL-OPEN, matching `_auto_unpark_sweep` exactly: any exception is swallowed with one stderr
    line. A background reconciliation must never be what stops an unattended pick."""
    try:
        if _reconcile_mode(config) != "off" and _reconcile_due(sdlc_dir, config, now=now):
            result = _load("reconcile").sweep_reconcile(sdlc_dir, config, apply=True, run=run)
            _reconcile_stamp(sdlc_dir, now=now)
            return frozenset(a["issue"] for a in (result or {}).get("actions", [])
                             if a.get("result") == "done")
    except Exception as exc:                 # noqa: BLE001 - fail-open; see docstring
        print(f"loop.py: reconcile sweep failed non-fatally ({exc}) — continuing", file=sys.stderr)
    return frozenset()


def _reconcile_watermark_path(sdlc_dir):
    return pathlib.Path(sdlc_dir) / "state" / "reconcile.meta.json"


def _reconcile_due(sdlc_dir, config, now=None):
    """True when no sweep has run inside the TTL. An unreadable/absent/malformed watermark reads as
    DUE -- the safe direction here is running a read-mostly sweep one extra time, never skipping it
    forever because a stamp file got corrupted."""
    now = now if now is not None else time.time()
    try:
        stamp = json.loads(_reconcile_watermark_path(sdlc_dir).read_text(encoding="utf-8"))
        last = float(stamp.get("swept_at") or 0)
    except Exception:
        return True
    return (now - last) >= _reconcile_ttl_minutes(config) * 60


def _reconcile_stamp(sdlc_dir, now=None):
    """Best-effort watermark write. A failure here costs one extra sweep next call, never a crash."""
    now = now if now is not None else time.time()
    try:
        path = _reconcile_watermark_path(sdlc_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": "reconcile-meta/1", "swept_at": now}), encoding="utf-8")
    except Exception:
        pass


def _auto_reclaim_stale_claims(sdlc_dir, source, config):
    """#1198: the crash-safety half of the `in_progress_label` fix. `_fetch_pending` (sources.py)
    now excludes ANY GitHub issue carrying `in_progress_label` unconditionally — closing the gap
    where `mark_in_progress` wrote that label on every pick, durably and visibly to every session,
    and the picker never read it back (an issue the loop had itself just claimed could be
    re-offered as the very next pick, including as the TOP pick). Excluding it unconditionally,
    on its own, would trade that bug for a worse one: a run that crashes mid-goal (claimed, label
    written, never reaches `record`/`release`) would leave the label in place FOREVER, making that
    goal permanently unpickable rather than merely temporarily stuck. This closes THAT gap: it
    reconciles the label against the ledger's own claim-lease TTL, the one place a "crashed" claim
    and a "still being worked" claim are already told apart.

    Gated on the EXISTING `ledger.enabled` switch (default off) — no new config key. With no
    ledger there is no durable claim TIMESTAMP to age a label against, so nothing here can tell
    "crashed" from "still being worked"; an adopter who never turns the ledger on pays literally
    nothing here, byte-identical to before this function existed.

    GitHub-only, and checked on the ACTUAL `source` object passed in — not reconstructed from
    config the way `_auto_unpark_sweep`'s own `sweep_unpark` does its own `sources.GitHubSource(
    config, ...)`. A `LocalSource` (or a test double) has no `in_progress_label` concept at all —
    `LocalSource.mark_in_progress` writes local `state.json`, single-machine, not the durable
    cross-session marker this reconciles against — so there is nothing here to reclaim for one.

    For every OPEN ledger claim aged past `ledger.lease_ttl_seconds(config)` (0/false = never
    expire — `ledger.expired_claims` already returns `{}` then, so this is a no-op): releases it
    through the SAME sanctioned path a human/CLI `release` verb uses (`_release()` — GitHub label
    stripped, board card back in Ready, one audit-trail comment, a `release` ledger entry
    recorded), naming the lease TTL as why. `_release()` mirrors `mark_in_progress`'s own
    best-effort `gh` calls, so a transient GitHub error here can never raise into this sweep.

    FAIL-OPEN, mirroring `_auto_unpark_sweep` exactly: ANY exception (an unreadable ledger, a bad
    config shape, anything `_release()` itself didn't already swallow) is caught here with one
    stderr line — a background reconciliation sweep must never be what stops an unattended pick.

    Returns a `frozenset` of the goal refs THIS call actually released. `_next()` folds this into
    its own pick-loop `skip` set — the SAME cooldown `_auto_unpark_sweep`'s docstring already
    explains: the goal a call just reclaimed must not ALSO be the one that SAME call's own pick
    returns, so a human gets one full `_next()` call's worth of a window on the audit comment
    before autonomous work can start on it. Free again from the very next `_next()` call.

    PR #1235 REVIEW FIX (Finding 1/3): `_release()` below is now told WHO the ledger itself
    recorded as the original claimant (`claim_actor`, threaded straight from `expired_claims()`'s
    own `(actor, writer, ts)` tuple — never re-derived, never guessed) so the `release` entry it
    writes can durably end THAT actor's lease even though the release is self-attributed (audit-
    honestly) to whichever actor is running THIS sweep. Without this, a cross-actor reclaim — the
    norm for any real multi-person ledger, and reachable solo whenever `ledger.actor` drifts
    between runs — never actually cleared `ledger.open_claims()`'s view: the SAME 'expired' claim
    was re-discovered and re-released, posting a duplicate GitHub audit comment, on every
    subsequent call, forever. See `ledger._held()`'s own docstring for the mechanism, and
    `test_auto_reclaim_durably_clears_a_different_actors_stale_claim`/
    `test_auto_reclaim_does_not_repost_a_duplicate_comment_on_a_second_sweep`
    (tests/test_loop.py) for the exact repro proven fixed.

    PR #1235 REVIEW FIX (round 2 — liveness corroboration): elapsed lease time alone is NOT
    sufficient evidence a claim was abandoned. The TTL only measures how long ago the last
    claim-refreshing ledger write landed, not whether the goal is still genuinely being worked —
    a long-running worker that writes no further ledger entries between `claimed` and its own
    eventual `record` (the common shape: exactly one append at each end, none in between) sails
    straight past the TTL while still actively in flight. An unconditional TTL-only sweep would
    therefore reclaim it out from under itself: label stripped, board card back in Ready, goal
    handed to a SECOND `_next()` call while the first is still working it. That is exactly the
    double-work collision #1197 documents for the picker's own claim check (a since-exited
    picker-process pid mistaken for the worker's own liveness) — this sweep had the identical hole
    on its release side, just reached via wall-clock age instead of a dead-pid misread. Fixed by
    requiring BOTH the lease to have expired AND `_goal_has_registered_worker` (loop.py:250 — the
    PER-GOAL `agent_alive` corroboration `_next()` and `work.py`'s
    `_resume_blocked_by_a_live_sibling` also use for the identical "is anyone still genuinely
    driving THIS goal?" question; see the #1284 FIX paragraph below for why this call site uses
    the per-goal check and not the OR'd `_claimed_goal_has_live_worker` `claim_lock_alive` above
    trusts) to report no live worker before a claim is reclaimed. A goal with a live worker is
    left untouched even past the TTL — it becomes eligible again on a later sweep once the worker
    marker itself goes stale, exactly like every other caller of this helper already degrades;
    nothing here invents a new state. This marker is opt-in and fails open to "no marker" when
    `agent_watch.enabled` is unset (see `_goal_has_registered_worker`'s own docstring), so an
    adopter who never turns it on sees byte-identical behavior to before this fix — this only
    NARROWS what gets reclaimed, never widens it. See
    `test_auto_reclaim_does_not_reclaim_a_stale_lease_with_a_live_worker_present`/
    `test_auto_reclaim_still_reclaims_a_stale_lease_with_no_live_worker` (tests/test_loop.py).

    PR #1269 REVIEW FIX (blocking finding 1): the liveness check just above closes the window
    against a genuine long-running WORKER, but not against the SAME actor's own SECOND, concurrent
    PROCESS legitimately re-claiming this exact goal, under a NEW `run_id`, in the real window
    between the `expired`/`expired_runs` snapshot below and this loop's own `_release()` call — the
    `_goal_has_registered_worker` check itself does one or more `gh` calls, not instant. Without
    this fix, `_release()`'s `reclaimed_actor=claim_actor` alone could still wipe that live re-claim
    (same actor name, different process — actor-only scoping cannot tell them apart; see
    `ledger._held()`'s own docstring). Fixed by ALSO threading `claim_run` — the stale claim's own
    `run_id`, sourced from `ledger.expired_claim_run_ids()` at the SAME snapshot moment as
    `claim_actor`, never re-derived after the race window opens — through to `_release()`'s new
    `claim_run` parameter, so its `reclaimed_run` durably names the STALE run being ended, and
    `ledger._held()` can tell it apart from a live reclaim under a different run. See
    `test_auto_reclaim_does_not_wipe_the_same_actors_live_reclaim_mid_sweep` (tests/test_loop.py).

    #1284 FIX (closes the gap found during the #1199 rebase, formerly pinned by what is now
    `test_auto_reclaim_sweep_reclaims_past_an_unrelated_live_session`): this call site used to
    check `_claimed_goal_has_live_worker`, which ORs in the GOAL-AGNOSTIC `session_active` signal,
    so an unrelated live session blocked THIS sweep from reclaiming a DIFFERENT, genuinely crashed
    goal — the same class of bug #1197's third follow-up had already fixed for `_next()`'s own
    `claim_belongs_to_me` check and `work.py`'s `_resume_blocked_by_a_live_sibling`, just never
    applied here (#1198 merged before #1197's correction existed). This matters more since #1199:
    every ordinary `/sigma-loop` call now passes `--session-pid "$PPID"`, so a live, correctly-
    registered session is the NORMAL running state, and `_fetch_pending` (sources.py) unconditionally
    excludes any issue still carrying `in_progress_label` — this sweep is the ONLY thing that clears
    that label after a crash, so the gap made a crashed goal invisible to the picker for as long as
    the unrelated session ran anything at all. Fixed by switching to `_goal_has_registered_worker`
    (per-goal only) above, mirroring #1197's own correction. See
    `test_auto_reclaim_sweep_reclaims_past_an_unrelated_live_session`/
    `test_auto_reclaim_sweep_still_blocked_by_a_live_worker_on_the_same_goal` (tests/test_loop.py)."""
    try:
        if not ledger.enabled(config) or not isinstance(source, sources.GitHubSource):
            return frozenset()
        ttl = ledger.lease_ttl_seconds(config)
        entries = ledger.read_all(sdlc_dir)     # one snapshot -- `expired`/`expired_runs` agree
        expired = ledger.expired_claims(entries, ttl_seconds=ttl)
        expired_runs = ledger.expired_claim_run_ids(entries, ttl_seconds=ttl)
        reclaimed = set()
        for goal, (claim_actor, _writer, _ts) in expired.items():
            if _goal_has_registered_worker(sdlc_dir, goal, config):
                continue                 # lease expired, but a genuine worker is still on it — leave it
            _release(sdlc_dir, source, goal, reason="claim aged past the ledger lease TTL",
                     claim_actor=claim_actor, claim_run=expired_runs.get(goal))
            reclaimed.add(goal)
        return frozenset(reclaimed)
    except Exception as exc:               # noqa: BLE001 - fail-open; see docstring
        print(f"loop.py: auto-reclaim failed non-fatally ({exc}) — continuing", file=sys.stderr)
        return frozenset()


def _feature_needs_label_sweep(sdlc_dir, source, config):
    """#1468: clear the `sdlc:needs-label` overlay from goals held for a `feature:` label that now
    exists. Returns the goals released (`[]` on any problem).

    Runs BEFORE the pick, in `_next()`, so a goal whose label a human created moments ago is pickable
    on this very call — that is the point, and unlike `_auto_unpark_sweep` there is deliberately no
    cooldown: the overlay is the loop's OWN state, not a human's park, so nothing is being overridden
    and there is no decision for a person to catch up on. See `feature_labels.resume_needs_label`.

    GATED ON THE BUDGET BY ITS CALLER, unlike the three sweeps beside it. Those reconcile state the
    loop already owns and are meant to run on every call; this one WRITES to GitHub — removes a
    label, posts a comment — and #1468's own rule is that a call returning `("BUDGET", …)` must not
    have mutated anything. The attach step was moved below the budget gate for exactly that reason,
    and a sweep added above it would have reintroduced the same defect one line higher.

    Fail-open in the same shape as the sweeps above it: a problem here costs one deferred release,
    never a pick."""
    try:
        return feature_labels.resume_needs_label(sdlc_dir, source, config)
    except Exception as exc:                # noqa: BLE001 - a sweep must never break the pick
        try:
            print(f"loop.py: needs-label sweep failed non-fatally ({exc})", file=sys.stderr)
        except Exception:
            pass
        return []


def _feature_needs_unit_sweep(sdlc_dir, source, config):
    """#2263: clear the `sdlc:needs-unit` overlay from goals held for declaring no unit at all,
    now that one has been declared. Mirrors `_feature_needs_label_sweep` exactly -- same position
    (budget-gated, before the pick, in `_next()`), same no-cooldown reasoning, same fail-open
    shape -- see that function's own docstring, which this one does not repeat. See
    `feature_labels.resume_needs_unit`.

    Byte-identical-when-off: `resume_needs_unit` itself returns `[]` before making any call when
    `no_dangling_goal_enabled` is False (the default) or the source lacks the method surface, so
    an adopter who never opted in pays nothing beyond one cheap attribute read per call, here."""
    try:
        return feature_labels.resume_needs_unit(sdlc_dir, source, config)
    except Exception as exc:                # noqa: BLE001 - a sweep must never break the pick
        try:
            print(f"loop.py: needs-unit sweep failed non-fatally ({exc})", file=sys.stderr)
        except Exception:
            pass
        return []


def _feature_scope_ok(source, goal, unit):
    """#1661: the LAST word on `--feature` exclusivity -- may this run claim `goal`, given that
    `unit` is what it actually resolves to? True when no scope is in force.

    THE GUARANTEE, in the settled wording shared with `docs/branching-model.md` §14 and
    `/sigma-goal` (`tests/test_docs.py` pins the three copies against each other, byte for byte):

        While a `--feature` run is active, no goal outside that unit may be picked.

    Three commitments come with it, and this function plus `sources._issue_in_feature` are where
    they are kept:

      1. EXCLUSIVE, NOT PREFERENTIAL. Not as a fallback, not when the unit is drained, not as a
         "nothing else to do" convenience. When the unit has nothing pickable left the run STOPS
         (`_next` returns `("DONE", None)`) and `_feature_drained_note` says which kind of empty
         that was -- there is no widening back out to the backlog anywhere in the pick path.
      2. MEMBERSHIP IS THE DECLARATION PAIR, NEVER THE LABEL ALONE. The label is attached at pick,
         so a member nobody has picked yet may carry none; `sources._issue_in_feature` therefore
         adjudicates §4's pair through `features.read` rather than filtering the query on the
         label. See that method for what it costs and the one place a label filter survives.
      3. AN EXCLUDED GOAL IS LEFT UNTOUCHED -- not relabelled, not commented on, not parked, not
         claimed. See the paragraph on `_scope_ok_at_pick` below.

    WHY A SECOND CHECK EXISTS AT ALL, given (2) already filters the pool. `sources` decides
    membership from the LIST payload; `_unit_at_pick` is the value every OTHER pick-time gate and
    every downstream step (base resolution #1467, the registry #1469, sibling propagation) actually
    acts on, and the two can differ -- `_unit_at_pick` re-reads over REST when the GraphQL read
    failed (#1567), and a body edited between the list read and the claim resolves differently.
    Refusing on the resolved unit means the run refuses on exactly the value the rest of the pick
    will use, and it holds for any future queue path that never passes through `_fetch_pending`.

    A `unit` OF None IS REFUSED TOO, and that is commitment 1 stated in code: a goal that declares
    nothing is not "unclaimed by any unit and therefore available", it is outside the one unit this
    run was told to work. That covers the `UNREADABLE` case for free -- a declaration we could not
    read is not evidence the goal belongs here.

    NOT A REFUSAL IN THE `_scope_ok_at_pick`/`_owner_ok_at_pick` SENSE, and it must run BEFORE
    both. Those two hold a goal for a human (`sdlc:needs-confirmation`, a comment, a ledger line)
    because the goal itself has a problem. This one is not about the goal at all -- it is this
    run's own narrowing, and the goal is perfectly fine for the run that IS scoped to its unit. So
    nothing is written to it: no label, no overlay, no comment, no claim. It is simply passed
    over, exactly like a `--skip`ped goal, and the next candidate is tried."""
    wanted = getattr(source, "feature", None)
    if not wanted:
        return True
    if unit and str(unit).lower() == str(wanted).lower():
        return True
    print("loop.py: not claiming #%s — this run is confined to unit %r and #%s %s. Left untouched "
          "(still %s, no claim); taking the next candidate"
          % (goal, wanted, goal,
             ("belongs to %r" % unit) if unit else "declares no unit",
             getattr(source, "goal_label", "sdlc:goal")), file=sys.stderr)
    return False


def _feature_drained_note(source, skip):
    """#1661: the ONE thing a scoped run owes its operator when it stops -- WHICH kind of empty.

    "Nothing pickable" and "this unit has nothing pickable, though the board does" are different
    facts, and only the second explains why a run stopped with visible work still open. Without it
    a scoped run reads exactly like a finished backlog, which is the same failure mode
    `_warn_unseeded` and #1499's own held-goal summary were both written to close; this is the
    third instance of that pattern and it keeps their voice.

    Costs ONE query, on the terminal path only -- see `sources.pending_outside_feature`, which also
    explains why it is safe to run before `_emit_run_stop_once`. Fail-open: a probe that cannot
    answer says nothing rather than guessing, because a wrong answer here is worse than silence."""
    wanted = getattr(source, "feature", None)
    probe = getattr(source, "pending_outside_feature", None)
    if not wanted or not callable(probe):
        return
    try:
        outside = probe(skip)
    except Exception as exc:                    # noqa: BLE001 - a diagnostic never breaks a stop
        print("loop.py: unit %r has nothing pickable (and the check for work outside it did not "
              "run: %s)" % (wanted, exc), file=sys.stderr)
        return
    if outside:
        print("loop.py: unit %r has nothing pickable — but the rest of the board does (#%s is next "
              "once the scope is lifted). --feature is exclusive, so nothing outside the unit was "
              "picked, or could have been. Drop the flag to work the rest of the board."
              % (wanted, outside), file=sys.stderr)
    else:
        print("loop.py: unit %r has nothing pickable, and neither has the rest of the board — the "
              "backlog is drained, not merely out of scope" % wanted, file=sys.stderr)


#: `.sdlc` dirs whose board mirror THIS PROCESS has already force-refreshed because a goal it was
#: about to claim was missing from it. One process, one forced refresh: a goal that is absent for a
#: reason a refetch cannot fix (past `mirror._OPEN_LIMIT`, or simply not carrying the goal label any
#: more) must not buy two `gh` calls on every pick, forever. See `_pick_dependency_hold`.
_MIRROR_FORCED = set()


def _dependency_gate_mode(config):
    """`discovery.dependency_gate.mode` -> 'on' (DEFAULT) | 'off'.

    The OPPOSITE default from `_reconcile_mode` / `sources._auto_unpark`, and the asymmetry is
    deliberate rather than an oversight. Those gate mechanisms that WRITE to the board, so an
    unrecognised value there must read as off -- a typo can never be allowed to start mutating a
    repository. This gate's only power is to DECLINE to claim something: the worst a spurious hold
    can do is defer one pick to the next call with the reason printed, while the failure it
    prevents is an unattended run stalling outright on a dependency graph. So a typo reads as ON.

    The off switch exists for the residual false positive the vocabulary cannot rule out (see
    `blocker_scan.TRIGGERS`): a goal whose body says `blocked by #N` about something that is not
    really a dependency has no cheap per-goal escape at pick time, because the sanctioned dismissal
    (`backlog_check.dismiss_comment`) lives in a COMMENT and reading comments is exactly the
    per-candidate remote call this gate refuses to make. Editing the body is the per-goal fix; this
    is the repo-wide one."""
    block = ((config or {}).get("discovery") or {}).get("dependency_gate")
    value = block.get("mode") if isinstance(block, dict) else None
    return "off" if value == "off" else "on"


def _mirror_index(mirror, sdlc_dir):
    """{"<issue number>": <mirror record>} off `board-mirror.ndjson`. A garbage line is already
    dropped by `read_mirror`; a non-dict record is dropped here rather than poisoning the index."""
    return {str(r.get("number")): r for r in mirror.read_mirror(sdlc_dir) if isinstance(r, dict)}


def _resolve_blockers(by_ref, refs):
    """`(holding, unknown)` for `refs` resolved against a mirror index. Pure, no I/O.

    Split out of `_pick_dependency_hold` because #1650 runs it TWICE — once against the mirror as
    found on disk, and again against a forced refresh when the first answer was a hold. Two
    hand-written walks of the same records would be free to disagree about what "open" means, which
    is the mistake `blocker_scan.read_refs` exists to prevent one layer down."""
    holding, unknown = [], []
    for ref, phrase in refs:
        blocker = by_ref.get(ref)
        if blocker is None:
            unknown.append(ref)
        elif str(blocker.get("state") or "open").lower() == "open":
            holding.append("#%s (%s)" % (ref, phrase or "blocked by"))
    return holding, unknown


def _mirror_age_phrase(mirror, sdlc_dir):
    """How old the mirror on disk is, in words, for the one message that has to admit its evidence
    is stale. Sub-minute reads as "seconds ago" rather than "0 minute(s) ago"."""
    age = mirror.age_seconds(sdlc_dir)
    if age is None:
        return "at an unknown time"
    minutes = int(age // 60)
    return "%d minute(s) ago" % minutes if minutes else "seconds ago"


def _pick_dependency_hold(sdlc_dir, source, goal, config, cache):
    """#1499: the reason `goal` must NOT be claimed on this pick -- a prerequisite it declares that
    is still OPEN -- or `""` when it may be claimed.

    THE BUG THIS CLOSES. `_next()` claimed first and asked afterwards. The only reader of the
    dependency data was `precheck`, which runs AFTER `mark_in_progress`: the label is on, the claim
    lock is taken, the issue is out of everyone else's queue, and the best available outcome is a
    claim-then-park round trip that consumed a lane. Measured live on a real DAG-shaped backlog
    (three goals, two of them `**Blocked by:** #1594`): three of four picks claimed a goal whose
    blocker was still open, and EVERY pick after the first returned the same blocked goal, so the
    loop could not reach the one pickable issue behind it without a human `--skip` on every call.
    An unattended run does not degrade on a dependency graph -- it stalls.

    A HOLD IS NOT A PARK, and that ordering is the whole fix. The goal is left exactly as it was
    found -- `sdlc:goal` intact, no `sdlc:in-progress`, no lock, no comment, no ledger line -- and
    the pick moves to the next candidate. It becomes pickable by itself the moment its blocker
    closes; nothing has to unpark it. This is why the check sits at the TOP of `_next()`'s candidate
    loop, ahead of the lease read and the claim lock, rather than anywhere after them.

    WHAT IT COSTS PER PICK: one local read of `board-mirror.ndjson`, memoized in `cache` for the
    whole `_next()` call AND RE-POPULATED BY AT MOST ONE FORCED REFETCH (the absence hole below, or
    #1650's hold refresh -- never both, and the two paragraphs after this one price them), plus
    `mirror.fetch_and_write`, which is TTL-guarded and therefore a stat and a return on a fresh
    mirror. ZERO `gh` calls whenever nothing is held — which is every pick
    on a backlog with no open dependency edge, i.e. the overwhelming common case. That bound is not
    a nicety: a single pick already burns ~35-45 GraphQL calls against a 5,000/hr bucket shared by
    every session on the account, so a gate that read one issue per candidate would compete with the
    disease it cures. Nothing here is per-candidate except a dict lookup. (The TTL refetch itself is
    two `gh issue list` calls per TTL window, and it is the SAME call `precheck` already makes, so
    on a `backlog_check.enabled` repo the gate adds nothing at all; on a repo without it, it is two
    calls an hour, which is what makes the gate work on a stock config rather than only where the
    cross-check happens to be switched on.)

    A HOLD, AND ONLY A HOLD, BUYS A REFRESH (#1650) — two more `gh issue list` calls, at most ONCE
    per `_next()` call however many candidates are held, and never when this call already fetched
    live. The measured defect: `ttl_minutes` ships at 60, so a blocker an operator had just closed
    by hand went on holding its dependents for up to an hour while the loop reported `DONE, nothing
    pickable` — indistinguishable from a stall, and misdiagnosed as one by an earlier trial. The
    branch is deliberately here rather than beside the absence check above: staleness only changes
    the OUTCOME when the answer is a hold, so paying for it anywhere earlier would buy the call on
    picks the mirror was already right about. Not bounded per PROCESS the way `_MIRROR_FORCED` is,
    and that asymmetry is the point — a goal absent from the mirror is absent for a reason a second
    fetch cannot fix, whereas a blocker's state is exactly the thing that changes underneath a
    long-lived `run_loop`, so the bound has to reset every call. Worst case is 2 calls per call that
    holds; best and common case is unchanged at zero (`test_a_goal_whose_declared_prerequisites_
    are_all_closed_pays_nothing`).

    THE REFRESH IS MERGED OVER THE MIRROR, NEVER SUBSTITUTED FOR IT. A refreshed corpus that no
    longer carries a blocker (its goal label removed, past `mirror._OPEN_LIMIT`, an assignee filter)
    holds LESS evidence than the mirror already did, and the last thing observed about that blocker
    was that it was OPEN. Replacing the index would demote it to `unknown`, which fails open — the
    right posture for a ref nobody has ever seen, and the wrong one for a ref seen open a minute
    ago. So the fresh records win where they exist and the old ones survive where they do not; a
    refresh can release a hold but can never lose the evidence behind one. And when the refresh
    cannot be made at all (no `gh`, offline, a `gh` error object), the hold STILL STANDS and the age
    of the evidence behind it is printed — #1650's own weaker remedy, kept as the fallback for
    exactly the case where the stronger one cannot run.

    THE `goal_not_in_corpus` HOLE, and why a TTL alone is not enough. A mirror that is fresh by TTL
    but was written BEFORE the issues now being picked existed answers "I have no dependency data
    for this goal" in exactly the same shape as "I checked, there is none". With the default
    60-minute TTL that is a coin flip immediately after a batch of issues is filed -- which is
    precisely when a dependency chain is newest and most load-bearing. So a goal ABSENT from the
    mirror forces one refresh rather than proceeding on no evidence, bounded by `_MIRROR_FORCED` to
    once per process, and skipped entirely when this same call already fetched live.

    FAIL OPEN, AND SAY SO. Three states cannot be determined, and all three CLAIM: no mirror at all,
    a goal still absent after the forced refresh, and a prerequisite the mirror does not carry (its
    open half is `sdlc:goal`-filtered and its closed half is windowed, so an open non-goal issue or
    an old close resolves to nothing). Claiming is the recoverable direction -- a wrongly-held goal
    stalls the loop, which is the bug being fixed -- but an undeterminable state is never silent:
    each prints what it could not resolve. The outer `except` has the same posture as
    `_auto_unpark_sweep`'s: a gate that breaks must never be the thing that stops an unattended pick.

    ONE VOCABULARY. The refs come off the mirror record via `blocker_scan.read_refs` -- the field
    `blocker_scan.extract_refs` wrote at fetch time, read back by the module that wrote it. No regex
    runs here and no second parser exists; `test_the_pick_gate_declares_no_blocker_vocabulary_of_
    its_own` pins that at the source level. Note this is the EXPLICIT subset (`blocked by` /
    `depends on` / `depends upon`) rather than the wider check-time set: declining to claim is an
    autonomous act, and that subset is the one measured on a real board at whole-body width (21 of
    21 genuine edges, no phantom -- see `blocker_scan.TRIGGERS`). A weak trigger inside the excerpt
    is still found by `precheck`, where a human sees a comment and can dismiss it."""
    try:
        if _dependency_gate_mode(config) == "off":
            return ""
        mirror = cache.get("mirror")
        if mirror is None:
            mirror = cache["mirror"] = _load("mirror")
        if not mirror.is_github_mode(config):
            return ""                      # local-files mode has no mirror; unchanged from before
        run = getattr(source, "_run", None)     # the source's own transport, so this stays hermetic
        if "by_ref" not in cache:               # in tests and reuses whatever auth the run is using
            cache["live"] = mirror.fetch_and_write(sdlc_dir, config=config,
                                                   run=run) is not None
            cache["by_ref"] = _mirror_index(mirror, sdlc_dir)
        by_ref = cache["by_ref"]
        rec = by_ref.get(str(goal))
        if rec is None and not cache["live"] and str(sdlc_dir) not in _MIRROR_FORCED:
            _MIRROR_FORCED.add(str(sdlc_dir))
            # #1650: the flag is SHARED with the hold refresh below, so a goal that is both
            # absent from the mirror and blocked buys one fetch on this call rather than two.
            if mirror.fetch_and_write(sdlc_dir, config=config, run=run, force=True) is not None:
                cache["live"] = True
            by_ref = cache["by_ref"] = _mirror_index(mirror, sdlc_dir)
            rec = by_ref.get(str(goal))
        if rec is None:
            print("loop.py: #%s is not in the board mirror even after a refresh, so its "
                  "prerequisites could not be checked — claiming it anyway" % goal, file=sys.stderr)
            return ""
        refs = mirror.blocker_scan.read_refs(rec, self_ref=goal)
        if not refs:
            return ""                      # the overwhelming common case: nothing declared, no cost
        holding, unknown = _resolve_blockers(by_ref, refs)
        unconfirmed = ""
        if holding and not cache["live"] and not cache.get("hold_refresh"):
            cache["hold_refresh"] = True   # #1650: once per CALL, whatever it walks past
            if mirror.fetch_and_write(sdlc_dir, config=config, run=run, force=True) is not None:
                cache["live"] = True
                # MERGED, not replaced: a record the refresh dropped keeps its last known state.
                by_ref = cache["by_ref"] = {**by_ref, **_mirror_index(mirror, sdlc_dir)}
                rec = by_ref.get(str(goal), rec)
                refs = mirror.blocker_scan.read_refs(rec, self_ref=goal)
                holding, unknown = _resolve_blockers(by_ref, refs)
            else:
                unconfirmed = _mirror_age_phrase(mirror, sdlc_dir)
        if unknown:
            print("loop.py: #%s names prerequisite(s) %s the board mirror does not carry — neither "
                  "an open goal nor a recent close, so their state is undeterminable and they are "
                  "NOT treated as blocking" % (goal, ", ".join("#" + r for r in unknown)),
                  file=sys.stderr)
        if holding and unconfirmed:
            print("loop.py: could not refresh the board mirror, so #%s's hold rests on evidence "
                  "written %s — a prerequisite closed since then is not visible yet"
                  % (goal, unconfirmed), file=sys.stderr)
        return ", ".join(holding)
    except Exception as exc:                # noqa: BLE001 - fail-open; see the docstring
        print("loop.py: dependency gate failed non-fatally (%s) — claiming anyway" % exc,
              file=sys.stderr)
        return ""


# --------------------------------------------------------------- #1962: the claim, in one place
#: Verbs that are evidence work on a goal has BEGUN, and whose argv shape is uniformly
#: (verb, sdlc_dir, goal, ...). `note` already takes a goal-scoped side effect keyed on exactly
#: `argv[2], argv[3]` (its own `agent_heartbeat_all` call), so this is an established shape here,
#: not a new one.
#:
#: `record` is deliberately NOT here, and that exclusion is the whole design. It is evidence work
#: has ENDED, so arming there would write claimed_ts ~= terminal_ts -- a synthesized span, which is
#: precisely what #1962 forbids ("Never synthesize a timestamp to make a number look better"). A
#: goal whose only loop.py call is `record` stays honestly unclaimed, and the downstream
#: claimed-coverage gap report is what makes that residue visible instead of silent.
#:
#: `qc` was in this tuple too and is out for BOTH reasons at once, caught by the full suite rather
#: than by inspection (`test_cli_qc_verb_is_a_safe_noop_for_local`). It is a Review-phase verb, so
#: arming there lands claimed_ts almost on terminal_ts -- the same drift toward a synthesized span
#: that `record` is excluded for -- and it is contractually BOARD-ONLY, so claiming through it made
#: `mark_in_progress` rewrite a local goal file that the verb promises never to touch. `note`
#: already covers every /sigma-goal phase from P1 onward, so nothing is lost by dropping it.
#:
#: `emit`, `log`, `precheck` and `decompose-check` were in this tuple in the first draft and were
#: removed at plan-review. They are EVALUATIVE, not committal: predict.py's `_emit_model_choice`
#: shells out to `loop.py emit <goal> model_choice` from an ADVISORY /sigma-model question that can
#: be asked about ANY goal, and `precheck`/`decompose-check` describe themselves below as an
#: "opt-in pre-work backlog cross-check" and an "opt-in oversized-goal classifier". Arming on those
#: reaches `mark_in_progress`, writing `sdlc:in-progress` on an issue nobody is working -- which
#: `_next()` then reads as claimed and SKIPS. Asking which tier a goal deserves would have silently
#: removed it from the backlog. Regression-covered both ways.
_ARMS_CLAIM = ("note", "verify", "agent-start")


def _claim(sdlc_dir, source, goal, config, session_pid=None, why=None, mark=True):
    """The claim, in ONE place (#1962). Extracted verbatim from `_next()` so that `next`, the
    `claim` verb and `_ensure_claimed` cannot drift into three subtly different claims.

    ORDER IS LOAD-BEARING and unchanged from `_next()`: `mark_in_progress` FIRST (the durable,
    cross-session signal every other session reads), then the ledger entry, then the actionlog,
    then the session registry.

    `why` is passed only by the self-arming path, so an armed claim stays distinguishable from a
    picked one in the ledger forever. It costs the pick path nothing: `append()` drops a None
    field, so a `next`-issued claim is written byte-for-byte as it was before this parameter
    existed (verified empirically, not assumed).

    `mark=False` (#2029) writes the LOCAL half only -- ledger, actionlog, session registry -- and
    skips `mark_in_progress`, the one step of a claim that reaches OUTSIDE this machine. Only
    `_ensure_claimed` ever passes it, and only for a target that is not a member of the backlog;
    see `_arming_may_mark`. The pick path (`_next`, the `claim` verb) never passes it and is
    byte-for-byte unchanged, because there membership is already proven -- `_fetch_pending` asks
    the server for `--label sdlc:goal` -- so marking IS picking.

    #2392: `mark_in_progress` now reports back whether its own claim-label write actually landed
    (`True`), or genuinely exhausted its retries and failed (`False` -- `GitHubSource` only;
    `LocalSource.mark_in_progress` returns `None`, which the `is False` check below deliberately
    does not match, since a local goal has no external label to fail). `mark_in_progress` already
    escalates loudly to stderr on that failure (`_escalate_claim_label_failure`) -- this folds the
    SAME fact into the `claimed` entry's own `why` too, so it is not lost once that stderr line
    scrolls away. Reuses the EXISTING `why` OPTIONAL_FIELD rather than adding a new ledger field:
    `why` is already free text, already rendered wherever a claimed entry is read, and every
    non-`None` `_swap_labels_best_effort` call site elsewhere in this codebase already writes to
    stderr, never to a dedicated ledger field, for the identical class of failure. Appended after
    any self-arming `why`, not replacing it, so an armed claim whose label write also failed keeps
    BOTH facts, not just one."""
    if mark:
        marked = source.mark_in_progress(goal)
        if marked is False:
            note = "claim-label write failed after retries -- cross-session exclusion degraded (#2392)"
            why = f"{why} | {note}" if why else note
    # #1121: run_id threads state.run_identity() (SIGMA_RUN_ID) onto the claim, so
    # ledger._held()'s stale-terminal-entry guard can scope by (actor, run) rather than actor
    # alone -- see that function's own docstring for the two-concurrent-processes-of-one-actor
    # race this closes. None (unset) writes exactly as before -- append() drops a None/"" field.
    ledger.safe_append(sdlc_dir, "claimed", goal, config=config,
                       run_id=state.run_identity(), why=why)
    actionlog.safe_append(sdlc_dir, goal, "claimed")
    _session_claim(sdlc_dir, session_pid, goal)     # #1199: register in-flight for THIS session


def _claim_marker_path(sdlc_dir, goal):
    """Sibling of `_claim_lock_path` -- same directory, same stem rule, same unsafe-goal refusal --
    recording that THIS machine has already established a claim for `goal`. Distinct from the
    `.lock` file, which is a short-lived mutex released at the end of the pick; this one is a
    durable "already done" marker, and its only job is keeping `_ensure_claimed` O(1)."""
    stem = work.stem(goal)
    reason = state.unsafe_goal_reason(stem)
    if reason:
        raise ValueError(f"unsafe goal {goal!r} for the claim marker: {reason}")
    return pathlib.Path(sdlc_dir) / "state" / "claims" / f"{stem}.claimed"


def _arming_may_mark(source, goal):
    """May the ARMING path write `in_progress_label` onto `goal`? (#2029)

    THE BUG THIS EXISTS FOR. `_ensure_claimed` guarded on `ledger.enabled(config)` and on nothing
    else, so `loop.py note` -- armed by `_ARMS_CLAIM` -- ran a full `_claim` on WHATEVER issue
    number it was handed, and `_claim`'s first act writes `sdlc:in-progress` to a real issue.
    `sigma-goal-review` closes on two `loop.py note` calls that deliberately target tickets which
    are NEVER goals: the Dossier (`story`) in SKILL.md §4d and the Spec/Epic in §5g. On
    2026-09-01 that labelled story #2017 and epic #2020 in progress, 2 seconds before each
    comment landed -- reproduced deterministically, not inferred.

    Three things were wrong at once, and the membership question fixes all three:
      * `docs/label-model.md` §4a's ORPHAN class -- `{sdlc:in-progress}` with no membership label
        -- was being MANUFACTURED by the tool whose `doctor.py:_orphan_in_progress_scan` exists to
        report it.
      * `loop.py`'s own contract for this very verb ("for `sigma-goal-review`'s REJECT verdict,
        this note is THE ENTIRE OUTPUT: no label, no board move, nothing else"), repeated in
        `skills/sigma-goal-review/SKILL.md` and in `AGENTS.md`, was false: a REJECT wrote a label.
      * on a real but UNPICKED goal it is a silent backlog deletion bounded by the lease --
        `_fetch_pending` excludes any issue carrying `in_progress_label` unconditionally, so a
        `note` from `/sigma-plan-review` or `/sigma-retro` removed a pending goal from the queue
        until `_auto_reclaim_stale_claims` reclaimed it 12h later.

    THE POSTURE IS DELIBERATELY ASYMMETRIC: fail OPEN on the local record, fail CLOSED on the
    external write. A claim we could not justify is still worth recording locally (that is the
    whole point of #1962 -- `claimed_ts` for goals the picker never chose), but a label we could
    not justify is a write onto somebody else's issue. So an unreadable answer skips the mark and
    says so on stderr; it never blocks the verb, and it never blocks the ledger entry.

    A SOURCE WITH NO UNIT SURFACE KEEPS TODAY'S BEHAVIOUR EXACTLY. `LocalSource` has no
    `fetch_body_labels` -- the same `hasattr` gate `feature_stamp.unit_of` uses for the same reason
    -- and in local mode a goal resolves to a file under `.sdlc/goals/`, so membership is inherent
    in the argument and there is no external surface to protect. Returning True there is not a
    concession; it is the correct answer.

    COST: one `gh issue view --json body,labels` per goal per machine, and no more, because the
    only caller reaches it behind `_ensure_claimed`'s O(1) marker on the miss path -- the same
    place the O(ledger) read already sits. `test_arming_reads_the_whole_ledger_at_most_once_per_goal`
    still holds, and `test_the_membership_question_is_asked_at_most_once_per_goal` pins this
    call to the same bound."""
    if source is None or not hasattr(source, "fetch_body_labels"):
        return True
    want = getattr(source, "goal_label", None) or "sdlc:goal"
    try:
        issue = source.fetch_body_labels(goal)
    except Exception as exc:                # noqa: BLE001 - "could not tell" is not "it is a goal"
        why = "its labels could not be read (%s)" % (exc,)
    else:
        names = [(l.get("name") if isinstance(l, dict) else l) or ""
                 for l in (issue.get("labels") or [])]
        if want in names:
            return True
        why = "it does not carry %s, so it is not a goal Sigma may claim" % (want,)
    print("loop: not marking %s in progress -- %s (#2029)" % (goal, why), file=sys.stderr)
    return False


def _ensure_claimed(sdlc_dir, goal):
    """Write a claim for `goal` iff nothing has claimed it yet. Never raises and never gates the
    verb that triggered it -- the same fail-open, best-effort posture as `_ensure_watcher`.

    WHY THIS EXISTS IN PYTHON RATHER THAN IN A SKILL.md LINE. `/sigma-goal` names its own goal
    instead of asking the picker for one, so nothing on that path emitted `claimed` at all. A
    SKILL.md instruction is prose an agent may forget, and this repo has already corrected exactly
    that failure mode once -- see `_predict_model_choice_at_pick`, moved into the code because a
    real `model_choice` event was "depending on a calling skill's SKILL.md remembering a second
    instruction". Cursor has no hooks either, so the trigger has to live here.

    TWO-STAGE CHECK, and the order is a measured cost decision. `ledger.read_all` is O(the whole
    ledger): measured 31 ms mean / 224 ms cold over 1,591 entries across 1,193 files on this repo,
    and the ledger only ever grows -- ~310 ms at 10x and ~3.1 s at 100x, which `note` would then
    pay on EVERY phase of EVERY goal. So a per-goal marker file is asked first (one stat, O(1)),
    and the full ledger read is reached only on a miss: at most once per goal per machine, never
    in steady state. `test_arming_reads_the_whole_ledger_at_most_once_per_goal` asserts that
    bound rather than trusting this paragraph.

    THE LEDGER STAGE IS NOT OPTIONAL, and the marker is deliberately not the only check: the
    marker is local and the actionlog is local-only and never shipped, so a goal claimed by a
    teammate's loop on ANOTHER machine has a ledger claim and nothing local at all. The cheap
    check alone would write it a second claim.

    THE TIMESTAMP IS AN OBSERVATION, NOT A FABRICATION. It is the first moment Sigma observed
    work on this goal, which is later than the true pick-up by however long the agent took to
    reach its first goal-scoped verb. `why` records which kind of claim it is, so the two are
    never conflated downstream.

    THE LEDGER OPT-IN IS NOT THE ONLY GUARD, and treating it as one was #2029. It answers "may
    Sigma record anything here?", never "is this ticket a goal?" -- so on an opted-in repo the
    claim's external half landed on whatever number the verb was handed, including the Dossier and
    the Epic that `sigma-goal-review` closes on by design. `_arming_may_mark` asks the membership
    question, and only the EXTERNAL half is gated on the answer: the ledger entry, the actionlog
    and the session registry are written either way, because they are this machine's own record
    of work it genuinely observed."""
    try:
        marker = _claim_marker_path(sdlc_dir, goal)
        if marker.exists():
            return                                  # O(1) -- the steady-state path
        config = state.load_config(sdlc_dir)
        # OPT-IN, and this guard is not a formality. `_claim` calls `source.mark_in_progress`,
        # which on a github-mode repo WRITES `sdlc:in-progress` to a real issue. A repo that has
        # not switched the ledger on gets no claimed event out of this (safe_append would write
        # nothing), so arming would be pure external side effect on an operator who opted out --
        # a `note` silently relabelling their board. `next` may mark in progress unconditionally
        # because marking IS picking; arming is not picking. No marker is written either, so the
        # moment the ledger is switched on, arming starts working with nothing to un-stick.
        if not ledger.enabled(config):
            return
        claimed = any(e.get("kind") == "claimed" and str(e.get("goal")) == str(goal)
                      for e in ledger.read_all(sdlc_dir))
        if not claimed:
            # Deliberately unlocked, unlike `_next()`'s pick. Two concurrent `note`s on one goal
            # could both miss the marker and both claim; the cost is a duplicate `claimed` entry,
            # and a downstream reader derives the claim time as the EARLIEST `claimed` entry, so a
            # duplicate is inert for every metric downstream. A lock here would buy nothing and
            # add a second place claim locking can deadlock.
            source = sources.get_source(sdlc_dir, config)
            _claim(sdlc_dir, source, goal, config,
                   why="armed at first observed work (#1962)",
                   # #2029: the ONE thing a claim does outside this machine is gated on
                   # membership. Arming is not picking, and `note`/`verify`/`agent-start` are
                   # routinely aimed at a Dossier or an Epic, neither of which is ever a goal.
                   mark=_arming_may_mark(source, goal))
        # Written on BOTH paths: a goal `next` already claimed must also stop paying the ledger
        # read on every later verb, not just one this function armed itself.
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    except Exception as exc:                        # noqa: BLE001 - never break the caller's verb
        print(f"loop: claim not armed (non-fatal): {exc}", file=sys.stderr)


def _unit_tracking_cooldown_path(sdlc_dir, goal):
    """Sibling of `_claim_marker_path` -- same directory family, same unsafe-goal refusal -- but a
    COOLDOWN marker (mtime-based, `_ledger_delivery_cooldown_path`'s shape) rather than a
    once-ever one: unlike a claim, a goal's declared unit can change AFTER the first trigger that
    observed it (the classifier attaching a label late, or a human editing the issue), so
    `_ensure_unit_tracking` has to keep re-asking, not just ask once per goal per machine. The
    cooldown is what keeps "keep re-asking" affordable -- see that function's own docstring."""
    stem = work.stem(goal)
    reason = state.unsafe_goal_reason(stem)
    if reason:
        raise ValueError(f"unsafe goal {goal!r} for the unit-tracking marker: {reason}")
    return pathlib.Path(sdlc_dir) / "state" / "unit-tracking" / f"{stem}.attempt"


def _ensure_unit_tracking(sdlc_dir, goal, cooldown_s=300):
    """A side job, and ONLY a side job: on every goal-scoped trigger (`_ARMS_CLAIM`'s own set --
    `note`, `verify`, `agent-start`), confirm the goal the loop is actively working still has a
    proper unit recorded BOTH on its issue (the `feature:<name>` label, or a bare body marker) AND
    in that unit's own registry entry, and repair the registry side when the two have drifted apart.

    MODELLED DIRECTLY ON `_ensure_watcher`/`_ensure_ledger_delivery`, PER THE EXPLICIT REQUEST THAT
    MOTIVATED IT: config-gated, fail-open, called from the same class of trigger those two are
    ("a loop trigger keeps X alive on its own"), and never once load-bearing to the pick or the
    goal it rides alongside. It NEVER blocks, NEVER raises into the verb that triggered it, and its
    only visible effect on failure is a warning: one stderr line (`feature_labels._note`, via
    `ensure_unit_tracking`) and, at most once per issue, one comment on it
    (`feature_labels._flag`) -- exactly "flag a warning... nothing breaking the main purpose... just
    a side job", never a second, competing answer to what `feature_sync.sync_at_pick` (`work.start`)
    already owns authoritatively at pick time. See `feature_labels.ensure_unit_tracking`'s own
    docstring for the full design and for why this is not the "second sync path" that module's own
    `main()` explicitly forbids.

    THE COOLDOWN IS THE COST CONTROL, and its reason is `_ensure_ledger_delivery`'s reason restated
    for a REST read instead of a ledger read: a goal-scoped verb fires many times across one goal's
    life (once per SDLC phase, typically), and re-reading the issue on every single one would be
    real, avoidable network cost for a check whose answer changes rarely. The marker is touched
    BEFORE the attempt (mirroring `_ensure_ledger_delivery`'s own ordering) so a check that itself
    raises cannot spin the next trigger into retrying the same expensive read immediately.

    `cooldown_s` defaults to the same 300s `_ensure_ledger_delivery` uses, for the same reason: no
    measurement argues for a different number, and matching an existing default is one fewer knob
    for an operator to have to understand is unrelated to the other one."""
    try:
        config = state.load_config(sdlc_dir)
        discovery = config.get("discovery") if isinstance(config, dict) else None
        no_dangling = discovery.get("no_dangling_goal") if isinstance(discovery, dict) else None
        if not (isinstance(no_dangling, dict) and no_dangling.get("enabled") is True):
            return
        marker = _unit_tracking_cooldown_path(sdlc_dir, goal)
        try:
            age = time.time() - marker.stat().st_mtime
        except OSError:
            age = None
        if age is not None and age < cooldown_s:
            return                            # bounded: at most one real check per cooldown window
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        except OSError:
            pass                               # a marker we cannot write must not stop the check
        source = sources.get_source(sdlc_dir, config)
        feature_labels.ensure_unit_tracking(sdlc_dir, source, goal, config)
    except Exception as exc:                    # noqa: BLE001 - fail-open by design, same as _ensure_watcher
        print(f"loop: unit-tracking not checked (non-fatal): {exc}", file=sys.stderr)


def _next(sdlc_dir, source, config, extra_skip=(), session_pid=None, refresh_heartbeat=True):
    """(kind, goal_or_reason): 'goal' (+marks in_progress, the commit point — second element is
    the goal ref), 'DONE' (drained — second element None on a genuine empty read, or, #1084, a
    short degraded-read reason string when `source.read_degraded()` is True — mirrors BUDGET's own
    shape), 'BUDGET' (second element a short diagnostic naming which ceiling tripped and the
    observed-vs-configured numbers, #411 — see `_budget_reason`; never None once budget genuinely
    trips), 'HANDOFF' (#2521, same shape as BUDGET's own diagnostic, from `_handoff_reason` — a
    deliberate, healthy context-bound stop). Precedence, most-checked-first: a REAL resource BUDGET
    stop (max_minutes/max_tokens, #2515) beats HANDOFF beats the remaining (max_iterations-only)
    BUDGET case — see the three-part gate's own comments just above its call site for why a resource
    ceiling cannot be allowed to lose that tie the way the iterations one deliberately does.
    Drained backlog reports DONE even
    if budget is also spent (empty wins the tie). A goal another actor — or a DIFFERENT, still-live
    process of MY OWN actor — already holds a ledger claim on is skipped (see
    `_lease`/`ledger.claim_belongs_to_me`, F10.5/#374) so two loops never double-start it, whether
    they're two different people or two of one person's own concurrent sessions. #1197: "still-live
    process" is no longer judged on the claim's WRITER pid alone — that pid is always the
    short-lived picker invocation that wrote the claim, dead within moments regardless of whether
    the goal itself is still being worked — a dead writer pid is now corroborated against
    `_goal_has_registered_worker` (passed as `claim_belongs_to_me`'s `live_worker_check`) before
    being treated as abandoned, so a second session picking minutes after the first's `next`
    invocation already exited can no longer re-pick a goal a long-running subagent still holds.
    Deliberately the PER-GOAL check, not the OR'd `_claimed_goal_has_live_worker` — see that
    function's own docstring for why a goal-agnostic "is anyone managing `.sdlc` at all" signal
    must never gate a specific goal's reclaim. A goal genuinely
    uncommitted-to-either-way that a SIBLING process on this machine wins the exact-instant race
    for is ALSO skipped (see `_try_acquire_claim_lock`, F10.5-2/#387) — #374 alone can only
    arbitrate a claim that already exists; this closes the narrower, true-simultaneous case #374
    structurally cannot. `extra_skip` (F4) is run_loop's own poison set — a goal neither the primary
    nor the fallback park-record could be recorded for, this run — so a doubly-failing source doesn't
    spin forever.

    #1199: ALSO skips every goal any OTHER live session has registered as in-flight (see
    `_session_in_flight_goals`) — the gap `test_next_without_skip_would_redispatch_a_goal_still_
    marked_in_progress` (tests/test_loop.py) proves is otherwise real: a source-level `in_progress`
    status alone does not stop a later `_next()` from re-serving the SAME goal, ledger or no ledger.
    Composed automatically, with NO `--skip` from the caller, GIVEN a correct `session_pid` —
    `session_pid` identifies THIS call's own session. #1239 review (finding 1): the default here
    (`os.getppid()`, resolved below when `session_pid` is `None`) is THIS `loop.py` process's OWN
    immediate parent — for a genuinely continuous caller (one live Python process across many
    `_next()` calls, e.g. `run_loop`, or a plain interactive shell that never forks an intermediate
    subprocess per command) that IS the caller's own stable identity. It is NOT, in general, stable
    across SEPARATE CLI invocations issued one-per-tool-call by a tool-calling agent: confirmed
    directly (two independent `python3 -c "import os; print(os.getppid())"` runs, each dispatched
    the way a single Bash-tool call actually wraps its payload — a shell that must genuinely fork
    because it also runs something else afterward, not tail-exec away — return DIFFERENT values,
    because each such invocation's immediate parent is a fresh, per-call shell wrapper, not any
    long-lived ancestor above it). The shipped `/sigma-loop` skill is exactly that shape, so it does
    NOT rely on this fallback: it captures its OWN invoking shell's `$PPID` (one level further up
    than any single `os.getppid()` call from inside a spawned process can see — the shell's own
    parent, which testably IS the same stable process across separate tool-style calls) and passes
    it explicitly via `--session-pid` on every `start`/`next`/`next-batch` call (see SKILL.md). This
    function's own `os.getppid()` default remains only as a last-resort value for a caller that
    supplies nothing at all — better than no identity, not a substitute for an explicitly threaded
    one. A goal this call goes on to claim below is registered as in-flight under `session_pid` —
    auto-registering it if `start --session-pid`/the CLI `start` verb never explicitly did — so the
    NEXT live session's own `_next()` call (a different `session_pid`) skips it without being told,
    PROVIDED both calls were given the same, genuinely stable, `session_pid`.

    #1129: also runs the opt-in auto-unpark sweep FIRST, before this call's own pick — the real
    production chokepoint every driver (the CLI `next` verb, `next_batch`, `run_loop`) already
    passes through, so wiring it once here covers all three, the same reasoning
    `_emit_run_stop_once`'s own docstring gives for sitting at this exact spot.

    #1129-followup cooldown: the sweep CAN flip a goal `sdlc:parked` -> `sdlc:goal` and post its
    audit comment in this same call — but the goal(s) it just flipped (`just_unparked`, from
    `_auto_unpark_sweep`'s return) are folded into this call's own `skip` set, so
    `source.next_pending()` right below can never ALSO return one of them in the SAME call. That
    gives a human one full `_next()` call's worth of a real window to read the audit comment and
    re-park it, if it was a deliberate checkpoint, before autonomous work can start on it — the
    sweep itself still can't tell a deliberate park from a stale one (see `auto_unpark.py`'s
    docstring), so this cooldown is the mitigation, not detection. `just_unparked` is a plain local
    — nothing persists it — so the goal is fully eligible again starting the very next `_next()`
    call, whether that's the next `run_loop` iteration, the next `next_batch` slot, or a wholly
    separate future invocation.

    #1198: also runs the opt-in (existing `ledger.enabled` switch, no new config) auto-reclaim
    sweep, `_auto_reclaim_stale_claims` — SAME cooldown, folded into this call's own `skip` set as
    `just_reclaimed` — releasing any GitHub claim aged past the ledger lease TTL so a crashed run's
    goal does not stay permanently unpickable now that `_fetch_pending` excludes `in_progress_label`
    unconditionally. Run BEFORE `_lease()` reads the ledger below, so a claim this same call just
    released is already reflected in that fresh read, not one call stale.

    #1468: the winning goal's declared unit label is attached between winning the claim LOCK and
    `mark_in_progress` — the last point at which nothing has yet acted on the goal, and every later
    step (base resolution, the registry, sibling propagation) reads that label. A goal whose label
    cannot be attached is REFUSED rather than parked: it keeps `sdlc:goal`, gains the
    `sdlc:needs-label` overlay and one comment on its own timeline, joins this call's `skip` set,
    and the pick moves to the next candidate — so an issue waiting on a human never stalls the queue
    behind it, is never reported as ready to pick, and needs no unpark once the human acts.
    `_feature_needs_label_sweep` (budget-gated, above) clears the overlay again. See
    `feature_labels.attach_at_pick` for the split (attach automatically, never create).

    #1499: and BEFORE any of that -- before the lease read, before the claim lock, before the label
    -- a candidate whose own body declares a prerequisite that is still OPEN is not claimed at all.
    It is not claimed and released, and not claimed and parked: it is left exactly as it was found,
    keeps `sdlc:goal`, joins this call's `skip` set, and the pick moves on, so it becomes pickable
    by itself the moment its blocker closes. This is the one ordering in this function that is
    load-bearing for a DAG-shaped backlog: with the check after the claim, every pick returned the
    same blocked goal and the loop could not reach the pickable issue behind it (measured live).
    See `_pick_dependency_hold` for what it reads, what it costs, and the three states it fails
    open on."""
    # #1445: the reconciliation sweep runs here TOO, not only in `next_batch`'s prologue. It was
    # placed there alone to avoid paying it once per goal in a batch -- but that amplification is
    # already prevented by its own TTL watermark (gate 2), which makes a not-due call one file read.
    # The cost of leaving it out was total: an operator who uses `next` rather than `next-batch`
    # NEVER swept, so a repo could sit ~23h past a 60-minute TTL while `next` ran repeatedly.
    session_pid = os.getppid() if session_pid is None else session_pid
    # A standalone picker pass is a loop tick, including an empty backlog. `next_batch` already
    # refreshed immediately before its one prologue sweep, so its slot calls opt out explicitly.
    if refresh_heartbeat:
        write_session_heartbeat(sdlc_dir, session_pid)
    _reconcile_sweep(sdlc_dir, config)
    just_unparked = _auto_unpark_sweep(sdlc_dir, config)
    just_reclaimed = _auto_reclaim_stale_claims(sdlc_dir, source, config)
    # #1468 / #2521: only when this call could actually do work. `_budget_reason`/`_handoff_reason`
    # are both local cursor reads, and skipping the sweep on a halted run is what keeps "a run that
    # reports BUDGET or HANDOFF mutated nothing" true of THIS commit's own code, not only of the
    # attach step below. HANDOFF is an equally valid "this call does no work" stop as BUDGET — a
    # `{"budget": {"max_iterations": 50}, "handoff": {"after_goals": 10}}` tuning (the template's
    # own `_handoff` comment explicitly invites tuning `after_goals` down) reaches goal 10 with
    # `_budget_reason` still None, so without this the sweeps would run on a call that goes on to
    # return `("HANDOFF", ...)` with no goal claimed. Count ceilings now read this
    # session's durable admissions, while resource ceilings still read the budget cursor.
    cursor_for_sweep_gate = state.load_cursor(sdlc_dir)
    if not (_budget_resource_reason(cursor_for_sweep_gate, config.get("budget", {}))
            or _session_count_stop(sdlc_dir, session_pid, config, cursor_for_sweep_gate)):
        _feature_needs_label_sweep(sdlc_dir, source, config)
        _feature_needs_unit_sweep(sdlc_dir, source, config)   # #2263
        # #232: close goals `record review` left waiting whose PR has since merged. Inside this
        # gate because it WRITES to GitHub (closes issues) -- same #1468 rule as the two above.
        _reconcile_awaiting_merges(sdlc_dir, config, source=source)
    me, my_writer, lease = _lease(sdlc_dir, config)
    skip = (set(extra_skip) | just_unparked | just_reclaimed
            | _session_in_flight_goals(sdlc_dir, config)
            | _awaiting_merge_skip(sdlc_dir))           # #232: a PR awaiting merge is not pickable
    # #1499: THE BOARD-MIRROR CACHE FOR THIS WHOLE CALL, shared by every candidate -- a dict
    # lookup per candidate and nothing more. NOT one read: #1650 re-populates it from a forced
    # refetch when a hold is about to be reported, and `_pick_dependency_hold` does the same when
    # the goal itself is absent, so this is read once plus AT MOST ONE refresh per call.
    gate = {}
    held = []       # ...and the goals it declined to claim, for the summary on the DONE path
    while True:
        goal = source.next_pending(skip=skip)
        if goal is None:
            # #1499: `("DONE", None)` on a backlog where everything left is waiting on an open
            # prerequisite is technically true and practically misleading -- it is what made the
            # live trial read as a silent stall. The terminal tuple is unchanged (a caller's
            # contract, and the goals genuinely are not pickable right now); the operator is told.
            if held:
                print("loop.py: nothing pickable — %d goal(s) are waiting on an open prerequisite "
                      "(%s). They become pickable by themselves when it closes; nothing was written "
                      "to them" % (len(held), ", ".join("#%s → %s" % h for h in held)),
                      file=sys.stderr)
            # #1084: read ONCE, before anything below can touch `_last_read_degraded`
            # (`_feature_drained_note` -> `pending_outside_feature` saves/restores it around its
            # own diagnostic probe, so reading first keeps this call's own verdict authoritative
            # rather than depending on that save/restore happening to run first).
            degraded = getattr(source, "read_degraded", lambda: False)()
            if not degraded and not held:
                _note_unlabelled_backlog(source)          # #230: stderr; stdout stays bare DONE
            # #1661: and WHICH kind of empty, when this run was confined to one unit. Before
            # `_emit_run_stop_once` only because `pending_outside_feature` puts the degraded-read
            # flag back where it found it; see its docstring.
            _feature_drained_note(source, skip)
            # #905: the real production chokepoint for the budget-exhaustion run_stop -- both the CLI `next`
            # verb and `run_loop` pass through here, so wiring it once covers both drivers.
            _emit_run_stop_once(sdlc_dir, config, source, "backlog-empty")
            return ("DONE", "degraded read — backlog state unknown" if degraded else None)
        # #1499: FIRST, ahead of the lease read and the claim lock below, because the whole defect
        # was ordering -- a goal with an unsatisfied prerequisite used to be claimed and only then
        # discovered to be unworkable. Nothing above this line has touched the goal, so a hold
        # leaves it exactly as it was found. See `_pick_dependency_hold` for the cost and the
        # fail-open cases.
        hold = _pick_dependency_hold(sdlc_dir, source, goal, config, gate)
        if hold:
            print("loop.py: not claiming #%s — open prerequisite %s. Left untouched (still "
                  "sdlc:goal, no claim); taking the next candidate" % (goal, hold), file=sys.stderr)
            held.append((goal, hold))
            skip.add(goal)
            continue
        holder_actor, holder_writer = lease.get(str(goal), (None, None))
        # #1197 (corrected post-#1237-review): `live_worker_check` is lazy — `claim_belongs_to_me`
        # only ever calls it once the claim's WRITER pid (always the short-lived picker that wrote
        # it) is already confirmed dead, so this closure's own `_goal_has_registered_worker` read
        # never runs for the overwhelming common case (my own claim, another actor's claim, a live
        # sibling picker). Deliberately `_goal_has_registered_worker`, NOT the OR'd, goal-agnostic
        # `_claimed_goal_has_live_worker` — a reclaim decision for THIS goal must be corroborated
        # by a marker that is actually about this goal, not by "is anyone managing `.sdlc` at all"
        # (see `_claimed_goal_has_live_worker`'s own docstring for the bug that combination caused).
        if holder_actor and not ledger.claim_belongs_to_me(
                holder_actor, holder_writer, me, my_writer,
                live_worker_check=lambda g=goal: _goal_has_registered_worker(sdlc_dir, g, config)):
            skip.add(goal)                      # another loop — or a live sibling of mine — owns this
            continue
        lock_fd = _try_acquire_claim_lock(sdlc_dir, goal)
        if lock_fd is None:
            skip.add(goal)                      # a sibling on this machine won this exact instant
            continue
        # #1468 (review F4) / #2521 / #2515: nothing may mutate before a gate whose whole job is to
        # stop the run, so all three checks below sit here, before the unit-label step, with the
        # lock released explicitly on each path (the try/finally that normally does it starts below).
        cursor = state.load_cursor(sdlc_dir)
        # #2515 + #2521: a genuine max_minutes/max_tokens budget stop — real wall-clock or real
        # spend already exhausted — wins over HANDOFF, checked FIRST. Both counters are cleared by
        # `state.start_run()`, which a HANDOFF-triggered relaunch calls in its own fresh session
        # (SKILL.md's printed resume command is exactly `/sigma-loop`); reporting HANDOFF here would
        # silently let an unattended, supervised drain keep spending past the operator's own
        # configured hard cap, forever, one hand-off at a time — the "REFUSES loudly" SAFETY
        # property (AGENTS.md) this repo is judged against. `_budget_resource_reason` checks
        # minutes/tokens INDEPENDENTLY of max_iterations (see its own docstring for why this must
        # NOT be "whatever _budget_reason reports once iterations is ruled out": on the SHIPPED
        # DEFAULT config, max_iterations and handoff.after_goals are the same number watching the
        # same counter, so an iterations trip coinciding with a real max_tokens/max_minutes breach
        # is the COMMON case, not a rare one — eliminating on "iterations already explains it" would
        # silently swallow exactly the breach this gate exists to surface).
        reason = _budget_resource_reason(cursor, config.get("budget", {}))
        if reason:
            _release_claim_lock(lock_fd)
            _emit_run_stop_once(sdlc_dir, config, source, "budget", why=reason)   # #905
            return ("BUDGET", reason)
        # #2521: HANDOFF wins a tie with max_iterations; both inspect this session's
        # settled plus active admissions. Another session's start cannot reset the count.
        count_stop = _session_count_stop(sdlc_dir, session_pid, config, cursor)
        if count_stop:
            _release_claim_lock(lock_fd)
            kind, reason = count_stop
            _emit_run_stop_once(sdlc_dir, config, source, kind.lower(), why=reason)
            return count_stop
        # #1468: the unit label THIS goal's body declares is attached HERE — inside the lock (so only
        # the winner writes) and before `mark_in_progress` below, which is the ordering the whole
        # branching level turns on. Every downstream step — base resolution (#1467), the registry
        # (#1469), sibling propagation — reads the LABEL, so a goal claimed before the attach would
        # resolve against the wrong base and record itself under nothing. A refusal (the declared
        # label does not exist, or the issue contradicts itself) is NOT a park: the goal keeps
        # `sdlc:goal`, gains the `sdlc:needs-label` OVERLAY so nothing reports it as ready to pick
        # and no later slot re-reads it, and is flagged once on its own timeline.
        # `_feature_needs_label_sweep` above clears that overlay the moment the label exists, so the
        # human's one gesture is enough. A LABEL OF OUR OWN, never `sdlc:blocked`: that one is
        # cleared by `auto_unpark` whenever the goal's body names an already-closed dependency,
        # whoever set it, which made the two sweeps fight forever over any goal carrying both.
        # Release the lock on the refusal path: this is the one branch that leaves the loop
        # without reaching the `try/finally` below, and a refused goal that leaked its claim lock
        # would be unpickable by anyone until something cleared it.
        # #1477: the scope check runs on the unit `attach_at_pick` just RESOLVED, so it reads no
        # issue of its own. A goal declaring a unit from a repo that unit does not list is refused
        # here rather than parked: it becomes `sdlc:needs-confirmation` (inert until its owner
        # promotes it -- a scope expansion needs a DECISION, unlike a missing label, which
        # self-heals), joins this call's `skip` set, and the pick moves on. Same lock discipline as
        # the line above it: the refusal path leaves the loop without reaching the `try/finally`
        # below, so the claim lock is released explicitly or the goal is unpickable by anyone.
        # #1479: and then WHO FILED IT. Same lock, same moment, same refusal shape as the scope
        # check above -- `sdlc:needs-confirmation`, inert until its owner promotes it -- but a
        # different question: that one asks whether the unit may touch this repo, this one asks
        # whether the person who opened the issue was entitled to put work on this board at all.
        # Short-circuited behind it deliberately: a goal already held for scope must not also be
        # held for ownership, or one pick would collect two comments and two ledger entries.
        # #1567: and the unit BOTH of them are given comes from `_unit_at_pick`, not straight off
        # the decision. `attach_at_pick` reads the declaration over GraphQL; `work._declared_unit`
        # reads the SAME declaration over REST three lines below and bases the goal on
        # `feature/<unit>`. One un-retried `gh issue view` made the first answer None and left the
        # second untouched, so the goal was cut from the unit's branch while the gate governing work
        # against that unit had been told there is no unit. Only on a FAILED read, and only on an
        # adopted project; see that function for what it costs and what it deliberately does not do.
        # #1661: `--feature`'s exclusivity is enforced HERE, on the resolved unit, and FIRST among
        # the three gates below. First because the other two hold a goal FOR A HUMAN -- an overlay,
        # a comment, a ledger line -- and a goal that is merely outside this run's scope has nothing
        # wrong with it: it must be passed over in silence, not flagged. On the resolved unit
        # because the scoped query filters on the LABEL, and on a self-contradicting issue the label
        # and the declaration disagree. See `_feature_scope_ok`.
        # Reserve this goal against BOTH count ceilings before any label or issue mutation.
        # A batch can fill its last two slots, record one, and refill while the other is
        # still running: run_iteration alone then undercounts what this run admitted.
        try:
            admission_stop = _reserve_goal_slot(sdlc_dir, session_pid, goal, config)
        except BaseException:
            _release_claim_lock(lock_fd)
            raise
        if admission_stop:
            _release_claim_lock(lock_fd)
            kind, why = admission_stop
            _emit_run_stop_once(sdlc_dir, config, source, kind.lower(), why=why)
            return admission_stop
        try:
            decision = feature_labels.attach_at_pick(sdlc_dir, source, goal, config)
            unit = _unit_at_pick(sdlc_dir, goal, config, decision)
            proceed = (decision.proceed
                       and _feature_scope_ok(source, goal, unit)
                       and _scope_ok_at_pick(sdlc_dir, source, goal, config, unit)
                       and _owner_ok_at_pick(sdlc_dir, source, goal, config, unit))
        except BaseException:
            _cancel_goal_slot(sdlc_dir, session_pid, goal)
            _release_claim_lock(lock_fd)
            raise
        if proceed:
            break
        _cancel_goal_slot(sdlc_dir, session_pid, goal)
        _release_claim_lock(lock_fd)
        skip.add(goal)
    # The lock's whole job is bridging the gap until a DURABLE claim exists — once mark_in_progress/
    # safe_append below have run, #374's own claim-interpretation takes over correctly for every
    # later _next() call, local or not. Release unconditionally, not just on success, so an
    # unexpected exception can never leak the lock.
    try:
        # #1962: the claim itself now lives in `_claim()`, so `next`, the `claim` verb and
        # `_ensure_claimed` share ONE implementation. Behaviour here is unchanged --
        # `test_next_still_emits_claimed_through_the_extracted_helper` is the characterisation lock.
        _claim(sdlc_dir, source, goal, config, session_pid=session_pid)
        # #1472: the claim is durable and nothing has been BUILT yet — the one moment where learning
        # that a cross-repo unit cannot land in one of its repos still costs nothing. See
        # `_check_cross_repo_at_pick` for why it is advisory rather than a gate.
        _check_cross_repo_at_pick(sdlc_dir, goal, config)
        # #1627: same moment, same reasoning — resolve + record this goal's model tier from code,
        # not from an agent remembering a SKILL.md instruction. See
        # `_predict_model_choice_at_pick` for why it is advisory rather than a gate.
        _predict_model_choice_at_pick(sdlc_dir, source, goal, config)
        return ("goal", goal)
    except BaseException:
        _session_release(sdlc_dir, goal)
        raise
    finally:
        _release_claim_lock(lock_fd)


DEFAULT_GOALS_MAX_CONCURRENT = 3   # matches slices.py's own DEFAULT_MAX_CONCURRENT — same default,
                                    # independent config block (F10.5-3/#375, see goals_parallel)


def goals_parallel(config):
    """-> (enabled, max_concurrent) for GOAL-level parallelism (F10.5-3/#375) — config
    `parallel.goals.{enabled,max_concurrent}`, a SIBLING of the existing `parallel.{enabled,
    max_concurrent}` block `slices.py` already reads for INTRA-goal slice dispatch (unchanged,
    untouched by this). `is True` on purpose, matching that same convention: a truthy string or a
    stray 1 must not silently start several concurrent worktree subagents against one repo. A
    non-numeric cap falls back to the default rather than raising — a typo in config must not turn
    an intended-sequential run into a crash."""
    block = (config.get("parallel") or {}).get("goals") or {}
    try:
        cap = int(block.get("max_concurrent", DEFAULT_GOALS_MAX_CONCURRENT))
    except (TypeError, ValueError):
        cap = DEFAULT_GOALS_MAX_CONCURRENT
    return (block.get("enabled") is True, max(1, cap))


def next_batch(sdlc_dir, source, config, max_concurrent=None, extra_skip=(), session_pid=None):
    """Up to `max_concurrent` goals to dispatch AS CONCURRENT WORKTREE SUBAGENTS this pass
    (F10.5-3/#375) — repeatedly calls `_next()`, accumulating each pick on top of `extra_skip` so
    the SAME session's own multiple slots can never collide with each other, without even
    touching the ledger a second time per pick (the exact `extra_skip` hook #335/F4 already added;
    #387's local lock backs genuine CROSS-session races further, orthogonal to this).

    #1199: `session_pid` (default `os.getppid()`, resolved once per `_next()` call — see that
    function's own docstring) now closes the OTHER half of the gap the next two paragraphs
    describe, automatically: a slot freed and refilled AFTER this call already returned is covered
    because the earlier pick is still sitting in THIS session's own in-flight registry, which
    `_next()` reads on every call, from any process, not only within one `next_batch` call. Passing
    `--skip`/`extra_skip` explicitly still works exactly as before (tests rely on it, and it is the
    only lever for skipping a goal that was never actually claimed by any session), but a caller no
    longer NEEDS it just to avoid re-dispatching its own prior picks.

    `extra_skip` matters beyond this one call: #374's writer-liveness check can only ever answer
    "is the process that WROTE this claim still literally running" — and the process that writes a
    claim is a single short-lived `loop.py` invocation that has already exited by the time this
    function returns, regardless of whether the GOAL itself is still being actively worked by a
    long-running subagent. So a caller refilling a freed slot some time AFTER this call returned —
    not within it — must pass the OTHER, still-active goals from prior calls back in as
    `extra_skip` itself; nothing pid-based stands in for that once the picking call that wrote the
    claim is gone. `next()`'s CLI verb takes the same thing as `--skip a,b,c` for exactly this.

    #1197 narrows this, it does not remove it: `_next()` now also corroborates a dead writer pid
    against `_goal_has_registered_worker` before reclaiming, which is exactly what protects a
    LATER picker (a wholly separate `next`/`next-batch` invocation, possibly minutes on) once the
    dispatched subagent has reached SKILL.md step 3a and actually called `agent-start`. It cannot
    help THIS same-pass refill: the slot this call just filled has not been dispatched yet, so no
    marker for it exists at the moment a sibling slot's refill runs — `extra_skip` remains the only
    thing that closes that specific, immediate window, unchanged.

    `max_concurrent` defaults to `goals_parallel(config)`'s own setting when omitted — OFF (the
    default; a repo that hasn't opted in) degrades to a single-item batch, byte-identical to
    calling `_next()` once — so a caller can always call this the SAME way regardless of whether
    goal-level parallelism is configured on, rather than needing two separate code paths.

    Unlike `slices.py`'s `schedule()`/`conflicts()`, goals need NO file-level conflict detection:
    each goal gets its OWN worktree+branch+PR (`work.py start()`), so two goals touching
    overlapping files is, at worst, a routine PR-rebase later (the established CHANGELOG-cascade
    pattern this whole plugin's own history already handles routinely) — not the
    silently-lost-edit risk `conflicts()` exists to prevent for slices sharing ONE worktree.

    Returns a list of `(kind, goal)` tuples: zero or more `("goal", <goal>)` entries, followed by
    exactly one terminal `("DONE", <reason-or-None>)`, `("BUDGET", <reason>)`, or `("HANDOFF",
    <reason>)` (#2521) IFF the backlog/budget/hand-off ceiling ran out before filling every slot —
    DONE's second element is `None` on a genuine empty read and, #1084, a short degraded-read
    reason string when `source.read_degraded()` is True (mirrors BUDGET's own shape); BUDGET's
    `<reason>` names which ceiling tripped and the observed-vs-configured numbers (#411, see
    `_budget_reason`), never `None` for a genuine budget stop. A full batch of `max_concurrent`
    `"goal"` entries with no terminal entry means every slot filled, with the backlog possibly not
    yet exhausted.

    Each `_next()` atomically reserves its own session's next admission before returning a goal.
    The same check governs a later `next` refill, so both paths honor goal-count ceilings even
    while sibling slots remain active or another session resets the shared budget cursor."""
    # #1391 step 5e: the reconciliation sweep runs ONCE per batch, in this PROLOGUE -- deliberately
    # NOT inside `_next()` where `_auto_unpark_sweep` sits. `next_batch` calls `_next()` up to
    # `max_concurrent` times, so anything placed there is paid 8x per batch on this repo's own
    # config; that amplification is an existing cost this must not copy.
    session_pid = os.getppid() if session_pid is None else session_pid
    # This prologue can be the only tick when reconciliation blocks or drains the batch.
    write_session_heartbeat(sdlc_dir, session_pid)
    _reconcile_sweep(sdlc_dir, config)
    if max_concurrent is None:
        enabled, cap = goals_parallel(config)
        max_concurrent = cap if enabled else 1
    # Keep the old batch shape (only the remaining number of goal lines, with the
    # terminal reported on the next call) using the SAME session-local count that
    # `_next` reserves atomically. The reservation remains the final race guard.
    ceilings = [n for n in (_handoff_ceiling(config.get("handoff") or {}),
                             (config.get("budget") or {}).get("max_iterations")) if n]
    if ceilings:
        cursor = state.load_cursor(sdlc_dir)
        snapshot = _session_admission_snapshot(sdlc_dir, session_pid, config, cursor)
        if isinstance(snapshot, tuple):
            max_concurrent = 1
        else:
            admitted = snapshot["settled_admissions"] + len(set(snapshot["in_flight"]))
            try:
                max_concurrent = min(int(max_concurrent or 1), max(0, min(ceilings) - admitted))
            except (TypeError, ValueError):
                pass  # malformed optional config still leaves the atomic `_next` guard in charge
    picks = []
    skip = set(extra_skip)
    for _ in range(max(1, max_concurrent)):
        kind, goal = _next(sdlc_dir, source, config, extra_skip=skip, session_pid=session_pid,
                           refresh_heartbeat=False)
        if kind != "goal":
            picks.append((kind, goal))
            break
        picks.append(("goal", goal))
        skip.add(goal)
    return picks


def _session_dir(sdlc_dir):
    return pathlib.Path(sdlc_dir) / "state" / "sessions"


def _session_heartbeat_dir(sdlc_dir):
    """The loop's liveness evidence, separate from admission state and safe to leave on a crash."""
    return pathlib.Path(sdlc_dir) / "state" / "heartbeat"


_CODEX_THREAD_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    re.IGNORECASE,
)


def _session_codex_thread():
    """Codex's per-task identity; Claude keeps its existing PID-only registry filenames."""
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_SESSION_ID"):
        return None
    thread = os.environ.get("CODEX_THREAD_ID")
    if not thread:
        if os.environ.get("CODEX_SESSION_ID"):
            raise ValueError("missing CODEX_THREAD_ID for Codex session registry")
        return None
    if not _CODEX_THREAD_ID.fullmatch(thread):
        raise ValueError("invalid CODEX_THREAD_ID for session registry")
    return thread.lower()


def _session_marker_path(sdlc_dir, session_pid):
    """One entry per session. Claude/legacy names remain `<pid>.active`; Codex desktop tasks can
    share one live shell parent PID, so their names include their own validated thread UUID as
    `<pid>-<thread>.active`. Liveness still comes from the numeric PID prefix and the entry's TTL."""
    pid = int(session_pid)
    thread = _session_codex_thread()
    return _session_dir(sdlc_dir) / (f"{pid}-{thread}.active" if thread else f"{pid}.active")


def session_heartbeat_path(sdlc_dir, session_pid):
    """One JSON heartbeat per managing-session identity, matching its registry filename exactly."""
    return _session_heartbeat_dir(sdlc_dir) / (_session_marker_path(sdlc_dir, session_pid).stem + ".json")


def write_session_heartbeat(sdlc_dir, session_pid, now=None, generation=None):
    """Atomically refresh a loop heartbeat. A write failure is diagnostic-only, never a run stop."""
    try:
        pid = int(session_pid)
        path = session_heartbeat_path(sdlc_dir, pid)
        path.parent.mkdir(parents=True, exist_ok=True)
        seen = time.time() if now is None else float(now)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + f".{os.getpid()}.")
        try:
            with os.fdopen(fd, "w") as stream:
                data = {"pid": pid, "last_seen": seen}
                if generation:
                    data["generation"] = generation
                json.dump(data, stream)
                stream.flush(); os.fsync(stream.fileno())
            os.replace(tmp, path)
        finally:
            pathlib.Path(tmp).unlink(missing_ok=True)
        return True
    except (OSError, TypeError, ValueError):
        return False


def refresh_registered_session_heartbeat(sdlc_dir, session_pid):
    """Refresh a heartbeat only while its session marker is still registered.

    Phase-boundary reporters run in short-lived processes.  They may arrive after a clean
    ``session-end`` has removed the managing session, so their liveness update must not recreate
    a heartbeat with no corresponding registry marker.  Read the owner generation and publish
    under the same stripe lock as ``session_end``; an end either happens before this no-op or
    after it and removes both files.
    """
    try:
        pid = int(session_pid)
        path = _session_marker_path(sdlc_dir, pid)

        def _refresh_if_registered():
            if not path.is_file():
                return False
            data = _session_read(path, strict=True)
            generation = data.get("generation")
            return write_session_heartbeat(
                sdlc_dir, pid,
                generation=generation if isinstance(generation, str) and generation else None,
            )

        return _session_locked(sdlc_dir, pid, _refresh_if_registered, path=path,
                               require_lock=True)
    except (OSError, TypeError, ValueError, RuntimeError):
        return False


def session_heartbeat_liveness(sdlc_dir, session_pid, config=None, now=None):
    """`idle` means fresh even when no goal moved; `dead` is stale or absent evidence with a marker.

    The bound deliberately shares the watcher's configuration-free rule, `max(3 * interval, 180)`.
    This probe never signals or otherwise alters the recorded process.
    """
    cfg = config if isinstance(config, dict) else {}
    ledger_cfg = cfg.get("ledger") if isinstance(cfg.get("ledger"), dict) else {}
    watch = ledger_cfg.get("watch") if isinstance(ledger_cfg.get("watch"), dict) else {}
    interval = watch.get("interval_seconds", 900)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval <= 0:
        interval = 900
    try:
        data = json.loads(session_heartbeat_path(sdlc_dir, session_pid).read_text())
        seen = data.get("last_seen")
        if isinstance(seen, bool) or not isinstance(seen, (int, float)):
            raise ValueError("invalid heartbeat")
        age = max(0.0, (time.time() if now is None else now) - seen)
    except (OSError, ValueError, TypeError):
        return ("dead", None)
    return ("idle" if age < _load("sync").stale_after_seconds(interval) else "dead", age)


def _session_lock_path(sdlc_dir, entry_path):
    """A stable, bounded 256-stripe on-disk lock namespace.

    Stripe identity must not depend on a process's current CPU count: two concurrent
    processes could then lock different files for the same admission marker. The byte
    width is a file-format constant, not a host resource budget. Locks are created
    lazily, remain after clean exit, and are capped at 256 per repository.
    """
    slot = hashlib.sha256(entry_path.name.encode()).digest()[0]
    return _session_dir(sdlc_dir) / "locks" / f"{slot}.lock"


def _session_locked(sdlc_dir, session_pid, fn, path=None, require_lock=False):
    """Run `fn()` — a read-modify-write against ONE session's own registry entry — inside an
    exclusive, blocking `flock` on a stable lock stripe for that entry, closing a lost-update race #1239 review
    (finding 3) reproduced deterministically: two genuinely concurrent writers to the SAME
    `<pid>.active` file (e.g. two `next-batch` slots each claiming a different goal under the same
    session, or a parallel `next` refill racing a sibling slot's `record`/`release`) each read the
    same pre-write `in_flight` list and the second write silently clobbers the first's addition —
    10/10 reproductions with a real `multiprocessing.Process` pair lost one goal's registration.

    Mirrors `_try_acquire_claim_lock`'s established `fcntl.flock` pattern in this same file for the
    identical class of problem — except BLOCKING (`LOCK_EX`, no `LOCK_NB`): a session's own registry
    write is a fast, in-process critical section (read one small JSON file, mutate a list, write it
    back), not a multi-goal claim race where a busy competitor should be skipped rather than waited
    on — losing a moment blocked behind a sibling's write is the right trade here, not a bug.

    For a cross-session release or prune, `path` is the enumerated entry, so this call locks THAT
    entry even when the caller's Codex thread differs from its owner. Without `path`, it locks the
    caller's own PID/thread entry, preserving Claude's PID-only behavior.

    Admission and registry updates pass `require_lock=True`: if locking is unavailable, refuse
    rather than risk concurrent writers overrunning the goal limit. Legacy optional callers may
    retain the prior best-effort fallback. The stripe set is bounded as sessions come and go."""
    entry_path = path if path is not None else _session_marker_path(sdlc_dir, session_pid)
    lock_path = _session_lock_path(sdlc_dir, entry_path)
    if fcntl is None:
        if require_lock:
            raise RuntimeError("session file locking is unavailable on this host")
        return fn()
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    except OSError:
        if require_lock:
            raise RuntimeError("cannot open session file lock for goal-count admission")
        return fn()
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fn()
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _session_read(path, strict=False):
    """Read one session registry entry. Strict admission reads refuse missing/corrupt counts;
    best-effort inventory reads treat unreadable entries as empty."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        if strict:
            raise RuntimeError(f"session admission marker unreadable: {path}") from exc
        return {"in_flight": [], "settled_admissions": None, "valid": False}
    if not isinstance(data, dict) or not isinstance(data.get("in_flight", []), list):
        if strict:
            raise RuntimeError(f"session admission marker malformed: {path}")
        return {"in_flight": [], "settled_admissions": None, "valid": False}
    if strict and ("in_flight" not in data or
                   any(not isinstance(g, (str, int)) or isinstance(g, bool)
                       for g in data["in_flight"]) or
                   len({str(g) for g in data["in_flight"]}) != len(data["in_flight"])):
        raise RuntimeError(f"session admission goals malformed: {path}")
    settled = data.get("settled_admissions")
    if strict and (isinstance(settled, bool) or not isinstance(settled, int) or settled < 0):
        raise RuntimeError(f"session admission count missing or invalid: {path}")
    return {"in_flight": [str(g) for g in data.get("in_flight", []) if isinstance(g, (str, int))],
            "settled_admissions": settled if isinstance(settled, int) and not isinstance(settled, bool) and settled >= 0 else None,
            "valid": True}


def _session_write(path, data):
    """Publish a load-bearing session marker atomically, inside its caller's file lock."""
    # Admission/claim/release writers predate heartbeat ownership and intentionally pass only
    # their fields. Preserve an existing generation here so any of those routine updates cannot
    # silently turn a later owner-checked `session_end` into a no-op.
    if "generation" not in data:
        try:
            existing_generation = json.loads(path.read_text()).get("generation")
            if isinstance(existing_generation, str) and existing_generation:
                data = {**data, "generation": existing_generation}
        except (OSError, ValueError, AttributeError):
            pass
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + f".{os.getpid()}.")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        pathlib.Path(tmp).unlink(missing_ok=True)


def _session_entries(sdlc_dir):
    """Yield `(pid, path, in_flight)` for every session registry file on disk, live or stale alike
    — callers filter liveness themselves (`_session_pid_live`) since "is it live" and "what does it
    say" are independent questions here. Parse the numeric PID prefix from both legacy `<pid>` and
    Codex `<pid>-<thread UUID>` filenames. Stray names are skipped rather than raising — this
    directory is scanned on every `_next()` call, so it must degrade on unexpected entries."""
    d = _session_dir(sdlc_dir)
    if not d.is_dir():
        return
    for path in sorted(d.glob("*.active")):
        try:
            pid_text, separator, thread = path.stem.partition("-")
            pid = int(pid_text)
            if separator and not _CODEX_THREAD_ID.fullmatch(thread):
                continue
        except ValueError:
            continue
        yield pid, path, _session_read(path)["in_flight"]


def _session_pid_live(pid, path, config):
    """The same two-signal liveness check `session_active`/`agent_alive` already apply to their own
    markers (F10.5/#374's own pattern, not reinvented here): `ledger.pid_alive()` first — a
    definitively dead pid needs no TTL to disqualify — then the entry file's own mtime against
    `ledger.lease_ttl_seconds(config)` as the pid-reuse backstop."""
    if not ledger.pid_alive(pid):
        return False
    ttl = ledger.lease_ttl_seconds(config)
    if ttl is None:
        return True
    try:
        return (time.time() - path.stat().st_mtime) < ttl
    except OSError:
        return True                          # can't even stat it — fail toward "still active"


def session_start(sdlc_dir, session_pid, generation=None):
    """Record that a managing session — a routine/cron firing, or a manual overnight run — is now
    driving `.sdlc` (F10.5-4/#377), so a routine firing again before this one finishes can tell NOT
    to launch a redundant one — the same class of risk F10.5/#374 already closes one level down (a
    routine blindly resuming ANOTHER session's in-flight worktree, not launching a whole second one).

    Writes THIS session's own registry entry (`_session_marker_path`) rather than the old shared
    file. Calling it again for the same identity preserves its `in_flight` goals. Claude's identity
    remains the PID; Codex desktop adds CODEX_THREAD_ID because distinct tasks share a shell parent.

    `session_pid` must be the CALLER's own long-lived process id — the routine/skill layer's, not
    any individual `loop.py` invocation's own. Confirmed empirically that this distinction is real,
    not theoretical: two separate shell-tool calls in the SAME host session get two DIFFERENT `$$`
    (a fresh subprocess each time — the same reason `next`'s `--skip` flag exists, see F10.5-3/#375),
    but the SAME `$PPID` (the long-lived parent the shell-tool calls share — confirmed directly by
    reading `$PPID` from the SHELL itself across separate calls, not by calling `os.getppid()` from
    inside a spawned `python3`; see the next paragraph for why those two are NOT the same value).
    Recording THIS call's own `os.getpid()` — a `python3 loop.py ...` process that exits within
    moments of returning — would make the marker read as immediately dead, useless for a session
    running minutes-to-hours across many separate `loop.py` calls. The routine/skill prompt captures
    `$PPID` (or an equivalent stable id) ONCE — really, once per call, since re-reading the SHELL's
    own `$PPID` keeps yielding that same stable value every time, no persistence needed — and passes
    it through every call that needs it, via `--session-pid`. On Codex desktop that PID proves
    liveness but does not uniquely identify a task; a validated CODEX_THREAD_ID supplies that half.

    #1239 review (finding 1/2): `_next()`/`next_batch()` ALSO default to `os.getppid()` internally
    when no explicit `session_pid` is given — but that is `os.getppid()` measured from INSIDE the
    `loop.py` PYTHON PROCESS itself, which is one level SHALLOWER than the shell's own `$PPID` this
    docstring just established as stable: it returns THIS process's immediate parent, the per-call
    shell wrapper a tool-calling agent's Bash-tool-style invocation forks fresh each time (confirmed:
    it differs across separate calls), not the long-lived grandparent above it. That Python-level
    default is a last-resort fallback for a caller that passes nothing at all (a genuinely continuous
    process like `run_loop`, or a plain manual shell where it happens to coincide) — it is NOT a
    substitute for the shell-level `$PPID` capture-and-pass-through this docstring describes, and the
    shipped `/sigma-loop` skill does NOT rely on it: SKILL.md passes `--session-pid "$PPID"`
    explicitly on `start`/`next`/`next-batch` (see `main`'s dispatch for all three).

    Invalid PIDs remain a no-op for legacy callers. A valid session whose marker cannot
    be locked, read or published refuses loudly because admission now depends on it."""
    try:
        pid = int(session_pid)
    except (TypeError, ValueError):
        return
    path = _session_marker_path(sdlc_dir, pid)
    path.parent.mkdir(parents=True, exist_ok=True)

    selected_generation = generation or uuid.uuid4().hex
    def _write():
        nonlocal selected_generation
        if path.exists():
            existing = _session_read(path, strict=True)
            try:
                raw_generation = json.loads(path.read_text()).get("generation")
                if generation is None and isinstance(raw_generation, str) and raw_generation:
                    selected_generation = raw_generation
            except (OSError, ValueError, AttributeError):
                pass
            data = {"in_flight": existing["in_flight"],
                    "settled_admissions": existing["settled_admissions"], "generation": selected_generation}
        else:
            data = {"in_flight": [], "settled_admissions": 0, "generation": selected_generation}
        _session_write(path, data)
        # Publish both halves before releasing the lifecycle lock.  Otherwise a clean end can
        # remove the new marker between these two writes and this delayed heartbeat becomes orphaned.
        write_session_heartbeat(sdlc_dir, pid, generation=selected_generation)

    _session_locked(sdlc_dir, pid, _write, path=path, require_lock=True)
    return selected_generation


def session_end(sdlc_dir, session_pid=None, generation=None):
    """Clear only the caller's own registry entry. Claude/legacy identity is the PID; Codex adds
    its thread ID, so ending one of two tasks under the same host PID leaves the other registered.
    A generation-bearing entry also requires its matching generation token; a bare cleanup is a
    safe no-op rather than risking deletion of a successor. Best-effort and a safe no-op if that
    entry was never created. Defaults to `os.getppid()` when no PID was passed, matching the CLI's
    existing fallback."""
    try:
        pid = os.getppid() if session_pid is None else int(session_pid)
        path = _session_marker_path(sdlc_dir, pid)
        heartbeat = session_heartbeat_path(sdlc_dir, pid)
        # Keep registry and heartbeat cleanup in the SAME critical section.  A successor start
        # cannot publish its marker, then have this old cleanup unlink its fresh heartbeat.
        def _end_if_owner():
            try:
                owner_generation = json.loads(path.read_text()).get("generation")
            except (OSError, ValueError, AttributeError):
                return False
            # A generation-bearing marker has an owner token.  A bare session-end cannot know
            # whether it belongs to that owner or to a predecessor, so it must leave it alone.
            # Old unversioned registry entries retain their best-effort cleanup behavior.
            if isinstance(owner_generation, str) and owner_generation:
                if generation != owner_generation:
                    return False
            path.unlink(missing_ok=True); heartbeat.unlink(missing_ok=True)
            return True
        _session_locked(sdlc_dir, pid, _end_if_owner, path=path, require_lock=True)
    except (OSError, TypeError, ValueError):
        pass


def session_active(sdlc_dir, config):
    """True iff ANY managing session is still active for `sdlc_dir` — what a routine checks BEFORE
    deciding whether to launch one. #1199: scans every entry in the registry (`_session_entries`)
    rather than reading one fixed path, so a second session registering — or a first session ending
    — can never hide a DIFFERENT, genuinely-live session from this check the way the old single-
    file marker did. Per-entry liveness is unchanged: the same two independent signals the ledger's
    own claim-lease machinery already combines (F10.5/#374) — `ledger.pid_alive()` primary,
    `ledger.lease_ttl_seconds(config)`-bounded mtime as the pid-reuse fallback (see
    `_session_pid_live`). No registry directory at all, or every entry stale/dead, reads as not
    active — never raises."""
    return any(_session_pid_live(pid, path, config) for pid, path, _ in _session_entries(sdlc_dir))


def _prune_dead_session_entries(sdlc_dir, config):
    """Opportunistic cleanup of dead sessions' own registry entries — #1239 review round 3, finding
    B: crashed sessions leave `state/sessions/*.active` behind. Lock files now use a bounded stripe
    set and persist safely; only the per-session markers need pruning.

    Triggered as a side effect of the registry already being read (`_session_in_flight_goals`, below
    — on every `_next()`/`next_batch()` call, the real production chokepoint every driver already
    passes through, the same "wire it once at the chokepoint" argument `_auto_unpark_sweep`'s own
    docstring makes) or re-announced (the CLI `start` verb, alongside `session_start` — see `main`'s
    dispatch) — no new background process, no schedule of its own.

    Uses the EXACT SAME liveness check (`_session_pid_live`: `ledger.pid_alive()` primary, lease-
    TTL-bounded mtime as the pid-reuse fallback) every OTHER registry reader in this file already
    applies to judge a session dead — so a genuinely-live session merely quiet past routine-firing
    frequency, but still inside the TTL, is never pruned out from under it; only an entry that
    ALREADY reads as dead to every other caller here is removed.

    Best-effort throughout: an unlink failure (permissions, already gone, a concurrent pruner) must
    never raise or block whatever real operation triggered this scan — the file is simply tried
    again on the next opportunistic pass, exactly like every other marker write/cleanup in this
    module."""
    # A process killed between mkstemp and os.replace leaves its private temp file.
    # Reap one whose writer is dead, or any older than a day (including pre-PID
    # temp names). The age fallback handles PID reuse and keeps repeated crashes
    # bounded without touching a normal in-progress atomic write.
    session_dir = _session_dir(sdlc_dir)
    # This deletes (and `_session_locked` below creates a lock directory), and `glob` follows a link:
    # a linked `state/sessions` or `state` would make it act on whatever that names, so it does
    # nothing at all, exactly as `liveness_prune._sweep_sessions` already does (B6 #466).
    if session_dir.is_symlink() or session_dir.parent.is_symlink():
        return
    now = time.time()
    for temp in session_dir.glob("*.active.*"):
        try:
            remainder = temp.name.split(".active.", 1)[1]
            writer_text = remainder.split(".", 1)[0]
            stale = now - temp.stat().st_mtime >= 24 * 3600
            writer_dead = writer_text.isdigit() and not ledger.pid_alive(int(writer_text))
            if stale or writer_dead:
                temp.unlink(missing_ok=True)
        except (OSError, IndexError, ValueError):
            pass
    for pid, path, _in_flight in _session_entries(sdlc_dir):
        if _session_pid_live(pid, path, config):
            continue
        def _prune(path=path, pid=pid):
            # Re-check under this entry's own lock: a session may have re-announced itself after
            # the outer scan. Lock the enumerated path, not this pruner's Codex thread identity.
            if _session_pid_live(pid, path, config):
                return False
            path.unlink(missing_ok=True)
            return True
        try:
            _session_locked(sdlc_dir, pid, _prune, path=path, require_lock=True)
        except (OSError, RuntimeError):
            pass


def _session_in_flight_goals(sdlc_dir, config):
    """#1199: the union of every goal currently registered in-flight by a LIVE session — what
    `_next()` folds into its own skip set automatically, closing the gap
    `test_next_without_skip_would_redispatch_a_goal_still_marked_in_progress` documents (a source-
    level `in_progress` status alone does not stop a re-pick). A stale/dead session's own claims are
    excluded exactly like `session_active` excludes that session from "active" — so a crashed
    session can never hold a goal hostage forever (AC3): the SAME liveness check, applied per entry,
    is what both functions share.

    #1239 review round 3, finding B: also opportunistically prunes every entry that already reads as
    dead by this same scan (`_prune_dead_session_entries`) — this is the highest-traffic read against
    the registry (every `_next()` call), so it is the natural place to fold that maintenance in,
    rather than leaving a crashed session's leftover files to sit on disk forever."""
    _prune_dead_session_entries(sdlc_dir, config)
    goals = set()
    for pid, path, in_flight in _session_entries(sdlc_dir):
        if _session_pid_live(pid, path, config):
            goals.update(in_flight)
    return goals


def _session_admission_snapshot(sdlc_dir, session_pid, config, cursor):
    """Return this session's authoritative count record, or a loud stop tuple.

    A real `start` gives the cursor a timestamp and creates the marker. If that marker
    later expires or is pruned, never silently recreate an empty allowance. The
    zero-timestamp fallback supports legacy direct library callers that did not run
    `start`; the shipped skill always runs it first.
    """
    pid = int(session_pid)
    path = _session_marker_path(sdlc_dir, pid)
    if not path.exists():
        if cursor["run_started_at"]:
            return ("HANDOFF", "session admission marker missing; start a fresh /sigma-loop session")
        return {"in_flight": [], "settled_admissions": cursor["run_iteration"]}
    if cursor["run_started_at"] and not _session_pid_live(pid, path, config):
        return ("HANDOFF", "session admission marker expired; start a fresh /sigma-loop session")
    try:
        return _session_read(path, strict=True)
    except RuntimeError as exc:
        return ("HANDOFF", str(exc) + "; start a fresh /sigma-loop session")


def _session_count_stop(sdlc_dir, session_pid, config, cursor=None):
    """The per-session goal-count stop; resource budgets are checked separately."""
    ceiling = _handoff_ceiling(config.get("handoff") or {})
    iterations = (config.get("budget") or {}).get("max_iterations")
    if not ceiling and not iterations:
        return None
    cursor = state.load_cursor(sdlc_dir) if cursor is None else cursor
    snapshot = _session_admission_snapshot(sdlc_dir, session_pid, config, cursor)
    if isinstance(snapshot, tuple):
        return snapshot
    admitted = snapshot["settled_admissions"] + len(set(snapshot["in_flight"]))
    if ceiling and admitted >= ceiling:
        return ("HANDOFF", f"{admitted} admitted goals >= handoff.after_goals {ceiling}")
    if iterations and admitted >= iterations:
        return ("BUDGET", f"{admitted} admitted iterations >= max_iterations {iterations}")
    return None


def _reserve_goal_slot(sdlc_dir, session_pid, goal, config):
    """Atomically admit one goal against this session's completed plus active count.

    `next-batch` limits its initial batch, but the documented `next` refill also needs
    to count sibling goals still in flight. The session registry already owns those
    claims; use its per-session lock for the count-and-reserve operation so two
    simultaneous refills from the same session cannot both take the last slot.
    Return a `(kind, reason)` stop tuple, or None after reserving `goal`.
    """
    pid = int(session_pid)
    path = _session_marker_path(sdlc_dir, pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    budget = config.get("budget") or {}
    handoff = config.get("handoff") or {}
    ceiling = _handoff_ceiling(handoff)
    iterations = budget.get("max_iterations")
    # The existing session-registry lock is POSIX-only. Without it, a concurrent
    # refill could exceed a configured ceiling; refuse that guarantee explicitly.
    if fcntl is None and (ceiling or iterations):
        raise RuntimeError("goal-count admission requires a session file lock on this host")

    def _check_and_write():
        cursor = state.load_cursor(sdlc_dir)
        resource_reason = _budget_resource_reason(cursor, budget)
        if resource_reason:
            return ("BUDGET", resource_reason)
        stop = _session_count_stop(sdlc_dir, pid, config, cursor=cursor)
        if stop:
            return stop
        current = (_session_admission_snapshot(sdlc_dir, pid, config, cursor)
                   if ceiling or iterations else
                   (_session_read(path) if path.exists() else
                    {"in_flight": [], "settled_admissions": 0}))
        in_flight = current["in_flight"]
        goal_text = str(goal)
        if goal_text not in in_flight:
            in_flight.append(goal_text)
        _session_write(path, {"in_flight": in_flight,
                              "settled_admissions": current["settled_admissions"] or 0})
        return None

    return _session_locked(sdlc_dir, pid, _check_and_write, path=path,
                           require_lock=bool(ceiling or iterations))


def _cancel_goal_slot(sdlc_dir, session_pid, goal):
    """Refund a reservation rejected BEFORE `_claim`, never a possibly durable claim."""
    pid = int(session_pid)
    path = _session_marker_path(sdlc_dir, pid)

    def _cancel():
        current = _session_read(path, strict=True)
        goals = current["in_flight"]
        if str(goal) in goals:
            _session_write(path, {"in_flight": [g for g in goals if g != str(goal)],
                                  "settled_admissions": current["settled_admissions"]})

    _session_locked(sdlc_dir, pid, _cancel, path=path, require_lock=True)


def _session_claim(sdlc_dir, session_pid, goal):
    """#1199: register `goal` as in-flight under `session_pid`'s own registry entry, auto-creating
    that entry if this session never explicitly called `session_start` (the normal case for a
    bare `loop.py next`/`next-batch` — see `_next()`'s own docstring) — so registration needs no
    hand-added flag anywhere in the call chain. Admission is reserved before `_claim` runs; an
    update failure leaves that reservation in place, conservatively consuming the slot.

    #1239 review (finding 3): the read-modify-write below (read `in_flight`, append, write the
    whole list back) is run inside `_session_locked` — two genuinely concurrent claims under the
    SAME session (e.g. two `next_batch` slots each picking a different goal) used to race this
    exact read-then-write and silently lose one goal's registration (confirmed with a real
    `multiprocessing.Process` pair, barrier-synchronized: 10/10 runs dropped one goal without this
    guard — see `test_session_claim_race_loses_a_goal_without_a_lock_guard`). The list is re-read
    from disk INSIDE the locked section, not reused from before the lock was acquired, so a
    sibling's write that landed while this call was waiting on the lock is never clobbered."""
    try:
        pid = int(session_pid)
        path = _session_marker_path(sdlc_dir, pid)
        path.parent.mkdir(parents=True, exist_ok=True)
        goal = str(goal)

        def _write():
            current = _session_read(path, strict=True) if path.exists() else {"in_flight": [], "settled_admissions": 0}
            in_flight = current["in_flight"]
            if goal not in in_flight:
                in_flight.append(goal)
            _session_write(path, {"in_flight": in_flight, "settled_admissions": current["settled_admissions"]})

        _session_locked(sdlc_dir, pid, _write, path=path, require_lock=True)
    except (OSError, TypeError, ValueError):
        pass


def _session_release(sdlc_dir, goal, settle=True):
    """#1199: remove `goal` from EVERY session's in-flight list, wherever it turns up — called from
    `_record()`/`_release()` once a goal's claim genuinely ends. Sweeps rather than targeting one
    session's own entry because the process completing the goal (a dispatched subagent, an operator
    hand-running `record`) does not necessarily share the CLAIMING process's own `os.getppid()`;
    without this a completed goal could stay "in-flight" under a session that no longer has any
    stake in it — a worse bug than the one this file exists to fix (a done goal skipped forever).
    Best-effort, mirrors `agent_end`'s own unconditional-cleanup posture: attempted even for a goal
    with no in-flight registration anywhere (a stale claim already cleared, or one this session
    never itself claimed) — a safe no-op either way.

    #1239 review (finding 3): each session's own write is now scoped inside THAT session's own
    `_session_locked` (same guard `_session_claim`/`session_start` use for their entry) — this
    call touches potentially several sessions' files, one lock at a time, never all at once, so a
    release can never deadlock against a sibling `_session_claim`/`session_start` that holds a
    DIFFERENT session's lock. `in_flight` is re-read from disk inside the lock (the copy from
    `_session_entries` above may already be stale by the time the lock is acquired) so a claim that
    landed in the gap between the scan and the lock is never dropped by this release.

    The lock is now taken for EVERY entry `_session_entries` yields, not only ones whose STALE
    `in_flight` snapshot already shows `goal` — an earlier version of this fix filtered on that
    snapshot first (`if goal in in_flight: ...`) and reproduced a second, subtler race
    (`test_session_claim_and_release_race_do_not_lose_updates`): `Path.write_text` truncates its
    target to 0 bytes on open, before writing the new content, so this call's own UNLOCKED scan can
    land inside a CONCURRENT locked writer's truncate-then-write window, read invalid/empty JSON,
    and — via `_session_read`'s own by-design fail-open, `except (OSError, ValueError): return
    {"in_flight": []}` — see an empty list for a file that genuinely, durably contains the goal.
    The stale pre-check then skipped that session's lock entirely: no exception, no attempt, the
    goal silently never released. Locking unconditionally means the only read that can ever decide
    whether `goal` is removed is the one taken INSIDE that session's own lock, where no concurrent
    writer can be mid-truncate — correctness over the minor cost of locking a session that turns
    out not to hold `goal`."""
    goal = str(goal)
    for pid, path, _stale_in_flight in _session_entries(sdlc_dir):
        def _write(path=path):
            current = _session_read(path) if path.exists() else {"in_flight": [], "settled_admissions": None}
            cur = current["in_flight"]
            if goal in cur:
                data = {"in_flight": [g for g in cur if g != goal]}
                if current["settled_admissions"] is not None:
                    data["settled_admissions"] = current["settled_admissions"] + int(settle)
                _session_write(path, data)
        try:
            _session_locked(sdlc_dir, pid, _write, path=path, require_lock=True)
        except (OSError, RuntimeError) as exc:
            print(f"loop: session admission release delayed for {goal}: {exc}", file=sys.stderr)


def _unsafe_thread_reason(thread):
    """None iff `thread` is safe to embed as a single path component in `_agent_marker_path`,
    else the reason it is not — the one place this check lives, shared by that function AND the
    `agent-start` CLI verb's own loud refusal, so the two can never drift apart (matching this
    file's other shared-validator idiom, e.g. `ledger.reject_newline`).

    `thread` reaches here from an LLM-authored slice id (`.sdlc/plans/<goal>.slices.json`,
    validated by `slices.py` with only `.strip()` — no character/format check) inside an
    unattended pipeline, so it must be treated as untrusted, exactly like any other agent-typed
    CLI value. `f"{thread}.active"` is built as a single Python string, but pathlib's own `/`
    join operator RE-PARSES a string argument for separator characters — so a `/` or `\\` inside
    `thread` does not stay one filename, it becomes ADDITIONAL path segments, some of which can
    be a literal `..` (or, worse, an absolute-path segment that pathlib's `/` operator lets
    silently REPLACE everything joined before it). Checking for a literal `..` alone would miss
    that: rejecting any separator closes the join's only real danger directly, and `..` is kept
    as an explicit belt-and-suspenders check for readability and in case a future refactor ever
    changes the fixed `.active` suffix this relies on. `:` is rejected too for the same class of
    risk on Windows (a drive-letter-rooted path). Never raises — `str(thread)` handles anything."""
    text = str(thread)
    if any(c in text for c in ("/", "\\", ":")) or ".." in text:
        return ("must not contain '/', '\\', ':', or '..' "
                 "(it becomes a filename under .sdlc/state/agents/<goal>/)")
    return None


def _agent_marker_path(sdlc_dir, goal, thread="main"):
    """Raises ValueError for an unsafe `thread` (see `_unsafe_thread_reason`) OR an unsafe `goal`
    (see `state.unsafe_goal_reason` — added after independent review of #486/PR #487 found `goal`
    had NO validation here at all, unlike `thread`: `agent_end()`'s unconditional, ungated
    `shutil.rmtree()` on the resulting path — reachable from the everyday `record` verb, not just
    the `agent-end` escape hatch — made this the most severe of the five sibling gaps that review
    found). Every existing caller already treats a ValueError here as fail-open: `agent_start`'s
    try/except already catches it, `agent_alive` catches it too (the call sits inside its own try
    block, right alongside the read it was already guarding), `agent_end`'s broad `except
    Exception` catches it BEFORE `shutil.rmtree` is ever reached (the raise happens while
    computing `d`, not after), and `agent_threads` now has its own guard added alongside this
    fix."""
    reason = _unsafe_thread_reason(thread)
    if reason:
        raise ValueError(f"unsafe thread id {thread!r}: {reason}")
    stem = work.stem(goal)
    reason = state.unsafe_goal_reason(stem)
    if reason:
        raise ValueError(f"unsafe goal {goal!r} for the agent marker: {reason}")
    return pathlib.Path(sdlc_dir) / "state" / "agents" / stem / f"{thread}.active"


def _agent_marker_identity(sdlc_dir, goal, thread="main"):
    """(pid, Codex thread ID or None) from a marker; accept legacy and Claude bare PIDs."""
    path = _agent_marker_path(sdlc_dir, goal, thread)
    raw = path.read_text().strip()
    if raw.startswith("{"):
        data = json.loads(raw)
        pid = int(data["pid"])
        owner = data["codex_thread_id"]
        if not isinstance(owner, str) or not _CODEX_THREAD_ID.fullmatch(owner):
            raise ValueError("invalid Codex agent marker identity")
        return pid, owner.lower()
    return int(raw), None


def _agent_marker_expired(path, config):
    """Only an unreadable marker older than the configured lease may be recovered."""
    ttl = ledger.lease_ttl_seconds(config)
    if ttl is None:
        return False
    try:
        return (time.time() - path.stat().st_mtime) >= ttl
    except OSError:
        return False


def agent_start(sdlc_dir, goal, agent_pid, config, thread="main"):
    """Register `agent_pid` — the CALLER's own long-lived process id, the identical $PPID-capture
    contract `--session-pid` already documents, NOT any individual `loop.py` invocation's own —
    as the process driving (goal, thread) right now.

    #1197: unconditional as of this fix — no longer gated on `agent_watch.enabled`. This marker is
    now the load-bearing signal `ledger.claim_belongs_to_me`'s `live_worker_check` consults (via
    `_goal_has_registered_worker`, from both `_next()` and `work.py`'s
    `_resume_blocked_by_a_live_sibling`) to tell a genuinely abandoned claim apart from one whose
    picker process exited normally — as it always does within moments — while a long-lived worker
    keeps going. SKILL.md step 3a already calls this unconditionally in the ordinary per-goal flow
    (`agent-start --pid $PPID`), but the shipped config template sets `agent_watch.enabled: false`
    EXPLICITLY (not merely absent) — gating the WRITE on that flag made the whole mechanism inert
    for every fresh `/sigma-init` scaffold, exactly the gap #1197's own corrections section names.
    `agent_watch.enabled` still gates the SEPARATE dead-agent NOTIFY tick (`agent_watch.py`'s own
    `enabled()`, unchanged) — that pass is a genuinely opt-in convenience (an email or a ledger
    note); writing this tiny local marker file is unconditional, cheap, and fail-open, the same
    posture `agent_end`'s cleanup already takes ("no gate: cleanup always attempts").

    `config` supplies the marker lease TTL. A live marker naming a DIFFERENT pid, or a live Codex
    marker naming a different thread, is never overwritten (#2527) -- nor is a fresh unreadable
    marker. A stale unreadable marker may be replaced after that TTL, so a crash during an older
    write does not wedge the goal forever. The CLI refuses a Codex registration it could not make;
    legacy Claude marker content remains a bare PID."""
    try:
        path = _agent_marker_path(sdlc_dir, goal, thread)
        path.parent.mkdir(parents=True, exist_ok=True)
        codex_thread = _session_codex_thread()
        if codex_thread and fcntl is None:
            return False

        def _write():
            # Code review, #2527: `agent_pid` parsed ONCE here (was up to twice below, and inline
            # inside the new `marker_pid != int(agent_pid)` comparison specifically) -- a malformed
            # `agent_pid` now returns the same clean `False` every other branch in this function
            # already gives, rather than raising `ValueError` out of the comparison itself, which
            # the OUTER `except (OSError, TypeError, ValueError)` catches but answers `None` for a
            # plain Claude caller (its own documented fail-open posture for an INTERNAL I/O/parse
            # hiccup) -- ambiguous where `False` is a definite, CLI-visible refusal
            # (`loop.py`'s own `agent-start` dispatch branches on `is False`, not on truthiness).
            # Confirmed unreachable via the shipped CLI (`--pid` is validated as an int before
            # `agent_start` is ever called) or any in-repo caller today, but the same clean
            # `False` every other malformed-input path here already returns costs nothing to keep.
            try:
                pid_int = int(agent_pid)
            except (TypeError, ValueError):
                return False
            marker_state, marker_pid = agent_alive(sdlc_dir, goal, config, thread=thread)
            if (path.exists() and marker_state == "unknown"
                    and not _agent_marker_expired(path, config)):
                return False
            if marker_state == "alive":
                try:
                    owner = _agent_marker_identity(sdlc_dir, goal, thread)[1]
                except (OSError, TypeError, ValueError, KeyError):
                    return False
                # #2527: the ORIGINAL check here compared owner identity alone. For a plain Claude
                # caller `_session_codex_thread()` returns None on BOTH sides of a genuine collision
                # (this marker's owner AND this call's codex_thread), so `owner != codex_thread` was
                # always False and this branch fell through to an unconditional overwrite --
                # destroying the exact evidence `work.py`'s `_blocked_by_a_live_foreign_agent`
                # depends on one step later, so a real two-session collision produced NO refusal at
                # all (see that function's own `pid not in mine or marker_owner != codex_thread`,
                # the identical shape mirrored here with a caller-supplied identity instead of an
                # inferred one -- `agent_pid` IS the identity, given directly, not guessed from a
                # process tree). Both the PID and the owner must now match to proceed.
                if marker_pid != pid_int or owner != codex_thread:
                    return False
            if not codex_thread:
                path.write_text(f"{pid_int}\n")
                return True

            # Readers do not take this lock, so publish the new JSON in one replace operation.
            payload = json.dumps({"pid": pid_int, "codex_thread_id": codex_thread}) + "\n"
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                                 prefix=f".{path.name}.", delete=False) as temp:
                    temp_path = pathlib.Path(temp.name)
                    temp.write(payload)
                    temp.flush()
                    os.fsync(temp.fileno())
                os.replace(temp_path, path)
            finally:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)
            return True

        if fcntl is None:
            return _write()  # Claude's legacy path on hosts without POSIX flock.
        with open(path.with_suffix(".lock"), "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                return _write()
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
    except (OSError, TypeError, ValueError):
        # Preserve Claude's historical fail-open I/O behavior. Codex identity failures must be
        # visible because a shared PID cannot serve as a safe fallback identity.
        if not (os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_SESSION_ID")) and (
                os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")):
            return False
        return None


def agent_heartbeat(sdlc_dir, goal, thread="main"):
    """#1391: refresh `goal`'s liveness marker so its mtime measures LIVENESS, not START TIME.

    THE BUG THIS CLOSES. `agent_start` writes the marker once and nothing ever touched it again, so
    `agent_alive`'s TTL check (`mtime past lease_ttl_seconds` -> "dead") measured how long ago the
    agent STARTED. A goal worked for longer than the lease — this repo ships
    `budget.max_minutes: 900` against `lease.ttl_hours: 12`, so an ordinary overnight goal qualifies
    — read as DEAD while its agent was still actively working. Every consumer of
    `_goal_has_registered_worker` therefore lost its protection exactly when the goal was
    long-running, which is precisely when losing it hurts most.

    The TTL itself is still the right backstop: it guards PID REUSE (a different process inheriting
    a recorded pid). Heartbeating does not weaken that — a reused pid is not the thing refreshing
    THIS marker — it just stops the check firing on a healthy long-running agent.

    Deliberately a TOUCH, not a rewrite of the pid: the registered pid is the agent's identity and
    must not drift, so a heartbeat only moves the timestamp. Fail-open and never raises — a missed
    heartbeat costs one stale reading, and the pid check still answers correctly."""
    try:
        path = _agent_marker_path(sdlc_dir, goal, thread)
        if path.exists():
            codex_thread = _session_codex_thread()
            if _agent_marker_identity(sdlc_dir, goal, thread)[1] != codex_thread:
                return False
            os.utime(path, None)
            return True
    except (OSError, TypeError, ValueError, KeyError):
        pass
    return False


def agent_heartbeat_all(sdlc_dir, goal):
    """Heartbeat EVERY registered thread of `goal` — intra-goal slice parallelism registers more
    than one, and `_goal_has_registered_worker` is an `any()` across them, so refreshing only
    "main" would let a slice-parallel goal go stale anyway."""
    beat = False
    for thread in agent_threads(sdlc_dir, goal):
        beat = agent_heartbeat(sdlc_dir, goal, thread=thread) or beat
    return beat


def agent_alive(sdlc_dir, goal, config, thread="main"):
    """(state, pid) — state in "alive" | "dead" | "unknown"; pid is the recorded int if the
    marker parsed, else None. "unknown" (no marker at all) is NOT "dead": nobody registered a
    pid for this (goal, thread) on THIS machine — a claim held by a different actor, a different
    machine (pid_alive() is single-machine-only), or a session that predates this feature. Only a
    marker whose pid resolves DEFINITIVELY dead, or whose file is past the lease TTL despite a
    resolvable pid (reuse risk, same reasoning session_active already applies), is "dead" — the
    same fail-toward-inaction bias pid_alive()'s own docstring argues for. An unsafe `thread`
    (see `_agent_marker_path`) degrades to "unknown" too, via the same except clause — that is
    the correct answer either way: nobody validly registered a marker for it."""
    try:
        path = _agent_marker_path(sdlc_dir, goal, thread)
        pid, _owner = _agent_marker_identity(sdlc_dir, goal, thread)
    except (OSError, ValueError, TypeError, KeyError):
        return "unknown", None
    if not ledger.pid_alive(pid):
        return "dead", pid
    ttl = ledger.lease_ttl_seconds(config)
    if ttl is not None:
        try:
            if (time.time() - path.stat().st_mtime) >= ttl:
                return "dead", pid
        except OSError:
            pass
    return "alive", pid


def agent_threads(sdlc_dir, goal):
    """Every thread name with a currently-registered marker for `goal` — ["main"] is the common
    case; more than one entry means genuine intra-goal (slice) parallelism registered
    independently-tracked pids (see SKILL.md wiring, item 6). An unsafe `goal` (see
    `_agent_marker_path`) degrades to "no threads registered" — the correct answer either way:
    nobody validly registered a marker for it."""
    try:
        d = _agent_marker_path(sdlc_dir, goal).parent
    except ValueError:
        return []
    return sorted(p.stem for p in d.glob("*.active")) if d.is_dir() else []


def agent_end(sdlc_dir, goal):
    """Clear every thread's marker for `goal` — called automatically from _record() (item 7), not
    the agent, so a cleanly-finished goal never lingers in agent_watch's candidate set. No gate:
    cleanup always attempts, even if agent_watch was later turned off — a stale marker directory
    left over from when it was on is exactly what this prevents. Safe no-op if nothing to clear.

    NEVER call this for anything OTHER than a genuinely terminal goal — it clears EVERY thread's
    marker unconditionally, with no identity check at all. #2015: that used to also be reachable
    as a manual CLI escape hatch (`loop.py agent-end <dir> <goal>`, no `--pid`), which `work.py`'s
    own blocked-resume message suggested to a stuck agent — so an agent merely wrong about another
    thread's liveness, or unaware a concurrent slice thread even existed, silently evicted a live
    sibling's registration with zero verification and zero trace. That CLI verb is retired; the
    identity-checked replacement for "I am blocked, is the other side actually dead" is
    `agent_reclaim()` below, never this function."""
    try:
        d = _agent_marker_path(sdlc_dir, goal).parent
        if d.is_dir():
            import shutil
            shutil.rmtree(d, ignore_errors=True)
    except Exception:                # noqa: BLE001 - fail-open by design
        pass


def _phase_marker_end(sdlc_dir, goal):
    """Unlink `phase_report.py`'s per-goal phase marker. Called from `_record()` ONLY (see its own
    comment for why not `_release`, and not `phase_report.py end`). Touches exactly one path,
    derived the same way `phase_report.marker_path` derives it — an unsafe goal stem yields no path
    and therefore no deletion, never a guess. Fail-open: cleanup never costs a terminal record."""
    try:
        stem = work.stem(goal)
        if state.unsafe_goal_reason(stem):
            return
        (pathlib.Path(sdlc_dir) / "state" / "phase" / f"{stem}.json").unlink(missing_ok=True)
    except Exception:                # noqa: BLE001 - fail-open, exactly like agent_end above
        pass


def agent_reclaim(sdlc_dir, goal, config, thread=None):
    """{"reclaimed": [(thread, pid), ...], "blocked": [(thread, pid), ...]} — the identity-checked
    replacement for the old blind `agent-end` CLI escape hatch (#2015). Unlike `agent_end()`, which
    unconditionally `shutil.rmtree`s the WHOLE marker directory (every thread, not just the one the
    caller is actually blocked on), this verifies each TARGET thread's own recorded pid via
    `agent_alive()` — the identical pid+lease-TTL check `_blocked_by_a_live_foreign_agent` already
    trusts to tell a live sibling apart from a genuinely abandoned one — before touching anything,
    and removes only the single marker FILE for a thread confirmed dead, never the parent
    directory. A takeover is recorded as an `agent_reclaimed` action-log entry, so silently
    evicting a stale registration becomes an AUDITED one.

    `thread=None` sweeps every currently-registered thread for `goal` (mirrors
    `agent_heartbeat_all`'s own "reclaim everything registered" default) — the right shape for a
    caller that does not know in advance which thread is stuck. `thread=<name>` narrows to exactly
    one and touches nothing else, even a different thread that is ALSO genuinely dead — the shape
    `work.py`'s own refusal message hands back, since it already names the one thread that blocked
    it. An unsafe thread name (see `_unsafe_thread_reason`) degrades to nothing-to-reclaim rather
    than raising — the same fail-open posture every other `_agent_marker_path`-adjacent function
    in this file already takes.

    Per-thread verdict from `agent_alive()`:
      "dead"    -> the marker FILE is unlinked and an `agent_reclaimed` entry is written;
                   reported in `reclaimed`.
      "alive"   -> untouched; reported in `blocked` so the caller can tell the reclaim refused
                   rather than silently doing nothing.
      "unknown" -> untouched, reported in neither — nobody validly registered this (goal, thread),
                   so there is nothing to reclaim or to be blocked by.
    `agent_alive()` itself never raises (see its own docstring), but any unexpected exception
    reaching this loop is still treated exactly like "alive" — fail toward inaction, the same bias
    `ledger.pid_alive()`'s own docstring argues for ("guessing True and waiting is far better than
    guessing False and reclaiming a live sibling's goal"). Never raises."""
    if thread is not None:
        targets = [] if _unsafe_thread_reason(thread) else [thread]
    else:
        targets = agent_threads(sdlc_dir, goal)
    reclaimed, blocked = [], []
    for t in targets:
        try:
            state_, pid = agent_alive(sdlc_dir, goal, config, thread=t)
        except Exception:            # noqa: BLE001 - fail toward inaction, see docstring
            state_, pid = "alive", None
        if state_ == "dead":
            try:
                _agent_marker_path(sdlc_dir, goal, t).unlink(missing_ok=True)
                actionlog.safe_append(sdlc_dir, goal, "agent_reclaimed", thread=t, pid=pid)
            except Exception:        # noqa: BLE001 - fail-open; leave the marker rather than half-act
                continue
            reclaimed.append((t, pid))
        elif state_ == "alive":
            blocked.append((t, pid))
        # "unknown" -> no-op, reported nowhere: nothing was ever validly registered here.
    return {"reclaimed": reclaimed, "blocked": blocked}


def _surface_inbox(sdlc_dir):
    """Print anything a teammate needs from you BEFORE handing over the next goal.

    Between goals is the only boundary that works: nothing can inject a message into a running
    session, and interrupting a goal mid-flight is how half-finished work gets lost. Worst-case
    latency is therefore one goal, which is the right trade. stderr ONLY — stdout is the goal the
    caller parses. Fail-open: no watcher, no inbox, no problem."""
    try:
        watch = _load("watch")
        text = watch.read_inbox(sdlc_dir)
        if text:
            print("\n=== LEDGER INBOX — a teammate needs you ===\n" + text
                  + "\n=== end inbox ===\n", file=sys.stderr)
            watch.clear_inbox(sdlc_dir)
    except Exception:
        pass


#: F17/#342's generous `verify.enforce` read. One copy since #312: `state.enforce_enabled` (its
#: docstring carries the full rationale), aliased here so existing callers and tests are unchanged.
_enforce_enabled = state.enforce_enabled


def _config_warnings(config):
    """Loud, once-per-run heads-ups for config states that silently do the wrong thing — found the hard
    way adopting into real repos. Plain strings so the CLI prints them to stderr, never touching stdout
    (the goal channel the caller parses)."""
    out = []
    # #236: silent once the choice is on record -- `work._enabled_why` is written only by a
    # deliberate gesture (`preflight.py local-only`, `/sigma-init --local-only`). A nag after the
    # user said "local-only" on purpose is noise; one that is still unexplained points at init.
    if not work.enabled(config) and not (config.get("work") or {}).get("_enabled_why"):
        out.append("work.enabled is off — the loop writes NOTHING to git: a completed goal's changes "
                   "stay in your working tree, no branch/commit/PR. Run /sigma-init (--work on, or "
                   "--local-only to keep this and stop this warning), or set work.enabled.")
    verify = config.get("verify") or {}
    if _enforce_enabled(verify) and not verify.get("command"):
        out.append("verify.enforce is on but verify.command is empty — EVERY `done` will be refused. "
                   "Set verify.command (or a per-goal verify_command) — "
                   + _VERIFY_SET_HINT + ".")
    out.extend(_autowatch_setup_warnings(config))
    return out


def _autowatch_setup_warnings(config):
    """#1323/#1322: `ledger.autowatch.enabled` alone does nothing — a mention/assignment/blocker is
    still only ever detected, never acted on, until an adapter is actually wired up. Fires at the
    SAME trigger point as `_ensure_watcher` (every `start`, "as soon as someone runs any sdlc
    command" — the epic's own instruction, so nobody has to separately remember a setup step), one
    nudge per machine's declared `ledger.autowatch.surface` (#1323's design: declared, not
    runtime-detected — see `autowatch.resolve_surface`'s own docstring for why). Fail-open: any
    error here (missing autowatch.py, unreadable config) must not block a run over a reminder."""
    try:
        if not ledger.enabled(config):
            return []
        aw = _load("autowatch")
        if not aw.enabled(config):
            return []
        if aw.adapter_wired(config):
            return []
        surface = aw.resolve_surface(config)
        if surface == "desktop":
            return ["ledger.autowatch is enabled (surface=desktop) but no scheduled task is set up "
                    "yet — ask me (in this Desktop session) to \"create a scheduled task that runs "
                    "every ledger.autowatch.poll_interval_minutes and executes `python3 "
                    "skills/sigma-loop/scripts/autowatch.py tick .sdlc`, with the isolated-worktree "
                    "toggle on\" to finish setup."]
        return ["ledger.autowatch is enabled (surface=cli) but ledger.autowatch.channel_webhook_url "
                "is not set — see skills/sigma-loop/channels/sigma-autowatch/README.md for the "
                "one-time Channels setup, then set channel_webhook_url once it's running."]
    except Exception:                                       # noqa: BLE001 - a reminder must never
        return []                                            # block a real run


# Ordered, first match wins, case-insensitive, matched against a lowercased `detail`. Every needle
# below (other than "irreversible", "rate limit", and the decompose_check-owned group starting at
# "needs manual decomposition") is a verbatim substring of real work.py PARK: text, verified against
# the live source — see plan #139 Design decision 4 for the file:line each one comes from.
# The decompose_check group is instead verbatim substrings of loop.py's OWN decompose_check park
# details, not work.py's: "needs manual decomposition" from #519's `park`/`file`-degrade path, plus
# five more from #522's real `file`-mode filing. "rate limit" (#1242) is instead a verbatim substring
# of loop.py's OWN `_record()` downgrade-to-park detail (`source error recording '<result>' (<exc>)`,
# #1201) when the wrapped exception is a real GitHub rate-limit/quota error -- "API rate limit
# exceeded ..." for a primary limit, "... a secondary rate limit ..." for the secondary one, both
# verbatim GitHub API error bodies (see gh_session.py's own header comment) and both containing this
# substring. Unmatched text (including every agent-supplied `loop.py record ... parked "<free
# text>"`, AND work.py's own fixed, machine-generated park/gate-verdict messages -- gate()'s
# mergeability-retry line, an unreadable PR-state or local-branch-tip read, a missing headRefOid on
# either side, and an exhausted BEHIND rebase race -- deliberately stay unclassed here rather than
# each getting its own "unknown" needle; see `_MECHANICAL_UNKNOWN_NEEDLES` below for how those are
# told apart from genuine free text once reason_class alone has already put both in the same
# "unknown" bucket) maps to "unknown", never guessed. None of these needles is currently a substring
# of another EXCEPT "needs manual decomposition" also appearing inside the `file`-mode-with-no-
# create-seam detail below (both map to the same class, so the overlap is harmless) — the ordering
# is otherwise defensive rather than load-bearing.
_REASON_CLASS_RULES = (
    ("irreversible", "irreversible"),                       # SKILL.md's own park-for-irreversible
                                                              # prose — best-effort only, see _reason_class's docstring.
                                                              # This rule firing first used to make the
                                                              # "irreversible" signal word inside
                                                              # decision_tier's own _PATTERNS dead code
                                                              # (it always matched HERE first, so
                                                              # decision_tier never saw it) -- #1185
                                                              # fixed that by adding "irreversible" to
                                                              # _DECISION_TIER_REASON_CLASSES below.
    ("rate limit", "quota"),                                # #1242: a quota/rate-limit park (the
                                                              # `source.complete()` downgrade-to-park
                                                              # path from #1201, when the wrapped
                                                              # exception is an exhausted GitHub
                                                              # REST/GraphQL rate limit) -- a
                                                              # transient failure of the remote
                                                              # SYSTEM, not a judgment about the
                                                              # goal's own state. See the (removed)
                                                              # #1201 scoping note this supersedes,
                                                              # kept in git history, and
                                                              # test_reason_class_quota_detail_is_
                                                              # unknown_by_design's replacement below.
    ("no pr for this goal", "dependency"),
    ("no fresh verify evidence", "no_evidence"),
    # #278: `work.rebase()`'s tree-guard refusal. Before "rebase deferred" and its own needle: nothing
    # conflicts -- the base holds a revert of the goal's own work, and a human decides what it means.
    (work.REBASE_WOULD_DROP, "needs_decision"),
    ("rebase deferred", "merge_conflict"),
    ("conflicts with the base branch", "merge_conflict"),
    ("stale head", "merge_conflict"),
    ("not safe to merge", "failing_check"),
    ("changes requested", "needs_decision"),
    ("unresolved review thread", "needs_decision"),
    ("not approved yet", "needs_decision"),
    ("sigma:block", "needs_decision"),
    ("did not converge", "review_cap"),
    ("needs manual decomposition", "needs_decision"),       # loop.py's own decompose_check (#519)
    # #522: goal_decompose's `file` mode -- five more decompose_check park details, each its own
    # needle so none of them falls through to the "unknown" default the way relying solely on the
    # rule above would otherwise leave them.
    ("file mode needs an issue tracker", "needs_decision"),               # no create seam (local mode)
    ("could not confirm whether a decomposition was already filed", "no_evidence"),  # idempotency read raised
    ("decomposition already filed", "dependency"),                        # idempotency hit (marker found)
    ("failed to file decomposition goal", "needs_decision"),              # create_tracked_issue came back empty
    ("decomposition filed as #", "dependency"),                           # happy path (assigned or not)
    # #1774: the three fixed phrases `merge()`'s gated-action refusal can carry. Classified
    # DELIBERATELY rather than left to the "unknown" default: "unknown" IS in
    # `_DECISION_TIER_REASON_CLASSES`, so all three would reach a human either way -- but by
    # accident, and `contract/vocabulary.json`'s consumers would read a governed refusal as
    # unclassified free text. `needs_decision` is the honest existing member (a human restores the
    # grant, repairs the store, or drops the declaration); no new REASON_CLASSES member is added,
    # for exactly the cross-file-pinned-vocabulary reason #1201 records immediately below.
    # These are NOT in `work.MECHANICAL_PARK_PREFIXES` and must not be: unlike a rebase race or a
    # GitHub-API blip, a governed refusal is precisely the case a human is meant to look at.
    ("has been revoked", "needs_decision"),                            # access revoked
    ("has no verified value this call", "needs_decision"),             # locked key unverifiable
    ("proceeding on an unverified locked key", "needs_decision"),      # check-unavailable
)
# #1242 does the taxonomy work #1201 explicitly scoped OUT: a quota/rate-limit park (e.g. the
# `source.complete()` -> downgrade-to-park path #1201 itself added, when the underlying failure is
# an exhausted GitHub REST/GraphQL rate limit) now gets its own REASON_CLASSES member ("quota",
# matched by the "rate limit" needle above) instead of classifying as "unknown" indistinguishably
# from a genuinely unclassified park. This required touching every hand-synced copy of the pinned
# vocabulary: ledger.py's REASON_CLASSES tuple, contract/vocabulary.json's "reason_classes"
# list (CONTRACT_VERSION unchanged -- contract/README.md's own version rule says adding a
# vocabulary member is additive, not breaking), and
# tests/test_ledger.py::test_vocabulary_constants_match_spec_table's pinned assertion. Reusing an
# EXISTING class instead (the only alternative that would have stayed inside this one file) would
# have been worse than "unknown": a quota-exhausted `gh issue close` is a failure of the remote
# SYSTEM, not a judgment about the goal's own state, so filing it under e.g. "dependency" or
# "needs_decision" would corrupt the recurrence signal the pinned vocabulary exists to protect
# (a downstream park-recurrence gap report) -- which is exactly why "quota" is
# also excluded from `_DECISION_TIER_REASON_CLASSES` below, the same posture as dependency/
# no_evidence/merge_conflict/failing_check/review_cap. The park event's `why` field already carried
# the full exception text verbatim via `detail` before this (see `_record`'s "source error recording
# 'done'" downgrade above); "quota" now makes that same fact aggregable as a `reason_class` bucket
# too, not just readable one park at a time. See test_reason_class_quota_detail_classifies_as_quota,
# which supersedes this issue's own predecessor test
# (test_reason_class_quota_detail_is_unknown_by_design) that pinned the old by-design behavior.

# #1185: the reason_classes whose text can carry a genuine human judgment call -- and therefore the
# only ones `_record` ever OFFERS to `decision_tier.resolve()` (see `_mechanical_unknown_detail`
# below for a second, finer-grained check inside "unknown" that this class-level gate alone does not
# capture). `needs_decision` is #953's original gate. `irreversible` joins it because that
# reason_class's own rule (above) fires FIRST and intercepts any detail containing "irreversible"
# before it can ever reach `needs_decision` -- meaning "irreversible" was dead code inside
# decision_tier's own `_PATTERNS` (one of its own escalate_l0 signal words, unreachable through the
# only caller) until this widened the gate. `unknown` joins it because that is the reason_class this
# module's OWN `_reason_class` docstring says every agent-typed `loop.py record ... parked "<free
# text>"` falls into when it matches none of the ~20 fixed substrings above -- i.e. it is A home of
# real decision prose decision_tier's vocabulary was written to classify (#818/#952's worked
# examples). BUT `unknown` is NOT a pure free-text bucket: it is ALSO where every one of work.py's
# OWN fixed, machine-generated park/gate-verdict messages lands, for the identical reason genuine
# free text does -- no `_REASON_CLASS_RULES` needle happens to cover their wording either
# (`work.MECHANICAL_PARK_PREFIXES`, #1240: gate()'s mergeability-retry line, an unreadable PR-state
# or local-branch-tip read, a missing headRefOid on either side, and an exhausted BEHIND rebase
# race, and as of #2009 the two stale-resume refusals). #1185's own review caught only the
# FIRST of those (mergeability) and excluded it with a
# single needle hardcoded a second time in this file -- leaving the others to fabricate a
# judgment tier on an ordinary infra hiccup exactly like real decision prose would, until #1240's
# review found the gap and generalized the exclusion below to read work.py's own tuple instead of
# re-enumerating literals here. The six still-fully-EXCLUDED reason_classes
# (dependency/no_evidence/merge_conflict/failing_check/review_cap/quota -- "quota" joining the set
# as of #1242) are fixed, mechanical park reasons with no judgment call embedded in the text --
# classifying them would only ever add a noisy, uninformative escalate_l1 default.
_DECISION_TIER_REASON_CLASSES = ("needs_decision", "irreversible", "unknown")

# #1240: work.py's own fixed, machine-generated park/gate-verdict prefixes, lowercased once here for
# the same case-insensitive substring match `_REASON_CLASS_RULES` uses. Read from
# `work.MECHANICAL_PARK_PREFIXES` -- that module's own single source of truth for this wording, not
# re-typed as an independent copy in this file -- so a future edit to any one of those messages can
# only ever change WHAT this matches, never leave the two files silently disagreeing about it the
# way #1185's original single hardcoded needle here could have.
_MECHANICAL_UNKNOWN_NEEDLES = tuple(prefix.lower() for prefix in work.MECHANICAL_PARK_PREFIXES)


def _reason_class_matched(detail):
    """Classify a park `detail` string like `_reason_class` does, but also report WHICH
    `_REASON_CLASS_RULES` needle fired -- `None` when nothing matched and the text fell through to
    the "unknown" default. Pure, no I/O."""
    text = (detail or "").lower()
    for needle, cls in _REASON_CLASS_RULES:
        if needle in text:
            return cls, needle
    return "unknown", None


def _reason_class(detail):
    """Classify a park `detail` string into one of `ledger.REASON_CLASSES`, by ordered substring
    match — pure, no I/O. Two of the eleven documented classes are honestly NOT guaranteed
    reachable from here, and that is stated rather than silently absent:

    - `irreversible` is best-effort only. It matches SKILL.md's own park-for-irreversible prose,
      but that prose gives no fixed wording contract for what an agent actually writes into
      `detail` when it parks for an irreversible action, so this rule has a real chance of firing
      but is a heuristic, not a proof.
    - `budget` is PROVABLY unreachable by this function. `run_loop`'s BUDGET branch breaks the run
      loop before `_next` ever returns a goal to record against, so `_record` (and therefore this
      classifier) is never called on the budget path at all — there is no `detail` string for it
      to classify.

    See `_reason_class_matched` for the sibling that also reports which needle fired, if any, and
    `_mechanical_unknown_detail` for the separate check that tells work.py's own mechanical
    "unknown" text apart from genuine free-text decision prose landing in the same class."""
    return _reason_class_matched(detail)[0]


def _mechanical_unknown_detail(detail, reason_class):
    """True when `detail` reached reason_class "unknown" not because it is genuine free-text
    decision prose, but because it IS (or contains) one of work.py's own fixed, machine-generated
    park/gate-verdict messages (`_MECHANICAL_UNKNOWN_NEEDLES`, sourced from
    `work.MECHANICAL_PARK_PREFIXES`) -- a git/GitHub-API-mechanics failure gate()/_reconcile_behind()
    already handles by failing closed, never a human weighing a tradeoff. Independent review of
    #1185 caught the first instance of this (the mergeability-retry message) via a needle hardcoded
    directly in this file; independent review of #1240 then found five more of the identical shape
    that needle never covered, simply because none of them happened to contain THAT one needle's
    text. Generalized here against work.py's own tuple instead of enumerating more literals a second
    time, so `_record` never fabricates a "this needs human judgment" tier for an ordinary infra
    hiccup -- a transient git read failure, GitHub API flakiness, a rebase race -- which is precisely
    the signal-to-noise problem `_DECISION_TIER_REASON_CLASSES` exists to avoid. Pure, no I/O; only
    meaningful when `reason_class == "unknown"` (every other class already carries its own, more
    specific meaning, so this always returns False for those without even checking `detail`)."""
    if reason_class != "unknown":
        return False
    text = (detail or "").lower()
    return any(needle in text for needle in _MECHANICAL_UNKNOWN_NEEDLES)


def _release_checkout(sdlc_dir, goal, merged=False):
    """Drop a completed goal's worktree, from CODE. Called only from `_record`, only on a real
    `done`, and only after every piece of that goal's own bookkeeping has already landed.

    THE MECHANISM EXISTED; NOTHING CALLED IT. `work.finish()` has always done this correctly, but
    its only trigger was a sentence in `skills/sigma-loop/SKILL.md` ("After a `done`, release the
    checkout"), mid-paragraph in a block otherwise about `auto_merge` modes -- so it ran only when
    the agent read that line, was still alive after `record done`, and chose to act. Every turn
    that ended, crashed, compacted or was interrupted in between leaked a checkout permanently,
    because nothing ever looked at a closed goal again. Measured on this repo 2026-09-01: 33 live
    work records, 8 of 9 sampled goals CLOSED, 50 worktrees accumulated -- and since `finish()`
    unlinks the record on every successful path, it had demonstrably never run for any of them.
    A lifecycle step whose only trigger is prose has no reliability story (AGENTS.md: triggers live
    in Sigma's own Python, and a host hook may be an accelerator, never load-bearing).

    NOT A NEW POLICY. `work._open_pr_refusal`'s own docstring already states that SKILL.md routes a
    done "unconditionally to `finish`, with no wait step" -- this moves that documented,
    already-intended call out of prose and into the one chokepoint every terminal outcome passes
    through. What changes is that it now actually happens.

    CALLING IT UNCONDITIONALLY IS SAFE BECAUSE OF ITS REFUSALS, NOT DESPITE THEM. `finish` keeps a
    tree that still holds uncommitted work (a parked goal stays intact for whoever picks it up) and
    one whose PR is POSITIVELY CONFIRMED still OPEN and unarmed -- `state/work/<goal>.json` is the
    only place that PR number lives, and every later `merge`/`rebase`/`post-review` needs it
    (#1202). Both are fail-open: no `gh`, no network, an unreadable reply, a MERGED or a
    CLOSED-unmerged PR all proceed. No `work.enabled(config)` gate is added here for the same
    reason: with no record, `finish` returns "nothing to finish" without touching git, so it is
    already inert on a repo that never cuts checkouts.

    FAIL-OPEN, the same posture the `source.complete()` handler above takes. This is housekeeping
    layered on a terminal outcome already written to the ledger, the action log and the cursor; an
    exception must never propagate out of `_record` and turn a shipped goal into a traceback. The
    record survives a failure, so the state stays recoverable rather than half-erased.

    REPORTED, NOT SILENT. A checkout that was KEPT is the one case this deliberately does not clean
    up, and LIVENESS says it must be distinguishable from one that was never there -- so the reason
    is named on stderr instead of being discovered in `git worktree list` months later. "nothing to
    finish" is the routine work-disabled case and stays quiet.

    `config` is loaded here rather than threaded through `_record`'s eighteen call sites: `finish`
    accepts it but never reads it, and `state.load_config` RAISES on a missing/malformed config --
    which is precisely why the load sits inside the guard."""
    # #255 (1): `merged` -- the caller confirmed the merge by REST moments ago -- is forwarded only
    # when true, so `finish` skips its two GraphQL `gh pr view` reads; every other call keeps the
    # three-argument shape the existing test doubles declare.
    try:
        config = state.load_config(sdlc_dir)
        outcome = (work.finish(sdlc_dir, config, goal, merged=True) if merged
                   else work.finish(sdlc_dir, config, goal))
    except Exception as exc:                  # noqa: BLE001 - cleanup must never cost a recorded done
        print(f"loop.py record: releasing {goal}'s checkout failed ({exc}) — the goal is still "
              "recorded done; its work record is intact, so `work.py finish` can retry it.",
              file=sys.stderr)
        return None
    if outcome and outcome != "nothing to finish":
        print(f"loop: {outcome}", file=sys.stderr)
    return outcome


def _warn_knowledge_graph(sdlc_dir):
    """#2704: on every pick, say -- once a day, on stderr -- when `auto_refresh` is on and the graph
    was never built or has fallen behind the corpus. THE HOST-AGNOSTIC TIER: `hooks/session_start.sh`
    prints the same line as additionalContext on Claude Code, but Cursor has no hooks and every host
    reaches `loop.py next`, so this is the surface the warning is allowed to depend on. `kg.py warn`
    owns the rule, the text and the once-a-day stamp (shared with the hook, so a repo gets one
    warning a day, not one per surface); this is only the Rule-3 subprocess hop -- the same shape as
    `_refresh_knowledge_graph` below, down to the fail-open except. stderr ONLY: stdout is the goal
    channel the skill parses. `warn` prints "" when nothing is due, so a repo with the graph off
    (the shipped default) costs one short subprocess and no output.

    HEADLESS WORKERS SKIP IT, like the hook's own tiers: a `claude -p /sigma-loop` worker under
    supervise_daemon.py (SIGMA_RUN_ID set) has nobody reading its stderr, and since the daily
    stamp is shared, letting it print would spend the day's one warning where no person sees it and
    keep the human's own session start silent on exactly the machines that run the loop."""
    try:
        if os.environ.get("SIGMA_RUN_ID"):
            return
        kg_py = _HERE.parent.parent / "sigma-kg" / "scripts" / "kg.py"
        if not kg_py.exists():
            return
        r = subprocess.run([sys.executable, str(kg_py), "warn", str(sdlc_dir)],
                           capture_output=True, text=True, timeout=30)
        for line in (r.stdout or "").strip().splitlines():
            print(f"sigma: {line}", file=sys.stderr)
    except Exception:                               # noqa: BLE001 - a warning must never cost a pick
        pass


#: Outer bound on the whole refresh hop, kept strictly ABOVE `kg.py`'s own `_REFRESH_TIMEOUT` (900s)
#: so the inner cap is what normally fires — that one reports the builder's real error, where this
#: one could only report "timed out". This exists solely so a builder that ignores its own timeout
#: cannot wedge a completed goal forever.
_KG_REFRESH_TIMEOUT = 1200


def _refresh_knowledge_graph(sdlc_dir, goal):
    """#1562: rebuild the knowledge graph when `knowledge_graph.auto_refresh` is true. THE TRIGGER
    THE DOCS HAVE ALWAYS DESCRIBED, finally existing.

    `skills/sigma-kg/SKILL.md`, `README.md` and `skills/sigma-retro/SKILL.md` all said the graph
    rebuilds itself at the end of every Retrospective. Nothing called a builder — `sigma-retro`'s §4
    even pointed at "this skill's own step 3 above" for a call step 3 never made. Measured on this
    repo 2026-09-02: `auto_refresh=True`, a 526-document corpus, `graph: not built`, and not one
    error anywhere to say so. That silence is the defect; a stale graph is only its symptom.

    A SUBPROCESS, NOT AN IMPORT. `kg.py` belongs to the sibling `sigma-kg` skill, and north-star
    Architecture Rule 3 is "skills do not import each other's Python — a skill needing a sibling
    shells out to that sibling's CLI". This is the same shape `_predict_model_tier` above already
    uses for `sigma-model`'s `predict.py`, down to the broad except and the non-fatal stderr line.
    (The checked-in plan for this issue proposed an `importlib` cross-load instead; that would have
    broken Rule 3, which nothing enforces structurally — `tests/test_import_boundary.py` covers only
    the core↔private-side boundary.)

    HOST-AGNOSTIC BY CONSTRUCTION. It is Python on the terminal outcome path that every host reaches,
    not a hook: Cursor has none, and `/sigma-loop` and `/sigma-goal` both end a goal through `record`.

    ONLY STDERR IS FORWARDED, and that is the whole reporting design. `kg.py refresh` sends a
    correctly-gated no-op to stdout and anything else — a real build, or any failure — to stderr, so
    a repo that never turned the graph on stays completely silent while a refresh that DIED is
    visible on the console. Fail-open throughout: this runs after the goal's bookkeeping has already
    landed, so nothing here can cost a recorded outcome."""
    try:
        kg_py = _HERE.parent.parent / "sigma-kg" / "scripts" / "kg.py"
        if not kg_py.exists():                      # a partial install must not break a record
            return
        # #707 (TM-04): `knowledge_graph.builder` is a repository-supplied executable. Anything but
        # the shipped default name needs the same Git-local operator opt-in as `verify.command`;
        # refusing here (not in kg.py, which must not import a sibling skill) keeps the graph a
        # no-op rather than running a committed binary. Lever: the opt-in in shell_policy's message.
        kgc = (state.load_config(sdlc_dir) or {}).get("knowledge_graph")
        if (isinstance(kgc, dict) and kgc.get("enabled") and kgc.get("auto_refresh")
                and kgc.get("builder", "graphify") != "graphify"
                and not shell_policy.repository_shell_commands_allowed(
                    pathlib.Path(sdlc_dir).resolve().parent)):
            print(f"sigma: knowledge-graph refresh skipped for {goal}: custom "
                  f"knowledge_graph.builder; " + shell_policy.refusal_message(), file=sys.stderr)
            return
        r = subprocess.run([sys.executable, str(kg_py), "refresh", str(sdlc_dir)],
                           capture_output=True, text=True, timeout=_KG_REFRESH_TIMEOUT)
        for line in (r.stderr or "").strip().splitlines():
            print(f"sigma: {line}", file=sys.stderr)
    except Exception as exc:                        # noqa: BLE001 - never break a recorded outcome
        print(f"sigma: knowledge-graph refresh skipped for {goal} (non-fatal): {exc}",
              file=sys.stderr)


def _warn_missing_knowledge_note(sdlc_dir, goal):
    """#2701: say so, on stderr, when Retrospective ran but `sigma-retro` §4 wrote no analysis note.

    Plans and research have a pre-push guard; the KG note is written AFTER merge, so its only
    checkpoint is record. A retro that skipped §4 used to leave the corpus (and every peer machine)
    silently missing that goal. Warn, never refuse: the goal is already merged and every piece of its
    bookkeeping has landed by the time this runs.

    Same shape as `_refresh_knowledge_graph` above, for the same reasons: a SUBPROCESS into the
    sibling `kg.py` (Architecture Rule 3 — the filename comes from the `note_id` that writes it, not
    a copy here); the `enabled` gate lives in `kg.py note-check`, which sends "disabled" and
    "present" to stdout and the MISSING line to stderr, so forwarding stderr alone is the whole
    gate; fail-open throughout — a missing `kg.py` is a silent return, and nothing here can raise."""
    try:
        kg_py = _HERE.parent.parent / "sigma-kg" / "scripts" / "kg.py"
        if not kg_py.exists():                      # a partial install must not break a record
            return
        r = subprocess.run([sys.executable, str(kg_py), "note-check", str(sdlc_dir), str(goal)],
                           capture_output=True, text=True, timeout=15)   # a config read + one stat
        for line in (r.stderr or "").strip().splitlines():
            print(f"sigma: {line}", file=sys.stderr)
    except Exception as exc:                        # noqa: BLE001 - never break a recorded outcome
        print(f"sigma: knowledge-note check skipped for {goal} (non-fatal): {exc}",
              file=sys.stderr)


#: #2698: the knowledge-notes hop's two verbs, IN ORDER — publish commits this machine's notes (a
#: rebase needs a clean tree) and its own retry already fetch-rebases; pull then brings in whatever
#: landed meanwhile. A module constant, not a local, ON PURPOSE: `_load("sync")` builds a fresh module
#: per call, so no monkeypatch on any `sync` object can reach the loop's copy — the pull seam has to
#: live on THIS module for a test to remove it (P4 finding B3; `tests/test_kg_sync.py`).
_KNOWLEDGE_SYNC_VERBS = ("publish", "pull")
#: Per-verb wall-clock bound for the hop's subprocess; `knowledge_graph.sync.timeout_seconds` overrides.
_KNOWLEDGE_SYNC_TIMEOUT = 300


def _sync_knowledge_notes(sdlc_dir, goal):
    """#2698: publish this machine's knowledge-graph analysis notes and pull everyone else's, so the
    `kg.py refresh` that follows rebuilds over the UNION. Same shape as `_refresh_knowledge_graph`
    below and for the same reasons: a subprocess (`sync.py <verb> <sdlc> --channel knowledge`), bounded
    by a timeout, `GIT_TERMINAL_PROMPT=0` so a credential prompt can never hang a completed goal,
    fail-open and stderr-only. Runs AFTER every piece of the goal's bookkeeping has landed, so
    nothing here can cost a recorded outcome.

    SILENT when there is nothing to say: no config, sync off, or a quiet outcome (`published`,
    `nothing to publish`, `pulled`). Each verb is guarded separately so the line on stderr names the
    verb that failed. A worktree that was never bootstrapped is named with the one command that
    fixes it — the notes stay local until then, and nothing pushes without the operator asking."""
    try:
        try:
            cfg = ledger._config(sdlc_dir)
        except (OSError, ValueError):
            return
        sync = _load("sync")
        if not sync.knowledge_enabled(cfg):
            return
        if not sync.is_worktree(sdlc_dir, channel=sync.KNOWLEDGE):
            print("sigma: knowledge notes sync is on but .sdlc/knowledge/analysis is not a worktree "
                  "— run `sync.py bootstrap .sdlc --channel knowledge` once in this clone (notes stay "
                  "local until then)", file=sys.stderr)
            return
        raw = sync.knowledge_settings(cfg).get("timeout_seconds")
        timeout = raw if isinstance(raw, (int, float)) and raw > 0 else _KNOWLEDGE_SYNC_TIMEOUT
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        for verb in _KNOWLEDGE_SYNC_VERBS:
            try:
                # Through `run_with_timeout.py`, not a bare `timeout=`: a bare timeout kills only the
                # direct `sync.py` child and leaves a hung `git push`/ssh underneath as an orphan;
                # the wrapper kills the whole process GROUP (the same reason watch_daemon.py's tick
                # uses it). The outer `timeout=` is the belt for the wrapper itself. `stdin` is
                # closed so an ssh host-key prompt — which GIT_TERMINAL_PROMPT does not cover —
                # cannot block on a tty (review round 1).
                r = subprocess.run([sys.executable, str(_HERE / "run_with_timeout.py"), str(timeout),
                                    str(_HERE / "sync.py"), verb, str(sdlc_dir),
                                    "--channel", sync.KNOWLEDGE],
                                   capture_output=True, text=True, timeout=timeout + 30, env=env,
                                   stdin=subprocess.DEVNULL)
                out = (r.stdout or r.stderr or "").strip()
                if r.returncode != 0 or out not in sync.KNOWLEDGE_QUIET_OUTCOMES:
                    print(f"sigma: knowledge notes {verb}: {out or f'exit {r.returncode}'}",
                          file=sys.stderr)
            except Exception as exc:                # noqa: BLE001 - per verb, so the line names it
                print(f"sigma: knowledge notes {verb} skipped for {goal} (non-fatal): {exc}",
                      file=sys.stderr)
    except Exception as exc:                        # noqa: BLE001 - never break a recorded outcome
        print(f"sigma: knowledge notes sync skipped for {goal} (non-fatal): {exc}", file=sys.stderr)


#: #232: the audit line `record review` leaves on the goal's own timeline, once per goal.
_AWAITING_MERGE_NOTE = ("PR #{pr} is open and awaiting a merge. This goal is not done until the PR "
                        "merges, so the issue stays open (board: QC). It is closed automatically once "
                        "the merge is observed.")


def _record_awaiting_merge(sdlc_dir, source, goal, detail="", retro_grade=None):
    """#232: `record review` -- the goal's work is finished and its PR is open, awaiting a merge the
    loop does not perform now (`auto_merge: off`, a fork / read-only repo, `protected` against an
    unguarded base, or an armed auto-merge GitHub has not landed yet). NOT a terminal outcome: done
    means merged, so the issue stays open and `_reconcile_awaiting_merges` records the `done` (and
    closes the issue) once the PR is observed merged.

    What it does, in order, and why each piece:
      - flags the work record `awaiting_merge` (the ONLY thing the reconcile pass reads -- local,
        so the eventual close does not depend on any GitHub write below having landed);
      - `source.await_merge`: removes no label (membership + the claim's `sdlc:in-progress` stay)
        and moves the card to QC -- existing label/board vocabulary, docs/label-model.md; `_next`
        also skips a flagged goal locally (`_awaiting_merge_skip`);
      - ledger `parked` ("left the active queue pending something", the #1394 precedent): ENDS the
        claim lease, so `_auto_reclaim_stale_claims` can never hand a waiting goal back to Ready.
        No `park` EVENT -- the park metrics stay about parks;
      - cursor, action log (`recorded result=review`), agent / phase / session markers: the same
        per-goal bookkeeping a terminal outcome gets, because this session is finished with it.
    It does NOT release the checkout (the `done` recorded at merge time does), close the issue, or
    signal unit completion. Returns "review"."""
    pr, first = work.mark_awaiting_merge(sdlc_dir, goal)
    note = _AWAITING_MERGE_NOTE.format(pr=pr) if first else None
    try:
        if hasattr(source, "await_merge"):
            source.await_merge(goal, note)
        elif hasattr(source, "mark_qc"):
            source.mark_qc(goal)
    except Exception as exc:                      # noqa: BLE001 - the local flag is what matters
        print(f"loop.py record: marking {goal} awaiting merge on the source failed ({exc}) — the "
              "local record is kept; the merge-reconcile pass still closes it", file=sys.stderr)
    state.advance_cursor(sdlc_dir, f"last: {pathlib.Path(goal).name} -> review")
    why = detail or f"awaiting merge of PR #{pr}"
    ledger.safe_append(sdlc_dir, "parked", goal, why=why, run_id=state.run_identity())
    if retro_grade is not None:
        ledger.safe_append(sdlc_dir, "retro", goal, stream=ledger.EVENTS, grade=retro_grade)
    actionlog.safe_append(sdlc_dir, goal, "recorded", result="review", detail=why)
    agent_end(sdlc_dir, goal)
    _phase_marker_end(sdlc_dir, goal)
    _session_release(sdlc_dir, goal)
    if retro_grade is not None:
        _warn_missing_knowledge_note(sdlc_dir, goal)
        _sync_knowledge_notes(sdlc_dir, goal)
        _refresh_knowledge_graph(sdlc_dir, goal)
    return "review"


def _awaiting_merge_skip(sdlc_dir):
    """#232: every goal `record review` left waiting on a merge, as refs AND stems, for `_next`'s
    skip set. Its issue keeps the claim's `sdlc:in-progress` overlay, but that label is not
    re-asserted (single-writer rule), so a label lost in between must not make a finished goal
    pickable again on this machine. Local read only; fail-open to an empty set."""
    try:
        goals = work.awaiting_merge_goals(sdlc_dir)
    except Exception:                              # noqa: BLE001 - a skip hint never costs a pick
        return set()
    return set(goals) | {work.stem(g) for g in goals}


#: #255 (7): consecutive failed closes of a MERGED goal's issue before the pass parks it in public.
#: Below it a failure is retried quietly (next pass, >= MERGE_RECHECK_SECONDS later); the park lands
#: exactly once, on this attempt, and the retries continue after it -- the PR did merge, so the goal
#: still closes by itself the moment the source recovers.
MERGE_CLOSE_ATTEMPTS = 3

#: #255 (3): the share of the watch tick's per-call bound (`SIGMA_WATCH_CALL_TIMEOUT`, default 120 s,
#: enforced by `run_with_timeout.py`) one pass may spend STARTING goals. Half, so the goal in hand
#: when the budget runs out still has the other half to finish its close and release before the
#: tick's own kill; a goal not reached is read on the next pass (latency, never loss).
MERGE_PASS_BUDGET_SHARE = 0.5
_MERGE_PASS_TIMEOUT_DEFAULT = 120.0

#: #255 (2): the pass's mutual-exclusion lock file. See `_merge_reconcile_lock`.
MERGE_RECONCILE_LOCK = "merge-reconcile.lock"


def _merge_reconcile_budget():
    """Seconds one pass may keep starting goals: MERGE_PASS_BUDGET_SHARE of the tick's call bound.
    DERIVED from the same env knob `watch_daemon.py` kills by, never a second constant."""
    try:
        value = float(str(legacy.getenv("SIGMA_WATCH_CALL_TIMEOUT") or "").strip() or
                      _MERGE_PASS_TIMEOUT_DEFAULT)
    except ValueError:
        value = _MERGE_PASS_TIMEOUT_DEFAULT
    return (value if value > 0 else _MERGE_PASS_TIMEOUT_DEFAULT) * MERGE_PASS_BUDGET_SHARE


@contextlib.contextmanager
def _merge_reconcile_lock(sdlc_dir):
    """#255 (2): serialize the merge-reconcile pass across processes -- a `next` and the watch tick
    racing on one merged goal used to BOTH record `done` (two ledger entries, two close comments,
    two unit-completion signals). Yields True (held), False (another pass holds it -- skip; that
    pass is doing this work) or None (locking is unavailable -- the caller REFUSES the pass
    loudly rather than run unguarded, AGENTS.md SAFETY).

    NON-BLOCKING, so there is no timeout to derive and no waiter to wedge: the loser skips and its
    goals are read by the holder or by the next pass. A KERNEL lock (`fcntl.flock` on POSIX,
    `msvcrt.locking` on Windows -- `state.phase_end_lock`'s pair), so it cannot go stale: it dies
    with its holder, including the SIGKILL `run_with_timeout.py` sends an overrunning tick. The file
    itself is never read for state; the holder's pid is written into it only for diagnosis.
    THE POLITE LEVER for a pass that seems wedged: the busy line names the holder pid -- stop THAT
    process (the watch tick's own timeout already does, within SIGMA_WATCH_CALL_TIMEOUT). Never
    delete the lock file: a new inode would admit a second pass beside the live holder."""
    path = pathlib.Path(sdlc_dir) / "state" / MERGE_RECONCILE_LOCK
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        print(f"loop.py: merge lock REFUSED -- cannot open {path}: {exc}", file=sys.stderr)
        yield None
        return
    held = False
    try:
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif os.name == "nt":
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                yield None
                return
            held = True
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                yield False
            else:
                print(f"loop.py: merge lock REFUSED -- {path}: {exc}; "
                      "use a filesystem that supports exclusive locks", file=sys.stderr)
                yield None
            return
        try:
            # Windows locks byte zero; keep that byte and store diagnostics after it.
            offset = 0 if fcntl is not None else 1
            os.lseek(fd, offset, os.SEEK_SET)
            os.write(fd, str(os.getpid()).encode())
            os.ftruncate(fd, os.lseek(fd, 0, os.SEEK_CUR))
        except OSError:
            pass
        yield True
    finally:
        if held:
            try:
                # Clear diagnostics while still owning the lock, never after the next owner.
                os.ftruncate(fd, 0 if fcntl is not None else 1)
            except OSError:
                pass
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                else:
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        os.close(fd)


def _merge_lock_holder(sdlc_dir):
    try:
        with (pathlib.Path(sdlc_dir) / "state" / MERGE_RECONCILE_LOCK).open() as handle:
            if fcntl is None and os.name == "nt":
                handle.seek(1)  # byte zero is mandatory-locked on Windows
            return handle.read().strip() or "?"
    except OSError:
        return "?"


def _reconcile_awaiting_merges(sdlc_dir, config, source=None, run=None, now=None,
                               min_interval=None, limit=None, clock=None):
    """#232: finish the goals `record review` left waiting -- the "issue closes when the PR merges"
    half of done-means-merged. -> [(goal, outcome, pr)] for each goal this pass RECORDED.

    For each work record flagged `awaiting_merge` (oldest-checked first, at most
    `work.MERGE_RECONCILE_MAX_PER_PASS`, skipping any re-read within `min_interval` seconds):
    one REST read of its PR (`work.pr_landing_state`), then
      merged          -> `work.done_refusal` given THAT read (replays the merge receipt: the
                         existing merge-observation adapter) and `_record(done, merged_pr=True)`,
                         which closes the issue, strips the lifecycle labels, moves the card to Done
                         and releases the checkout -- the same chokepoint a `record done` passes;
      closed unmerged -> `_record(parked, "PR #N was closed without merging")` -- a human closed
                         it, so a human decides;
      open / unknown  -> stamp `checked_at` and leave it.

    ONE PASS AT A TIME (#255 (2)): the whole pass runs under `_merge_reconcile_lock`; a pass that
    finds it held skips (stderr names the holder pid) -- the holder is doing this work.

    CRASH-SAFE (#255 (3)): `_record` clears the flag itself, immediately after its ledger terminal
    entry and BEFORE the slow tail (unit-completion signal, checkout release), so a pass killed in
    that tail by the watch tick's timeout never records `done` a second time. A kill before the
    record (during the close) leaves the flag, and the next pass retries; `complete()` is safe on an
    already-closed issue. And the pass is BOUNDED in time: no goal is started once
    `_merge_reconcile_budget()` (half of SIGMA_WATCH_CALL_TIMEOUT) is spent.

    QUIET RETRY (#255 (7)): a raising `complete()` records nothing and parks nothing on the first
    MERGE_CLOSE_ATTEMPTS - 1 failures (counted on the flag); the MERGE_CLOSE_ATTEMPTS-th parks in
    public, once. A `done` downgraded that way keeps the flag, so the close is still retried.

    COST (#255 (1), measured by tests/test_merge_reconcile.py): zero `gh` calls when nothing is
    flagged (one directory listing). Otherwise one REST `pulls/<n>` GET per goal read, at most
    `limit` per pass whatever the backlog, and no `gh pr view` (GraphQL) at all: the confirming read
    is reused and `finish` is told the merge is confirmed. A goal that closes adds one more REST read
    only when the ledger or journal is on (the merge facts), plus the issue GraphQL reads and writes
    `source.complete()` makes for any `record done`. A larger backlog costs close LATENCY (every PR
    re-read within ceil(N/limit) passes), never more calls per pass.

    Only on `work.enabled` (no PR is ever opened otherwise). FAIL-OPEN per goal and overall: a sweep
    must never cost the caller its pick or its tick."""
    results = []
    try:
        if not work.enabled(config):
            return results
        limit = work.MERGE_RECONCILE_MAX_PER_PASS if limit is None else limit
        interval = work.MERGE_RECHECK_SECONDS if min_interval is None else min_interval
        goals = work.awaiting_merge_goals(sdlc_dir, now=now, min_interval=interval)[:limit]
        if not goals:
            return results
        with _merge_reconcile_lock(sdlc_dir) as held:
            if held is None:
                print("loop.py: merge reconcile REFUSED -- exclusive file locking is unavailable; "
                      "an unguarded pass can record a merged goal done twice", file=sys.stderr)
                return results
            if not held:
                print(f"loop.py: another merge-reconcile pass or record done is running (pid "
                      f"{_merge_lock_holder(sdlc_dir)}) -- skipping; it reads these goals",
                      file=sys.stderr)
                return results
            clock = clock or time.monotonic
            started, budget = clock(), _merge_reconcile_budget()
            # Re-listed under the lock: a pass that held it a moment ago may have recorded some.
            still = set(work.awaiting_merge_goals(sdlc_dir))
            goals = [g for g in goals if g in still]
            source = source if source is not None else sources.get_source(sdlc_dir, config)
            for goal in goals:
                if clock() - started >= budget:
                    print(f"loop.py: merge reconcile stopped at its {budget:.0f}s budget -- the "
                          "rest are read next pass", file=sys.stderr)
                    break
                try:
                    _reconcile_one(sdlc_dir, config, source, goal, run, now, results)
                except Exception as exc:          # noqa: BLE001 - one goal never costs the rest
                    print(f"loop.py: merge reconcile skipped {goal} (non-fatal): {exc}",
                          file=sys.stderr)
    except Exception as exc:                      # noqa: BLE001 - fail-open; see docstring
        print(f"loop.py: merge reconcile failed non-fatally ({exc}) — continuing", file=sys.stderr)
    return results


def _reconcile_one(sdlc_dir, config, source, goal, run, now, results):
    rec = work._record(sdlc_dir, goal) or {}
    flag = rec.get("awaiting_merge")
    if not isinstance(flag, dict):
        return                                   # recorded by an earlier pass meanwhile
    pr = str(rec.get("pr") or "")
    landing = work.pr_landing_state(sdlc_dir, rec, run)
    work.stamp_merge_check(sdlc_dir, goal, now=now, successful=landing[0] != work.UNKNOWN)
    if landing[0] == work.MERGED:
        if work.done_refusal(sdlc_dir, config, goal, run=run, landing=landing):
            return                               # not confirmed after all -- next pass
        failures = int(flag.get("close_failures") or 0)
        outcome = _record(sdlc_dir, source, goal, "done",
                          f"PR #{pr} merged (observed by the merge-reconcile pass)",
                          merged_pr=True, housekeeping=False,
                          retry_close=failures + 1 != MERGE_CLOSE_ATTEMPTS)
        if outcome == "retry":
            n = work.note_close_failure(sdlc_dir, goal)
            print(f"loop.py: closing {goal} failed ({n} of {MERGE_CLOSE_ATTEMPTS} before it is "
                  "parked) -- retried quietly next pass", file=sys.stderr)
            return
        if outcome != "done":
            work.note_close_failure(sdlc_dir, goal)
    elif landing[0] == work.CLOSED_PR:
        outcome = _record(sdlc_dir, source, goal, "parked", f"PR #{pr} was closed without merging")
    else:
        return
    results.append((goal, outcome, pr))


def _record(sdlc_dir, source, goal, result, detail="", retro_grade=None, transition="park",
            merged_pr=False, retry_close=False, housekeeping=True, qkind=None):
    """`qkind` (#994): a declared question kind; appends one machine line to the text handed to
    `source.park` (and only there). None keeps the comment byte-identical.
    `housekeeping` (#465): False only from the merge-reconcile pass, which holds
    `merge-reconcile.lock`, so the worktree sweep is not run under it. NOT `merged_pr`: every ordinary
    `record done` on a PR-bearing goal sets that too.
    `merged_pr` (#255 (1)): the caller confirmed the goal's PR merged by a REST read just now;
    forwarded to the checkout release so it does not ask GitHub again. `retry_close` (#255 (7)):
    a raising `complete()` returns "retry" having written NOTHING (no park, no ledger, no cursor) --
    only the merge-reconcile pass passes it, and it counts the failure and retries."""
    if result == "review":
        return _record_awaiting_merge(sdlc_dir, source, goal, detail, retro_grade)
    requested = result
    if result == "done":
        try:
            source.complete(goal)
        except Exception as exc:
            if retry_close:
                print(f"loop.py record: source.complete() failed for {goal} ({exc}) — nothing "
                      "recorded; the merge-reconcile pass retries it", file=sys.stderr)
                return "retry"
            # #1201: `source.complete()` raising here (the reported case: `gh issue close` failing
            # on an exhausted GitHub GraphQL quota) used to propagate straight out of `_record` —
            # this was the CLI `record` verb's ONLY call to `_record`, with no try/except anywhere
            # above it (unlike `run_loop`, whose own #335 outer handler already downgrades this to a
            # park). That skipped EVERY local write below (cursor advance, the ledger `done` entry,
            # the action-log `recorded` line): the goal shipped, but nothing local ever recorded it,
            # and the issue stayed open + goal-labelled, so a later run silently re-served it.
            # Downgrading here — the one chokepoint every terminal outcome already passes through —
            # fixes both the CLI path AND is what `run_loop` itself now relies on (see its own
            # comment): the "complete() raised -> record a park instead" behavior lives in exactly
            # one place, not reimplemented a second time per caller. Never a silent swallow: named
            # here (which remote op failed) rather than surfacing as an uncaught traceback.
            print(f"loop.py record: source.complete() failed for {goal} ({exc}) — "
                  "recording as parked instead of losing the terminal record entirely", file=sys.stderr)
            result = "parked"
            detail = f"source error recording 'done' ({exc})"
    reason_class = None
    tier = None
    if result == "failed" and hasattr(source, "fail"):
        source.fail(goal, detail or result)      # hasattr: a source without fail() parks instead
    elif result != "done" and transition == "blocked" and hasattr(source, "mark_blocked"):
        # #1394: the goal is blocked on MACHINE-resolvable work, not on a person. `mark_blocked`
        # keeps membership and adds the `sdlc:blocked` overlay, so the goal stays visible to every
        # sweep and is resumed automatically when its blockers close -- where `park()` would drop
        # membership and hand it to a human, which is the wrong state for a block the loop is
        # already resolving.
        #
        # The LEDGER outcome stays `parked`, deliberately. That kind means "left the active queue
        # pending something", which is true either way, and the distinction that actually
        # matters -- membership, and whether anything will resume it -- lives in the LABELS. Adding
        # a `blocked` kind would touch `ledger.KINDS`, `SHARED_KINDS` and the pinned
        # `contract/vocabulary.json` for no behavioural gain. `reason_class` already carries
        # `dependency`, which is the honest machine why.
        reason_class = _reason_class(detail)
        source.mark_blocked(goal)
        if detail and hasattr(source, "note"):
            try:
                source.note(goal, "Blocked — " + detail)
            except Exception:                     # noqa: BLE001 - audit trail is best-effort
                pass
    elif result != "done":                        # parked (or failed on a fail-less source), OR
                                                   # downgraded from "done" by the try/except above
        reason_class = _reason_class(detail)
        # #953/#1185/#1240: decision_tier.resolve() is called ONLY for a reason_class in
        # _DECISION_TIER_REASON_CLASSES (see that constant's own comment for exactly which, and
        # why) -- every other reason_class never touches it at all (no cycles spent, no
        # behavior-change risk on a path this feature isn't about). resolve() itself stays inert
        # (returns None) unless the repo has opted in via config.json's `decision_tier: "auto"`
        # key (#952) -- widening WHICH details get offered to it never changes whether it's on.
        #
        # Independent review of #1185 (then #1240) caught that "unknown" is not a pure free-text
        # bucket: work.py's own fixed, machine-generated park/gate-verdict messages land there too,
        # reached the same way genuine free text is (no `_REASON_CLASS_RULES` needle happens to
        # cover their wording). `_mechanical_unknown_detail` tells the two apart so those messages
        # stay plain, tier-less parks exactly like they were before #1185 widened the gate -- never
        # a fabricated "this needs human judgment" tier on an ordinary infra hiccup.
        mechanical_unknown = _mechanical_unknown_detail(detail, reason_class)
        decision_tier_eligible = reason_class in _DECISION_TIER_REASON_CLASSES and not mechanical_unknown
        tier = decision_tier.resolve(detail, sdlc_dir) if decision_tier_eligible else None
        # `tier` is passed as a NEW keyword only when there is one to report AND the source's own
        # `park()` actually accepts one -- review of #953 found that checking `tier is not None`
        # alone still crashes a pre-#953 source/test-double (old 2-positional-arg `park(goal,
        # reason)`) with a TypeError once decision_tier resolves a real tier, e.g. via the
        # `result == "failed"` branch above falling through here for a source with no `fail()`.
        # `inspect.signature` mirrors the same capability-check spirit as `hasattr(source, "fail")`
        # one line up -- ask what the source can actually do before calling it that way, the same
        # discipline this function already applies to `fail()`.
        park_text = detail or result
        if qkind is not None:                      # #994: the declared kind rides inside the text
            park_text = park_text + "\n" + _load("qkind").render_line(qkind)
        if tier is not None and "tier" in inspect.signature(source.park).parameters:
            source.park(goal, park_text, tier=tier)
        else:
            source.park(goal, park_text)
    # Single atomic patch (#531) -- the old load_cursor-then-save_cursor pair spanned two calls,
    # so two concurrent _record()s could each read the same pre-increment cursor and lose one.
    state.advance_cursor(sdlc_dir, f"last: {pathlib.Path(goal).name} -> {result}")
    # The outcome, once, on the single chokepoint both the CLI and run_loop paths pass through.
    outcome = result if result in ("done", "failed") else "parked"
    # #1121: same run_id attribution as the claim above -- lets `ledger._held()` tell THIS
    # process's own terminal entry apart from a stale one written by a different concurrent
    # process of the same actor.
    ledger.safe_append(sdlc_dir, outcome, goal, why=detail or None, run_id=state.run_identity())
    # #255 (3)/(5): the `awaiting_merge` flag ends HERE, right after the terminal ledger entry and
    # before the slow tail below (unit-completion signal, checkout release, KG hops) -- so a pass
    # killed in that tail by the watch tick's timeout never finds the flag and records `done` again.
    # Cleared on a real `done`, and on a REQUESTED `parked`/`failed`: that is a human's (or the
    # loop's own) decision about the goal, and a later merge must not record `done` over it. A
    # `done` that the #1201 handler above downgraded to `parked` KEEPS the flag -- the PR merged and
    # only the close failed, so the pass retries it.
    if outcome == "done" or requested in ("parked", "failed"):
        try:
            work.clear_awaiting_merge(sdlc_dir, goal)
        except Exception as exc:                  # noqa: BLE001 - the record already landed
            print(f"loop.py record: clearing {goal}'s awaiting-merge flag failed ({exc})",
                  file=sys.stderr)
    if outcome == "parked":
        ledger.safe_append(sdlc_dir, "park", goal, stream=ledger.EVENTS,
                           reason_class=reason_class, why=detail or None, decision_tier=tier)
    # issue #1013: a retro grade is an agent's own judgment (achieved/partial/diverged), never
    # computed here -- emitted only when the caller actually supplies one (the CLI `record` verb's
    # --retro-grade flag, validated against ledger.RETRO_GRADES before this is ever reached).
    # Absence is the honest default: no grade passed -> no fabricated event, byte-identical to
    # this function's behavior before this parameter existed. Not gated on `outcome` -- Retrospective
    # (SKILL.md step 5 in sigma-loop / step 3 in sigma-goal) always runs before Record for ANY
    # terminal outcome that reaches this call, so a goal that completes retro and then parks (e.g.
    # a merge conflict discovered after Retrospective already ran) can still carry a real grade.
    if retro_grade is not None:
        ledger.safe_append(sdlc_dir, "retro", goal, stream=ledger.EVENTS, grade=retro_grade)
    # One call regardless of outcome — the local action-log counterpart of the ledger calls above.
    actionlog.safe_append(sdlc_dir, goal, "recorded", result=outcome, detail=(detail or None))
    # Review text is durable evidence, but a copied review checkout is reproducible growth.  The
    # successful path reaches work.finish below; a failed terminal record intentionally keeps its
    # goal worktree for repair, so both paths meet at this narrow, fail-open evidence-only cleanup.
    if outcome in ("done", "failed"):
        try:
            work.prune_terminal_review_copies(sdlc_dir, goal)
        except Exception as exc:              # noqa: BLE001 - terminal bookkeeping is already durable
            print(f"loop.py record: review-copy cleanup skipped for {goal!r} ({exc})", file=sys.stderr)
    # #457: the same terminal moment sweeps OTHER closed goals' old action-log and witness streams
    # (bounded, fail-open; `retention.py` states exactly what it will and will not delete).
    if outcome == "done":
        try:
            retention.prune_closed_goal_streams(sdlc_dir)
        except Exception as exc:              # noqa: BLE001 - retention must never cost a terminal record
            print(f"loop.py record: stream retention skipped ({exc})", file=sys.stderr)
    # A goal's terminal outcome, however it ended, always clears every thread's death-watch
    # marker — a cleanly-finished goal must never linger in agent_watch's candidate set.
    agent_end(sdlc_dir, goal)
    # #2658: the same rule, one file over. `phase_report.py` stamps `.sdlc/state/phase/<goal>.json`
    # at every `start` and NOTHING ever unlinked it — observed 2026-09-24: 63 markers, the oldest 30
    # days old, 57 of them on closed issues. Pruned HERE, and only here, for the reason `agent_end`
    # is called here: `_record` is the one chokepoint every terminal outcome passes through, it is
    # per-goal, and a goal that reaches it is genuinely over.
    #
    # Deliberately NOT in `_release` — see that function's #1391 note: a release is a RECLAIM, not
    # a terminal outcome, and wiping a marker on one destroys a live worker's state. Deliberately
    # NOT in `phase_report.py end` either: that command stays re-runnable against its own marker
    # (its `interval_ms` comment says so outright, and the Codex ceiling path relies on retrying
    # the same marker), so consuming it there would break a documented recovery.
    #
    # A goal that crashes before ever reaching `_record` still leaks its one marker file. That is
    # accepted, not overlooked: a leaked marker is NOT inert -- `end` drops a DIFFERENT-phase
    # marker outright (#2658) and keeps a SAME-phase marker's start time only when its identity
    # (`--pid`, Codex thread, Claude session) matches the caller's (#2667); `stale_marker_reason`
    # names what still gets through. An age-based sweep here would be a third mechanism with an
    # age bound to derive, on top of the two #2667 already added (pid liveness, then the lease).
    _phase_marker_end(sdlc_dir, goal)
    # #1199: same reasoning, one level up — a finished goal must never linger in the SESSION
    # registry's own in-flight lists either, or it would be skipped forever by every future _next().
    _session_release(sdlc_dir, goal)
    # #1478: LAST, and only on a real `done` — after every piece of this goal's own bookkeeping has
    # already landed, so the signal is structurally incapable of costing any of it. `outcome`, not
    # `result`: a `done` the try/except above downgraded to `parked` did NOT close the issue, so
    # there is nothing new for a unit's completion to be measured from. Its return value is ignored
    # on purpose — this surfaces, and a human decides.
    if outcome == "done":
        _signal_unit_completion(sdlc_dir, goal)
        _release_checkout(sdlc_dir, goal, merged=merged_pr)
        # After `finish` has unlinked the work record (the gate's only way back to this evidence), so
        # a record that survived a refused finish keeps its evidence; the next `done` sweeps it.
        try:
            work.prune_terminal_review_generations(sdlc_dir, goal, goal_done=True)
        except Exception as exc:              # noqa: BLE001 - terminal bookkeeping is already durable
            print(f"loop.py record: review-evidence cleanup skipped for {goal!r} ({exc})", file=sys.stderr)
    # #1562: LAST, for the reason #1478 gives one block up -- every piece of this goal's own
    # bookkeeping has already landed, so a builder that hangs, fails or is missing is structurally
    # incapable of costing any of it.
    #
    # GATED ON `retro_grade`, NOT ON `outcome`. The docs place this "at the end of the Retrospective
    # phase", and `retro_grade is not None` is exactly the existing signal that Retrospective RAN --
    # both SKILL.md files say to omit the flag entirely on a goal that never reached it. So a goal
    # that finished its retro and then parked still refreshes (its corpus note was already written,
    # by `sigma-retro` §4, before this call), and a pre-work park refreshes nothing. Gating on
    # `outcome == "done"` instead would get both of those backwards.
    #
    # #2698: the notes hop runs FIRST, under the same gate, so the rebuild below reads the union of
    # every machine's notes rather than this one's alone. Order is the whole point of the pairing.
    if retro_grade is not None:
        _warn_missing_knowledge_note(sdlc_dir, goal)    # #2701: before the (slow) refresh
        _sync_knowledge_notes(sdlc_dir, goal)
        _refresh_knowledge_graph(sdlc_dir, goal)
    # #1201: the ACTUAL outcome recorded, which a caller must use for its own bookkeeping instead
    # of whatever `result` it originally passed in — a "done" call that got downgraded to "parked"
    # above returns "parked" here, not the stale request. `run_loop` relies on this (see its own
    # comment) so its per-run done/parked/failed counters reflect what was truly recorded rather
    # than re-deriving the same "did complete() raise?" decision a second, independent way.
    # #458: LAST, and only on a real `done`: a bounded sweep of EARLIER terminal goals' per-goal
    # state records. It never touches this goal (inside the 7-day window), whose verify evidence is
    # read after `done` by the onboarding control and the contract's downstream ingester. Fail-open.
    if outcome == "done":
        try:
            _load("goal_state_prune").after_done(sdlc_dir, agent_alive=agent_alive)
        except Exception as exc:              # noqa: BLE001 - terminal bookkeeping is already durable
            print(f"loop.py record: per-goal state cleanup skipped for {goal!r} ({exc})", file=sys.stderr)
        try:                                  # #464: stale claim locks and old claim markers
            _load("liveness_prune").after_start_or_done(sdlc_dir)
        except Exception as exc:              # noqa: BLE001 - terminal bookkeeping is already durable
            print(f"loop.py record: claim marker sweep skipped for {goal!r} ({exc})", file=sys.stderr)
        if housekeeping:
            try:                              # #465: opt-in sweep of provably finished goal worktrees
                _load("worktree_prune").after_start_or_done(sdlc_dir, exclude=(str(goal),))
            except Exception as exc:          # noqa: BLE001 - terminal bookkeeping is already durable
                print(f"loop.py record: worktree sweep skipped for {goal!r} ({exc})", file=sys.stderr)
    return outcome


def _release(sdlc_dir, source, goal, reason="", claim_actor=None, claim_run=None, note=None):
    """#841: undo a stale claim. `next`/`next-batch` claim a goal via `mark_in_progress` as part of
    picking it — `sdlc:in-progress` added (GitHub) or `status: in_progress` written (local). If the
    caller then never calls `record` on that SAME goal — next-batch picked more than got dispatched
    this run, or a later pick was `--skip`ped after an earlier one already claimed it — nothing
    ever undoes that claim: the label/status sticks forever with no sanctioned way to clear it, so
    an operator had to hand-run `gh issue edit --remove-label` plus write their own comment
    (observed live on #813/#815/#817/#821). This is the sanctioned replacement: `source.release`
    does the actual undo (the label/board-status reset + audit comment, or the local status flip +
    journey note), then this records the SAME `release` ledger kind `_held()` now treats as ending
    an open claim (`ledger.KINDS` declared it from the start, but nothing ever wrote it before this
    verb existed), mirrors it to the local action log, and clears any agent-death-watch marker for
    this goal — the same bookkeeping trio `_record()` runs for a real terminal outcome.

    Deliberately NEVER touches `state.advance_cursor`: unlike `_record()`, a release is not a
    completed iteration (the goal was never actually worked), so it must not consume a slot of
    `budget.max_iterations` — the goal remains exactly as pending as it was before it was ever
    picked, and a fresh `next`/`next-batch` call in the SAME run can pick it right back up.

    POST-REVIEW FIX (PR #1107, Finding 3): `source.release()` now returns True/False (did it
    actually release a claim, or was the goal/issue already done/parked/failed — a clean no-op).
    Returned here unchanged so the CLI layer can print a clear, non-fatal message instead of
    claiming a release happened when it did not. Additive only: no caller predating this read the
    return value, and the ledger/action-log/agent_end bookkeeping below stays UNCONDITIONAL exactly
    as before a no-op release is harmless to record — `ledger._held()`'s own actor-scoped claim view
    (Finding 2's fix) only ever ends a lease still attributed to the SAME actor, so a stray
    `release` entry for a goal with no open claim (or one held by someone else) is a no-op there
    too, never a false "un-claim". `agent_end` is always safe to call regardless (see its own
    docstring: "Safe no-op if nothing to clear").

    `claim_actor` (PR #1235 review, Finding 1/3): OPTIONAL, and ONLY ever passed by
    `_auto_reclaim_stale_claims` — the actor the LEDGER itself recorded as holding the claim being
    reclaimed (from `ledger.expired_claims()`'s own return value), never operator/CLI-typed. The
    CLI `release` verb (this function's other caller, below) never passes it, so a human-run
    `release` writes byte-identical to before this fix. Threaded through to
    `ledger.safe_append(..., reclaimed_actor=...)` so the release entry's OWN `actor` field stays
    an honest, self-attributed record of who actually performed the reclaim (this process), while
    still letting `ledger._held()` durably end the ORIGINAL claimant's lease — see that function's
    own docstring for why the plain actor-scoped check alone could never do this for a genuinely
    cross-actor reclaim, and why naming the target this way does not reopen the #1107 hazard.

    `claim_run` (PR #1269 review, blocking finding 1): the same pattern, one axis finer — OPTIONAL,
    ONLY ever passed by `_auto_reclaim_stale_claims`, sourced from `ledger.expired_claim_run_ids()`'s
    own return value (the STALE claim's own `run_id`, snapshotted at the same moment as
    `claim_actor`, never re-derived after). Threaded through to `reclaimed_run` so `ledger._held()`
    can tell the claim actually being reclaimed apart from the SAME actor's own live re-claim by a
    DIFFERENT, concurrent process — see `ledger._held()`'s own docstring for the exact race this
    closes. `None` for the CLI `release` verb, exactly like `claim_actor` — a human-run release
    writes byte-identical to before this fix."""
    # #2009: `note` re-frames the audit comment for a caller whose situation the default wording
    # describes wrongly (a stale-resume release is on a goal that WAS started). Forwarded only when
    # given, so every existing caller -- and the seven `release(self, goal, reason)` test doubles --
    # keep being called with exactly the two arguments they declare.
    released = source.release(goal, reason, note=note) if note else source.release(goal, reason)
    # #1121: run_id is THIS call's own run (the writing process), same as `actor` always is --
    # for a human/CLI release that's simply this process's own attribution; for an automated
    # `_auto_reclaim_stale_claims` sweep it is the SWEEP's run, not the original claim's -- that
    # distinction is exactly why `reclaimed_run` (PR #1269 review fix) is a SEPARATE field, sourced
    # from `claim_run` above, never from this `run_id` -- see `ledger._held()`'s own docstring.
    ledger.safe_append(sdlc_dir, "release", goal, why=reason or None, reclaimed_actor=claim_actor,
                        reclaimed_run=claim_run, run_id=state.run_identity())
    actionlog.safe_append(sdlc_dir, goal, "released", reason=(reason or None))
    # #1391: `agent_end(sdlc_dir, goal)` USED TO RUN HERE, and it was the load-bearing step in a
    # real data-loss chain. A release is a RECLAIM ("this claim looked abandoned, put the goal
    # back"), not a terminal outcome — unlike `_record`, which legitimately clears the marker
    # because the goal genuinely finished. If a release is WRONG about abandonment, wiping the
    # marker destroys the only evidence that would have stopped the next picker:
    #   _release closes the claim AND wipes the marker -> a later pick calls work.start(goal) ->
    #   the worktree path is deterministic (`<worktree_dir>/<stem>`) so the record and directory
    #   both still exist -> `_resume_blocked_by_a_live_sibling` refuses ONLY on an open ledger
    #   claim, which this release just closed -> "already started" -> the new agent attaches to the
    #   LIVE agent's directory, and `work.commit()` runs `git add -A` in it.
    # Leaving the marker in place is safe now that `agent_heartbeat` exists: a genuinely abandoned
    # goal's marker goes stale on its own (dead pid, or mtime past the lease TTL) and stops
    # blocking anything, while a live worker keeps its protection. Removing the call is the fix;
    # nothing replaces it here.
    if released is not False:
        _session_release(sdlc_dir, goal, settle=False)  # an undispatched release refunds its slot
    return released


def precheck(sdlc_dir, goal, config, source, run=None, now=None):
    """Pre-work backlog cross-check hook (OPT-IN via `backlog_check.enabled is True`). Before a picked
    goal spends a token: refresh the board mirror (TTL-guarded; a no-op in local / non-github mode),
    cross-check the pick, and per `backlog_check.action` (default 'park') either PARK-with-proof a
    confident duplicate/obsolete/blocked goal — a comment carrying the evidence, then the loop advances
    to the next goal — or annotate a weak match and proceed. The engine spends ZERO LLM tokens.

    Returns a one-line result the SKILL reads by its first word: 'OFF' (disabled) | 'PARKED <reason>'
    (do not research it; take the next goal) | 'PROCEED[ (advisory)]' (carry on). FAIL-OPEN: any error
    returns 'PROCEED' — the cross-check must never block or crash the loop. Note: a PARKED goal goes
    through _record, so it advances the per-run iteration cursor like any other outcome."""
    try:
        if (config.get("backlog_check") or {}).get("enabled") is not True:
            return "OFF"                                                 # gate inside the guard: a
        bc = _load("backlog_check")                                      # malformed config -> PROCEED
        _load("mirror").fetch_and_write(sdlc_dir, config=config, run=run, now=now)
        pack = bc.cross_check(sdlc_dir, goal, config=config, run=run, now=now)
        decision = bc.decide(pack, config)
        if decision["action"] == "park":
            # #1393: a park on a BLOCKED-BY finding is not the end of the story -- it is the point
            # where the blocker either gets resolved or the goal is knowingly skipped. Try to
            # resolve first: grant membership to a blocker nothing can pick, route one that belongs
            # to someone else, and only then park with a reason that names what actually survived.
            #
            # Blockers named by a HUMAN (a "Blocked by #N" typed into an issue body) reach the loop
            # ONLY here -- `handoff.create_tracked_issue` covers the ones Sigma files itself.
            # That is why both hooks exist: this one is opt-in with `backlog_check.enabled`, the
            # other is always on, and neither alone is enough.
            reason, transition = _resolve_blockers_for_park(sdlc_dir, config, source, goal, pack,
                                                            decision["reason"], run)
            # #1394: a block whose every named blocker is now workable is NOT a park. Parking it
            # would drop membership and hand a machine-resolvable wait to a human -- the exact
            # confusion between "waiting to be picked" and "waiting for a person" this release
            # exists to remove. `transition="blocked"` keeps membership and the auto-resume.
            _record(sdlc_dir, source, goal, "parked", reason, transition=transition)
            return ("BLOCKED " if transition == "blocked" else "PARKED ") + reason
        if decision["note"]:
            try:
                source.note(goal, decision["note"])
            except Exception:
                pass
        return "PROCEED" + (" (advisory)" if decision["note"] else "")
    except Exception as e:
        print(f"loop.py precheck: non-fatal ({e}) — proceeding", file=sys.stderr)
        return "PROCEED"


def _resolve_blockers_for_park(sdlc_dir, config, source, goal, pack, reason, run):
    """Resolve the blockers a `blocked-by` finding names, and return the park reason to record.

    Returns `(reason, transition)` where `transition` is `"blocked"` only when EVERY named blocker
    is now workable — the goal is then waiting on the loop, not on a person, and keeps membership.
    Anything else is `"park"`. `reason` is UNCHANGED whenever there is nothing to say -- no blocked-by findings, a source
    that cannot act, or any failure at all. This runs on the park path, so it must never be the
    thing that stops a park from happening: every failure degrades to today's behaviour.

    When blockers WERE acted on, the returned reason says so, which is the point. "blocked" alone
    leaves a human to work out from scratch what is blocking and whether anything is moving; this
    names each blocker and what happened to it -- promoted, routed to its owner, or surviving and
    why. A goal whose blockers were ALL resolved is still parked (it genuinely cannot proceed yet),
    but it is now parked behind work that is actually moving, and `auto_unpark` resumes it the
    moment that work closes."""
    try:
        refs = sorted({str(f.get("ref")) for f in (pack.get("findings") or [])
                       if f.get("kind") == "blocked-by" and f.get("confident")
                       and str(f.get("ref") or "").isdigit()}, key=int)
        if not refs:
            return reason, "park"
        blockers = _load("blockers")
        result = blockers.resolve(sdlc_dir, config, source, goal, refs, run=run)
        acted = [r for r in result["results"] if r.get("acted")]
        if acted:
            print("loop.py precheck: made %d blocker(s) of %s workable\n%s"
                  % (len(acted), goal, blockers.render(result)), file=sys.stderr)
        survived = blockers.park_reason(result)
        if survived is None:
            return (reason + " — every named blocker is now workable ("
                    + ", ".join("#%s %s" % (r["ref"], r["verdict"]) for r in result["results"])
                    + "); this goal resumes automatically when they close"), "blocked"
        # Something needs a person, so this IS a park: membership goes, and the reason names what
        # survived and what it needs.
        return reason + " — " + survived, "park"
    except Exception as exc:                            # noqa: BLE001 - never break the park path
        print(f"loop.py precheck: blocker resolution failed ({exc}) — parking as before",
              file=sys.stderr)
        return reason, "park"


def _num(cfg, key, default):
    """Coerce a config value to the default's numeric type, falling back to the default on anything
    bad (a hand-edited `max_children: "many"` must degrade to the default, not crash the `file`-mode
    branch). `bool` (an int subclass) is treated as unset. Local copy of `backlog_check.py`'s own
    `_num` idiom (#522) — this repo's modules deliberately don't share private helpers (see
    `_flags`'s own docstring one screen up)."""
    v = cfg.get(key, default)
    if isinstance(v, bool):
        return default
    try:
        return type(default)(v)
    except (TypeError, ValueError):
        return default


_DEFAULT_MAX_CHILDREN = 8   # matches config.json.tmpl's own `goal_decompose.max_children` default


def decompose_check(sdlc_dir, goal, config, source):
    """Pre-work oversized-goal classifier (OPT-IN via `goal_decompose.enabled is True`), mirroring
    `precheck`'s own shape one section up. Before a picked goal spends a token: read its own title
    +body through `source.fetch_title_body` (never a module-level gh shell-out — see
    `sources.GitHubSource.fetch_title_body`'s own docstring for why that matters for testability),
    skip a goal that is ITSELF a decomposition child or meta-goal (first line of the BODY only —
    see the anchoring note below), then classify what is left with `goal_size.classify` — a
    deterministic, zero-LLM heuristic. `log`/`park` never file anything; `file` additionally opens
    ONE idempotency-guarded "Decompose #N" meta-issue (#522) — even so, the actual decomposition
    (drafting child goals) always runs LATER as its own normal SDLC goal, protected by the same
    plan-review/budget/claims machinery every goal already gets. This verb only ever detects, and
    in `file` mode also files a tracking issue for that later goal to act on — it never decomposes
    anything itself.

    Returns a one-line result the SKILL reads by its first word: 'OFF' (disabled) | 'PARKED
    <reason>' (parked for a human to split — or, in `file` mode, for the filed meta-goal to pick up
    later; do not research it, take the next goal) | 'PROCEED' (unflagged, or a decomposition
    child/meta-goal) | 'PROCEED (flagged: <reason>)' (`log` mode — annotated only, the goal still
    runs). FAIL-OPEN: any error BEFORE a park decision is made, INCLUDING a config value so
    malformed the guard itself cannot evaluate it (e.g. `goal_decompose: "on"` instead of a dict —
    `.get` on a non-dict raises), returns 'PROCEED' with one stderr line — this check must never
    block or crash the loop, matching `precheck`'s own "gate inside the guard" idiom: disabled /
    absent / falsy-malformed short-circuits to 'OFF' with no warning, only a genuinely ill-typed
    value reaches the outer catch. Once a park decision IS made, that guarantee flips: `_record`'s
    own bookkeeping (cursor/ledger/actionlog) is wrapped in its OWN try/except (the `_park` helper
    below, R9/#522 — the ONE place every `park`-mode and `file`-mode exit routes through), separate
    from the outer one, so a failure there — AFTER `source.park` has already gone out — still
    reports 'PARKED', never downgrades to 'PROCEED'. `file` mode's own multi-step sequence (steps
    1-4 below) carries the identical guarantee one level up: it runs inside its OWN inner
    try/except, so ANY failure partway through — even `create_tracked_issue` unexpectedly raising,
    breaking its own documented "never raises" contract — still reaches `_park`, never the outer
    catch (a caller reading PROCEED would go implement the very epic-shaped goal this feature
    exists to catch, possibly on top of a real GitHub issue this run already filed against it).

    Anchoring: the refusal guards match ONLY the first line of the BODY (`.splitlines()[:1]`,
    CRLF-tolerant) — never the title, and never a marker that only appears further down the body —
    so a goal merely discussing decomposition in passing is not exempted by accident.

    Deliberate asymmetry, not drift (#521 review): this guard reads the RAW, unstripped body's first
    line, so a body starting with a blank line sees an empty first line and is NOT exempted here.
    `backlog_check`'s own dedup exemption (its `exempt` computation, backlog_check.py:427-432) instead
    reads the first NON-BLANK line of the stripped excerpt, because local-mode bodies always start
    with a blank line after the frontmatter delimiter. Net effect: a body opening with a blank line
    before its marker is dedup-exempt in `backlog_check` but still fully classified by this guard —
    a recorded decision, not an inconsistency to fix.

    mode: 'log' (default once enabled) classifies + annotates via the local action log, zero
    mutation ever. 'park' parks a flagged goal (`_record`, the same chokepoint every other outcome
    goes through) for a human to split. 'file' (#522) parks too, but FIRST attempts to file ONE
    idempotency-guarded "Decompose #N" meta-issue via `handoff.create_tracked_issue`: a strict read
    (`source.fetch_comments_strict`) checks the parent's own comments for a prior
    `sigma:decompose-filed` marker first — that read failing, returning a malformed shape, or
    the source not offering it at all, all fail CLOSED to a park, never silently treated as "no
    marker". This makes a re-run that gets AS FAR AS THE MARKER safe against double-filing; it is
    NOT an absolute guarantee — a hard crash strictly between the create call landing and the
    marker comment going out (not an ordinary exception, which is already caught and parked) can
    still leave one meta-issue with no marker yet, so a later re-pick could file a second one. The
    meta-goal template's own step 0 (lower-number-wins) is the mitigation for that residual case,
    not a claim it cannot occur. A backlog source with no issue-creation seam at all
    (`hasattr(source, "create_dependency")` false — e.g. `LocalSource`) degrades honestly to the
    same visible `park` action, never a false "failed to file" park with nothing behind it. An
    unrecognized mode string warns once to stderr and falls back to 'log' — the printed result
    always stays inside the OFF | PARKED | PROCEED vocabulary above, never `None`."""
    try:
        gd = config.get("goal_decompose") or {}
        if gd.get("enabled") is not True:
            return "OFF"                                                  # gate inside the guard: a
        title_body = source.fetch_title_body(goal) or {}                  # malformed config -> PROCEED
        body = title_body.get("body") or ""
        gs = _load("goal_size")                     # single load, reused below for classify() too
        first_line = body.splitlines()[:1]
        first_line = first_line[0] if first_line else ""
        # #1825: a "Design #N" meta-issue (design_check's own filed meta-goal, never itself
        # design-worthy -- see design_goal.py) is exempt here too, symmetric with the two markers
        # this check already recognized. Deliberately NOT exempting a design-of goal's own PARENT
        # or any DECOMPOSED_FROM_MARKER child -- those are real, size-classifiable goals.
        dg_marker = _load("design_goal").DESIGN_OF_MARKER
        if (legacy.has_marker(first_line, gs.DECOMPOSED_FROM_MARKER)
                or legacy.has_marker(first_line, gs.DECOMPOSE_OF_MARKER)
                or legacy.has_marker(first_line, dg_marker)):
            return "PROCEED"                        # a child (depth-limited to 1) or a meta-goal itself
        flagged, reason = gs.classify(body)
        if not flagged:
            return "PROCEED"
        mode = gd.get("mode") or "log"               # absent -> 'log', silently — see docstring
        if mode not in ("log", "park", "file"):
            print(f"loop.py decompose-check: unrecognized mode {mode!r} for goal_decompose "
                  "— treating as 'log'", file=sys.stderr)
            mode = "log"
        if mode == "log":
            actionlog.safe_append(sdlc_dir, goal, "decompose_check",
                                   verdict="flagged", reason=reason, mode=mode)
            return f"PROCEED (flagged: {reason})"

        def _park(detail):
            # R9/#522: the one guarded park exit every `park`-mode AND `file`-mode branch below
            # routes through — `detail` is built BEFORE calling `_record`, which gets its OWN
            # try/except here, separate from the outer one further down: `_record` calls
            # `source.park` FIRST, then cursor/ledger/actionlog bookkeeping — once that park
            # mutation has actually gone out, a LATER bookkeeping failure must never downgrade this
            # to PROCEED (a caller reading PROCEED would go implement the very epic-shaped goal this
            # feature exists to catch, on top of it now ALSO being parked on GitHub). So a park
            # decision, once made, is reported as PARKED no matter what happens next.
            try:
                _record(sdlc_dir, source, goal, "parked", detail)
            except Exception as e:
                print(f"loop.py decompose-check: park record failed after park — "
                      f"treating as PARKED anyway: {e}", file=sys.stderr)
            return "PARKED " + detail

        base_detail = f"too large per goal_size ({reason}) — needs manual decomposition"
        if mode == "park":
            return _park(base_detail)

        # mode == "file" (#522): park AND file one idempotency-guarded "Decompose #N" meta-issue --
        # the actual decomposition (drafting children) runs LATER as its own normal SDLC goal.
        if not (hasattr(source, "create_dependency") and hasattr(source, "fetch_comments_strict")):
            # `file` mode needs BOTH seams: one to CREATE the meta-issue, and a per-item comment
            # timeline to read its own idempotency marker back from. Testing only creation used to
            # work as a proxy for "this is GitHub" -- it stopped being one when LocalSource gained
            # create_dependency, so the guard now names what it actually requires. A local backlog
            # can create a goal but has no comment thread, so it cannot guarantee it won't
            # double-file: degrade to the visible park rather than a false "failed to file" park
            # with nothing behind it.
            return _park(base_detail + " (file mode needs an issue tracker)")
        try:
            # step 1: idempotency read -- direct read of the PARENT's own timeline, never a search
            # (search-API results are only eventually consistent, #447). Fails CLOSED on both "no
            # such method" and "the method raised": either way we cannot confirm there is no marker,
            # so the only safe answer is the same park a genuine hit would give.
            fetch_strict = getattr(source, "fetch_comments_strict", None)
            if fetch_strict is None:
                return _park("could not confirm whether a decomposition was already filed — "
                             "check comments")
            try:
                strict = fetch_strict(goal)
            except Exception:
                # NEVER treat an unreadable timeline as "no marker" -- that could double-file the
                # meta-issue -- and never fall through to the outer catch's PROCEED either.
                return _park("could not confirm whether a decomposition was already filed — "
                             "check comments")
            if not isinstance(strict, dict) or not isinstance(strict.get("comments"), list):
                # #522 review fix 2: `fetch_strict` is resolved via a bare `getattr` off whatever
                # source we were given, not guaranteed to be a real GitHubSource -- a return value
                # that ISN'T a well-shaped dict (None, a list, {}, a dict missing "comments"
                # entirely) is exactly as untrustworthy as the read raising outright. Defaulting it
                # to "no comments" here would silently re-open the fail-open hole this strict read
                # exists to close, one layer down from the "raises" case just above.
                # #529 extends that one level further, from the KEY to its VALUE: `{"comments":
                # <non-list>}` used to pass, and the marker scan below then found nothing in it --
                # a string yields characters, a dict yields keys, and both skip the isinstance(dict)
                # filter, so an unreadable timeline read as "no marker" and the meta-issue was filed.
                # The scan is only meaningful over a list, so requiring one IS the confirmation.
                return _park("could not confirm whether a decomposition was already filed — "
                             "check comments")
            dg = _load("decompose_goal")
            comments = strict.get("comments") or []
            if any(legacy.has_marker(c.get("body") or "", dg.DECOMPOSE_FILED_MARKER)
                   for c in comments if isinstance(c, dict)):
                return _park("decomposition already filed — see comments")

            # step 2: create exactly ONE meta-issue. area/priority ride the parent's own labels
            # (fetched in the same strict read above) so the filed issue lands in the right area
            # instead of a made-up default, wherever the parent itself was already triaged.
            names = {(l.get("name") or "") for l in (strict.get("labels") or [])
                     if isinstance(l, dict)}
            area = next((n[len("area:"):] for n in names if n.startswith("area:")), "unknown")
            parent_priority = next((n[len("priority:"):] for n in names
                                    if n.startswith("priority:")), None)

            hf = _load("handoff")
            meta_title = f"Decompose #{goal}: {title_body.get('title') or ''}"[:256]
            # floored at 2 (#522 review fix 6): a hand-edited/typo'd 0, 1, or a negative number is
            # not a valid split size at all -- rendering "Plan 2..0 children" into the filed
            # meta-goal's own instructions would be nonsensical, not just unusual.
            max_children = max(2, _num(gd, "max_children", _DEFAULT_MAX_CHILDREN))
            meta_body = dg.render_meta_body(goal, max_children)
            # dedup=False (#1204 review): every meta-issue body is `dg._META_BODY`, a fixed,
            # multi-paragraph template with only the parent id/title varying per filing -- run
            # through `create_tracked_issue`'s file-time duplicate search (its default), TWO
            # DIFFERENT parents' decompose issues measure as TF-IDF "duplicates" of each other from
            # shared boilerplate alone (0.5+, past the 0.45 duplicate line -- see
            # tests/test_handoff.py's dedup-collision regression), so decompose_check would
            # silently reuse the WRONG parent's meta-issue as `m` below and permanently orphan
            # THIS goal's own decomposition. The idempotency check just above (the
            # DECOMPOSE_FILED_MARKER scan over THIS parent's own comments) is the correctly-scoped
            # guard against double-filing here already; the generic search adds no protection for
            # this caller, only a false-positive risk.
            report = hf.create_tracked_issue(
                sdlc_dir, config, goal=goal, area=area,
                why="oversized goal — needs decomposition before implementation",
                same_area=True, immediately_actionable=True, blocks_goal=False,
                priority=parent_priority or hf.DEFAULT_PRIORITY,
                title=meta_title, body=meta_body,
                extra_labels=["sdlc:decompose"], source=source, dedup=False)

            warnings = list(report["warnings"])
            m = report.get("issue")
            if not m:
                detail = "too large — failed to file decomposition goal"
                if warnings:
                    detail += ": " + "; ".join(warnings)
                return _park(detail + " — needs a human")

            # step 3: marker comment on the parent -- source.note() is UNGUARDED by
            # create_tracked_issue itself for this specific call (it already posted its OWN
            # narrative note above), so this call needs its own try/except here.
            try:
                source.note(str(goal), dg.filed_marker_comment(m))
            except Exception as e:
                # the marker only matters if the park below then ALSO somehow fails to record --
                # fold it into the eventual park detail and keep going, never abort the park itself.
                warnings.append(f"could not post the decompose-filed marker: {e}")

            # step 4: park the parent.
            assignee_applied = getattr(source, "last_assignee_applied", True)
            if assignee_applied:
                detail = f"too large per goal_size ({reason}) — decomposition filed as #{m}"
            else:
                detail = (f"too large per goal_size ({reason}) — decomposition filed as #{m} but "
                          "unassigned — a human must assign it before any loop can see it")
            if warnings:
                detail += " (" + "; ".join(warnings) + ")"
            return _park(detail)
        except Exception as e:
            # R8/#522: anything above raising unexpectedly -- including create_tracked_issue's own
            # documented "never raises" contract somehow breaking -- must still park, never bubble
            # to the OUTER catch below (which would answer PROCEED for a goal that may already have
            # a real GitHub issue filed against it this run).
            return _park(base_detail + f" — file-mode filing hit an unexpected error ({e})")
    except Exception as e:
        print(f"loop.py decompose-check: non-fatal ({e}) — proceeding", file=sys.stderr)
        return "PROCEED"


_DESIGN_MODES = ("full", "lane")


def design_check(sdlc_dir, goal, config, source):
    """Pre-work undesigned-goal RETROFIT gate (OPT-IN via `goal_design.enabled is True`, #1825),
    mirroring `decompose_check`'s own `file`-mode shape one section up — the Tech-side half of
    `docs/dossier-pipeline.md` §6 (the retrofit gate). Before a picked goal spends a
    token: unless it already carries `sdlc:designed`, park it and file ONE idempotency-guarded
    "Design #N" meta-issue instructing a codebase-mapping design pass (`skills/sigma-goal-design/
    SKILL.md`) — the coverage mechanism that closes the gap named in the contract's own §2: nothing
    skips the blast-radius mapping just by being born outside the Dossier (Product-path) front door.

    Placed in SKILL.md step 3 directly AFTER `decompose_check`, for the identical ordering reason
    `decompose_check` itself is placed after `precheck`: a goal that is BOTH oversized AND undesigned
    must be split first, so each eventual child gets its OWN design check individually, rather than
    one design pass being filed against a goal about to be decomposed out from under it.

    Returns a one-line result the SKILL reads by its first word: 'OFF' (disabled) | 'PARKED
    <reason>' (parked — a "Design #N" meta-issue was filed, or filing itself could not be confirmed
    safe; do not research it, take the next goal) | 'PROCEED' (already `sdlc:designed`, or a pure
    bookkeeping meta-goal exempt by construction). FAIL-OPEN for anything BEFORE the label/comment
    read below (disabled / absent / a goal whose title+body cannot be fetched at all all short-
    circuit to 'PROCEED', matching `precheck`/`decompose_check`'s own "gate inside the guard" idiom)
    — this check must never block or crash the loop on an ordinary transient failure. FAIL-CLOSED,
    deliberately unlike that default, for the ONE read this whole feature depends on: once `source.
    fetch_comments_strict` has been called, a failure OR a malformed shape parks rather than
    proceeds — an unreadable label set must never be treated as "already designed", exactly the
    reasoning `decompose_check`'s own idempotency read already applies to "no marker found" (see its
    docstring) — collapsing that ambiguity into PROCEED would silently reopen the coverage hole this
    feature exists to close. Once a park decision IS made, `_record`'s own bookkeeping is wrapped in
    its OWN try/except (the `_park` helper below, mirroring `decompose_check`'s R9), so a failure
    there — AFTER `source.park` has already gone out — still reports 'PARKED', never 'PROCEED'.

    Exemption (first BODY line only, CRLF-tolerant, same anchoring as `decompose_check`):
    `goal_size.DECOMPOSE_OF_MARKER` or `design_goal.DESIGN_OF_MARKER` — a pure bookkeeping
    meta-issue (decompose's own, or this check's own) is never itself design-worthy. Deliberately
    NOT `goal_size.DECOMPOSED_FROM_MARKER` — a decompose CHILD is a real, independently-implementable
    slice with no blast-radius mapping of its own; exempting it would defeat the coverage guarantee
    this feature exists to provide (contract §2's stated gap: `goal_decompose`'s classifier measures
    size, never blast radius).

    `mode`: `"full"` (default once enabled — the contract's own stated default depth, §6a) |
    `"lane"` (a cheaper, lane-sized check for smaller retrofit goals, reusing `discovery.py`'s
    small/medium/large concept). Unlike `goal_decompose.mode`'s three values (log/park/file, which
    pick the ACTION), this axis never changes what `design_check` DOES — once enabled it always
    park-and-files — it only changes the DEPTH instructions rendered into the filed meta-issue's own
    body (`design_goal.render_meta_body`). An unrecognized mode string warns once to stderr and
    falls back to `"lane"` — the least-invasive rung, mirroring `decompose_check`'s own fallback
    MECHANISM (not its literal default value, since the two `mode` axes mean different things)."""
    try:
        gdz = config.get("goal_design") or {}
        if gdz.get("enabled") is not True:
            return "OFF"                                                   # gate inside the guard: a
        title_body = source.fetch_title_body(goal) or {}                   # malformed config -> PROCEED
        body = title_body.get("body") or ""
        gs = _load("goal_size")
        dg = _load("design_goal")
        first_line = body.splitlines()[:1]
        first_line = first_line[0] if first_line else ""
        if (legacy.has_marker(first_line, gs.DECOMPOSE_OF_MARKER)
                or legacy.has_marker(first_line, dg.DESIGN_OF_MARKER)):
            return "PROCEED"                       # a pure bookkeeping meta-goal, never design-worthy

        if not (hasattr(source, "fetch_comments_strict") and hasattr(source, "create_dependency")):
            # Same reasoning as decompose_check's identical guard: a local backlog (or any source
            # with no label/comment-timeline surface) cannot safely confirm "not already designed"
            # NOR file a tracked meta-issue -- degrade to a plain, honest park rather than a false
            # "failed to file" park with nothing behind it.
            return _park_design(sdlc_dir, source, goal,
                                 "not yet designed — coverage check needs an issue tracker")

        def _park(detail):
            # Mirrors decompose_check's own R9/#522 nested `_park`: once a park decision is made it
            # is reported as PARKED no matter what a later bookkeeping failure does.
            try:
                _record(sdlc_dir, source, goal, "parked", detail)
            except Exception as e:
                print(f"loop.py design-check: park record failed after park — "
                      f"treating as PARKED anyway: {e}", file=sys.stderr)
            return "PARKED " + detail

        try:
            strict = source.fetch_comments_strict(goal)
        except Exception:
            # NEVER treat an unreadable label/comment set as "already designed" -- that is exactly
            # the coverage hole this feature exists to close.
            return _park("could not confirm whether this goal already carries sdlc:designed — "
                         "check labels/comments")
        if (not isinstance(strict, dict) or not isinstance(strict.get("labels"), list)
                or not isinstance(strict.get("comments"), list)):
            # A source that returns a well-shaped dict is guaranteed by GitHubSource's own contract,
            # but `fetch_comments_strict` is resolved via a bare `hasattr` above, not a guaranteed
            # GitHubSource -- a malformed shape is exactly as untrustworthy as the read raising.
            return _park("could not confirm whether this goal already carries sdlc:designed — "
                         "check labels/comments")

        names = {(l.get("name") or "") for l in strict["labels"] if isinstance(l, dict)}
        if "sdlc:designed" in names:
            return "PROCEED"

        comments = strict["comments"]
        if any(legacy.has_marker(c.get("body") or "", dg.DESIGN_FILED_MARKER)
               for c in comments if isinstance(c, dict)):
            return _park("design already filed — see comments")

        # `mode` is only resolved here — after we've confirmed a meta-issue is actually going to be
        # filed — so an operator's typo'd config doesn't warn to stderr on every already-designed
        # or already-filed pick too (this axis picks DEPTH, not whether we act, so it has no reason
        # to be evaluated before we know we're acting).
        mode = gdz.get("mode") or "full"           # absent -> 'full', the contract's stated default
        if mode not in _DESIGN_MODES:
            print(f"loop.py design-check: unrecognized mode {mode!r} for goal_design "
                  "— treating as 'lane'", file=sys.stderr)
            mode = "lane"

        area = next((n[len("area:"):] for n in names if n.startswith("area:")), "unknown")
        parent_priority = next((n[len("priority:"):] for n in names
                                if n.startswith("priority:")), None)
        hf = _load("handoff")
        meta_title = f"Design #{goal}: {title_body.get('title') or ''}"[:256]
        meta_body = dg.render_meta_body(goal, mode)
        try:
            # dedup=False (mirrors decompose_check's own #1204 reasoning): every meta-body shares
            # the same fixed template modulo the goal id/mode text -- the generic TF-IDF duplicate
            # search would risk reusing a DIFFERENT goal's meta-issue as `m` below. The idempotency
            # check just above (DESIGN_FILED_MARKER scan over THIS goal's own comments) is the
            # correctly-scoped guard against double-filing here already.
            report = hf.create_tracked_issue(
                sdlc_dir, config, goal=goal, area=area,
                why="not yet designed — needs a codebase-mapping design pass before implementation",
                same_area=True, immediately_actionable=True, blocks_goal=False,
                priority=parent_priority or hf.DEFAULT_PRIORITY,
                title=meta_title, body=meta_body, source=source, dedup=False)
        except Exception as e:
            return _park(f"not yet designed — file-mode filing hit an unexpected error ({e})")

        warnings = list(report["warnings"])
        m = report.get("issue")
        if not m:
            detail = "not yet designed — failed to file design goal"
            if warnings:
                detail += ": " + "; ".join(warnings)
            return _park(detail + " — needs a human")

        # marker comment on the flagged goal -- source.note() is UNGUARDED by create_tracked_issue
        # itself for this specific call (it already posted its OWN narrative note above), so this
        # call needs its own try/except here, mirroring decompose_check's own step 3.
        try:
            source.note(str(goal), dg.filed_marker_comment(m))
        except Exception as e:
            warnings.append(f"could not post the design-filed marker: {e}")

        assignee_applied = getattr(source, "last_assignee_applied", True)
        if assignee_applied:
            detail = f"not yet designed — design pass filed as #{m}"
        else:
            detail = (f"not yet designed — design pass filed as #{m} but unassigned — "
                      "a human must assign it before any loop can see it")
        if warnings:
            detail += " (" + "; ".join(warnings) + ")"
        return _park(detail)
    except Exception as e:
        print(f"loop.py design-check: non-fatal ({e}) — proceeding", file=sys.stderr)
        return "PROCEED"


def _park_design(sdlc_dir, source, goal, detail):
    """The no-issue-tracker degrade path for `design_check`, factored out so it shares the exact
    same guarded-`_record` contract as the nested `_park` closure above without duplicating it
    inline — this one branch runs BEFORE that closure is defined (it fires before any network read),
    so it cannot simply call it."""
    try:
        _record(sdlc_dir, source, goal, "parked", detail)
    except Exception as e:
        print(f"loop.py design-check: park record failed after park — "
              f"treating as PARKED anyway: {e}", file=sys.stderr)
    return "PARKED " + detail


_evidence_path = state.evidence_path       # both live in state.py so work.py can require the same
_done_refusal = state.done_refusal         # evidence without loop.py and work.py importing each other


def _flake_verdict(sdlc_dir, goal, root, passed):
    """#1933: re-run this goal's CHANGED tests 3x with varied order and a fresh temp dir.

    -> the dict `flake_check.check` returns, always. Never raises, never gates verify_goal.

    SCOPED TO CHANGED TESTS (done_when 3). Running the whole suite 3x would triple every goal's
    verify cost for no added signal on tests that have already proven stable. `changed_test_files`
    is #1937's diff parser, reused rather than reimplemented, so #1933/#1934/#1935 all inherit one
    answer to "which tests are in play" instead of three free to disagree.

    NOT RUN ON A RED VERIFY. If the suite already failed, the red IS the finding -- asking three
    more times whether it is deterministic costs 3x for nothing.

    THE VERDICT IS NOT WRITTEN INTO `verify_state`, and that is deliberate rather than an oversight.
    The downstream verify-state metric folds any value other than 'pass'/'fail' into ABSENT, and
    the downstream ingester accepts only those two before falling back to `exit` -- so an
    'unverified' there would render a flaky goal as "no evidence recorded for this goal at all",
    which is worse than the truth, not better. It goes in its own `flake` key, whose
    verified/unverified/absent vocabulary is the one #1934 must consume rather than minting a
    second."""
    empty = {"verdict": "absent", "runs": [], "disagreement": None, "ms": 0}
    if not passed:
        return empty
    try:
        planned = _planned_tests(sdlc_dir, goal, root)
        if planned is not None:
            return flake_check.check(root, sorted({n.split("::")[0] for n in planned}), node_ids=planned)
        rec = work._record(sdlc_dir, goal) or {}
        base = rec.get("base") or "HEAD"
        diff = subprocess.run(["git", "diff", "%s...HEAD" % base], cwd=root,
                               capture_output=True, text=True).stdout
        changed = tamper_scan.changed_test_files(diff)
        return flake_check.check(root, changed) if changed else empty
    except Exception as exc:              # noqa: BLE001 - advisory; must never break a verify
        print(f"loop: flake check skipped (non-fatal): {exc}", file=sys.stderr)
        return empty


def _planned_tests(sdlc_dir, goal, root):
    """One strict planned scope for observation, flake and witness; absent legacy plans use diff."""
    plan = work.verification_plan(sdlc_dir, goal, root)
    if plan is None or "## Tests" not in plan.read_text().splitlines():
        return None
    proof = _load("red_green")
    return proof.collect(root, proof.selectors(plan))


def _changed_tests(sdlc_dir, goal, root):
    """The goal's changed test FILES -> node ids, via #1937's parser and #1933's collector.

    One resolution shared by the flake check (#1933) and the witness record (#1934), so the two can
    never disagree about which tests are in play for a goal.

    THE REMOTE-TRACKING REF, NEVER THE BARE LOCAL NAME (#2235). `rec["base"]` is a branch NAME
    ("main"), not a ref by itself -- diffing that bare name resolves to LOCAL `refs/heads/main`,
    which every worktree of this repo shares and which nothing here ever advances (a `git fetch`
    only moves `refs/remotes/<remote>/<base>`; the local branch sits wherever it last was pulled,
    which can be arbitrarily far behind). `work.start()` cuts every fresh worktree from
    `<remote>/<base>` for exactly this reason, and every other "this goal's own diff" call in the
    kit already qualifies the ref the same way (`work.py`'s `_pr_body`, `_behind_count`,
    `_branch_touches_source`: `f"{remote}/{base}"`). Using the stale local ref instead pulls in
    every file changed by whatever landed on the real base since -- the live incident that
    mis-attributed another goal's own test file into this goal's witness scope, writing 574 wrong
    records on #1810."""
    planned = _planned_tests(sdlc_dir, goal, root)
    if planned is not None:
        return planned
    rec = work._record(sdlc_dir, goal) or {}
    base, remote = rec.get("base"), rec.get("remote")
    ref = f"{remote}/{base}" if remote and base else (base or "HEAD")
    diff = subprocess.run(["git", "diff", "%s...HEAD" % ref],
                           cwd=root, capture_output=True, text=True).stdout
    files = tamper_scan.changed_test_files(diff)
    return flake_check._collect(root, files, flake_check._run) if files else []


def _witness_verdict(sdlc_dir, goal, root, passed, output):
    """#1934: on RED, record what was seen to fail and HOW. On GREEN, judge what that earned.

    -> the dict `witness.verdict` returns. Never raises, never gates verify_goal.

    THE RED PATH IS THE ONLY MOMENT THE EVIDENCE EXISTS. A failure's kind (assertion vs
    collection-error) and the test's content hash are both readable only while the run that produced
    them is in hand; asking later gets a green tree that remembers nothing. So the witness is written
    HERE, from the same `proc` output the evidence file already quotes.

    THE HASH IS TAKEN AT RED, and compared at green. That pairing is what makes "the test that
    passed is the test that failed" checkable rather than asserted."""
    try:
        tests = _changed_tests(sdlc_dir, goal, root)
        if not passed:
            failed = flake_check._failed_ids(output)
            # NEVER `failed or tests` (#2235). When `_failed_ids` finds no `FAILED <nodeid>` line
            # -- an INTERNALERROR, a collection abort, a timeout, anything that killed the run
            # before it could name a test -- the failure cannot be attributed to any ONE changed
            # test. `classify(output)` still returns a single kind for the whole blob, and that
            # blob can contain "AssertionError" from deep in an internal traceback that has nothing
            # to do with any test's own assertion. Falling back to `tests` used to stamp EVERY
            # changed test with that one kind -- fabricating a STRONG witness (witness.STRONG_KINDS
            # includes "assertion") for tests that were never individually run, let alone failed.
            # The live incident wrote 574 such records on #1810. The honest behaviour is to record
            # nothing for this run: an unattributed red leaves each test exactly where it already
            # was ("never seen red" if it has no prior witness), never "verified" on a reading it
            # never earned. This does not weaken STRONG_KINDS or classify() themselves -- it only
            # stops using them where there is no specific test to attach the classification to.
            if failed:
                kind = witness.classify(output)
                for node in failed:
                    witness.record(sdlc_dir, goal, node, kind,
                                   witness.source_hash(witness.test_source(node, root=root)))
        return witness.verdict(sdlc_dir, goal, tests, root=root)
    except Exception as exc:              # noqa: BLE001 - advisory; must never break a verify
        print(f"loop: witness skipped (non-fatal): {exc}", file=sys.stderr)
        return {"verdict": witness.ABSENT, "verified": [], "unverified": [], "detail": {}}


def _diff_revert_verdict(sdlc_dir, goal, root, passed):
    """#2240: mutmut-free companion to #1935 -- reverts the goal's production code in a SCRATCH
    worktree (never this tree) and reruns its touched tests there; a real RED there is recorded as
    a STRONG `mutation` witness, the kind #1935 was designed to write and, being unreachable
    (mutmut is absent on every dev machine and wired into nothing), never has.

    -> the dict `diff_revert.run` returns. Never raises, never gates verify_goal -- advisory, like
    its `_flake_verdict`/`_witness_verdict` siblings.

    NOT RUN ON A RED VERIFY, same reasoning as `_flake_verdict`: "does it survive without the
    change" means nothing when the suite does not even survive WITH it.

    ITS OWN DIFF, DELIBERATELY NOT `_changed_tests`'s. `_changed_tests` reads `base...HEAD` -- a
    range between two COMMITS, so it sees nothing on a goal's most common verify call, the one
    BEFORE `work.py commit` has ever run (SKILL.md: "No phase subagent commits — only `work.py
    commit` does, once, at step 6"), where the entire diff is still uncommitted. Measured live,
    2026-09-04: an uncommitted edit against `origin/main...HEAD` diffs empty; against a single-ref
    `git diff origin/main` (content_fingerprint's own technique, "measured against the WORKING
    TREE, so uncommitted work counts") it does not. Reusing the three-dot form here would make this
    control `absent` on exactly the call it most needs to run on -- so it resolves its own test list
    the working-tree-inclusive way instead, including untracked new test files (`git diff` alone
    never lists those), and hands `diff_revert.run` the same test-id shape #1934 already produces
    so a kill here can be recorded as a witness for the identical test."""
    empty = {"verdict": diff_revert.ABSENT, "kills": [], "survivors": [], "reason": None, "ms": 0}
    if not passed:
        return empty
    try:
        wrec = work._record(sdlc_dir, goal) or {}
        base_ref = (f"{wrec['remote']}/{wrec['base']}"
                    if wrec.get("remote") and wrec.get("base") else None)
        if not base_ref:
            return empty
        fork_code, fork_out = diff_revert._git(root, ["merge-base", "HEAD", base_ref])
        if fork_code != 0:
            return empty
        diff_code, diff_out = diff_revert._git(root, ["diff", "--no-renames", fork_out.strip()])
        diff_text = diff_out if diff_code == 0 else ""
        files = set(tamper_scan.changed_test_files(diff_text))
        others_code, others_out = diff_revert._git(root, ["ls-files", "--others",
                                                            "--exclude-standard"])
        if others_code == 0:
            files |= {p for p in others_out.splitlines() if p and tamper_scan._TEST_PATH.search(p)}
        if not files:
            return empty
        tests = flake_check._collect(root, sorted(files), flake_check._run)
        result = diff_revert.run(sdlc_dir, goal, root, base_ref, tests)
        for node in result.get("kills", []):
            witness.record(sdlc_dir, goal, node, "mutation",
                            witness.source_hash(witness.test_source(node, root=root)),
                            detail="diff-revert (#2240): green with the goal's production code, "
                                   "red with it reverted to %s" % fork_out.strip()[:12])
        return result
    except Exception as exc:              # noqa: BLE001 - advisory; must never break a verify
        print(f"loop: diff-revert check skipped (non-fatal): {exc}", file=sys.stderr)
        return dict(empty, reason=f"skipped: {exc}")


def _verified_tree(sdlc_dir, goal, config=None):
    """-> (root, head, fell_back). WHICH TREE a verify is about to run against, and whether that is
    the goal's own worktree or the project-root fallback.

    THE FALLBACK IS CORRECT; BEING SILENT ABOUT IT WAS THE DEFECT. `work.root` returns the project
    root for any goal with no work record -- its own docstring says so, and a repo with the worktree
    feature off depends on it. But the evidence file recorded the command, the exit code and the
    output tail, and NEVER which tree produced them. So a goal verified against a checkout 77
    commits behind origin/main is byte-indistinguishable from one verified against its own code.

    That is not hypothetical. On 2026-09-01 six merged goals were verified against exactly that
    stale checkout: it collects 3539 tests where the current tree collects 4077, six of them failed,
    `record done` refused for every goal, and the shortfall was misread as a TRUNCATED run of the
    right tree rather than a COMPLETE run of the wrong one. AGENTS.md's own rule is the summary --
    a green status file is not freshness.

    `head` is read fail-open and is None when it cannot be: a project root that is not a git
    checkout at all is a legitimate configuration, and inventing a sha (or dropping the key) would
    make an unverifiable record look verified."""
    root = work.root(sdlc_dir, goal, config)
    # ASK WHERE WE LANDED, NOT WHETHER A RECORD EXISTS. `fell_back` used to be read off the work
    # record, which stopped being the same question in two directions once `work.root` learned to
    # recover a worktree from disk (#1984's open checkbox):
    #   * no record but a worktree recovered -> the record test says "fell back" and the warning
    #     names a GOAL WORKTREE as "the PROJECT ROOT". A warning that lies is worse than none.
    #   * a record whose worktree directory has since been deleted -> `work.root` returns the
    #     project root and the record test says it did not. That one was silent BEFORE this change
    #     (`test_root_falls_back_when_the_worktree_is_gone` is exactly that state), so comparing
    #     the resolved path closes a pre-existing hole as well as the new one.
    # The honest definition of "fell back" is "the root I am about to use IS the project root".
    fell_back = pathlib.Path(root).resolve() == work.project_root(sdlc_dir)
    head = None
    try:
        proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                               capture_output=True, text=True)
        if proc.returncode == 0:
            head = (proc.stdout or "").strip() or None
    except Exception:                       # noqa: BLE001 - a bookkeeping read must never break verify
        head = None
    return (root, head, fell_back)


#: The one gesture every "no verify command" message names (#228), so the loop, the merge gate,
#: /sigma-init and /sigma-doctor all point at the same fix. Built by `state.verify_set_hint` since
#: #312, so `work.py merge`'s park names the identical fix. `confirm .sdlc <n> <id>` re-derives
#: candidate n from the repo itself: no repository text is ever pasted into a shell.
_VERIFY_SET_HINT = state.verify_set_hint()

#: #312: one copy, in state.py, read by `record done` here and by `work.merge` -- see
#: `state.verify_required` for the rule the two gates share.
_declared_verify_command = state.declared_verify_command


def verify_goal(sdlc_dir, goal):
    """Run the goal's proving command and persist MACHINE evidence (sigma-verify's
    prose gate, made checkable). With an acceptance record, run the repository command then
    the recorded goal command (local frontmatter fallback), each in its own shell. Without a
    record retain local-frontmatter-over-repository precedence. Evidence's command label is
    an ordered JSON array for multiple commands; single-command labels remain unchanged.
    Exit: 0 verified · 1 the command failed · 2 unsafe goal (refused before running anything) ·
    3 no command declared (honest absence) · 4 worktree behind its base and the auto-rebase could
    not apply cleanly (refused before running anything -- #1890)."""
    import hashlib, json as _json, subprocess
    # Checked FIRST, before the subprocess even runs — not just left to _evidence_path()'s own
    # guard further down, which would let an unsafe goal's proving command execute for nothing
    # (see state.unsafe_goal_reason's docstring for the reproduced vulnerability this closes).
    goal_stem = pathlib.Path(str(goal)).stem if str(goal).endswith(".md") else str(goal)
    reason = state.unsafe_goal_reason(goal_stem)
    if reason:
        print(f"loop.py verify: unsafe goal {goal!r}: {reason}", file=sys.stderr)
        return 2
    config = state.load_config(sdlc_dir)
    try:
        acceptance_hash = state.acceptance_module().digest(sdlc_dir, goal)
        commands = state.declared_verify_commands(goal, config, sdlc_dir)
        cmd = state.verify_command_label(commands)
    except (OSError, ValueError) as exc:
        print("loop.py verify: invalid acceptance record: " + str(exc), file=sys.stderr)
        return 2
    if not cmd:
        print("NO-COMMAND (set goal frontmatter `verify_command` or config `verify.command`; "
              f"{_VERIFY_SET_HINT})", file=sys.stderr)
        ledger.safe_append(sdlc_dir, "verify", goal, config=config, stream=ledger.EVENTS,
                           ok=False, exit=3, absent=True)
        return 3
    # #1890: catch a worktree that has drifted behind its own base BEFORE the expensive subprocess
    # below ever runs -- #1617 (78 commits behind) and #1829 both burned a full multi-hundred-second
    # verify against stale code before a human noticed. `ensure_fresh` auto-rebases via the existing
    # `work.rebase()` when that applies cleanly (logging why, loudly); a genuine conflict refuses
    # here rather than running the suite against code that provably still conflicts with the base.
    # No evidence is written on this path (the `ev.write_text(...)` call below is never reached),
    # so `state.done_refusal` already refuses `record done` with zero changes needed there.
    stale = work.ensure_fresh(sdlc_dir, config, goal)
    if stale:
        print(f"STALE exit=4 -- {stale}", file=sys.stderr)
        # #1899: exit=4 (like exit=2 unsafe-goal) previously wrote NO ledger/actionlog event at
        # all, leaving a goal repeatedly hitting this refusal invisible to /sigma-log beyond one
        # stderr line nobody unattended is reading -- an overnight/autonomous LIVENESS gap
        # (AGENTS.md), confirmed by an independent retro comment on this issue's own thread.
        # `exit=4` ALONE (no `ok`, no `absent`) on both writes, deliberately NOT a literal copy of
        # exit=3's `absent=True` shape: a downstream goal-timeline view's verify synthesis
        # reads `latest["absent"] is True` to mean "no command configured" -- true for
        # exit=3 (no command is configured at all) but FALSE for exit=4 (a command IS configured;
        # the goal was refused because its worktree was behind its base and the auto-rebase could
        # not apply cleanly). Reusing `absent=True` here would make the
        # dashboard assert something false. Omitting both `ok` and `absent` leaves that reader's
        # `latest["ok"]` NULL, falling through to its existing, honest `"unknown"` bucket instead.
        # Independent plan-review (#1899) verified both `ledger.append()` and `actionlog.append()`
        # skip any field whose value is `None` -- so `exit=4` alone is legal on both calls, and the
        # actionlog write (which the ledger-only shape originally missed) is what actually makes a
        # goal repeatedly hitting this refusal visible in `/sigma-log` (`ledger.EVENTS` is not what
        # that tool reads -- see `skills/sigma-log/scripts/log.py`'s own module docstring).
        ledger.safe_append(sdlc_dir, "verify", goal, config=config, stream=ledger.EVENTS,
                           exit=4)
        actionlog.safe_append(sdlc_dir, goal, "verify_run", exit=4)
        return 4
    # Proving commands run where THIS GOAL'S CODE IS — its worktree when `work` is on, else the
    # project root, exactly as before (same injectable-root rule as pipeline.py's `repo_root`).
    # Deriving it from sdlc_dir alone would test the main checkout while the change sits in the
    # worktree: a green that proves nothing, and one that `record done` would happily accept.
    root, head, fell_back = _verified_tree(sdlc_dir, goal, config)
    if fell_back:
        # Loud, at the moment it happens. Left to be inferred later this costs a wrong diagnosis:
        # on 2026-09-01 stale-tree failures were chased as a harness bug for several passes.
        print(f"loop.py verify: goal {goal!r} has no work record — verifying the PROJECT ROOT "
              f"({root}) at {head or 'unknown HEAD'}, NOT a goal worktree. If that checkout is "
              f"stale, this result is about the wrong code.", file=sys.stderr)
    if not shell_policy.repository_shell_commands_allowed(root):
        print("loop.py verify: " + shell_policy.refusal_message(), file=sys.stderr)
        ledger.safe_append(sdlc_dir, "verify", goal, config=config, stream=ledger.EVENTS, exit=2)
        actionlog.safe_append(sdlc_dir, goal, "verify_run", exit=2)
        return 2
    _wrec = work._record(sdlc_dir, goal) or {}
    _base_ref = (f"{_wrec['remote']}/{_wrec['base']}"
                 if _wrec.get("remote") and _wrec.get("base") else None)
    content_before = state.content_fingerprint(root, _base_ref, pathlib.Path(sdlc_dir).name)
    start = time.perf_counter()
    outputs = []
    for command in commands:
        proc = subprocess.run(command, shell=True, capture_output=True, text=True, cwd=root)
        outputs.append(proc.stdout + proc.stderr)
        if proc.returncode != 0:
            break
    # #267: an explicit per-node run proves planned tests actually executed, even when the
    # configured shell command only prints success. Missing scope remains advisory at verify;
    # work.pr fails closed unless the operator supplies the published exception reason.
    try:
        test_first = _load("red_green").observe(
            sdlc_dir, goal, root, work.verification_plan(sdlc_dir, goal, root))
    except (OSError, ValueError) as exc:
        test_first = {"passed": False, "error": str(exc)}
    if test_first.get("provenance") and not test_first.get("passed") and proc.returncode == 0:
        proc.returncode = test_first.get("exit") or 1
        outputs.append(test_first.get("error", "planned tests failed"))
    ms = int((time.perf_counter() - start) * 1000)
    ev = _evidence_path(sdlc_dir, goal)
    ev.parent.mkdir(parents=True, exist_ok=True)
    tail = "\n".join(outputs).strip().splitlines()[-5:]
    this_run = state.run_identity()          # #498: this run's id (SIGMA_RUN_ID) or None
    # #498 (suggestion 2): warn loudly before clobbering an evidence file a DIFFERENT run wrote
    # whose writer process is STILL LIVE — i.e. a genuinely concurrent verify on one goal, the setup
    # this issue is about. Gated on pid-liveness so a legitimate SEQUENTIAL re-verify of a reopened
    # goal (a different, long-dead run) does not cry wolf. Best-effort and NEVER fatal (a bookkeeping
    # read must not stop a verify), so it is fully guarded; we still write OUR own correctly-
    # attributed evidence below, which is what actually protects the sibling (its `record done` will
    # see our run-id, mismatch, and be refused by state.done_refusal — the warning is only a heads-up).
    if ev.exists():
        try:
            prev = _json.loads(ev.read_text())
            prev_run, prev_pid = prev.get("run"), prev.get("pid")
            if (prev_run is not None and prev_run != this_run
                    and isinstance(prev_pid, int) and ledger.pid_alive(prev_pid)):
                print(f"loop.py verify: WARNING overwriting verify evidence for goal {goal!r} "
                      f"written by a different LIVE run {prev_run!r} (writer pid {prev_pid}) — "
                      f"concurrent verify on one goal?", file=sys.stderr)
        except Exception:                    # noqa: BLE001 - a journal read must never break verify_goal
            pass
    # `at` keeps sub-second precision (F11/#341) — see state.start_run's comment; flooring both
    # this stamp and the run's start to whole seconds is what let a stale green tie with a run
    # that started a fraction of a second later and get accepted as fresh.
    # `run`/`pid` (#498) attribute the write to THIS run so state.done_refusal can refuse a
    # concurrent sibling's green rather than inheriting it; both are additive — a pre-#498 reader
    # simply ignores them, and a pre-#498 evidence file (no `run`) is handled by done_refusal.
    # `verify_state` (#997) is the literal 'pass'/'fail' string the downstream ingester
    # reads to populate its own verify-state column — additive, same posture as run/pid:
    # a pre-#997 reader of this file (done_refusal, the WARNING check above) only ever reads via
    # `.get()`, so an older reader ignores the new key exactly as the ledger's own "an older
    # reader ignores a field it does not know" contract already promises elsewhere in this repo.
    # Derived from the SAME `proc.returncode` as `exit` below, never a second, independently-
    # computed signal that could disagree with it.
    # #1933: a green suite is not yet a TRUSTWORTHY one. Computed before the evidence write so the
    # verdict is part of the same atomic record, never a second file a reader could miss.
    flake = _flake_verdict(sdlc_dir, goal, root, proc.returncode == 0)
    # #2240: the mutmut-free kill control. Runs BEFORE `_witness_verdict` below, deliberately --
    # a real kill here is recorded as a STRONG `mutation` witness, and ordering it first means
    # `witness.verdict` (called inside `_witness_verdict`) already sees that witness on disk in
    # THIS SAME evidence write, rather than crediting it only on the goal's next verify.
    revert = _diff_revert_verdict(sdlc_dir, goal, root, proc.returncode == 0)
    # #1934: red-before-green as data. Written on the RED path because that is the only moment the
    # failure kind and the at-red content hash exist; judged on the green one.
    seen_red = _witness_verdict(sdlc_dir, goal, root, proc.returncode == 0,
                                 (proc.stdout or "") + (proc.stderr or ""))
    # #1897: WHAT this run verified, so `state.done_refusal` can refuse a tree that has been edited
    # since. Computed HERE -- after the proving command AND after the flake/witness passes above,
    # both of which re-run tests and can touch the tree -- so the recorded content is the content
    # standing when the evidence is written, which is the same content a later `record done` sees.
    # `head` alone cannot serve: at verify time the whole change is uncommitted (SKILL.md runs
    # `work.py commit` only after verify is green), so `head` is the shared base. See
    # `state.content_fingerprint`. Fails open into an honest `detail` -- a repo with no work record
    # (`work.enabled: false`) writes a null fingerprint and the gate stays exactly as it was.
    content_after = state.content_fingerprint(root, _base_ref, pathlib.Path(sdlc_dir).name)
    if (test_first.get("passed") and content_before.get("fingerprint")
            and content_before.get("fingerprint") != content_after.get("fingerprint")):
        test_first.update(passed=False, error="repository content changed during verify")
        proc.returncode = proc.returncode or 1
    ev.write_text(_json.dumps({"command": cmd, "exit": proc.returncode,
                               "acceptance_sha256": acceptance_hash,
                               "verify_state": "pass" if proc.returncode == 0 else "fail",
                               "at": time.time(), "run": this_run, "pid": os.getpid(),
                               "root": root, "head": head,
                               "content": content_after,
                               "flake": flake,
                               "test_first": test_first,
                               "witness": {"verdict": seen_red["verdict"],
                                           "unverified": seen_red["unverified"],
                                           "detail": seen_red["detail"]},
                               "diff_revert": revert,
                               "tail": tail}, indent=2))
    # `cmd` is externally-derived (goal frontmatter or config) and `.encode()`/`hashlib.sha256`
    # are evaluated as call ARGUMENTS in THIS frame, not inside `safe_append`'s own try/except — a
    # raise there would take down verify_goal itself unless guarded here too, not just at the
    # append() call (Python evaluates arguments before the call happens).
    try:
        ledger.safe_append(sdlc_dir, "verify", goal, config=config, stream=ledger.EVENTS,
                           ok=(proc.returncode == 0), exit=proc.returncode, ms=ms,
                           command_sha256=hashlib.sha256(cmd.encode("utf-8")).hexdigest())
    except Exception:                # noqa: BLE001 - fail-open; a journal hash must never break verify_goal
        pass
    # Independent concern from the ledger's own hash-computation guard above — an unconditional
    # call, not nested inside that try.
    actionlog.safe_append(sdlc_dir, goal, "verify_run", ok=(proc.returncode == 0), exit=proc.returncode, ms=ms)
    # The working-time record. The two writes above both carry this same `ms` and both are
    # config-gated, so on a stock install the figure is computed and thrown away; this one is not
    # gated. It sits BEFORE the exit-5 return below on purpose — a suite that burned twenty minutes
    # proving itself flaky did that work, and must not report as having taken none.
    #
    # `name` is a fixed label, never `cmd`: a verify command can carry a token or an internal
    # hostname, which is why the ledger records only a hash of it
    # (`test_verify_event_never_contains_the_raw_command`). A durable local file must not become
    # the place that leaks what the event stream was careful to hide.
    timing_store.safe_append(sdlc_dir, goal, "verify", "command", ms)
    # The flake and diff-revert passes re-run tests and can dominate the proving command on a green
    # goal; both already measure themselves and both figures were previously discarded, which made
    # `background` materially short. A pass that did not run reports `ms: 0` — omitted, because a
    # zero is not a measurement.
    for _label, _result in (("flake", flake), ("diff_revert", revert)):
        try:
            _pass_ms = int((_result or {}).get("ms") or 0)
        except (TypeError, ValueError):
            _pass_ms = 0
        if _pass_ms > 0:
            timing_store.safe_append(sdlc_dir, goal, "verify", _label, _pass_ms)
    if flake["verdict"] == "unverified":
        # #1933: the suite PASSED but is not deterministic, so it has not earned a credit. Exit 5 --
        # its own code, not 1, which means "the tests failed" and would misreport what happened.
        print(f"UNVERIFIED exit=5 -- {flake['disagreement']} evidence={ev}", file=sys.stderr)
        return 5
    print(f"{'VERIFIED' if proc.returncode == 0 else 'FAILED'} exit={proc.returncode} evidence={ev}")
    return 0 if proc.returncode == 0 else 1


def _arm_run_id(session_pid):
    """#889: attribute a bare `/sigma-loop` session by exporting a session-derived
    `SIGMA_RUN_ID` into THIS process, when no launcher already set one.

    Called from the three verbs that resolve a genuinely stable session pid AND can reach
    `_emit_run_stop_once` -- `start`, `next`, `next-batch`. Setting the env var rather than
    threading a parameter is what keeps the diff at zero signature changes: `state.run_identity()`
    is read at five separate production sites (the `claimed` append, the terminal-outcome append,
    the reclaim append, `_auto_reclaim_stale_claims`, and `state.done_refusal`), and all five see
    one consistent value for the rest of this process.

    NOT a central pre-parse in `_dispatch`. `--session-pid` is parsed per-verb here, and `record`
    disambiguates positionally (`argv[5]` is a freeform reason), so a shared `_flags(argv[3:])`
    ahead of dispatch would misparse it.

    `supervise.sh` STILL WINS: an already-exported id is never overwritten. Its id is stable across
    a worker's whole lifetime including budget-stop relaunches, which a per-session one is not.

    Pass the EXPLICIT flag value only -- see `state.derive_run_id`'s own docstring for why
    `os.getppid()` (loop.py's `start` fallback, just below its own call to this) must never reach
    here. A None/unusable pid leaves the env untouched and the run unattributed, exactly as before.
    """
    if os.environ.get("SIGMA_RUN_ID"):
        return
    derived = state.derive_run_id(session_pid)
    if derived:
        os.environ["SIGMA_RUN_ID"] = derived


def _emit_run_stop(sdlc_dir, config, reason_class, why=None):
    """#547: write the run-level `run_stop` event a downstream budget-exhaustion rate reads.

    Goal is EMPTY on purpose: a run-stop is scoped to the project, not any one goal within it, so
    a downstream ingester lands it with a null goal — exactly the null-goal scoping a
    budget-exhaustion reader uses to keep a per-goal `park` (even one carrying
    reason_class='budget') from ever being swept in. `safe_append` is fail-open, so a ledger
    problem here never aborts the drain — matching every other ledger call site in this file.

    Unconditional and unguarded — callers must not call this directly from `_next()`/`run_loop`;
    use `_emit_run_stop_once` below, which is the actual gated, deduped, degradation-aware entry
    point (#905). Kept as its own function so the ledger-write concern stays separate from the
    attribution/dedupe/degradation policy wrapped around it."""
    ledger.safe_append(sdlc_dir, "run_stop", "", config=config, stream=ledger.EVENTS,
                       reason_class=reason_class, why=why)


def _emit_run_stop_once(sdlc_dir, config, source, reason_class, why=None):
    """#905: the gated, deduped wrapper `_next()` calls at its own DONE/BUDGET terminal returns —
    the real production chokepoint (both the CLI `next` verb and `run_loop` pass through `_next()`,
    so wiring it here covers both drivers with one edit; `#547`'s `_emit_run_stop` above had no
    production caller — the live loop is CLI `next`/`record`-driven, never in-process `run_loop`).

    Three gates, in the order the code below actually checks them (PR-review correction: an
    earlier revision of this docstring listed dedupe before degradation; degradation genuinely
    runs SECOND, dedupe third, and that order is deliberate — see point 2):
    1. ATTRIBUTION — only emit when `state.run_identity()` is not `None`. An unattributed session
       (no `SIGMA_RUN_ID`: any interactive/direct `/sigma-loop` session not wrapped in
       `supervise.sh`) is skipped, not recorded under a shared `None` key — a durable "at most one
       row per identity" guard keyed on `None` would let the FIRST unattributed run ever claim
       that key and silently suppress every later unattributed run's real stop, forever.
       `done_refusal` already has this exact precedent (`state.py`'s own docstring: "degrades to
       the pre-#498 freshness-only behaviour" when unattributed) rather than inventing unsafe
       cross-process coordination for a case #498 already decided not to support.
    2. DEGRADATION (reason_class == 'backlog-empty' only) — `source.read_degraded()`, when the
       source exposes it (`GithubSource` does; nothing else needs to — `getattr(..., lambda: False)`
       defaults any other source to "not degraded"), distinguishes a genuinely drained backlog from
       a masked read failure (`sources.py`'s own `_fetch_pending` returns `(0, None)`/`(N, None)`
       for both). A degraded read skips the emit entirely: an honest absence, never a possibly-false
       'backlog-empty' row. Checked BEFORE the dedupe claim below on purpose: a degraded read must
       never burn that drain's claim slot, or a later, genuinely successful read in the SAME drain
       (a retried poll) would find the slot already taken and stay silently unrecorded too. The
       `BUDGET` branch needs no equivalent guard — `_budget_reason()` is a deterministic local
       computation over `state.load_cursor`, independent of the (possibly-failing) remote backlog
       read; `_next()` only ever reaches the budget check after a goal was already found via a
       successful read.
    3. DEDUPE — `state.claim_run_stop`, keyed on `(run_id, run_started_at)`, not the bare run id
       alone (see its own docstring for why: `run_id` is stable across a whole `supervise.sh`
       worker's lifetime, INCLUDING every budget-stop-then-relaunch cycle, so a bare-id key would
       silently drop every terminal event after the first one ever reached under that id). Repeated
       `next` polling within one unchanged drain (no intervening `start`/`start-run`) shares one
       `run_started_at` and so dedupes to one row — the original concern in #905's own issue body
       ("the polled 'next' verb... would emit a duplicate... on every idle poll"). A relaunch
       (fresh `run_started_at`) claims its own new row."""
    run_id = state.run_identity()
    if run_id is None:
        return
    if reason_class == "backlog-empty" and getattr(source, "read_degraded", lambda: False)():
        return
    run_started_at = state.load_cursor(sdlc_dir)["run_started_at"]
    if not state.claim_run_stop(sdlc_dir, run_id, run_started_at):
        return
    _emit_run_stop(sdlc_dir, config, reason_class, why=why)


def run_loop(sdlc_dir, run_goal):
    session_pid = os.getpid()
    # A new in-process run can reuse a PID after a prior run has not yet cleaned up.  Give this
    # invocation its own ownership token, so its eventual cleanup cannot erase that successor.
    session_generation = session_start(sdlc_dir, session_pid, generation=uuid.uuid4().hex)
    state.start_run(sdlc_dir)                       # reset per-run budget (resume-safe)
    config = state.load_config(sdlc_dir)
    _ensure_watcher(sdlc_dir, config)               # a loop trigger keeps the ledger flowing on its own
    _ensure_ledger_delivery(sdlc_dir, config)        # ...and notices when that flow has stalled (#2393)
    source = sources.get_source(sdlc_dir, config)   # one source per run (e.g. github labels ensured once)
    done = parked = failed = review = 0
    poisoned = set()      # F4: goals neither record attempt below could record — skipped for the
                          # REST OF THIS RUN so a doubly-failing source can't spin on one goal forever
    while True:
        kind, goal = _next(sdlc_dir, source, config, extra_skip=poisoned,
                           session_pid=session_pid)
        if kind == "DONE":
            # #905: _next() itself already emitted (gated/deduped/degradation-checked) before
            # returning — nothing to do here but record the outcome for THIS function's own caller.
            stopped = "backlog-empty"
            break
        if kind == "BUDGET":
            stopped = "budget"
            break
        if kind == "HANDOFF":
            stopped = "handoff"
            break
        result, detail = run_goal(goal)
        # #232: the programmatic driver never passes through the CLI `record done` refusal, so the
        # same rule is applied here: a `done` whose PR is not confirmed merged is recorded `review`
        # (open / unreadable) or `parked` (closed unmerged), never `done`.
        if result == "done" and work.enabled(config):
            refusal = work.done_refusal(sdlc_dir, config, goal)
            if refusal:
                result, detail = (("parked", refusal) if "closed without merging" in refusal
                                  else ("review", ""))
        try:
            # #1201: `_record` itself now downgrades a raising `source.complete()` to a recorded
            # park internally (the single chokepoint both this loop and the CLI `record` verb share
            # — see its own docstring) and returns the outcome it ACTUALLY recorded, which may
            # differ from the `result` just passed in. Trust that return value for this run's
            # counters rather than re-deriving "did complete() fail?" a second, independent way —
            # exactly the duplication issue #1201 calls out avoiding.
            outcome = _record(sdlc_dir, source, goal, result, detail)
        except Exception as exc:
            # F4: this is now the BACKSTOP for whatever `_record`'s own internal downgrade could not
            # absorb — e.g. `source.park()` itself also raising right after the downgrade, or any
            # other source-op failure while recording ONE goal. Must never abort the whole drain —
            # park-and-continue is the loop's core promise. Downgrade to a recorded PARK, never a
            # silently-claimed "done": if the remote couldn't confirm completion, the issue may
            # still be open and re-pickable next run, so the local record must not claim otherwise.
            try:
                outcome = _record(sdlc_dir, source, goal, "parked",
                                   f"source error recording '{result}' ({exc})")
            except Exception:
                # POST-REVIEW FIX: even the park-record failed — this goal cannot be reliably
                # recorded at all. Without this, `source.next_pending` would re-serve it forever
                # (nothing ever de-listed it): `_next` -> `run_goal` -> both records fail -> `_next`
                # picks the SAME goal again, spinning run_loop on it at ~100% CPU with no
                # termination — an independent review reproduced this empirically. That silently
                # defeats `max_iterations` (worse than the original crash: loud and bounded beats
                # silent and unbounded). Poison it for this run only — a fresh run may retry it,
                # the goal itself is untouched (still exactly as pending as before this attempt).
                poisoned.add(goal)
                outcome = "parked"
        done += (outcome == "done")
        failed += (outcome == "failed")
        review += (outcome == "review")          # #255 (9): awaiting merge is not a park
        parked += (outcome not in ("done", "failed", "review"))
    result = {"done": done, "parked": parked, "failed": failed, "review": review,
              "iterations": state.load_cursor(sdlc_dir)["iteration"], "stopped": stopped}
    session_end(sdlc_dir, session_pid, generation=session_generation)
    return result


#: #541: the flags this module's own CLI verbs ever hand a real value to -- every EVENTS-stream
#: field this file can emit (`spend`/`emit`, any `EVENT_FIELDS` kind) or log to the local action
#: trace (`log`, any `AGENT_FIELDS` kind), plus the handful of narrow structured flags (`pid`,
#: `thread`, `session-pid`, `skip`). Computed from `ledger.EVENT_FIELDS`/`actionlog.AGENT_FIELDS`
#: themselves, not re-listed, so this set can never drift from the real field vocabulary.
_VALUE_FLAGS = frozenset(
    {"pid", "thread", "session-pid", "session-generation", "skip", "feature"}
    | {f for fields in ledger.EVENT_FIELDS.values() for f in fields}
    | {f for fields in actionlog.AGENT_FIELDS.values() for f in fields}
)


def _flags(argv):
    """`--name value` / bare `--flag` (-> `"true"`) scanner, plus `--name=value` (unambiguous for
    ANY flag) and unconditional-consume for this module's own known value-taking flags
    (`_VALUE_FLAGS`) -- #541: a value that itself starts with '--' used to be silently swallowed:
    the flag landed on the `"true"` sentinel and the value's own text was misparsed as a SECOND,
    garbage flag. A flag name is never legitimately whitespace-bearing -- that shape is always
    leaked prose from a value the old heuristic failed to consume, never a flag a caller meant to
    pass, so it is dropped instead of kept as a nonsense key. A local copy of `ledger.py`'s own
    `_flags` idiom, not an import: it's a small idiom and `loop.py`/`ledger.py` stay decoupled --
    neither imports the other's private helpers today."""
    out = {}
    i = 0
    while i < len(argv):
        token = argv[i]
        if token.startswith("--"):
            name, eq, value = token[2:].partition("=")
            if eq:                                       # --name=value: always unambiguous
                if " " not in name:                       # same never-a-real-flag rule as below
                    out[name] = value
            elif name in _VALUE_FLAGS:                    # known value-taking flag: consume unconditionally
                if i + 1 < len(argv):
                    out[name] = argv[i + 1]
                    i += 2
                    continue
                out[name] = "true"                        # nothing left to consume
            elif i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                out[name] = argv[i + 1]
                i += 2
                continue
            elif " " not in name:
                out[name] = "true"
            # else: whitespace in `name` -- never a real flag; drop rather than keep a nonsense key
        i += 1
    return out


def _cli_skip(argv_tail):
    """Parse `--skip a,b,c` (comma-separated goal identifiers) off a CLI tail into a set, for `next`
    /`next-batch`'s `extra_skip` (F10.5-3/#375) — empty when absent, so a caller that never passes
    it behaves exactly as before this flag existed. Whitespace around each entry is stripped and
    empty entries dropped, so a trailing comma or accidental space never turns into a phantom
    goal id nothing will ever match. A bare `--skip` with no value (`_flags`'s "true" sentinel for
    a flag nothing follows) is treated the same as absent, not as a literal goal named "true"."""
    raw = _flags(argv_tail).get("skip", "")
    if raw == "true":
        return set()
    return {item.strip() for item in raw.split(",") if item.strip()}


def _cli_feature(argv_tail, source):
    """#1661: parse `--feature <name>` off a CLI tail and confine `source` to that unit.

    -> the unit name, or None when the flag is absent. Raises `sources.FeatureScopeError` for a
    name that could never be a unit, and for a backlog that has no unit to scope to -- both are
    turned into one stderr line and exit 2 by the caller, BEFORE any query is made.

    A BARE `--feature` IS ITS OWN REFUSAL, and it is not a hypothetical: `_flags` hands a valueless
    flag the string `"true"`, and `true` is a perfectly legal unit name (`features._is_unit_name`
    accepts it), so without this branch `--feature` alone would validate cleanly and quietly scope
    the whole run to `feature:true` -- a label no repo has, which reads as a drained unit. Note the
    asymmetry with `_cli_skip` directly above, which treats its own `"true"` sentinel as ABSENT:
    that is right for a filter whose empty value means "exclude nothing", and wrong here, where an
    unusable value means the operator asked for a confinement they are not getting.

    THE SCOPE LIVES ON THE SOURCE, not in a parameter threaded through `_next`. `next_batch` and
    `run_loop` both drive `_next` and neither would have to change, so a scope carried in an
    argument would be one refactor away from a path that quietly picks unscoped. Everything that
    can hand the loop a goal already goes through the source."""
    raw = _flags(argv_tail).get("feature")
    if raw is None:
        return None
    if raw == "true":
        raise sources.FeatureScopeError(
            "--feature needs a unit name (e.g. --feature voice-interview). Nothing was queried.")
    return sources.scope_to_feature(source, raw)


def _phase_gate_notice(sdlc_dir, config, kind, payload):
    """#684: at the moment a goal is picked, say on stderr (where the orchestrator reads it) that the phase
    record is ENFORCED for it. A live run that never saw this ran the whole goal inline and was refused at
    `done`; the instruction in SKILL.md is read once, this line is read at every pick. Silent when the gate
    is off; never fails a pick."""
    try:
        if kind == "goal" and work.phase_gate_on(config):
            print(f"sigma: phase record is ENFORCED for {payload}: run research, plan, plan-review, implement, "
                  "review and retro each as its OWN dispatched subagent, never inline, and bracket each with "
                  "phase_report.py start/end; `record done` is REFUSED otherwise. `loop.py phases "
                  f"{sdlc_dir} <goal>` names the next step.", file=sys.stderr)
    except Exception:                       # noqa: BLE001 - a notice never costs a pick
        pass


def _print_pick(kind, payload):
    """Shared by `next`/`next-batch`'s own CLI dispatch (#411): prints exactly what each already
    printed before this fix for `"goal"`/`"DONE"` (`payload`/`kind` respectively) — UNCHANGED, on
    stdout. For `"BUDGET"`, stdout STILL gets the bare token alone, on its own line: a real, live
    consumer (`supervise_classify.py`'s own `_BUDGET` pattern, `^\\s*BUDGET\\s*$`, is an EXACT-LINE
    match) and this file's own `test_cli_start_next_record_and_budget` both depend on that exact
    line, so it is never touched. The diagnostic `_next`/`next_batch` now carry in `payload` (which
    ceiling tripped and the observed-vs-configured numbers, #411) goes to STDERR instead — the same
    channel `_surface_inbox`'s own LEDGER INBOX block already uses for information that must not
    contaminate stdout. `supervise.sh` merges both streams (`$CMD > "$RUNOUT" 2>&1`) before handing
    them to the classifier, so the diagnostic still reaches it — `re.search` finds the bare `BUDGET`
    line regardless of what a LATER line says (see test_supervise.py's own consumer-check test).

    #1084: `"DONE"` now carries the same stderr-diagnostic treatment as `"BUDGET"` — stdout is
    UNCHANGED either way (still the bare `DONE` token alone, on its own line: `supervise_classify.
    py`'s own `_DONE` pattern, `^\\s*DONE\\s*$`, and this file's own DONE-stdout tests both depend
    on that exact line). When `payload` is a degraded-read reason string, it rides stderr as
    `DONE (<reason>)`, parallel to `BUDGET (<reason>)` and picked up by the new `_DONE_DEGRADED`
    pattern in `supervise_classify.py` (checked before `_DONE` there)."""
    if kind == "goal":
        print(payload)
        return
    print(kind)
    if payload and kind in ("BUDGET", "DONE", "HANDOFF"):
        print(f"{kind} ({payload})", file=sys.stderr)


#: #140 (amendment A): the ONLY kinds `emit` will write — a POSITIVE allowlist, not merely
#: "whatever ledger.EVENT_KINDS accepts". `verify`/`slice`/`park`/`scan` are Class-1 events
#: written exclusively by deterministic code (verify_goal, slices.py, pipeline.py, `record`'s
#: own park path) — an agent typing `emit ... verify --ok true --exit 0` must never be able to
#: fabricate a "tests passed" record byte-indistinguishable from a real one. `emit` only ever
#: owns the four kinds #140's SKILL.md prose actually instructs.
#:
#: issue #1030 adds a fifth: `model_choice`, written by `skills/sigma-model/scripts/predict.py`'s
#: own `resolve`/`resolve_step` shelling out to this same `emit` verb, before returning, so a
#: goal's predicted model tier reaches the ledger unconditionally rather than depending on a
#: calling skill's SKILL.md remembering a second instruction (the exact pattern #1013 found
#: failing for `sigma-retro`'s own prose-only `emit retro` line: zero events recorded across 559
#: ledger files). NOT given reliability class 1 downstream despite being code-driven: unlike verify/slice/
#: park/scan above, `model_choice` IS reachable through this same agent-facing `emit` CLI (that is
#: the whole point -- predict.py calls the SAME command an agent could type by hand), so an agent
#: typing `emit ... model_choice --model opus --signal "fake"` directly is exactly as possible as
#: it is for `phase`/`gate`/`retro`/`spend` above -- marking it class 1 would reopen the identical
#: forgery hole this allowlist exists to close. Stays at the class-2 default, same as its four
#: siblings here.
_EMIT_KINDS = ("phase", "gate", "retro", "spend", "model_choice")

#: Within `_EMIT_KINDS`, `gate` is narrowed AGAIN. `ledger.GATE_KINDS` has 13 members; the ones
#: written at all (merge, decision, code_review, post_review, test_trust — and, since #910,
#: risk_migration/risk_contract/risk_security) are emitted deterministically by shipped code, from a
#: measured verdict — letting `emit` write `--gate merge --verdict pass` would forge a Class-1 "this
#: PR merged clean" record. #140 owns exactly two: the plan-review verdict and the periodic
#: alignment check.
#:
#: #910 DELIBERATELY DID NOT WIDEN THIS. Its emitter (`work.py`'s `_emit_risk_gates`) derives both
#: the gate and the verdict from risk-detect.sh's output and takes neither from an argument, so it
#: needed no agent-typable `--gate risk_security` to exist — and adding one would hand an agent the
#: forged `risk_security pass` this allowlist exists to prevent. Pinned by
#: tests/test_risk_gate_events.py::test_loop_emit_still_refuses_a_hand_typed_risk_gate.
#: `risk_release` and `risk_debug` remain unwritten anywhere: no detector produces them.
_EMIT_GATE_KINDS = ("plan_review", "alignment")


def _validate_event(kind, flags, kind_allowlist=None):
    """The one refusal check shared by `emit` and the extended `spend` verb's event path (#140
    PR-review findings 1+2). `spend` calls this with `kind_allowlist=None` — it legitimately
    always writes the `spend` kind, chosen by code, never by an agent-supplied argument, so
    there is nothing to allowlist there; `emit` passes `_EMIT_KINDS` because its `kind` IS
    agent-supplied.

    Checks, in order:
      1. kind allowlist (only when `kind_allowlist` is given)
      2. unknown flag NAMES against `ledger.EVENT_FIELDS[kind]`
      3. #141: a raw newline in ANY flag value — not only the declared prose fields (`why`/
         `model`). Scoping to "any flag" needs no second declared list here, is a strict superset
         of "reject newlines in why/model", and changes nothing for a field that would already
         fail the vocabulary checks below on a `\n`-corrupted value. This is deliberately a HARD
         REJECT (exit 2, nothing written) — unlike `append()`'s own flatten-only treatment, which
         must never reject because it also sits behind the three deterministic, fail-open call
         sites (a hook's `deny`, an autonomous park). `emit`/`spend` are synchronous, explicitly-
         typed CLI verbs that already return an exit code for bad input, so a hard rule is safe
         here specifically. Uses `ledger.reject_newline` — the shared helper `work.py`'s
         post-review branch also calls, so the two never drift into near-identical hand-written
         copies of the same rule again.
      4. POST-REVIEW FIX (THE LEAK): a declared-NUMERIC field whose value does not parse as an
         int, or a declared-BOOL field whose value isn't a recognised spelling — checked by FIELD
         name against `ledger.EVENT_NUMERIC_FIELDS[kind]` / `ledger.EVENT_BOOL_FIELDS[kind]`. An
         independent review proved by execution that `tokens_in`/`cycle`/`debt_count` (declared
         "safe" only because they weren't prose) were plain CLI strings with ZERO shape
         enforcement — a payload with no literal newline sailed through with a full secret inside.
         This is the CLI's clear, actionable half of the fix (exit 2, a usable message); the
         `append()` chokepoint enforces the same rule again as the fail-open backstop for the call
         sites that never reach a CLI at all.
      5. out-of-vocabulary VALUES, checked by FIELD name (not by `kind`) for every
         vocabulary-bearing field present in `flags` — `phase`/`gate`/`verdict`/`grade`/`state`.
         By-field (not by-kind) is what closes finding 1: `EVENT_FIELDS["spend"]` carries a
         `phase` field with the identical `PHASE_KINDS` vocabulary as `phase`-kind events, so a
         future kind that also carries `phase` inherits this check automatically instead of
         needing its own `kind == "..."` branch.

    Returns an error message (no "emit:"/"spend:" prefix — the caller adds its own), or None."""
    if kind_allowlist is not None and kind not in kind_allowlist:
        return f"unknown kind {kind!r} (expected one of {', '.join(kind_allowlist)})"
    allowed = ledger.EVENT_FIELDS[kind]
    bad = sorted(set(flags) - set(allowed))
    if bad:
        return (f"unknown flag(s) {', '.join(bad)} for kind {kind!r} "
                f"(expected one of {', '.join(allowed)})")
    bad_nl = [msg for msg in (ledger.reject_newline(v, f"--{n}") for n, v in flags.items()) if msg]
    if bad_nl:
        return "; ".join(bad_nl)
    bad_numeric = sorted(n for n in flags if n in ledger.EVENT_NUMERIC_FIELDS.get(kind, ())
                          and not ledger._looks_numeric(flags[n]))
    if bad_numeric:
        return (f"{', '.join('--' + n for n in bad_numeric)} must be a whole number for kind "
                f"{kind!r} (the events stream stores counts/durations, not free text)")
    bad_bool = sorted(n for n in flags if n in ledger.EVENT_BOOL_FIELDS.get(kind, ())
                       and not ledger._looks_bool(flags[n]))
    if bad_bool:
        return (f"{', '.join('--' + n for n in bad_bool)} must be true/false for kind {kind!r}")
    if "phase" in flags and flags["phase"] not in ledger.PHASE_KINDS:
        return (f"unknown phase {flags['phase']!r} "
                f"(expected one of {', '.join(ledger.PHASE_KINDS)})")
    if "gate" in flags and flags["gate"] not in _EMIT_GATE_KINDS:
        return (f"unknown gate {flags['gate']!r} "
                f"(expected one of {', '.join(_EMIT_GATE_KINDS)})")
    if "verdict" in flags and flags["verdict"] not in ledger.VERDICTS:
        return (f"unknown verdict {flags['verdict']!r} "
                f"(expected one of {', '.join(ledger.VERDICTS)})")
    if "grade" in flags and flags["grade"] not in ledger.RETRO_GRADES:
        return (f"unknown grade {flags['grade']!r} "
                f"(expected one of {', '.join(ledger.RETRO_GRADES)})")
    if "state" in flags and flags["state"] not in ("start", "end"):
        return f"unknown state {flags['state']!r} (expected start or end)"
    if kind == "model_choice" and flags.get("model") not in {"haiku", "sonnet", "opus", "fable"}:
        return "model_choice --model must be a portable model tier"
    return None


USAGE = ("usage: loop.py start <dir> [--session-pid PID] | start-run <dir> | "
         "next <dir> [--skip a,b] [--feature NAME] [--session-pid PID] | "
         "next-batch <dir> [--skip a,b] [--feature NAME] [--session-pid PID] | "
         "session-active <dir> | prune-state <dir> [--dry-run] [--keep-days N] [--limit N] | session-end <dir> [--session-pid PID] | "
         "claim <dir> <goal> [--session-pid PID] | "
         "agent-start <dir> <goal> --pid PID [--thread T] | agent-reclaim <dir> <goal> [--thread T] | "
         "phases <dir> <goal> | waive-phases <dir> <goal> research,retro --reason <why> | "
         "precheck <dir> <goal> | "
         "qc <dir> <goal> | decompose-check <dir> <goal> | design-check <dir> <goal> | "
          "spend-approval <dir> <goal> --action \"<text>\" [--class <name>] | "
         "mark-designed <dir> <goal> | feature-frontier <dir> <unit> | "
         "note <dir> <goal> <text> | "
         "record <dir> <goal> done|review|parked|failed [reason] | reconcile-merges <dir> | "
         "record <dir> <goal> parked \"<text>\" --qkind <kind> | "
         "agent-beat <dir> <goal> | "
         "release <dir> <goal> [reason] | "
         "spend <dir> <tokens> [goal] [--k v ...] | emit <dir> <goal> <kind> [--k v ...] | "
         "log <dir> <goal> <kind> [--thread T] [--k v ...] | "
         "escalate <dir> <goal> <tier> --after plan-review|code-review|pr-review | "
         "escalate <dir> <goal> --show | "
         "verify <dir> <goal>")


_ESCALATE_USAGE = ("usage: loop.py escalate <dir> <goal> <current-tier> "
                    "--after plan-review|code-review|pr-review | escalate <dir> <goal> --show")


def _codex_mapping_problem(sdlc_dir, tier, effort=None):
    """None when `predict.py host-model codex <tier>` preserves a valid selected effort, else
    the reason. The same two checks `_predict_model_choice_at_pick` makes (a sibling process, never
    an import: skills do not import each other's Python), so a bad `model_host_overrides.codex`
    leaves no `model_choice` row and no floor for a tier a Codex dispatch would refuse. Fails
    CLOSED: a resolver that cannot run is a refusal, never a pass."""
    predict_py = _HERE.parent.parent / "sigma-model" / "scripts" / "predict.py"
    try:
        argv = [sys.executable, str(predict_py), "host-model", "codex", tier, str(sdlc_dir)]
        if effort is not None:
            argv += ["--effort", effort]
        host = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"the host-model resolver could not run: {exc}"
    if host.returncode != 0:
        return ((host.stderr or "").strip().splitlines()[-1:] or [f"exit {host.returncode}"])[0]
    try:
        _parse_codex_host_model(host.stdout)
    except ValueError as exc:
        return str(exc)
    return None


def _escalation_floor_path(sdlc_dir, goal):
    """`<sdlc>/state/escalation/<stem>.json` -- the goal's escalation floor (#2828). Same stem and
    same unsafe-goal refusal as the action log's own per-goal file (`actionlog.log_path`)."""
    stem = work.stem(goal)
    reason = state.unsafe_goal_reason(stem)
    if reason:
        raise ValueError(f"unsafe goal {goal!r}: {reason}")
    return pathlib.Path(sdlc_dir) / "state" / "escalation" / f"{stem}.json"


def _escalate_show(sdlc_dir, goal):
    """`loop.py escalate <dir> <goal> --show`: the tier this goal was escalated to, or `none`. A
    later phase's dispatch reads its ceiling as the higher of the pick-time tier and this, so an
    escalation outlives the phase that earned it and survives a resume. Clamped to the CURRENT
    price ceiling, and `none` unless `model_selection` is "auto". Writes nothing."""
    te = _load("tier_escalation")
    try:
        path = _escalation_floor_path(sdlc_dir, goal)
    except ValueError as exc:
        print(f"loop.py escalate: {exc}", file=sys.stderr)
        return 2
    try:
        entries = actionlog.read_goal(sdlc_dir, goal)
    except Exception:                               # noqa: BLE001 - memory is best-effort
        entries = []
    print(te.shown_tier(_host_model_config(state.load_config(sdlc_dir)), entries,
                        te.read_floor(path)) or "none")
    return 0


def _escalate(sdlc_dir, goal, current, rest):
    """#2828: `loop.py escalate` -- the one sanctioned move when a review sends work back and the
    tier it ran at cannot converge the revision. The decision lives in `tier_escalation.py` (pure,
    host-agnostic); this is the CLI shell around it plus the recording.

    stdout's FIRST WORD is the answer, the same read-the-first-word convention `work.py merge` uses:
      `ESCALATE <tier> effort=<e>` (exit 0) -- re-dispatch the phase fresh at <tier>; recorded as
                        `model_choice`.
      `CEILING <tier>`  (exit 3) -- no higher tier within the price ceiling; park/fail rules apply.
      `OFF`             (exit 3) -- `model_selection` is not "auto"; there is no tier to raise.
    Bad input (unknown tier, a missing or unknown `--after`, any other argument) exits 2 and writes
    nothing. `--after` is REQUIRED: a default would silently label a code-review escalation as a
    plan-review one in the recorded signal.
    Sigma: an ESCALATE whose target tier the Codex host mapping refuses also exits 2 with nothing
    written -- the mapping is validated before any `model_choice` is recorded, as `predict.resolve` does.
    Exit 3 rather than 0 at the ceiling so a caller that only checks the status cannot mistake "no
    higher tier" for "escalated"."""
    after = None
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--after" and i + 1 < len(rest):
            after, i = rest[i + 1], i + 2
        elif tok == "--after":
            print(f"loop.py escalate: --after needs a value\n{_ESCALATE_USAGE}", file=sys.stderr)
            return 2
        elif tok.startswith("--after="):
            after, i = tok.partition("=")[2], i + 1
        else:
            print(f"loop.py escalate: unexpected argument {tok!r}\n{_ESCALATE_USAGE}",
                  file=sys.stderr)
            return 2
    if after is None:
        print(f"loop.py escalate: --after is required\n{_ESCALATE_USAGE}", file=sys.stderr)
        return 2
    te = _load("tier_escalation")
    config = _host_model_config(state.load_config(sdlc_dir))
    try:
        floor_path = _escalation_floor_path(sdlc_dir, goal)
    except ValueError as exc:
        print(f"loop.py escalate: {exc}", file=sys.stderr)
        return 2
    try:
        entries = actionlog.read_goal(sdlc_dir, goal)
    except Exception:                               # noqa: BLE001 - memory is best-effort
        entries = []
    floor = te.read_floor(floor_path)
    try:
        verdict, tier, message = te.decide(config, current, after, entries, floor)
    except ValueError as exc:
        print(f"loop.py escalate: {exc}\n{_ESCALATE_USAGE}", file=sys.stderr)
        return 2
    if verdict == te.ESCALATE:
        problem = _codex_mapping_problem(sdlc_dir, tier, te.EFFORT[tier])
        if problem:
            print(f"loop.py escalate: Codex host mapping refused for {tier} ({problem}); nothing "
                  f"was recorded. Fix model_host_overrides.codex in config.json and run it again.",
                  file=sys.stderr)
            return 2
        frm = te.from_tier(config, current, entries, floor)
        try:
            te.write_floor(floor_path, tier, after, frm)
        except OSError as exc:
            # The floor is what bounds the ladder, so an unwritable one is said out loud -- the
            # answer still stands (a re-dispatch at a higher tier is the correct move either way).
            print(f"loop.py escalate: escalation floor not saved ({exc}); pass {tier} as "
                  f"<current-tier> next time", file=sys.stderr)
        te.record(sdlc_dir, goal, config, frm, tier, after, ledger, actionlog)
        print(f"{te.ESCALATE} {tier} effort={te.EFFORT[tier]} -- {te.signal(after, frm)}; {message}")
        return 0
    print(f"{verdict} {tier} -- {message}" if verdict == te.CEILING else f"{verdict} -- {message}")
    return 3


def main(argv, *, _merge_lock_held=False):
    """Thin wrapper around `_dispatch` — the ONE place a never-`/sigma-init`'d `.sdlc` dir (no
    config.json at all) turns into a clear one-line stderr message instead of a raw traceback
    (#403). Every verb below calls `state.load_config` at some point before its own logic runs;
    rather than guard each call site separately, `state.load_config` itself raises the distinctly-
    typed `state.ConfigMissing` on a missing file (see its docstring), and this is the single
    catch — so `next`/`next-batch`/`start`/`session-active`, and every other verb that reads
    config, all get the same graceful handling for free, not just whichever one a bug report
    happened to name."""
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    try:
        if len(argv) > 2 and argv[1] != "prune-state" and os.path.isdir(argv[2]):
            # #708: one choke point, every verb. `prune-state` is exempt: it only DELETES, with its
            # own never-follow guard (tests/test_state_retention.py), and is how a bad tree is cleaned.
            state.refuse_symlinked_tree(argv[2])
        # #324: with work enabled (the pass's scope), lock before any done side effect.
        # Work-disabled local recorders keep their existing concurrent cursor-update contract.
        # Re-entry is lexical, not a process-global exemption that would let another caller through.
        if (not _merge_lock_held and len(argv) >= 5 and argv[1] == "record"
                and argv[4] == "done" and work.enabled(state.load_config(argv[2]))):
            with _merge_reconcile_lock(argv[2]) as held:
                if held is not True:
                    why = (f"another merge-reconcile pass or record done holds the lock "
                           f"(pid {_merge_lock_holder(argv[2])})" if held is False
                           else "exclusive file locking is unavailable")
                    print(f"REFUSED: record done -- {why}; check the goal status and retry "
                          "after the holder finishes or locking is restored", file=sys.stderr)
                    return 4
                return main(argv, _merge_lock_held=True)
        return _dispatch(argv)
    except state.UnsafeStatePath as exc:
        print(f"loop.py: {exc}", file=sys.stderr)
        return 2
    except state.ConfigMissing as exc:
        print(f"loop.py: {exc}", file=sys.stderr)
        return 2


def _note_unlabelled_backlog(source):
    """#230: a bare `DONE` on a repo where NO open issue carries the goal label reads as "the loop
    finished"; it means "nothing was ever queued". Say so, on stderr (stdout's bare `DONE` line is
    a contract with `supervise_classify.py`). Only a source that can count (`goal_label_census`,
    GitHub) is asked, only on the genuinely-empty path, and only a MEASURED zero is reported: one
    REST read of one item, spent once per drained run -- never per pick."""
    census = getattr(source, "goal_label_census", None)
    if census is None:
        return
    try:
        count = census()
    except Exception:
        return
    if count == 0:
        label = getattr(source, "goal_label", "sdlc:goal")
        print("loop.py: DONE — 0 issues carry %s — label one to start" % label, file=sys.stderr)


def _bootstrap_github_labels(sdlc_dir, config):
    """#230: in github mode, make sure every label the pick query and the claim depend on exists
    BEFORE the first pick -- `sources.py` picks by a REST query on `sdlc:goal`, so a missing label
    reads as an empty backlog and the loop would report `DONE` forever. Defensive: `/sigma-init
    --github` and `setup.py labels` normally did this already, and then it costs one REST read and
    zero writes (`GitHubSource.ensure_labels_report`). Per-label outcome goes to stderr (stdout of
    `start` stays empty); returns False -- `start` exits 1 -- when any label could not be created,
    so a token without label-write permission is refused loudly instead of starting a loop that
    can never pick. Local mode: no-op, no `gh` call."""
    if ((config.get("discovery") or {}).get("source")) != "github":
        return True
    source = sources.get_source(sdlc_dir, config)
    results = source.ensure_labels_report()
    lines, failed = sources.render_label_report(source.repo, results)
    for line in lines:
        print("loop: " + line.strip(), file=sys.stderr)
    if failed:
        print("loop.py start: refusing to start -- required label(s) missing; see above",
              file=sys.stderr)
    return not failed


def _dispatch(argv):
    if len(argv) >= 2 and argv[1] in ("start", "next", "next-batch", "claim", "session-end"):
        try:
            _session_codex_thread()
        except ValueError as exc:
            print(f"loop.py: {exc}", file=sys.stderr)
            return 2
    # #1962: a goal-scoped verb that means "work has begun here" arms the claim if nothing has
    # claimed this goal yet. ONE site rather than four in-verb calls, because the argv shape is
    # uniform across `_ARMS_CLAIM` -- see that tuple for which verbs qualify and, more importantly,
    # for the four that were deliberately excluded. Fail-open inside `_ensure_claimed`: it can
    # never stop the verb the caller actually asked for.
    if len(argv) >= 4 and argv[1] in _ARMS_CLAIM:
        _ensure_claimed(argv[2], argv[3])
        # #2435: the SAME trigger set, for a different side job -- see `_ensure_unit_tracking`'s own
        # docstring. Cooldown-gated internally, so sharing `_ARMS_CLAIM` costs the steady state one
        # cheap marker `stat()`, not a network read on every one of these calls.
        _ensure_unit_tracking(argv[2], argv[3])
    if len(argv) >= 3 and argv[1] == "start":
        config = state.load_config(argv[2])
        for warning in _config_warnings(config):
            print("loop: " + warning, file=sys.stderr)       # surface the trap up front, not 40 goals in
        _coexist_notice(argv[2], "loop.py start")            # #314: a notice, never a refusal
        coexist = _load("coexist")
        coexist.write_owner(argv[2])                         # Sigma owns this state dir now
        offer = coexist.takeover_line(argv[2])               # the dry-run command; never --apply
        if offer:
            print(offer, file=sys.stderr)
        if not _bootstrap_github_labels(argv[2], config):     # #230: before any session is registered
            return 1
        # #1199: registers a session by DEFAULT now, not only when a caller remembers `--session-
        # pid` (F10.5-4/#377's original opt-in) — so a caller that forgets the flag still gets SOME
        # registration rather than none. #1239 review (finding 1/2): `os.getppid()` here is THIS
        # invocation's own immediate parent, NOT the shell-level `$PPID` `session_start`'s docstring
        # establishes as the actually-stable one across separate tool-style calls (see that
        # docstring for the confirmed distinction) — so this fallback is a last resort, not the
        # mechanism the shipped skill relies on. SKILL.md always passes `--session-pid "$PPID"`
        # explicitly on this call (its shell's own `$PPID`, captured fresh — no cross-call
        # persistence needed since re-reading it keeps giving the same stable value).
        start_flags = _flags(argv[3:])
        session_pid = start_flags.get("session-pid")
        _arm_run_id(session_pid)        # #889: BEFORE the fallback below -- os.getppid() is not stable
        if session_pid is None or session_pid == "true":
            session_pid = os.getppid()
        generation = session_start(argv[2], session_pid,
                                   generation=start_flags.get("session-generation") or uuid.uuid4().hex)
        # A separate `session-end` invocation cannot infer an earlier owner's random token from
        # disk without reopening the successor-deletion race.  Return it for the documented shell
        # gesture and retain it for same-process library callers.
        os.environ["SIGMA_SESSION_GENERATION"] = generation
        print(generation)
        state.start_run(argv[2])
        # #1239 review round 3, finding B: `start` is the OTHER real chokepoint the registry grows
        # through (alongside `_next()`/`next_batch()`, pruned in `_session_in_flight_goals`) — sweep
        # any sibling session that already reads as dead here too, using the config already loaded
        # above, so a long-lived repo's registry directory does not grow without bound just because
        # nothing else ever reads it between runs.
        _prune_dead_session_entries(argv[2], config)
        try:                                      # #464: stale claim locks and old claim markers
            _load("liveness_prune").after_start_or_done(argv[2], config=config)
        except Exception as exc:                  # noqa: BLE001 - housekeeping never blocks a start
            print(f"loop.py start: claim marker sweep skipped ({exc})", file=sys.stderr)
        try:                                      # #465: opt-in sweep of provably finished goal worktrees
            _load("worktree_prune").after_start_or_done(argv[2], config=config)
        except Exception as exc:                  # noqa: BLE001 - housekeeping never blocks a start
            print(f"loop.py start: worktree sweep skipped ({exc})", file=sys.stderr)
        return 0
    # #712: a standalone reset of JUST the run budget cursor -- unlike `start` above, no config-
    # warning prints and no session-marker write, so a mid-session "begin a fresh run" (or an
    # overnight supervisor relaunch that wants one) never has to hand-edit config.json's budget
    # numbers, and never re-triggers `start`'s own once-per-session bootstrap side effects.
    if len(argv) >= 3 and argv[1] == "start-run":
        state.start_run(argv[2]); return 0
    if len(argv) >= 3 and argv[1] == "session-end":          # F10.5-4/#377: routine cleanup on exit
        # #1199: `session_end` now needs to know WHICH session's registry entry to clear (a bare
        # call used to clear the one shared marker, no matter who wrote it). Mirrors `start`'s own
        # `os.getppid()` fallback when `--session-pid` is absent — the same last-resort caveat
        # applies (see `start`'s own comment above): the README's recommended routine prompt passes
        # `--session-pid "$PPID"` explicitly here too, the same value it captured for `start`.
        end_flags = _flags(argv[3:])
        session_pid = end_flags.get("session-pid")
        generation = end_flags.get("session-generation") or os.environ.get("SIGMA_SESSION_GENERATION")
        session_end(argv[2], None if session_pid in (None, "true") else session_pid,
                    generation=None if generation == "true" else generation)
        return 0
    if len(argv) >= 3 and argv[1] == "prune-state":          # #457: closed-goal log/witness retention
        return retention.cli(argv[2:])
    if len(argv) >= 3 and argv[1] == "session-active":       # F10.5-4/#377: routine pre-flight check
        config = state.load_config(argv[2])
        print("ACTIVE" if session_active(argv[2], config) else "FREE")
        return 0
    if len(argv) >= 4 and argv[1] == "agent-start":           # background-agent-death watch marker
        config = state.load_config(argv[2])
        flags = _flags(argv[4:])
        pid = flags.get("pid")
        if pid is None or pid == "true":
            print("loop.py agent-start: --pid is required", file=sys.stderr)
            return 2
        try:
            pid = int(pid)
        except ValueError:
            print(f"loop.py agent-start: --pid {pid!r} is not an integer", file=sys.stderr)
            return 2
        thread = flags.get("thread") or "main"
        reason = _unsafe_thread_reason(thread)
        if reason:
            print(f"loop.py agent-start: --thread {thread!r} is invalid: {reason}", file=sys.stderr)
            return 2
        if agent_start(argv[2], argv[3], pid, config, thread=thread) is False:
            print("loop.py agent-start: cannot register agent identity; a live different task "
                  "may own this goal/thread, or its marker is unreadable", file=sys.stderr)
            return 2
        return 0
    if len(argv) >= 4 and argv[1] == "agent-reclaim":         # #2015: identity-checked manual escape
        config = state.load_config(argv[2])                   # hatch -- replaces the old blind `agent-end`
        flags = _flags(argv[4:])
        thread = flags.get("thread")
        thread = None if thread in (None, "true") else thread
        if thread is not None:
            reason = _unsafe_thread_reason(thread)
            if reason:
                print(f"loop.py agent-reclaim: --thread {thread!r} is invalid: {reason}", file=sys.stderr)
                return 2
        result = agent_reclaim(argv[2], argv[3], config, thread=thread)
        for t, pid in result["reclaimed"]:
            print(f"reclaimed {t} (pid {pid}, confirmed dead)")
        for t, pid in result["blocked"]:
            print(f"blocked {t} (pid {pid}, still alive -- not touched)")
        if not result["reclaimed"] and not result["blocked"]:
            print(f"nothing to reclaim for thread {thread!r}" if thread
                  else "nothing registered for this goal")
        return 1 if (not result["reclaimed"] and result["blocked"]) else 0
    if len(argv) >= 3 and argv[1] == "next":
        config = state.load_config(argv[2])
        # #1661: the source is built and scoped BEFORE the watchers and the inbox read below, so a
        # `--feature` this run cannot honour costs nothing at all -- no watcher spawned, no query
        # made, no side effect to undo. `get_source` is a pure constructor; moving it up changes no
        # call and no order.
        source = sources.get_source(argv[2], config)
        try:
            _cli_feature(argv[3:], source)
        except sources.FeatureScopeError as exc:
            print(f"loop.py: {exc}", file=sys.stderr)
            return 2
        _ensure_watcher(argv[2], config)            # every loop trigger keeps the watcher (and the ledger) alive
        _ensure_ledger_delivery(argv[2], config)    # ...and notices when that flow has stalled (#2393)
        _surface_inbox(argv[2])                     # stderr; stdout stays exactly the goal/DONE/BUDGET
        _warn_knowledge_graph(argv[2])              # #2704: stderr, once a day, on every host
        skip = _cli_skip(argv[3:])                  # --skip a,b,c: goals a still-running sibling slot
        # #1239 review finding 1: this verb used to silently DROP `--session-pid` on the floor --
        # `_next()` would then always fall back to its OWN `os.getppid()`, which is THIS CLI
        # process's immediate parent, not the caller's. See `_next`'s own docstring for why that
        # default cannot be trusted for the shipped skill's own dispatch shape.
        session_pid = _flags(argv[3:]).get("session-pid")
        session_pid = None if session_pid in (None, "true") else session_pid
        _arm_run_id(session_pid)                            # #889: attribute a bare session
        kind, payload = _next(argv[2], source, config,
                               extra_skip=skip, session_pid=session_pid)
        _print_pick(kind, payload)
        _phase_gate_notice(argv[2], config, kind, payload)
        return 0
    if len(argv) >= 3 and argv[1] == "next-batch":  # F10.5-3/#375: up to parallel.goals.max_concurrent
        config = state.load_config(argv[2])         # goals to dispatch as concurrent worktree subagents
        source = sources.get_source(argv[2], config)
        try:
            _cli_feature(argv[3:], source)          # #1661: same up-front refusal as `next` above
        except sources.FeatureScopeError as exc:
            print(f"loop.py: {exc}", file=sys.stderr)
            return 2
        _ensure_watcher(argv[2], config)
        _ensure_ledger_delivery(argv[2], config)    # ...and notices when that flow has stalled (#2393)
        _surface_inbox(argv[2])
        _warn_knowledge_graph(argv[2])              # #2704: same, for a batch refill call
        skip = _cli_skip(argv[3:])                  # --skip a,b,c: same, for a batch refill call
        session_pid = _flags(argv[3:]).get("session-pid")   # #1239 review finding 1, same gap as `next`
        session_pid = None if session_pid in (None, "true") else session_pid
        _arm_run_id(session_pid)                            # #889: attribute a bare session
        for kind, payload in next_batch(argv[2], source, config, extra_skip=skip, session_pid=session_pid):
            _print_pick(kind, payload)
            _phase_gate_notice(argv[2], config, kind, payload)
        return 0
    if len(argv) >= 3 and argv[1] == "reconcile-merges":   # #232: close goals whose PR has merged
        config = state.load_config(argv[2])
        # The explicit gesture re-reads every flagged PR (still at most MERGE_RECONCILE_MAX_PER_PASS);
        # only the automatic triggers (`next`, the watch tick) honour the re-check interval.
        for goal, outcome, pr in _reconcile_awaiting_merges(argv[2], config, min_interval=0):
            what = "merged" if outcome == "done" else "closed without merging"
            print(f"{goal} {outcome} (PR #{pr} {what})")
        return 0
    if len(argv) >= 4 and argv[1] == "qc":          # board-only: move a goal to QC at the Review phase
        config = state.load_config(argv[2])
        agent_heartbeat_all(argv[2], argv[3])   # #1391: in-flight proof of life, see `note`
        sources.get_source(argv[2], config).mark_qc(argv[3]); return 0
    if len(argv) >= 4 and argv[1] == "precheck":    # opt-in pre-work backlog cross-check (fail-open)
        config = state.load_config(argv[2])
        print(precheck(argv[2], argv[3], config, sources.get_source(argv[2], config)))
        return 0
    if len(argv) >= 4 and argv[1] == "decompose-check":   # opt-in oversized-goal classifier (fail-open)
        config = state.load_config(argv[2])
        print(decompose_check(argv[2], argv[3], config, sources.get_source(argv[2], config)))
        return 0
    if len(argv) >= 4 and argv[1] == "design-check":   # opt-in undesigned-goal retrofit gate (fail-open)
        config = state.load_config(argv[2])
        print(design_check(argv[2], argv[3], config, sources.get_source(argv[2], config)))
        return 0
    if len(argv) >= 4 and argv[1] == "spend-approval":   # #722: exempt THIS goal's park-on-spend
        # Deliberately NOT in `_ARMS_CLAIM`: asking whether a step is approved is not "work has
        # begun here". Fail-CLOSED: every unreadable/malformed/unauthorised path prints DENIED and
        # exits 3 (park as before); only a fully authenticated, audited marker exits 0. See
        # spend_approval.py's docstring for the guards and what this verb does NOT do.
        config = state.load_config(argv[2])
        out, code = _load("spend_approval").run_verb(
            argv[2], argv[3], argv[4:], config, sources.get_source(argv[2], config))
        print(out)
        return code
    if len(argv) >= 4 and argv[1] == "mark-designed":   # #1826: goal-review's write-back for sdlc:designed
        config = state.load_config(argv[2])
        src = sources.get_source(argv[2], config)
        if not hasattr(src, "mark_designed"):
            print("UNSUPPORTED")   # e.g. LocalSource -- no story/epic tickets, nothing to overlay
            return 0
        print("OK" if src.mark_designed(argv[3]) else "FAILED")
        return 0
    if len(argv) >= 4 and argv[1] == "feature-frontier":   # #2265: read-only unit-scoped ready set
        # A VIEW, never a gate: no claim, no mutation, no interaction with the pick path at all --
        # see feature_frontier.py's own module docstring for the fail-CLOSED posture this verb
        # deliberately takes, a stated divergence from the pick-time dependency gate's fail-open one.
        config = state.load_config(argv[2])
        ff = _load("feature_frontier")
        result = ff.compute(argv[2], argv[3], config=config)
        print(ff.render(result), end="")
        return 1 if result.get("degraded") else 0
    if len(argv) >= 4 and argv[1] == "phases":      # #684: which SDLC phases are recorded, and the next step
        config = state.load_config(argv[2])
        print(_load("phase_gate").format_status(argv[2], config, argv[3]))
        return 0
    if len(argv) >= 5 and argv[1] == "waive-phases":   # #684: the explicit, recorded trivial-goal lever
        config = state.load_config(argv[2])
        flags = _flags(argv[5:])
        try:
            done_ = _load("phase_gate").waive(argv[2], config, argv[3],
                                              [x for x in argv[4].split(",") if x], flags.get("reason", ""))
        except ValueError as exc:
            print(f"loop.py waive-phases: {exc}", file=sys.stderr)
            return 2
        print(f"loop: WAIVED phases {', '.join(done_)} for {argv[3]} -- reason: {flags.get('reason')} "
              "(recorded in the action log; plan, plan-review, implement and review are never waivable)",
              file=sys.stderr)
        return 0
    if len(argv) >= 5 and argv[1] == "note":        # record a journey-log / critical-insight note
        config = state.load_config(argv[2])
        # #1391: a note is proof the agent is alive RIGHT NOW, so it doubles as a free heartbeat.
        # Piggybacking on work the agent already does beats asking agents to remember a new verb —
        # the marker's whole failure mode was that nothing ever refreshed it.
        agent_heartbeat_all(argv[2], argv[3])
        try:
            sources.get_source(argv[2], config).note(argv[3], sys.stdin.read() if (argv[4] == "-" and not sys.stdin.isatty()) else argv[4])   # `-`: stdin (#713)
        except Exception as e:
            # #1986: the OLD shape here printed one easy-to-miss stderr line and always returned 0
            # regardless — for `sigma-goal-review`'s REJECT verdict, this note is THE ENTIRE OUTPUT
            # (§7g: no label, no board move, nothing else), so a failure this quiet left a rejected
            # idea with zero trace anywhere. `source.note` (GitHubSource, #1986) already retried a
            # transient failure itself before this was ever reached — what still lands here survived
            # that and is reported LOUDLY: `FAILED` on stdout, mirroring `mark-designed`'s own
            # OK/FAILED convention that SKILL.md steps already know to read, PLUS a non-zero exit so
            # a caller that only checks the return code (not stderr) still sees it.
            print(f"loop.py note: recording failed after retries: {e}", file=sys.stderr)
            print("FAILED")
            return 1
        print("OK")
        return 0
    if len(argv) >= 4 and argv[1] == "agent-beat":  # #1391: explicit heartbeat for a long quiet stretch
        print("beat" if agent_heartbeat_all(argv[2], argv[3]) else "no marker registered")
        return 0
    if len(argv) >= 5 and argv[1] == "record":
        _coexist_notice(argv[2], "loop.py record", once=True)   # #251/#314: once per run
        config = state.load_config(argv[2])
        # #2041: finishing a goal is the moment a stalled watcher most needs restarting -- it is
        # precisely when there is new activity to publish. `next`/`next-batch`/`verify`/`run_loop`
        # already arm these; `record` did not, so a session that drove goals without the picker
        # (which is the shape #1962 exists to support) left them dead.
        #
        # #2578 NARROWED WHAT "THESE" MEANS. This site once armed a private-side shipper too, and the
        # sentence above was written about a stalled SHIPPER. #2578 removed the private-side
        # starters from the core -- the git hooks are the only revival path for those -- so what is armed
        # here is the ledger watcher and the ledger-delivery check, both core's own.
        #
        # WRAPPED HERE, unlike the other arming sites, and the asymmetry is deliberate. Each helper
        # is already fail-open internally, so this guard is unreachable through them today. But
        # `record` is the one verb whose failure LOSES DATA: #1201 exists because a raising
        # `source.complete()` used to cost a goal its terminal record entirely. Journal
        # bookkeeping must never be able to do the same, however it comes to raise.
        try:
            _ensure_watcher(argv[2], config)
            _ensure_ledger_delivery(argv[2], config)    # ...and notices when that flow has stalled (#2393)
        except Exception as exc:          # noqa: BLE001 - a daemon must never cost a terminal record
            print(f"loop: daemons not armed (non-fatal): {exc}", file=sys.stderr)
        # issue #1013: argv[5] is the freeform positional `reason` only when it does not itself
        # look like a flag -- `record <dir> <goal> done --retro-grade achieved` (no reason) must
        # never let "--retro-grade" become the literal reason text. Mirrors the `spend` verb's own
        # positional-vs-flag disambiguation (amendment C, ~line 1480 below), one position later.
        reason = argv[5] if len(argv) > 5 and not argv[5].startswith("--") else ""
        flags = _flags(argv[6:] if reason else argv[5:])
        qkind = flags.get("qkind")
        if qkind is not None and qkind not in _load("qkind").QKINDS:
            print(f"loop.py record: unknown --qkind {qkind!r} "
                  f"(expected one of {', '.join(_load('qkind').QKINDS)})", file=sys.stderr)
            return 2
        retro_grade = flags.get("retro-grade")
        if retro_grade is not None and retro_grade not in ledger.RETRO_GRADES:
            print(f"loop.py record: unknown grade {retro_grade!r} "
                  f"(expected one of {', '.join(ledger.RETRO_GRADES)})", file=sys.stderr)
            return 2
        # Machine done_when (opt-in): with verify.enforce on, a `done` needs fresh
        # passing evidence from `loop.py verify` — the sigma-verify prose gate, enforced.
        # _enforce_enabled (not a strict `is True`): F17/#342 — `enforce: 1` / `"true"` must not
        # silently skip this gate just because they aren't the literal bool `True`.
        if argv[4] == "done" and _load("hard_stop_guard").enabled(config):
            # decision rubric slice 10: the downstream net. A logged action that matches a hard-stop class and has
            # no recorded permission-use or park is refused. Closed (the default), this block is never entered.
            unrecorded = _load("hard_stop_guard").record_done_net(_load("actionlog").read_goal(argv[2], argv[3]), config)
            if unrecorded:
                print("REFUSED: hard-stop action(s) in the log with no recorded permission or park: "
                      + "; ".join(unrecorded[:3]), file=sys.stderr)
                return 4
        if argv[4] == "done" and _enforce_enabled(config.get("verify") or {}):
            # unsafe goal -> ValueError from _evidence_path (see state.unsafe_goal_reason);
            # caught here (exit 2, matching verify_goal's own unsafe-goal convention) instead of
            # letting it reach `main`'s ConfigMissing-only catch as a raw traceback.
            try:
                refusal = _done_refusal(argv[2], argv[3])
            except ValueError as exc:
                print(f"loop.py record: {exc}", file=sys.stderr)
                return 2
            if refusal:
                try:
                    command = _declared_verify_command(argv[3], config, argv[2])
                except (OSError, ValueError) as exc:
                    print(f"loop.py record: invalid acceptance record: {exc}", file=sys.stderr)
                    return 2
                if command is None:
                    # #228: "run verify first" is the wrong advice when there is nothing to run --
                    # verify would print NO-COMMAND. Name the real cause and the one-line fix.
                    print("REFUSED: no verify command declared for this goal (config "
                          "verify.enforce is on, verify.command is empty, and the goal has no "
                          f"`verify_command`) — {_VERIFY_SET_HINT}", file=sys.stderr)
                    return 4
                print(f"REFUSED: {refusal} — run `loop.py verify {argv[2]} <goal>` first "
                      "(config verify.enforce is on)", file=sys.stderr)
                return 4
        # #684: every SDLC phase must be on the record (research, plan, approved plan-review, implement
        # after it, approved review, retro). Plain Python, so it holds in local-only mode and on every
        # host; gates.phase_record.enabled (scaffold: true) switches it, `loop.py waive-phases` is the
        # recorded lever. The refusal names each missing item and its fix.
        if argv[4] == "done":
            try:
                phase_refusal = work.phase_record_refusal(argv[2], config, argv[3])
            except ValueError as exc:
                print(f"loop.py record: {exc}", file=sys.stderr)
                return 2
            if phase_refusal:
                print(f"REFUSED: {phase_refusal}", file=sys.stderr)
                return 4
            try:
                _rows = _load("actionlog").read_goal(argv[2], argv[3])
                _w = [r for r in _rows if r.get("kind") == "phase_waived"]
                if work.phase_gate_on(config) and any(r.get("kind") == "verdict" and r.get("route") == "inline"
                                                      for r in _rows):
                    print("loop: WARNING: a review route is INLINE here, so the different-agent and "
                          "one-subagent-per-phase checks did not apply to this goal (the phases are recorded, "
                          "not proved independent)", file=sys.stderr)
            except Exception:                # noqa: BLE001 - a diagnostic never costs a terminal record
                _w = []
            if _w:
                print("loop: recording done with WAIVED phases: " + "; ".join(
                    f"{r.get('phase')} ({r.get('reason')})" for r in _w), file=sys.stderr)
        if argv[4] == "done" and not work.enabled(config):
            print("loop: work.enabled is off — this goal's change is only in your working tree; no "
                  "branch/commit/PR was created"
                  + ("." if (config.get("work") or {}).get("_enabled_why")      # #236: chosen
                     else " (run /sigma-init --work on, or set work.enabled)."), file=sys.stderr)
            # #2116: the one configuration where `gates.hard_plan_gate` has NO host-agnostic
            # enforcement point at all. `work.py main()` refuses every verb with `work.enabled` off,
            # so `pr()` -- the check that makes the org lock uniform across hosts -- never runs, and
            # `hooks/plan_gate.sh` is again the only thing enforcing it: Claude Code gets a gate,
            # Cursor and Codex get nothing. AGENTS.md: "Where a guarantee cannot be made on a
            # platform, the code REFUSES loudly rather than proceeding weakly." Saying so here is
            # that refusal, on the only per-goal surface this configuration has.
            #
            # A LOCAL READ, DELIBERATELY -- no `effective_hard_plan_gate`, no subprocess. `record` is
            # the one verb whose failure LOSES DATA (see the daemon block above), and a 5s-bounded
            # spawn on it to improve a diagnostic line is not a trade worth making. The line says
            # what it read, so an adopted checkout is pointed at the resolver rather than told a
            # number this call cannot verify. Fail-open for the same reason the daemons above are.
            try:
                if work.hard_plan_gate_on(work._gate_block(config)):
                    print("loop: gates.hard_plan_gate is on in local config but NOT ENFORCED on "
                          "this checkout — with work.enabled off, `work.py pr` never runs, so only "
                          "the Claude Code hook enforces it and Cursor/Codex get nothing. Set "
                          "work.enabled to enforce it on every host. (Local config only; on a "
                          "centrally-adopted checkout, ask your managed-settings resolver for the "
                          "effective value.)", file=sys.stderr)
            except Exception as exc:      # noqa: BLE001 - a diagnostic must never cost a terminal record
                print(f"loop: hard_plan_gate disclosure skipped (non-fatal): {exc}", file=sys.stderr)
        # #254 -> #232: the PR-state sibling of the verify.enforce refusal above. DONE MEANS MERGED
        # (owner decision on #232): with `work.enabled` and a PR on record, `done` passes only on a
        # PR confirmed merged -- whatever `work.auto_merge` says. Open / unreadable -> record
        # `review`; closed unmerged -> `parked`. See `work.done_refusal`'s docstring. Never raises.
        merged_pr = False
        if argv[4] == "done" and work.enabled(config):
            refusal = work.done_refusal(argv[2], config, argv[3])
            if refusal:
                print(f"REFUSED: {refusal}", file=sys.stderr)
                return 4
            # #255 (1): no refusal with a PR on record means the REST read just confirmed MERGED.
            merged_pr = bool((work._record(argv[2], argv[3]) or {}).get("pr"))
        if argv[4] == "review":
            # #232: the non-terminal "PR awaiting merge" outcome. Meaningless without a PR the loop
            # opened -- refuse rather than silently park (the pre-#232 fall-through for any
            # unrecognised result).
            if not work.enabled(config):
                print("loop.py record: `review` means a PR is awaiting merge, but work.enabled is "
                      "off -- no PR is ever opened here; record done/parked/failed", file=sys.stderr)
                return 2
            try:
                rec = work._record(argv[2], argv[3])
            except ValueError as exc:
                print(f"loop.py record: {exc}", file=sys.stderr)
                return 2
            if not rec or not rec.get("pr"):
                print(f"loop.py record: `review` means a PR is awaiting merge, but no PR is on record "
                      f"for {argv[3]} -- run `work.py pr` first, or record done/parked/failed",
                      file=sys.stderr)
                return 2
        if merged_pr:
            _record(argv[2], sources.get_source(argv[2], config), argv[3], argv[4],
                    reason, retro_grade=retro_grade, merged_pr=True, qkind=qkind); return 0
        _record(argv[2], sources.get_source(argv[2], config), argv[3], argv[4],
                reason, retro_grade=retro_grade, qkind=qkind); return 0
    if len(argv) >= 4 and argv[1] == "claim":       # #1962: claim a goal you already chose
        _coexist_notice(argv[2], "loop.py claim", once=True)    # #251/#314: once per run
        config = state.load_config(argv[2])
        goal = argv[3]
        # `open_claims_detailed` returns {goal: (actor, writer)} -- a TWO-tuple (see its own
        # docstring) -- and the identity pair is ledger.actor(config)/ledger.my_writer(config).
        # `claim_belongs_to_me` is what tells "my own current process" apart from "a different,
        # still-live writer of my own actor"; reusing it means this verb and `_next()` make that
        # call exactly one way, never two.
        holder = ledger.open_claims_detailed(ledger.read_all(argv[2])).get(goal)
        if holder:
            holder_actor, holder_writer = holder
            if not ledger.claim_belongs_to_me(holder_actor, holder_writer,
                                               ledger.actor(config), ledger.my_writer(config)):
                print(f"loop.py: {goal} is already claimed by {holder_actor}", file=sys.stderr)
                return 2
            return 0                    # already mine -- idempotent, never a second claimed event
        session_pid = _flags(argv[4:]).get("session-pid")
        session_pid = None if session_pid in (None, "true") else session_pid
        _arm_run_id(session_pid)                            # #889: attribute a bare session
        _claim(argv[2], sources.get_source(argv[2], config), goal, config, session_pid=session_pid)
        return 0
    if len(argv) >= 4 and argv[1] == "release":     # #841: undo a stale claim -- remove sdlc:in-progress
        config = state.load_config(argv[2])         # (or local status: in_progress) + an audit comment
        # Same positional-vs-flag disambiguation as `record`'s own optional trailing reason —
        # there is no flag `release` recognises today, but the guard costs nothing and stays
        # consistent should one ever be added.
        reason = argv[4] if len(argv) > 4 and not argv[4].startswith("--") else ""
        released = _release(argv[2], sources.get_source(argv[2], config), argv[3], reason)
        # #1107 review, Finding 3: a no-op (goal/issue already done/parked/failed) is not an ERROR
        # -- exit 0 either way, unlike `record done`'s REFUSED/exit-4 convention. A future automated
        # staleness sweep (issue #841's own named follow-up) will call this over MANY goals; treating
        # "nothing to release" as a fatal exit would abort a whole batch over one already-finished
        # goal. `is False` (not falsy) so a legacy test double whose release() has no return
        # statement (implicit None) is never misread as a refusal.
        if released is False:
            print(f"loop.py release: {argv[3]} is already done/parked/failed — no change made",
                  file=sys.stderr)
        return 0
    # issue #1091 (the 4th disposition #1013 missed): this call site IS unconditional and real too
    # -- `spend` needs no allowlist entry and already passes #1013's own stricter vocabulary-
    # coverage test trivially. Its zero-events history on this repo is neither a REACHABILITY gap
    # (like `scan`, pipeline.py:286) nor a plan-declaration gap (like `slice`, slices.py:492) --
    # `spend` is HOST-INTEGRATION-ONLY BY DESIGN: this file's own top-of-file docstring says
    # outright "the loop never measures spend itself; no reports == no enforcement", and nothing in
    # skills/ or hooks/ ever calls `loop.py spend` itself -- it exists to be invoked by whatever
    # external process hosts/wraps the loop. A fourth, distinct reason behind the same zero-count
    # symptom; see the research dossier for #1013 (retro decision emitters) §5.
    if len(argv) >= 4 and argv[1] == "spend":       # host-reported token spend → budget.max_tokens
        # A non-integer token count (a float, empty, comma-grouped, garbage) must REFUSE loudly —
        # exit 2, a usable message — rather than a raw traceback. "Fail-open" (the docstring's
        # promise that budget accounting is unconditional) means the RUN never crashes over this;
        # it does not mean silently mis-adding a value that isn't a count. Validate BEFORE calling
        # `state.add_tokens`, the same refuse-with-a-message shape `_validate_event` uses below.
        try:
            tokens = int(argv[3])
        except ValueError:
            print(f"loop.py spend: token count {argv[3]!r} is not an integer", file=sys.stderr)
            return 2
        if tokens < 0:
            # #632: a spend report only ever adds. A negative count would lower run_tokens and
            # re-open a budget the run already spent, so it is refused before any write.
            print(f"loop.py spend: token count {tokens} is negative; a spend report can only add",
                  file=sys.stderr)
            return 2
        # `state.add_tokens` runs FIRST and unconditionally — `budget.max_tokens` enforcement
        # depends on it, so an invalid event flag below must never skip the budget accounting.
        # On a validation failure, the tokens above are still counted; we just refuse to write
        # the (invalid) event, print the same usable message `emit` would, and exit 2 — matching
        # the story's own done-when ("an invalid flag is refused with a usable message") without
        # ever making budget tracking conditional on the event succeeding.
        state.add_tokens(argv[2], tokens)
        # Optional trailing goal + flags (spec §A.5 item 12: "extends the existing spend verb").
        # amendment C: argv[4] is a GOAL only when it does not itself look like a flag — a bare
        # `spend <dir> <tokens> --tokens_in 10 --tokens_out 20` (flags, no goal) must never let
        # "--tokens_in" become the literal goal string (previously silent: wrong goal recorded,
        # the bare "10" dropped, no error at all). With no goal there is nothing correct to
        # attribute the flags to, so no event is written — budget-only, exactly like the
        # untouched 2-arg form above.
        if len(argv) >= 5 and not argv[4].startswith("--"):
            sdlc_dir, goal = argv[2], argv[4]
            flags = _flags(argv[5:])
            # #140 PR-review finding 2: this used to call `ledger.safe_append` directly, so an
            # unknown flag NAME silently dropped instead of refusing. Shares `emit`'s validator
            # (`_validate_event`) — no kind allowlist, since `spend` always writes the `spend`
            # kind by code, never by agent-supplied argument.
            err = _validate_event("spend", flags)
            if err:
                print(f"loop.py spend: {err}", file=sys.stderr)
                return 2
            ledger.safe_append(sdlc_dir, "spend", goal, config=state.load_config(sdlc_dir),
                               stream=ledger.EVENTS, **flags)
        return 0
    if len(argv) >= 5 and argv[1] == "emit":        # agent-emitted journal event (Class 2, best-effort)
        sdlc_dir, goal, kind = argv[2], argv[3], argv[4]
        flags = _flags(argv[5:])
        err = _validate_event(kind, flags, kind_allowlist=_EMIT_KINDS)
        if err:
            print(f"loop.py emit: {err}", file=sys.stderr)
            return 2
        # #141: `_validate_event` above already rejected a raw newline in any flag (exit 2,
        # nothing written); `ledger.append()` now flatten+scrub+caps every declared prose field
        # (`why` here) uniformly, for every caller — no per-call-site cap/flatten stopgap needed.
        config = state.load_config(sdlc_dir)
        try:
            entry = ledger.append(sdlc_dir, config, kind, goal, stream=ledger.EVENTS, **flags)
        except ValueError as exc:                  # genuinely bad input — refuse loudly
            print(f"loop.py emit: {exc}", file=sys.stderr)
            return 2
        except OSError as exc:
            # amendment B: fail-open past this point. Validation already passed; a write
            # failure now (full disk, an unwritable ledger dir) must never crash the caller —
            # matching every other ledger call site's fail-open contract (safe_append's own
            # message shape, reused here so the two read the same way in a log).
            print(f"ledger: entry skipped (non-fatal): {exc}", file=sys.stderr)
            return 0
        print(entry["id"] if entry else
              "OFF (the journal is not enabled: set \"journal\": {\"enabled\": true}, "
              "or have your organisation's managed settings lock it on)")
        return 0
    if len(argv) >= 5 and argv[1] == "escalate":    # #2828: a send-back the tier cannot converge
        if argv[4] == "--show" and len(argv) == 5:
            return _escalate_show(argv[2], argv[3])
        return _escalate(argv[2], argv[3], argv[4], argv[5:])
    if len(argv) >= 5 and argv[1] == "log":         # agent-emitted LOCAL action-trace (never the ledger)
        sdlc_dir, goal, kind = argv[2], argv[3], argv[4]
        flags = _flags(argv[5:])
        thread = flags.pop("thread", "main")        # an entry-level label, not a per-kind field
        if kind not in actionlog.AGENT_KINDS:        # the CLI can never reach INTERNAL_KINDS — mirrors
            print(f"loop.py log: unknown kind {kind!r} (expected one of "  # _EMIT_KINDS fencing `emit`
                  f"{', '.join(actionlog.AGENT_KINDS)})", file=sys.stderr)  # off the ledger's Class-1 kinds
            return 2
        try:
            entry = actionlog.append(sdlc_dir, goal, kind, "agent", thread=thread, **flags)
        except ValueError as exc:                   # bad flag/newline/vocabulary — a usable refusal
            print(f"loop.py log: {exc}", file=sys.stderr)
            return 2
        except OSError as exc:
            # Same fail-open shape as `emit`'s own OSError branch: validation already passed, so a
            # write failure now (full disk, an unwritable state dir) must never crash the caller.
            print(f"actionlog: entry skipped (non-fatal): {exc}", file=sys.stderr)
            return 0
        if entry is None:                           # action_log.enabled is not true — best-effort,
            print('OFF (config needs "action_log": {"enabled": true})')  # never gates progress
        return 0                                     # silent on success — nothing parses this verb's stdout
    if len(argv) >= 4 and argv[1] == "verify":      # machine done_when: run + persist evidence
        # #1902: verify is a loop trigger too, not just start/next/next-batch — a goal's own
        # Research-through-Retro lifecycle can run for hours through nothing but verify/commit/pr/
        # post-review/merge, entirely outside the pick-time-only coverage those three used to have.
        # Fires unconditionally, before verify_goal's own NO-COMMAND/unsafe-goal checks: a loop
        # trigger has happened the moment this CLI verb runs, regardless of what verify_goal goes
        # on to find — same posture `next`/`next-batch` already take (below, before any source
        # query). Each _ensure_* is independently config-gated and fail-open on its own.
        _verify_cfg = state.load_config(argv[2])
        _ensure_watcher(argv[2], _verify_cfg)
        _ensure_ledger_delivery(argv[2], _verify_cfg)    # ...and notices when that flow has stalled (#2393)
        return verify_goal(argv[2], argv[3])
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    # The script-time floor: this run's wall time, recorded to the session resolved at exit.
    sys.exit(timing_store.timed_main(main, sys.argv, "loop"))
