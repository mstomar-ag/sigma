"""Support for the slice 5 tests (backup refs: create, restore, prune). No test lives here, and nothing here asserts
at import time.

It loads the backup module by path (never by import, so a missing module is an assertion INSIDE a test and never a
collection error), builds throwaway real-git remotes, records the commands a function runs, and holds a small
stateful stand-in for a remote that is used only where a thousand refs would make real git slow. The stand-in is
itself checked against real git by an unlisted test, so a test that uses it does not rest on a guess."""
import fnmatch
import os
import pathlib
import subprocess

import upkeep_support as support

ROOT = support.ROOT
SCRIPTS = support.SCRIPTS
MODULE = SCRIPTS / "feature_backup.py"
MISSING = "feature_backup.py does not exist yet: the backup module has not been written"
OPEN = {"upkeep": {"enabled": True}}
CLOCK = 1790000000          # 2026-09-21 14:13:20 UTC, a fixed wall clock for every stamp
DAY = 86400
OLD = "0123456789abcdef0123456789abcdef01234567"      # a well-formed object name that names nothing

_MEMO = {}
_ENV = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_TERMINAL_PROMPT="0",
            GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="t",
            GIT_COMMITTER_EMAIL="t@example.com")


def module():
    """The real backup module, loaded once per process; an assertion (not an exception) when it is not there."""
    assert MODULE.is_file(), MISSING
    if "real" not in _MEMO:
        _MEMO["real"] = support.script("feature_backup")
    return _MEMO["real"]


def engine():
    """The engine module under a variant name (a fresh copy, so a test may set its seams)."""
    return support.script("feature_rebase")


def pushed(world, old, run, clock=CLOCK, unit="u", backup=True):
    """The engine's unit push (`_pushed`) run in `world.local`, with or without the backup descriptor
    -> (outcome, report). The descriptor is what the pass builds only when the upkeep gate is open."""
    m = engine()
    report = {"why": "", "tip": None}
    descriptor = {"unit": unit, "clock": clock} if backup else None
    outcome = m._pushed(run, str(world.local), str(world.local), "feature/" + unit, old, "origin", report, backup=descriptor)
    return outcome, report


def variant(old, new):
    """The module rebuilt with one source substitution (a mutation control); the target must exist."""
    assert MODULE.is_file(), MISSING
    return support.script("feature_backup", edit=(old, new))


def config(**backup):
    return {"upkeep": {"enabled": True, "backup": dict(backup)}} if backup else OPEN


def stamp_days_ago(days, seconds=0):
    return module().stamp_of(CLOCK - days * DAY - seconds)


def git(cwd, *args, env=None, check=True):
    """Real git, output captured, an AssertionError (naming the command) on a non-zero exit."""
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, env=dict(_ENV, **(env or {})))
    if check and proc.returncode != 0:
        raise AssertionError("git %s failed in %s: %s" % (" ".join(args), cwd, (proc.stderr or proc.stdout).strip()))
    return proc.stdout.strip()


def runner(cwd, argv):
    """The engine's runner contract: `(cwd, argv) -> stdout`, raising RuntimeError on a non-zero exit."""
    proc = subprocess.run([str(a) for a in argv], cwd=str(cwd), capture_output=True, text=True, env=_ENV)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip() or "%s exited %s" % (argv[0], proc.returncode))
    return proc.stdout.strip()


def remote_refs(local):
    """{ref: sha} of everything on the remote named origin, read from checkout `local` (any World, ours or the engine suite's)."""
    out = git(local, "ls-remote", "origin")
    return {ref: sha for sha, _, ref in (line.partition("\t") for line in out.splitlines())}


class Recorder:
    """Wraps a runner; keeps every argv; `before(argv)` runs just ahead of each call (a place to inject a race)."""

    def __init__(self, inner=runner, before=None):
        self.inner, self.before, self.calls = inner, before, []

    def __call__(self, cwd, argv):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        if self.before is not None:
            self.before(argv)
        return self.inner(cwd, argv)

    def pushes(self):
        return [c for c in self.calls if c[:2] == ["git", "push"]]


