#!/usr/bin/env python3
"""The cross-repo access check, and the landing tier it selects (#1472, epic #1464, story #1427).

GitHub cannot merge two PRs atomically, so a unit of work spanning two repos lands by one of two
strategies, and which one applies depends on what the actor ACTUALLY HAS:

  * **tier 1 -- access to both repos**: a tracking issue owns the pair and neither side merges until
    both are green (the gate itself is #1474, not this module);
  * **tier 2 -- no access to one side**: contract-first, or a human coordinates, and the unavailable
    half is raised through the ledger addressed to whoever owns it.

This module is the gate between them, and it exists because that gate must be **checked, never
assumed**. A permissions failure discovered at merge time is the most expensive moment to discover
it: the work is written, the branch is cut, the PR is open, and the answer is that it can never
land.

THE FAILURE THIS MODULE IS SHAPED AROUND IS NOT "no access". It is **"no answer, read as no
access"**. Tier 2 is the safe-looking strategy, which is exactly what makes it dangerous as a
default: a 502, a rate limit, a DNS blip or an expired token would each silently select it, record
"no access" against a repo that is in fact fully reachable, and raise a ledger request to a human
who had nothing to answer. So the verdict vocabulary has THREE members where two would do --
`granted`, `denied`, `unknown` -- and `unknown` is not a soft `denied`. It selects NO tier at all.
The decision comes back `flagged`, and a human is told. The whole module can be read as one rule:

    a check that cannot get an answer must not answer.

Every default therefore points at `unknown`: an unrecognised `gh` failure, an unparseable payload, a
response with no `permissions` block or one that names no write flag, a repo name that is not a repo
name, a repo the caller never measured. None of them is a denial. `_classify_failure`'s final line is
the single most important line here, and it is pinned by a mutation test for that reason.

THERE ARE EXACTLY FOUR WAYS TO REACH `DENIED`, and all four are GitHub answering about this repo and
this token rather than about the transport: a 404 from a confirmed identity, an `archived` repo, a
`disabled` repo, and a stated permissions block whose write flags are all false. Notably NOT a 403 --
see `_classify_failure`, which explains why that status cannot be a denial on this endpoint.

THE WRONG-ACCOUNT 404, WHICH IS THE HARD HALF AND THE REASON `identity()` EXISTS. GitHub answers a
read of a private repo the token cannot see with **404 -- byte-identical to the answer for a repo
that does not exist**. That is unavoidable (it is deliberate: a 403 would confirm the repo's
existence to someone with no right to know). It is also fine, ordinarily: "cannot see it" and "it is
not there" are the same operational fact -- we cannot land a PR there either way.

It stops being fine the moment the identity behind the token is not the intended one. `gh`'s active
account is a single device-global keyring slot: any other tool, project or person sharing the machine
can switch it, and folder-scoping it via `GH_CONFIG_DIR` was measured and does NOT isolate it. So on
a machine with more than one account configured -- an ordinary state, and the one this plugin was
developed on -- the same private repo reads `granted` or `denied` depending on nothing but which slot
happened to be active, and neither the status code nor the body says so.

The only thing that makes those two 404s distinguishable is the thing GitHub *will* tell you: **who
you are**. So `identity()` resolves the authenticated login once and compares it against the login
this project is configured to act as (`discovery.github.assignee`), and `check_access` refuses to
return ANY verdict -- positive or negative -- from an identity that is not confirmed. A drifted
account yields `unknown`/`wrong-account`, which flags, names both logins, and is fixed in one
gesture. `tests/test_cross_repo.py::test_the_same_404_is_denial_or_flag_purely_by_identity` reads
one identical 404 twice to prove the distinction is real and not merely described here.

The positive verdict is suppressed too, not just the negative one, and that is deliberate: a drifted
account that HAS push access would select tier 1, and then the merge -- run later, under whichever
account is active by then -- would fail at exactly the moment this module exists to move earlier.
One rule, no exceptions: no verdict survives an unpinned identity.

`@me` is not a pin. It is the shipped default for `assignee` and names nobody, so there is nothing to
compare against and verdicts are trusted -- but the login that produced them is recorded in the
decision either way, so the answer is always attributable after the fact. The exposure is narrower
than it looks: under `@me` the loop picks the issues assigned to whoever is authenticated, so a
drifted account changes which issues are picked at all, and the drift is visible at the top of the
funnel rather than hidden at the bottom.

WHY THE TRANSIENT VOCABULARY IS NOT SHARED WITH `sources.py`. `GitHubSource._TRANSIENT` exists to
decide whether to RETRY a `gh project` call, where a false positive costs one extra attempt and a
false negative costs a dropped board update -- a substring table is right for that. Here a
misclassification costs a wrong landing tier and a ledger request to a human, so this module parses
the HTTP status shape instead of matching substrings. Sharing one table would force the looser rule
on the stricter caller. What must not drift is the CONTAINMENT -- anything that source already calls
transient must still be `unknown` here -- and that is pinned by a test rather than by an import.

SCOPE. This module decides and RECORDS; it does not enforce. The both-green merge gate is #1474 and
reads `recorded()`; propagating the registry entry to sibling repos is #1477. Ownership enforcement
(§7.3's `authorized` grant, a separate question from whether the API lets us in at all) is level 5.
The split matters for the timing property this goal is really about: the API check happens once, at
pick, in `check_at_pick`, and the merge-time consumer reads a file. `recorded()` has no `run`
parameter and no way to acquire one, so "at pick time, not at merge time" is a property of the
shape, not a promise about call order.
"""
import importlib.util
import json
import pathlib
import re
import subprocess
import sys
import time

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


features = _load("features")                    # what unit does this issue declare? (#1465)
feature_registry = _load("feature_registry")    # what does the repo remember about it? (#1469)
ledger = _load("ledger")                        # the `to`-addressed transport that already exists
legacy = _load("legacy")                        # #239: the previous name's record schema reads too
state = _load("state")                          # `unsafe_goal_reason`, the shared path-component guard
work = _load("work")                            # `stem`: the one goal -> filename rule in this plugin
gh_session = _load("gh_session")                # #78: a Remote session's proxy block is not a denial


# --------------------------------------------------------------------------- the vocabulary

#: A repo's measured verdict. THREE, not two -- see the module docstring. `UNKNOWN` is a first-class
#: answer meaning the question was not answered, and it is never a softer `DENIED`.
GRANTED, DENIED, UNKNOWN = "granted", "denied", "unknown"
VERDICTS = (GRANTED, DENIED, UNKNOWN)

#: What the unit's landing strategy is. `FLAGGED` is the outcome for "not measurable right now" and
#: is deliberately NOT one of the tiers: selecting a tier on no evidence is the bug this module
#: exists to prevent, and a fallback selected by accident is still selected by accident.
TIER_1, TIER_2, FLAGGED = "tier-1", "tier-2", "flagged"
#: The three ways a goal is simply not this module's business, kept distinct from `FLAGGED` so a
#: repo that never adopted the branching model does not read as a problem.
NOT_ADOPTED, NO_UNIT, NOT_CROSS_REPO = "not-adopted", "no-unit", "not-cross-repo"
OUTCOMES = (TIER_1, TIER_2, FLAGGED, NOT_ADOPTED, NO_UNIT, NOT_CROSS_REPO)

