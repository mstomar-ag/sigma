#!/usr/bin/env python3
"""Outcome proxy for merged Sigma goals (#1636): how often is merged work reverted or quickly fixed.

  outcomes.py report [--window DAYS] [--base REF] [--sdlc DIR]

READ-ONLY and local: it reads the action log (via the sibling log.py) and runs `git log` and
`git rev-parse` in the repository that holds the sdlc directory. It writes no file (it never calls
the timing wrapper, and it stops Python writing bytecode while loading log.py), opens no network
connection, calls no `gh`, reads no environment variable and adds no config key.

THIS IS A PROXY, and the first line of every output says so. It counts commits that LOOK like
reverts or fixes and touch a file a goal changed. An unrelated fix to a shared file, a defect nobody
fixed, and a fix landed by a later Sigma goal are all invisible or excluded, so no rate is ever
printed: counts only, and `unmeasured` whenever there are fewer than MIN_GOALS mature goals.

How a goal becomes a row:
  * its action log holds a `merged` record, or a `merge_armed` record, naming a PR;
  * that PR resolves to a commit on the base branch's first-parent history by the LAST `(#N)` at the
    end of a subject (the squash-merge shape); the merge time is that commit's COMMITTER time, never
    a log timestamp, which a reconcile pass can write days late;
  * a goal with a `merged` record that does not resolve is `unresolved` (counted in the header, never
    guessed); merge-commit and rebase merges carry no such marker, so they land here;
  * a goal with only `recorded done` and no `merged`/`merge_armed` record is INVISIBLE, not
    unresolved: nothing in the log says which PR to look for;
  * a goal merged less than FIX_DAYS ago is `immature` and is neither listed nor counted.
Which record wins when a goal has several: a `merged` PR that resolves, else the last
`merge_armed` PR that resolves on the base branch.
"""
import collections
import fnmatch
import importlib.util
import pathlib
import re
import subprocess
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:                                   # noqa: BLE001
    pass

DEFAULT_WINDOW = 30
DEFAULT_RETENTION = 90        # retention.py's default; the real cap is read from config
MIN_RETENTION = 30            # retention.py's floor
HARD_MAX_WINDOW = 365         # only when retention is disabled: bounds the single git pass
FIX_DAYS = 14
DAY = 86400
MIN_GOALS = 10
FILES_PER_GOAL_CAP = 200
ROW_CAP = 100
GIT_TIMEOUT = 60
HOT_SHARE = 5                 # a file in more than 1/HOT_SHARE of the pass's commits is hot
HOT_SHOWN = 10
DENYLIST = ("CHANGELOG.md", "README.md", "docs/launch/write-surface.json", "docs/configuration.md",
            ".sdlc/*", "*.lock")

PROXY = ("proxy: counts reverts and fix-subject commits that touch a goal's files; unrelated fixes "
         "and unreported defects are not counted, so this bounds no defect rate")
USAGE = ("usage: outcomes.py report [--window DAYS] [--base REF] [--sdlc DIR]  "
         f"(window 1-{DEFAULT_RETENTION} by default, capped at the action-log retention)")


class Refusal(Exception):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _window_arg(raw):
    # ASCII digits only: int() alone would also take `1_5`, ` 5`, `+5` and non-ASCII digits.
    if not re.fullmatch(r"[0-9]+", str(raw)):
        raise Refusal("bad-window", f"--window needs a whole number of days, got {raw!r}")
    window = int(raw)
    if window < 1:
        raise Refusal("bad-window", f"--window must be at least 1 day, got {window}")
    return window


def _parse(argv):
    """-> (window, base, sdlc). Raises Refusal; touches nothing."""
    if len(argv) < 2:
        raise Refusal("no-verb", "a verb is required: report")
    if argv[1] != "report":
        raise Refusal("unknown-verb", f"unknown verb {argv[1]!r}; the only verb is report")
    opts = {"--window": str(DEFAULT_WINDOW), "--base": None, "--sdlc": ".sdlc"}
    rest = argv[2:]
    i = 0
    while i < len(rest):
        flag = rest[i]
        if flag not in opts:
            raise Refusal("bad-flag", f"unknown flag {flag!r}")
        if i + 1 >= len(rest):
            raise Refusal("bad-flag", f"{flag} needs a value")
        opts[flag] = rest[i + 1]
        i += 2
    return _window_arg(opts["--window"]), opts["--base"], opts["--sdlc"]


