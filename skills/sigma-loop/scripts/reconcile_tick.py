#!/usr/bin/env python3
"""#2294: give `reconcile.py`'s AUTOMATIC-tier sweep a wall-clock heartbeat independent of whether
anyone is actively driving `/sigma-loop`.

PC-4 (design #2287): `loop.py`'s `_reconcile_sweep` is already the correct, fully-gated
entry point for this -- `discovery.reconcile.mode` (default 'off'), then a TTL watermark
(`discovery.reconcile.ttl_minutes`, default 60), then `reconcile.sweep_reconcile` itself. It is
only ever CALLED from inside `next_batch`/`_next` (loop.py:1828-1829, 2060), so an idle repo --
nobody running `/sigma-loop` -- gets zero board sanitation no matter how the TTL is set, because
nothing calls the function that checks it. This script is that second caller: `watch_daemon.py`'s own
tick sequence (BR-13) and the `AUTOWATCH.md` Desktop/CLI adapter pattern (BR-14) are both already
periodic and host-agnostic, so the fix is one more line in a loop that already exists, not a new
watcher class.

Thin wiring, mirroring `agent_watch.py`/`comment_watch.py`'s own shape exactly: same
`tick(sdlc_dir, config=None, ...)` -> one-line-summary-or-"" contract, same
`main(argv)` CLI convention, same non-fatal failure posture (a sweep failure must never crash the
watch_daemon.py loop -- AGENTS.md's RESILIENCY bar).

NO new config key, NO new gate. `tick()` calls `loop._reconcile_sweep(sdlc_dir, config, ...)`
directly rather than re-deriving `discovery.reconcile.mode`/`ttl_minutes`/the TTL watermark here --
that function is ALREADY exactly this: opt-in gated, TTL-throttled, and fail-open (never raises).
Reusing it, rather than hand-rolling a second copy of the same three gates, is what makes "the same
config an operator already understands governs both the loop-driven and the watch_daemon.py-driven
trigger" true by construction, not by two independently-maintained copies staying in sync by hand --
and it inherits `_reconcile_sweep`'s own, already-tested idempotency for free: two calls inside the
same TTL window cost one `gh` census, whichever caller gets there first.

Deliberately reaches into `loop.py`'s underscore-prefixed `_reconcile_sweep` -- Python enforces no
real privacy boundary between sibling scripts in this skill, and `autowatch.py`'s own
`_check_no_concurrent_session` already sets this precedent for `loop._goal_has_registered_worker`.

    python3 reconcile_tick.py <sdlc_dir>
"""
import importlib.util
import json
import pathlib
import sys
import time

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


ledger = _load("ledger")
loop = _load("loop")


def tick(sdlc_dir, config=None, run=None, now=None):
    """-> a one-line summary for watch_daemon.py's log, or "" when nothing needed anyone. Mirrors
    agent_watch.py/comment_watch.py's tick() shape exactly.

    `config=None` reads `config.json` fresh via `ledger._config` (same idiom as both siblings), so
    a config edit between two ticks is picked up on the very next one, matching every other
    watch-tick script in this kit."""
    config = config if config is not None else ledger._config(sdlc_dir)
    swept = loop._reconcile_sweep(sdlc_dir, config, run=run, now=now)
    # #232: the second, UNGATED duty of this tick -- close goals `record review` left waiting whose
    # PR has since merged. Not behind `discovery.reconcile.mode` (default off): done-means-merged is
    # the shipped behaviour, so its close must not depend on an opt-in. Bounded and throttled inside
    # `_reconcile_awaiting_merges` itself; zero `gh` calls when nothing is awaiting. #255: it holds a
    # kernel lock (a racing `next` skips rather than recording `done` twice) and stops starting goals
    # after half of SIGMA_WATCH_CALL_TIMEOUT, the bound this very call is killed by.
    merged = loop._reconcile_awaiting_merges(sdlc_dir, config, run=run, now=now)
    backfilled = _board_backfill(sdlc_dir, config, run)
    repaired = _blocker_rank_repair(sdlc_dir, config, run, now)
    parts = []
    if swept:
        parts.append("%d issue(s) reconciled — %s" % (
            len(swept), ", ".join("#%s" % issue for issue in sorted(swept))))
    if merged:
        parts.append("%d awaiting-merge goal(s) recorded — %s" % (
            len(merged), ", ".join("%s %s" % (goal, outcome) for goal, outcome, _pr in merged)))
    if backfilled:
        parts.append("%d board card(s) backfilled" % backfilled)
    if repaired:
        parts.append(repaired)
    return "; ".join(parts)