#: Why a verdict is what it is. Reasons are a closed vocabulary because they are written into the
#: decision record #1474 reads and into the ledger line a human reads; free text in either would be
#: unmatchable by the first and unskimmable by the second.
READ_ONLY = "read-only"                 # the API answered, and the answer is "you may not push"
ARCHIVED = "archived"                   # `push: true` on an archived repo is a lie until you write
NOT_VISIBLE = "not-visible"             # 404 from the CONFIRMED identity -- absent or private, same fact
FORBIDDEN = "forbidden"                 # a 403 nothing more specific matched -- still not a denial
RATE_LIMITED = "rate-limited"
SERVER_ERROR = "server-error"
NETWORK = "network"
UNAUTHENTICATED = "unauthenticated"     # 401: says nothing about the repo, only about the token
SSO_REQUIRED = "sso-required"           # a credential state a human fixes in seconds, not a denial
SESSION_PROXY = "session-proxy"         # #78, and never a permission fact
UNCLASSIFIED = "unclassified"           # the fail-safe default -- see `_classify_failure`
UNREADABLE = "unreadable-payload"
NO_PERMISSIONS = "no-permissions"
MALFORMED_REPO = "malformed-repo"
UNMEASURED = "unmeasured"               # named in the registry, never checked -- not "fine"
WRONG_ACCOUNT = "wrong-account"
IDENTITY_UNRESOLVED = "identity-unresolved"
#: No account pinned AND no backlog filter -- see `_expected_login`. Distinct from
#: `IDENTITY_UNRESOLVED` (we know who we are, nothing says who we should be) because the remedy is
#: a one-line config change rather than a transport problem to wait out.
IDENTITY_UNPINNED = "identity-unpinned"
#: 403 sub-cases that are about the TOKEN or the caller's network position, never about the repo.
TOKEN_SCOPE = "token-scope"
IP_ALLOWLIST = "ip-allowlist"
#: A repo GitHub has switched off (billing lapse, org suspension) -- `archived`'s sibling, and a
#: write is refused for the same reason.
DISABLED = "disabled"
#: The registry named the unit's repos in a shape this module cannot read -- not "no repos".
UNREADABLE_REPOS = "unreadable-repos"

#: The version key of the decision record. `recorded()`'s consumer is a different goal in a
#: different release, so the shape it reads is a contract and carries a version like every other.
RECORD_SCHEMA = "sigma/landing@1"

#: Where a pick-time decision is kept: one file per goal, under `.sdlc/state/`, which
#: `sigma-setup`'s `RUNTIME_IGNORES` already gitignores. One file per goal for the same reason the
#: feature registry is one file per unit -- two concurrent picks must not share a write target.
RECORD_DIRNAME = "landing"

#: `owner/name`, and nothing that could become extra path segments or a query string. Deliberately
#: strict: this string is interpolated into a `gh api repos/<repo>` path.
_REPO_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*\Z")

#: `gh` reports the status as `(HTTP 404)`; some shapes print `HTTP 404:` instead. Anchored on the
#: literal word so a bare `404`/`500` ANYWHERE in a message body -- a repo named `sku-403`, an issue
#: number, a byte count -- can never be mistaken for a status line. A missed status falls through to
#: `UNCLASSIFIED`, which is `UNKNOWN`, so anchoring tightly costs a flag and never a wrong tier.
#:
#: This claim used to be made in prose and pinned by nothing, and a mutation run showed it: dropping
#: the anchor to a bare `(\d{3})` survived the whole suite, on the exact input this comment names.
#: `test_a_three_digit_run_in_free_text_is_not_a_status` is the pin. It matters more than it looks:
#: this regex is the only line in the module that turns free text into a `DENIED`.
_STATUS_RE = re.compile(r"\bhttp[ /]?(\d{3})\b")

#: Failure shapes that are about the NETWORK or the ceiling, never about the repo. Matched before
#: the status switch below so that a 403 arriving DRESSED as one of them keeps its own sub-reason
#: rather than falling through to the generic 403 arm -- not because a 403 would otherwise deny; it
#: cannot. See `_classify_failure`, which carries the measurement.
_RATE_LIMIT_MARKERS = ("rate limit", "secondary rate", "abuse detection", "too many requests")
_NETWORK_MARKERS = ("timeout", "timed out", "deadline exceeded", "could not resolve host",
                    "no such host", "dial tcp", "connection refused", "connection reset",
                    "network is unreachable", "temporary failure in name resolution",
                    "tls handshake", "unexpected eof", "broken pipe", "try again", "temporarily")
_SSO_MARKERS = ("saml", "single sign-on", "single sign on", "sso")
#: 403 bodies that name the TOKEN as the limitation rather than the repo. Diagnostic only -- every
#: 403 is `UNKNOWN` regardless (see `_classify_failure`); these just say WHICH kind so the flag a
#: human reads names the fix.
_TOKEN_SCOPE_MARKERS = ("not accessible by personal access token", "not accessible by integration",
                        "fine-grained permission", "must have push access",
                        "repository read permissions", "resource not accessible")
_IP_ALLOWLIST_MARKERS = ("ip allow list", "ip address", "not permitted to access")


def _note(message):
    """One stderr line, never an exception -- the same shape and the same reason as
    `features._note`: a diagnostic must never be the thing that breaks a pick."""
    try:
        sys.stderr.write(message)
    except Exception:                     # noqa: BLE001 - a diagnostic must never break a pick
        pass


def _mapping(value):
    """`value` if it is a mapping, else `{}` -- the ONE reducer every config/payload read here goes
    through.

    Written as a named helper rather than inline `(x or {})` because that idiom is what produced the
    same defect twice in this file: it reduces `None` and `{}` correctly and then raises
    `AttributeError` on any OTHER non-mapping (a bare string, a list), which is exactly the
    hand-edited config shape the guard exists to survive. A module whose docstrings promise "never
    raises" cannot afford an idiom that is right for two of the three cases."""
    return value if isinstance(value, dict) else {}


#: Seconds any one `gh` call may take before it is treated as a network failure. A module whose
#: vocabulary includes `NETWORK`/`timeout` could not previously observe one from its OWN call --
#: `subprocess.run` without a timeout waits forever, so a wedged `gh` hung the pick rather than
#: degrading to the `flagged` this module exists to produce. Generous, because a slow answer is
#: still an answer and re-asking costs a whole pick.
GH_TIMEOUT_SECONDS = 30


