"""The upkeep pass under the opt-in: the first live entry point behind the upkeep gate (project door).

WHAT IT DOES TODAY. One question and one record. `upkeep_pass` asks whether a long-lived unit branch has already LANDED in
its base, and if so skips it and says why. A landed unit is never moved to the base's tip. It does not rebase, push, back
up, resolve or file anything yet: a unit that has not landed comes back as `not-landed`, which is the hand-off point for
the engine slices that follow.

LANDED means one of two things, as one predicate:
  landed-contained  the unit tip is an ancestor of the base tip (a merge or rebase landing leaves it so);
  landed-pr         a merged landing request into the base has this exact unit tip as its head (the only thing that
                    recognises a squash landing). The requests are supplied by the caller as a callable returning
                    dicts with `merged` (true), `base` and `head_sha`; this module makes no network call. A unit that
                    later gets NEW commits has a different tip, so its already-landed commits are never replayed.

GATE. The decorator is the first action: with the gate closed nothing runs, nothing is read, written or spawned. The
decorator is the only reader of the config block.

TOTAL. A git question that cannot be answered (a failed or timed-out command, a name that is not a commit) reads as
`unreadable` and records nothing: "could not tell" is never "landed". Nothing here raises for a git failure.

STATE. A skip records the pass outcome `current` for the unit, with both tips, in the per-unit upkeep state (so the drift
measure counts from the right anchor and the backstop is not re-armed for a branch with nothing to do).
"""
import importlib.util
import pathlib
import re

_HERE = pathlib.Path(__file__).resolve().parent
_LOADED = {}


def _sibling(name):
    if name not in _LOADED:
        spec = importlib.util.spec_from_file_location(name, _HERE / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _LOADED[name] = module
    return _LOADED[name]


feature_upkeep = _sibling("feature_upkeep")

GIT_TIMEOUT_SECONDS = 60
LANDED_CONTAINED = "landed-contained"
LANDED_PR = "landed-pr"
NOT_LANDED = "not-landed"
UNREADABLE = "unreadable"
_HEX = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _git(cwd, argv):
    """The default runner, `(cwd, argv) -> stdout`, raising on a failed, refused or timed-out command."""
    bounded = _sibling("bounded_run")
    result = bounded.run_group(["git"] + list(argv), str(cwd), GIT_TIMEOUT_SECONDS, env=bounded.unattended_env(), merge=False)
    if result.outcome != bounded.OK:
        raise RuntimeError("git %s: %s" % (argv[0] if argv else "", result.outcome))
    return (result.out or "").strip()


def _tip(run, cwd, ref):
    if not (isinstance(ref, str) and ref and not ref.startswith("-")):
        raise ValueError("not a ref")
    tip = run(cwd, ["rev-parse", "--verify", "--quiet", ref + "^{commit}"]).strip()
    if not _HEX.fullmatch(tip):
        raise ValueError("not a commit")
    return tip


def landed(run, cwd, unit_ref, base_ref, base_name, landing_prs=None):
    """-> (verdict, unit_tip, base_tip): verdict is LANDED_CONTAINED, LANDED_PR, NOT_LANDED or UNREADABLE."""
    try:
        unit_tip, base_tip = _tip(run, cwd, unit_ref), _tip(run, cwd, base_ref)
        ahead = run(cwd, ["rev-list", "--max-count=1", unit_tip, "--not", base_tip]).strip()
    except Exception:                                       # noqa: BLE001 - could not tell is never landed
        return UNREADABLE, None, None
    if ahead == "":
        return LANDED_CONTAINED, unit_tip, base_tip
    try:
        requests = list(landing_prs()) if landing_prs else []
    except Exception:                                       # noqa: BLE001 - the API path is best effort; the git answer stands
        requests = []
    for request in requests:
        if (isinstance(request, dict) and request.get("merged") is True and request.get("base") == base_name
                and request.get("head_sha") == unit_tip):
            return LANDED_PR, unit_tip, base_tip
    return NOT_LANDED, unit_tip, base_tip


@feature_upkeep.gated("project")
def upkeep_pass(config, sdlc_dir, unit, unit_ref, base_ref, base_name, now, *, run=None, cwd=".", landing_prs=None):
    """One pass for one unit. -> {"result": "skipped"|"not-landed"|"unreadable", "reason", "unit", "unit_tip", "base_tip"}."""
    verdict, unit_tip, base_tip = landed(run or _git, cwd, unit_ref, base_ref, base_name, landing_prs)
    report = {"result": NOT_LANDED, "reason": verdict, "unit": unit, "unit_tip": unit_tip, "base_tip": base_tip}
    if verdict == UNREADABLE:
        report["result"] = UNREADABLE
    elif verdict in (LANDED_CONTAINED, LANDED_PR):
        report["result"] = "skipped"
        state = _sibling("feature_upkeep_state").record_outcome(sdlc_dir, unit, now, "current", unit_tip, base_tip)
        report["recorded"] = bool(getattr(state, "ok", False))
    return report
