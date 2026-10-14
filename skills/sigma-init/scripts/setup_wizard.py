# SPDX-License-Identifier: MIT
"""The guided first-run setup wizard's pure decision logic (issue #1560).

Wraps doctor.check() rather than re-detecting anything -- verified directly, doctor.check() runs
safely against a repo with no .sdlc/ at all and reports it as its own first failing check
("project layer"). This module's only job is CLASSIFICATION (how should a failing check be
handled?) and PERSISTENCE (has the user already said no to this one?) -- never re-implementing
detection doctor.py already owns.

WHY THREE MODES, NOT TWO. "auto_fixable" (we can act, with consent) and "guidance_only" (we can
only describe manual steps) looked sufficient at first, but gh auth login/gh auth refresh are a
real third case: NOT auto-fixable (interactive OAuth, no tool should attempt it), but NOT merely
"guidance" either -- doctor.check() genuinely CAN recheck these afterward, unlike the true
GitHub-UI-only quirks (board view layout) which cannot be checked at all. Collapsing "human must
run one command, then we verify" into "guidance_only" (which offers no recheck) would silently
drop the recheck this plan's own done_when requires. Kept as its own mode: "human_command".

THE HARD REQUIREMENT THIS FILE ENCODES: every step ships a `degraded` string, in the SAME
structure the caller receives -- stating in plain language what does not work while this check
stays unresolved. This is not optional decoration; per this session's explicit product
requirement, no fallback may be presented without stating its cost in the same breath.
"""
import json
import pathlib
import time


def _doctor_check(sdlc_dir=".sdlc", run=None, scheduled_tasks_dir=None, site_packages_dirs=None):
    """Imported lazily, inside the function that uses it, not at module load -- doctor.py pulls
    in real subprocess/gh machinery this module's own pure tests must never need. Module-level
    indirection (this wrapper, not a bare `from doctor import check`) is what lets tests
    monkeypatch `setup_wizard._doctor_check` without needing doctor.py importable at all.

    `cheap_only=True` IS NOT OPTIONAL HERE. This wrapper's only caller is `wizard_status()`, whose
    only caller is the UNCONDITIONAL SessionStart hook -- so anything expensive in `doctor.check()`
    is paid once per session, in every repo, forever. Before this feature, every session_start
    behaviour sat behind an opt-in flag; making the wizard unconditional (correctly -- silence was
    the original bug) inherited none of that consent, and `_plugin_versions()` plus the companions
    row spawn the `claude` CLI and curl raw.githubusercontent.com. Measured on a `{}`-config repo,
    same box, same run: 3.82s cold / 3.00s warm for the full check, 0.01s with cheap_only=True.
    (An earlier note here said 3.97s; that was a different sample of the same call.) AGENTS.md's SAFETY
    property is explicit that nothing spawns processes or sends data without the operator opting
    in, so the wizard's own path takes the cheap subset and leaves the full sweep -- network
    included -- to an explicit `/sigma-doctor`, which the user typed on purpose."""
    import importlib.util
    path = pathlib.Path(__file__).resolve().parent.parent.parent / "sigma-doctor" / "scripts" / "doctor.py"
    spec = importlib.util.spec_from_file_location("doctor", path)
    doctor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doctor)
    return doctor.check(sdlc_dir=sdlc_dir, run=run,
                        scheduled_tasks_dir=scheduled_tasks_dir,
                        site_packages_dirs=site_packages_dirs,
                        cheap_only=True)