def _run_gh(args, timeout=GH_TIMEOUT_SECONDS):
    """-> `(returncode, stdout, stderr)`. Deliberately NOT `ledger._run_gh`'s shape (a string, empty
    on failure): this module's whole job is telling failures apart, and a runner that discards the
    exit code and the error text has already thrown away everything the classification needs.

    A timeout becomes a synthetic non-zero result whose text names it, so it flows through
    `_classify_failure` as `NETWORK` like every other transport failure instead of arriving as an
    exception the caller has to special-case -- the same shape `autowatch._run_drive` uses for its
    own timeout."""
    try:
        proc = subprocess.run(["gh", *[str(a) for a in args]], capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", "gh: the call timed out after %ss" % timeout
    return proc.returncode, proc.stdout, proc.stderr


def tier_of(outcome):
    """The tier NUMBER, for the two outcomes that are tiers, and `None` for every other. Exists so
    no caller has to re-derive it and accidentally map `flagged` onto 2."""
    return {TIER_1: 1, TIER_2: 2}.get(outcome)


# --------------------------------------------------------------------------- identity


def _expected_login(config):
    """-> `(expected_login_or_None, backlog_is_filtered)` from `discovery.github.assignee`.

    TWO facts, not one, because "no pin" comes in two kinds that must not be treated alike.

    THE NORMALISATION IS THE KIT'S, NOT A SECOND OPINION. `sources.py::_assignee_login` already
    resolves this exact config field for the picker: empty means no filter at all, the literal
    `"@me"` means whoever is authenticated, and anything else is `raw.lstrip("@").casefold()`. That
    last clause is why `@handle` is a perfectly ordinary pin and not a permanent mismatch -- a value
    the picker happily accepts must not be the value this check calls a drifted account. Pinned by a
    test against `sources`' own rule rather than by an import, because that resolver is a method
    with caching and a network call for `@me`; only its normalisation is shareable.

      - `""` (the `sigma-init` template's shipped default) -> `(None, False)`. NO pin AND no filter:
        `mirror.py` and `sources.py` both drop the assignee argument entirely for it, so the backlog
        is byte-identical under every account. Nothing anywhere ties the run to an identity, which
        is why `identity()` refuses to confirm it -- see that function.
      - `"@me"` -> `(None, True)`. No literal to compare against, but the picker filters the backlog
        BY the authenticated account, so a drifted account changes which issues are picked at all
        and the drift is visible at the top of the funnel rather than hidden at the bottom. That is
        a real, if indirect, pin, and it is the one case where trusting a verdict is defensible.
      - anything else -> `(<normalised login>, True)`."""
    raw = _mapping(_mapping(config).get("discovery")).get("github")
    raw = _mapping(raw).get("assignee")
    raw = raw.strip() if isinstance(raw, str) else ""
    if not raw:
        return None, False
    if raw == "@me":
        return None, True
    return raw.lstrip("@").casefold(), True


def identity(config, run=None):
    """Who is `gh` actually authenticated as, and may this project reason from that?

    -> `{"login", "expected", "confirmed", "reason", "detail"}`. Never raises -- an identity that
    cannot be established is an unconfirmed identity, which is a flag, not a crash, and every read
    of `config` on the way here goes through `_mapping` so a hand-edited block cannot make the
    promise false.

    `confirmed` is True in exactly two cases: the resolved login matches the configured one, or the
    project pins nothing but at least filters its backlog by the authenticated account (`@me`). It
    is False when no login could be resolved, when the resolved login is not the configured one,
    and -- the case that is easy to miss -- when the project neither pins an account nor filters by
    one, because then no answer this module gets is attributable to anybody and a wrong-account 404
    is indistinguishable at every level of the system, not just this one."""
    expected, filtered = _expected_login(config)
    try:
        code, out, err = (run or _run_gh)(["api", "user", "-q", ".login"])
    except Exception as exc:              # noqa: BLE001 - an unresolvable identity is a flag
        code, out, err = 1, "", str(exc)
    login = out.strip() if (code == 0 and isinstance(out, str)) else ""
    if not login:
        return {"login": None, "expected": expected, "confirmed": False,
                "reason": IDENTITY_UNRESOLVED,
                "detail": (str(err) or "gh could not report the authenticated account").strip()}
    if expected and login.casefold() != expected:
        return {"login": login, "expected": expected, "confirmed": False, "reason": WRONG_ACCOUNT,
                "detail": "gh is authenticated as %s but this project is configured to act as %s"
                          % (login, expected)}
    if expected is None and not filtered:
        return {"login": login, "expected": None, "confirmed": False, "reason": IDENTITY_UNPINNED,
                "detail": "discovery.github.assignee is empty, so the backlog is not filtered by "
                          "any account and nothing ties this answer to %s -- set it to the login "
                          "this project acts as, or to @me" % login}
    return {"login": login, "expected": expected, "confirmed": True, "reason": None, "detail": ""}


# --------------------------------------------------------------------------- the access check


def _verdict(repo, verdict, reason, detail=""):
    return {"repo": repo if isinstance(repo, str) else repr(repo),
            "verdict": verdict, "reason": reason, "detail": str(detail or "")[:400]}


def _classify_failure(repo, text):
    """A non-zero `gh api repos/<repo>` result -> a verdict.

    EXACTLY ONE STATUS PRODUCES `DENIED`: 404, and only ever from a confirmed identity (the guard
    is `check_access`'s, which is this function's only caller). Everything else that arrives here --
    named or not, 4xx or 5xx -- is `UNKNOWN`. That is not caution for its own sake: on
    `GET /repos/{owner}/{repo}` a genuine "you may not have this" IS the 404, so any other status is
    by construction a statement about the transport, the ceiling or the credential rather than about
    the repo. The 403 arm below says the same thing at length, because it is the one people expect
    to deny.

    ORDER IS THE WHOLE DESIGN -- BUT IT PROTECTS THE DIAGNOSIS, NOT THE VERDICT. That distinction is
    the whole of it, and this docstring previously got it wrong: it said the keywords are matched
    first because "a 403 is otherwise a real denial", which was the pre-#1472-review rationale and
    is the opposite of what the code forty lines down now does. Since no 403 can deny, NOTHING in the
    ordering changes any verdict between `denied` and `unknown`. What it changes is the REASON, and
    the reason is what a human acts on and what the ledger note carries.

    So the rate-limit and SSO markers sit above the status switch because a 403 arriving DRESSED as
    one of them must reach its own sub-reason instead of falling through to the generic 403 arm and
    reporting a bare `forbidden`. Both are measured, not assumed: moving either below the switch
    turns `rate-limited` into `forbidden` and `sso-required` into `forbidden`, which is exactly what
    the `rate-limit-after-status` and `sso-after-status` mutants report.

    Two arms then cut back across that grain, each for its own reason:

      - an explicit 5xx outranks the NETWORK keywords, because `504 Gateway Timeout` contains the
        word "timeout" and is a server-side failure, not a local one. This one does change the
        reason a human reads (`server-error`, not `network`) and nothing else;
      - `429` and `401` sit INSIDE the status switch despite denying nothing, for two different
        reasons that were once wrongly given as one. 429's prose form (`too many requests`) is
        already matched above, so its arm only catches a bare `(HTTP 429)` with no message. 401 has
        NO keyword form anywhere above it -- `bad credentials` is in none of the marker tuples -- so
        that arm is the only thing that ever classifies it.

    The keyword matching is loose, and safe anyway, because every marker resolves to `UNKNOWN` and
    only one arm of the status switch can deny: the matching is ONE-DIRECTIONAL, so a collision can
    only make this module more conservative (a lost tier 2, flagged for a human), never less. The
    looseness is bounded by the direction, not by the precision of the words."""
    lowered = text.casefold()
    match = _STATUS_RE.search(lowered)
    status = int(match.group(1)) if match else None
    if gh_session.proxy_session_block(text):
        return _verdict(repo, UNKNOWN, SESSION_PROXY, gh_session.REMEDIATION)
    if any(m in lowered for m in _RATE_LIMIT_MARKERS):
        return _verdict(repo, UNKNOWN, RATE_LIMITED, text)
    if status is not None and 500 <= status <= 599:
        # AHEAD of the network markers, not behind them, and the case that settled it is `504
        # Gateway Timeout`: the word "timeout" is in it, so a keyword-first order diagnosed a
        # server-side failure as a local network one. An explicit 5xx is the most authoritative
        # signal available -- the server answered, and told us it was the one at fault.
        return _verdict(repo, UNKNOWN, SERVER_ERROR, text)
    if any(m in lowered for m in _NETWORK_MARKERS):
        return _verdict(repo, UNKNOWN, NETWORK, text)
    if any(m in lowered for m in _SSO_MARKERS):
        return _verdict(repo, UNKNOWN, SSO_REQUIRED, text)
    if status is not None:
        if status == 429:
            return _verdict(repo, UNKNOWN, RATE_LIMITED, text)
        if status == 401:
            return _verdict(repo, UNKNOWN, UNAUTHENTICATED, text)
        if status == 404:
            # The ONE place a negative answer is believed, and only ever reached from a CONFIRMED
            # identity -- see `check_access`'s guard and the module docstring. Absent and
            # private-and-invisible are the same operational fact for this identity: no PR of ours
            # can land there.
            return _verdict(repo, DENIED, NOT_VISIBLE, text)
        if status == 403:
            # A 403 IS NEVER A DENIAL ON THIS ENDPOINT, and this is the arm the whole classifier
            # turns on. `GET /repos/{owner}/{repo}` answers "you may not have this repo" with 404,
            # not 403 -- 404 is what hides a private repo's existence from someone with no right to
            # know it exists. So every 403 that reaches here is about something else: a token whose
            # scopes or fine-grained permissions are too narrow (`Resource not accessible by
            # personal access token`, `not accessible by integration`), an org IP allow-list, a
            # corporate proxy's own block page, or an SSO/rate-limit shape the markers above did not
            # match. Each of those is the SAME kind of fact as the 401 two lines up -- about the
            # credential or the caller's network position, never about the repo -- and reading it as
            # "no access" selects tier 2 and raises a request to an owner who has nothing to answer.
            # The sub-reason is best-effort and diagnostic; the verdict is not conditional on it.
            if any(m in lowered for m in _TOKEN_SCOPE_MARKERS):
                return _verdict(repo, UNKNOWN, TOKEN_SCOPE, text)
            if any(m in lowered for m in _IP_ALLOWLIST_MARKERS):
                return _verdict(repo, UNKNOWN, IP_ALLOWLIST, text)
            return _verdict(repo, UNKNOWN, FORBIDDEN, text)
    # THE FAIL-SAFE DEFAULT. An error nobody has classified is not evidence of anything, least of
    # all of the fallback tier. Pinned by a mutation test precisely because flipping this one word
    # is invisible in review and turns every future unrecognised `gh` failure into a silent tier 2.
    return _verdict(repo, UNKNOWN, UNCLASSIFIED, text)


def check_access(repo, ident, run=None):
    """Can work land in `repo`, as the identity `ident` describes? -> one verdict dict.

    Never raises. The two guards run BEFORE the request, not after it: an answer that may not be
    interpreted is not worth asking for, and asking first would leave the interpretation as the only
    thing standing between a drifted account and a wrong tier."""
    if not (isinstance(repo, str) and _REPO_RE.match(repo)):
        return _verdict(repo, UNKNOWN, MALFORMED_REPO,
                        "%r is not an owner/name repo -- nothing can be asked about it" % (repo,))
    # Reduced to a mapping FIRST: an `ident` that is not one carries no reason and no detail, and
    # `(ident or {}).get(...)` would have raised on any non-empty non-dict (a bare string, say) --
    # turning the guard that exists to stop a wrong verdict into the thing that breaks the pick.
    ident = ident if isinstance(ident, dict) else {}
    confirmed = ident.get("confirmed") is True
    if not confirmed:
        # No verdict survives an unpinned identity, positive or negative. See the module docstring.
        return _verdict(repo, UNKNOWN, ident.get("reason") or IDENTITY_UNRESOLVED,
                        ident.get("detail") or "the authenticated account is not confirmed")
    try:
        code, out, err = (run or _run_gh)(["api", "repos/" + repo])
    except Exception as exc:              # noqa: BLE001 - a runner that dies answered nothing
        return _verdict(repo, UNKNOWN, UNCLASSIFIED, "the gh call could not be run: %s" % exc)
    if code != 0:
        return _classify_failure(repo, "%s\n%s" % (out or "", err or ""))
    try:
        repo_json = json.loads(out)
    except Exception:                     # noqa: BLE001 - an unreadable 200 answered nothing either
        return _verdict(repo, UNKNOWN, UNREADABLE, "the 200 response was not JSON")
    if not isinstance(repo_json, dict):
        return _verdict(repo, UNKNOWN, UNREADABLE, "the 200 response was not a repo object")
    if repo_json.get("archived") is True:
        # Checked BEFORE permissions, because GitHub keeps reporting `push: true` on an archived
        # repo -- the write is refused at the moment it is attempted, which is merge time.
        return _verdict(repo, DENIED, ARCHIVED, "the repo is archived and accepts no writes")
    if repo_json.get("disabled") is True:
        # `archived`'s sibling on the same object, for the same reason: GitHub disables a repo for
        # a lapsed bill or a suspended org and keeps reporting the permissions the account WOULD
        # have. Reading those as access defers the failure to the push, which is merge time.
        return _verdict(repo, DENIED, DISABLED, "the repo is disabled and accepts no writes")
    permissions = repo_json.get("permissions")
    if not isinstance(permissions, dict):
        # A 200 with no permission block is an unauthenticated or proxied read, not a grant.
        return _verdict(repo, UNKNOWN, NO_PERMISSIONS,
                        "the repo object carried no permissions for this identity")
    # THE THREE FLAGS THAT DECIDE THIS, AND THE PAYLOAD HAS TO CARRY AT LEAST ONE OF THEM.
    # `if k in permissions` alone was a no-op on the shape it most needed to catch: `{}` and
    # `{"pull": true}` name none of the write flags, so nothing was type-checked, `.get` returned
    # `None` three times, and the function fell through to `read-only` -- a POSITIVE statement that
    # this account cannot push, derived from a payload GitHub never answered with. An absent `push`
    # key is exactly as much of an answer as an absent `permissions` block, and now reads as one.
    stated = [k for k in ("push", "admin", "maintain") if k in permissions]
    if not stated:
        return _verdict(repo, UNKNOWN, NO_PERMISSIONS,
                        "the permissions block named none of push/admin/maintain, so it states "
                        "nothing about writing")
    if any(not isinstance(permissions.get(k), bool) for k in stated):
        # A PERMISSION FLAG THAT IS NOT A BOOLEAN IS NOT AN ANSWER either. GitHub returns booleans;
        # a string, a number or a null arrived from something else -- a proxy, a `--jq` filter, a
        # hand-edited fixture. The dangerous reading is not that `"false"` might be believed as
        # access (`is True` below already refuses it), it is the same fall-through to `read-only`.
        # Only the keys actually consulted are checked: a garbled `triage` has no bearing here.
        return _verdict(repo, UNKNOWN, UNREADABLE,
                        "the permissions block carried a non-boolean write flag")
    if (permissions.get("push") is True or permissions.get("admin") is True
            or permissions.get("maintain") is True):
        # `is True`, never truthiness -- and honestly, this is belt-and-braces rather than the
        # tested guard it reads as. Past the two gates above, each of these keys is either absent
        # (`.get` -> `None`) or a real `bool`, so the reachable domain is exactly `{None, True,
        # False}` and `is True` agrees with truthiness on all three. It is kept because it is what
        # still holds if those gates are ever relaxed, and said plainly so nobody mistakes it for
        # the load-bearing one. The load-bearing ones are `stated` and the isinstance check.
        return _verdict(repo, GRANTED, None, "")
    return _verdict(repo, DENIED, READ_ONLY,
                    "the repo is readable but this identity cannot push to it")


# --------------------------------------------------------------------------- the tier decision


def _decision(outcome, **over):
    base = {"schema": RECORD_SCHEMA, "goal": None, "unit": None, "outcome": outcome,
            "tier": tier_of(outcome), "cross_repo": False, "identity": None, "repos": {},
            "denied": [], "unknown": [], "unaddressed": [], "raised": [], "raise_suppressed": False,
            "why": "", "at": None}
    base.update(over)
    return base


def decide(entry, accesses, goal=None, unit=None, ident=None):
    """The pure half: a registry entry plus the verdicts measured for it -> the landing decision.

    A REPO NAMED IN THE ENTRY BUT NOT MEASURED IS `UNKNOWN`, NOT ABSENT. The alternative -- deciding
    over whatever happens to be in `accesses` -- makes a caller that skipped a repo, or crashed
    halfway through the list, produce a confident tier 1 over a repo nobody ever asked about. The
    registry names the participants; this function insists on an answer for each of them."""
    raw_repos = _mapping(entry).get("repos")
    unreadable_repos = raw_repos is not None and not isinstance(raw_repos, dict)
    entry = feature_registry.normalise_entry(entry)
    repos = sorted(entry["repos"])
    measured = {}
    for one in accesses or ():
        # `one.get("repo")` is membership-tested against a dict, so an UNHASHABLE repo (a list, from
        # a hand-built call -- `decide` is the pure half #1474 may call directly) would raise
        # `TypeError` out of the one function in here that is supposed to be total.
        name = one.get("repo") if isinstance(one, dict) else None
        if isinstance(name, str) and name in entry["repos"]:
            measured[name] = {"verdict": one.get("verdict") if one.get("verdict") in VERDICTS
                              else UNKNOWN,
                              "reason": one.get("reason"), "detail": one.get("detail", "")}
    for repo in repos:
        measured.setdefault(repo, {"verdict": UNKNOWN, "reason": UNMEASURED, "detail": ""})

    if unreadable_repos:
        # A `repos` block that is not a mapping is a registry this module cannot read, and
        # `normalise_entry` turns it into `{}` -- which would arrive here indistinguishable from a
        # unit that genuinely names none, and be answered "nothing spans a boundary". That is a
        # conclusion drawn from a value nobody could parse. The NORMALISATION is #1469's contract
        # and correct; the INTERPRETATION of the result is this module's, and ignorance is the
        # honest one.
        return _decision(FLAGGED, goal=goal, unit=unit, identity=ident, repos=measured,
                         why="the registry entry's repos block is a %s, not a mapping, so which "
                             "repos this unit spans cannot be read" % type(raw_repos).__name__)
    if len(repos) < 2:
        # Not cross-repo, so there is no sibling to gate against and no tier to select. Kept
        # distinct from tier 1 so #1474's both-green gate is never engaged for a unit that has
        # nothing to be green alongside.
        #
        # ZERO repos is deliberately grouped here rather than flagged, and the trade is worth
        # naming: an entry recording no repos cannot PROVE the unit is single-repo, but `repos` is
        # filled by propagation (#1477), so before that runs an empty block is the ordinary state of
        # every freshly-recorded unit. Flagging it would flag nearly every unit today and teach a
        # human to ignore the flag. The `why` says which case it was, so the record never claims
        # more than it measured.
        return _decision(NOT_CROSS_REPO, goal=goal, unit=unit, identity=ident, repos=measured,
                         why=("the registry records no repos for this unit yet" if not repos else
                              "the unit names 1 repo; nothing spans a boundary"))

    unknown = sorted(r for r in repos if measured[r]["verdict"] == UNKNOWN)
    denied = sorted(r for r in repos if measured[r]["verdict"] == DENIED)
    # UNKNOWN OUTRANKS DENIED, and the order of these two branches IS that rule. An outstanding
    # unknown could be the repo that changes the answer, so no tier may be selected while one
    # stands -- including the fallback tier, which is still a decision even though it is the safe
    # one. Pinned by a mutation test that reverses exactly these two branches.
    if unknown:
        outcome = FLAGGED
    elif denied:
        outcome = TIER_2
    else:
        outcome = TIER_1
    return _decision(outcome, goal=goal, unit=unit, identity=ident, cross_repo=True, repos=measured,
                     denied=denied, unknown=unknown, why=_why(outcome, measured, unknown, denied))


def _why(outcome, measured, unknown, denied):
    def named(repos):
        return ", ".join("%s (%s)" % (r, measured[r]["reason"] or "-") for r in repos)
    if outcome == FLAGGED:
        return "access to %s could not be measured, so no tier was selected" % named(unknown)
    if outcome == TIER_2:
        return "no access to %s, so the unit lands contract-first" % named(denied)
    return "access confirmed to every repo the unit names"


# --------------------------------------------------------------------------- the record


def decision_path(sdlc_dir, goal):
    """The one place a goal becomes a record path, and therefore the one place that refuses.

    `work.stem` is BORROWED, never re-derived: the merge-time consumer looks the record up by the
    same goal, and two opinions about what a goal's filename is would be two files. Rejects an empty
    stem as well as an unsafe one -- `state.unsafe_goal_reason` is about path ESCAPE and considers
    `""` safe, but a record filed under no goal is a record nothing can ever find again."""
    goal_stem = work.stem(goal)
    if not str(goal_stem).strip():
        raise ValueError("a landing decision needs a goal to be filed under, and %r reduces to "
                         "nothing" % (goal,))
    reason = state.unsafe_goal_reason(goal_stem)
    if reason:
        raise ValueError("unsafe goal %r for the landing record: %s" % (goal, reason))
    return pathlib.Path(sdlc_dir) / "state" / RECORD_DIRNAME / (str(goal_stem) + ".json")


def recorded(sdlc_dir, goal):
    """What pick time decided about this goal's landing, or None.

    THE MERGE-TIME READ, AND IT TAKES NO RUNNER. There is nowhere to put one, so this function
    cannot perform the access check however badly a future caller wants it to -- which is what makes
    "at pick time, not at merge time" structural rather than a convention. `None` means no decision
    was ever recorded, which is a refusal for the gate to act on, never an implied tier."""
    try:
        got = json.loads(decision_path(sdlc_dir, goal).read_text(encoding="utf-8"))
    except Exception:                     # noqa: BLE001 - absent and corrupt both mean "no decision"
        return None
    return got if isinstance(got, dict) and legacy.schema_is(got.get("schema"), RECORD_SCHEMA) else None


#: CEILING on the landing records one unit-keyed lookup will read. The lookup scans the landing folder once (one
#: small file read per record, nothing indexed by unit), so it is linear in the records ever written; nothing prunes
#: them. At 10x or 100x that growth is the cost, so the scan is bounded and over the bound it REFUSES (cannot
#: answer) rather than reading unboundedly or guessing. PROVISIONAL default, not measured.
MAX_UNIT_LOOKUP_RECORDS = 5000

#: Decisions that say a goal has no pair at all. Every other outcome on a record naming the unit is either a pair
#: (`tier-1`, `tier-2`) or an unknown (`flagged`, or anything this release has not heard of), and unknown refuses.
_NO_PAIR_OUTCOMES = (NOT_ADOPTED, NO_UNIT, NOT_CROSS_REPO)


def _unit_landing_records(sdlc_dir, unit, max_records=None):
    """-> `(records, error)`: every landing record that names `unit`, read from the goal-keyed store.

    READ-ONLY and keyed by the unit: the store is filed per goal (`decision_path`), so the unit's records are found
    by scanning it. The store itself is never written here. Anything that stops the scan from being a complete
    answer is an `error` string (an unreadable folder, a symlink, a file that does not parse as a landing record,
    more files than the ceiling), because a record that cannot be read might be this unit's."""
    folder = pathlib.Path(sdlc_dir) / "state" / RECORD_DIRNAME
    try:
        if not folder.exists():
            return [], None
        if folder.is_symlink() or not folder.is_dir():
            return [], "the landing record folder is not a plain directory"
        files = sorted(p for p in folder.iterdir() if p.name.endswith(".json"))
    except OSError as exc:
        return [], "the landing record folder could not be listed (%s)" % exc
    ceiling = MAX_UNIT_LOOKUP_RECORDS if max_records is None else max_records
    if len(files) > ceiling:
        return [], "there are more than %d landing records, the most one lookup will read" % ceiling
    found = []
    for path in files:
        try:
            if path.is_symlink():
                return [], "landing record %s is a symbolic link" % path.name
            got = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return [], "landing record %s could not be read (%s)" % (path.name, type(exc).__name__)
        if not isinstance(got, dict) or not legacy.schema_is(got.get("schema"), RECORD_SCHEMA):
            return [], "landing record %s is not a landing record this release understands" % path.name
        if got.get("unit") == unit:
            found.append(got)
    return found, None


def unit_sibling_check(sdlc_dir, unit, here, landed=None, max_records=None):
    """-> `(ok, reason)`: has every OTHER repository's half of `unit` landed?

    The unit-keyed sibling lookup the landing engine runs before it merges anything. `work.sibling_gate` is keyed by
    a goal and stays unwired; this one is keyed by the unit, because the engine has a unit and no goal id.

    Siblings are the repositories, other than `here`, that either the unit's landing records or the feature
    registry name. `landed(repo, branch)` is the caller's measurement of whether that repository's unit branch has
    landed (the shared landed predicate, supplied by the engine); only the boolean `True` counts as landed.

    FAIL CLOSED ON ERROR, NOT ON ABSENCE. A unit with no records and no other repository is simply not cross-repo and
    passes without any lookup. But a record that cannot be read, a `flagged` or unknown decision, a sibling with no
    recorded branch, a missing or failing `landed`, or any answer other than `True` refuses. The reason names the
    sibling so a person can act. Pure of writes; never touches `state/landing` beyond reading it."""
    sync = work._feature_sync()
    records, error = _unit_landing_records(sdlc_dir, unit, max_records)
    if error:
        return False, "cross-repo unit `%s`: the sibling lookup cannot answer: %s" % (unit, error)
    repos = {}                                  # sibling repo -> spelling kept from the first source that named it
    for rec in records:
        outcome = rec.get("outcome")
        if outcome in _NO_PAIR_OUTCOMES:
            continue
        if outcome not in (TIER_1, TIER_2):
            return False, ("cross-repo unit `%s`: goal %s has a landing decision of `%s`, which says nothing about "
                           "whether another repository has to land alongside" % (unit, rec.get("goal"), outcome))
        named = rec.get("repos")
        if not isinstance(named, dict):
            return False, ("cross-repo unit `%s`: goal %s has a landing decision that names no repositories"
                           % (unit, rec.get("goal")))
        for repo in named:
            if isinstance(repo, str) and not sync.same_repo(repo, here):
                repos.setdefault(repo.lower(), repo)
    try:
        features_dir = feature_registry.registry_dir(sdlc_dir)
        entry = feature_registry.normalise_entry(feature_registry.read(features_dir).get(unit))
    except Exception as exc:                    # noqa: BLE001 - an unreadable registry is not "no siblings"
        return False, ("cross-repo unit `%s`: the sibling lookup cannot answer: the feature registry could not be "
                       "read (%s)" % (unit, type(exc).__name__))
    for repo in entry["repos"]:
        if not sync.same_repo(repo, here):
            repos.setdefault(repo.lower(), repo)
    if not repos:
        return True, ""                         # not a pair: nothing to check, nothing looked up
    for repo in sorted(repos.values()):
        branch = (entry["repos"].get(sync.repo_key(entry["repos"], repo)) or {}).get("branch")
        if not branch:
            return False, ("cross-repo unit `%s`: the feature registry records no branch for %s, so the other half "
                           "cannot be identified" % (unit, repo))
        if landed is None:
            return False, ("cross-repo unit `%s`: nothing was supplied to measure whether %s has landed" % (unit, repo))
        try:
            verdict = landed(repo, branch)
        except Exception as exc:                # noqa: BLE001 - an unreadable answer is not "landed"
            return False, ("cross-repo unit `%s`: whether %s `%s` has landed could not be read (%s)"
                           % (unit, repo, branch, type(exc).__name__))
        if verdict is not True:
            if verdict is False:
                return False, ("cross-repo unit `%s`: the other half in %s `%s` has not landed -- landing one half "
                               "of a pair alone is a human decision" % (unit, repo, branch))
            return False, ("cross-repo unit `%s`: whether the other half in %s `%s` has landed could not be "
                           "determined (no clear answer)" % (unit, repo, branch))
    return True, ""


def _record(sdlc_dir, decision):
    """Persist, best-effort. A record that cannot be written costs the merge gate its shortcut --
    it reads `None` and refuses -- and must not cost the pick its goal."""
    try:
        path = decision_path(sdlc_dir, decision["goal"])
        state.refuse_symlinks(sdlc_dir, path, create_parents=True)     # #708
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(decision, indent=2, sort_keys=True), encoding="utf-8")
    except Exception as exc:              # noqa: BLE001 - never break a pick over a record
        _note("sigma: cross-repo: the landing decision for %s could not be recorded (%s); the "
              "merge gate will have to ask again.\n" % (decision.get("goal"), exc))


