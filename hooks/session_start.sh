#!/usr/bin/env bash
# SessionStart hook — the wizard check below is UNCONDITIONAL for an interactive session (issue
# #1560), with one exception: a headless/supervised run (SIGMA_RUN_ID set) skips it, since its
# flow is a real yes/no conversation and there is nobody there to hold one. The policy-brief block
# further down stays its own separate, pre-existing opt-in feature. Two concerns, kept apart on purpose:
# gating the wizard behind session_start.enabled would silently inherit an unrelated feature's
# off-by-default and never fire for anyone.
# Policy-brief half: OPT-IN. Injects a short SDLC policy brief (+ a doctor-lite install self-check) as
# additionalContext at the start of a session, so Sigma's conventions are in context before the first
# prompt (the UserPromptSubmit gate only fires once the user actually types). Off by default — silent
# unless the repo has a .sdlc/ AND opted in:
#   .sdlc/config.json → {"session_start": {"enabled": true}}
# Advisory only: it never blocks; the self-check warns, never fails. Fail-open everywhere: no python3 /
# no config / not enabled / no .sdlc → emit nothing, exit 0 (a session with no Sigma is untouched).
set -uo pipefail

# allow(): the hook's quiet exit. When the coexistence notice (below) found something and no later
# tier emitted, it emits that notice alone -- the one additionalContext of this invocation.
allow() {
    if [ -n "${HOOK_COEXIST_NOTICE:-}" ] && command -v python3 >/dev/null 2>&1; then
        python3 -c 'import json, os; print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": os.environ["HOOK_COEXIST_NOTICE"]}}))' 2>/dev/null
    fi
    exit 0
}
PROJECT="${CLAUDE_PROJECT_DIR:-$PWD}"
CFG="$PROJECT/.sdlc/config.json"
# The wizard below has to import ITS OWN plugin code (setup_wizard.py), which lives beside THIS hook
# file -- not inside $PROJECT, which is whatever repo the user is actually working in (almost never the
# plugin's own source tree). Resolved from $0, the same __file__-relative convention decision_gate.py
# already uses for its own sibling-script imports (Path(__file__).resolve().parent...), kept
# independent of $PROJECT on purpose so the wizard actually fires for a real adopting repo, not only
# when $PROJECT happens to be this plugin's own source tree.
HOOK_DIR="$(dirname "$0")"
PLUGIN_ROOT="$(dirname "$HOOK_DIR")"

command -v python3 >/dev/null 2>&1 || allow

# --- The plugin under the previous name also active here (issues #240, #314) -----------------
# An ACCELERATOR only: it tells the session up front. The behaviour lives in Sigma's own Python on
# every write surface (init, loop start, claim/record, the watcher and its spawn, migrate) via
# skills/sigma-loop/scripts/coexist.py -- Cursor has no hooks, so nothing here decides anything.
# Since #314 that is a NOTICE, never a refusal: Sigma is handling this repository. So this is ONE
# line (coexist.notice_line), READ-ONLY (it writes no mark: running it twice says the same thing,
# idempotent), and silent under SIGMA_ALLOW_COEXIST=1. Both plugins' session hooks may fire; this
# line only states what Sigma does and names the uninstall command -- it asks nothing of the other.
# NOT a tier: it never ends the hook. Its text is carried in HOOK_COEXIST_NOTICE and PREPENDED to
# whichever tier below emits (only one additionalContext per invocation), or emitted alone by
# allow() when none does -- so the watcher-staleness check, the wizard and the policy brief still
# run (AGENTS.md LIVENESS: a dead watcher is never hidden by this).
# Silent when nothing is active or the check cannot run.
HOOK_COEXIST_NOTICE=""
if [ -d "$PROJECT/.sdlc" ]; then
HOOK_COEXIST_NOTICE="$(python3 - "$PROJECT" "$PLUGIN_ROOT" <<'PY' 2>/dev/null
import importlib.util, os, sys
project, plugin_root = sys.argv[1], sys.argv[2]
try:
    spec = importlib.util.spec_from_file_location(
        "coexist", os.path.join(plugin_root, "skills", "sigma-loop", "scripts", "coexist.py"))
    coexist = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(coexist)
    line = coexist.warn_line(os.path.join(project, ".sdlc"))
    if line:
        print("Tell the user this: " + line)
