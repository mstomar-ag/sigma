#!/usr/bin/env python3
"""Ledger-triggered wake-and-work: `autowatch.py tick <sdlc_dir> [--issue N]`.

Part of #1318 (the ledger-autowatch epic). This is the ONE shared decision core both eventual
adapters (a Desktop scheduled task, the CLI/Channels plugin — #1318's other child issues) call
identically, so the safety-critical logic that decides "is it safe to spend real tokens and touch
the worktree RIGHT NOW, unattended" exists exactly once. Neither adapter is wired here — this issue
(#1319) is the core script alone, callable directly via CLI for testing.

WHAT ONE TICK DOES, in order:
  1. `ledger.autowatch.enabled` gate. Not `true` (absent or `false`, matching every other opt-in
     feature's own convention) → no-op, exit 0. Nothing read, nothing written.
  2. Find the ONE thing to act on: `--issue N` names it directly, or this pulls the ledger and
     picks the oldest unactioned mention/assignment/blocker addressed to `me` (never more than one
     per tick — a later tick picks up the rest).
  3. Evaluate preconditions, in a fixed order, short-circuiting on the first failure:
     `require_gh_auth` → `no_concurrent_session` → `load_average_ceiling` →
     `spend_ceiling_tokens_per_week`.
  4. A hop-limit check on the candidate's own `autowatch_hop` chain (not one of the 4 preconditions
     above — a per-candidate cap, not an environment gate).
  5. Only if every check passes: drive `/sigma-loop` scoped to that ONE issue — never an
     open-ended backlog drain while nobody is watching.
  6. On every single outcome — success, any precondition block, hop-limit refusal, "nothing to
     do", or a genuine failure inside the driven run, INCLUDING an unhandled exception anywhere in
     this module — write one ledger note. `tick()` itself never raises; see its own docstring.

SCOPE → LEDGER KIND. The epic's `ledger.autowatch.scope` vocabulary (mentions/assignments/
blockers) is a human-facing description of ledger.py's own `to`-addressed kind space, not a third
ledger kind of its own: `ledger.py`'s KINDS has exactly two kinds a human answers — `handoff` (a
genuine cross-area BLOCKING dependency; see `handoff.py`'s own docstring) and `note` (everything
else addressed to someone: a same-area FYI, a non-blocking cross-area note, a plain mention).
`assignments` and `blockers` both resolve to kind="handoff" — the only addressed kind whose
resolution `ledger.py` already tracks via `outstanding()`/`unanswered()`; `mentions` resolves to
kind="note". See SCOPE_KINDS below.

"UNACKED", precisely. For a `handoff` candidate, "unacked" is `ledger.unanswered()` — nobody has
even replied (not `outstanding()`, which still counts a hand-off someone has already `accepted` or
`deferred`; once a human takes it, autowatch backs off). A `note` candidate has no ack concept in
`ledger.py` at all, so it needs its own dedup: autowatch's own outcome note is written with
`ref=<candidate's own ledger id>` and `state="resolved"` (an existing, validated `ledger.py`
STATES value, not a new one) whenever the drive reaches a genuine `done` OR `parked` terminal
state (see "REAL OUTCOME VERIFICATION" below — a park is a complete, correctly-escalated outcome
from autowatch's own perspective, not a failure), and a candidate whose id already has such a note
is never re-selected. A precondition BLOCK, a properly-recorded `failed` outcome, or a driven run
that never reached ANY recorded terminal state deliberately does NOT set `state="resolved"` —
those are retriable, so the same candidate is picked up again on a later tick once conditions
allow it (a `handoff` candidate is additionally, independently bounded by the real `ack` mechanism
the moment anyone — including the driven run's own goal work — answers it).

REAL OUTCOME VERIFICATION (#1332) — exit-code 0 is NOT proof of success. A real, live end-to-end
run against a genuine issue exited 0 after doing real research and posting a comment, but never
called `loop.py record` at all — no plan, no implementation, no PR. After every drive, this module
re-reads the ledger for a NEW `done`/`parked`/`failed` entry for the target goal (the one thing
`loop.py record` itself always writes) before deciding the outcome: `done` → success/resolved;
`parked` → also resolved (a real, correct escalation, not a failure); `failed` → NOT resolved,
retriable later; nothing found → NOT resolved, treated as `failed` with a note naming exactly this
failure mode, so the mention stays available rather than silently vanishing forever.

HOP LIMIT. `autowatch_hop` (ledger.py OPTIONAL_FIELDS, #1319) counts how many autowatch-triggered
`/sigma-loop` runs already precede a given ledger entry in an unattended chain. The incoming hop is
the higher of two numbers (#1334): the candidate's own `autowatch_hop` field (0 if absent — a
fresh, non-chained trigger, e.g. a hand-off filed by someone else's own goal work), and the
highest `autowatch_hop` autowatch itself has already recorded in one of ITS OWN outcome notes
against this exact candidate (`_prior_autowatch_hop`) — needed because a retriable outcome (a
precondition block, a genuine `failed`, or a driven run that never recorded anything) leaves the
candidate's own static field untouched, so re-reading only that field would hand back the same
hop forever and this cap would never trip on repeated retries of the SAME candidate. That number
plus one must not exceed `ledger.autowatch.hop_limit`, or this tick refuses instead of driving.
The incremented hop is threaded onto the driven subprocess's environment
(`SIGMA_AUTOWATCH_HOP`) so that ANY ledger entry the driven run's own goal work later files —
e.g. a fresh cross-area hand-off via handoff.py — automatically carries it too (ledger.py's own
`append()` fills the field from that env var when a caller doesn't pass one explicitly), closing
the loop with zero changes needed to any other script.

SPEND CEILING — READ THIS BEFORE TRUSTING IT. `spend_ceiling_tokens_per_week` is checked against a
LOCAL, gitignored, append-only file (`.sdlc/state/autowatch-spend.json`, rolling 7-day window) that
records only what THIS machine's autowatch-driven runs themselves reported spending (parsed from
the driven `claude -p --output-format json` call's own `total_cost_usd`, converted to an estimated
token count via a rough, DELIBERATELY approximate blended $/token constant — see
`_estimate_tokens_from_cost`'s own docstring). It is a heuristic approximation of "recent autowatch
spend on this machine" and NOTHING MORE: it cannot see a human's own interactive usage, any other
surface's spend, or another machine's autowatch history, and it MUST NEVER be presented to a human
as a true balance or a real budget check.

PRECONDITIONS ARE SAFETY-CRITICAL, NOT ADVISORY. This drives real, unattended `/sigma-loop`
invocations in production use — every check here gets the same rigor as `loop.py`'s own dispatch
logic, because a bug here means unattended spend or a corrupted worktree with nobody watching. The
governing failure-direction rule, applied consistently: when a check CANNOT be answered (a probe
errors, a binary is missing, a value fails to parse), it fails CLOSED (block) — a false block costs
one skipped tick, retried automatically later; a false pass risks exactly the harm this issue exists
to prevent. Zero deps beyond the stdlib.
"""
import importlib.util
import json
import os
import pathlib
import shlex
import signal
import subprocess
import sys
import threading
import time

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ledger = _load("ledger")
legacy = _load("legacy")   # #239: operator env vars under the previous prefix
loop = _load("loop")
shell_policy = _load("shell_policy")

#: See the module docstring's "SCOPE → LEDGER KIND" section. Deliberately maps `assignments` and
#: `blockers` onto the SAME ledger kind (`handoff`) — `ledger.py`'s own vocabulary has no third
#: kind to give either one a distinct home, and both describe the identical write site
#: (`handoff.py`'s cross-area, blocking hand-off).
SCOPE_KINDS = {
    "mentions": ("note",),
    "assignments": ("handoff",),
    "blockers": ("handoff",),
}
DEFAULT_SCOPE = ("mentions", "assignments", "blockers")