# --------------------------------------------------------------------------- the ledger raise


def _raise_to(sdlc_dir, config, goal, to, why):
    """One ledger line, addressed. `kind="note"`, deliberately NOT `handoff`.

    `handoff` is the one kind `backlog_check._ledger_signals` reads as a real block, so raising the
    unavailable half as a hand-off would park the very goal tier 2 has just told to carry on --
    inverting the property the fallback exists to provide ("nothing is lost when access is
    missing", not "everything stops"). `to` reaches the owner through `addressed_to()` regardless of
    kind, and with autowatch it reaches them without anyone needing write access to a board they do
    not own. No new transport, exactly as the issue requires."""
    ledger.safe_append(sdlc_dir, "note", goal, config=config, to=to or None, why=why)


def _raise_unavailable(sdlc_dir, config, decision):
    """Raise every half this decision could not reach, and record who it reached.

    THE TWO OUTCOMES RAISE DIFFERENT THINGS, and mixing them was a real defect caught in review. A
    flagged decision can also carry denied repos (one repo denied, another unmeasured), and raising
    the per-repo note for those would tell their owners the unit "lands contract-first" -- naming a
    strategy that was NOT selected, because nothing was. So the per-repo raise belongs to tier 2
    alone, and `flagged` raises exactly one thing: that the unit could not be tiered, to the person
    who owns it end to end. The full per-repo detail is in the record either way.

    An entry with no resolvable owner is still WRITTEN, and the decision's own `unaddressed` list is
    what reports it. An earlier version of this docstring pointed at `sigma-doctor`'s
    unaddressable-hand-off check (#1456) as the safety net; that was wrong and is corrected here
    rather than quietly dropped. That check asks whether a CODEOWNERS/`ledger.owners` roster exists
    at all -- it never inspects a feature-registry entry's `owner`, which is the source resolved
    below. A repo with a CODEOWNERS file and a registry entry with no `owner` reports green while
    the note reaches nobody, so `unaddressed` in the record is the only signal there is."""
    entry_owner = decision.get("owner")
    for repo in (decision["denied"] if decision["outcome"] == TIER_2 else ()):
        to = decision["repos"][repo].get("owner") or entry_owner
        if not to:
            decision["unaddressed"].append(repo)
        _raise_to(sdlc_dir, config, decision["goal"], to,
                  "cross-repo unit %s cannot land in %s (%s) from this account, so it lands "
                  "contract-first -- that half needs somebody with access"
                  % (decision.get("unit"), repo, decision["repos"][repo].get("reason") or "-"))
        decision["raised"].append({"repo": repo, "to": to})
    if decision["outcome"] == FLAGGED and decision["cross_repo"]:
        if not entry_owner:
            decision["unaddressed"].append("*")
        _raise_to(sdlc_dir, config, decision["goal"], entry_owner,
                  "cross-repo unit %s could not be tiered: %s" % (decision.get("unit"),
                                                                  decision["why"]))
        decision["raised"].append({"repo": None, "to": entry_owner})