except Exception:
    pass
PY
)" || HOOK_COEXIST_NOTICE=""
fi

# --- Upkeep window line and sweep start (issues #1224, #1387) --------------------------------------------------------
# ACCELERATOR only: reports, never rebases or pushes. The facts and wording live in Sigma's own Python
# (skills/sigma-loop/scripts/upkeep_session.py, callable on a host without hooks); this only runs it. Its lines ride
# with the coexistence notice (one additionalContext per invocation). Gate closed, nothing due, any failure: no
# output at all, so the hook is byte-identical to before. Headless skip as above.
if [ -z "${SIGMA_RUN_ID:-}" ] && [ -f "$PROJECT/.sdlc/config.json" ]; then
    HOOK_UPKEEP_LINES="$(python3 "$PLUGIN_ROOT/skills/sigma-loop/scripts/upkeep_session.py" lines "$PROJECT/.sdlc" 2>/dev/null)" || HOOK_UPKEEP_LINES=""
    # #1387: window open + machine opt-in starts ONE detached sweep (prints nothing, never waited on; gates inside the script).
    python3 "$PLUGIN_ROOT/skills/sigma-loop/scripts/upkeep_session.py" start "$PROJECT/.sdlc" >/dev/null 2>&1 </dev/null || true
    if [ -n "$HOOK_UPKEEP_LINES" ]; then
        if [ -n "$HOOK_COEXIST_NOTICE" ]; then
            HOOK_COEXIST_NOTICE="$HOOK_COEXIST_NOTICE"$'\n\n'"Sigma: $HOOK_UPKEEP_LINES"
        else
            HOOK_COEXIST_NOTICE="Sigma: $HOOK_UPKEEP_LINES"
        fi
    fi
fi

# --- North-star unreachable (D2 F03) ------------------------------------------------------------
# ACCELERATOR only: the line and the rule live in Sigma's own Python (north_star.py notice, also run by
# /sigma-doctor), so a host without hooks still sees it there. Silent unless a north-star exists but cannot
# be located or read; a project with none is never nagged. Rides with the notices above.
if [ -z "${SIGMA_RUN_ID:-}" ] && [ -f "$PROJECT/.sdlc/config.json" ]; then
    HOOK_NS_LINE="$(python3 "$PLUGIN_ROOT/skills/sigma-loop/scripts/north_star.py" notice "$PROJECT" 2>/dev/null)" || HOOK_NS_LINE=""
    if [ -n "$HOOK_NS_LINE" ]; then
        if [ -n "$HOOK_COEXIST_NOTICE" ]; then
            HOOK_COEXIST_NOTICE="$HOOK_COEXIST_NOTICE"$'\n\n'"$HOOK_NS_LINE"
        else
            HOOK_COEXIST_NOTICE="$HOOK_NS_LINE"
        fi
    fi
fi
export HOOK_COEXIST_NOTICE

