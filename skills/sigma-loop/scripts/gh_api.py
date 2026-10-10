"""GitHub-GraphQL capability check + REST-backed issue/PR helper (#801 slice 1, #895 slice 2a). Stdlib only.

HONESTY FIRST. Slice 1 of #801 was DETECTION AND REPORTING ONLY. Slice 2a of #895 wires ONE op,
`read_issue`, into `sources.py`'s seven issue reads; slice 3a adds the issue WRITE ops (see WRITES);
`create_pr`/`view_pr`/`merge_pr` still have no caller (`work.py` and the other modules keep their
direct `gh issue|pr` calls). The cache/probe path is not
exercised by any shipped caller (`doctor` always passes `probe=False`; `read_issue` checks env only),
and nothing here is evidence that /sigma-loop works in a Claude Code cloud session -- that is
unmeasured. The 403 wording the probe recognises comes from issue #801's text, not from a captured
live session. See docs/cloud-sessions.md.

RUN CONTRACT (one convention, applies to every op and the probe): `run` is the `sources` convention --
a callable taking `args: list[str]` WITHOUT a leading "gh", returning stdout as `str`, and RAISING on
failure (as `sources._run_gh` raises RuntimeError carrying `.hint`). `run=None` selects `_default_run`,
a thin subprocess `gh` caller that raises the same way; tests always inject a fake.

CAPABILITY CHECK, `graphql_available(env, cache_dir, run, now, probe)`. Precedence, first hit wins:
  1. `SIGMA_GH_GRAPHQL` = off/0/false (unavailable) or on/1/true (available): the operator's polite
     lever. Any other value is ignored (noted in `reason`), never an error.
  2. `CLAUDE_CODE_REMOTE` in {true, 1}: unavailable, free, no I/O.
  3. Cache `<cache_dir>/gh-capability.json` -- read ONLY when `probe=True`; valid while the stored
     `env_key` matches and age < `CACHE_TTL_SECONDS` (900), so a stale "unavailable" self-heals.
     Corrupt/unreadable = miss. Holds no secrets (env_key is the CLAUDE_CODE_REMOTE value only).
  4. One probe, ONLY when `probe=True` and `run` is supplied (`probe=True` with no `run` raises
     ValueError; the module never shells out implicitly): ONE `api graphql` call, no retry, no loop,
     no background process. Third-shape text -> unavailable. Any OTHER failure -> available with
     reason "probe inconclusive" (fail-open: a network blip must not disable features). Written to the
     cache atomically (temp file + os.replace); any OSError is swallowed and noted in `reason`.
  5. Default: available ("no cloud signal").
Cost: O(1). Rate pools: REST `core` 5000/h vs `graphql` 5000 points/h (REST ~1 point/call, measured in
#1829).

REST OPS raise `GhApiError` (wraps the underlying failure: `str()` is the original text, `.hint` is
copied from the original when present, `.status`/`.kind` from `classify`) on transport failure or
unparseable output; NO retries; every call passes `--method` explicitly; label names are URL-quoted.
`list_issues` pages with an explicit `cap`; `view_issue` pages comments by the issue's own `comments`
count (all, none, or only the tail pages for the newest k); never `--paginate`.

CLASSIFICATION, `classify(exc) -> (status|None, kind)`: over gh's stderr (`.hint`, no argv) or else
`str(exc)`. Proxy block -> `proxy`; a parsed `HTTP nnn` decides (429 / 403+rate-limit-text ->
`rate_limit`, 403 `permission`, 401 `auth`, 404 `not_found`, 422 `invalid`, 5xx `server`, else
`other`); only with no status, TimeoutExpired or Go net wording -> `transport`. The wording is
INFERRED (gh's `gh: <msg> (HTTP n)` format, Go net errors, GitHub docs), tested with fixtures only.

REST-FIRST READS, `read_issue` (#895): REST, then at most ONE `gh issue view` fallback (the one
place this module builds an `issue view` argv) on `rate_limit`/`server`/`transport` only, and only
while `graphql_available(env)` says so. A breaker and a bounded fallback log live under
`<sdlc_dir>/state/` (both #708-vetted) when an `sdlc_dir` is passed. One fallback is not a retry:
there is still no retry loop. Bot spellings, measured once on a public bot-filed GitHub Skills exercise issue
on 2026-10-09: issue author gh "app/github-actions" vs REST "github-actions[bot]" (mapped); comment
author gh "github-actions" vs REST "github-actions[bot]" (not mapped yet).

REST-FIRST LISTS, `list_issues_gh` (#895 slice 2c): the same policy for `gh issue list` (paged REST
list, ONE `gh issue list` fallback built in `_list_fallback`, the shared `_rest_first` breaker and log).
Newest-created-first by default to match gh; only `created`/`desc` and `updated`/`desc` are accepted.

WRITES (#895 slice 3a): `comment_issue`, `create_issue`, `add_labels`, `remove_label`, `close_issue`,
`edit_issue`, `add_assignees` go REST first behind `_rest_write` and the table `WRITE_POLICY` (ONE
fallback to today's `gh issue ...` argv, never retried; primary rate limit any write, 5xx idempotent
writes only, transport/timeout never, everything else never; none in a cloud session; no read breaker).
Classification reads gh's stderr (`.hint`) and the cause chain only, never `str(exc)`. The wording is
INFERRED, not captured live. FEATURE LABELS (was KNOWN BYPASS R5): REST would mint a missing `feature:*`
label, so `create_issue` and `add_labels` raise `GhApiError(kind="refused")` before any call (layer 1)
unless the caller passes `feature_labels_exist=True` after `label_exists` (layer 2, in GitHubSource).
Non-feature labels ARE auto-created by REST (UNMEASURED, documented GitHub behaviour). `create_pr`,
`view_pr`, `merge_pr` still have no caller. `merge_pr` has no auto-merge (REST has none; not emulated).
"""
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.parse

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


gh_session = _load("gh_session")

CACHE_TTL_SECONDS = 900
CACHE_FILE = "gh-capability.json"
OVERRIDE_ENV = "SIGMA_GH_GRAPHQL"
_OFF = ("off", "0", "false")
_ON = ("on", "1", "true")
_PROBE_ARGS = ["api", "graphql", "-f", "query={viewer{login}}"]


class GhApiError(Exception):
    """A REST op failed. `str(self)` is the wrapped failure's text; `.hint` is its `.hint` or None;
    `.status` (int|None) and `.kind` come from `classify` (see module doc)."""

    def __init__(self, text, hint=None, status=None, kind="other", returncode=None):
        super().__init__(text)
        self.hint = hint
        self.status = status
        self.kind = kind
        self.returncode = returncode      # the gh exit code when one was observed (#931), else None