#: Per-check classification, AND the wizard's allow-list. Keyed on doctor.py's own check `name`
#: string -- brittle to a rename over there, but doctor.py's names are user-facing prose already
#: (they're printed verbatim by `/sigma-doctor`), so a rename is a deliberate, visible product
#: decision, not an internal refactor that would silently desync this table.
#:
#: THIS TABLE IS AN ALLOW-LIST, NOT A LOOKUP. `wizard_status()` only ever turns a failing doctor
#: check into a wizard step when its name is a key HERE. That is the difference between a SETUP
#: wizard and a nag: doctor.check() has ~26 checks, and the great majority are ongoing hygiene
#: rows ("team ledger initialized", "hand-off owner roster configured", the census/label-coherence
#: scans, and several whose names embed live-changing values like "dependency markers: ... (3/12
#: open goal(s))"). Letting those fall through to a generic default meant the wizard declared
#: "setup is incomplete, run the wizard before anything else the user asked for" on every mature,
#: perfectly-working repo, forever -- a first-run wizard firing on a repo whose first run was
#: months ago. It also leaked _DEFAULT_MODE's placeholder prose to users, and dismissals could not
#: stick for a check whose name embeds a counter that changes between sessions.
#:
#: The seven below are the genuine first-run setup gaps: things a NEW adopter has not done yet, each
#: with authored, verified "what breaks if you skip this" text. A future check joins the wizard by
#: being added here with that text -- deliberately an explicit act, never automatic.
_MODES = {
    "project layer": ("auto_fixable",
        "Without this, nothing in Sigma works at all -- no loop, no board, "
        "no journal. Nothing runs until this exists."),
    "gh auth": ("human_command",
        "Without GitHub login, the loop cannot read your real issue backlog, create PRs, or "
        "update anything on GitHub. It can still run entirely on local goal files, which is a "
        "genuinely different, usually much smaller set of work -- not a full substitute."),
    # #229: with gh absent, doctor no longer emits a "gh auth" row (its prerequisite failed) --
    # this row is that same first-run gap, named correctly: install gh, not `gh auth login`.
    "gh installed": ("human_command",
        "Without the GitHub CLI, the loop cannot read your real issue backlog, create PRs, or "
        "update anything on GitHub. It can still run entirely on local goal files -- install gh, "
        "then log in, to get the rest."),
    "gh project scope": ("human_command",
        "Without this specific permission, the loop can still create and manage issues -- it "
        "just cannot move cards on your visual board. Issue tracking keeps working; only the "
        "Kanban view stops updating, silently, with no error."),
    "board marks closed items Done": ("guidance_only",
        "Without this, a card stays wherever it was when its issue closes, instead of moving to "
        "Done automatically. Cosmetic only -- nothing about issue tracking itself is affected."),
    "no open issue stranded at board Done": ("guidance_only",
        "A reopened issue's card can get stuck showing Done even though the work isn't. "
        "Cosmetic only -- the issue's real state is unaffected."),
    # #228: the permanent-refusal trap. A first-run gap, not hygiene: an older /sigma-init shipped
    # it as the default, and until it is fixed EVERY goal is refused at `done`. human_command, not
    # auto_fixable: only the user knows which command proves their repo -- the wizard must not
    # guess one. doctor.py's fix text carries the exact one-line gesture; doctor rechecks.
    "verify command present (enforce is on)": ("human_command",
        "Until this is fixed, EVERY goal is refused at `record done`: verify.enforce is on but no "
        "verify command exists, so `loop.py verify` has nothing to run (NO-COMMAND) and no goal "
        "can ever finish. Set the command, or turn enforce off -- either unblocks the loop."),
    "graphify installed": ("auto_fixable",
        "Without this, the knowledge graph feature silently does nothing -- Claude won't recall "
        "prior decisions automatically. Every other phase of the SDLC works identically either way."),
}

#: `_classify`'s fallback for a name that is not in `_MODES`. UNREACHABLE from `wizard_status()`,
#: which filters on `_MODES` membership before it ever classifies anything -- kept as a defensive
#: property of `_classify()` itself, so a direct caller with an unrecognised name gets the most
#: conservative mode (never claims to auto-fix or recheck) rather than a KeyError.
_DEFAULT_MODE = ("guidance_only",
    "This check is failing and has no specific explanation wired up yet -- treat it as "
    "informational until someone adds one; nothing has been verified about its real impact.")


def _classify(name):
    return _MODES.get(name, _DEFAULT_MODE)


def _dismissed_path(sdlc_dir):
    return pathlib.Path(sdlc_dir) / "state" / "setup-wizard-dismissed.json"


def read_dismissed(sdlc_dir):
    """Never raises: a missing or corrupt dismissal file reads as "nothing dismissed yet",
    the same fail-open posture every other state file in this codebase uses."""
    try:
        data = json.loads(_dismissed_path(sdlc_dir).read_text(encoding="utf-8"))
        return set(data) if isinstance(data, list) else set()
    except (OSError, ValueError):
        return set()