#: Conservative default when `ledger.autowatch.hop_limit` is absent — a safety cap, not a feature
#: toggle, so (unlike most of this kit's opt-in knobs) it has a real default even unconfigured.
DEFAULT_HOP_LIMIT = 3

SPEND_STATE_FILE = "autowatch-spend.json"
SPEND_WINDOW_SECONDS = 7 * 24 * 3600

#: A ROUGH, DELIBERATELY APPROXIMATE blended $/token estimate — see `_estimate_tokens_from_cost`'s
#: own docstring for why no better number exists and what this cannot promise.
USD_PER_MILLION_TOKENS_ESTIMATE = 10.0

#: Overridable via `SIGMA_AUTOWATCH_CMD` (mirrors `supervise_daemon.py`'s own `SIGMA_CLAUDE_CMD`)
#: or `ledger.autowatch.drive_cmd`. `--output-format json` is load-bearing: it is how
#: `_parse_total_cost_usd` recovers this run's own reported cost for the spend ceiling.
DEFAULT_DRIVE_CMD = "claude -p --output-format json"
DEFAULT_DRIVE_TIMEOUT_SECONDS = 2 * 3600

#: #425: the escalation when a driven session must be stopped (timeout, or a signal/exception
#: reaching the tick mid-drive). The driven session runs in a process group of its own; the WHOLE
#: group gets SIGTERM, then up to `DRIVE_TERM_GRACE_SECONDS` to exit (a model CLI flushing its
#: transcript needs seconds, not minutes), then SIGKILL, then a drain of its pipes bounded by
#: `DRIVE_REAP_SECONDS`. Worst case a timed-out call returns timeout + 10s + 5s after it started.
#: Not machine-derived on purpose: these bound wall-clock waits, not resource use.
DRIVE_TERM_GRACE_SECONDS = 10
DRIVE_REAP_SECONDS = 5


# --------------------------------------------------------------------------- config


def _autowatch_settings(config):
    return (ledger.settings(config).get("autowatch") or {})


def enabled(config):
    """Strict `is True`, mirroring `ledger.enabled()`/`agent_watch.enabled()`'s own idiom — a
    truthy string or a stray 1 must never silently switch on a feature that spends real tokens
    unattended. Absent `ledger.autowatch` (or a present block with no/false `enabled`) is a no-op,
    matching every other opt-in feature in this kit."""
    return _autowatch_settings(config).get("enabled") is True


def _precondition_toggle(settings, key):
    """True unless config explicitly sets this precondition to JSON `false`. Mirrors
    `ledger.enabled()`'s strict-boolean posture in the opposite direction: only a real `false`
    disables a safety check, never a falsy-but-not-`False` value a hand-edit might produce."""
    return (settings.get("preconditions") or {}).get(key) is not False


def _hop_limit(settings):
    raw = settings.get("hop_limit")
    if raw is None:
        return DEFAULT_HOP_LIMIT
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_HOP_LIMIT


#: Valid values for `ledger.autowatch.surface` / `SIGMA_AUTOWATCH_SURFACE`. "desktop" is the
#: default (#1323 design decision): a Desktop scheduled task is the adapter most of a team ends up
#: on, since Claude Code CLI has no equivalent to it (`/loop` needs an already-open session — see
#: the module docstring's sibling issues). "cli" opts a machine into the Channels/webhook adapter
#: instead (#1322). This is a DECLARED choice, not runtime-detected: there is no documented
#: environment signal that distinguishes "running inside Claude Desktop" from "running inside
#: Claude Code CLI" (confirmed against Claude Code's own env-vars reference during #1323's design),
#: so guessing would risk a Desktop-only setup prompt misfiring inside an unattended CLI run with
#: nobody there to answer it. Declaring it sidesteps the detection problem entirely.
VALID_SURFACES = ("desktop", "cli")
DEFAULT_SURFACE = "desktop"


def resolve_surface(config):
    """The effective `ledger.autowatch.surface` for THIS machine: `SIGMA_AUTOWATCH_SURFACE`
    (a per-machine override, same idiom as `SIGMA_AUTOWATCH_CMD`/`watch_daemon.py`'s own
    `SIGMA_WATCH_INTERVAL`) wins over the shared, committed `config.json` value, which wins
    over the default. This is what lets a team share ONE `.sdlc/config.json` (most on Desktop, one
    person on CLI, say) without every machine needing its own personal config file — the env var is
    the already-established per-machine escape hatch this codebase uses elsewhere for exactly this
    shape of "shared default, personal override" need. An unrecognized value (typo, stale config)
    falls back to the default rather than silently misrouting to whichever branch happens to not
    match — fail toward the more common, safer-to-nudge-twice surface."""
    env = legacy.getenv("SIGMA_AUTOWATCH_SURFACE")
    if env in VALID_SURFACES:
        return env
    settings = _autowatch_settings(config)
    value = settings.get("surface")
    return value if value in VALID_SURFACES else DEFAULT_SURFACE


def _default_scheduled_tasks_dir():
    """`~/.claude/scheduled-tasks/`, honoring `CLAUDE_CONFIG_DIR` — matches
    desktop-scheduled-tasks.md's own documented SKILL.md path ("or under CLAUDE_CONFIG_DIR if
    set"). Deliberately duplicated from doctor.py's own `_default_scheduled_tasks_dir` (that file's
    established convention is to stay a standalone, dependency-free diagnostic script — see its
    `_enforce_enabled` docstring) rather than cross-imported; this copy is the canonical one real
    runtime code (loop.py) reaches for."""
    root = os.environ.get("CLAUDE_CONFIG_DIR") or str(pathlib.Path.home() / ".claude")
    return pathlib.Path(root) / "scheduled-tasks"


#: The exact invocation loop.py's own setup nudge asks a Desktop session to create (see
#: `_autowatch_setup_warnings`) — matched, not the bare "autowatch.py" filename alone, which a
#: stale/unrelated task's prose could contain by pure coincidence (a task named for some other
#: purpose that happens to mention this script in a comment or description, never actually
#: running it). Not a complete fix for a genuinely stale-but-once-real task left on disk after
#: being deleted/paused via the Desktop UI — that state isn't in SKILL.md at all (Desktop's own
#: docs: "Schedule, folder, model, and enabled state are not in this file"), so no on-disk check
#: can ever fully close that gap. This narrows the false-positive surface; it does not eliminate it.
_DESKTOP_TASK_MARKER = "autowatch.py tick"


def adapter_wired(config, scheduled_tasks_dir=None):
    """Is EITHER adapter actually set up, not just enabled in config? A `channel_webhook_url`
    means the CLI/Channels adapter (#1322) is configured; a scheduled task whose own SKILL.md
    prompt actually invokes autowatch.py (not just mentions it) means the Desktop adapter (#1323)
    is. Fails open to "not wired" on anything unreadable — a false "not wired" costs one extra
    reminder; a false "wired" hides a real setup gap forever. The canonical version of this check
    (loop.py's `_config_warnings` calls it); doctor.py keeps its own standalone copy, per that
    file's own duplication convention."""
    settings = _autowatch_settings(config)
    if settings.get("channel_webhook_url"):
        return True
    tasks_dir = pathlib.Path(scheduled_tasks_dir) if scheduled_tasks_dir is not None \
        else _default_scheduled_tasks_dir()
    try:
        skill_files = list(tasks_dir.glob("*/SKILL.md"))
    except OSError:
        return False
    for skill_md in skill_files:
        try:
            if _DESKTOP_TASK_MARKER in skill_md.read_text(encoding="utf-8"):
                return True
        except OSError:
            continue
    return False