# --------------------------------------------------------------------------- the pick-time entry


def _work_runner(run):
    """Adapt this module's `(rc, out, err)` runner to `work.py`'s `run(cwd, argv)` -> stdout shape.

    An adapter, not a second runner: `work._declared_unit` is the function being reused and it calls
    its injected runner the way the rest of `work.py` does. Raising on a non-zero exit is exactly
    what that runner's contract is, and is what `_declared_unit`'s own fail-open `except` reads as
    "the read failed" rather than "the issue declares nothing" -- the distinction B1 turns on."""
    def runner(cwd, argv):
        args = [str(a) for a in argv]
        if args and args[0] == "gh":
            args = args[1:]
        code, out, err = run(args)
        if code != 0:
            raise RuntimeError((err or out or "gh exited %s" % code).strip())
        return (out or "").strip()
    return runner


def unit_of(sdlc_dir, config, goal, run):
    """The unit this goal declares -> `(unit, error)`, exactly one of which is set.

    PUBLIC BECAUSE IT HAS A SECOND CALLER, and that caller is the point (#1567). `loop._unit_at_pick`
    reaches for this when the pick-time GraphQL read of the same declaration failed, precisely so
    the answer the GATES get and the answer the BASE gets come out of one reader rather than two.
    Reaching past this into `work._declared_unit` would have rebuilt the runner adapter and the
    `AmbiguousUnit` handling below at a third call site -- the duplication the next paragraph is
    about, one layer up.

    THIS DELEGATES TO `work._declared_unit`, AND THE DELEGATION IS THE POINT -- it is not a
    convenience. An earlier version of this function read the BODY marker alone, via
    `features.parse_body`, on the reasoning that no backlog source exposed an issue's labels through
    a shared accessor. That was true when it was written and stopped being true when #1467 merged:
    `work.py` now fetches the raw REST issue and hands it to `features.read`, the DUAL read, which
    treats a `feature:<name>` LABEL as a first-class declaration.

    The consequence of the two disagreeing was not a missed diagnostic, it was a fail-open on the
    one property this module exists to add. For an issue declaring its unit by label alone,
    `work.start()` cut the worktree from `feature/<unit>` while this module recorded `no-unit`, made
    zero `gh` calls and never flagged -- and `no-unit` is a positive statement that reads as
    "nothing to see here", not as ignorance. Two readers of one declaration is two answers; there is
    now one reader, so the base a goal is cut from and the tier it is checked under cannot diverge
    by construction, whatever `features.read` grows to accept next.

    IT ALSO INHERITS THE RIGHT THREE-WAY DEGRADATION, which this module needs and would otherwise
    have had to re-derive: a goal that cannot carry a declaration at all (local mode, a non-numeric
    stem) declares nothing and is RESOLVED; a read that failed is NOT resolved, and "the read
    failed" is never collapsed into "declares nothing"; and an issue contradicting itself raises
    `AmbiguousUnit`. The middle case is the one this module cares most about, and it maps onto
    exactly the ignorance `unknown` exists for."""
    try:
        unit, note, resolved = work._declared_unit(
            config, goal, _work_runner(run or _run_gh), work.project_root(sdlc_dir))
    except features.AmbiguousUnit as exc:
        # An issue that contradicts itself has no honest unit. `features.read`'s own docstring
        # requires every sweep to catch this per issue rather than letting one hand-edited body
        # take out the queue.
        return None, "the issue contradicts itself about its unit: %s" % exc
    except Exception as exc:              # noqa: BLE001 - not knowing is not "no unit"
        return None, "the issue could not be read, so its unit is unknown: %s" % exc
    if not resolved:
        return None, ("the unit declaration could not be read, so whether this goal is cross-repo "
                      "is unknown:%s" % (note or " the read failed"))
    return unit, None


