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
import json
import os
import pathlib
import re
import tempfile

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


def _shared():
    """The shared predicate's runner `(argv, cwd) -> (rc, out, err)`, built on the bounded group runner. It keeps the exit code
    (an ancestry answer of "no" is exit 1, a normal answer), which the engine runner's raise-on-failure shape would lose."""
    shared = _sibling("feature_landed")

    def default(argv, cwd):
        bounded = _sibling("bounded_run")
        result = bounded.run_group([str(a) for a in argv], str(cwd), GIT_TIMEOUT_SECONDS, env=bounded.unattended_env(),
                                   merge=False)
        if result.code is None:
            return shared.RC_UNRUNNABLE, "", ""
        return result.code, result.out or "", result.err or ""
    return default


def landed(run, cwd, unit_ref, base_ref, base_name, landing_prs=None):
    """-> (verdict, unit_tip, base_tip): verdict is LANDED_CONTAINED, LANDED_PR, NOT_LANDED or UNREADABLE. The ancestry half is
    the shared predicate's (`feature_landed`); the merged-request half reads an injected source (no network here)."""
    shared, runner = _sibling("feature_landed"), _shared()
    try:
        unit_tip, base_tip = _tip(run or _git, cwd, unit_ref), _tip(run or _git, cwd, base_ref)
        answer, _, _ = shared._ancestry(runner, str(cwd), unit_tip, base_tip)
    except Exception:                                       # noqa: BLE001 - could not tell is never landed
        return UNREADABLE, None, None
    if answer == "unknown":
        return UNREADABLE, None, None
    if answer == "yes":
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


# ------------------------------------------------------------------------------------------ the engine's settings

#: Seconds per git verb for the engine's runner. Bounds, not tuning: a hung command is killed with its whole group.
GIT_LIMITS = {"default": GIT_TIMEOUT_SECONDS, "fetch": 120, "push": 120, "ls-remote": 60, "rebase": 300}
REPLAY, CLEAN_PUSH, RESOLVED_PUSH = "replay", "clean-push", "resolved-push"


def engine_env(environ=None):
    """The environment the engine's git commands run in: no prompt, `GIT_EDITOR=true` (it outranks `-c core.editor`), and
    none of the repository-location variables. A new mapping; the input is never changed."""
    return _sibling("bounded_run").unattended_env(environ)


def hooks_policy(phase):
    """Hooks are OFF while commits are replayed (they fire once per replayed commit) and for the push of a clean replay
    (no new code), ON for a push after a resolved conflict (new code). An unknown phase reads OFF: the safe answer."""
    git_runner = _sibling("unattended_git")
    return git_runner.HOOKS_INHERIT if phase == RESOLVED_PUSH else git_runner.HOOKS_OFF


def engine_runner(hooks, environ=None):
    """-> `run(cwd, argv)` for the pass: config pins (no ref updates by rebase, no signing), the hooks policy, per-verb
    timeouts. Injected through `run=`; the default runner of the existing engine is not changed."""
    return _sibling("unattended_git").make_runner(GIT_LIMITS, hooks=hooks, environ=engine_env(environ))


# ------------------------------------------------------------------------------------------ tips and restarts

def recheck_tips(read, steps, max_restarts=2):
    """Run `steps` (callables taking the tips) in order, re-reading both tips (`read() -> (unit_tip, base_tip)`) before each
    one. A tip that moved restarts the whole sequence from the first step with the new tips, at most `max_restarts` times;
    one more move is the outcome `moved`, and the step it would have run (the push is last) does not run.
    -> {"outcome": "ok"|"moved"|"unreadable", "restarts": n, "tips": the tips last read}."""
    try:
        tips = read()
    except Exception:                                       # noqa: BLE001 - could not tell is never stable
        return {"outcome": "unreadable", "restarts": 0, "tips": None}
    restarts, index = 0, 0
    while index < len(steps):
        try:
            now = read()
        except Exception:                                   # noqa: BLE001
            return {"outcome": "unreadable", "restarts": restarts, "tips": tips}
        if now != tips:
            if restarts >= max_restarts:
                return {"outcome": "moved", "restarts": restarts, "tips": now}
            restarts, tips, index = restarts + 1, now, 0
            continue
        steps[index](tips)
        index += 1
    return {"outcome": "ok", "restarts": restarts, "tips": tips}