# --------------------------------------------------------------------------- candidate discovery


def _scope_kinds(settings):
    scope = settings.get("scope")
    names = scope if isinstance(scope, list) and scope else DEFAULT_SCOPE
    kinds = set()
    for name in names:
        kinds.update(SCOPE_KINDS.get(name, ()))
    return kinds


def _already_resolved_ids(entries, me):
    """Ledger ids of every candidate autowatch (writing as `me`) has already driven to a genuine
    SUCCESS — durable, shared-ledger state (not a local cursor), so a restarted process or a
    second machine running autowatch under the same identity never re-triggers the same mention
    twice just because it is still technically open. See the module docstring's "UNACKED,
    precisely" section for why only `state="resolved"` counts, not every outcome note."""
    return {e.get("ref") for e in entries
            if e.get("kind") == "note" and e.get("actor") == me
            and e.get("ref") and e.get("state") == "resolved"}


def _prior_autowatch_hop(entries, me, ref):
    """#1334 (review of #1332): the highest `autowatch_hop` autowatch itself (writing as `me`)
    has already recorded in an outcome note against THIS candidate's own ledger id (`ref`).

    `_tick_inner`'s `incoming_hop` cannot be read from the candidate's own static `autowatch_hop`
    field alone — that field belongs to the ORIGINAL, immutable ledger entry and never changes as
    long as the candidate stays unresolved, which every retriable outcome (a genuinely-recorded
    `failed`, or a driven run that never recorded anything at all) deliberately leaves it, so the
    same candidate can be picked up again later. Without this, re-reading only that static field
    would hand back the exact same hop on every later tick, `next_hop` would never advance, and
    `hop_limit` could never trip — turning a bounded retry into an unbounded one. Scans the same
    style of `ref`-keyed outcome notes `_already_resolved_ids` already relies on, so it needs no
    extra ledger read."""
    if not ref:
        return 0
    highest = 0
    for e in entries:
        if e.get("kind") != "note" or e.get("actor") != me or e.get("ref") != ref:
            continue
        try:
            hop = int(e.get("autowatch_hop") or 0)
        except (TypeError, ValueError):
            continue
        if hop > highest:
            highest = hop
    return highest


def _find_candidate(entries, me, settings):
    """The oldest unactioned mention/assignment/blocker addressed to `me`, or None. Reuses
    `ledger.addressed_to` (already exists, per the issue's own instruction — not reimplemented)
    for the base lookup; everything below it is this module's own "new and unacked" filter."""
    kinds = _scope_kinds(settings)
    addressed = ledger.addressed_to(entries, me)
    unanswered_keys = {ledger.settlement_key(e) for e in ledger.unanswered(entries)}
    resolved = _already_resolved_ids(entries, me)
    candidates = []
    for e in addressed:
        if e.get("kind") not in kinds:
            continue
        if e.get("id") in resolved:
            continue
        if e.get("kind") == "handoff" and ledger.settlement_key(e) not in unanswered_keys:
            continue  # already accepted/deferred/declined/resolved -- no longer "unacked"
        candidates.append(e)
    if not candidates:
        return None
    candidates.sort(key=lambda e: (e.get("ts") or "", e.get("id") or ""))
    return candidates[0]


def _find_candidate_for_issue(entries, me, settings, issue):
    """Like `_find_candidate`, but keyed to an explicit `--issue` target (webhook.ts's own
    instructed command, #1322/#1337) rather than picking the oldest addressed entry. `_tick_inner`
    needs this so the `--issue` path can recover the SAME candidate id `_find_candidate` would
    already have picked for this issue, when one exists -- without it, `ref` is never stamped on
    this path's outcome notes and `incoming_hop` can never be read back via
    `_prior_autowatch_hop`, so BOTH `hop_limit` and channel_notify.py's own retry-detection are
    silently defeated for every real, channel-triggered tick (webhook.ts always calls `--issue`).
    Returns None (not an error) when no addressed ledger entry matches this issue -- an explicit
    `--issue` invocation is still valid against a target with no ledger history at all (e.g. a
    human-run manual tick), matching this module's existing, tested behavior for that case."""
    kinds = _scope_kinds(settings)
    addressed = ledger.addressed_to(entries, me)
    matches = [e for e in addressed
               if e.get("kind") in kinds and str(e.get("issue") or e.get("goal")) == str(issue)]
    if not matches:
        return None
    matches.sort(key=lambda e: (e.get("ts") or "", e.get("id") or ""))
    return matches[0]


# --------------------------------------------------------------------------- preconditions


def _run_gh_auth_status():
    try:
        proc = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _check_require_gh_auth(sdlc_dir, config, settings, goal, deps):
    if not _precondition_toggle(settings, "require_gh_auth"):
        return True, ""
    probe = deps.get("run_gh_auth") or _run_gh_auth_status
    try:
        ok = bool(probe())
    except Exception as exc:                                # noqa: BLE001 - a probe must never raise
        return False, f"gh auth probe raised: {exc}"
    return (True, "") if ok else (False, "gh auth status failed — not authenticated")


def _agents_probe_reports_none_running(data):
    """True iff a parsed `claude agents --json` payload reports zero live agents. This module owns
    no pinned schema for that payload (no other script in this repo shells out to it yet) — handles
    a bare list of agent records and an envelope object with an `agents` list, the two most
    plausible shapes, and refuses to claim "none running" for anything else it doesn't recognize.
    Getting this wrong toward "none running" is the UNSAFE direction (see the module docstring's
    fail-closed rule), so an unrecognized shape returns False, not True."""
    if isinstance(data, list):
        return len(data) == 0
    if isinstance(data, dict):
        agents = data.get("agents")
        if isinstance(agents, list):
            return len(agents) == 0
    return False