def check_at_pick(sdlc_dir, goal, config, run=None, now=None):
    """THE PICK-TIME CHECK. Decide the landing tier for the goal just claimed, and record it.

    Called from `loop._next` at the moment the claim becomes durable -- before any worktree exists,
    before a branch is cut, before a PR is opened, and long before anything is merged. That ordering
    is the point of the goal: the cheapest moment to learn that a unit cannot land in one of its
    repos is the moment before anybody starts writing code for it.

    THE STEPS ARE ORDERED BY COST, AND THE CHEAPEST ONE IS THE OPT-OUT. A repo with no
    `.sdlc/features/` has not adopted the branching model and pays no issue fetch and no `gh` call
    -- it does still get a record, which is B5's fix and costs one small file: `recorded()` returning
    `None` has to mean "nothing ever ran here", and it cannot also be the normal answer for every
    project that has not adopted the model, or the consumer this interface exists to serve has no
    way to tell the two apart.

    NEVER RAISES, AND NEVER SILENTLY SUCCEEDS EITHER. Anything that goes wrong resolves to
    `FLAGGED`, which selects no tier -- not to a tier chosen by accident. The outer guard is what
    makes that promise total rather than aspirational: a malformed `config` used to raise straight
    out of `identity()`, past every `except` in here, and the module then produced NOTHING for a
    class of input it documents as flaggable."""
    try:
        return _check_at_pick(sdlc_dir, goal, config, run=run, now=now)
    except Exception as exc:              # noqa: BLE001 - "never raises" has to be total
        decision = _decision(FLAGGED, goal=goal, at=_stamp(now),
                             why="the landing check could not run: %s" % exc)
        _record(sdlc_dir, decision)
        return decision