def _load_log():
    """The sibling log.py, loaded by path under another module name (TD-1: one copy of the log
    reader, not a third). Bytecode writing is switched off for the load and restored after, so
    reading the log leaves no __pycache__ beside the script (AC-5)."""
    path = pathlib.Path(__file__).resolve().with_name("log.py")
    spec = importlib.util.spec_from_file_location("sigma_log_reader", path)
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def _retention_days(config):
    """Days of closed-goal history the action log keeps, or None when pruning is disabled. Same rule
    as retention.keep_days (default 90, floor 30, `false` disables): a format-only copy, because
    importing that module would couple this reader to sigma-loop."""
    settings = config.get("action_log") if isinstance(config, dict) else None
    raw = settings.get("retention_days") if isinstance(settings, dict) else None
    if raw is False:
        return None
    if isinstance(raw, int) and not isinstance(raw, bool):
        return max(raw, MIN_RETENTION)
    return DEFAULT_RETENTION


def _window_cap(config):
    days = _retention_days(config) if config is not None else DEFAULT_RETENTION
    return (HARD_MAX_WINDOW if days is None else days), ("disabled" if days is None else f"{days}d")


def _log_gate(log, sdlc, state_triple):
    """-> (config, reason). `reason` is None when the log can be read. `state_triple` is
    `log.action_log_state(sdlc)`, read once by main."""
    state, _detail, config = state_triple
    if state == log.LOG_UNKNOWN:
        return None, ("action log state unknown (config.json could not be read; a goal worktree has "
                      "none, so this is not the same as off)")
    if state == log.LOG_OFF:
        return None, "action log off (action_log.enabled is not true)"
    if not log.log_dir(sdlc).is_dir():
        return config, "no action log yet (nothing has been logged under state/log)"
    return config, None


def _git(args, cwd):
    """The ONLY subprocess call. Verbs used: log, rev-parse. Bounded by GIT_TIMEOUT."""
    return subprocess.run(["git", "--no-pager", "-c", "log.showSignature=false", *args],
                          cwd=str(cwd), capture_output=True, timeout=GIT_TIMEOUT)


def _git_text(args, cwd):
    r = _git(args, cwd)
    return r.returncode, r.stdout.decode("utf-8", errors="replace").strip()


def _git_gate(repo, base):
    """-> (resolved base ref, reason). `reason` is None when git can be read."""
    try:
        rc, out = _git_text(["rev-parse", "--is-shallow-repository"], repo)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"git could not be run ({type(exc).__name__})"
    if rc != 0:
        return None, f"{repo} is not a git repository"
    if out == "true":
        return None, "shallow clone (history is truncated, so reverts and fixes cannot be located)"
    for ref in ([base] if base else ["origin/main", "main"]):
        rc, _ = _git_text(["rev-parse", "--verify", "--quiet", ref + "^{commit}"], repo)
        if rc == 0:
            return ref, None
    return None, f"base ref {base or 'origin/main or main'} not found"


def _unmeasured(reason):
    return [PROXY, f"unmeasured: {reason}"]


MERGE_PR_RE = re.compile(r"\(#(\d+)\)\s*$")
_LOG_FORMAT = "%x01%H%x1f%P%x1f%ct%x1f%s%x1f%b%x1e"


def _merge_pr(subject):
    """The PR a squash-merge subject names: the LAST `(#N)`, and only at the very end. A quoted
    marker inside a revert subject (`Revert "x (#12)"`) is not a merge, and a merge-commit subject
    (`Merge pull request #12 ...`) carries no such marker, which is why those goals are unresolved."""
    m = MERGE_PR_RE.search(subject or "")
    return m.group(1) if m else None


class GitFailure(Exception):
    pass


