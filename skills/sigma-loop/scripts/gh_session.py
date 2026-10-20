"""Tell a Claude Code Remote session's `gh` proxy block apart from a real auth failure (#78).

THE PROBLEM. Inside a Claude Code Remote (cloud) session, every script here that shells out to `gh`
(work.py's `_run`, sources.py's `_run_gh`) can fail on a fully-configured project with a perfectly
valid `GH_TOKEN` — because the Remote environment sits an intercepting proxy in front of
`api.github.com` that gates access per-endpoint, independent of the token. `sigma-doctor`'s "gh auth"
check hit this too: it always reported the token MISSING there, and pointed at `gh auth login`, which
fixes nothing (the token is fine; the proxy gate is what blocks it).

THE TWO CONFIRMED SHAPES (issue #78's own repro, not guessed). Both are proxy-INJECTED — they read
nothing like GitHub's own error bodies ("Bad credentials", "Resource not accessible by integration",
"API rate limit exceeded", ...) and both link to `docs.anthropic.com/.../github-actions`:

  1. GraphQL calls (`gh auth status`, `gh pr list`) are allowlisted to a pinned set of PR-review ops:
     "This GraphQL query (PullRequestList, sent by gh pr list) is not enabled for this session —
     only the pinned set of PR-review operations is served. Use REST via
     `gh api repos/{owner}/{repo}/...` instead."

  2. Repo-scoped REST calls (`gh api repos/{owner}/{repo}`, `gh pr create/view/merge`, `gh repo
     view`, `gh api graphql` — everything work.py/sources.py actually use) need the session's own
     Claude GitHub App connection, not just a bearer token:
     "GitHub access is not enabled for this session. An org admin must connect the Claude GitHub App
     for this organization."

Both shapes share one exact phrase, "not enabled for this session" — present verbatim in each quote
above — so `proxy_session_block` anchors on that ONE substring rather than trying to tell the two
shapes apart. That is deliberate, not laziness: shape 1's own remediation text ("use REST instead")
is actively WRONG advice for a script like work.py, since REST hits shape 2's gate right behind it —
the only genuinely correct fix in EITHER case is the same one ("connect the Claude GitHub App for
this organization"), so a caller never needs to know which shape it hit. A second, narrower anchor
("connect the claude github app") rides along as a fallback in case a future proxy revision drops
"for this session" from shape 2 alone while keeping its own remediation sentence.

Matching is substring + case-insensitive, not a byte-exact quote match: the proxy's exact wording is
not a contract this plugin controls, and a byte-exact match would silently stop matching the first
time punctuation or phrasing drifts a little — regressing straight back to the bug this module exists
to fix. Both anchors are specific enough that a real GitHub API error is not expected to ever contain
either (confirmed against the issue's own repro transcript, which shows `gh api user` returning a
normal 200 alongside these bodies on the very same token).

ONE SHARED COPY, not three duplicates. work.py and sources.py both load this the same way they
already load every other sibling script (`_load("gh_session")`), and `sigma-doctor/scripts/doctor.py`
cross-loads it via its own existing `_load_loop_script` — the SAME narrow, already-justified exception
to that file's usual no-cross-skill-import convention that `_dependency_marker_scan` uses today to
reuse `sources.fetch_comments`/`backlog_check._BLOCK_RE` rather than a doctor-local copy. That
docstring's own reasoning applies here at least as strongly: a doctor-local copy of this pattern could
drift from work.py/sources.py's copy as the proxy's wording changes, and the drift's failure mode is
exactly the one this fix exists to close (a diagnosis that quietly stops firing, or fires on the wrong
thing) — not a cosmetic inconsistency. Zero deps.

THE THIRD SHAPE (#801 slice 1). Issue #801 reports a third, different block: HTTP 403 "GitHub GraphQL
is not available from Claude Code sessions". That wording is taken from the issue text, NOT captured
from a live cloud session (unverified). `graphql_unavailable` recognises it; it is deliberately NOT
folded into `proxy_session_block` / `REMEDIATION` (so every existing caller sees exactly what it saw
before) and is not yet wired into any caller — that is a migration follow-up.
"""

_SESSION_BLOCK_MARKERS = (
    "not enabled for this session",   # both proxy shapes (#78) — see the module docstring's quotes
    "connect the claude github app",  # shape 2's own remediation sentence, kept as a second anchor
)

