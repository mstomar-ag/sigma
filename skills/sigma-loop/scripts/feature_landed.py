"""The ONE shared "landed" predicate for a feature unit (#930, upkeep part C, slice 1).

WHAT IT ANSWERS. Is this unit's tip already landed on this base? `landed(...)` returns a structured verdict, never
a bare boolean: LANDED, NOT_LANDED or UNKNOWN, with `via` (ancestry or merged PR), the base sha it ran against, the
PR, the merged head and a reason. UNKNOWN means "do nothing and say why"; every caller treats it that way.

TWO HALVES, in this order.
1. ANCESTRY, local and cheap: `git merge-base --is-ancestor <tip> <base_ref>` through a bounded runner that KEEPS the
   exit code. rc 0 is LANDED; rc 1 is "not an ancestor"; anything else (124 timeout, 127 missing binary, 128 git
   failure) says nothing about ancestry. It never fetches, so it states the base sha it saw: a stale base is visible.
2. A HEAD-AWARE MERGED-PR READ, only when ancestry did not say LANDED (a squash-landed tip is never an ancestor):
   `GET repos/<slug>/pulls?head=<owner>:<branch>&state=all`, paged until a short page or a bounded page count. A PR
   lands the tip only when it is merged, its head sha IS the tip and its base ref IS the base. The commit-to-PR
   lookup is deliberately not used (it is unverified for a squash-landed tip).

COMBINE. LANDED if either half proves it. NOT_LANDED only when ancestry said "not an ancestor" AND the PR half read
cleanly (no error, no truncation) and found no merged PR on this exact head into this base. Everything else is
UNKNOWN. A truncated page is never read as "no merged PR".

MERGED HEAD. When a merged PR from this branch into the base exists with a head that is NOT the tip (a unit landed
by hand that later gained commits), the verdict carries that head in `merged_head`, so a later pass can replay with
`--onto <merged head>` instead of re-applying landed commits. This module only reports it.

THE RUNNER. `make_runner(timeout=None)` -> `run(argv, cwd) -> (rc, stdout, stderr)`. It never raises on a non-zero
exit, stops the whole process group on a timeout (rc 124), maps a missing binary or an unrunnable platform to rc 127,
scrubs the repository-location variables, pins no prompts and sets LC_ALL=C. The timeout follows the
SIGMA_WATCH_CALL_TIMEOUT convention (default 120, never below 5). It is built on `bounded_run.run_group`. The runners
the landing tail uses today raise on any non-zero exit, so rc 1 and rc 128 look alike there.

THE SLUG. `resolve_slug` -> `(owner/name or None, reason)`: the configured repository (or the remote's) and a check
that the remote's own URL names the same repository. A mismatch, an unreadable remote or gh's own placeholder is None
with a reason that never echoes a URL.

NOT HERE, ON PURPOSE. No `upkeep` config is read (the gate module is the only reader; gated callers consult it before
calling in), no write of any kind (every call is a read: rev-parse, merge-base, remote get-url, a GET), nothing marks
or finishes a unit.
"""
import collections
import importlib.util
import json
import os
import re
import urllib.parse

LANDED = "LANDED"
NOT_LANDED = "NOT_LANDED"
UNKNOWN = "UNKNOWN"
VERDICTS = (LANDED, NOT_LANDED, UNKNOWN)
VIA_ANCESTRY = "ancestry"
VIA_PR = "merged-pr"

PER_PAGE = 30
MAX_PAGES = 4
DEFAULT_TIMEOUT_SECONDS = 120
MIN_TIMEOUT_SECONDS = 5
TIMEOUT_ENV = "SIGMA_WATCH_CALL_TIMEOUT"
RC_TIMEOUT = 124
RC_UNRUNNABLE = 127

#: The projection kept per PR; head sha and base ref are the two the landing test needs.
PR_JQ = ("[.[] | {number, state, draft, merged_at, created_at, merge_commit_sha, head_sha: .head.sha, "
         "base_ref: .base.ref}]")

_SHA_RE = re.compile(r"\A[0-9a-f]{40}([0-9a-f]{24})?\Z")
_REASON_CHARS = 160

Verdict = collections.namedtuple("Verdict", "verdict via base_sha pr merged_head reason",
                                 defaults=(None, None, None, None, ""))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.dirname(os.path.abspath(__file__)) + os.sep
                                                  + name + ".py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _short(text):
    return " ".join(str(text or "").split())[:_REASON_CHARS]