def _git_log(repo, ref, since):
    """ONE pass over `ref`'s first-parent history since `since` (epoch), newest first, as a list of
    {sha, parents, ts, subject, body, files}. First-parent means the base branch's own history: a
    commit that reached it through a merge commit is not on it, which is also how a goal's own
    branch commits stay out of its count. `-z` keeps paths with spaces or non-ASCII bytes whole;
    `-M` reports a rename as both paths; output is decoded as UTF-8 with `errors=replace`."""
    try:
        r = _git(["log", ref, "--first-parent", "--topo-order", f"--since={int(since)}",
                  "--name-status", "-M", "-z", f"--format={_LOG_FORMAT}"], repo)
    except subprocess.TimeoutExpired:
        raise GitFailure(f"git log did not finish within {GIT_TIMEOUT}s")
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitFailure(f"git log could not run ({type(exc).__name__})")
    if r.returncode != 0:
        first = r.stderr.decode("utf-8", errors="replace").strip().splitlines()[:1]
        raise GitFailure(f"git log failed ({first[0] if first else 'exit ' + str(r.returncode)})")
    commits = []
    for chunk in r.stdout.decode("utf-8", errors="replace").split("\x01")[1:]:
        head, _, tail = chunk.partition("\x1e")
        fields = head.split("\x1f")
        if len(fields) != 5:
            continue
        sha, parents, ts, subject, body = fields
        tokens = tail.lstrip("\0\n").split("\0")
        files, i = set(), 0
        while i < len(tokens) and tokens[i]:
            status = tokens[i]
            width = 2 if status[0] in "RC" else 1
            files.update(t for t in tokens[i + 1:i + 1 + width] if t)
            i += 1 + width
        try:
            stamp = int(ts)
        except ValueError:
            continue
        commits.append({"sha": sha, "parents": parents.split(), "ts": stamp, "subject": subject,
                        "body": body, "files": files})
    return commits


#: Anchored at the start and bounded by a word boundary: `prefix:`, `fixture:` and `fixup!` are not
#: fixes, and neither is a subject that merely mentions the word. Each clause has its own control.
FIX_RE = re.compile(r"^fix\b", re.IGNORECASE)
REVERT_BODY_RE = re.compile(r"This reverts commit ([0-9a-f]{7,40})")
RELAND_PREFIX = 'Revert "Revert '


def _kind(subject, body, known=None):
    """'revert', 'fix' or None. A revert is `Revert "..."` or a body naming the reverted commit; a
    revert OF a revert is a re-land, not a defect, so it is None. A body-only mention counts only
    when `known(sha)` says the sha is a commit in this pass: a squash body that lists a branch's
    internal revert names a commit that never reached base, and is not a revert of anything here.
    `known=None` trusts the body (the classification table)."""
    if subject.startswith(RELAND_PREFIX):
        return None
    if subject.startswith('Revert "'):
        return "revert"
    named = REVERT_BODY_RE.search(body or "")
    if named and (known is None or known(named.group(1))):
        return "revert"
    return "fix" if FIX_RE.search(subject) else None


def _pr_text(value):
    text = str(value if value is not None else "").strip().lstrip("#")
    return text if text.isdigit() else None


def _goal_records(log, sdlc, stem, floor):
    """-> (prs in preference order, merged_seen, every pr this goal ever logged, readable). A `merged` PR
    outranks a `merge_armed` one; within each, the latest first. Records older than `floor` are too
    old to matter for this window and are skipped (so an old, pruned-from-git goal is silent)."""
    merged, armed, seen, every = [], [], False, set()
    entries = log.read_goal(sdlc, stem)
    for entry in entries:
        pr = _pr_text(entry.get("pr"))
        if pr:
            every.add(pr)
        kind = entry.get("kind")
        if kind not in ("merged", "merge_armed"):
            continue
        stamp = log._epoch(entry.get("ts"))
        if stamp is not None and stamp < floor:
            continue
        seen = seen or kind == "merged"
        if pr:
            (merged if kind == "merged" else armed).append(pr)
    return list(reversed(merged)) + list(reversed(armed)), seen, every, bool(entries)