def _run_claude_agents_probe(sdlc_dir):
    repo_root = str(pathlib.Path(sdlc_dir).resolve().parent)
    try:
        proc = subprocess.run(["claude", "agents", "--json", "--cwd", repo_root],
                               capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return None


def _check_no_concurrent_session(sdlc_dir, config, settings, goal, deps):
    if not _precondition_toggle(settings, "no_concurrent_session"):
        return True, ""
    probe = deps.get("run_agents_probe") or _run_claude_agents_probe
    try:
        data = probe(sdlc_dir)
    except Exception as exc:                                # noqa: BLE001 - a probe must never raise
        return False, f"claude agents probe raised: {exc}"
    if data is None:
        return False, "could not verify `claude agents --json` state (probe failed)"
    if not _agents_probe_reports_none_running(data):
        return False, "`claude agents --json` reports a live agent for this repo"
    # #1319: BOTH signals, per the issue's own instruction — `claude agents --json` (above) covers
    # a live Claude Code session anywhere against this repo; loop.py's own registry (below) covers
    # a managing `/sigma-loop` session or a worker already registered for THIS specific goal, which
    # `claude agents` has no visibility into (e.g. a headless `claude -p` worker with no
    # interactive agent list entry). Reaches into loop.py's underscore-prefixed
    # `_goal_has_registered_worker` deliberately — the issue names this exact function as the
    # machinery to reuse, and Python enforces no real privacy boundary between sibling scripts in
    # this skill.
    if loop.session_active(sdlc_dir, config):
        return False, "a managing /sigma-loop session is already active"
    if loop._goal_has_registered_worker(sdlc_dir, goal, config):
        return False, f"goal {goal} already has a registered worker"
    return True, ""


def _check_load_average_ceiling(sdlc_dir, config, settings, goal, deps):
    ceiling = (settings.get("preconditions") or {}).get("load_average_ceiling")
    if ceiling is None:
        return True, ""                                     # #1319: null config value -> skip entirely
    try:
        ceiling = float(ceiling)
    except (TypeError, ValueError):
        return False, "load_average_ceiling is configured but not a number"
    try:
        load1 = os.getloadavg()[0]
    except (OSError, AttributeError) as exc:
        return False, f"could not read os.getloadavg() ({exc})"
    cores = os.cpu_count() or 1
    normalized = load1 / cores
    if normalized > ceiling:
        return False, f"load average {normalized:.2f} (per-core) exceeds ceiling {ceiling:g}"
    return True, ""


def _spend_path(sdlc_dir):
    return pathlib.Path(sdlc_dir) / "state" / SPEND_STATE_FILE


def _load_spend_records(sdlc_dir):
    """`(records, ok)`. `ok=False` means the file EXISTS but could not be read/parsed -- a
    "cannot be answered" case per the module's own fail-closed rule (docstring above), NOT the
    same as "no spend history yet" (a genuinely absent file, which is fine and safe to treat as
    empty). Collapsing the two into one `return []` previously made a corrupted/unreadable
    spend-state file silently satisfy the spend ceiling forever -- the ceiling check must fail
    closed on `ok=False`, not treat it as zero spend."""
    path = _spend_path(sdlc_dir)
    if not path.exists():
        return [], True
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], False
    records = data.get("records") if isinstance(data, dict) else None
    return (list(records) if isinstance(records, list) else []), True


def _prune_spend_records(records, now):
    cutoff = now - SPEND_WINDOW_SECONDS
    out = []
    for r in records:
        if not isinstance(r, dict):
            continue
        try:
            ts_val = float(r.get("ts"))
        except (TypeError, ValueError):
            continue
        if ts_val >= cutoff:
            out.append(r)
    return out


def _rolling_week_spend_tokens(sdlc_dir, now):
    """The rolling-week total, or `None` when the spend-state file is present but unreadable --
    the caller must treat `None` as "cannot be answered" and fail closed, never as zero spend."""
    records, ok = _load_spend_records(sdlc_dir)
    if not ok:
        return None
    total = 0.0
    for r in _prune_spend_records(records, now):
        try:
            total += float(r.get("tokens") or 0)
        except (TypeError, ValueError):
            continue
    return total


def _record_spend(sdlc_dir, tokens, cost_usd, now):
    """Best-effort, append-only, pruned to the rolling window on every write. Never raises — a
    spend-tracking failure must never break the tick's own outcome recording (the ledger note is
    what matters; this file is a local approximation, not the source of truth). A corrupt existing
    file is treated as empty here (self-healing overwrite) — that's safe for a WRITE, unlike the
    ceiling CHECK above: this call only ever runs after a successful driven run, so starting the
    window fresh under-counts by at most the unreadable file's own history, never over-permits."""
    try:
        records, _ok = _load_spend_records(sdlc_dir)
        records = _prune_spend_records(records, now)
        records.append({"ts": now, "tokens": tokens, "cost_usd": cost_usd})
        path = _spend_path(sdlc_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"records": records}, indent=2, sort_keys=True), encoding="utf-8")
    except Exception:                                       # noqa: BLE001 - never raise
        pass


def _check_spend_ceiling_tokens_per_week(sdlc_dir, config, settings, goal, deps):
    ceiling = (settings.get("preconditions") or {}).get("spend_ceiling_tokens_per_week")
    if ceiling is None:
        return True, ""                                     # #1319: null config value -> skip entirely
    try:
        ceiling = float(ceiling)
    except (TypeError, ValueError):
        return False, "spend_ceiling_tokens_per_week is configured but not a number"
    now = deps.get("now") if deps.get("now") is not None else time.time()
    spent = _rolling_week_spend_tokens(sdlc_dir, now)
    if spent is None:
        return False, ("could not verify rolling spend history — "
                        f"{_spend_path(sdlc_dir).name} exists but is unreadable/corrupt; "
                        "failing closed rather than treating it as zero spend")
    if spent > ceiling:
        return False, (f"rolling 7-day estimated spend {spent:.0f} tokens exceeds ceiling "
                        f"{ceiling:.0f} (heuristic local estimate — see module docstring)")
    return True, ""


#: Fixed order, per the issue's own instruction — short-circuits on the first failure.
PRECONDITIONS = (
    ("require_gh_auth", _check_require_gh_auth),
    ("no_concurrent_session", _check_no_concurrent_session),
    ("load_average_ceiling", _check_load_average_ceiling),
    ("spend_ceiling_tokens_per_week", _check_spend_ceiling_tokens_per_week),
)


# --------------------------------------------------------------------------- driving /sigma-loop


def _estimate_tokens_from_cost(cost_usd):
    """A ROUGH, DELIBERATELY APPROXIMATE conversion of a driven run's own reported
    `total_cost_usd` (from `claude -p --output-format json`) into a token count comparable
    against `spend_ceiling_tokens_per_week`. Anchored to a round blended $/MTok figure in the
    neighborhood of Claude Sonnet 5's own blended input/output rate — NOT a precise conversion,
    and it cannot be one: the real cost-per-token depends on which model tier actually ran and the
    real input/output mix, and `claude -p --output-format json` reports only a single blended
    dollar figure, never a per-tier token breakdown. This is exactly the "local heuristic ...
    explicitly an approximation" the issue calls for — never surface it as a real balance."""
    try:
        cost = float(cost_usd)
    except (TypeError, ValueError):
        return 0.0
    if cost <= 0:
        return 0.0
    return cost * 1_000_000.0 / USD_PER_MILLION_TOKENS_ESTIMATE


def _drive_cmd(settings):
    return legacy.getenv("SIGMA_AUTOWATCH_CMD") or settings.get("drive_cmd") or DEFAULT_DRIVE_CMD


def _drive_prompt(issue, config=None):
    """#1332: two independent fixes to the prompt text, both found via a real, live end-to-end
    run — not theoretical. (1) The old wording hardcoded "GitHub issue #N", wrong for
    `local-goals` discovery mode (config.json.tmpl's own DEFAULT — a fresh adopter who never
    configures GitHub gets this out of the box). Now reads `discovery.source` and phrases the
    target accordingly, matching SKILL.md's own established "local goal file" vocabulary.
    (2) A real driven run, live-tested against a genuine issue, did real research (correctly
    found a second stale doc location that hadn't even been flagged), posted a comment, and then
    STOPPED — no plan, no implementation, no PR, no `loop.py record` call at all — yet exited 0.
    The old "run it through to a normal stop... then STOP" phrasing is exactly ambiguous enough
    for a capable-but-imperfect model to read "a normal stop" as "wherever I judge is reasonable
    to pause," including right after research. The new wording names the actual, unambiguous
    finish line: a real `loop.py record` call, not a vibe."""
    source = ((config or {}).get("discovery") or {}).get("source") or "local-goals"
    target_desc = f"GitHub issue #{issue}" if source == "github" else f"local goal file `{issue}`"
    return (
        f"/sigma-loop Work ONLY on {target_desc} — claim it directly (skip the normal backlog "
        f"picker and every other item in it). Research and discussion alone are NEVER a stopping "
        f"point: you MUST continue through Plan, Implement, and Review until you actually call "
        f"`loop.py record <goal> done|parked|failed` for this goal — that recorded call, not a "
        f"comment or a pause, is what \"a normal stop\" means here. Only THEN stop. Do not pick a "
        f"second item from the backlog."
    )


