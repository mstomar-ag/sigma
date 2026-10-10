#!/usr/bin/env python3
"""#2311 (slice 1 of Epic #2310, design #2289): the passive drift watcher's real logic.

WHAT "DRIFT" MEANS HERE (design `## Intent`, `### 2`). For every OPEN unit's own `feature/<name>`
branch: (1) the COMMIT DELTA -- what the integration branch did while the feature branch was away,
via `feature_rebase.landed_commits` (BR-7), args reversed from its usual rebase-upkeep direction;
(2) the PR DRIFT -- the feature branch's own landing pull request, open or closed, via
`unit_completion._landing_pull_requests` (BR-8). Both are reused DIRECTLY, never reimplemented --
the whole point of the design's own Blast-radius section is that this repo already has both reads.

READ-ONLY AGAINST THE REPOSITORY. Nothing here moves a branch, opens a pull request, writes a
label, or touches a board column -- strictly informational, matching `## Blockers`: "the passive
tick is read-only... and defaults OFF."

THE GATE COMPOSES; IT IS NOT A FLAT FLAG (design `### 4`, mirroring `channel_notify.enabled()`'s own
two-part check, BR-11 `:64-74`). `enabled(config)` checks `ledger.enabled(config)` FIRST -- the
dedup mechanism below is built entirely on the shared ledger (`ledger.safe_append`/`read_all`
degrade to silent no-ops whenever the ledger itself is off), so `drift_watch.enabled: true` alone,
however it gets set, can never run the tick while the ledger stays off. D-2's addendum names the
unbounded, every-tick-every-machine failure this composition exists to close.

THE DEDUP MECHANISM IS SHARED, NOT LOCAL (design `### 4`, D-2). `channel_notify.py`'s own
NOTIFY-ONCE-PER-ID cursor (BR-11) lives in `.sdlc/state/` -- per-machine, never synced (BR-12) --
which is correct for a webhook that only ever wakes ONE person's own machine, and wrong for a
summary every teammate's clone could independently decide to post to a channel the WHOLE TEAM sees.
So this reads the SHARED ledger's own union (`ledger.read_all`, BR-10) for a `note` entry carrying
this window's own `ref`, from ANY actor -- not a local cursor -- and writes the marker back with
`ledger.safe_append` ONLY after a successful post. Residual: a bounded TOCTOU race (D-2) -- however
many watchers' reads land inside the gap before any of their own writes is visible to the others
will each post once -- scaling with how many watchers race in that narrow window, never with total
team size, and a duplicate post is a harmless repeated text summary, never a repository mutation.

TWO SEPARATE STATE ARTIFACTS, ON PURPOSE. The TTL watermark (`.sdlc/state/drift.meta.json`,
BR-6's shape, mirroring `loop._reconcile_due`/`_reconcile_stamp` exactly) is stamped on EVERY due
sweep regardless of whether the post succeeded -- so a persistently unreachable Slack endpoint costs
one failed POST per window, not a retry on every 15-minute `watch_daemon.py` tick forever. The ledger
marker is written only on a confirmed post -- so a failed post leaves the window genuinely open for
whichever watcher (this one, next window, or a teammate's, sooner) manages to post it.

UNIT ENUMERATION, WITH THE UNADOPTED FALLBACK (D-4). No `.sdlc/features/` directory at all: inert,
report nothing, cost nothing -- mirrors `unit_completion.py`'s own `NOT_ADOPTED` posture. A registry
that exists (even untracked/local-only, as measured live on THIS repo, BR-9) but declares no OPEN
units: also nothing to check, not an error. Every OPEN unit's own branch is checked; a bare
single-branch-against-`work.base` fallback was considered (D-4's own alternative) and left out here
-- comparing `work.base` against itself measures nothing, so there is no second fallback shape to
build.

Module shape follows its siblings (`reconcile_tick.py`, `channel_notify.py`): zero third-party
dependencies, loaded via `_load(name)`, never raises out of its own public entry points."""
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
feature_rebase = _load("feature_rebase")
feature_registry = _load("feature_registry")
features = _load("features")
feature_sync = _load("feature_sync")
unit_completion = _load("unit_completion")
scrub = _load("scrub")
slack_client = _load("slack_client")
legacy = _load("legacy")                 # #239: `channels.<previous name>` still reads