# --- Guided setup wizard (issue #1560) ---------------------------------------------------------
# #236 / #186: ADOPTED REPOS ONLY. `setup_wizard.wizard_status()` returns "nothing needed" unless
# `.sdlc/config.json` exists and no other plugin owns the directory -- the gate lives in Python, so
# every caller (this hook, the sigma-wizard skill) gets it; a stranger's repo hears nothing, and a
# new repository's entry point is /sigma-init, not this hook.
# Only ONE additionalContext can be emitted per hook invocation, so this block's own exit status
# gates whether the policy-brief block below ever runs at all: exit 0 means "already printed,
# stop here" (caught by the `if`, which `exit 0`s the whole hook immediately, before the
# policy-brief block gets a chance to ALSO print and produce two concatenated JSON blobs on
# stdout); exit 1 -- nothing needed, or the check itself failed -- falls through so the
# policy-brief block runs exactly as it always has. `sys.exit(0)` alone (with no surrounding
# `if`) would only end the python3 subprocess, not this shell script, which is why the `if` here
# is load-bearing and not just style.
#
# HEADLESS SESSIONS SKIP THE WIZARD ENTIRELY. The wizard's whole flow is "ask a real yes/no
# question and act only on yes" -- there is nobody to answer it in a `claude -p /sigma-loop`
# worker, so injecting it there just spends context telling an autonomous run to hold a
# conversation. `SIGMA_RUN_ID` is the signal: skills/sigma-loop/scripts/supervise_daemon.py hands
# it to that subprocess's own `env=` before launching it (its run-id resolution step), and every
# child inherits it. Set -> fall
# through to today's behaviour (silent, or the policy brief if separately enabled).
#
# HONEST LIMIT, DELIBERATELY NOT CLOSED HERE: this reads "a supervised loop is in this process
# tree", which is NOT the same as "no human is watching" -- an interactive session a person
# launched from a shell that happens to carry the variable is skipped too. A proper signal
# (something the launcher sets to mean non-interactive specifically) is real follow-up work, not
# built in this plan. The direction of the error is the safe one: a skipped wizard leaves the user
# in exactly today's status quo, and `/sigma-doctor` still reports everything it would have said.
if [ -z "${SIGMA_RUN_ID:-}" ] && python3 - "$PROJECT" "$PLUGIN_ROOT" <<'PY' 2>/dev/null
import json, os, sys
project, plugin_root = sys.argv[1], sys.argv[2]
_notice = (os.environ.get("HOOK_COEXIST_NOTICE") or "") and os.environ["HOOK_COEXIST_NOTICE"] + "\n\n"
sys.path.insert(0, os.path.join(plugin_root, "skills", "sigma-init", "scripts"))
try:
    import setup_wizard
    status = setup_wizard.wizard_status(os.path.join(project, ".sdlc"))
except Exception:
    sys.exit(1)                                    # never let the wizard check break the session
if not status["needs_wizard"]:
    sys.exit(1)

lines = ["Sigma setup is incomplete. Run the sigma-wizard skill now, conversationally, before "
         "doing anything else the user asked for. For each item below: explain it in plain "
         "language (including the 'what breaks if skipped' text given), ask a real yes/no "
         "question, act only on yes, RECHECK by calling setup_wizard.wizard_status() again "
         "(never assume an action worked), and on 'no' persist the skip via "
         "setup_wizard.write_dismissed() -- a dismissed check is never asked about again until "
         "someone edits or removes .sdlc/state/setup-wizard-dismissed.json."]
for step in status["steps"]:
    lines.append(f"- [{step['mode']}] {step['name']}: {step['fix']} "
                 f"-- if skipped: {step['degraded']}")
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": _notice + "\n".join(lines),
}}))
sys.exit(0)
PY
then
    exit 0
fi
# --- Daemon stop-file present (issue #416) -------------------------------------------------------
# ACCELERATOR only: the facts and wording live in skills/sigma-loop/scripts/stopfiles.py (Python, any
# host), which /sigma-doctor reads too. A forgotten stop-file looks like a dead daemon that says
# nothing; its AGE is the tell. Same one-additionalContext idiom as the tiers around it. Placed
# BEFORE the watcher-staleness tier so the cause is named rather than a bare "looks stale".
# LIMIT: only one additionalContext per invocation, so when the setup wizard tier (earlier) fires
# this line is not shown (the coexist notice is prepended, not a competitor); /sigma-doctor's "daemon stop-files" row (Python) always covers it.
if [ -z "${SIGMA_RUN_ID:-}" ] && python3 - "$PROJECT" "$PLUGIN_ROOT" <<'PY' 2>/dev/null
import json, os, subprocess, sys
project, plugin_root = sys.argv[1], sys.argv[2]
_notice = (os.environ.get("HOOK_COEXIST_NOTICE") or "") and os.environ["HOOK_COEXIST_NOTICE"] + "\n\n"
try:
    r = subprocess.run([sys.executable, os.path.join(plugin_root, "skills", "sigma-loop", "scripts", "stopfiles.py"),
                        "line", os.path.join(project, ".sdlc")], capture_output=True, text=True, timeout=30)
    text = (r.stdout or "").strip()
except Exception:
    sys.exit(1)
if r.returncode != 0 or not text:
    sys.exit(1)
print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                         "additionalContext": _notice + "Sigma: " + text + " Run /sigma-doctor for detail."}}))
sys.exit(0)
PY
then
    exit 0