STATE_NAME = "blocker-repair.meta.json"


def _state_path(sdlc_dir):
    return pathlib.Path(sdlc_dir) / "state" / STATE_NAME


def read_repair_state(sdlc_dir):
    """-> {violations, last_repair, first_seen_nonzero, rollout_reported}; {} when absent or unreadable."""
    try:
        data = json.loads(_state_path(sdlc_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:                       # noqa: BLE001
        return {}


def _write_repair_state(sdlc_dir, record):
    """One small record, atomic replace. A failure costs one extra measurement, never a crash."""
    try:
        path = _state_path(sdlc_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        _load("state").atomic_write_text(path, json.dumps(record, sort_keys=True))
    except Exception:                       # noqa: BLE001
        pass


def always_repair_has_run(state):
    """#1526: True for a record the always-mode repair wrote (a count, and no other mode). The smart skip
    marker ({"mode": "smart", "rollout_reported": true}, or a legacy one with no count) is not one."""
    return state.get("mode", "always") == "always" and "violations" in state


def _blocker_rank_repair(sdlc_dir, config, run=None, now=None):
    """#1410: repair blockers ranked below what they block (`GitHubSource.repair_blocker_ranks`), after
    the board back-fill. Github source only; zero calls and no state file when the rule is off. Never
    fatal. The first run records a dry count on stderr before it raises anything. -> a summary part or ""."""
    try:
        disc = config.get("discovery") or {}
        if disc.get("source") != "github":
            return ""
        sources = _load("sources")
        src = sources.GitHubSource(config, run=run, sdlc_dir=sdlc_dir)
        if src.blocker_promotion_mode == "smart":
            # #1489: smart is the board pass only; say so once instead of staying silent.
            if read_repair_state(sdlc_dir).get("mode") != "smart":
                print("reconcile_tick: blocker rank repair skipped: mode is smart; set always to enable",
                      file=sys.stderr)
                _write_repair_state(sdlc_dir, {"mode": "smart", "rollout_reported": True})
            return ""
        if src.blocker_promotion_mode != "always":
            return ""
        now = now if now is not None else time.time()
        prior = read_repair_state(sdlc_dir)
        if not (prior.get("rollout_reported") and always_repair_has_run(prior)):
            dry = src.repair_blocker_ranks(apply=False)
            print("reconcile_tick: blocker rank repair, first run: %d blocker(s) ranked below what they "
                  "block would be raised" % dry["violations"], file=sys.stderr)
        res = src.repair_blocker_ranks()
        if res.get("skipped"):
            return ""
        wrote = res["raised"] or res["stripped"] or res["unmarked"]
        found = res["violations"]
        first = prior.get("first_seen_nonzero")
        _write_repair_state(sdlc_dir, {
            "violations": found,
            "last_repair": now if wrote else prior.get("last_repair"),
            "first_seen_nonzero": (first or now) if found else None,
            "rollout_reported": True})
        bits = []
        if res["raised"]:
            bits.append("%d blocker(s) raised" % len(res["raised"]))
        if res["stripped"]:
            bits.append("%d confirmation label(s) stripped" % len(res["stripped"]))
        if res["unmarked"]:
            bits.append("%d blocking mark(s) removed" % len(res["unmarked"]))
        return ", ".join(bits)
    except Exception:                       # noqa: BLE001 - a watcher tick is never fatal
        return ""


def _board_backfill(sdlc_dir, config, run=None):
    """#1305: the capped, rate-limit-aware board back-fill that single-issue verbs no longer do inline.
    Closed (0, no gh call) unless the source is github with the project board enabled; never raises."""
    try:
        disc = config.get("discovery") or {}
        proj = (disc.get("github") or {}).get("project") or {}
        if disc.get("source") != "github" or not proj.get("enabled"):
            return 0
        return _load("sources").GitHubSource(config, run=run, sdlc_dir=sdlc_dir).backfill_board()
    except Exception:                       # noqa: BLE001 - a watcher tick is never fatal
        return 0


USAGE = "usage: reconcile_tick.py [sdlc_dir]"


def main(argv):
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    sdlc_dir = argv[1] if len(argv) > 1 else ".sdlc"
    try:
        print(tick(sdlc_dir))
    except Exception as exc:                    # noqa: BLE001 - a watcher tick is never fatal
        print(f"reconcile_tick: tick failed (non-fatal): {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
