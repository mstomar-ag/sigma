#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Pre-push guard: refuse a push whose NEW commits carry a denied pattern (goal 1088).

WHY. A push to a public remote must not carry private names, paths or secret shapes in a commit
message or an added line. The terms to look for belong to the operator and stay out of this tree:
they are read from a patterns file, never from here.

USAGE (as the pre-push hook; git passes the remote name as argv and ref lines on stdin)
    python3 tools/prepush_scan.py --patterns FILE [--base REF] [REMOTE_NAME [REMOTE_URL]]
Install it with the two-line hook in docs/prepush-guard.md.

SCOPE. Per pushed ref it scans `rev-list LOCAL --not [REMOTE_SHA] --remotes`: only commits not
reachable from any remote-tracking ref. A merge of the default branch therefore adds none of the
upstream commits to the scan, and a new branch scans only what was never pushed. `--base REF`
replaces `--remotes` with that one ref. The output says how many commits were scanned and skipped.
Fetch before pushing: a stale remote-tracking ref makes the scan wider, never narrower.

PATTERNS FILE. `--patterns FILE`, else the `SIGMA_PREPUSH_PATTERNS` environment variable; there is
NO default path. One Python regular expression per line, case-insensitive; blank lines and lines
starting `#` are skipped. A pattern that does not compile or matches the empty string is refused
by LINE NUMBER, never its text. A file with no pattern is refused (a guard with nothing to look
for would pass everything).

OUTPUT. A hit is reported as commit, surface (commit message or added line) and the pattern's line
number; the matched text is never printed. Exit 0 clean, 1 a hit, 2 a usage or patterns refusal.
Exit 2 also covers any nonzero git exit (e.g. a remote sha unknown locally): the guard refuses
loudly rather than report "scanned 0 commits". Merge commits are diffed against their first parent
(`git show -m --first-parent`) so conflict-resolution lines are seen; lines merged in from the other
side also show, a conservative over-report. A delete push has nothing to scan and is skipped. Read-only: it runs only git read verbs.
"""
import argparse
import os
import re
import subprocess
import sys

ENV_PATTERNS = "SIGMA_PREPUSH_PATTERNS"
ZERO = "0" * 40
MAX_SHOWN = 25


class GitError(Exception):
    """A git read verb exited nonzero; carries only the verb and code, never git's output."""


def git(*args):
    try:
        r = subprocess.run(["git", *args], capture_output=True, text=True, errors="replace")
    except OSError as exc:
        raise GitError("cannot run git (%s)" % type(exc).__name__)
    if r.returncode:
        raise GitError("git %s failed (exit %d)" % (args[0], r.returncode))
    return r.stdout


def load_patterns(path):
    """[(line_no, compiled)] or raise ValueError carrying a text safe to print."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError("cannot read the patterns file (%s)" % type(exc).__name__)
    out = []
    for n, raw in enumerate(lines, 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        try:
            rx = re.compile(raw.strip(), re.I)
        except re.error:
            raise ValueError("patterns file line %d does not compile" % n)
        if rx.search(""):
            raise ValueError("patterns file line %d matches the empty string" % n)
        out.append((n, rx))
    if not out:
        raise ValueError("the patterns file holds no pattern")
    return out


def scan_commit(sha, patterns):
    hits = []
    msg = git("log", "-1", "--format=%B", sha)
    for n, rx in patterns:
        if rx.search(msg):
            hits.append((sha[:10], "commit message", n))
    for ln in git("show", "-m", "--first-parent", "--format=", "-U0", "--no-color", sha).splitlines():
        if ln.startswith("+") and not ln.startswith("+++"):
            for n, rx in patterns:
                if rx.search(ln):
                    hits.append((sha[:10], "added line", n))
    return hits


def main(argv=None, stdin=None):
    ap = argparse.ArgumentParser(prog="prepush_scan.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--patterns", help="deny-pattern file (else $%s)" % ENV_PATTERNS)
    ap.add_argument("--base", help="exclude commits reachable from this ref instead of every remote-tracking ref")
    ap.add_argument("remote", nargs="*", help="remote name and URL, as git passes them (unused)")
    args = ap.parse_args(argv)
    path = args.patterns or os.environ.get(ENV_PATTERNS)
    if not path:
        print("prepush_scan: REFUSED: no patterns file (--patterns FILE or $%s)" % ENV_PATTERNS, file=sys.stderr)
        return 2
    try:
        patterns = load_patterns(path)
    except ValueError as exc:
        print("prepush_scan: REFUSED: %s" % exc, file=sys.stderr)
        return 2
    try:
        return scan(args, patterns, stdin)
    except GitError as exc:
        print("prepush_scan: REFUSED: %s; nothing was verified" % exc, file=sys.stderr)
        return 2


def scan(args, patterns, stdin):
    exclude = [args.base] if args.base else ["--remotes"]
    scanned, skipped, bad = set(), 0, []
    for line in (stdin if stdin is not None else sys.stdin):
        parts = line.split()
        if len(parts) < 4:
            continue
        local_sha, remote_sha = parts[1], parts[3]
        if local_sha == ZERO:
            continue
        floor = [] if remote_sha == ZERO else [remote_sha]
        new = git("rev-list", local_sha, "--not", *floor, *exclude).split()
        everything = git("rev-list", local_sha, *(["--not", remote_sha] if floor else [])).split()
        skipped += len(set(everything) - set(new))
        for c in new:
            if c not in scanned:
                scanned.add(c)
                bad.extend(scan_commit(c, patterns))
    print("prepush_scan: scanned %d commit%s, skipped %d (excluded: commits already %s)"
          % (len(scanned), "" if len(scanned) == 1 else "s", skipped,
             "reachable from " + args.base if args.base else "on a remote-tracking ref"), file=sys.stderr)
    if bad:
        print("prepush_scan: REFUSED -- denied patterns would reach the remote:", file=sys.stderr)
        for c, where, n in bad[:MAX_SHOWN]:
            print("  %s %s: pattern %d" % (c, where, n), file=sys.stderr)
        if len(bad) > MAX_SHOWN:
            print("  ... and %d more" % (len(bad) - MAX_SHOWN), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