def verdict_dict(verdict):
    """A plain, JSON-able copy of a Verdict (what a report or a record carries)."""
    return dict(verdict._asdict())


# ---------------------------------------------------------------------------------------------- the runner


def _timeout_seconds(environ=None):
    source = os.environ if environ is None else environ
    try:
        return max(MIN_TIMEOUT_SECONDS, int(source.get(TIMEOUT_ENV) or DEFAULT_TIMEOUT_SECONDS))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS


def make_runner(timeout=None, environ=None):
    """-> run(argv, cwd) -> (rc, stdout, stderr). Never raises. rc 124 on a timeout (the group is stopped), 127 when
    the command cannot be started or the platform cannot run it safely."""
    budget = float(timeout) if timeout is not None else float(_timeout_seconds(environ))

    def run(argv, cwd):
        bounded = _load("bounded_run")
        env = bounded.unattended_env(environ)
        env["LC_ALL"] = "C"
        env["GIT_OPTIONAL_LOCKS"] = "0"
        try:
            result = bounded.run_group([str(a) for a in argv], cwd, budget, env=env, merge=False)
        except Exception as exc:                  # noqa: BLE001 - a runner answers, it does not raise
            return RC_UNRUNNABLE, "", _short(exc)
        if result.outcome == bounded.TIMEOUT:
            return RC_TIMEOUT, "", _short(result.detail)
        if result.code is None or result.outcome in (bounded.REFUSED, bounded.ERROR):
            return RC_UNRUNNABLE, "", _short(result.detail)
        return result.code, result.out, result.err

    return run


def _call(run, argv, cwd):
    """(rc, out, err) from the injected runner, a raise folded into rc 127."""
    try:
        rc, out, err = run(argv, cwd)
    except Exception as exc:                      # noqa: BLE001
        return RC_UNRUNNABLE, "", _short(exc)
    return rc, str(out or ""), str(err or "")


def _git(run, cwd, *args):
    return _call(run, ["git", "-C", str(cwd)] + list(args), cwd)


# ---------------------------------------------------------------------------------------------- the slug


def resolve_slug(config, run, cwd, remote="origin"):
    """-> (slug, reason). slug is `owner/name` only when the remote's own URL names the same repository (compared
    without case); otherwise None with a reason. The reason never contains a URL or a configured value."""
    sync = _load("feature_sync")

    def stdout_run(path, argv):                   # feature_sync's contract: stdout, raising on a non-zero exit
        rc, out, err = _call(run, argv, path)
        if rc != 0:
            raise RuntimeError(err or "exit %s" % rc)
        return out.strip()

    slug = sync.repo_slug(config, stdout_run, cwd, remote)
    if not slug:
        return None, "no repository could be resolved from the configuration or the remote"
    rc, url, _err = _git(run, cwd, "remote", "get-url", remote)
    if rc != 0:
        return None, "the remote cannot be read, so the repository cannot be checked against it"
    named = sync._slug_from_url(url.strip())
    if not named:
        return None, "the remote does not name a repository this tool can parse"
    if named.lower() != slug.lower():
        return None, "the configured repository differs from the one the remote names"
    return slug, ""


# ---------------------------------------------------------------------------------------------- the PR reader


def merged_pull_requests(run, cwd, slug, branch, base=None, per_page=None, max_pages=None):
    """-> (rows, truncated, error). Every PR (any state) whose head is `branch` in `slug`, paged until a short page
    or `max_pages` full pages; `base` filters by base branch when given. Rows are projected to number, state, draft,
    merged_at, created_at, merge_commit_sha, head_sha, base_ref and sorted by (created_at, number). `truncated` is
    True when the last page was full, so a caller can never read a clipped list as complete. Exactly one of rows and
    error is not None."""
    per_page, max_pages = per_page or PER_PAGE, max_pages or MAX_PAGES
    owner = slug.split("/")[0]
    rows = []
    for page in range(1, max_pages + 1):
        path = "repos/%s/pulls?head=%s:%s&state=all&per_page=%d&page=%d" % (
            slug, urllib.parse.quote(owner, safe=""), urllib.parse.quote(branch, safe="/"), per_page, page)
        if base:
            path += "&base=" + urllib.parse.quote(base, safe="/")
        rc, out, err = _call(run, ["gh", "api", path, "--jq", PR_JQ], cwd)
        if rc != 0:
            return None, False, "the pull request list could not be read (exit %s)" % rc
        try:
            chunk = json.loads(out)
        except ValueError:
            return None, False, "the pull request list was not JSON"
        if not isinstance(chunk, list):
            return None, False, "the pull request list was not a list"
        rows.extend(r for r in chunk if isinstance(r, dict))
        if len(chunk) < per_page:
            rows.sort(key=lambda r: (str(r.get("created_at") or ""), r.get("number") or 0))
            return rows, False, None
    rows.sort(key=lambda r: (str(r.get("created_at") or ""), r.get("number") or 0))
    return rows, True, None