fi
# --- Proactive ledger-watcher staleness check (issue #2444) --------------------------------------
# An ACCELERATOR on top of the existing, unchanged multi-trigger watcher-restart mechanism
# (_ensure_watcher in skills/sigma-loop/scripts/loop.py, untouched by this change) -- it narrows the
# DETECTION gap (nobody has to remember to run /sigma-doctor), it never replaces the restart
# mechanism itself (AGENTS.md: "revival is layered and host-agnostic -- no single trigger is
# trusted"). Duplicates (never imports) _ledger_watcher_state's exact math from
# skills/sigma-doctor/scripts/doctor.py::_ledger_watcher_state -- name the function, never the line
# (both line references in this block had already rotted) -- only the ledger watcher is checked
# here, the other five per-watcher doctor.py state functions are out of scope for this tier. Same
# one-additionalContext-per-invocation idiom as the wizard block above: `if ... then exit 0; fi` so
# a silent tier falls through to the next one below, and a firing tier exits the whole script
# before any other tier can also print.
if [ -z "${SIGMA_RUN_ID:-}" ] && python3 - "$PROJECT" <<'PY' 2>/dev/null
import json, os, sys, time, pathlib
project = sys.argv[1]
_notice = (os.environ.get("HOOK_COEXIST_NOTICE") or "") and os.environ["HOOK_COEXIST_NOTICE"] + "\n\n"
try:
    cfg = json.loads((pathlib.Path(project) / ".sdlc" / "config.json").read_text())
except Exception:
    cfg = {}
if not isinstance(cfg, dict):
    cfg = {}
ledger_cfg = cfg.get("ledger")
ledger_cfg = ledger_cfg if isinstance(ledger_cfg, dict) else {}
if ledger_cfg.get("enabled") is not True:
    sys.exit(1)                                    # off, missing, or malformed config -> silent

watch_cfg = ledger_cfg.get("watch")
watch_cfg = watch_cfg if isinstance(watch_cfg, dict) else {}
interval = watch_cfg.get("interval_seconds")
if not isinstance(interval, (int, float)) or isinstance(interval, bool) or interval <= 0:
    interval = 900
stale_after = max(3 * interval, 180)   # mirrors doctor.py::_ledger_watcher_state / watch_daemon.py's STALE_AFTER

state_dir = pathlib.Path(project) / ".sdlc" / "state"
reason = None
try:
    hb_mtime = (state_dir / "watch.heartbeat").stat().st_mtime
except OSError:
    if (state_dir / "watch.pid").exists():
        reason = "looks dead"
    else:
        reason = "has never started"
else:
    age = time.time() - hb_mtime
    if age >= stale_after:
        reason = "looks stale"

if reason is None:
    sys.exit(1)                                    # fresh -> silent

print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": _notice + (
        "Sigma's team ledger watcher %s. The shared ledger may not be staying pushed "
        "-- run /sigma-doctor for detail." % reason
    ),
}}))
sys.exit(0)
PY
then
    exit 0
fi
# --- Proactive managing-loop liveness check (issue #265) -----------------------------------------
# The loop's Python-owned heartbeat is load-bearing; this SessionStart tier only narrows the time
# until a human sees a crash.  It deliberately examines persisted evidence and never signals a PID.
if [ -z "${SIGMA_RUN_ID:-}" ] && python3 - "$PROJECT" <<'PY' 2>/dev/null
import json, pathlib, sys, time
project = pathlib.Path(sys.argv[1])
state = project / ".sdlc" / "state"
sessions = state / "sessions"
heartbeats = state / "heartbeat"
try:
    cfg = json.loads((project / ".sdlc" / "config.json").read_text())
except Exception:
    cfg = {}
if not isinstance(cfg, dict): cfg = {}
ledger = cfg.get("ledger") if isinstance(cfg.get("ledger"), dict) else {}
watch = ledger.get("watch") if isinstance(ledger.get("watch"), dict) else {}
interval = watch.get("interval_seconds", 900)
if isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval <= 0: interval = 900
stale_after = max(3 * interval, 180)
newest = None
for marker in sorted(sessions.glob("*.active")) if sessions.is_dir() else []:
    hb = heartbeats / (marker.stem + ".json")
    try:
        data = json.loads(hb.read_text()); seen = data["last_seen"]
        if isinstance(seen, bool) or not isinstance(seen, (int, float)): raise ValueError
        age = max(0, time.time() - seen)
    except Exception:
        age = None
    # A stale abandoned session may coexist with a newer live one.  Only the newest VALID
    # heartbeat describes the current loop; malformed/absent evidence remains a dead candidate.
    rank = float("-inf") if age is None else -age
    if newest is None or rank > newest[0]: newest = (rank, age)