#: The one remediation that is actually correct for EITHER shape (see module docstring for why a
#: single message, not one per shape, is the deliberate choice) — reused verbatim by every caller
#: (work.py's raised RuntimeError, sources.py's `.hint`, sigma-doctor's "gh auth" fix line) so the
#: advice reads identically no matter which script hit the block.
REMEDIATION = (
    "a Claude Code Remote session's proxy is blocking this gh call, independent of GH_TOKEN — this "
    "is not a token problem, and `gh auth login` will not fix it. An org admin must connect the "
    "Claude GitHub App for this organization (see docs.anthropic.com/.../github-actions)."
)


def proxy_session_block(text):
    """None when `text` (a failed `gh` call's combined stdout+stderr, or any prose that might embed
    it) does not look like a Claude Code Remote session's proxy block; else `REMEDIATION`, the one
    corrected fix line every caller folds into its own error/diagnosis. Never raises — `text` may be
    `None` (a call site with nothing captured) or already something other than a plain string in an
    unusual caller; both read as "no match" rather than crashing the check that's asking."""
    try:
        haystack = str(text or "").lower()
    except Exception:                            # noqa: BLE001 - a can't-tell must never crash a caller
        return None
    return REMEDIATION if any(marker in haystack for marker in _SESSION_BLOCK_MARKERS) else None


_GRAPHQL_UNAVAILABLE_MARKERS = (
    "github graphql is not available from claude code sessions",   # #801's quoted 403 wording
)


def graphql_unavailable(text):
    """True when `text` (a failed `gh` call's output) looks like the third shape: GitHub GraphQL is
    not served to this Claude Code session. Never raises; a non-str (or unconvertible) value reads as
    False. Independent of `proxy_session_block`, which is unchanged."""
    if not isinstance(text, str):
        return False
    haystack = text.lower()
    return any(marker in haystack for marker in _GRAPHQL_UNAVAILABLE_MARKERS)


# ---------------------------------------------------------------- D7: a remedy for a classified gh failure

#: One fix line per `gh_api.classify` kind. A failure that prints a reason and no remedy sends a
#: first-run user to a search engine; these are appended AFTER the raw reason, never in place of it
#: (callers and classifiers read the reason's prefix and `.hint`). The scopes named under
#: "permission" are what the first-run preflight (`skills/sigma-init/scripts/preflight.py`) demands:
#: repo + workflow. The words 'project', 'scope' and the organisation sign-on terms are left out on
#: purpose: callers scan error text for them (sources._note_scope, cross_repo), and a fix line must
#: never satisfy such a scan (tests/test_d7_gh_api_sites.py checks every marker tuple).
REMEDIES = {
    "auth": "fix: run `gh auth login` (check with `gh auth status`); a GH_TOKEN or GITHUB_TOKEN in the "
            "environment overrides gh's login, so unset or replace it if it is wrong",
    "not_found": "fix: Not Found means the resource does not exist or this token cannot see it; "
                 "check the repository, issue number or label named above (discovery.github.repo in "
                 ".sdlc/config.json is the repository), and that `gh auth status` is an account with "
                 "access to it (a private repository needs repo access)",
    "permission": "fix: the token needs the repo and workflow permissions: run "
                  "`gh auth refresh -s repo,workflow` (`gh auth status` shows what it has); a "
                  "fine-grained token needs Issues and Pull requests read and write; an organisation "
                  "that requires it must authorize the token on GitHub's token settings page",
    "rate_limit": "fix: wait until the retry time in the refused response itself (`gh api -i` shows X-RateLimit-Reset or Retry-After); `gh api rate_limit` can disagree with it; a secondary limit is not the hourly quota",
    "transport": "fix: check your network connection and retry",
    "server": "fix: GitHub returned a server error; retry in a minute (https://www.githubstatus.com)",
}


def remedy_for_kind(kind):
    """The fix line for a `gh_api.classify` kind, or None for a kind with no single fix (`other`,
    `invalid`, `proxy` -- the proxy shape already carries its own remedy in the failure text)."""
    return REMEDIES.get(kind)


def with_remedy(text, kind):
    """`text` with the kind's fix line APPENDED. Idempotent (a text that already carries a fix is
    returned as it is) and total: no remedy for the kind returns `text` unchanged."""
    fix = REMEDIES.get(kind)
    text = str(text)
    if not fix or fix in text:          # the EXACT line, so a 'fix: ...' title in argv cannot suppress it
        return text
    return "%s -- %s" % (text, fix)