def _already_raised(prior, decision):
    """Has an EARLIER pick of this goal already raised exactly this? (N3)

    A goal is re-picked as a matter of course -- `_auto_reclaim_stale_claims` exists to cause it --
    and the raise was unconditional, so three picks of one unreachable unit put three identical
    notes in front of its owner. The per-goal decision record is already on disk and is the natural
    watermark: same outcome over the same denied and unknown sets is the same request, and a request
    that has been made does not need making again.

    Compared on the DECISION, never on the note text: the wording carries a reason string that can
    legitimately shift between picks (`server-error` one pick, `network` the next) while the ask is
    identical. A CHANGED set is a different ask and does raise, which is the behaviour that matters
    -- a repo that has just become unreachable must reach its owner even if another already had."""
    if not isinstance(prior, dict):
        return False
    return (prior.get("outcome") == decision["outcome"]
            and prior.get("denied") == decision["denied"]
            and prior.get("unknown") == decision["unknown"])


def _check_at_pick(sdlc_dir, goal, config, run=None, now=None):
    features_dir = feature_registry.registry_dir(sdlc_dir)
    if not features_dir.is_dir():
        decision = _decision(NOT_ADOPTED, goal=goal, at=_stamp(now),
                             why="this project has no feature registry")
        _record(sdlc_dir, decision)
        return decision

    unit, error = unit_of(sdlc_dir, config, goal, run)
    if error:
        # Not knowing which unit a goal belongs to is the same class of ignorance as not knowing
        # whether a repo is reachable, and gets the same answer: flag it, do not resolve it.
        decision = _decision(FLAGGED, goal=goal, why=error, at=_stamp(now))
        _record(sdlc_dir, decision)
        return decision
    if unit is None:
        decision = _decision(NO_UNIT, goal=goal,
                             why="the issue declares no unit, so it bases as it always has",
                             at=_stamp(now))
        _record(sdlc_dir, decision)
        return decision

    entry = feature_registry.read(features_dir).get(unit)
    if entry is None:
        decision = _decision(FLAGGED, goal=goal, unit=unit, at=_stamp(now),
                             why="the registry records nothing under %r, so whether this unit "
                                 "spans repos is unknown" % unit)
        _record(sdlc_dir, decision)
        return decision

    normalised = feature_registry.normalise_entry(entry)
    repos = sorted(normalised["repos"])
    ident = identity(config, run=run) if len(repos) >= 2 else None
    accesses = [check_access(repo, ident, run=run) for repo in repos] if len(repos) >= 2 else []
    decision = decide(entry, accesses, goal=goal, unit=unit, ident=ident)
    decision["at"] = _stamp(now)
    decision["owner"] = normalised.get("owner")
    for repo in repos:
        decision["repos"][repo]["owner"] = normalised["repos"][repo].get("owner")
    if decision["outcome"] in (TIER_2, FLAGGED) and decision["cross_repo"]:
        prior = recorded(sdlc_dir, goal)
        if _already_raised(prior, decision):
            decision["raised"] = prior.get("raised") or []
            decision["unaddressed"] = prior.get("unaddressed") or []
            decision["raise_suppressed"] = True
        else:
            _raise_unavailable(sdlc_dir, config, decision)
    _record(sdlc_dir, decision)
    return decision


def _stamp(now=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now if now is not None else time.time()))


# --------------------------------------------------------------------------- CLI


USAGE = "usage: cross_repo.py check <sdlc_dir> <goal>"


def main(argv):
    """`cross_repo.py check <sdlc_dir> <goal>` -- read back what pick time decided.

    Read-only on purpose: there is no verb here that performs the check, because the check belongs
    to the pick and a second way to run it is a second answer."""
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    if len(argv) >= 4 and argv[1] == "check":
        got = recorded(argv[2], argv[3])
        print(json.dumps(got, indent=2, sort_keys=True) if got else
              "no landing decision recorded for %s" % argv[3])
        return 0
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
