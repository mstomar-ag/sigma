#!/usr/bin/env python3
"""The landing engine, FRONT HALF (#937, upkeep part C, slice 8): a person asks to land one feature unit; this verb
checks everything it can and STOPS at a rehearsal. It never calls the merge: the merge call, the pending-landing
record write, the read-back and the receipt belong to the next slice.

THE ORDER (design 915, section 4, steps 1 to 7).
1. THE GATE is the first call of every public entry point. Closed means no subprocess, no network, no model call and
   no file is touched. Then the unit name, any pending-landing record (a unit that has one is not re-landed here: the
   settle belongs to the next slice), the repository slug (computed ONCE, it must equal the remote's own), the base.
2. PRECONDITIONS, each a distinct refusal: verify not configured; slug mismatch; a merge queue on the base; merge
   commits not allowed; delete-branch-on-merge on; the unit tip already landed (success, nothing written) or its
   landed state unknown; an unattended request with no valid approval for the current tip (checked, NOT consumed).
3. A PASS now (part A's upkeep pass); its resulting remote unit tip is the candidate T. A pass that moved the tip
   voids an approval bound to the old one, so an unattended request is refused then (the push has happened by then).
4. Stamped-commit display: a no-op until part B exists.
5. VERIFY in a scratch worktree detached at T through the shared verify runner, never the checked-out tree.
6. RE-READ both remote tips. A unit tip other than T, or a base tip that moved, restarts from step 3 and counts as an
   attempt; three attempts end with "a tip kept moving".
7. THE PR: find the open non-draft landing PR for T on the base, or create one with an explicit non-draft body, then
   read it and require open, unmerged and head T. An open draft refuses and names the PR (a REST way to ready a draft
   is unverified). A PR closed unmerged, on any head, never blocks creation.
Then the outcome `rehearsal`. Outcomes: `already-landed`, `refused:<code>`, `rehearsal`.

NOT HERE. No per-unit engine lock (the pass takes its own; the engine lock is wrapped around the merge half in the
next slice), no approval consumption, no pending record write, no merge. Nothing reaches the merge function: a
structural test shows the scheduler's import closure cannot reach `land`, and a static test shows this module never
names a merge operation. Live REST statuses and the draft-ready gesture are unverified.
"""
import functools
import importlib.util
import os
import pathlib
import re
import sys
import tempfile

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


upkeep = _load("feature_upkeep")
registry = _load("feature_registry")

REHEARSAL = "rehearsal"
ALREADY_LANDED = "already-landed"
MAX_ATTEMPTS = 3
DEFAULT_REMOTE = "origin"
CONSENT_ALIASES = ("--requested-by-user",)
_SHA = re.compile(r"\A[0-9a-f]{40}\Z")


def _gate_first(fn):
    """The gate is each public entry point's first action: a closed gate returns the closed verdict and the body
    never runs (a static test checks that this stays the first thing)."""
    @functools.wraps(fn)
    def guarded(config, *args, **kwargs):
        verdict = upkeep.evaluate(config, "project")
        if not verdict["open"]:
            return {"closed": True, "door": "project", "missing": verdict["missing"], "problems": verdict["problems"]}
        return fn(config, *args, **kwargs)
    return guarded


def refuse(code, detail="", **extra):
    out = {"outcome": "refused:" + code, "reason": code, "detail": detail}
    out.update(extra)
    return out


class _Env:
    """The collaborators, swappable by tests: `run(argv, cwd)`, `gh_run(args)`, the pass and the verify runner."""

    def __init__(self, config, sdlc_dir, run=None, gh_run=None, rebase_pass=None, verify=None, now=None):
        self.config, self.sdlc_dir, self.now = config, str(sdlc_dir), now
        self.cwd = str(pathlib.Path(sdlc_dir).resolve().parent)
        self.landed = _load("feature_landed")
        self.rebase = _load("feature_rebase")
        self.gh_api = _load("gh_api")
        self.run = run or self.landed.make_runner()
        self.gh_run = gh_run or self.gh_api.bounded_runner(self.cwd)
        self.rebase_pass = rebase_pass or self.rebase.upkeep
        self.verify = verify
        self.remote = self.rebase._remote(config)


def _git(env, *args):
    try:
        rc, out, err = env.run(["git", "-C", env.cwd] + list(args), env.cwd)
    except Exception as exc:                      # noqa: BLE001 - a runner answers
        return 127, "", exc.__class__.__name__
    return rc, str(out or ""), str(err or "")


def _remote_tip(env, branch):
    """-> sha or None: the branch tip on the remote, read live."""
    rc, out, _ = _git(env, "ls-remote", env.remote, "refs/heads/" + branch)
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.strip() == "refs/heads/" + branch and _SHA.match(sha.strip()):
            return sha.strip()
    return None