def _merged_goals(log, sdlc, commits, now, window):
    """-> dict(mature=[rows], immature=int, unresolved=int, unreadable=int, goal_prs=set). A row is
    {goal, pr, sha, idx, ts, files}; `idx` is the merge commit's position in the newest-first base
    walk, which is what decides 'later on base' (time only caps)."""
    by_pr = {}
    for idx, commit in enumerate(commits):
        pr = _merge_pr(commit["subject"])
        if pr:
            by_pr.setdefault(pr, idx)            # newest wins a duplicate marker
    floor = now - (window + FIX_DAYS) * DAY
    start, mature_before = now - window * DAY, now - FIX_DAYS * DAY
    out = {"mature": [], "immature": 0, "unresolved": 0, "unreadable": 0, "goal_prs": set()}
    for stem in log._all_goal_stems(sdlc):
        prs, merged_seen, every, readable = _goal_records(log, sdlc, stem, floor)
        out["goal_prs"] |= every
        out["unreadable"] += 0 if readable else 1     # garbage, empty or unopenable: never silent
        hit = next((pr for pr in prs if pr in by_pr), None)
        if hit is None:
            out["unresolved"] += 1 if merged_seen else 0
            continue
        commit = commits[by_pr[hit]]
        if commit["ts"] < start:
            continue
        if commit["ts"] > mature_before:
            out["immature"] += 1
            continue
        out["mature"].append({"goal": stem, "pr": hit, "sha": commit["sha"], "idx": by_pr[hit],
                              "ts": commit["ts"], "files": commit["files"]})
    return out


def _hot_files(commits):
    """Files touched by more than 1/HOT_SHARE of the pass's own commits. A static denylist cannot
    keep up with whatever a repository churns, so the window itself names the files too shared to
    say anything about one goal. On this repo it changes no number: its derived set (CHANGELOG.md
    and the write-surface file) is already in the denylist (docs/action-log-and-ledger.md has the
    measured medians)."""
    seen = collections.Counter()
    for commit in commits:
        seen.update(commit["files"])
    return {path for path, n in seen.items() if n * HOT_SHARE > len(commits)}


def _ignored(path, hot):
    return path in hot or any(fnmatch.fnmatchcase(path, pattern) for pattern in DENYLIST)


def _scopes(rows, hot):
    """Give each row its `scope` (the files that can attribute a later commit to it): the merge
    commit's files minus denylisted and hot ones, sorted, cut at FILES_PER_GOAL_CAP. -> capped count."""
    capped = 0
    for row in rows:
        keep = sorted(path for path in row["files"] if not _ignored(path, hot))
        if len(keep) > FILES_PER_GOAL_CAP:
            capped += 1
            keep = keep[:FILES_PER_GOAL_CAP]
        row["scope"] = set(keep)
    return capped


def _candidates(commit, kind, rows, by_file, hot):
    """Rows a counted commit may belong to. A revert that names a goal's merge commit belongs to
    that goal alone, with no file test; anything else belongs to every row sharing a non-ignored file."""
    if kind == "revert":
        target = REVERT_BODY_RE.search(commit["body"] or "")
        if target:
            named = [row for row in rows if row["sha"].startswith(target.group(1))]
            if named:
                return named
    found = {}
    for path in commit["files"]:
        if not _ignored(path, hot):
            for row in by_file.get(path, ()):
                found[id(row)] = row
    return list(found.values())


def _attribute(commits, rows, goal_prs, hot):
    """-> {id(row): [reverts, fixes]}, {sha: set(goal)} for reverts, same for fixes.

    `commits` is newest first, so a commit at a smaller position is LATER on base: position decides
    order and the committer clock only caps (a skewed clock must not reorder history). A `fix` whose
    last `(#N)` is a goal PR is a Sigma goal's own work and is skipped; a revert of the same shape
    still counts, because an undone goal is a defect whoever undid it."""
    by_file = collections.defaultdict(list)
    for row in rows:
        for path in row["scope"]:
            by_file[path].append(row)
    shas = [commit["sha"] for commit in commits]

    def known(prefix):
        return any(sha.startswith(prefix) for sha in shas)

    tally = {id(row): [0, 0] for row in rows}
    who = {"revert": collections.defaultdict(set), "fix": collections.defaultdict(set)}
    for idx, commit in enumerate(commits):
        kind = _kind(commit["subject"], commit["body"], known)
        if kind is None or (kind == "fix" and _merge_pr(commit["subject"]) in goal_prs):
            continue
        for row in _candidates(commit, kind, rows, by_file, hot):
            if idx < row["idx"] and commit["ts"] <= row["ts"] + FIX_DAYS * DAY:
                tally[id(row)][0 if kind == "revert" else 1] += 1
                who[kind][commit["sha"]].add(row["goal"])
    return tally, who