#: #425: the refusal `_run_drive` returns (exit 2, and the same line on stderr) on a host where it
#: cannot start the driven session in a process group of its own and terminate that whole group.
NO_PROCESS_GROUP_REFUSAL = (
    "autowatch: REFUSED [no-process-group]: this host cannot run the driven session in a process "
    "group of its own and terminate the whole group on timeout (POSIX setsid/killpg required; "
    "Windows is not supported) -- not driving, rather than risk orphaned model processes")

#: Signals that, while a drive is in flight, are trapped in the main thread (a handler set to
#: SIG_IGN, or one installed outside Python, is left alone). The trap NEVER raises: it records the
#: signal and the drive loop, which wakes every `DRIVE_POLL_SECONDS`, terminates the model's group.
#: Afterwards the caller's own handlers are restored and every recorded signal is re-delivered, in
#: order -- so SIG_DFL still kills the caller, Ctrl-C still raises KeyboardInterrupt, and a
#: caller's own handler still runs. A handler that raised instead would have a window at every
#: bytecode boundary in the cleanup in which its exception could escape (three reviews found three).
#: Off the main thread nothing is trapped (Python runs handlers on the main thread only); the
#: lifeline sentinel is the backstop there if the process dies.
_TRAPPED_SIGNALS = ("SIGTERM", "SIGHUP", "SIGINT")

#: How often the drive wait wakes to check for a recorded signal: the worst-case delay between a
#: signal and the start of the group's termination.
DRIVE_POLL_SECONDS = 0.25

#: #425 lifeline sentinel. A tiny process in a session of its own, holding only the READ end of a
#: pipe whose write end exists solely in the process running `_run_drive`. It reads the driven
#: group's id, then blocks. `done` means the drive ended normally: exit quietly. EOF without it
#: means the caller is gone -- including by SIGKILL, which no handler can see, and including a
#: `killpg` of the caller's own group, which no longer reaches the model -- so it does the same
#: SIGTERM, grace, SIGKILL escalation `_terminate_group` does, against the driven group.
_LIFELINE_SENTINEL = r"""
import os, signal, sys, time
for name in ("SIGINT", "SIGHUP"):
    signal.signal(getattr(signal, name), signal.SIG_IGN)
grace = float(sys.argv[1])
try:
    pgid = int(sys.stdin.buffer.readline())
except ValueError:
    sys.exit(0)
if sys.stdin.buffer.read().startswith(b"done"):
    sys.exit(0)
def alive():
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
try:
    os.killpg(pgid, signal.SIGTERM)
except OSError:
    pass
deadline = time.monotonic() + grace
while alive() and time.monotonic() < deadline:
    time.sleep(0.05)
if alive():
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass
"""


def _process_group_refusal():
    if sys.platform == "win32" or not hasattr(os, "killpg"):
        return NO_PROCESS_GROUP_REFUSAL
    return None


def _trap_signals(received, saved):
    """Install a recording handler for each `_TRAPPED_SIGNALS` member, noting the previous handler
    in `saved` before replacing it. The handler appends to `received` and returns -- it never
    raises, so no window exists in which it can break the code it interrupts."""
    if threading.current_thread() is not threading.main_thread():
        return

    def handler(signum, _frame):
        received.append(signum)

    for name in _TRAPPED_SIGNALS:
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            previous = signal.getsignal(signum)
            if previous is None or previous is signal.SIG_IGN:
                continue
            saved[signum] = previous
            signal.signal(signum, handler)
        except (ValueError, OSError):
            saved.pop(signum, None)
    # A caller that restored SIGPIPE's default would otherwise be KILLED by a write to a dead
    # lifeline sentinel, orphaning the model, instead of getting the documented refusal. Ignored
    # for the drive only (restored with the rest); children get the default back from Popen's
    # `restore_signals`. Never recorded or re-delivered.
    sigpipe = getattr(signal, "SIGPIPE", None)
    if sigpipe is not None:
        try:
            previous = signal.getsignal(sigpipe)
            if previous is not None and previous is not signal.SIG_IGN:
                saved[sigpipe] = previous
                signal.signal(sigpipe, signal.SIG_IGN)
        except (ValueError, OSError):
            saved.pop(sigpipe, None)


def _restore_and_redeliver(received, saved):
    """Put the caller's handlers back, then re-deliver every recorded signal once, in arrival
    order. A delivery may end this process (SIG_DFL) or raise (KeyboardInterrupt) -- that is the
    caller's own signal semantics, which is the point. Every step runs in a `finally` of the one
    before it, so a restored handler raising mid-restore (a Ctrl-C landing the instant SIGINT's
    handler is back) can neither leave our recorder installed nor drop a recorded signal; the
    recorder, still installed for the not-yet-restored signals, keeps appending meanwhile."""
    _restore_each(list(saved.items()), received)


def _restore_each(items, received):
    if not items:
        seen = []
        for signum in received:
            if signum not in seen:
                seen.append(signum)
        _deliver_each(seen)
        return
    signum, previous = items[0]
    try:
        try:
            signal.signal(signum, previous)
        except (ValueError, OSError):
            pass
    finally:
        _restore_each(items[1:], received)


def _deliver_each(signums):
    """Deliver each signal even if an earlier delivery raises (KeyboardInterrupt, a caller
    handler's SystemExit): every later one is sent from a `finally`, so the first raise
    propagates only after the rest have been delivered."""
    if not signums:
        return
    try:
        os.kill(os.getpid(), signums[0])
    finally:
        _deliver_each(signums[1:])


def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_group(pgid, signum):
    try:
        os.killpg(pgid, signum)
    except (ProcessLookupError, PermissionError):
        pass


def _terminate_group(proc):
    """#425: stop the driven session AND everything it started, then reap. The session was started
    with `start_new_session=True`, so its pid is its process-group id and every descendant that did
    not itself call setsid shares it. Escalation: SIGTERM to the group; drain the pipes and wait up
    to `DRIVE_TERM_GRACE_SECONDS` for the whole group to be gone; SIGKILL to whatever remains; drain
    again, bounded by `DRIVE_REAP_SECONDS`, so a process that escaped the group (its own setsid --
    the one case a group signal cannot reach) and still holds the pipe cannot hang the caller.
    `pgid` is `proc.pid`, never `os.getpgid(...)`, which fails once the leader is reaped. A further
    signal cannot cut this short: the trap only records it. Never raises an `Exception`."""
    pgid = proc.pid
    drained = False
    _signal_group(pgid, signal.SIGTERM)
    deadline = time.monotonic() + DRIVE_TERM_GRACE_SECONDS
    try:
        proc.communicate(timeout=DRIVE_TERM_GRACE_SECONDS)
        drained = True
    except Exception:                                       # noqa: BLE001 - TimeoutExpired, or a
        pass                                                 # pipe already closed: escalate anyway
    while _group_alive(pgid) and time.monotonic() < deadline:
        proc.poll()                                          # reap the leader so it cannot keep the
        time.sleep(0.05)                                     # group "alive" as a zombie
    if _group_alive(pgid):
        _signal_group(pgid, signal.SIGKILL)
    if not drained:
        try:
            proc.communicate(timeout=DRIVE_REAP_SECONDS)
        except Exception:                                    # noqa: BLE001 - best-effort, bounded
            pass
    proc.poll()