DEFAULT_TTL_MINUTES = 90
WATERMARK_NAME = "drift.meta.json"
#: D-1's leaning: a compact one-line count-plus-newest-subject format for the commit-delta half.
NEWEST_SUBJECTS = 3
#: `feature_sync._run`'s exact `(cwd, argv) -> stdout` contract -- the same runner
#: `feature_rebase.py` itself aliases as `_run` (`sync = _load("feature_sync"); _run = sync._run`),
#: reused here rather than a fourth hand-rolled copy of the same subprocess wrapper.
_run = feature_sync._run


# --------------------------------------------------------------------------- config / gate


def settings(config):
    block = config.get("drift_watch") if isinstance(config, dict) else None
    return block if isinstance(block, dict) else {}


def enabled(config):
    """Strict `is True` on BOTH `ledger.enabled` and `drift_watch.enabled` -- see the module
    docstring for why the composition, not just the second half, is load-bearing."""
    if not ledger.enabled(config):
        return False
    return settings(config).get("enabled") is True


def ttl_minutes(config):
    try:
        return max(1, int(settings(config).get("ttl_minutes", DEFAULT_TTL_MINUTES)))
    except (TypeError, ValueError):
        return DEFAULT_TTL_MINUTES


# --------------------------------------------------------------------------- TTL watermark (BR-6)


def _watermark_path(sdlc_dir):
    return pathlib.Path(sdlc_dir) / "state" / WATERMARK_NAME


def due(sdlc_dir, config, now=None):
    """Mirrors `loop._reconcile_due` exactly: an unreadable/absent/malformed watermark reads as
    DUE -- the safe direction is one extra read-only sweep, never silence forever over a corrupted
    stamp file."""
    now = now if now is not None else time.time()
    try:
        stamp = json.loads(_watermark_path(sdlc_dir).read_text(encoding="utf-8"))
        last = float(stamp.get("swept_at") or 0)
    except Exception:
        return True
    return (now - last) >= ttl_minutes(config) * 60


