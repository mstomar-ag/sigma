#!/usr/bin/env python3
"""Which daemon stop-files are sitting in <sdlc>/state, and how old (#416).

A stop-file is the polite, documented way to halt a daemon -- and the daemon that honours it exits
SILENTLY from the operator's point of view, then (for the watcher) is not restarted while the file
exists. A forgotten one therefore looks exactly like a dead daemon with nothing to say. Age is the
tell: a file touched a minute ago is a deliberate stop, one a week old is almost certainly forgotten.

One home for the facts so `/sigma-doctor` and `hooks/session_start.sh` (an accelerator only; Cursor
has no hooks) say the same thing. Read-only: it never removes a stop-file -- that is the operator's
lever. Never raises.

    stopfiles.py line <sdlc_dir>    # prints one line when any stop-file exists, nothing otherwise
"""
import os
import pathlib
import sys
import time

#: (filename under <sdlc>/state, what honours it, the daemon's OWN predicate for "stopped"). Mirrors
#: each daemon exactly so a file the daemon ignores is never reported as a stop: watch_daemon.py uses
#: `Path.is_file()`, supervise_daemon.py and slack_commands_listen.py use `Path.exists()`.
STOP_FILES = (
    ("watch.stop", "ledger watcher", lambda p: p.is_file()),
    ("supervisor.stop", "loop supervisor", lambda p: p.exists()),
    ("slack-commands.stop", "slack-commands listener", lambda p: p.exists()),
    ("upkeep.stop", "upkeep job", lambda p: p.exists()),
)


def _age(seconds):
    if seconds < 90:
        return "%ds" % seconds
    if seconds < 5400:
        return "%dm" % round(seconds / 60.0)
    if seconds < 172800:
        return "%.1fh" % (seconds / 3600.0)
    return "%dd" % (seconds // 86400)


def _unreadable(state):
    """True when <state> exists but cannot be searched: every lstat below it would then say
    "absent", a silent all-clear, so it has to be told apart from a genuinely empty directory."""
    try:
        return os.path.lexists(state) and not os.access(state, os.R_OK | os.X_OK)
    except OSError:
        return True


def present(sdlc_dir, now=None):
    """[(filename, daemon, age_seconds_or_None, path, honoured)] for every stop-file entry that
    exists. `honoured` is False when something is there but the daemon's own predicate would not
    treat it as a stop (a dangling symlink, or a directory where the watcher wants a regular file)."""
    now = time.time() if now is None else now
    state = pathlib.Path(sdlc_dir) / "state"
    found = []
    for name, daemon, honours in STOP_FILES:
        path = state / name
        try:
            if not os.path.lexists(path):
                continue
            honoured = bool(honours(path))
        except OSError:
            continue
        try:
            age = max(0, now - path.stat().st_mtime)
        except OSError:
            age = None
        found.append((name, daemon, age, path, honoured))
    return found


def _describe(name, daemon, age):
    return "%s (%s, %s)" % (name, daemon, "age unknown" if age is None else _age(age) + " old")


def line(sdlc_dir, now=None):
    """One human line, or "" when there is nothing to say."""
    if _unreadable(pathlib.Path(sdlc_dir) / "state"):
        return "COULD NOT CHECK stop-files: .sdlc/state is not readable; not an all-clear"
    found = present(sdlc_dir, now)
    stops = [f for f in found if f[4]]
    ignored = [f for f in found if not f[4]]
    parts = []
    if stops:
        parts.append("STOPPED by stop-file: %s. That daemon exits (watcher: not restarted) while the "
                     "file exists; delete %s to resume." % (
                         "; ".join(_describe(n, d, a) for n, d, a, _, _ in stops),
                         "it" if len(stops) == 1 else "them"))
    if ignored:
        parts.append("Present but not a regular file, so the daemon ignores it: %s." %
                     "; ".join(n for n, _, _, _, _ in ignored))
    return " ".join(parts)


def main(argv):
    if argv[1:] in (["-h"], ["--help"]):
        print("usage: stopfiles.py line <sdlc_dir>\n\n" + __doc__.strip())
        return 0
    if len(argv) != 3 or argv[1] != "line":
        print("usage: stopfiles.py line <sdlc_dir>", file=sys.stderr)
        return 2
    try:
        text = line(argv[2])
    except Exception:            # noqa: BLE001 - a report must never break its caller
        return 0
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