def _start_lifeline():
    """Spawn the lifeline sentinel. Returns `(sentinel_proc, write_fd)`; raises OSError on failure.
    The write end is non-inheritable (PEP 446) and every Popen here closes fds, so no child --
    least of all the model -- holds it: only this process's death or an explicit close ends it."""
    read_fd, write_fd = os.pipe()
    try:
        sentinel = subprocess.Popen(
            [sys.executable, "-c", _LIFELINE_SENTINEL, str(DRIVE_TERM_GRACE_SECONDS)],
            stdin=read_fd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
    except BaseException:
        os.close(write_fd)
        raise
    finally:
        os.close(read_fd)
    return sentinel, write_fd


def _end_lifeline(sentinel, write_fd):
    """Release the sentinel with `done`: this process is alive and has either seen the model
    finish or terminated its group itself, so there is nothing for the sentinel to sweep (a sweep
    now would signal an already-reaped group id). Only this process's death -- the case the
    sentinel exists for -- ends the pipe without `done`. Reaped with a bound; an unreaped sentinel
    exits by itself as soon as it reads `done`."""
    try:
        os.write(write_fd, b"done")
    except OSError:
        pass
    try:
        os.close(write_fd)
    except OSError:
        pass
    try:
        sentinel.wait(timeout=DRIVE_REAP_SECONDS)
    except Exception:                                       # noqa: BLE001 - bounded, best-effort
        pass


def _run_drive(cmd_str, prompt, cwd, env, timeout, on_spawn=None):
    """The real subprocess invocation. Returns (exit_code, stdout) and never raises an `Exception` —
    a launch failure or a timeout becomes a synthetic non-zero exit with the error as `stdout`, so
    every caller treats it identically to a real CLI failure.

    #425 -- the WHOLE driven tree, not just the direct child. The session starts in a process group
    of its own (`start_new_session=True`), and every way this call can end early goes through
    `_terminate_group` (SIGTERM the group, `DRIVE_TERM_GRACE_SECONDS` grace, SIGKILL, bounded drain):
      - the timeout -> `(124, ...)`;
      - SIGTERM/SIGHUP/SIGINT to this process (main thread) -> recorded by the trap, noticed within
        `DRIVE_POLL_SECONDS`, group terminated, then the caller's handlers restored and every
        recorded signal re-delivered in order (a SIG_DFL caller dies by it, Ctrl-C raises
        KeyboardInterrupt, a caller's own handler runs; if the process survives that, this returns
        `(128 + first signum, ...)`). A signal arriving before the model is spawned means it is
        never spawned;
      - any exception escaping the wait -> group terminated, exception re-raised.
    The new group is what makes the trap necessary: a signal addressed to the caller's own group no
    longer reaches the model by sharing it. For what NO handler can see -- SIGKILL to this process,
    or `killpg` of its group (what `run_with_timeout.py` does) -- the lifeline sentinel
    (`_LIFELINE_SENTINEL`) sees its pipe close without `done` and runs the same escalation. Not
    covered: a descendant that calls setsid itself; the sentinel itself being SIGKILLed; this
    process dying by SIGKILL in the instant between the model's spawn and the sentinel learning its
    group id, or between a normal finish and `done` (the sentinel then sweeps the finished model's
    group -- its leftovers, or nobody); a drive on a non-main thread, where nothing is trapped and
    a signal reaches the caller's own handling (the lifeline is the backstop if the process dies);
    a caller thread that has SIGTERM blocked, whose mask the model inherits, so the graceful step
    is lost and the model is SIGKILLed after the grace; a non-main-thread drive in a caller with
    SIGPIPE at SIG_DFL whose sentinel has already died (the main thread ignores SIGPIPE for the
    drive; a worker thread cannot). Repeats of one signal are re-delivered
    once. On a host without
    POSIX process groups (Windows) the call REFUSES -- `(2, NO_PROCESS_GROUP_REFUSAL)`, the same
    line on stderr, nothing spawned -- rather than proceed with a kill that cannot reach the tree.
    A sentinel that cannot be started is likewise a refusal to drive (exit 1).

    `on_spawn(pid)` (#2338, Component H's round-2 REJECT fix) is an OPTIONAL, additive, backward-
    compatible callback invoked the INSTANT the child process exists — before anything blocks
    waiting for it to finish, and before any timeout has a chance to fire. Every existing caller
    (`_drive`'s own autowatch tick) omits it. This exists so a caller that dispatches a driven
    session unattended (the Slack-commands listener, #2338) can register a REAL liveness marker for
    the CHILD's own pid (also its process-group id) — not just its own, short-lived, calling
    process's pid — closing the gap where a listener killed while blocked inside this call would
    otherwise orphan the child with nothing tracking it (see `slack_commands_listen.dispatch`)."""
    if (cmd_str != DEFAULT_DRIVE_CMD and cmd_str != legacy.getenv("SIGMA_AUTOWATCH_CMD")
            and not shell_policy.repository_shell_commands_allowed(cwd)):
        # #707 (TM-04): at the ONE spawn point, so the autowatch tick, the Slack listener and its
        # merge-drive paths are all covered. A command that is neither the shipped default nor the
        # operator's own env var came from repo config: it needs the Git-local opt-in.
        print("autowatch: " + shell_policy.refusal_message(), file=sys.stderr)
        return 2, "autowatch: " + shell_policy.refusal_message()
    try:
        args = shlex.split(cmd_str) + [prompt]
    except ValueError as exc:
        return 1, f"autowatch: could not parse drive_cmd {cmd_str!r}: {exc}"
    refusal = _process_group_refusal()
    if refusal:
        print(refusal, file=sys.stderr)
        return 2, refusal
    received = []
    saved = {}
    try:
        _trap_signals(received, saved)
        return _drive_under_trap(args, cwd, env, timeout, on_spawn, received)
    finally:
        _restore_and_redeliver(received, saved)


def _drive_under_trap(args, cwd, env, timeout, on_spawn, received):
    """`_run_drive`'s body, run with the recording trap installed. Never raises an `Exception`."""
    def interrupted():
        return 128 + received[0], f"autowatch: driven run interrupted by signal {received[0]}"

    if received:
        return interrupted()
    try:
        sentinel, write_fd = _start_lifeline()
    except Exception as exc:                                # noqa: BLE001 - never raise
        return 1, f"autowatch: not driving -- could not start the lifeline sentinel: {exc}"
    try:
        if received:
            return interrupted()
        try:
            proc = subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, start_new_session=True)
        except Exception as exc:                            # noqa: BLE001 - never raise
            return 1, f"autowatch: failed to launch driven run: {exc}"
        with proc:
            try:
                try:
                    os.write(write_fd, f"{proc.pid}\n".encode())
                except OSError as exc:
                    _terminate_group(proc)
                    return 1, f"autowatch: lifeline sentinel gone, driven run stopped: {exc}"
                if on_spawn is not None:
                    try:
                        on_spawn(proc.pid)
                    except Exception:                       # noqa: BLE001 - a caller's callback must
                        pass                                 # never break the drive it is watching
                deadline = time.monotonic() + timeout
                while True:
                    if received:
                        _terminate_group(proc)
                        return interrupted()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        _terminate_group(proc)
                        return 124, (f"autowatch: driven run timed out after {timeout}s "
                                     f"(process group terminated)")
                    try:
                        stdout, _stderr = proc.communicate(
                            timeout=min(remaining, DRIVE_POLL_SECONDS))
                    except subprocess.TimeoutExpired:
                        continue
                    return proc.returncode, stdout
            except BaseException:
                _terminate_group(proc)
                raise
    except Exception as exc:                                # noqa: BLE001 - never raise
        return 1, f"autowatch: driven run failed: {exc}"
    finally:
        _end_lifeline(sentinel, write_fd)