def _stamp(sdlc_dir, now=None):
    """Best-effort watermark write -- a failure here costs one extra sweep next call, never a
    crash. Called unconditionally once a sweep has actually run (see `sweep`'s own docstring)."""
    now = now if now is not None else time.time()
    try:
        path = _watermark_path(sdlc_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": "drift-meta/1", "swept_at": now}), encoding="utf-8")
    except Exception:
        pass


# --------------------------------------------------------------------------- unit enumeration (D-4)


def _open_units(sdlc_dir):
    """D-4: every OPEN unit's own branch -> `[(name, branch), ...]`, sorted for determinism.

    `[]`, costing nothing, when `.sdlc/features/` does not exist at all -- mirrors
    `unit_completion.py`'s own `NOT_ADOPTED` posture. `read_index` already degrades to `{}` on a
    missing/corrupt/wrong-schema `index.json` (its own docstring), so an adopted-but-empty or
    adopted-but-unreadable registry also answers `[]` here -- nothing this tick could check, not an
    error. `read_index` is used, not the per-unit `units/*.json` files: this tick runs on a wall-
    clock cadence far coarser than any single pick, so reading the materialised chart sheet once is
    the right cost, matching `feature_propagate`'s own registry-wide reads."""
    features_dir = feature_registry.registry_dir(sdlc_dir)
    if not features_dir.is_dir():
        return []
    index = feature_registry.read_index(features_dir)
    found = []
    for name, raw in (index or {}).items():
        entry = feature_registry.normalise_entry(raw)
        if entry.get("open") is not False:
            found.append((name, features.BRANCH_PREFIX + name))
    return sorted(found)


# --------------------------------------------------------------------------- the two drift signals


def _fetch(run, cwd, remote, refs):
    """Best-effort `git fetch <remote> <base> <branch1> <branch2> ...` -- ONE network round trip
    regardless of unit count (a real scalability property, not an assumption: git accepts any
    number of refspecs in one `fetch` call), so the cost of this step does not grow with how many
    units are being watched. Non-fatal: a fetch failure degrades to whatever remote-tracking refs
    are already locally known, matching `work._behind_count`'s own fetch-before-compare discipline
    without inheriting its raise-on-failure posture (a watcher tick must never crash on a network
    blip -- AGENTS.md's RESILIENCY bar)."""
    try:
        run(cwd, ["git", "fetch", remote, *refs])
    except Exception:
        pass


def _commit_delta(run, cwd, remote, base, branch):
    """-> `{"count": int, "subjects": [...]}`, or `None` when the refs could not be read at all
    (e.g. `branch` was never pushed, or `base` is misconfigured). Reuses
    `feature_rebase.landed_commits` DIRECTLY, args REVERSED from its usual rebase-upkeep direction
    (design `### 2` step 1): `landed_commits(run, cwd, feature_ref, base_ref)` answers "every commit
    put on `base_ref` since `feature_ref`" -- exactly "what the integration branch did while this
    feature branch was away." `subjects` keeps the newest `NEWEST_SUBJECTS` (git log's own
    newest-first order, which `landed_commits` does not re-sort)."""
    base_ref, feature_ref = "%s/%s" % (remote, base), "%s/%s" % (remote, branch)
    try:
        commits = feature_rebase.landed_commits(run, cwd, feature_ref, base_ref)
    except Exception:
        return None
    return {"count": len(commits), "subjects": [subject for _, subject in commits[:NEWEST_SUBJECTS]]}


def _pr_status(run, cwd, repo_ref, owner_ref, branch):
    """-> a short PR-drift string: `"open #N"`, `"merged #N"`, `"closed #N (never merged)"`,
    `"none"`, or `"unknown (<why>)"` when the read itself failed. Reuses
    `unit_completion._landing_pull_requests` (`state=all`, BR-8 -- closed PRs are the point, see
    that function's own docstring) and `unit_completion._pick_landing` (open-outranks-ended)
    DIRECTLY, rather than re-deriving either rule here."""
    rows, error = unit_completion._landing_pull_requests(run, cwd, repo_ref, owner_ref, branch)
    if error is not None:
        return "unknown (%s)" % error
    open_pr, ended_pr = unit_completion._pick_landing(rows or [])
    if open_pr is not None:
        return "open #%s" % open_pr.get("number")
    if ended_pr is not None:
        if ended_pr.get("merged_at"):
            return "merged #%s" % ended_pr.get("number")
        return "closed #%s (never merged)" % ended_pr.get("number")
    return "none"


#: The `_pr_status` return-string prefix that means the landing PR is already merged -- parsed
#: back out of its existing string form (module docstring / issue #2344) rather than changing
#: `_pr_status`'s own return shape, which stays exactly as `sweep` and its tests already expect it.
_MERGED_PREFIX = "merged "


def _merged_pr_ref(pr):
    """-> the bare `"#N"` reference when `pr` is `_pr_status`'s `"merged #N"` string, else `None`."""
    if isinstance(pr, str) and pr.startswith(_MERGED_PREFIX):
        return pr[len(_MERGED_PREFIX):]
    return None


def _landed_field(config, landed_run, cwd, remote, base, branch):
    """-> the optional `landed` field for one drifted unit, or None. ONLY called with the upkeep gate open (the
    caller checks `feature_upkeep.enabled` first), so with the gate closed none of this runs and the report is
    byte-identical to before. One shared predicate (`feature_landed.landed`), never a second copy of the rule."""
    landed_mod = _load("feature_landed")
    runner = landed_run or landed_mod.make_runner()
    slug, why = landed_mod.resolve_slug(config, runner, cwd, remote)
    rc, tip, _err = runner(["git", "-C", cwd, "rev-parse", "--verify", "--quiet", "%s/%s" % (remote, branch)], cwd)
    if rc != 0:
        return landed_mod.verdict_dict(landed_mod.Verdict(landed_mod.UNKNOWN, reason="the unit tip cannot be resolved"))
    verdict = landed_mod.landed(tip.strip(), "%s/%s" % (remote, base), branch, slug, runner, cwd, base_name=base)
    if slug is None and not verdict.reason:
        verdict = verdict._replace(reason=why)
    return landed_mod.verdict_dict(verdict)


def _summarize_unit(r):
    """One Slack-formatted block for a single drifted unit (issue #2344's fix direction):

    - the unit name is Slack-bold -- `*text*`, a single asterisk each side, verified against
      Slack's own mrkdwn docs (`api.slack.com/reference/surfaces/formatting`): `**text**` is a
      GitHub-ism Slack's `chat.postMessage` `text` field does not render.
    - an already-merged landing PR (`_pr_status` returned `"merged #N"`) LEADS the line with that
      fact plainly, per the issue's ★ finding -- a merged landing PR means the unit already shipped
      and this is stale registry bookkeeping, not an active neglected branch, so that is the
      headline, not an afterthought after a scary commit count.
    - example commits are their own indented, dash-bulleted lines -- Slack's own docs say plain
      `mrkdwn` text has "no specific list syntax"; a manually dash-prefixed line is its documented
      workaround, not a GitHub-style auto-list.
    - truncation is stated explicitly whenever `NEWEST_SUBJECTS` shows fewer than the real count,
      so "3 commit subjects" is never mistaken for the whole 180-commit story (the issue's #2 finding).
    """
    delta = r["delta"]
    name = r["unit"] or r["branch"]
    count = delta["count"]
    subjects = delta["subjects"]
    pr = r["pr"]

    merged_ref = _merged_pr_ref(pr)
    if merged_ref is not None:
        head = "*%s* — already landed via %s; registry is stale (%d commit(s) behind)" % (
            name, merged_ref, count)
    else:
        head = "*%s*: %d commit(s) behind — landing PR %s" % (name, count, pr)

    lines = [head]
    if isinstance(r.get("landed"), dict):          # present only when the upkeep gate was open
        lines.append("  _(landed check: %s)_" % r["landed"].get("verdict"))
    lines.extend("  - %s" % subject for subject in subjects)
    if count > len(subjects):
        lines.append("  _(showing %d of %d commits)_" % (len(subjects), count))
    return "\n".join(lines)


def _summarize(reports):
    """One Slack-formatted summary across every DRIFTED unit -- D-1's leaning, reformatted per
    issue #2344. A current unit costs nothing to mention and would just be noise, so only drifted
    units ever reach here (the caller already filtered). Each unit is its own block, separated by a
    blank line, so multiple drifted units never run together into one paragraph."""
    return "\n\n".join(_summarize_unit(r) for r in reports)


# --------------------------------------------------------------------------- channel resolution (D-5)


def _channel_id(config):
    """D-5 / design `### 5`: which ONE of the two hardcoded channels this repo posts to, resolved
    entirely by config -- zero conditional logic on repo identity. `None` when neither is set (an
    operator has turned the watcher on but not yet filled in a channel), which is a documented,
    non-fatal state (`## Blockers`: "a repo can turn the watcher on and see its own logs long before
    anything ever reaches an external channel"). `sigma` wins if both happen to be set at once
    (should not occur in a correctly-deployed config -- each repo fills in exactly one -- but a
    fixed tie-break beats an ambiguous double-post)."""
    channels = settings(config).get("channels") or {}
    for key in ("sigma", "org"):
        value = legacy.channel_value(channels, key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


# --------------------------------------------------------------------------- dedup (§4, D-2)


def _window_id(config, now):
    return int(now // (ttl_minutes(config) * 60))


def _window_ref(window_id):
    return "drift-window:%d" % window_id


def _already_posted(sdlc_dir, ref):
    """The SHARED ledger dedup check (design `### 4`, D-2) -- the UNION across every actor's own
    file (`ledger.read_all`), never a local cursor. True iff ANY actor's entry already carries a
    `note` for this exact window's `ref`."""
    return any(e.get("kind") == "note" and e.get("ref") == ref for e in ledger.read_all(sdlc_dir))


# --------------------------------------------------------------------------- the sweep


def sweep(sdlc_dir, config=None, run=None, now=None, post=None, landed_run=None):
    """The whole tick's real logic (`drift_tick.py` is the thin wrapper threaded into `watch_daemon.py`).

    -> a one-line summary for `watch_daemon.py`'s own log, or `""` when the gate is closed, the sweep is
    not yet due, there is nothing to check, or nothing has drifted. `run`/`now`/`post` are DI for
    tests, matching every sibling watch-tick script's convention (`post` is `channel_notify.tick`'s
    own `run_post` under a name that reads right for a single message rather than a per-tick push).

    NEVER RAISES on its own -- every external call below (`git`, `gh`, Slack) is already wrapped by
    its own callee (`_fetch`, `_commit_delta`, `_pr_status`, `slack_client.post_message`), matching
    `channel_notify.tick`'s own posture of leaving the CLI's `main()` as the outer, blanket
    fail-open boundary rather than duplicating one here too."""
    config = config if config is not None else ledger._config(sdlc_dir)
    if not enabled(config):
        return ""
    now = now if now is not None else time.time()
    if not due(sdlc_dir, config, now=now):
        return ""
    units = _open_units(sdlc_dir)
    work_settings = config.get("work") if isinstance(config, dict) else None
    work_settings = work_settings if isinstance(work_settings, dict) else {}
    base = (feature_rebase.state_safe_ref("work.base", work_settings.get("base")) or "").strip()
    if not units or not base:
        _stamp(sdlc_dir, now=now)
        return ""

    run = run or _run
    cwd = str(pathlib.Path(sdlc_dir).parent)
    remote = (feature_rebase.state_safe_ref("work.remote", work_settings.get("remote")) or "").strip() or "origin"
    _fetch(run, cwd, remote, [base] + [branch for _, branch in units])

    repo_ref = owner_ref = None
    upkeep_open = _load("feature_upkeep").enabled(config)   # the gate module is the only reader of `upkeep`
    reports = []
    for name, branch in units:
        delta = _commit_delta(run, cwd, remote, base, branch)
        if delta is None or delta["count"] == 0:
            continue                      # current, or unreadable -- neither is worth a gh call
        if repo_ref is None:
            repo_ref, owner_ref = unit_completion._repo_ref(config, run, cwd, remote)
        pr = _pr_status(run, cwd, repo_ref, owner_ref, branch)
        report = {"unit": name, "branch": branch, "delta": delta, "pr": pr}
        if upkeep_open:
            report["landed"] = _landed_field(config, landed_run, cwd, remote, base, branch)
        reports.append(report)

    # BR-6's shape: stamp regardless of what follows, so a persistently unreachable Slack endpoint
    # (or simply nothing having drifted) costs one sweep per TTL window, never a retry on every
    # 15-minute watch_daemon.py tick.
    _stamp(sdlc_dir, now=now)
    if not reports:
        return ""

    summary = _summarize(reports)
    channel_id = _channel_id(config)
    if channel_id is None:
        return "drift detected in %d unit(s), but no drift_watch.channels.* is configured yet" % len(reports)

    window_id = _window_id(config, now)
    ref = _window_ref(window_id)
    if _already_posted(sdlc_dir, ref):
        return "drift summary suppressed (window %d already posted)" % window_id

    text = scrub.scrub("*Branch drift detected:*\n\n%s" % summary)
    poster = post or slack_client.post_message
    ok = poster(channel_id, text, config)
    if not ok:
        return "drift summary for %d unit(s) — Slack post failed (non-fatal, window %d, will retry)" % (
            len(reports), window_id)
    # Only a CONFIRMED post writes the marker (design `### 4`) -- a failed post must leave the
    # window genuinely open for whoever posts it next, this machine or a teammate's.
    ledger.safe_append(sdlc_dir, "note", "", config=config, ref=ref, to=None)
    return "posted drift summary for %d unit(s) (window %d)" % (len(reports), window_id)


USAGE = "usage: drift_watch.py [sdlc_dir]"


def main(argv):
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    sdlc_dir = argv[1] if len(argv) > 1 else ".sdlc"
    try:
        print(sweep(sdlc_dir))
    except Exception as exc:                    # noqa: BLE001 - a watcher tick is never fatal
        print(f"drift_watch: sweep failed (non-fatal): {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