# ------------------------------------------------------------------------------------------ acks

ACKS_REL = "state/upkeep/acks.json"      # one string: the block name is read by the gate alone (a pin)


def _read_acks_file(path):
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        units = data.get("units") if isinstance(data, dict) else None
        return units if isinstance(units, dict) else {}
    except (OSError, ValueError):
        return {}


@feature_upkeep.gated("project")
def ack_union(config, sdlc_dir, unit, acks):
    """Recompute the acks of `unit` after a rewrite: the union of what the runtime file already holds and `acks` (dicts with
    `sha` and `patch_id`), written atomically (temp file in the same directory, then replace) to `.sdlc/state/upkeep/acks.json`.

    A RUNTIME file, not the tracked store: the tracked store cannot be rewritten in the root checkout. It is per machine; a
    second clone derives its own (the limit is documented, not worked around). -> {"ok": bool, "path"}."""
    path = pathlib.Path(sdlc_dir).joinpath(*ACKS_REL.split("/"))
    units = _read_acks_file(path)
    seen, merged = set(), []
    for entry in list(units.get(unit) or []) + [dict(a) for a in acks if isinstance(a, dict)]:
        if not isinstance(entry, dict):
            continue
        item = {"sha": str(entry.get("sha") or ""), "patch_id": str(entry.get("patch_id") or "")}
        if (item["sha"] or item["patch_id"]) and (item["sha"], item["patch_id"]) not in seen:
            seen.add((item["sha"], item["patch_id"]))
            merged.append(item)
    units[unit] = merged
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".acks-", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"version": 1, "units": units}, indent=2, sort_keys=True))
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:
        return {"ok": False, "path": str(path)}
    return {"ok": True, "path": str(path)}


# ------------------------------------------------------------------------------------------ limits and skip reasons

def reanchor_allowed(level, verify_green):
    """Evidence is re-anchored only after a mechanical (Level 1) change or a green verify; anything else, including an
    unknown level or a truthy-but-not-true verdict, is no."""
    return level in (1, "mechanical") or verify_green is True


def classify_push_refusal(*, pr_merged, remote_branch_exists):
    """A refused push to a goal branch whose pull request already merged and whose branch is gone is a SKIP with a reason
    (the replay would resurrect a deleted branch); anything else stays a refusal. -> {"skip": bool, "reason"}."""
    if pr_merged is True and remote_branch_exists is False:
        return {"skip": True, "reason": "goal-branch-deleted-after-merge"}
    return {"skip": False, "reason": ""}


# ------------------------------------------------------------------------------------------ the ledger note

@feature_upkeep.gated("project")
def ledger_note(config, sdlc_dir, unit_key, outcome, sha12, *, append=None):
    """One UNADDRESSED `note` for a pass outcome: goal `upkeep-<unit-key>`, ref `upkeep:<outcome>:<sha12>`, written in this
    process with `ledger.safe_append`. Never through `loop.py note` (it arms a real claim) and never addressed (an addressed
    note wakes the inbox and the autowatcher). The append is injectable for tests. -> {"ok": bool, "entry"}."""
    append = append or _sibling("ledger").safe_append
    entry = append(sdlc_dir, "note", "upkeep-" + str(unit_key).lower(), config=config,
                   ref="upkeep:%s:%s" % (outcome, str(sha12)[:12]))
    return {"ok": entry is not None, "entry": entry}


def _value(thing):
    return thing() if callable(thing) else thing


def _current(run, cwd, base_tip, unit_tip):
    """True when the base tip is already inside the unit (nothing to catch up on); False or None (could not tell) otherwise."""
    shared = _sibling("feature_landed")
    try:
        answer, _, _ = shared._ancestry(_shared(), str(cwd), base_tip, unit_tip)
    except Exception:                                       # noqa: BLE001
        return None
    return {"yes": True, "no": False}.get(answer)