# ---------------------------------------------------------------- failure classification (#895)

FALLBACK_KINDS = frozenset({"rate_limit", "server", "transport"})
_STATUS_RE = re.compile(r"HTTP (\d{3})")
_RATE_RE = re.compile(r"rate.?limit|abuse", re.I)
# INFERRED wording (gh's `gh: <msg> (HTTP n)` format, Go net errors, GitHub docs), not captured live.
_TRANSPORT_RE = re.compile(r"dial tcp|timeout|timed out|deadline exceeded|connection (refused|reset)"
                           r"|no such host|^EOF$", re.I | re.M)
_STATUS_KINDS = {429: "rate_limit", 401: "auth", 404: "not_found", 422: "invalid"}


def classify(exc):
    """-> (status|None, kind). Matches `.hint` (gh's stderr, no argv) when present, else `str(exc)`
    (which embeds argv, so a repo named `timeout-svc` must not decide). Order: proxy block (never a
    fallback) -> a parsed `HTTP nnn` (a response arrived, so it decides) -> transport/rate wording.
    403 is rate-limited only by TEXT (headers are invisible to `gh api` without -i): ceiling, a
    permission 403 whose message says "rate limit" falls back once."""
    hint = getattr(exc, "hint", None)
    for t in (hint, str(exc)):
        if gh_session.proxy_session_block(t) or gh_session.graphql_unavailable(t):
            return None, "proxy"
    text = hint or str(exc)
    m = _STATUS_RE.search(text)
    if m:
        status = int(m.group(1))
        if status == 403:
            return status, ("rate_limit" if _RATE_RE.search(text) else "permission")
        if 500 <= status <= 599:
            return status, "server"
        return status, _STATUS_KINDS.get(status, "other")
    cur = exc
    while cur is not None:
        if isinstance(cur, subprocess.TimeoutExpired):
            return None, "transport"
        cur = cur.__cause__
    if _TRANSPORT_RE.search(text):
        return None, "transport"
    if _RATE_RE.search(text):
        return None, "rate_limit"
    return None, "other"


def _default_run(args):
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        hint = proc.stderr.strip() or "is `gh` installed and authenticated? run `gh auth status`"
        exc = RuntimeError("gh " + " ".join(args) + " failed: " + hint)
        exc.hint = hint
        raise exc
    return proc.stdout


# ---------------------------------------------------------------- capability check

def _result(available, reason, source):
    return {"available": available, "reason": reason, "source": source}


def _read_cache(path, env_key, now):
    try:
        data = json.loads(path.read_text())
        if data.get("env_key") == env_key and 0 <= now - float(data["at"]) < CACHE_TTL_SECONDS:
            return data
    except Exception:                                 # noqa: BLE001 - corrupt/missing cache = miss
        pass
    return None


