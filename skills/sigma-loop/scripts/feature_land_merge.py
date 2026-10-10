"""The landing engine, BACK HALF (#938, upkeep part C, slice 9): the guarded, head-pinned merge of one verified head.

A library loaded by path from `feature_land` (the front half) and never imported by the scheduler. The front half
verified a head T in a scratch tree, re-read both tips, and found an open non-draft pull request at T. This half does
the rest, in this order, and stops at the first refusal:

1. THE GATE. The upkeep project gate is the first call of the public entry; closed means no read, no write, no call.
2. LAST TIP RE-READ. The unit tip must still be T and the base tip must still be the one T was verified against.
   Otherwise refuse (`tip-moved`) before the guard, so an approval is not spent on a head that is already stale.
3. THE SIBLING GUARD, then THE APPROVAL GUARD, LAST. `work.unit_sibling_guard` refuses (`sibling`) while another
   repository's half of the unit has not landed, before an approval can be spent. `landed(repo, branch)` is the
   caller's measurement; none supplied means a unit that names another repository refuses. Then
   `feature_land_approval.authorize`: attended consent, or the single-use approval consumed
   create-once and bound to T. Every other check has passed by then; nothing but the record and the call follow.
4. THE PENDING RECORD, THEN THE CALL. `feature_upkeep_landing.run_landing` writes the pending record BEFORE the call (no
   record, no call), makes the call, reads the pull request and the merge commit back, classifies and settles.
   The call carries T as its head pin, an explicit merge method, no auto-merge, no admin flag and no branch delete.
   A head that moved makes the host refuse; the read-back then shows the pull request still open: `refused`.
5. RECORD ONCE. On merged or merged-with-warning a create-once marker is taken, then the observation writer runs; a
   second completion finds the marker and records nothing.
6. THE BRANCH CHECK. A unit branch gone from the remote is reported with a restore hint and never recreated. Landing
   never deletes a branch and never marks the unit finished.

The base can move between the last re-read and the merge; a head pin cannot pin the base. That is detected after the
fact (merged-with-warning) and reported, not prevented. Live REST statuses are unverified; nothing here claims them.
Outcomes: `merged`, `merged-with-warning`, `armed`, `refused:<reason>`, `unconfirmed:<reason>`.
"""
import functools
import importlib.util
import os
import pathlib
import time

_HERE = pathlib.Path(__file__).resolve().parent
MERGE_METHOD = "merge"
MARKER_KIND = ".recorded-%d.json"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


upkeep = _load("feature_upkeep")


def _gate_first(fn):
    """The gate is the first action of the public entry; a closed gate returns the closed verdict and the body never runs."""
    @functools.wraps(fn)
    def guarded(config, *args, **kwargs):
        verdict = upkeep.evaluate(config, "project")
        if not verdict["open"]:
            return {"closed": True, "door": "project", "missing": verdict["missing"], "problems": verdict["problems"]}
        return fn(config, *args, **kwargs)
    return guarded


def _refuse(code, detail="", **extra):
    out = {"outcome": "refused:" + code, "reason": code, "detail": detail}
    out.update(extra)
    return out


def take_marker(sdlc_dir, unit, number):
    """Create-once: True only for the caller that created the marker for this unit and pull request."""
    landing = _load("feature_upkeep_landing")
    state = _load("state")
    base = landing.record_path(sdlc_dir, unit)
    path = base.with_name(base.name[:-len(landing.SUFFIX)] + MARKER_KIND % number)
    try:
        state.refuse_symlinks(sdlc_dir, path.relative_to(pathlib.Path(sdlc_dir)), create_parents=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    except OSError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write('{"schema":"sigma/unit-landing-recorded@1","unit":"%s","pr":%d}\n' % (
            _load("feature_registry").unit_key(unit), number))
    return True


@_gate_first
def complete(config, sdlc_dir, unit, *, slug, branch, base, head, base_tip, number, argv, environ, gh_run, read_tip,
             record=None, now=None, landed=None):
    """Steps 2 to 6 for one verified head -> a result dict with `outcome`."""
    gh_api = _load("gh_api")
    landing = _load("feature_upkeep_landing")
    approval = _load("feature_land_approval")
    stamp = int(time.time()) if now is None else now
    if read_tip(branch) != head or read_tip(base) != base_tip:
        return _refuse("tip-moved", "a tip moved after verify; nothing was approved or merged")
    ok, why = _load("work").unit_sibling_guard(sdlc_dir, config, unit, slug, landed=landed)
    if not ok:
        return _refuse("sibling", why)
    verdict = approval.authorize(config, sdlc_dir, unit, slug, head, argv=argv, environ=environ, now=now)
    if not verdict.get("ok"):
        return _refuse("guard", verdict.get("reason", "the landing guard denied"))
    pre = {"unmerged": True, "head": head, "base": base_tip}

    def do_merge():
        try:
            reply = gh_api.merge_pr_pinned(gh_run, slug, number, head, merge_method=MERGE_METHOD)
        except gh_api.GhApiError as exc:
            return {"kind": "lost" if exc.status is None else "error", "status": exc.status}
        return {"kind": "ok", "status": 200, "sha": gh_api.merge_reply_sha(reply)}

    result = landing.run_landing(sdlc_dir, config, unit, number, pre, do_merge,
                                 lambda n: gh_api.view_pr(gh_run, n, slug),
                                 lambda sha: gh_api.commit_parents(gh_run, slug, sha), stamp)
    out = {"outcome": result.outcome, "reason": result.reason, "pr": number, "head": head, "base_tip": base_tip,
           "called": result.called}
    if result.outcome in (landing.REFUSED, landing.UNCONFIRMED):
        out["outcome"] = "%s:%s" % (result.outcome, result.reason)
        return out
    if result.outcome in (landing.MERGED, landing.MERGED_WARNING):
        out["recorded"] = False
        if take_marker(sdlc_dir, unit, number):
            try:
                if record is not None:
                    record(number)
                out["recorded"] = True
            except Exception:                     # noqa: BLE001 - a completed landing is never reported as failed
                out["recorded"] = False
        if read_tip(branch) is None:
            out["branch_gone"] = True
            out["detail"] = ("the unit branch is gone from the remote; it is not recreated here. Restore it by pushing "
                             "the verified head %s to the branch name" % head)
    return out