if newest is not None and (newest[1] is None or newest[1] >= stale_after):
    age = newest[1]
    ago = "unknown" if age is None else (f"{int(age // 3600)}h ago" if age >= 3600 else f"{int(age // 60)}m ago")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": (
        "Sigma loop died (last heartbeat %s) while a managing session was recorded; run /sigma-doctor for detail." % ago)}}))
    sys.exit(0)
sys.exit(1)
PY
then
    exit 0
fi
# --- Knowledge graph never built / stale under auto_refresh (issue #2704) ------------------------
# Same tier idiom as the two above, and the same ACCELERATOR status: the load-bearing surface is
# `kg.py warn` itself, which `loop.py next`/`next-batch` print on every host (Cursor has no hooks).
# Unlike the watcher tier this one does not duplicate the math: it runs the sibling skill's own CLI
# (skills/sigma-kg/scripts/kg.py, resolved from PLUGIN_ROOT like setup_wizard.py above), which owns
# the once-a-day stamp too, so the hook and the loop share ONE daily budget per repo. `warn` prints
# "" and exits 0 when nothing is due (graph off, auto_refresh off, graph fresh, warned today), so
# the python wrapper below exits 1 to fall through in exactly that case. Headless skip as above.
if [ -z "${SIGMA_RUN_ID:-}" ] && python3 - "$PROJECT" "$PLUGIN_ROOT" <<'PY' 2>/dev/null
import json, os, subprocess, sys
project, plugin_root = sys.argv[1], sys.argv[2]
_notice = (os.environ.get("HOOK_COEXIST_NOTICE") or "") and os.environ["HOOK_COEXIST_NOTICE"] + "\n\n"
kg_py = os.path.join(plugin_root, "skills", "sigma-kg", "scripts", "kg.py")
try:
    r = subprocess.run([sys.executable, kg_py, "warn", os.path.join(project, ".sdlc")],
                       capture_output=True, text=True, timeout=30)
    text = (r.stdout or "").strip()
except Exception:
    sys.exit(1)                                    # a broken helper must never break the session
if r.returncode != 0 or not text:
    sys.exit(1)                                    # nothing due -> silent, fall through
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": _notice + "Sigma " + text,
}}))
sys.exit(0)
PY
then
    exit 0
fi
# --- existing opt-in policy brief (unchanged below) --------------------------------------------
[ -f "$CFG" ] || allow

# Exit 1 when silent, so allow() still emits the coexistence notice when there is one.
python3 - "$PROJECT" <<'PY' 2>/dev/null || allow
import json, os, sys
project = sys.argv[1]
_notice = (os.environ.get("HOOK_COEXIST_NOTICE") or "") and os.environ["HOOK_COEXIST_NOTICE"] + "\n\n"
try:
    cfg = json.load(open(os.path.join(project, ".sdlc", "config.json")))
except Exception:
    sys.exit(1)                                    # unreadable config -> silent
if not isinstance(cfg, dict) or (cfg.get("session_start") or {}).get("enabled") is not True:
    sys.exit(1)                                    # off by default -> silent

# doctor-lite install self-check: warn (never block) on a half-set-up adoption.
warnings = []
if not os.path.exists(os.path.join(project, ".sdlc", "context", "north-star.md")):
    warnings.append("no north-star yet - run /sigma-vision to set the direction the plan-review gate checks.")

policy = (
    "Sigma SDLC is active in this repo. Work rides the loop: "
    "Goal -> Research -> Plan -> Plan-Review -> Implement -> Review -> Retrospective. "
    "Run /sigma-loop to drain the backlog autonomously, or /sigma-goal for a single goal. "
    "Ground every change in .sdlc/context/north-star.md and the repo's CLAUDE.md. "
    "Plan before editing source; every changed behavior carries a test; the reviewer should not be the author."
)
if warnings:
    policy = "Sigma setup notes: " + " ".join(warnings) + "\n\n" + policy

print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": _notice + policy,
}}))
PY
exit 0