def _write_cache(path, entry):
    """Atomic write; returns None on success or a short failure note (never raises)."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(entry))
        os.replace(tmp, path)
        return None
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        return "cache not written (%s)" % exc.__class__.__name__


def graphql_available(env=None, cache_dir=None, run=None, now=None, probe=False):
    """-> {"available": bool, "reason": str, "source": "override|env|cache|probe|default"}. See module doc."""
    env = os.environ if env is None else env
    note = ""
    override = str(env.get(OVERRIDE_ENV, "")).strip().lower()
    if override in _OFF:
        return _result(False, "%s=%s (operator override)" % (OVERRIDE_ENV, override), "override")
    if override in _ON:
        return _result(True, "%s=%s (operator override)" % (OVERRIDE_ENV, override), "override")
    if override:
        note = " (ignored unrecognised %s=%s)" % (OVERRIDE_ENV, override)
    remote = str(env.get("CLAUDE_CODE_REMOTE", "")).strip().lower()
    if remote in ("true", "1"):
        return _result(False, "CLAUDE_CODE_REMOTE set: Claude Code cloud session, GitHub GraphQL is "
                              "blocked by the cloud proxy (inferred from env, not probed)" + note, "env")
    if probe:
        if run is None:
            raise ValueError("graphql_available(probe=True) needs an injected run=; it never shells out implicitly")
        now = time.time() if now is None else now
        path = pathlib.Path(cache_dir) / CACHE_FILE if cache_dir else None
        if path is not None:
            hit = _read_cache(path, remote, now)
            if hit is not None:
                return _result(bool(hit["available"]), str(hit.get("reason", "")) + " (cached)", "cache")
        try:
            run(list(_PROBE_ARGS))
            available, reason = True, "graphql probe succeeded"
        except Exception as exc:                      # noqa: BLE001 - classify, never propagate
            if gh_session.graphql_unavailable(str(exc)):
                available, reason = False, "graphql probe: GitHub GraphQL not available from this session"
            else:
                return _result(True, "probe inconclusive (not cached): %s" % str(exc)[:120] + note, "probe")
        if path is not None:
            failed = _write_cache(path, {"env_key": remote, "available": available,
                                         "reason": reason, "at": now})
            if failed:
                reason += "; " + failed
        return _result(available, reason + note, "probe")
    return _result(True, "no cloud signal" + note, "default")


# ---------------------------------------------------------------- REST ops

def _endpoint(repo, tail):
    return "repos/%s/%s" % (repo or "{owner}/{repo}", tail)


def _call(run, args):
    try:
        return (run or _default_run)(args)
    except GhApiError:
        raise
    except Exception as exc:                          # noqa: BLE001 - wrap, keep the text + hint
        status, kind = classify(exc)
        raise GhApiError(str(exc), getattr(exc, "hint", None), status, kind,
                         getattr(exc, "returncode", None)) from exc


def _json(run, args):
    raw = _call(run, args)
    try:
        return json.loads(raw or "null")
    except (ValueError, TypeError) as exc:
        raise GhApiError("unparseable gh api output for %s: %s" % (args[1], str(raw)[:120])) from exc


def _list_fetch():
    # Lazy on purpose: loading `sources` reconfigures stdout/stderr and pulls a dozen siblings.
    return _load("sources").fetch_issues_rest


def list_issues(run=None, repo=None, labels=(), cap=200, state="open", fetch=None):
    """Open issues via the injected `fetch` (default: sources.fetch_issues_rest, loaded lazily)."""
    fetch = fetch or _list_fetch()
    try:
        return fetch(run or _default_run, repo, list(labels), cap, state=state)
    except GhApiError:
        raise
    except Exception as exc:                          # noqa: BLE001
        raise GhApiError(str(exc), getattr(exc, "hint", None)) from exc


def _malformed(what, number, got):
    return GhApiError("malformed REST %s for issue #%d (%s)" % (what, number, type(got).__name__))


def view_issue(run, number, repo=None, comments="all"):
    """GET the issue (raw REST JSON); its `comments` count is replaced by the comment list.

    `comments` (default "all"): "none" = 1 request, the REST count left as is; "all" = pages
    1..ceil(n/100), so `1 + ceil(n/100)` requests (1000 comments = 11; uncapped, matching gh's
    preload-all); int k = only the pages holding the newest k, ascending (at most 2 when k <= 100).
    n is the issue's own `comments` count. A non-dict issue, a non-int count or a non-list page raises
    GhApiError (kind other). Re-sorted by `created_at`: a comment posted mid-read can shift the tail by
    one, so the newest comment can be missed (ceiling)."""
    issue = _json(run, ["api", _endpoint(repo, "issues/%d" % number), "--method", "GET"])
    if not isinstance(issue, dict):
        raise _malformed("issue", number, issue)
    if comments == "none":
        return issue
    n = issue.get("comments")
    if not isinstance(n, int) or isinstance(n, bool):
        raise _malformed("comment count", number, n)
    last = -(-n // 100)
    first = 1 if comments == "all" else max(1, -(-(n - int(comments) + 1) // 100))
    got = []
    for page in range(first, last + 1):
        rows = _json(run, ["api", _endpoint(repo, "issues/%d/comments" % number), "--method", "GET",
                           "-f", "per_page=100", "-f", "page=%d" % page])
        if not isinstance(rows, list):
            raise _malformed("comments page", number, rows)
        got.extend(rows)
        if comments == "all" and len(rows) < 100:
            break
    got.sort(key=lambda c: str(c.get("created_at") or "") if isinstance(c, dict) else "")
    issue["comments"] = got if comments == "all" else got[-int(comments):]
    return issue


# ---------------------------------------------------------------- gh-shape normaliser (#895)

GH_FIELDS = ("number", "title", "state", "stateReason", "author", "closedAt", "body", "labels", "assignees", "comments")


def _check_fields(fields):
    unknown = [f for f in fields if f not in GH_FIELDS]
    if unknown:
        raise ValueError("gh_api: no REST mapping for field(s) %s" % ", ".join(unknown))


def _login(user):
    """ISSUE author only: a REST Bot `<slug>[bot]` reads as `app/<slug>`, which is what
    `gh issue view --json author` shows. Measured once on a public bot-filed GitHub Skills exercise issue
    on 2026-10-09: REST "github-actions[bot]", gh "app/github-actions"."""
    if not isinstance(user, dict):
        return None
    login = user.get("login") or ""
    if user.get("type") == "Bot" and login.endswith("[bot]"):
        return {"login": "app/" + login[:-len("[bot]")]}
    return {"login": login}


def _comment(c):
    # Comment authors are NOT bot-mapped yet (plan B3): gh shows a bare `<slug>`, REST `<slug>[bot]`
    # ("github-actions" vs "github-actions[bot]"); mapping them is a follow-up. `id` is node_id ==
    # the GraphQL id (REST node_id "IC_kwDOVCU5ZM8AAAABamNa1g" == gh id), so dedup by id survives a
    # REST/fallback switch. Both measured once on a public bot-filed GitHub Skills exercise issue, 2026-10-09.
    # `authorAssociation` is emitted only when REST sent it, so `trusted_marker_comments`' "no
    # association, cannot judge trust" refusal still fires on a payload that lacks it.
    c = c if isinstance(c, dict) else {}
    out = {"id": c.get("node_id") or str(c.get("id") or ""),
           "author": {"login": (c.get("user") or {}).get("login") or ""},
           "body": c.get("body") or "", "createdAt": c.get("created_at") or ""}
    if "author_association" in c:
        out["authorAssociation"] = c["author_association"]
    return out


def to_gh_shape(issue, fields):
    """REST issue -> the `gh issue view --json <fields>` shape, requested keys only, so callers'
    parsers stay byte-identical. Unknown field -> ValueError. A requested `body` absent from the
    payload raises GhApiError (a null body is ""): reading it as "" would let `append_to_body`
    replace a real body with only its marker."""
    _check_fields(fields)
    out = {}
    for f in fields:
        if f == "number":
            out[f] = issue.get("number")
        elif f == "title":
            out[f] = issue.get("title") or ""
        elif f == "state":
            # REST reports a merged PR as `closed`; gh says MERGED (#895 2b). Closed-unmerged stays CLOSED.
            pr = issue.get("pull_request")
            merged = isinstance(pr, dict) and pr.get("merged_at")
            out[f] = "MERGED" if merged else str(issue.get("state") or "").upper()
        elif f == "stateReason":
            out[f] = str(issue.get("state_reason") or "").upper() or None
        elif f == "author":
            out[f] = _login(issue.get("user"))
        elif f == "closedAt":
            out[f] = issue.get("closed_at")
        elif f == "body":
            if "body" not in issue:
                raise GhApiError("malformed REST issue: no body key")
            out[f] = issue["body"] or ""
        elif f == "comments":
            out[f] = [_comment(c) for c in issue.get("comments") or []]
        else:
            out[f] = issue.get(f) or []
    return out


# ---------------------------------------------------------------- read_issue: REST first (#895)

BREAKER_THRESHOLD = 3
COOLDOWN_ENV = "SIGMA_GH_BREAKER_COOLDOWN"
COOLDOWN_DEFAULT = 300
FALLBACK_LOG_CAP = 200
_BREAKER_REL = "state/gh-rest-breaker.json"
_LOG_REL = "state/gh-fallback.json"
_state_mod = None


def _state():
    global _state_mod
    if _state_mod is None:
        _state_mod = _load("state")       # lazy: only a read with an sdlc_dir needs the #708 guard
    return _state_mod


def _cooldown(env):
    try:
        v = int(str(env.get(COOLDOWN_ENV, COOLDOWN_DEFAULT)).strip())
    except ValueError:
        return COOLDOWN_DEFAULT
    return v if v >= 0 else COOLDOWN_DEFAULT


def _vet(sdlc_dir, create):
    """#708: lstat-vet both files and their `.tmp` names; returns {rel: Path} or None (with one
    stderr REFUSED note) when any component is a symlink -- persistence is then off for this call."""
    st = _state()
    try:
        for rel in (_BREAKER_REL, _LOG_REL):
            st.refuse_symlinks(sdlc_dir, rel + ".tmp", create_parents=create)
            st.refuse_symlinks(sdlc_dir, rel, create_parents=create)
    except st.UnsafeStatePath as exc:
        print("sigma: gh REST breaker/fallback log not used: %s" % exc, file=sys.stderr)
        return None
    except OSError:
        return None
    base = pathlib.Path(sdlc_dir)
    return {rel: base / rel for rel in (_BREAKER_REL, _LOG_REL)}


def _read_json(path, kind):
    try:
        data = json.loads(path.read_text())
    except Exception:                                 # noqa: BLE001 - corrupt/missing = closed/empty
        return kind()
    return data if isinstance(data, kind) else kind()


def _save(sdlc_dir, rel, data):
    paths = _vet(sdlc_dir, create=True)
    if paths is not None:
        _write_cache(paths[rel], data)                # never raises; a lost write self-heals later


def _is_open(breaker, now, cooldown):
    try:
        return int(breaker.get("consecutive") or 0) >= BREAKER_THRESHOLD and \
            0 <= now - float(breaker["opened_at"]) < cooldown
    except (KeyError, TypeError, ValueError):
        return False


def read_issue(run, number, fields, repo=None, *, gql_run=None, env=None, sdlc_dir=None, now=None,
               comment_limit=None):
    """One issue in `gh issue view --json <fields>` shape: REST first, then at most ONE
    `gh issue view` fallback, only on a FALLBACK_KINDS failure and only while
    `graphql_available(env)` says so (env/override only, never a probe). No loop, no retry.

    With `sdlc_dir`, a breaker (`state/gh-rest-breaker.json`) skips REST after BREAKER_THRESHOLD
    consecutive fallback-class failures for `SIGMA_GH_BREAKER_COOLDOWN` seconds (default 300) when
    the fallback is available; every failed probe re-opens it, any REST success resets it. A bounded
    log (`state/gh-fallback.json`, last FALLBACK_LOG_CAP entries, no error text/argv/body) records
    each fallback-class event. Without `sdlc_dir` nothing is written. See module doc."""
    fields = list(fields)
    _check_fields(fields)
    gql_run = gql_run or run

    def rest():
        return to_gh_shape(view_issue(run, number, repo, comments=(
            "none" if "comments" not in fields else comment_limit or "all")), fields)

    return _rest_first("issue_read", number, "issue #%d read" % number, "gh issue view", rest,
                       lambda rest_err: _fallback(gql_run, number, fields, repo, rest_err),
                       env=env, sdlc_dir=sdlc_dir, now=now)


def _rest_first(op, number, what, cmd, rest, fallback, *, env=None, sdlc_dir=None, now=None):
    """The shared REST-then-ONE-fallback policy of `read_issue` and `list_issues_gh`. `rest()` returns the
    result or raises GhApiError; `fallback(rest_err)` is the ONE `gh` call (rest_err is None when the
    breaker is open and REST was skipped). Only a FALLBACK_KINDS failure falls back, and only while
    `graphql_available(env)` says so; the breaker, its stderr lines and the bounded log are shared by
    every op (`op` and `number` only label the log entry; `what`/`cmd` word the stderr lines)."""
    env = os.environ if env is None else env
    now = time.time() if now is None else now
    cooldown = _cooldown(env)
    paths = _vet(sdlc_dir, create=False) if sdlc_dir else None
    breaker = _read_json(paths[_BREAKER_REL], dict) if paths else {}
    was_open = _is_open(breaker, now, cooldown)
    fallback_ok = None

    def log(kind, status, fell_back, why):
        if paths is None:
            return
        entries = _read_json(paths[_LOG_REL], list)
        entries.append({"ts": now, "op": op, "number": number, "kind": kind,
                        "status": status, "fell_back": fell_back, "why": why})
        _save(sdlc_dir, _LOG_REL, entries[-FALLBACK_LOG_CAP:])

    if was_open:
        fallback_ok = graphql_available(env)["available"]
        if fallback_ok:
            log(breaker.get("last_kind"), None, True, "breaker open")
            return fallback(None)
    try:
        shaped = rest()
    except GhApiError as exc:
        if exc.kind not in FALLBACK_KINDS:
            raise
        rest_err = exc
    else:
        if paths is not None and breaker.get("consecutive"):
            _save(sdlc_dir, _BREAKER_REL, {"consecutive": 0, "opened_at": None, "last_kind": None})
        return shaped
    try:
        consecutive = int(breaker.get("consecutive") or 0) + 1
    except (TypeError, ValueError):
        consecutive = 1
    opened_at = now if consecutive >= BREAKER_THRESHOLD else breaker.get("opened_at")
    if paths is not None:
        _save(sdlc_dir, _BREAKER_REL, {"consecutive": consecutive, "opened_at": opened_at,
                                       "last_kind": rest_err.kind})
    if fallback_ok is None:
        fallback_ok = graphql_available(env)["available"]
    newly_open = paths is not None and consecutive >= BREAKER_THRESHOLD and not was_open
    head = "sigma: gh REST %s failed (%s, HTTP %s); " % (what, rest_err.kind, rest_err.status)
    if newly_open:
        print(head + "breaker open for %ds: REST failing%s" % (
            cooldown, ", reads use %s" % cmd if fallback_ok else " (no fallback in this session)"),
            file=sys.stderr)
    elif fallback_ok:
        print(head + "fell back to %s once" % cmd, file=sys.stderr)
    if not fallback_ok:
        log(rest_err.kind, rest_err.status, False, "fallback unavailable")
        raise rest_err
    log(rest_err.kind, rest_err.status, True, "rest failed")
    return fallback(rest_err)


def _fallback(gql_run, number, fields, repo, rest_err):
    """ONE `gh issue view`. Malformed JSON degrades to {} exactly as today's parsers did, then the
    result is filtered to `fields` (so a missing key stays missing)."""
    repo_args = ["--repo", repo] if repo else []
    try:
        raw = _call(gql_run, ["issue", "view", str(number), *repo_args, "--json", ",".join(fields)])
    except GhApiError as fb:
        if rest_err is None:
            raise
        raise GhApiError("gh REST read of issue #%d failed (%s); fallback gh issue view also failed: %s"
                         % (number, rest_err, fb.hint or fb), fb.hint or rest_err.hint, rest_err.status,
                         rest_err.kind) from fb
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {k: data[k] for k in fields if k in data}


LIST_ORDERS = {("created", "desc"), ("updated", "desc")}      # the only orders the fallback argv can express


def list_issues_gh(run, fields, *, repo=None, labels=(), state="open", cap=200, sort="created",
                   direction="desc", gql_run=None, fetch=None, env=None, sdlc_dir=None, now=None):
    """#895 slice 2c: issues in `gh issue list --json <fields>` shape, REST first (paged
    `gh api repos/{o}/{r}/issues` GET through `fetch`, default `sources.fetch_issues_rest`), then at
    most ONE `gh issue list` fallback on a FALLBACK_KINDS failure while `graphql_available(env)` says
    so (the one place this module builds an `issue list` argv). Breaker and log are `_rest_first`'s,
    shared with `read_issue`, and exist only when `sdlc_dir` is given.

    ORDER: `gh issue list` is newest-created-first, so the default is `created`/`desc` (NOT
    `fetch_issues_rest`'s asc): a board over `cap` must return its NEWEST rows. Only `created`/`desc`
    and `updated`/`desc` are accepted (the fallback is `--search sort:updated-desc` for the latter);
    anything else raises ValueError before any call rather than fall back in a different order.
    `comments` is refused: the REST list carries a count, not the comments.

    Every REST page must be a non-empty JSON list; empty output, malformed JSON or a non-list page is
    GhApiError(kind "other"): never a fallback and never an empty result, so a failure cannot read as
    an empty board. All failures raise GhApiError (an Exception)."""
    fields = list(fields)
    _check_fields(fields)
    if "comments" in fields:
        raise ValueError("gh_api.list_issues_gh: the REST list has no comment bodies; use read_issue")
    if (sort, direction) not in LIST_ORDERS:
        raise ValueError("gh_api.list_issues_gh: order %s/%s cannot be expressed by the gh fallback; allowed: %s"
                         % (sort, direction, ", ".join("%s/%s" % o for o in sorted(LIST_ORDERS))))
    labels = list(labels)
    gql_run = gql_run or run
    fetch = fetch or _list_fetch()

    def checked(args):
        raw = _call(run, args)
        try:
            page = json.loads(raw) if str(raw or "").strip() else None
        except (ValueError, TypeError) as exc:
            raise GhApiError("unparseable gh api output for %s: %s" % (args[1], str(raw)[:120])) from exc
        if not isinstance(page, list):
            raise GhApiError("gh api %s returned %s, not a JSON list" % (args[1], type(page).__name__))
        return raw

    def rest():
        try:
            items = fetch(checked, repo, labels, cap, state=state, sort=sort, direction=direction)
        except GhApiError:
            raise
        except Exception as exc:                      # noqa: BLE001 - classify like every REST failure
            status, kind = classify(exc)
            raise GhApiError(str(exc), getattr(exc, "hint", None), status, kind) from exc
        return [to_gh_shape(i, fields) for i in items]

    return _rest_first("issue_list", None, "issue list", "gh issue list", rest,
                       lambda rest_err: _list_fallback(gql_run, fields, repo, labels, state, cap, sort, rest_err),
                       env=env, sdlc_dir=sdlc_dir, now=now)


def _list_fallback(gql_run, fields, repo, labels, state, cap, sort, rest_err):
    """ONE `gh issue list`. Unlike `_fallback` (a view degrades to {} like the old parsers), a list that
    is not a JSON list is an error: an empty board must never be invented from garbage."""
    argv = ["issue", "list", *(["--repo", repo] if repo else [])]
    for name in labels:
        argv += ["--label", name]
    argv += ["--state", state, "--json", ",".join(fields), "--limit", str(cap)]
    if sort == "updated":
        argv += ["--search", "sort:updated-desc"]
    try:
        raw = _call(gql_run, argv)
    except GhApiError as fb:
        if rest_err is None:
            raise
        raise GhApiError("gh REST issue list failed (%s); fallback gh issue list also failed: %s"
                         % (rest_err, fb.hint or fb), fb.hint or rest_err.hint, rest_err.status,
                         rest_err.kind) from fb
    try:
        data = json.loads(raw) if str(raw or "").strip() else None
    except ValueError:
        data = None
    if not isinstance(data, list):
        raise GhApiError("gh issue list returned no JSON list")
    return [{k: it[k] for k in fields if k in it} if isinstance(it, dict) else it for it in data]


# ---------------------------------------------------------------- WRITE policy (#895 slice 3a)

# The single source of truth for WRITE fallback: the code reads it, the tests iterate it, and
# docs/cloud-sessions.md must name every case id. Columns: case id, description, may an IDEMPOTENT
# write fall back to ONE `gh issue ...` call, may a NON-idempotent write (comment, create). Idempotent
# ops: add_labels, remove_label, close_issue, edit_issue(body=), add_assignees.
# The wording that selects a case is INFERRED (no live capture), see classify().
WRITE_POLICY = (
    ("primary_rate_limit", "HTTP 429, or 403 whose gh stderr says rate limit and NOT secondary/abuse "
                           "(rejected before the write executes: documented GitHub behaviour, UNMEASURED here)",
     True, True),
    ("secondary_rate_limit", "403 or 429 whose gh stderr says secondary or abuse", False, False),
    ("server_5xx", "HTTP 500-599: the write may have committed, so idempotent ops only", True, False),
    ("transport_ambiguous", "timeout / connection failure after the request was sent: NEVER", False, False),
    ("auth_401", "HTTP 401", False, False),
    ("not_found_404", "HTTP 404", False, False),
    ("invalid_422", "HTTP 422 (also a dropped assignee, a team slug)", False, False),
    ("permission_403", "HTTP 403 that is not a rate limit", False, False),
    ("proxy_block", "cloud proxy / GraphQL-unavailable text", False, False),
    ("refused", "the feature-label guard (GhApiError kind=refused); not a classify kind", False, False),
    ("other_unparsed", "anything else, including a malformed 2xx body and a failure with no gh stderr",
     False, False),
)
_WRITE_FLAGS = {row[0]: (row[2], row[3]) for row in WRITE_POLICY}
# classify() kind -> case id (rate_limit is refined to primary/secondary by _write_case).
WRITE_CASE_FOR_KIND = {
    "rate_limit": "primary_rate_limit", "server": "server_5xx", "transport": "transport_ambiguous",
    "auth": "auth_401", "not_found": "not_found_404", "invalid": "invalid_422",
    "permission": "permission_403", "proxy": "proxy_block", "other": "other_unparsed",
    "refused": "refused",
}
_SECONDARY_RE = re.compile(r"secondary|abuse", re.I)


def _is_secondary(text):
    return bool(_SECONDARY_RE.search(text or ""))


def _hint_of(exc):
    h = getattr(exc, "hint", None)
    return h if isinstance(h, str) else ""


def _write_status(exc):
    """The HTTP status parsed from gh's stderr (`.hint`) ONLY, or None. `str(exc)` embeds argv and so
    the write BODY, and `exc.status`/`exc.kind` were derived from it when there was no hint: a comment
    saying `HTTP 502` must never read as a server error, so neither is consulted."""
    m = _STATUS_RE.search(_hint_of(exc))
    return int(m.group(1)) if m else None


def _write_case(exc):
    """-> a WRITE_POLICY case id, from the `.hint` (gh stderr, no argv) and the `__cause__` chain only."""
    if getattr(exc, "kind", None) == "refused":
        return "refused"
    hint = _hint_of(exc)
    if hint and (gh_session.proxy_session_block(hint) or gh_session.graphql_unavailable(hint)):
        return "proxy_block"
    status = _write_status(exc)
    if status is not None:
        if status == 429 or (status == 403 and _RATE_RE.search(hint)):
            return "secondary_rate_limit" if _is_secondary(hint) else "primary_rate_limit"
        if status == 403:
            return "permission_403"
        if 500 <= status <= 599:
            return "server_5xx"
        return {401: "auth_401", 404: "not_found_404", 422: "invalid_422"}.get(status, "other_unparsed")
    cur = exc
    while cur is not None:
        if isinstance(cur, subprocess.TimeoutExpired):
            return "transport_ambiguous"
        cur = cur.__cause__
    if hint and _TRANSPORT_RE.search(hint):
        return "transport_ambiguous"
    return "other_unparsed"


def _write_fallback_ok(exc, idempotent):
    """May this REST write failure fall back to the ONE `gh issue ...` call? Derived from WRITE_POLICY."""
    return _WRITE_FLAGS[_write_case(exc)][0 if idempotent else 1]


def _rest_write(op, number, what, cmd, rest, fallback, *, idempotent, env=None, sdlc_dir=None, now=None):
    """The WRITE policy (#895 slice 3a), deliberately NOT `_rest_first`: `rest()` returns the result or
    raises GhApiError; on a failure `WRITE_POLICY` decides (by `_write_case`, i.e. gh's stderr and the
    cause chain only, never `str(exc)` which embeds the write body) whether ONE `fallback()` call may
    run, and only while `graphql_available(env)` says so. The fallback is never retried and its own
    exception propagates untouched. No breaker: this never opens, closes or reads
    `state/gh-rest-breaker.json`. With an `sdlc_dir` each fallback (or one the session forbade) is
    logged to the SAME bounded `state/gh-fallback.json` the reads use (cap FALLBACK_LOG_CAP, shared with
    them, so a write burst can push read entries out); an entry carries op/number/case/status only,
    never text, argv or body."""
    try:
        return rest()
    except GhApiError as exc:
        case = _write_case(exc)
        if not _WRITE_FLAGS[case][0 if idempotent else 1]:
            raise
        env = os.environ if env is None else env
        ok = graphql_available(env)["available"]
        _log_write(sdlc_dir, op, number, case, _write_status(exc), ok, "rest failed" if ok else "fallback unavailable",
                   time.time() if now is None else now)
        if not ok:
            raise
        print("sigma: gh REST %s failed (%s, HTTP %s); fell back to %s once" % (what, case, _write_status(exc), cmd),
              file=sys.stderr)
        return fallback()


def _log_write(sdlc_dir, op, number, case, status, fell_back, why, now):
    if not sdlc_dir:
        return
    paths = _vet(sdlc_dir, create=False)
    if paths is None:
        return
    entries = _read_json(paths[_LOG_REL], list)
    entries.append({"ts": now, "op": op, "number": number, "kind": case,
                    "status": status, "fell_back": fell_back, "why": why})
    _save(sdlc_dir, _LOG_REL, entries[-FALLBACK_LOG_CAP:])


_fl_mod = None


def _feature_labels():
    global _fl_mod
    if _fl_mod is None:
        _fl_mod = _load("feature_labels")             # lazy: only a label-carrying write needs it
    return _fl_mod


def _refuse_feature_labels(labels, feature_labels_exist):
    """Layer 1 of the feature-label refusal (#1468, #895 slice 3a): REST would mint a missing `feature:*`
    label on `POST /issues` or `POST /labels`, where `gh` failed. Raised BEFORE any call. A caller that
    has verified the label exists (GitHubSource, via `label_exists`) passes `feature_labels_exist=True`."""
    if feature_labels_exist:
        return
    fl = _feature_labels()
    for name in labels:
        if fl.is_feature_label(name):
            raise GhApiError("sigma: refusing to create the label %r — Sigma attaches an existing feature "
                             "label but never creates one; a human creates the first label of a unit "
                             "(see docs/label-model.md §5)" % str(name), kind="refused")


def _repo_flag(repo):
    return ["--repo", repo] if repo else []


def _write(op, number, cmd, rest, fb_argv, idempotent, fallback_run, run, env, sdlc_dir, now):
    """Common wiring of one write op: `rest()` then (per WRITE_POLICY) ONE `gh issue ...` through
    `fallback_run` (default: the same `run`)."""
    verb = op.replace("issue_", "").replace("_", " ")
    return _rest_write(op, number, "issue %s%s" % ("#%s " % number if number is not None else "", verb),
                       cmd, rest, lambda: _call(fallback_run or run, fb_argv),
                       idempotent=idempotent, env=env, sdlc_dir=sdlc_dir, now=now)


def comment_issue(run, number, body, repo=None, *, fallback_run=None, env=None, sdlc_dir=None, now=None):
    """POST a comment. NOT idempotent: no fallback on a 5xx or a transport failure (it may have landed)."""
    return _write("issue_comment", number, "gh issue comment",
                  lambda: _json(run, ["api", _endpoint(repo, "issues/%d/comments" % number), "--method", "POST",
                                      "-f", "body=%s" % body]),
                  ["issue", "comment", str(number), *_repo_flag(repo), "--body", body], False,
                  fallback_run, run, env, sdlc_dir, now)


def add_labels(run, number, labels, repo=None, *, feature_labels_exist=False, fallback_run=None, env=None,
               sdlc_dir=None, now=None):
    labels = list(labels)
    _refuse_feature_labels(labels, feature_labels_exist)
    args = ["api", _endpoint(repo, "issues/%d/labels" % number), "--method", "POST"]
    for name in labels:
        args += ["-f", "labels[]=%s" % name]
    fb = ["issue", "edit", str(number), *_repo_flag(repo)]
    for name in labels:
        fb += ["--add-label", name]
    return _write("issue_add_labels", number, "gh issue edit", lambda: _json(run, args), fb, True,
                  fallback_run, run, env, sdlc_dir, now)


def _hint_404(exc):
    return _write_status(exc) == 404


def label_exists(run, name, repo=None):
    """GET one label: True; a structured 404 (gh's stderr, never argv/body) -> False; anything else raises
    GhApiError (the feature-label check fails closed on it). A read: no fallback, no log."""
    try:
        _json(run, ["api", _endpoint(repo, "labels/%s" % urllib.parse.quote(name, safe="")), "--method", "GET"])
    except GhApiError as exc:
        if _hint_404(exc):
            return False
        raise
    return True


def remove_label(run, number, label, repo=None, *, fallback_run=None, env=None, sdlc_dir=None, now=None):
    """DELETE one label. A 404 is a no-op, matched on the STRUCTURED status (gh's stderr), never on
    `"404" in str(exc)` (argv/body would match). KNOWN BLIND SPOT, documented rather than fixed: exact
    GitHub 404 bodies are UNMEASURED, so "label not on the issue" and "issue not found" are both a no-op
    here; a caller that must notice a vanished issue relies on its next write failing. Everything else
    raises."""
    args = ["api", _endpoint(repo, "issues/%d/labels/%s" % (number, urllib.parse.quote(label, safe=""))),
            "--method", "DELETE"]
    try:
        return _write("issue_remove_label", number, "gh issue edit", lambda: _json(run, args),
                      ["issue", "edit", str(number), *_repo_flag(repo), "--remove-label", label], True,
                      fallback_run, run, env, sdlc_dir, now)
    except GhApiError as exc:
        if _hint_404(exc):
            return None
        raise


def create_issue(run, title, body, labels=(), repo=None, *, feature_labels_exist=False, fallback_run=None,
                 env=None, sdlc_dir=None, now=None):
    """REST create. NOT idempotent: no fallback on a 5xx or a transport failure. Refuses `feature:*`
    labels before any call (layer 1) unless the caller verified they exist. The fallback result is the
    URL `gh issue create` prints; the REST result is the issue dict."""
    labels = list(labels)
    _refuse_feature_labels(labels, feature_labels_exist)
    args = ["api", _endpoint(repo, "issues"), "--method", "POST", "-f", "title=%s" % title,
            "-f", "body=%s" % body]
    fb = ["issue", "create", *_repo_flag(repo), "--title", title, "--body", body]
    for name in labels:
        args += ["-f", "labels[]=%s" % name]
        fb += ["--label", name]
    return _write("issue_create", None, "gh issue create", lambda: _json(run, args), fb, False,
                  fallback_run, run, env, sdlc_dir, now)


def close_issue(run, number, repo=None, reason=None, *, fallback_run=None, env=None, sdlc_dir=None, now=None):
    args = ["api", _endpoint(repo, "issues/%d" % number), "--method", "PATCH", "-f", "state=closed"]
    fb = ["issue", "close", str(number), *_repo_flag(repo)]
    if reason:
        args += ["-f", "state_reason=%s" % reason]
        fb += ["--reason", reason.replace("_", " ")]
    return _write("issue_close", number, "gh issue close", lambda: _json(run, args), fb, True,
                  fallback_run, run, env, sdlc_dir, now)


def edit_issue(run, number, body=None, repo=None, *, fallback_run=None, env=None, sdlc_dir=None, now=None):
    """PATCH the body (idempotent). `-f` (raw field), NEVER `-F`: a body starting with `@` or looking like
    key=value must stay literal text."""
    if body is None:
        raise ValueError("gh_api.edit_issue: body= is required (the only editable field)")
    return _write("issue_edit", number, "gh issue edit",
                  lambda: _json(run, ["api", _endpoint(repo, "issues/%d" % number), "--method", "PATCH",
                                      "-f", "body=%s" % body]),
                  ["issue", "edit", str(number), *_repo_flag(repo), "--body", body], True,
                  fallback_run, run, env, sdlc_dir, now)


def add_assignees(run, number, assignees, repo=None, *, fallback_run=None, env=None, sdlc_dir=None, now=None):
    """POST assignees (idempotent). Three REST-vs-gh differences are handled here: `@me` is client-side
    in gh and literal in REST, so it is resolved with ONE `GET user` (if that fails nothing is sent and
    the lookup's own error is raised; a 403 there is `permission`, which never falls back); a team slug
    (`org/team`) is refused before any call; and REST silently DROPS an unassignable user, so the response
    `assignees[].login` must contain every requested login (case-insensitive) or this raises
    `kind="invalid"`. The fallback `gh issue edit --add-assignee` receives the ORIGINAL spellings
    (gh resolves `@me` itself)."""
    originals = [str(a) for a in assignees]
    for a in originals:
        if "/" in a:
            raise GhApiError("gh_api.add_assignees: %r looks like a team; issues can only be assigned to users" % a,
                             kind="invalid")

    def rest():
        logins = []
        for a in originals:
            name = a[1:] if a.startswith("@") else a
            if name.lower() == "me":
                me = _json(run, ["api", "user", "--method", "GET"])
                name = me.get("login") if isinstance(me, dict) else None
                if not name:
                    raise GhApiError("gh api user returned no login")
            logins.append(name)
        args = ["api", _endpoint(repo, "issues/%d/assignees" % number), "--method", "POST"]
        for name in logins:
            args += ["-f", "assignees[]=%s" % name]
        resp = _json(run, args)
        got = {str(x.get("login", "")).lower() for x in (resp.get("assignees") or []) if isinstance(x, dict)} \
            if isinstance(resp, dict) else set()
        missing = [n for n in logins if n.lower() not in got]
        if missing:
            raise GhApiError("GitHub did not assign %s to #%d (not assignable, or not a collaborator)"
                             % (", ".join("@" + m for m in missing), number), kind="invalid")
        return resp

    fb = ["issue", "edit", str(number), *_repo_flag(repo)]
    for a in originals:
        fb += ["--add-assignee", a]
    return _write("issue_add_assignees", number, "gh issue edit", rest, fb, True, fallback_run, run, env,
                  sdlc_dir, now)


def create_pr(run, title, body, head, base, repo=None):
    return _json(run, ["api", _endpoint(repo, "pulls"), "--method", "POST", "-f", "title=%s" % title,
                       "-f", "body=%s" % body, "-f", "head=%s" % head, "-f", "base=%s" % base])


def view_pr(run, number, repo=None):
    return _json(run, ["api", _endpoint(repo, "pulls/%d" % number), "--method", "GET"])


def merge_pr(run, number, merge_method="squash", sha=None, repo=None):
    """PUT /pulls/{n}/merge. No auto-merge exists in REST; not emulated. `sha` guards against races."""
    args = ["api", _endpoint(repo, "pulls/%d/merge" % number), "--method", "PUT",
            "-f", "merge_method=%s" % merge_method]
    if sha:
        args += ["-f", "sha=%s" % sha]
    return _json(run, args)


# ---------------------------------------------------------------- landing plumbing (#931, upkeep part C slice 2)
# Additive: no caller yet (the landing engine of a later slice is the first). Nothing above changes, so with the
# upkeep gate closed every existing path is byte-identical. These helpers read no configuration. Every one takes an
# EXPLICIT repository (the placeholder `{owner}/{repo}` resolves against whatever directory the process runs in) and
# refuses a bad argument BEFORE any call. REST only, no GraphQL fallback. The endpoint shapes are unverified live.
# Rejected (D-20): the CLI merge with a head-match flag; it resolves the repository from the working directory, exits 0
# on an enqueue and cannot return the reply `sha`.

_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_MERGE_METHODS = ("merge", "squash", "rebase")


def _refuse(what):
    return GhApiError("gh_api: " + what, kind="invalid")


def _need_repo(repo):
    if not isinstance(repo, str) or not _REPO_RE.match(repo):
        raise _refuse("an explicit owner/name repository is required")
    return repo


def _need_sha(sha, what="sha"):
    if not isinstance(sha, str) or not _SHA_RE.match(sha):
        raise _refuse("%s must be a full 40-character lowercase hex sha" % what)
    return sha


def _need_number(number):
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise _refuse("a positive integer pull request number is required")
    return number


def bounded_runner(cwd=None, timeout=120, *, popen=None):
    """-> a `run(args)` callable for the ops above that KEEPS THE FAILURE CLASS. `_default_run` has no working
    directory and drops the exit code; this binds `cwd` and a wall-clock `timeout`, and every failure is raised as a
    `GhApiError` already carrying `.status`, `.kind` (via `classify`, from gh's stderr) and `.returncode`: a non-zero
    exit keeps its code, a timeout is kind `transport`, a missing binary is kind `other` (never retried).
    `popen` (default `subprocess.run`) is a test seam."""
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("gh_api.bounded_runner: timeout must be a positive number of seconds")
    popen = popen or subprocess.run

    def run(args):
        try:
            proc = popen(["gh", *args], capture_output=True, text=True, timeout=timeout, cwd=cwd)
        except subprocess.TimeoutExpired as exc:
            raise GhApiError("gh timed out after %ss" % timeout, "gh timed out after %ss" % timeout,
                             None, "transport") from exc
        except OSError as exc:
            hint = "could not run gh (%s); is it installed?" % exc.__class__.__name__
            raise GhApiError(hint, hint, None, "other") from exc
        if proc.returncode != 0:
            hint = (proc.stderr or "").strip() or "gh exited %s with no message" % proc.returncode
            err = RuntimeError("gh failed: " + hint)
            err.hint = hint
            status, kind = classify(err)
            raise GhApiError(str(err), hint, status, kind, proc.returncode)
        return proc.stdout

    return run


def merge_pr_pinned(run, repo, number, sha, *, merge_method):
    """PUT /pulls/{n}/merge with an EXPLICIT method (keyword-only, no default: `merge_pr` defaults to squash), the
    repository required, and the head pin REQUIRED and validated as 40 lowercase hex (`merge_pr` silently drops an
    empty one). GitHub refuses the merge (409) if the head moved. No auto-merge, no admin flag, no branch-delete
    option. Returns the reply dict; read its commit with `merge_reply_sha`. A non-dict reply or a reply `sha` that is
    not 40-hex raises."""
    _need_repo(repo)
    _need_number(number)
    _need_sha(sha, "the head pin")
    if merge_method not in _MERGE_METHODS:
        raise _refuse("merge_method must be one of %s" % ", ".join(_MERGE_METHODS))
    reply = _json(run, ["api", _endpoint(repo, "pulls/%d/merge" % number), "--method", "PUT",
                        "-f", "merge_method=%s" % merge_method, "-f", "sha=%s" % sha])
    if not isinstance(reply, dict):
        raise GhApiError("malformed merge reply for #%d (%s)" % (number, type(reply).__name__))
    got = reply.get("sha")
    if got is not None and not (isinstance(got, str) and _SHA_RE.match(got)):
        raise GhApiError("malformed merge reply for #%d: sha is not a full hex sha" % number)
    return reply


def merge_reply_sha(reply):
    """The merge commit's sha from a `merge_pr_pinned` reply, or None when the reply carried none."""
    got = reply.get("sha") if isinstance(reply, dict) else None
    return got if isinstance(got, str) and _SHA_RE.match(got) else None


def commit_parents(run, repo, sha):
    """GET /commits/{sha} -> the list of parent shas (a merge commit has two). Works in a REST-only session."""
    _need_repo(repo)
    _need_sha(sha)
    data = _json(run, ["api", _endpoint(repo, "commits/%s" % sha), "--method", "GET"])
    parents = data.get("parents") if isinstance(data, dict) else None
    if not isinstance(parents, list):
        raise GhApiError("malformed commit payload for %s: no parents list" % sha[:12])
    out = []
    for p in parents:
        got = p.get("sha") if isinstance(p, dict) else None
        if not (isinstance(got, str) and _SHA_RE.match(got)):
            raise GhApiError("malformed commit payload for %s: bad parent entry" % sha[:12])
        out.append(got)
    return out


def branch_rules(run, repo, branch):
    """GET /rules/branches/{branch} -> the list of rules active on the branch (rulesets only; a queue set through
    classic branch protection is invisible, and a failed read is the caller's to treat as unknown)."""
    _need_repo(repo)
    if not isinstance(branch, str) or not branch:
        raise _refuse("a branch name is required")
    rules = _json(run, ["api", _endpoint(repo, "rules/branches/%s" % urllib.parse.quote(branch, safe="")),
                        "--method", "GET"])
    if not isinstance(rules, list):
        raise GhApiError("malformed branch rules for %s (%s)" % (branch, type(rules).__name__))
    return rules


def rules_have_merge_queue(rules):
    """True when any rule in a `branch_rules` list is a merge queue. A non-list raises."""
    if not isinstance(rules, list):
        raise _refuse("rules must be a list")
    return any(isinstance(r, dict) and r.get("type") == "merge_queue" for r in rules)


def repo_settings(run, repo):
    """GET /repos/{repo} -> the repository object (a dict)."""
    _need_repo(repo)
    data = _json(run, ["api", "repos/%s" % repo, "--method", "GET"])
    if not isinstance(data, dict):
        raise GhApiError("malformed repository payload (%s)" % type(data).__name__)
    return data


def delete_branch_on_merge(settings):
    """True/False from a `repo_settings` dict's `delete_branch_on_merge`; None when absent or not a boolean (unknown)."""
    got = settings.get("delete_branch_on_merge") if isinstance(settings, dict) else None
    return got if isinstance(got, bool) else None


def create_pr_nondraft(run, title, body, head, base, repo):
    """POST /pulls with an explicit, TYPED `draft: false` (`-F`, so a boolean, not the string). `create_pr` sends no
    `draft` field and is unchanged. The repository is required."""
    _need_repo(repo)
    return _json(run, ["api", _endpoint(repo, "pulls"), "--method", "POST", "-f", "title=%s" % title,
                       "-f", "body=%s" % body, "-f", "head=%s" % head, "-f", "base=%s" % base,
                       "-F", "draft=false"])