def _adopted(sdlc_dir):
    """Has this repo actually adopted Sigma? THE CONSENT GATE FOR EVERY WRITE THIS MODULE
    MAKES. The SessionStart hook fires in EVERY repo the user opens, including ones that have
    never heard of Sigma -- so an unconditional `mkdir(parents=True)` in here created an
    untracked `.sdlc/state/...` file, and a dirty `git status`, in a stranger's repo before the
    user was ever asked anything. That directly breaches this plan's own Global Constraint ("a
    user who skips everything ends up in exactly today's status quo -- never worse, never silently
    different"), so both writers below no-op until `.sdlc/` itself exists.

    `.sdlc/` (the directory), not `.sdlc/state/`, is the tell: `/sigma-init` and the wizard's own
    `run_scaffold()` both create the directory, and that act IS the adoption. Nothing is lost by
    not caching in an unadopted repo -- the only check `wizard_status()` can report there is
    "project layer", which is a local `.exists()` call with no `gh` and no network behind it, so
    there is no expensive result worth persisting in the first place."""
    return pathlib.Path(sdlc_dir).is_dir()


def adopted_by_sigma(sdlc_dir):
    """#236 / #186: may the wizard SPEAK here? Stricter than `_adopted` (which only gates this
    module's own writes): the repository must have adopted Sigma -- `.sdlc/config.json` exists, the
    ONE adoption marker every gate hook reads (`hooks/gate_state.py:adopted_root`) -- and no other
    plugin has claimed the directory (`state/owner.json`, written by init and loop start, #240).

    Before this the wizard fired in EVERY repository the user opened: with no `.sdlc/`, doctor's
    "project layer" row fails and is an allow-listed step, and because the writers no-op there a
    decline could never be remembered -- a nag, forever, in repos that never asked for Sigma.
    `/sigma-init` is the entry point for a new repository; the wizard is for an adopted one.
    Never raises: anything unreadable reads as "not adopted" -- silence, today's status quo."""
    try:
        base = pathlib.Path(sdlc_dir)
        if not (base / "config.json").is_file():
            return False
        owner = json.loads((base / "state" / "owner.json").read_text(encoding="utf-8"))
        return not (isinstance(owner, dict) and isinstance(owner.get("plugin"), str)
                    and owner["plugin"] != "sigma")
    except (OSError, ValueError):
        return (pathlib.Path(sdlc_dir) / "config.json").is_file()


def sigma_scaffold_interrupted(sdlc_dir):
    """Review of PR #286: a `.sdlc/` Sigma OWNS (`state/owner.json` says "sigma" -- `/sigma-init`
    writes it before it scaffolds) but with no `config.json` is an interrupted `/sigma-init`. The
    adoption gate above keeps the wizard silent there (no config.json = not adopted), which left
    that user with nothing; this is the one exception, and it reaches no doctor. An ownerless bare
    `.sdlc/` (another tool's) or another plugin's stays silent. Never raises."""
    try:
        base = pathlib.Path(sdlc_dir)
        if (base / "config.json").exists():
            return False
        owner = json.loads((base / "state" / "owner.json").read_text(encoding="utf-8"))
        return isinstance(owner, dict) and owner.get("plugin") == "sigma"
    except (OSError, ValueError):
        return False


#: The flow the interrupted-scaffold fix names; a module attribute so a test can point it at a path
#: with a space in it and see the printed gesture quoted.
INIT_FLOW = pathlib.Path(__file__).resolve().parent / "init_flow.py"