def _goal_pass(entry, run_replay, run_push, report):
    """One idle goal branch. A branch whose request merged and whose remote branch is gone is a skip with a reason BEFORE any
    replay (a replay would push, and a push would resurrect it); a refusal learned afterwards is classified the same way and
    anything else refused is a conflict of the goal, never a skip. Evidence is re-anchored only when `reanchor_allowed`."""
    name = str(entry.get("name"))
    row = {"name": name, "result": "replayed", "reason": "", "reanchored": False}
    report["goals"].append(row)

    def refusal():
        try:
            return classify_push_refusal(pr_merged=_value(entry.get("pr_merged")),
                                         remote_branch_exists=_value(entry.get("remote_exists")))
        except Exception:                                   # noqa: BLE001 - could not tell is not a skip
            return {"skip": False, "reason": ""}
    gone = refusal()
    if gone["skip"]:
        row.update(result="skipped", reason=gone["reason"])
        return
    try:
        outcome = entry["replay"](run_replay, run_push) or {}
    except Exception:                                       # noqa: BLE001 - one goal never costs the pass
        outcome = {"ok": False}
    if outcome.get("push_refused"):
        late = refusal()
        if late["skip"]:
            row.update(result="skipped", reason=late["reason"])
        else:
            row.update(result="refused", reason="push-refused")
            report["conflicts"].append(name)
        return
    if outcome.get("ok") is not True:
        row.update(result="failed", reason="replay-failed")
        report["conflicts"].append(name)
        return
    if reanchor_allowed(outcome.get("level"), outcome.get("verify_green")) and callable(entry.get("reanchor")):
        entry["reanchor"]()
        row["reanchored"] = True


def _engine(config, sdlc_dir, unit, unit_ref, base_ref, run, cwd, report, rewrite, push, direct_commits, goals, factory):
    """The costly steps, behind the tip re-reads. `rewrite(run, tips) -> {"ok", "resolved", "acks"}` must not move the unit ref
    (the push is the only step that does), so a moved tip is somebody else's. Sets `report["result"]` and `["reason"]`."""
    git = run or _git
    read = lambda: (_tip(git, cwd, unit_ref), _tip(git, cwd, base_ref))     # noqa: E731
    unit_tip, base_tip = report["unit_tip"], report["base_tip"]
    if _current(git, cwd, base_tip, unit_tip) is True:
        extra = []
        if direct_commits is not None:
            try:
                extra = list(direct_commits((unit_tip, base_tip)) or [])
            except Exception:                               # noqa: BLE001
                extra = []
        report.update(result="skipped", reason="direct-commits-on-current-unit" if extra else "current")
        return
    box = {}

    def step_rewrite(tips):
        try:
            outcome = rewrite(factory(hooks_policy(REPLAY)), tips) or {}
        except Exception:                                   # noqa: BLE001 - a failed rewrite is a failed pass, never a push
            outcome = {"ok": False}
        box["rewrite"] = outcome
        if outcome.get("acks"):
            ack_union(config, sdlc_dir, unit, outcome["acks"])

    def step_push(tips):
        if (box.get("rewrite") or {}).get("ok") is False:   # nothing was rewritten: nothing is pushed
            return
        phase = RESOLVED_PUSH if (box.get("rewrite") or {}).get("resolved") else CLEAN_PUSH
        try:
            push(factory(hooks_policy(phase)), tips)
        except Exception:                                   # noqa: BLE001 - a refused or failed push is a failed pass
            box["rewrite"] = {"ok": False}
    steps = [step_rewrite] + ([step_push] if push is not None else [])
    checked = recheck_tips(read, steps)
    report["restarts"] = checked["restarts"]
    if checked["outcome"] != "ok":
        report.update(result=checked["outcome"], reason="tips-" + checked["outcome"])
        return
    if (box.get("rewrite") or {}).get("ok") is False:
        report.update(result="failed", reason="rewrite-failed")
        return
    report.update(result="rewritten", reason="")
    for entry in goals or []:
        if isinstance(entry, dict) and "replay" in entry:
            _goal_pass(entry, factory(hooks_policy(REPLAY)), factory(hooks_policy(CLEAN_PUSH)), report)