class World:
    """A bare remote, a checkout with a remote named origin, and `feature/u` pushed once."""

    def __init__(self, root):
        self.root = pathlib.Path(root)
        self.remote = self.root / "remote.git"
        self.local = self.root / "local"
        self.sdlc = self.local / ".sdlc"

    def build(self, unit="u"):
        git(self.root, "init", "-q", "--bare", str(self.remote))
        git(self.root, "init", "-q", "-b", "main", str(self.local))
        git(self.local, "remote", "add", "origin", str(self.remote))
        self.commit("seed.txt", "seed")
        git(self.local, "push", "-q", "origin", "main:refs/heads/feature/" + unit)
        return self

    def commit(self, name, text, date=None, message="c"):
        (self.local / name).write_text(text + "\n", encoding="utf-8")
        git(self.local, "add", name)
        env = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date} if date else {}
        git(self.local, "commit", "-q", "-m", message, env=env)
        return self.head()

    def amend(self, message="amended"):
        git(self.local, "commit", "-q", "--amend", "-m", message)
        return self.head()

    def head(self):
        return git(self.local, "rev-parse", "HEAD")

    def refs(self):
        out = git(self.local, "ls-remote", "origin")
        return {ref: sha for sha, _, ref in (line.partition("\t") for line in out.splitlines())}

    def tip(self, branch="feature/u"):
        return self.refs().get("refs/heads/" + branch, "")

    def ref(self, name):
        return self.refs().get(name, "")

    def push_raw(self, sha, ref, force=False):
        git(self.local, "push", "-q", *(["--force"] if force else []), "origin", "%s:%s" % (sha, ref))

    def clone(self, name):
        target = self.root / name
        git(self.root, "clone", "-q", "--no-local", str(self.remote), str(target))
        return target

    def local_refs(self, cwd=None, pattern="refs/"):
        out = git(cwd or self.local, "for-each-ref", "--format=%(refname)", pattern)
        return [line for line in out.splitlines() if line]

    def other_writer_pushes(self, branch="feature/u", name="other"):
        """Another clone commits on top of the remote branch and pushes it; -> the new remote tip."""
        other = self.clone(name)
        git(other, "checkout", "-q", "-b", "w", "origin/" + branch)
        (other / "other.txt").write_text("other\n", encoding="utf-8")
        git(other, "add", "other.txt")
        git(other, "commit", "-q", "-m", "other writer")
        git(other, "push", "-q", "origin", "w:refs/heads/" + branch)
        return git(other, "rev-parse", "HEAD")


class FakeRemote:
    """A stateful stand-in for a remote, speaking only the two commands the prune uses. `ls-remote` matches a
    pattern the way git does (from the end of the name, `*` also crossing a slash); a delete honours each ref's
    lease and reports a stale one after processing the rest. Anything else is a bug in the test, so it raises."""

    def __init__(self, refs=None, before=None):
        self.refs = dict(refs or {})
        self.before, self.calls = before, []

    def __call__(self, cwd, argv):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        if self.before is not None:
            self.before(argv, self)
        if argv[:2] == ["git", "ls-remote"]:
            patterns = argv[3:]
            hits = [(sha, ref) for ref, sha in sorted(self.refs.items())
                    if not patterns or any(fnmatch.fnmatchcase("/" + ref, "*/" + p) for p in patterns)]
            return "\n".join("%s\t%s" % hit for hit in hits)
        if argv[:3] == ["git", "push", "--delete"]:
            rest = argv[3:]
            leases = {}
            while rest and rest[0].startswith("--force-with-lease="):
                ref, _, sha = rest.pop(0)[len("--force-with-lease="):].rpartition(":")
                leases[ref] = sha
            stale = []
            for ref in rest[1:]:
                if ref in self.refs and leases.get(ref) not in (None, self.refs[ref]):
                    stale.append(ref)
                else:
                    self.refs.pop(ref, None)
            if stale:
                raise RuntimeError("stale info: " + " ".join(stale))
            return ""
        raise AssertionError("FakeRemote cannot emulate: %r" % (argv,))

    def deletes(self):
        return [c for c in self.calls if c[:3] == ["git", "push", "--delete"]]