def _fetch(env, branch):
    rc, _, _ = _git(env, "fetch", "--no-tags", env.remote,
                    "+refs/heads/%s:refs/remotes/%s/%s" % (branch, env.remote, branch))
    return rc == 0


def _verify_command(env):
    commands = _load("state").declared_verify_commands("", env.config, None)
    return commands[0] if len(commands) == 1 else None


def _unattended(argv, environ, unit):
    tokens = ["--user-requested" if a in CONSENT_ALIASES else a for a in (argv or ())]
    approval = _load("feature_land_approval")
    return not approval._attended(tokens, environ, unit), tokens


def _preconditions(env, slug, base):
    """-> a refusal dict, or None. Queue detection and the repository settings (REST reads, no verify spend yet)."""
    gh = env.gh_api
    try:
        if gh.rules_have_merge_queue(gh.branch_rules(env.gh_run, slug, base)):
            return refuse("merge-queue", "the base branch uses a merge queue, which this path cannot drive; the "
                          "pull request is untouched")
    except Exception:                             # noqa: BLE001 - a failed read proceeds; the merge refusal is the backstop
        pass
    try:
        settings = gh.repo_settings(env.gh_run, slug)
    except Exception as exc:                      # noqa: BLE001
        return refuse("settings-unreadable", "the repository settings could not be read (%s)" % exc.__class__.__name__)
    if settings.get("allow_merge_commit") is not True:
        return refuse("merge-commits-not-allowed", "the repository does not allow merge commits (or the setting "
                      "could not be read)")
    on_merge = gh.delete_branch_on_merge(settings)
    if on_merge is not False:
        return refuse("delete-branch-on-merge", "the repository deletes a branch when its pull request merges"
                      if on_merge else "the delete-branch-on-merge setting could not be read")
    return None


def _scratch_verify(env, tip, command):
    """Run the shared verify runner in a scratch worktree detached at `tip`. -> (ok, detail)."""
    bounded = _load("bounded_run")
    seconds = upkeep.read(env.config).settings["verify.timeout_minutes"] * 60
    with tempfile.TemporaryDirectory(prefix="land-scratch-") as parent:
        tree = os.path.join(parent, "tree")
        rc, _, err = _git(env, "worktree", "add", "--detach", tree, tip)
        if rc != 0:
            return False, "the scratch worktree could not be created"
        try:
            verify = env.verify or bounded.run_verify
            result = verify(command, tree, seconds)
        finally:
            _git(env, "worktree", "remove", "--force", tree)
    if getattr(result, "outcome", None) == bounded.OK:
        return True, ""
    return False, "verify did not pass: %s" % getattr(result, "outcome", "unknown")


def _pull_request(env, slug, branch, base, tip, title):
    """Step 7 -> (refusal or None, number). Find or create the non-draft landing PR, then require it open, unmerged
    and at `tip`."""
    rows, truncated, error = env.landed.merged_pull_requests(env.run, env.cwd, slug, branch, base=base)
    if error is not None or truncated:
        return refuse("pr-list-unreadable", error or "more pull requests than were read"), None
    open_rows = [r for r in rows if r.get("state") == "open" and r.get("base_ref") == base]
    for row in open_rows:
        if row.get("draft"):
            return refuse("draft-open", "pull request #%s is an open draft; ready it first (no REST way to ready "
                          "a draft is verified)" % row.get("number"), pr=row.get("number")), None
    number = None
    for row in open_rows:
        if row.get("head_sha") == tip:
            number = row.get("number")
    if number is None and open_rows:
        return refuse("pr-head-mismatch", "open pull request #%s is not at the verified head" % open_rows[-1].get("number"),
                      pr=open_rows[-1].get("number")), None
    try:
        if number is None:
            made = env.gh_api.create_pr_nondraft(env.gh_run, title, "Landing %s at a verified head." % branch,
                                                 branch, base, slug)
            number = made.get("number") if isinstance(made, dict) else None
        view = env.gh_api.view_pr(env.gh_run, number, slug) if isinstance(number, int) else None
    except Exception as exc:                      # noqa: BLE001
        return refuse("pr-unavailable", "the pull request could not be created or read (%s)" % exc.__class__.__name__), None
    head = ((view or {}).get("head") or {}).get("sha") if isinstance(view, dict) else None
    if not (isinstance(view, dict) and view.get("state") == "open" and view.get("merged") is False and head == tip):
        return refuse("pr-not-open-at-head", "the pull request is not open, unmerged and at the verified head",
                      pr=number), None
    return None, number