def _init_gesture():
    """`<python> <init_flow.py> .`, built by the SAME helpers every other printed gesture uses
    (`verify_detect.python_command` / `_q`): `python3`, else `python`, else `py` (Windows), and the
    path quoted for the host's shell (so a path with a space, or a Windows one, runs as pasted).
    Loaded lazily, and only on this rare branch -- verify_detect is stdlib-only."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "setup_wizard_verify_detect", str(pathlib.Path(__file__).resolve().parent / "verify_detect.py"))
    vd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vd)
    return f"{vd.python_command()} {vd._q(str(INIT_FLOW))} ."


def _interrupted_step():
    return {
        "name": "project layer",
        "fix": f"re-run /sigma-init (Codex/Cursor: {_init_gesture()}): .sdlc/ is Sigma's but has no "
               "config.json -- an interrupted scaffold. It is skip-if-exists, so re-running it keeps "
               "every file already there.",
    }


def write_dismissed(sdlc_dir, names):
    """Best-effort: a wizard that cannot persist a skip must still let the user proceed with
    their actual request this session -- it would just ask again next time, which is annoying,
    not broken. No-ops entirely (same silent posture) in a repo that has no `.sdlc/` yet -- see
    `_adopted`.

    Only names in `_MODES` are kept (B6 #466): `wizard_status` ignores every other name, so one
    could never suppress a step, and dropping it here bounds the file to `len(_MODES)` names by
    code rather than by what the skill prose happens to pass. A non-string entry is dropped too
    instead of raising out of `sorted`. Version skew: an older plugin rewriting this file drops a
    name only a newer plugin knows, and the user is asked about that step once more."""
    if not _adopted(sdlc_dir):
        return
    path = _dismissed_path(sdlc_dir)
    try:
        kept = sorted({n for n in names if isinstance(n, str) and n in _MODES})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(kept), encoding="utf-8")
    except (OSError, TypeError):
        pass


#: Plan-review finding A. doctor.check() in GitHub mode makes real `gh auth status` / `gh project
#: list` / `gh project item-list` / `gh project field-list` calls (confirmed directly against
#: doctor.py). Re-running that on every single SessionStart forever, once a repo is already
#: healthy, is a real ongoing cost -- API quota (this repo's own history includes a quota
#: exhaustion that blocked PR creation across every loop slot at once) and real network latency
#: before the user's first message even gets processed. One hour: long enough that the common,
#: steady-state case (already fully set up) pays almost nothing; short enough that a genuinely
#: new problem (a revoked token, a permission pulled) surfaces within an hour rather than being
#: silently stale for a day. The same mtime early-out a downstream shipper's scheduler uses -- same
#: trade-off, same shape of fix.
_CACHE_TTL_SECONDS = 3600

#: #1334: a result whose ONLY failing wizard row is `gh auth` may be a blip, not a state. It is
#: cached this briefly (key `transient_until`) so a second session start makes no gh calls.
_TRANSIENT_TTL_SECONDS = 120
_TRANSIENT_FIX = ("could not confirm the gh login (possibly transient); "
                  "re-run /sigma-doctor to check it")


def _cache_path(sdlc_dir):
    return pathlib.Path(sdlc_dir) / "state" / "setup-wizard-cache.json"


def _read_cache(sdlc_dir):
    """Never raises -- a missing, corrupt or WRONGLY-TYPED cache reads as "no cache", forcing a
    real check, which is always the safe direction to fail in (worst case: pays a cost it could
    have skipped; never: skips a check it should have run).

    The types are validated, not just the keys: a file that is valid JSON and has both keys but
    holds e.g. `{"checked_at": "yesterday"}` would otherwise raise TypeError out of the arithmetic
    in `wizard_status()`. The hook's own outer `except Exception` would swallow that, but the
    SKILL.md-documented direct call (`setup_wizard.wizard_status(...)` from the wizard skill) has
    no such net, so the guard belongs here where the bad data is read."""
    try:
        data = json.loads(_cache_path(sdlc_dir).read_text(encoding="utf-8"))
        if (isinstance(data, dict)
                and isinstance(data.get("checked_at"), (int, float))
                and not isinstance(data.get("checked_at"), bool)
                and isinstance(data.get("needs_wizard"), bool)):
            return data
    except (OSError, ValueError):
        pass
    return None


def _write_cache(sdlc_dir, needs_wizard, now=None, transient=False):
    """No-ops entirely in a repo that has not adopted Sigma -- see `_adopted`."""
    if not _adopted(sdlc_dir):
        return
    now = now if now is not None else time.time()
    try:
        _cache_path(sdlc_dir).parent.mkdir(parents=True, exist_ok=True)
        _cache_path(sdlc_dir).write_text(
            json.dumps(dict({"checked_at": now, "needs_wizard": needs_wizard},
                            **({"transient_until": now + _TRANSIENT_TTL_SECONDS}
                               if transient else {}))), encoding="utf-8")
    except OSError:
        pass


def _only_auth_failing(checks):
    bad = [c["name"] for c in checks if not c["ok"] and c["name"] in _MODES]
    # #1399: with a project board on, an unconfirmed login also fails "gh project scope"; that pair
    # is the same blip. Scope alone, or any other row alongside, is a real state.
    return "gh auth" in bad and set(bad) <= {"gh auth", "gh project scope"}


def _transient_result(dismissed):
    """#1334: never advise `gh auth login` from the session hook on one failed check."""
    if "gh auth" in dismissed:
        return {"needs_wizard": False, "steps": []}
    mode, degraded = _classify("gh auth")
    return {"needs_wizard": True,
            "steps": [{"name": "gh auth", "fix": _TRANSIENT_FIX, "mode": mode, "degraded": degraded}]}


def wizard_status(sdlc_dir, run=None, dismissed=None, allow_cache=True, now=None):
    """The one entry point the hook calls. `dismissed`, when None, is read from disk; tests pass
    an explicit set to avoid touching the filesystem for pure-logic assertions.

    THE CACHE ONLY EVER SHORT-CIRCUITS A CLEAN RESULT. A cache recording `needs_wizard: True` is
    never trusted, regardless of age -- an active, unresolved problem must always be rechecked
    promptly, not silently served stale for up to an hour. This is the one invariant that makes
    the whole cache safe to have added: it can only make a healthy repo cheaper to keep checking,
    never make a broken one look healthy for longer than a single tick."""
    now = now if now is not None else time.time()
    if not adopted_by_sigma(sdlc_dir):
        if sigma_scaffold_interrupted(sdlc_dir) and "project layer" not in (
                read_dismissed(sdlc_dir) if dismissed is None else dismissed):
            mode, degraded = _classify("project layer")
            return {"needs_wizard": True, "steps": [dict(_interrupted_step(), mode=mode, degraded=degraded)]}
        return {"needs_wizard": False, "steps": []}      # #186: not adopted -> say nothing
    if allow_cache:
        cached = _read_cache(sdlc_dir)
        # `0 <=` matters: a NEGATIVE delta means the clock moved backwards since the cache was
        # written (a laptop resuming with a corrected clock, an NTP step, a timezone-naive
        # restore). Without the lower bound, `-86400 < 3600` reads as "fresh" and keeps reading
        # that way for as long as the skew lasts -- an unbounded stale window, not an hour-long
        # one. Treating a backwards clock as "no usable cache" costs one real check.
        if (cached is not None and cached["needs_wizard"] is False
                and 0 <= (now - cached["checked_at"]) < _CACHE_TTL_SECONDS):
            return {"needs_wizard": False, "steps": []}
        until = cached.get("transient_until") if cached is not None else None
        if (isinstance(until, (int, float)) and not isinstance(until, bool)
                and 0 <= (now - cached["checked_at"]) and now < until):
            if dismissed is None:
                dismissed = read_dismissed(sdlc_dir)
            return _transient_result(dismissed)

    if dismissed is None:
        dismissed = read_dismissed(sdlc_dir)
    checks = _doctor_check(sdlc_dir=sdlc_dir, run=run)
    if _only_auth_failing(checks):
        checks = _doctor_check(sdlc_dir=sdlc_dir, run=run)      # #1334: one retry of a blip
        if _only_auth_failing(checks):
            result = _transient_result(dismissed)
            if allow_cache:
                _write_cache(sdlc_dir, result["needs_wizard"], now=now, transient=True)
            return result
    steps = []
    for c in checks:
        # `not in _MODES` is the allow-list gate (see `_MODES`): only the seven explicitly-authored
        # FIRST-RUN SETUP checks can ever become a wizard step. Every other failing doctor row --
        # ongoing hygiene, board-state drift, anything added to doctor.py later -- is silently
        # ignored here and left to `/sigma-doctor`, which is where it belongs. This is also what
        # keeps "north-star filled" permanently out of scope (a content/strategic-quality concern
        # the plan-review gate and /sigma-align both already treat as a soft no-op) without needing
        # a separate exclusion set to maintain alongside this one.
        if c["ok"] or c["name"] not in _MODES or c["name"] in dismissed:
            continue
        mode, degraded = _classify(c["name"])
        steps.append({"name": c["name"], "fix": c["fix"], "mode": mode, "degraded": degraded})
    result = {"needs_wizard": bool(steps), "steps": steps}
    if allow_cache:
        _write_cache(sdlc_dir, result["needs_wizard"], now=now)
    return result