# ---------------------------------------------------------------------------------------------- the predicate


def _ancestry(run, cwd, tip, base_ref):
    """-> ("yes" | "no" | "unknown", base_sha_or_None, reason)."""
    rc, out, _ = _git(run, cwd, "rev-parse", "--verify", "--quiet", base_ref + "^{commit}")
    base_sha = out.strip() if rc == 0 and _SHA_RE.match(out.strip()) else None
    if base_sha is None:
        return "unknown", None, "the base ref cannot be resolved (exit %s)" % rc
    rc, _, _ = _git(run, cwd, "merge-base", "--is-ancestor", tip, base_ref)
    if rc == 0:
        return "yes", base_sha, ""
    if rc == 1:
        return "no", base_sha, ""
    return "unknown", base_sha, "the ancestry check did not answer (exit %s)" % rc


def landed(tip, base_ref, branch, slug, run, cwd, base_name=None):
    """Is `tip` (a commit of `branch`) landed on `base_ref`? -> Verdict. Never raises.

    `base_ref` is what git resolves (for example `origin/main`); `base_name` is the base BRANCH the PR targets
    (default: `base_ref`). `run` is `run(argv, cwd) -> (rc, out, err)`, normally `make_runner()`. `slug` is
    `owner/name` from `resolve_slug`, or None when none could be resolved (the PR half is then unanswered)."""
    try:
        return _landed(tip, base_ref, branch, slug, run, cwd, base_name or base_ref)
    except Exception as exc:                      # noqa: BLE001 - a predicate answers, it does not raise
        return Verdict(UNKNOWN, reason="the check failed: " + _short(exc.__class__.__name__))


def _landed(tip, base_ref, branch, slug, run, cwd, base_name):
    rc, out, _ = _git(run, cwd, "rev-parse", "--verify", "--quiet", str(tip) + "^{commit}")
    tip_sha = out.strip() if rc == 0 and _SHA_RE.match(out.strip()) else None
    if tip_sha is None:
        return Verdict(UNKNOWN, reason="the unit tip cannot be resolved (exit %s)" % rc)
    ancestry, base_sha, why = _ancestry(run, cwd, tip_sha, base_ref)
    if ancestry == "yes":
        return Verdict(LANDED, VIA_ANCESTRY, base_sha, reason="the tip is contained in the base")
    if not slug:
        return Verdict(UNKNOWN, base_sha=base_sha, reason=why or "no repository slug, so merged pull requests "
                       "were not read")
    rows, truncated, error = merged_pull_requests(run, cwd, slug, branch, base=base_name)
    if error is not None:
        return Verdict(UNKNOWN, base_sha=base_sha, reason=error)
    merged = [r for r in rows if r.get("merged_at") and r.get("base_ref") == base_name]
    for row in merged:
        if row.get("head_sha") == tip_sha:
            return Verdict(LANDED, VIA_PR, base_sha, row.get("number"), tip_sha,
                           "a merged pull request has this exact head")
    newest = max(merged, key=lambda r: (str(r.get("merged_at")), r.get("number") or 0)) if merged else None
    merged_head = newest.get("head_sha") if newest else None
    pr = newest.get("number") if newest else None
    if truncated:
        return Verdict(UNKNOWN, None, base_sha, pr, merged_head,
                       "more pull requests than were read; the list is incomplete")
    if ancestry != "no":
        return Verdict(UNKNOWN, None, base_sha, pr, merged_head, why)
    return Verdict(NOT_LANDED, None, base_sha, pr, merged_head,
                   "not contained in the base and no merged pull request has this head")