@_gate_first
def land(config, sdlc_dir, unit, *, argv=(), environ=None, run=None, gh_run=None, rebase_pass=None, verify=None,
         now=None):
    """Land `unit`: steps 1 to 7, ending in `rehearsal`. Never merges. -> a result dict with `outcome`."""
    environ = os.environ if environ is None else environ
    if not (isinstance(unit, str) and registry.is_unit_name(unit)):
        return refuse("bad-unit", "not a unit name")
    env = _Env(config, sdlc_dir, run, gh_run, rebase_pass, verify, now)
    approval = _load("feature_land_approval")
    landing = _load("feature_upkeep_landing")
    if landing.read_record(sdlc_dir, unit) is not None:
        return refuse("pending-landing", "an earlier landing of this unit left a pending record; settling it is not "
                      "part of this verb yet")
    slug, why = env.landed.resolve_slug(config, env.run, env.cwd, env.remote)
    if not slug:
        return refuse("slug-mismatch", why)
    base = env.rebase._safe_ref("work.base", env.rebase._settings(config).get("base")) or ""
    if not base:
        return refuse("no-base", "no integration branch is configured")
    branch = env.rebase.features.BRANCH_PREFIX + unit
    command = _verify_command(env)
    if not command:
        return refuse("verify-not-configured", "no single verify command is configured")
    bad = _preconditions(env, slug, base)
    if bad:
        return bad
    if not (_fetch(env, base) and _fetch(env, branch)):
        return refuse("fetch-failed", "the base or the unit branch could not be fetched")
    tip = _remote_tip(env, branch)
    if tip is None:
        return refuse("no-branch", "the unit has no branch on the remote")
    verdict = env.landed.landed(tip, "%s/%s" % (env.remote, base), branch, slug, env.run, env.cwd, base_name=base)
    if verdict.verdict == env.landed.LANDED:
        return {"outcome": ALREADY_LANDED, "tip": tip, "via": verdict.via, "pr": verdict.pr}
    if verdict.verdict != env.landed.NOT_LANDED:
        return refuse("unknown", verdict.reason)
    unattended, tokens = _unattended(argv, environ, unit)
    if unattended:
        got = approval.peek(config, sdlc_dir, unit, slug, tip, now=now)
        if not got["ok"]:
            return refuse("unattended-no-approval", got["reason"])
    for attempt in range(1, MAX_ATTEMPTS + 1):
        report = env.rebase_pass(sdlc_dir, config, unit, unit, run=None, cwd=env.cwd, remote=env.remote)
        outcome = report.get("outcome") if isinstance(report, dict) else None
        if outcome not in (env.rebase.CURRENT, env.rebase.REBASED):
            return refuse("pass-" + str(outcome), "the upkeep pass did not leave the unit current")
        candidate = _remote_tip(env, branch)
        if candidate is None:
            return refuse("no-branch", "the unit branch vanished from the remote")
        if unattended and candidate != tip:
            return refuse("tip-changed", "the tip changed since approval: approve %s" % candidate, tip=candidate)
        base_before = _remote_tip(env, base)
        ok, detail = _scratch_verify(env, candidate, command)
        if not ok:
            return refuse("verify-failed", detail, tip=candidate)
        if _remote_tip(env, branch) == candidate and _remote_tip(env, base) == base_before:
            break
        tip = candidate
    else:
        return refuse("tip-kept-moving", "a tip kept moving")
    bad, number = _pull_request(env, slug, branch, base, candidate, "Land %s" % unit)
    if bad:
        return bad
    return {"outcome": REHEARSAL, "pr": number, "head": candidate, "base_tip": base_before, "slug": slug,
            "attempts": attempt}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Land one feature unit (rehearsal only in this release: every check "
                                     "runs, the merge is never called).")
    parser.add_argument("verb", choices=["land"])
    parser.add_argument("unit")
    parser.add_argument("--state-dir", default=".sdlc")
    parser.add_argument("--user-requested", "--requested-by-user", dest="user_requested", metavar="UNIT",
                        help="consent flag: must name this unit exactly")
    parser.add_argument("--rehearse", action="store_true", help="accepted; this release always stops at the rehearsal")
    args = parser.parse_args(argv)
    state = _load("state")
    try:
        config = state.load_config(args.state_dir)
    except Exception as exc:                      # noqa: BLE001
        print("refused: %s" % exc.__class__.__name__, file=sys.stderr)
        return 2
    tokens = ["--user-requested", args.user_requested] if args.user_requested else []
    out = land(config, args.state_dir, args.unit, argv=tokens, environ=os.environ)
    if out.get("closed"):
        print("refused: the upkeep gate is closed", file=sys.stderr)
        return 4
    line = out["outcome"] + (" (%s)" % out["detail"] if out.get("detail") else "")
    if out["outcome"] in (REHEARSAL, ALREADY_LANDED):
        print(line)
        return 0
    print(line, file=sys.stderr)
    return 3


if __name__ == "__main__":
    sys.exit(main())