def _iso(stamp):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp))


def _hot_text(hot):
    names = sorted(hot)
    if not names:
        return "none"
    return ",".join(names[:HOT_SHOWN]) + (f",+{len(names) - HOT_SHOWN}more" if len(names) > HOT_SHOWN else "")


def report(log, sdlc, state, window, base, now):
    config, reason = _log_gate(log, sdlc, state)
    if reason:
        return _unmeasured(reason)
    if window <= FIX_DAYS:
        return _unmeasured(f"a window of {FIX_DAYS} days or less cannot hold a goal merged at least "
                           f"{FIX_DAYS} days ago, so no goal is ever mature; use a longer --window")
    repo = pathlib.Path(sdlc).resolve().parent
    ref, reason = _git_gate(repo, base)
    if reason:
        return _unmeasured(reason)
    try:
        commits = _git_log(repo, ref, now - (window + FIX_DAYS) * DAY)
    except GitFailure as exc:
        return _unmeasured(str(exc))
    goals = _merged_goals(log, sdlc, commits, now, window)
    retention = _window_cap(config)[1]
    rows = sorted(goals["mature"], key=lambda r: (r["ts"], r["goal"]))
    if len(rows) < MIN_GOALS:
        return _unmeasured(f"{len(rows)} mature goals in the last {window}d (retention={retention}), "
                           f"need {MIN_GOALS}; immature={goals['immature']} "
                           f"unresolved={goals['unresolved']} unreadable={goals['unreadable']}")
    hot = _hot_files(commits)
    capped = _scopes(rows, hot)
    tally, who = _attribute(commits, rows, goals["goal_prs"], hot)
    head = (f"window={window}d mature={len(rows)} immature={goals['immature']} "
            f"unresolved={goals['unresolved']} unreadable={goals['unreadable']} retention={retention} base={ref} commits={len(commits)} "
            f"denylist={','.join(DENYLIST)} hot={_hot_text(hot)}"
            + (f" files-capped={capped}" if capped else ""))
    lines = [PROXY, head]
    for row in rows[:ROW_CAP]:
        reverts, fixes = tally[id(row)]
        lines.append(f"{row['goal']}\t{row['pr']}\t{_iso(row['ts'])}\t{reverts}\t{fixes}")
    if len(rows) > ROW_CAP:
        lines.append(f"... {len(rows) - ROW_CAP} more goals not listed; totals below cover all {len(rows)}")
    multi = sum(1 for kind in who.values() for goals_hit in kind.values() if len(goals_hit) > 1)
    lines.append(f"totals reverts={len(who['revert'])} fixes={len(who['fix'])} (distinct commits) "
                 f"multi-attributed={multi}")
    return lines


def main(argv, now=None):
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    try:
        window, base, sdlc = _parse(argv)
        log = _load_log()
        state = log.action_log_state(sdlc)
        # Order: the window is checked against retention BEFORE the log is judged on or off. With
        # the log off or unreadable, retention is unknown and the default (90) is the cap, so
        # `--window 91` is refused even then; an over-long window is a usage error whatever the log says.
        cap, _label = _window_cap(state[2] if state[0] == log.LOG_ON else None)
        if window > cap:
            raise Refusal("window-exceeds-retention",
                          f"--window {window} is longer than the {cap} days the action log keeps "
                          f"closed goals (action_log.retention_days); goals older than that are "
                          f"pruned, so the window would silently undercount")
    except Refusal as exc:
        print(f"outcomes.py: REFUSED [{exc.code}]: {exc.detail}", file=sys.stderr)
        return 2
    lines = report(log, sdlc, state, window, base, time.time() if now is None else now)
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