@feature_upkeep.gated("project")
def upkeep_pass(config, sdlc_dir, unit, unit_ref, base_ref, base_name, now, *, run=None, cwd=".", landing_prs=None,
                rewrite=None, push=None, goals=None, direct_commits=None, ledger_append=None, runner_factory=None):
    """One pass for one unit. -> {"result": "skipped"|"not-landed"|"unreadable"|"rewritten"|"moved"|"failed", "reason", "unit",
    "unit_tip", "base_tip"}, plus `restarts`, `goals` and `conflicts` once the engine ran.

    The engine keywords are all injectable and all optional; a call with none of them is exactly the landed-unit skip.
    `rewrite(run, tips)` and `push(run, tips)` get runners from `runner_factory` (default `engine_runner`) built with the hooks
    policy of their phase; `goals` is a list of dicts for `_goal_pass`; `direct_commits(tips)` lists commits made straight on a
    unit that is already current. With any of them (or `ledger_append`), the end of the pass writes the unaddressed note."""
    verdict, unit_tip, base_tip = landed(run, cwd, unit_ref, base_ref, base_name, landing_prs)
    report = {"result": NOT_LANDED, "reason": verdict, "unit": unit, "unit_tip": unit_tip, "base_tip": base_tip}
    if verdict == UNREADABLE:
        report["result"] = UNREADABLE
    elif verdict in (LANDED_CONTAINED, LANDED_PR):
        report["result"] = "skipped"
        state = _sibling("feature_upkeep_state").record_outcome(sdlc_dir, unit, now, "current", unit_tip, base_tip)
        report["recorded"] = bool(getattr(state, "ok", False))
    elif rewrite is not None:
        report.update(goals=[], conflicts=[], restarts=0)
        _engine(config, sdlc_dir, unit, unit_ref, base_ref, run, cwd, report, rewrite, push, direct_commits, goals,
                runner_factory or engine_runner)
    wired = any(x is not None for x in (rewrite, push, goals, direct_commits, ledger_append))
    if wired and unit_tip:
        ledger_note(config, sdlc_dir, unit, report["result"], unit_tip, append=ledger_append)
    return report


# ------------------------------------------------------------------------------------------ the shipped entry point

def unit_callables(cwd, remote, branch):
    """-> (rewrite, push): the pass's two callables for one unit. `rewrite` replays the unit onto the base tip in a throwaway
    detached worktree (the unit ref is never moved; a conflict aborts and reads `ok: False`, nothing is resolved here) and
    remembers the new commit; `push` sends that commit to `remote` as `branch` under a lease on the tip the pass read, so a
    branch somebody else moved is refused, never overwritten. The run it is handed carries the hooks policy of its phase."""
    box = {}

    def rewrite(run, tips):
        unit_tip, base_tip = tips
        box.pop("new", None)
        with tempfile.TemporaryDirectory(prefix="sigma-upkeep-") as parent:
            tree = os.path.join(parent, "wt")
            run(cwd, ["git", "worktree", "add", "--detach", tree, unit_tip])
            try:
                try:
                    run(tree, ["git", "rebase", base_tip])
                except Exception:                           # noqa: BLE001 - a conflict or failure: leave nothing half-done
                    try:
                        run(tree, ["git", "rebase", "--abort"])
                    except Exception:                       # noqa: BLE001
                        pass
                    return {"ok": False}
                box["new"] = run(tree, ["git", "rev-parse", "HEAD"])
            finally:
                run(cwd, ["git", "worktree", "remove", "--force", tree])
        return {"ok": True, "resolved": False}

    def push(run, tips):
        new = box.get("new")
        if not (isinstance(new, str) and _HEX.fullmatch(new)):
            raise RuntimeError("nothing rewritten to push")
        run(cwd, ["git", "push", "--force-with-lease=refs/heads/%s:%s" % (branch, tips[0]), remote,
                  "%s:refs/heads/%s" % (new, branch)])
    return rewrite, push


@feature_upkeep.gated("project")
def run_unit_pass(config, sdlc_dir, unit, unit_ref, base_ref, base_name, now, *, remote, branch, cwd=".", **kw):
    """The shipped caller of `upkeep_pass`: supplies the rewrite and push callables (`unit_callables`) so a pass on a unit that
    is behind its base replays it and pushes it. Anything else (`run`, `landing_prs`, ...) goes through to the pass."""
    rewrite, push = unit_callables(cwd, remote, branch)
    return upkeep_pass(config, sdlc_dir, unit, unit_ref, base_ref, base_name, now, cwd=cwd, rewrite=rewrite, push=push, **kw)
