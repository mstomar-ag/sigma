"""The unattended git runner: the engine's `(cwd, argv) -> stdout` contract, with the pins an unattended job needs.

WHAT IT IS. `make_runner(limits, ...)` returns a function with exactly the contract of the rebase engine's default
runner (the sync module's `_run`): it runs `argv` in `cwd`, returns the stripped stdout, and RAISES on a non-zero exit
(a caller reads an exception as "the question went unanswered", and an empty string as "the answer is nothing").
A gated caller injects it as the engine's `run=` argument; the default runner is left exactly as it is, so every
existing caller is unchanged. Nothing in the product calls this yet.

WHAT IT ADDS to a git command, all per command and never stored in any configuration:
  * ENVIRONMENT (from `bounded_run.unattended_env`): no terminal prompt, no editor (an exported editor outranks
    `-c core.editor`, so it is exported), the askpass variables set to the empty text (the terminal-prompt switch
    alone does not stop an askpass helper, a configured askpass program or the ssh helper from hanging git), and the
    repository-location variables removed (a hook-time index or git dir would redirect a replay).
  * ARGUMENTS placed right after `git`, before any option the caller passed: signing off for commits and pushes
    (a failed signature stops a replay and looks like a conflict), `rebase.updateRefs=false` (a user setting would
    move other local branches), and `maintenance.auto=false` (a fetch would start background maintenance).
  * HOOKS POLICY, a factory argument: "off" points `core.hooksPath` at a directory that does not exist (replays and
    clean pushes carry no new code); "inherit" leaves the repository's hooks alone (a push after a resolved
    conflict carries new code). A caller decides per runner; there is no default that runs hooks.
  * FETCH: a `fetch` also gets `--no-write-fetch-head`, so a background fetch leaves the checkout's fetch record
    alone. Both levers are needed: that flag keeps the record but not the maintenance child, and the config switch
    stops the child but not the record. (`-c fetch.writeFetchHEAD=false` and `-c gc.auto=0` were measured to do
    neither.)
  * A TIME LIMIT per command, REQUIRED (`limits` maps a git verb to seconds and must carry "default"; the caller
    derives the numbers, this module has no constants for them). An overrun stops the whole process group
    (SIGTERM first, see `bounded_run`) and raises `GitTimeout`, a RuntimeError that carries the argv, the limit and
    whether SIGKILL was needed, so a caller can tell a timeout from a conflict.

KNOWN GAPS. The ssh transport has no prompt lever here (none was measured); the time limit and the group stop bound
it. `--no-write-fetch-head` needs a recent git (recalled as 2.29, not measured on an older one). Caller options of
the same key placed after the pins win, since git reads the last `-c`.
"""
import importlib.util
import os
import pathlib
import uuid

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bounded_run = _load("bounded_run")