#: The exact set `loop.py`'s own `_record()` chokepoint writes to the ledger
#: (`ledger.safe_append(sdlc_dir, outcome, goal, ...)` where `outcome` is one of these three) —
#: the single source of truth for "did the driven run actually reach a real SDLC terminal state,"
#: independent of and more trustworthy than the driven subprocess's own exit code. See #1332.
TERMINAL_OUTCOME_KINDS = ("done", "parked", "failed")


def _terminal_outcome_ids(entries, goal):
    return {e.get("id") for e in entries
            if str(e.get("goal")) == str(goal) and e.get("kind") in TERMINAL_OUTCOME_KINDS}


def _new_terminal_outcome_kind(sdlc_dir, goal, before_ids):
    """#1332: re-reads the ledger AFTER the drive returns and looks for a `done`/`parked`/`failed`
    entry for `goal` that did not exist before the drive started (`before_ids`, a snapshot taken
    from the SAME `entries` read already done at the top of `_tick_inner` — no extra read needed
    on the common path). Returns that entry's `kind`, or `None` if no such entry exists.

    WHY THIS EXISTS, not a hypothetical: a real, live end-to-end run against a genuine issue
    exited 0 (`claude -p`'s own exit code) after doing real research and posting a real comment,
    but NEVER called `loop.py record` — no plan, no implementation, no PR, nothing. The driven
    CLI process completing without crashing proves only that the subprocess didn't crash; it says
    nothing about whether the underlying SDLC goal actually reached a recorded outcome. Trusting
    exit-code-0 alone as "success" silently and PERMANENTLY marked that real, unfinished mention
    `resolved` — exactly the "nobody should ever come back to silence" failure this whole feature
    exists to prevent, now happening BECAUSE of a gap in the feature's own success check."""
    entries = ledger.read_all(sdlc_dir)
    for e in entries:
        if (str(e.get("goal")) == str(goal) and e.get("kind") in TERMINAL_OUTCOME_KINDS
                and e.get("id") not in before_ids):
            return e.get("kind")
    return None