HOOKS_OFF = "off"
HOOKS_INHERIT = "inherit"
CONFIG_PINS = ("commit.gpgsign=false", "push.gpgSign=false", "rebase.updateRefs=false", "maintenance.auto=false")
FETCH_FLAGS = ("--no-write-fetch-head",)
#: Global git options that take their value as the next argument, so the verb is not mistaken for one.
_VALUE_OPTIONS = frozenset(("-c", "-C", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"))


class GitTimeout(RuntimeError):
    """A git command overran its limit and its process group was stopped."""

    def __init__(self, argv, limit, escalated, seconds):
        self.argv, self.limit, self.escalated, self.seconds = list(argv), limit, escalated, seconds
        verb = verb_of(self.argv)
        super().__init__("%s %s: timed out after %gs" % (os.path.basename(self.argv[0]), verb or "", limit))


def verb_index(argv):
    """The position of the git verb in `argv` (argv[0] is git), skipping global options; None when there is none."""
    i = 1
    while i < len(argv):
        if argv[i] in _VALUE_OPTIONS:
            i += 2
        elif argv[i].startswith("-"):
            i += 1
        else:
            return i
    return None


def verb_of(argv):
    """The verb of a git argv, or the program's own name for anything else."""
    if os.path.basename(argv[0]) in ("git", "git.exe"):
        index = verb_index(argv)
        return None if index is None else argv[index]
    return os.path.basename(argv[0])


def no_hooks_dir(cwd):
    """A path under `cwd` that does not exist, fresh for every call, so `core.hooksPath` set to it finds no hook."""
    return os.path.join(str(cwd), ".hooks-off-" + uuid.uuid4().hex)


def decorate(cwd, argv, hooks=HOOKS_OFF):
    """-> the argv to run: git's arguments with the pins, the hooks policy and the fetch flags added. Anything that
    is not git is returned as a list unchanged."""
    if hooks not in (HOOKS_OFF, HOOKS_INHERIT):
        raise ValueError("hooks must be %r or %r" % (HOOKS_OFF, HOOKS_INHERIT))
    argv = [str(a) for a in argv]
    if not argv or os.path.basename(argv[0]) not in ("git", "git.exe"):
        return argv
    pins = []
    for pin in CONFIG_PINS:
        pins += ["-c", pin]
    if hooks == HOOKS_OFF:
        pins += ["-c", "core.hooksPath=" + no_hooks_dir(cwd)]
    out = [argv[0]] + pins + argv[1:]
    index = verb_index(argv)
    if index is not None and argv[index] == "fetch":
        at = len(pins) + index + 1
        out[at:at] = FETCH_FLAGS
    return out


def _limits(limits):
    if not isinstance(limits, dict) or "default" not in limits:
        raise ValueError('limits must be a dict of git verb -> seconds that includes "default"')
    table = {}
    for verb, value in limits.items():
        if not isinstance(verb, str) or not verb:
            raise ValueError("limits keys must be git verbs (text)")
        table[verb] = bounded_run.seconds(value, "limits[%s]" % verb)
    return table


def make_runner(limits, *, hooks=HOOKS_OFF, environ=None, term_grace=None, max_out_bytes=None):
    """-> run(cwd, argv) -> stripped stdout, raising RuntimeError (GitTimeout on an overrun) otherwise.
    `limits` is required; `hooks` is "off" or "inherit"; `environ` is the base environment (default: the process's,
    read at each call)."""
    table = _limits(limits)
    if hooks not in (HOOKS_OFF, HOOKS_INHERIT):
        raise ValueError("hooks must be %r or %r" % (HOOKS_OFF, HOOKS_INHERIT))
    grace = bounded_run.TERM_GRACE_SECONDS if term_grace is None else term_grace
    cap = bounded_run.MAX_OUT_BYTES if max_out_bytes is None else max_out_bytes

    def run(cwd, argv):
        original = [str(a) for a in argv]
        if not original:
            raise ValueError("run needs a command")
        final = decorate(cwd, original, hooks)
        limit = table.get(verb_of(original), table["default"])
        result = bounded_run.run_group(final, str(cwd), limit, env=bounded_run.unattended_env(environ), merge=False,
                                       term_grace=grace, max_out_bytes=cap)
        if result.outcome == bounded_run.TIMEOUT:
            raise GitTimeout(original, limit, result.escalated, result.seconds)
        if result.outcome in (bounded_run.REFUSED, bounded_run.ERROR, bounded_run.STOPPED):
            raise RuntimeError("%s: %s" % (original[0], result.detail))
        if result.truncated:
            raise RuntimeError("%s: output exceeded %d bytes" % (original[0], cap))
        if result.code != 0:
            raise RuntimeError((result.err or result.out or "").strip() or "%s exited %s" % (original[0], result.code))
        return result.out.strip()

    return run


def unmerged_paths(run, cwd):
    """The paths git lists as unmerged in `cwd` -> a sorted list (quoted as git prints them). Empty for a rebase that
    stopped for any other reason (a failed signature, a timeout kill), which is what tells a failure from a conflict.
    Raises what `run` raises."""
    return sorted(line for line in run(cwd, ["git", "diff", "--name-only", "--diff-filter=U"]).splitlines() if line)