def _parse_total_cost_usd(stdout_text):
    try:
        data = json.loads(stdout_text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    cost = data.get("total_cost_usd")
    try:
        return float(cost) if cost is not None else None
    except (TypeError, ValueError):
        return None


_GUARD = []


def _guard():
    """resource_guard, loaded once (#1758): the one place a model session asks for admission."""
    if not _GUARD:
        spec = importlib.util.spec_from_file_location(
            "resource_guard", pathlib.Path(__file__).resolve().parent / "resource_guard.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _GUARD.append(module)
    return _GUARD[0]


def _drive(sdlc_dir, config, settings, issue, next_hop, deps):
    run = deps.get("run_drive") or _run_drive
    cmd_str = _drive_cmd(settings)
    prompt = _drive_prompt(issue, config)
    repo_root = str(pathlib.Path(sdlc_dir).resolve().parent)
    if (not legacy.getenv("SIGMA_AUTOWATCH_CMD") and settings.get("drive_cmd")
            and not shell_policy.repository_shell_commands_allowed(repo_root)):
        # #707 (TM-04): a repo-config `drive_cmd` is a repository-supplied executable. The env var
        # is the operator's own and stays ungated.
        print("autowatch: " + shell_policy.refusal_message(), file=sys.stderr)
        return 2, "autowatch: " + shell_policy.refusal_message()
    env = dict(os.environ)
    env["SIGMA_AUTOWATCH_HOP"] = str(next_hop)
    if not env.get("SIGMA_RUN_ID"):
        env["SIGMA_RUN_ID"] = f"autowatch-{os.getpid()}-{int(time.time())}"
    timeout = settings.get("timeout_seconds") or DEFAULT_DRIVE_TIMEOUT_SECONDS
    return run(cmd_str, prompt, repo_root, env, timeout)


# --------------------------------------------------------------------------- outcome recording


def _record_outcome(sdlc_dir, config, goal, why, ref=None, to=None, hop=None, resolved=False):
    """The one write site every code path in this module funnels through, per the issue's own
    "on ANY outcome ... write a ledger entry" contract. Uses `ledger.safe_append` (never raises,
    and is itself a true no-op when `ledger.enabled` is off — same fail-open posture every other
    watcher in this kit already has). `state="resolved"` is what `_already_resolved_ids` looks
    for; every other outcome leaves `state` unset so the candidate can be retried later."""
    fields = {"why": why}
    if ref:
        fields["ref"] = ref
    if to:
        fields["to"] = to
    if hop is not None:
        fields["autowatch_hop"] = str(hop)
    if resolved:
        fields["state"] = "resolved"
    ledger.safe_append(sdlc_dir, "note", goal, config=config, **fields)


# --------------------------------------------------------------------------- tick


def _tick_inner(sdlc_dir, config, settings, issue, deps):
    me = ledger.actor(config)
    entries = ledger.read_all(sdlc_dir)

    candidate = None
    incoming_hop = 0
    if issue:
        target = str(issue)
        # #1337: resolve back to the SAME ledger candidate `_find_candidate` would have picked
        # for this issue, when one exists -- webhook.ts's own instructed command always drives
        # this exact `--issue` path, so without this `candidate` stayed None on every real,
        # channel-triggered tick: `ref` was never stamped on the outcome note, and
        # `_prior_autowatch_hop` (below, same as the auto-discovery branch) could never read
        # anything back, silently defeating `hop_limit` for repeat calls against the same
        # candidate. None when no addressed entry matches -- an explicit `--issue` invocation is
        # still valid with no ledger history at all (e.g. a human-run manual tick).
        candidate = _find_candidate_for_issue(entries, me, settings, issue)
        if candidate is not None:
            try:
                incoming_hop = int(candidate.get("autowatch_hop") or 0)
            except (TypeError, ValueError):
                incoming_hop = 0
            incoming_hop = max(incoming_hop, _prior_autowatch_hop(entries, me, candidate.get("id")))
    else:
        candidate = _find_candidate(entries, me, settings)
        if candidate is None:
            _record_outcome(sdlc_dir, config, "autowatch",
                             "nothing to do — no unactioned mention/assignment/blocker found")
            return "nothing to do"
        target = str(candidate.get("issue") or candidate.get("goal"))
        try:
            incoming_hop = int(candidate.get("autowatch_hop") or 0)
        except (TypeError, ValueError):
            incoming_hop = 0
        # #1334: fold in any hop autowatch has already recorded against this EXACT candidate
        # across prior ticks -- the candidate's own static field alone is not enough, see
        # _prior_autowatch_hop's own docstring.
        incoming_hop = max(incoming_hop, _prior_autowatch_hop(entries, me, candidate.get("id")))

    ref = candidate.get("id") if candidate else None
    to = candidate.get("actor") if candidate else None

    for name, check in PRECONDITIONS:
        ok, why = check(sdlc_dir, config, settings, target, deps)
        if not ok:
            _record_outcome(sdlc_dir, config, target, f"blocked on precondition {name}: {why}",
                             ref=ref, to=to, hop=incoming_hop)
            return f"blocked: {name}"

    next_hop = incoming_hop + 1
    limit = _hop_limit(settings)
    if next_hop > limit:
        _record_outcome(sdlc_dir, config, target,
                         f"refused — hop {next_hop} would exceed ledger.autowatch.hop_limit ({limit})",
                         ref=ref, to=to, hop=incoming_hop)
        return "blocked: hop_limit"

    before_ids = _terminal_outcome_ids(entries, target)

    try:
        # #1758: a driven model session asks for admission like every other spawn; a hold defers the tick.
        with _guard().session_lease("autowatch", target, repo=str(pathlib.Path(sdlc_dir).resolve().parent),
                                       ttl=(settings.get("timeout_seconds") or DEFAULT_DRIVE_TIMEOUT_SECONDS) * 2 + 60) as lease:
            held = lease if lease is not None and hasattr(lease, "reason") else None
            if held is None:
                exit_code, output = _drive(sdlc_dir, config, settings, target, next_hop, deps)
    except Exception as exc:                                # noqa: BLE001 - the driven run must never
        _record_outcome(sdlc_dir, config, target,                          # take this tick down with it
                         f"failed — driving /sigma-loop raised: {exc}", ref=ref, to=to, hop=incoming_hop)
        return "failed"

    if held is not None:
        _record_outcome(sdlc_dir, config, target, f"deferred — session held ({held.reason}); nothing was started",
                         ref=ref, to=to, hop=incoming_hop)
        return "deferred: resource hold"

    if exit_code != 0:
        why = f"failed — driven /sigma-loop exited {exit_code}"
        if output == NO_PROCESS_GROUP_REFUSAL:              # #425: the refusal must reach the
            why += f": {output}"                             # ledger, not only stderr
        _record_outcome(sdlc_dir, config, target, why, ref=ref, to=to, hop=incoming_hop)
        return "failed"

    now = deps.get("now") if deps.get("now") is not None else time.time()
    cost_usd = _parse_total_cost_usd(output)
    if cost_usd is not None:
        _record_spend(sdlc_dir, _estimate_tokens_from_cost(cost_usd), cost_usd, now)

    # #1332: exit-code 0 proves only that the subprocess didn't crash — it is NOT proof the goal
    # reached a real SDLC terminal state. Re-check the ledger's own `done`/`parked`/`failed`
    # record (the one thing `loop.py record` itself writes) before trusting "success".
    real_kind = _new_terminal_outcome_kind(sdlc_dir, target, before_ids)
    if real_kind == "done":
        _record_outcome(sdlc_dir, config, target, "driven /sigma-loop completed",
                         ref=ref, to=to, hop=next_hop, resolved=True)
        return "success"
    if real_kind == "parked":
        # A real park IS a complete, correct outcome from autowatch's own perspective: the goal is
        # now visible via the normal sdlc:parked mechanism, and a human will see it there — this
        # is autowatch successfully escalating, not autowatch failing.
        _record_outcome(sdlc_dir, config, target,
                         "driven /sigma-loop parked the goal for a human — see its own park "
                         "label/comment", ref=ref, to=to, hop=next_hop, resolved=True)
        return "success"
    if real_kind == "failed":
        # Deliberately NOT resolved: a genuine, properly-recorded failure (a flaky test, a
        # transient error) should get another chance on a later tick, bounded by hop_limit —
        # matching the hop-limit mechanism's own purpose (bound retries, don't ban them). `hop`
        # is `next_hop`, not `incoming_hop` (#1334): a real driven attempt actually ran at
        # `next_hop`, and this note is the ONLY durable record of that — `_prior_autowatch_hop`
        # reads it back on the next tick so the count actually accumulates instead of re-reading
        # the same static candidate field forever.
        _record_outcome(sdlc_dir, config, target,
                         "driven /sigma-loop recorded the goal as failed — will retry on a later "
                         "tick, subject to hop_limit", ref=ref, to=to, hop=next_hop)
        return "failed"
    # real_kind is None: the exact live-reproduced bug this fix exists for. The subprocess exited
    # cleanly but never called `loop.py record` at all — deliberately NOT resolved, so this
    # mention remains available for a later tick rather than silently vanishing forever. `hop` is
    # `next_hop` for the same reason as the `failed` branch just above (#1334).
    _record_outcome(sdlc_dir, config, target,
                     "failed — driven /sigma-loop exited cleanly but never recorded a terminal "
                     "outcome (done/parked/failed) for the goal", ref=ref, to=to, hop=next_hop)
    return "failed"


def tick(sdlc_dir, issue=None, config=None, now=None,
         run_gh_auth=None, run_agents_probe=None, run_drive=None):
    """-> a one-line summary for the log. NEVER raises, regardless of what happens downstream —
    every code path (including a genuinely unexpected exception anywhere in this module or in the
    driven run) still ATTEMPTS exactly one ledger note before returning, per the issue's own
    "nobody should ever come back to silence" contract; that note actually lands on every path
    except the two where it structurally cannot: `enabled:false`/absent (the one true no-op — zero
    reads beyond config, zero ledger writes, zero `/sigma-loop` invocation — checked and returned
    BEFORE anything else in this function runs) and an unreadable `config.json` itself (a ledger
    write needs `enabled`/`actor` FROM that same file, so there is nothing safe to write with —
    see the config-load `try` below).

    `run_gh_auth`/`run_agents_probe`/`run_drive` are DI seams for tests (mirrors `agent_watch.py`'s
    own `run_email` idiom) — default `None` uses the real `gh`/`claude agents`/driven-subprocess
    calls.

    Reading `config.json` itself is INSIDE the guard below, not before it: `ledger._config` is a
    bare `json.loads(path.read_text())` with no error handling of its own, so a missing/mid-write/
    malformed config.json (concurrent writer, corrupt disk, not-yet-scaffolded `.sdlc/`) must not
    propagate out of this "never raises" function. We genuinely cannot know `enabled(config)` in
    that case (it lives IN config.json), so this is NOT the same as the true `enabled:false` no-op
    below — `_record_outcome` is still attempted (it will itself safely no-op, per
    `ledger.safe_append`'s own long-standing fail-open contract, rather than fabricate a
    guessed actor/enabled state to force a write)."""
    try:
        config = config if config is not None else ledger._config(sdlc_dir)
    except Exception as exc:                                # noqa: BLE001 - config.json itself must
        _record_outcome(sdlc_dir, config, "autowatch",      # never take a tick down with it
                         f"failed — could not load config.json: {exc}")
        return "failed: could not load config"
    settings = _autowatch_settings(config)
    if not enabled(config):
        return ""
    deps = {"run_gh_auth": run_gh_auth, "run_agents_probe": run_agents_probe,
            "run_drive": run_drive, "now": now}
    try:
        return _tick_inner(sdlc_dir, config, settings, issue, deps)
    except Exception as exc:                                # noqa: BLE001 - a tick must never be fatal
        _record_outcome(sdlc_dir, config, "autowatch", f"failed — unexpected error: {exc}")
        return "failed: unexpected error"


# --------------------------------------------------------------------------- CLI


def _flags(argv_tail):
    """Minimal `--name value` scanner for this script's one optional flag — a miniature of
    `loop.py`'s own `_flags` idiom, not imported (a two-line idiom, and this module stays
    dependency-light like every other watch-tick script in this skill)."""
    out = {}
    i = 0
    while i < len(argv_tail):
        token = argv_tail[i]
        if token.startswith("--") and i + 1 < len(argv_tail):
            out[token[2:]] = argv_tail[i + 1]
            i += 2
            continue
        i += 1
    return out


USAGE = "usage: autowatch.py tick <sdlc_dir> [--issue N]"


def main(argv):
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    if len(argv) < 3 or argv[1] != "tick":
        print(USAGE, file=sys.stderr)
        return 2
    sdlc_dir = argv[2]
    issue = _flags(argv[3:]).get("issue")
    try:
        print(tick(sdlc_dir, issue=issue))
    except Exception as exc:                                # noqa: BLE001 - a tick must never be fatal
        print(f"autowatch: tick failed (non-fatal): {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
