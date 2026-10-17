#!/usr/bin/env python3
"""Resolve WHICH mechanism gives this host an author-blind reviewer (issue #1983).

WHY THIS EXISTS. `sigma-review` opened by ASSERTING "you did not write this code" -- true when
/sigma-loop dispatches the gate, false whenever the authoring session runs /sigma-review itself.
An unenforceable assertion in prose is exactly what that skill's own evidence rule bans.

WHERE THE BOUNDARY IS. `breaker.py`'s `no_channel()` already states the half this module supplies:
"that the ORCHESTRATOR spawns the breaker, never the maker -- is a spawn topology this function
cannot observe from a string ... and must be enforced at the dispatch site." This IS that site. It
does not re-implement breaker's leak checks; it calls them (see `check`).

HOST-AGNOSTIC BY BRANCHING, NOT BY LOWEST COMMON DENOMINATOR. Each host reaches the SAME property --
a model context that never saw the author's reasoning reads the brief -- by its own best route. No
branch is a downgrade. `inline` is the only degradation, and it is per-MACHINE, never per-host: a
Claude machine with no marker degrades exactly as a Cursor machine without `cursor-agent` does.

AUTO-DETECTION uses observed session markers only. Claude's markers retain precedence;
`CODEX_SESSION_ID`/`CODEX_THREAD_ID` identify Codex on a real Codex host. Cursor still needs
`review.host` because no marker has been verified there. A missing CLI is named, never guessed.

Fail-open: every failure resolves `inline` WITH A STATED REASON and exit 0 -- a broken resolver must
not stop a review. `check` is the opposite and fails CLOSED, because a leak guard that passes on
error is decoration. Zero deps.
"""
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import shlex
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid

#: What the SKILL prose branches on.
MECHANISMS = ("subagent", "process", "command", "inline")

#: Legal `review.host` values. An unknown value is a loud `inline`, never a guess.
HOSTS = ("auto", "claude", "cursor", "codex", "command", "inline")

#: Per-host invocation, and whether that branch has been RUN end to end BY THIS PROJECT -- NOT
#: whether this machine can run it (that is what the PATH check answers, separately). The two are
#: different questions and conflating them would let an unproven branch look proven merely because
#: its binary is installed. `verified` remains False for Codex until an entire review gate, including
#: its brief and verdict, has been exercised on the actual host.
_HOST_COMMANDS = {
    "cursor": (["cursor-agent", "-p", "--output-format", "text"], False),
    "codex": (["codex", "exec", "--sandbox", "read-only", "--ephemeral", "-"], False),
}


def _load(name):
    """Import a sibling script by path -- the kit's standard zero-install module loader. Legal here
    only because `breaker.py` lives in this same skill; a DIFFERENT skill must shell out to the CLI
    instead (north-star Architecture Rule 3)."""
    spec = importlib.util.spec_from_file_location(
        name, pathlib.Path(__file__).resolve().parent / (name + ".py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


_GUARD = []


def _guard():
    """resource_guard, loaded once (#1738): the one place a model session asks for admission."""
    if not _GUARD:
        _GUARD.append(_load("resource_guard"))
    return _GUARD[0]


def _config(sdlc_dir):
    try:
        return json.loads((pathlib.Path(sdlc_dir) / "config.json").read_text(encoding="utf-8"))
    except Exception:               # noqa: BLE001 - fail-open by design; see module docstring
        return {}


def _out(mechanism, host, reason, command=None, verified=True, timeout=900):
    got = {"mechanism": mechanism, "host": host, "verified": verified, "reason": reason,
           "timeout_seconds": timeout}
    if command is not None:
        got["command"] = command
    return got


shell_policy = _load("shell_policy")      # #707


def resolve(sdlc_dir, env=None, which=None):
    """-> the dict the SKILL prose branches on. Never raises; never returns an unknown mechanism."""
    env = os.environ if env is None else env
    which = shutil.which if which is None else which
    cfg = _config(sdlc_dir)
    review = cfg.get("review") if isinstance(cfg, dict) else None
    # EVERY read below is coerced. `_config` guarded only the read+parse, so a config that WAS valid
    # JSON but the wrong shape still crashed the gate -- and a list-valued `command` is not
    # contrived, since this function's own `process` output carries `command` as a list.
    review = review if isinstance(review, dict) else {}
    host = str(review.get("host") or "auto").strip()
    codex_here = (env.get("SIGMA_HOST") == "codex" or
                  (env.get("SIGMA_HOST") != "claude" and
                   (env.get("CODEX_SESSION_ID") or env.get("CODEX_THREAD_ID")) and
                   not (env.get("CLAUDECODE") or env.get("CLAUDE_CODE_SESSION_ID"))))
    overrides = review.get("host_overrides")
    if codex_here and isinstance(overrides, dict) and "codex" in overrides:
        host = str(overrides["codex"]).strip()
    command = str(review.get("command") or "").strip()
    try:
        timeout = int(review.get("timeout_seconds") or 900)
    except (TypeError, ValueError):
        timeout = 900

    if review.get("independent") is False:
        # The loop already honours this flag; without it here, a direct /sigma-review told the agent
        # to dispatch while doctor reported "a fresh reviewer is not spawned" for the same config.
        return _out("inline", "other", "review.independent is false (operator opted out)",
                    timeout=timeout)
    if host not in HOSTS:
        return _out("inline", "other", "unknown review.host %r; expected one of %s"
                    % (host, ", ".join(HOSTS)), timeout=timeout)
    if host == "inline":
        return _out("inline", "other", "review.host is inline (operator's explicit choice)",
                    timeout=timeout)
    if host == "command" or (host == "auto" and command):
        if not command:
            return _out("inline", "other", "review.host is command but review.command is empty",
                        timeout=timeout)
        # Unverified for the same reason codex is: no review has been run through an operator
        # command end to end by this project. `verified` is about THIS PROJECT's evidence.
        return _out("command", "other", "review.command configured", command=command,
                    verified=False, timeout=timeout)
    if host in _HOST_COMMANDS:
        argv, verified = _HOST_COMMANDS[host]
        if not which(argv[0]):
            return _out("inline", host,
                        "review.host is %s but %s is not on PATH" % (host, argv[0]),
                        timeout=timeout)
        return _out("process", host, "%s resolved on PATH" % argv[0],
                    command=list(argv), verified=verified, timeout=timeout)
    if env.get("CLAUDECODE") or env.get("CLAUDE_CODE_SESSION_ID"):
        return _out("subagent", "claude", "host dispatches its own subagents", timeout=timeout)
    if host == "auto" and (env.get("CODEX_SESSION_ID") or env.get("CODEX_THREAD_ID")):
        argv, verified = _HOST_COMMANDS["codex"]
        if not which(argv[0]):
            return _out("inline", "codex", "Codex session detected but codex is not on PATH",
                        timeout=timeout)
        return _out("process", "codex", "Codex session marker and codex CLI resolved",
                    command=list(argv), verified=verified, timeout=timeout)
    if host == "claude":
        # Symmetric with the PATH check above: an explicit host that cannot actually be reached
        # degrades loudly. Handing back `subagent` where none can be spawned produces an inline
        # self-review stamped "dispatched subagent", which is the failure this module prevents.
        return _out("inline", "claude",
                    "review.host is claude but no CLAUDECODE / CLAUDE_CODE_SESSION_ID marker is set",
                    timeout=timeout)
    return _out("inline", "other",
                "no host marker in the environment and no review.command configured",
                timeout=timeout)


#: breaker's finding for implementation content, which does NOT apply to a reviewer. See `check`.
_IMPLEMENTATION_FINDING = "implementation content reached the brief"


def check(brief_path, scratch_paths=()):
    """-> (ok, findings). The dispatch-site half of breaker.py's guarantee, for REVIEWS.

    `no_channel`'s own docstring names this as the requirement it cannot check from a string, because
    a string cannot reveal who spawned whom. So the check lives here, at the site that actually
    spawns the reviewer -- and it CALLS breaker rather than cloning its patterns, so a tightening
    of the shared-scratch rule is inherited rather than missed.

    BUT A REVIEWER IS NOT A BREAKER, AND ONE OF BREAKER'S TWO FINDINGS MUST NOT BE APPLIED HERE.
    A breaker must never see the implementation -- that is its whole isolation property. A code
    reviewer MUST see the diff: it is the artifact under review. `_LEAK` also matches any line
    starting with `-` (breaker.brief() switched its own bullets to `*` for exactly this reason) and
    any prose containing `def `/`class `/`import `/`return `, so a markdown review brief trips it on
    formatting alone. Measured, not assumed: the real #1983 brief was flagged on its first run.
    Carrying that through would be the false positive breaker's own comment warns about -- "exactly
    the sort of false positive that gets a real guard switched off".

    What DOES bind a reviewer is the shared-scratch rule: a brief naming a scratch file the maker can
    write is a maker->reviewer channel whatever the artifact is. The other half of the reviewer's
    isolation -- that the maker's reasoning never arrives -- is structural in `review_context.py`,
    which has no field to carry it, and is not something a string check could confirm anyway.

    Fails CLOSED, unlike `resolve`: a brief that could not even be read has not been checked, and a
    leak guard that reports ok when it checked nothing is decoration."""
    try:
        text = pathlib.Path(brief_path).read_text(encoding="utf-8")
    except OSError as exc:
        return (False, ["brief unreadable, so nothing was checked: %s" % exc])
    return _check_brief_text(text, scratch_paths)


def _check_brief_text(text, scratch_paths):
    """Check the bytes that will actually be dispatched, without a second file read."""
    _, findings = _load("breaker").no_channel(text, scratch_paths)
    findings = [f for f in findings if f != _IMPLEMENTATION_FINDING]
    return (not findings, findings)


def _stop_review_process(proc):
    """Kill the dedicated review process group, then reap its leader on every failure path."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        proc.kill()
    try:
        proc.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        # A process that deliberately detached from our group may still hold a pipe. Close our
        # descriptors rather than letting cleanup itself wait forever; the direct child is reaped.
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None:
                pipe.close()
        proc.kill()
        proc.wait(timeout=2)


#: A review verdict is a line that is NOTHING but the verdict. Prose such as "Verdict: approve would be
#: premature" ahead of a final `VERDICT: block` must not count (the checker could flip its own block).
_VERDICT_LINE = re.compile(
    r"(?im)^[\s*_`]*verdict[\s*_`]*:[\s*_`]*(approve|approved|block|blocked|unblock|unblocked)[\s*_`.]*$")
_PLAN_SHA_LINE = re.compile(r"(?m)^Plan sha256: ([0-9a-f]{64})\s*$")

#: Phases whose approving verdict needs a run receipt (the names `phase_gate.record_verdict` uses).
RECEIPT_PHASES = ("plan_review", "review")


def parse_verdicts(output):
    """-> the set of normalised standalone verdicts (approve|block|unblock) found in `output`."""
    return {"approve" if m.startswith("approv") else "unblock" if m.startswith("unblock") else "block"
            for m in (x.group(1).lower() for x in _VERDICT_LINE.finditer(output))}


def lone_verdict(output):
    """The ONE verdict `output` prints, or None for none or for conflicting ones. Never picks among
    several: a reviewer that prints both `approve` and `block` has not approved."""
    found = parse_verdicts(output)
    return next(iter(found)) if len(found) == 1 else None


#: Phase -> the `# Independent review brief - <name>` header a brief for it carries.
_BRIEF_NAME = {"plan_review": "plan-review", "review": "code-review"}
_BRIEF_HEADER = re.compile(r"(?m)\A# Independent review brief - (\S+)")
#: Ceilings for the code-review key. Past either, the checkout cannot be bound cheaply, so the run
#: FAILS (`content_unbound`, naming which) rather than approving something unbound.
_MAX_UNTRACKED = 5000
_KEY_SECONDS = 120.0

#: git for a READ of a repository we do not trust: no fsmonitor hook, no hooks path, no external diff,
#: no optional index-refresh lock. Argv list only, never a shell; paths always follow `--`.
_GIT = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
        "-c", "diff.external="]


class _Unbound(Exception):
    """The checkout cannot be covered by the key; the message is the cause shown to a person."""


def _frame(kind, name, inner):
    """One typed, length-prefixed record: kind byte, 8-byte name length, name, 32-byte inner digest.
    Inner digests are fixed size, so no content can imitate the next record (a file whose bytes look
    like a record header, an empty file vs a missing one, a name containing any separator)."""
    return kind + len(name).to_bytes(8, "big") + name + inner


def _over_budget(deadline):
    """ONE overall budget, checked on every record -- regular files, symlinks and absent paths alike."""
    if time.monotonic() > deadline:
        raise _Unbound("diff too large or slow to hash within %ds: commit or stash the large "
                       "change, or split it" % _KEY_SECONDS)


def _open_checked(path, st):
    """Open `path` O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC and require the opened file to be the one `st` (an lstat
    taken first) described: same device and inode, regular. A path swapped for a link (bytes from outside
    the checkout) or a FIFO (a blocking read) in between is refused, never read."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise _Unbound("%r changed while it was being hashed or is a link (%s)" % (path, exc.strerror))
    opened = os.fstat(fd)
    if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino):
        os.close(fd)
        raise _Unbound("%r changed while it was being hashed or is not a regular file (a FIFO, link or "
                       "device); remove it or git-ignore it" % path)
    return fd


def _read_head(path, deadline):
    """The first 4 KB of a nested repository's `.git/HEAD`, read with the same checks as `_file_record`:
    never followed through a link, never opened if it is a FIFO or other non-regular file."""
    _over_budget(deadline)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return b""
    except OSError as exc:
        raise _Unbound("a nested repository's HEAD cannot be read (%r: %s)" % (path, exc.strerror))
    if not stat.S_ISREG(st.st_mode):
        raise _Unbound("%r is not a regular file (a link, FIFO or device): a nested repository's HEAD must "
                       "be a plain file; remove or git-ignore that repository" % path)
    fd = _open_checked(path, st)
    try:
        return os.read(fd, 4096)
    except OSError as exc:
        raise _Unbound("a nested repository's HEAD cannot be read (%r: %s)" % (path, exc.strerror))
    finally:
        os.close(fd)


def _ref_path_is_clean(gitdir, ref):
    """Validate the ref name a nested HEAD names and refuse any symlink among its parent directories
    under `.git`, so reading it cannot reach a file OUTSIDE the checkout."""
    text = ref.decode("utf-8", "replace")
    if (not text.startswith("refs/") or ".." in text or text.startswith("/") or "\\" in text
            or "\0" in text or not all(text.split("/"))):
        raise _Unbound("a nested repository's HEAD names an unusable ref (%r)" % text[:80])
    current = gitdir
    for part in text.split("/")[:-1]:
        current = os.path.join(current, part)
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(st.st_mode):
            raise _Unbound("%r is a symlink inside a nested repository's .git, so its ref would be read from "
                           "outside the checkout: restore the directory" % current)


def _file_record(path, deadline):
    """-> (kind, inner32) for one path WITHOUT following links or running git: a regular file's bytes
    (kind F, or X when executable), a symlink's target (L), absent (G). The file is opened
    O_NOFOLLOW|O_NONBLOCK and its fstat must match the lstat taken first (same device, inode, regular),
    so a path swapped for a symlink (bytes from outside the checkout) or a FIFO (a blocking read) between
    the two is refused, never read. Anything that is not a regular file, symlink or directory is
    refused with a named cause."""
    _over_budget(deadline)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return b"G", hashlib.sha256(b"").digest()
    except OSError as exc:
        raise _Unbound("a path cannot be read (%r: %s)" % (path, exc.strerror))
    if stat.S_ISLNK(st.st_mode):
        return b"L", hashlib.sha256(os.fsencode(os.readlink(path))).digest()
    if not stat.S_ISREG(st.st_mode):
        raise _Unbound("%r is not a regular file, directory or symlink (a FIFO, socket or device) and "
                       "cannot be covered: remove it or git-ignore it" % path)
    fd = _open_checked(path, st)
    inner = hashlib.sha256()
    try:
        while True:
            _over_budget(deadline)
            try:
                chunk = os.read(fd, 1 << 20)
            except OSError as exc:
                raise _Unbound("a path cannot be read (%r: %s)" % (path, exc.strerror))
            if not chunk:
                break
            inner.update(chunk)
    finally:
        os.close(fd)
    return (b"X" if st.st_mode & 0o111 else b"F"), inner.digest()


def _parents_are_real(cwd, name, seen):
    """Refuse a listed path whose parent directory is a symlink: files under it would be read from
    OUTSIDE the checkout (a tracked directory replaced by a link to ../elsewhere)."""
    current = cwd
    for part in name.split(b"/")[:-1]:
        if not part:
            continue
        current = os.path.join(current, os.fsdecode(part))
        if current in seen:
            continue
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(st.st_mode):
            raise _Unbound("%r is a symlink, so files under it would be read from outside the checkout: "
                           "restore the directory" % current)
        seen.add(current)


def _nested_digest(path, deadline, budget):
    """Digest of a nested repository or submodule checkout WITHOUT running git in it (its own config
    could name an fsmonitor or hook command): every working file, plus its HEAD, packed-refs and the
    ref HEAD names, read as plain files. Not covered: its object store."""
    out = hashlib.sha256()

    def one(rel, full):
        _over_budget(deadline)
        budget[0] += 1
        if budget[0] > _MAX_UNTRACKED:
            raise _Unbound("a nested repository has more than %d files: commit, remove or git-ignore it"
                           % _MAX_UNTRACKED)
        kind, inner = _file_record(full, deadline)
        out.update(_frame(kind, rel, inner))

    def walk(directory, prefix):
        for entry in sorted(os.scandir(directory), key=lambda e: e.name):
            rel = prefix + os.fsencode(entry.name)
            if prefix == b"" and entry.name == ".git":
                continue
            if entry.is_dir(follow_symlinks=False):
                walk(entry.path, rel + b"/")
            else:
                one(rel, entry.path)
    try:
        walk(path, b"")
    except OSError as exc:
        raise _Unbound("a nested repository cannot be read (%r: %s)" % (path, exc.strerror))
    gitdir = os.path.join(path, ".git")
    if os.path.isdir(gitdir) and not os.path.islink(gitdir):
        names = [b".git/HEAD", b".git/packed-refs"]
        text = _read_head(os.path.join(gitdir, "HEAD"), deadline)
        if text.startswith(b"ref: "):
            ref = text[5:].strip()
            _ref_path_is_clean(gitdir, ref)
            names.append(b".git/" + ref)
        for rel in names:
            kind, inner = _file_record(os.path.join(path, os.fsdecode(rel)), deadline)
            out.update(_frame(kind, rel, inner))
    else:                               # a gitfile (submodule / linked worktree): its pointer text
        kind, inner = _file_record(gitdir, deadline)
        out.update(_frame(kind, b".git", inner))
    return out.digest()


def _content_key(sdlc_dir, goal):
    """-> (key, cause). `key` is a sha256 over typed, length-prefixed records (see `_frame`) of the goal's
    checkout as a code reviewer sees it: HEAD (or "unborn"); every tracked path
    (every one hashed from its working bytes by Python, never by git: exec bit, symlink target, deletion); every untracked non-ignored path; nested
    repositories by their working files and refs, read WITHOUT running git there. Only
    `<sdlc>/state` (the loop's own records, this receipt included) and `<sdlc>/work` (the maker's
    scratch) are left out; plans, research and config under `.sdlc` ARE covered. `key` is None, with
    `cause` naming why in words a person can act on, when something cannot be covered; both callers
    FAIL CLOSED on that. Not covered: ignored files, a nested repository's object store."""
    try:
        stem = pathlib.Path(str(goal)).stem if str(goal).endswith(".md") else str(goal)
        if _load("state").unsafe_goal_reason(stem):
            return None, "the goal name %r is not usable" % (goal,)
        p = pathlib.Path(sdlc_dir) / "state" / "work" / ("%s.json" % stem)
        try:                       # the work record, read directly: importing work.py per call costs ~16 MB
            rec = json.loads(p.read_text())
            rec = rec if isinstance(rec, dict) else {}
        except (OSError, ValueError):               # no record means the project root, below
            rec = {}
        cwd = str(rec.get("worktree") or pathlib.Path(sdlc_dir).resolve().parent)
        if not os.path.isdir(cwd):
            return None, "the checkout %r does not exist" % cwd
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        root, sd = os.path.realpath(cwd), os.path.realpath(str(sdlc_dir))
        rel = os.path.relpath(sd, root) if sd.startswith(root + os.sep) else None
        spec = ["--", "."] + ([":(exclude,literal)%s/%s" % (rel, x) for x in ("state", "work")] if rel else [])
        deadline = time.monotonic() + _KEY_SECONDS

        def git(*args):
            got = subprocess.run([*_GIT, "-C", cwd, *args], capture_output=True,
                                 timeout=max(1.0, _KEY_SECONDS), env=env)
            return got.returncode, got.stdout
        if git("rev-parse", "--is-inside-work-tree")[0]:
            return None, "this directory is not a git checkout"
        code, head = git("rev-parse", "--verify", "-q", "HEAD")
        digest = hashlib.sha256()
        digest.update(_frame(b"H", b"HEAD", hashlib.sha256(head.strip() if code == 0 else b"unborn").digest()))
        # Tracked paths: git only LISTS them (reading the index runs no filter); Python hashes the working
        # bytes itself. `git diff-index`/`diff-files` would hash stat-dirty files and run the repository's
        # clean/process filters (code named by .gitattributes + config) -- measured, and refused here.
        code, listing = git("ls-files", "-z", *spec)
        if code:
            return None, "git could not list this checkout's tracked files"
        changed = sorted(n for n in listing.split(b"\0") if n)
        code, listing = git("ls-files", "--others", "--exclude-standard", "-z", *spec)
        if code:
            return None, "git could not list this checkout's untracked files"
        names = sorted(n for n in listing.split(b"\0") if n)
        if len(names) > _MAX_UNTRACKED:
            return None, ("there are %d untracked files (limit %d): commit, remove or git-ignore build "
                          "output" % (len(names), _MAX_UNTRACKED))
        budget = [len(names)]
        seen = set()
        for section, group in ((b"t", changed), (b"u", names)):
            for name in group:
                _parents_are_real(cwd, name, seen)
                full = os.path.join(cwd, os.fsdecode(name))
                if os.path.isdir(full) and not os.path.islink(full):    # nested repo / submodule
                    digest.update(_frame(section + b"R", name, _nested_digest(full, deadline, budget)))
                else:
                    kind, inner = _file_record(full, deadline)
                    digest.update(_frame(section + kind, name, inner))
    except _Unbound as exc:
        return None, str(exc)
    except subprocess.TimeoutExpired:
        return None, "git took longer than %ds on this checkout" % _KEY_SECONDS
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "git could not be run here (%s)" % type(exc).__name__
    return digest.hexdigest(), None


def _receipt_path(sdlc_dir, goal, phase):
    if phase not in RECEIPT_PHASES:
        raise ValueError("phase must be one of %s" % ", ".join(RECEIPT_PHASES))
    p = pathlib.Path(str(goal))
    goal_stem = p.stem if p.suffix == ".md" else str(goal)
    bad = _load("state").unsafe_goal_reason(goal_stem)
    if bad:
        raise ValueError("unsafe goal %r for the reviewer run receipt: %s" % (goal, bad))
    return pathlib.Path(sdlc_dir) / "state" / "reviewer-runs" / ("%s.%s.json" % (goal_stem, phase))


def _write_receipt(sdlc_dir, goal, phase, **fields):
    """ONE file per goal+phase, atomically replaced: the LATEST run's outcome is the only one that
    counts, so a later success clears a failure and a later failure overwrites a success. Bounded by
    construction (no log to prune); `work.py finish` removes a goal's receipts with its record."""
    path = _receipt_path(sdlc_dir, goal, phase)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields.update(at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), goal=str(goal), phase=phase)
    fd, tmp = tempfile.mkstemp(prefix=".run-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(fields, handle, sort_keys=True); handle.write("\n"); handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def clear_receipts(sdlc_dir, goal):
    for phase in RECEIPT_PHASES:
        try:
            _receipt_path(sdlc_dir, goal, phase).unlink(missing_ok=True)
        except (ValueError, OSError):
            pass


def _config_state(sdlc_dir):
    """-> (cfg_dict_or_None, unreadable). A missing config is the defaults; a damaged one is not."""
    try:
        cfg = json.loads((pathlib.Path(sdlc_dir) / "config.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, False
    except (OSError, ValueError):
        return None, True
    return (cfg, False) if isinstance(cfg, dict) else (None, True)


def _independent_on(cfg):
    review = cfg.get("review") if isinstance(cfg, dict) else None
    return not (isinstance(review, dict) and review.get("independent") is False)


def configured_reviewer_unavailable(cfg, route):
    """True when `route` is inline although the operator CONFIGURED a reviewer (any `review.host` other
    than `auto`/`inline`, or `review.command` under `auto`) -- the host's binary is missing, the command
    empty, the host unknown, no session marker. Decided from CONFIG, not from a flag `resolve` could
    forget to set. `host: inline` (the operator's explicit choice) and a machine with nothing configured
    are not this."""
    if route.get("mechanism") in ("process", "command", "subagent"):
        return False
    review = cfg.get("review") if isinstance(cfg, dict) else None
    review = review if isinstance(review, dict) else {}
    host = str(review.get("host") or "auto").strip()
    env = os.environ
    codex_here = (env.get("SIGMA_HOST") == "codex" or
                  (env.get("SIGMA_HOST") != "claude" and
                   (env.get("CODEX_SESSION_ID") or env.get("CODEX_THREAD_ID")) and
                   not (env.get("CLAUDECODE") or env.get("CLAUDE_CODE_SESSION_ID"))))
    overrides = review.get("host_overrides")
    if codex_here and isinstance(overrides, dict) and "codex" in overrides:   # as resolve() reads it
        host = str(overrides["codex"]).strip()
    return host != "inline" and (host != "auto" or bool(str(review.get("command") or "").strip()))


#: #1551 running-receipt bounds. A live run's receipt says `running`; a reader DERIVES its deadline as
#: started_at + timeout_seconds + _RUN_GRACE (never stored, so never NaN, inf or forged larger). The grace
#: covers the runner's post-reviewer work: `_stop_review_process` (<= 4 s) and, on `review`, `_content_key`'s
#: four sequential git calls at `_KEY_SECONDS` each, plus the hashing tail and fsync.
_RUN_GRACE = 4 * int(_KEY_SECONDS) + 60
_MAX_RUN_SECONDS = 86400
_CLOCK_SKEW = 60
_MAX_PID = 2 ** 31 - 1              # os.kill raises OverflowError (not OSError) above this
_FINDINGS_CAP = 32768               # encoded JSON bytes of the persisted findings
#: Below the one measured Codex `exec_command` yield (30.2 s), so a `wait` call is not itself yielded.
WAIT_DEFAULT_SECONDS = 20
_MISSING_RETRY = 5                  # a wait issued at launch may arrive before the running write


def _posix():
    return os.name == "posix"


def _pid_alive(pid):
    """Whether `pid` exists on this machine. Off POSIX nothing is probed (the deadline bounds it). Fails
    toward ALIVE on anything but "no such process": the derived deadline is the backstop. An unreaped
    zombie runner reads alive until that deadline."""
    if not _posix():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _int(x):
    return isinstance(x, int) and not isinstance(x, bool)


def _classify(got, now=None):
    """A `running` receipt stays running only while its runner is provably live; otherwise it reads
    `failed/not_completed`, KEEPING every other field (route, plan_sha256, content_key, run_id), so every
    existing fail-closed path applies unchanged. Any other outcome is returned as written."""
    if got.get("outcome") != "running":
        return got
    now = time.time() if now is None else now
    pid, started, span = got.get("pid"), got.get("started_at"), got.get("timeout_seconds")
    if not (_int(pid) and 0 < pid <= _MAX_PID and _int(started) and _int(span) and span <= _MAX_RUN_SECONDS
            and started <= now + _CLOCK_SKEW):
        detail = "malformed running receipt"
    elif not now < started + span + _RUN_GRACE:
        detail = "deadline passed"
    elif not _pid_alive(pid):
        detail = "runner pid %d gone" % pid
    else:
        return got
    return {**got, "outcome": "failed", "failure": "not_completed", "detail": detail}


def _wait_gesture(sdlc_dir, goal, phase, run_id):
    return ("reviewer.py wait %s --goal %s --phase %s --max-seconds %d --run-id %s"
            % (shlex.quote(str(sdlc_dir)), goal, phase, WAIT_DEFAULT_SECONDS, run_id))


def _wait_text(sdlc_dir, goal, phase, receipt):
    return ("reviewer still running for %s %s (pid %s): wait with `%s`; do not park"
            % (goal, phase, receipt.get("pid"), _wait_gesture(sdlc_dir, goal, phase, receipt.get("run_id"))))


def running_receipt(sdlc_dir, goal, phase):
    """-> the LIVE running receipt for goal+phase when its route applies to the current one, else None.
    Same shape of decision as `failed_receipt`: a gate seeing it says "wait", never approve, never park."""
    cfg, unreadable = _config_state(sdlc_dir)
    if not unreadable and not _independent_on(cfg):
        return None
    try:
        state, got = _read_receipt(sdlc_dir, goal, phase)
    except ValueError:
        return None
    if state == "ok" and got.get("outcome") == "running" and _route_applies(got, resolve(sdlc_dir)):
        return got
    return None


def wait_review(sdlc_dir, goal, phase, max_seconds, run_id=None, sleep=time.sleep, clock=time.monotonic):
    """Poll the receipt once a second for at most `max_seconds` -> (code, text): 0 FINISHED (any verdict;
    finished is not approved), 1 FAILED, 2 usage / unreadable / another run / still missing after a short
    retry, 3 RUNNING. The path is resolved once (it costs a state.py load); each poll is classified."""
    try:
        path = _receipt_path(sdlc_dir, goal, phase)
    except ValueError as exc:
        return 2, "reviewer wait: %s" % exc
    start = clock()
    while True:
        state, got = _read_receipt_at(path)
        elapsed = clock() - start
        if state == "unreadable":
            return 2, "reviewer wait: the receipt for %s %s is unreadable (%s)" % (goal, phase, got)
        if state == "missing":
            if elapsed >= min(_MISSING_RETRY, max_seconds):
                return 2, "reviewer wait: no reviewer run is on record for %s %s" % (goal, phase)
        elif run_id and got.get("run_id") != run_id:
            return 2, "reviewer wait: a different run is on record: %s" % got.get("run_id")
        elif got.get("outcome") == "ok":
            return 0, ("FINISHED verdict=%s (finished is not approved; only verdict=approve is)\n%s"
                       % (got.get("verdict"), got.get("findings") or ""))
        elif got.get("outcome") != "running":
            return 1, "FAILED %s: %s" % (got.get("failure") or got.get("outcome") or "unknown", got.get("detail") or "")
        elif elapsed >= max_seconds:
            now = time.time()
            return 3, ("RUNNING pid=%s run_id=%s elapsed=%ds deadline_in=%ds"
                       % (got["pid"], got.get("run_id"), now - got["started_at"],
                          got["started_at"] + got["timeout_seconds"] + _RUN_GRACE - now))
        sleep(1)


def _read_receipt(sdlc_dir, goal, phase):
    """-> ("missing", None) | ("unreadable", why) | ("ok", dict) -- `ok` meaning a readable object,
    whatever its outcome. The ONE door every reader uses, so a dead or expired `running` receipt reads
    `failed/not_completed` everywhere (`_classify`)."""
    return _read_receipt_at(_receipt_path(sdlc_dir, goal, phase))


def _read_receipt_at(path):
    try:
        got = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "missing", None
    except (OSError, ValueError) as exc:
        return "unreadable", str(exc)
    return ("ok", _classify(got)) if isinstance(got, dict) else ("unreadable", "not a JSON object")


def _route_tag(route):
    return {"mechanism": route.get("mechanism"), "host": route.get("host")}


_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_MAX_REASON = 500


def _waiver_valid(sdlc_dir, goal, phase, receipt, plan_sha256=""):
    """An accept-inline waiver covers ONLY the bytes the owner saw: the checkout's content key (code
    review) or the plan hash (plan-review). Anything edited since needs a fresh choice."""
    if phase == "review":
        key = receipt.get("content_key")
        return bool(key) and key == _content_key(sdlc_dir, goal)[0]
    return not plan_sha256 or receipt.get("plan_sha256") == plan_sha256


def waiver_reason(sdlc_dir, goal, phase, plan_sha256=""):
    """The bounded reason of a VALID accept-inline waiver that covers the inline route now, else None
    (stamped on the verdict row so it survives `work.py finish`, which deletes receipts)."""
    try:
        state, got = _read_receipt(sdlc_dir, goal, phase)
    except ValueError:
        return None
    if (state == "ok" and got.get("outcome") == "waived_inline" and resolve(sdlc_dir).get("mechanism") == "inline"
            and _waiver_valid(sdlc_dir, goal, phase, got, plan_sha256)):
        return got.get("reason")
    return None


def _failure_applies(receipt, route, stale_waiver=False):
    """Whether a failed receipt still bars an approval on the CURRENT route. A failure belongs to the route
    it was made under: another process/command host starts clean (its own run overwrites the receipt); the
    subagent route has its own proof (an agent id); INLINE needs the owner's recorded choice
    (`accept-inline`), never just a config edit. A receipt with no route (older) applies: fail closed."""
    if receipt.get("outcome") == "waived_inline":
        return stale_waiver and route.get("mechanism") == "inline"   # a waiver for other bytes no longer covers these
    if receipt.get("outcome") in ("ok", "running"):   # a LIVE run is "wait", never a failure (#1551)
        return False
    return _route_applies(receipt, route)


def _route_applies(receipt, route):
    """Whether a receipt made under its tagged route bears on the CURRENT route: never on subagent (its own
    proof); always on inline or for an untagged (older) receipt (fail closed); else only on a tag match."""
    tag = receipt.get("route")
    mech = route.get("mechanism")
    if mech == "subagent":
        return False
    if mech == "inline" or not isinstance(tag, dict):
        return True
    return tag.get("mechanism") == mech and tag.get("host") == route.get("host")


#: Failure kinds that are NOT the reviewer failing: the checkout could not be bound to the review, or
#: moved under it. The lever is to fix that and re-run, not to park.
_CHECKOUT_KINDS = ("content_unbound", "content_changed_during_run")


def failed_receipt(sdlc_dir, goal, phase):
    """-> (kind, detail) of a recorded run that did not finish OK AND still applies to the current route
    (see `_failure_applies`), else None. Ignored when the operator opted out (`review.independent:
    false`). An unreadable receipt counts as failed: fail closed."""
    cfg, unreadable = _config_state(sdlc_dir)
    if not unreadable and not _independent_on(cfg):
        return None
    try:
        state, got = _read_receipt(sdlc_dir, goal, phase)
    except ValueError:
        return None
    route = resolve(sdlc_dir)
    if state == "unreadable":
        return None if route.get("mechanism") == "subagent" else ("unreadable", got)
    stale = (state == "ok" and got.get("outcome") == "waived_inline"
             and not _waiver_valid(sdlc_dir, goal, phase, got))
    if state == "ok" and _failure_applies(got, route, stale_waiver=stale):
        return got.get("failure") or "unknown", got.get("detail") or ""
    return None


def subagent_transcript_refusal(sdlc_dir, goal, phase, verified):
    """After ANY recorded failed run for this goal+phase, a subagent-route approval must carry a
    transcript-backed agent id (`verified == "transcript"`): an id nothing could check (`unverified`: no
    readable session store) must not clear a failure by a config-and-env flip. No prior failure: None,
    so ordinary flows are not stricter."""
    if verified == "transcript":
        return None
    cfg, unreadable = _config_state(sdlc_dir)
    if not unreadable and not _independent_on(cfg):
        return None
    try:
        state, got = _read_receipt(sdlc_dir, goal, phase)
    except ValueError:
        return None
    if state == "unreadable":
        kind = "unreadable receipt"
    elif state == "ok" and got.get("outcome") == "running":     # route-agnostic, like the failure read
        return _wait_text(sdlc_dir, goal, phase, got)
    elif state == "ok" and got.get("outcome") not in ("ok", "waived_inline"):
        kind = got.get("failure") or "unknown"
    else:
        return None
    return ("an independent reviewer FAILED earlier for %s %s (%s), so a subagent approval needs an agent id "
            "backed by a real transcript, and this one could not be verified: dispatch the review from a real "
            "subagent so its transcript exists in this session's store, or have the owner record `reviewer.py "
            "accept-inline %s --goal %s --phase %s --reason \"<the owner's choice>\"` (an audit trail the "
            "OWNER runs, not an authority check; not the maker)" % (goal, phase, kind, sdlc_dir, goal, phase))


def accept_inline(sdlc_dir, goal, phase, reason):
    """The OWNER's explicit, recorded choice to review inline after an independent reviewer failed (the
    `choose inline review` answer in /sigma-unpark). AN AUDIT TRAIL THE OWNER RUNS, NOT AN AUTHORITY
    CHECK: anyone with a shell can call it, so the maker must not. Not a config edit: config must already
    say inline, the failure stays on the receipt, the reason (bounded, control characters stripped) is
    stored with it AND stamped on the verdict row, and the waiver covers only the checkout (code review) or
    plan (plan-review) as it is NOW. Refuses to launder: a route that is not inline, a configured reviewer
    that is merely missing, nothing to accept, or a second call for the same bytes."""
    reason = " ".join(_CONTROL.sub(" ", str(reason or "")).split())
    if not reason:
        raise ValueError("--reason is required: the owner's choice is recorded, never silent")
    if len(reason) > _MAX_REASON:
        raise ValueError("--reason is %d characters; the limit is %d" % (len(reason), _MAX_REASON))
    if phase not in RECEIPT_PHASES:
        raise ValueError("phase must be one of %s" % ", ".join(RECEIPT_PHASES))
    cfg, unreadable = _config_state(sdlc_dir)
    if unreadable:
        raise ValueError("%s/config.json is unreadable" % sdlc_dir)
    route = resolve(sdlc_dir)
    if configured_reviewer_unavailable(cfg, route):
        raise ValueError("the configured reviewer is missing here (%s): set review.host to `inline` in "
                         "config first if self-review is the owner's choice" % route.get("reason"))
    if route.get("mechanism") != "inline":
        raise ValueError("the resolved route is %s, not inline: set review.host to `inline` first, or "
                         "re-run the reviewer" % route.get("mechanism"))
    state, got = _read_receipt(sdlc_dir, goal, phase)
    if state == "ok" and got.get("outcome") == "running" and _route_applies(got, route):
        raise ValueError("reviewer still running (pid %s): wait for it or kill it first" % got.get("pid"))
    if state != "ok" or got.get("outcome") == "ok":
        raise ValueError("nothing to accept: no failed independent run is on record for %s %s" % (goal, phase))
    binding = {}
    if phase == "review":
        key, cause = _content_key(sdlc_dir, goal)
        if key is None:
            raise ValueError("this checkout cannot be bound to the owner's choice: %s" % cause)
        binding["content_key"] = key
    else:
        if not got.get("plan_sha256"):
            raise ValueError("no plan hash is on record for this failed run: re-run reviewer.py run first")
        binding["plan_sha256"] = got["plan_sha256"]
    if got.get("outcome") == "waived_inline" and _waiver_valid(sdlc_dir, goal, phase, got):
        raise ValueError("already recorded for these bytes (reason: %s): the first reason stands"
                         % got.get("reason"))
    _write_receipt(sdlc_dir, goal, phase, outcome="waived_inline", failure=got.get("failure"),
                   reason=reason, route=_route_tag(route), **binding)
    return _read_receipt(sdlc_dir, goal, phase)[1]


def inline_review_refusal(sdlc_dir, goal, route):
    """For the PR-review generation path's inline branch: refuse an inline route the operator configured away from, or one after
    a failed code-review run for this goal, whatever the live route says. None otherwise."""
    live = running_receipt(sdlc_dir, goal, "review")
    if live:
        return _wait_text(sdlc_dir, goal, "review", live)
    failed = failed_receipt(sdlc_dir, goal, "review")
    if failed:
        return ("the independent reviewer FAILED for %s (%s): an inline self-review does not replace it. "
                "Park the goal (`--qkind reviewer_down`), fix the reviewer and re-run it; or, if the owner "
                "chose inline review, record that with `reviewer.py accept-inline %s --goal %s --phase review "
                "--reason \"<why>\"` (an audit trail the owner runs, not an authority check; not the maker)"
                % (goal, failed[0], sdlc_dir, goal))
    cfg, unreadable = _config_state(sdlc_dir)
    if unreadable:
        return "%s/config.json is unreadable: no inline approval until it parses" % sdlc_dir
    if _independent_on(cfg) and configured_reviewer_unavailable(cfg, route):
        return ("the configured reviewer is not installed or not usable here (%s): install or log in to it, "
                "or set review.host to `inline` explicitly" % route.get("reason"))
    return None


def _failed_message(sdlc_dir, goal, phase, kind, detail, gesture):
    if kind in _CHECKOUT_KINDS:
        return ("the independent review could not be bound to this checkout for %s %s (%s%s): this is not "
                "the reviewer failing, so do not park. Fix that, then re-run `%s`"
                % (goal, phase, kind, ": " + detail if detail else "", gesture))
    return ("the independent reviewer FAILED for %s %s (%s): no approving verdict can be recorded, "
            "and an inline self-review does not replace it, whatever route resolves now. Park the goal "
            "(`loop.py record %s %s parked \"independent reviewer failed: %s\" --qkind reviewer_down`); "
            "to resume, fix the reviewer (login, command, timeout) and re-run `%s`, or point review.host at "
            "another reviewer and run that"
            % (goal, phase, kind, sdlc_dir, goal, kind, gesture))


def independent_run_refusal(sdlc_dir, goal, phase, plan_sha256=""):
    """None, or why an APPROVING `phase` verdict may not be recorded for `goal` (AGENTS.md: a failed
    independent reviewer is never an approval).

    Order matters. (1) A damaged config refuses: `resolve()` fails open to `inline` and a verdict gate
    must not inherit that. (2) With `review.independent` on, a recorded run that did not finish OK
    refuses WHATEVER route resolves now -- the reviewer binary may have vanished since, turning the
    route inline, and that must not launder the failure. (3) An inline answer caused by something the
    operator configured being unavailable refuses, loudly (decided from config, not a flag); an inline answer the operator
    chose, or that nothing was configured for, stays allowed. (4) On a process/command route the
    approval must match a receipt the runner wrote -- never the agent restating a verdict -- bound to
    the brief's phase, the plan bytes (plan-review) or the checkout (code review). Proves the process
    ran and approved these bytes; a maker can still call the CLI by hand or write the file."""
    cfg, unreadable = _config_state(sdlc_dir)
    if unreadable:
        return ("%s/config.json is unreadable, so whether review.independent is on cannot be told: no "
                "approving verdict is recorded until it parses" % sdlc_dir)
    if not _independent_on(cfg):
        return None
    gesture = ("reviewer.py run %s <brief> --scratch <dir> --goal %s --phase %s" % (sdlc_dir, goal, phase))
    try:
        state, receipt = _read_receipt(sdlc_dir, goal, phase)
    except ValueError as exc:
        return "no approval for an unusable goal or phase (%s)" % exc
    route = resolve(sdlc_dir)
    mechanism = route.get("mechanism")
    if mechanism == "subagent":
        return None                      # bound by its own proof (an agent id), not by a process receipt
    if state == "unreadable":
        return "the reviewer run receipt for %s %s is unreadable (%s): re-run `%s`" % (goal, phase, receipt, gesture)
    if state == "ok" and receipt.get("outcome") == "running" and _route_applies(receipt, route):
        return _wait_text(sdlc_dir, goal, phase, receipt)
    stale = (state == "ok" and receipt.get("outcome") == "waived_inline" and mechanism == "inline"
             and not _waiver_valid(sdlc_dir, goal, phase, receipt, plan_sha256))
    applies = state == "ok" and _failure_applies(receipt, route, stale_waiver=stale)
    accept = ("`reviewer.py accept-inline %s --goal %s --phase %s --reason \"<the owner's choice>\"` (an audit "
              "trail the OWNER runs, not an authority check: not the maker)" % (sdlc_dir, goal, phase))
    if mechanism not in ("process", "command"):
        if configured_reviewer_unavailable(cfg, route):
            earlier = (" (an earlier run FAILED: %s)" % (receipt.get("failure") or "unknown")) if applies else ""
            return ("the configured reviewer is not installed or not usable here (%s)%s, so no approving "
                    "verdict is recorded: install or log in to it; or, if self-review is the owner's choice, set "
                    "review.host to `inline`%s" % (route.get("reason"), earlier,
                                                     " and then run " + accept if applies else ""))
        if applies:
            return ("the independent reviewer FAILED for %s %s (%s) and this machine now reviews inline: an "
                    "inline self-review does not replace it unless the owner chooses it. Record that choice with "
                    "%s, or fix the reviewer and re-run it" % (goal, phase, receipt.get("failure") or "unknown", accept))
        return None
    if applies:
        return _failed_message(sdlc_dir, goal, phase, receipt.get("failure") or "unknown",
                               receipt.get("detail") or "", gesture)
    if state == "missing" or receipt.get("outcome") != "ok":
        return ("the resolved reviewer route is `%s` but no run of it is on record for %s %s, so no "
                "approving verdict can be recorded (a self-review is not independent and is not counted): "
                "run `%s` and record what it printed" % (mechanism, goal, phase, gesture))
    if receipt.get("verdict") != "approve":
        return ("the independent reviewer's last run for %s %s did not print a lone `VERDICT: approve` "
                "(got %r): an approval cannot be recorded; address its findings and re-run `%s`"
                % (goal, phase, receipt.get("verdict"), gesture))
    if receipt.get("brief_phase") != _BRIEF_NAME.get(phase):
        return ("the reviewer's last run for %s %s was given a %r brief, not a %s one: re-run `%s`"
                % (goal, phase, receipt.get("brief_phase"), _BRIEF_NAME.get(phase), gesture))
    if phase == "review":
        key = receipt.get("content_key")
        if not key or key != _content_key(sdlc_dir, goal)[0]:
            return ("the checkout changed since the independent reviewer last ran for %s (or could not be "
                    "bound to it): re-run `%s` and record that verdict" % (goal, gesture))
    if phase == "plan_review" and (not plan_sha256 or receipt.get("plan_sha256") != plan_sha256):
        return ("the independent reviewer approved a different plan than sha256 %s…: re-run `%s` on "
                "the current plan" % (plan_sha256[:12], gesture))
    return None


def run_review(sdlc_dir, brief_path, scratch_paths, goal=None, phase=None, hint=False):
    """Execute a resolved process/command review with a real timeout and fresh process group.

    The operator-owned `review.command` retains its shell semantics. `process` commands remain
    argv lists. Unsupported platforms refuse before spawning: killing only a shell while its
    children keep consuming quota would be a silent half-guarantee.

    With `goal` and `phase` it leaves the run receipt. A run refused beforehand (leak guard, shell
    policy, a bad timeout) -- a maker's typo, not a reviewer failure -- never overwrites an earlier good
    one. Past those refusals the receipt says `running` (this process's pid, started_at, timeout, a
    run_id nonce) until the run's ONE final write replaces it (`_finish_receipt`); a crash in between is
    finalised `not_completed`, and a SIGKILLed runner reads so via its dead pid (`_classify`). `hint`
    (the CLI only) prints the bounded `wait` gesture to stderr; an in-process caller's captured errors
    stay free of it. The reviewer keeps the repo cwd: it must read the repository.
    """
    if os.name != "posix" or not hasattr(os, "killpg"):
        print("reviewer run: process-tree timeout is unsupported on this platform", file=sys.stderr)
        return 2
    if not scratch_paths:
        print("reviewer run: at least one --scratch path is required for the leak check",
              file=sys.stderr)
        return 2
    route = resolve(sdlc_dir)
    mechanism = route["mechanism"]
    receipt = bool(goal and phase)
    if (goal or phase) and not receipt:
        print("reviewer run: --goal and --phase go together", file=sys.stderr)
        return 2
    if mechanism not in ("process", "command"):
        print(f"reviewer run: resolved {mechanism}; use that route instead ({route['reason']})",
              file=sys.stderr)
        return 2
    if receipt:
        try:
            _receipt_path(sdlc_dir, goal, phase)
        except ValueError as exc:
            print(f"reviewer run: cannot record the run receipt ({exc})", file=sys.stderr)
            return 2
    try:
        brief = pathlib.Path(brief_path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"LEAK: brief unreadable, so nothing was checked: {exc}", file=sys.stderr)
        return 1
    ok, findings = _check_brief_text(brief, scratch_paths)
    if not ok:
        for finding in findings:
            print(f"LEAK: {finding}", file=sys.stderr)
        return 1
    if mechanism == "command" and not shell_policy.repository_shell_commands_allowed(
            pathlib.Path(sdlc_dir).resolve().parent):
        # #707 (TM-04): `review.command` is a repository-supplied shell string, and its stdout is
        # accepted as the reviewer's verdict. Same gate as `verify.command`; no verdict is produced.
        print("reviewer run: " + shell_policy.refusal_message(), file=sys.stderr)
        return 2
    timeout = route["timeout_seconds"]
    if not isinstance(timeout, int) or timeout <= 0:
        print("reviewer run: review.timeout_seconds must be positive", file=sys.stderr)
        return 2
    if timeout > _MAX_RUN_SECONDS:
        print("reviewer run: review.timeout_seconds must be at most %d" % _MAX_RUN_SECONDS, file=sys.stderr)
        return 2
    command = route["command"]

    plan = _PLAN_SHA_LINE.search(brief)
    plan_sha = plan.group(1) if plan else None
    run_id, key, finished = uuid.uuid4().hex, None, False

    def finish(**fields):
        """The run's ONE final receipt write, under its own nonce; False when it did not land."""
        nonlocal finished
        finished = True
        return not receipt or _finish_receipt(sdlc_dir, goal, phase, run_id, route=_route_tag(route),
                                              plan_sha256=plan_sha, content_key=key, **fields)

    def failed(kind, code, message, detail=""):
        print(message, file=sys.stderr)
        finish(outcome="failed", failure=kind, detail=detail)
        return code
    if receipt:
        # Written BEFORE the pre-launch key step (it can take minutes on a large checkout), so a yielding
        # host never reads the previous run's receipt as this one's; the hint names the exact wait gesture.
        _write_receipt(sdlc_dir, goal, phase, outcome="running", pid=os.getpid(), started_at=int(time.time()),
                       timeout_seconds=timeout, run_id=run_id, route=_route_tag(route), plan_sha256=plan_sha)
        if hint:
            print("reviewer run: running pid %d run_id %s, deadline in %ds. If this call yields, do NOT park: %s "
                  "(exit 3 = still running; 0 = finished, read the verdict)"
                  % (os.getpid(), run_id, timeout + _RUN_GRACE, _wait_gesture(sdlc_dir, goal, phase, run_id)),
                  file=sys.stderr)
    try:
        if receipt and phase == "review":
            key, cause = _content_key(sdlc_dir, goal)
            if key is None:
                return failed("content_unbound", 1, "reviewer run: this checkout cannot be bound to the review: "
                              "%s. Fix that and re-run (nothing was sent to the reviewer)" % cause, cause)
        with _guard().session_lease("reviewer", goal or "run", ttl=timeout * 2 + 60,
                                    config=_config(sdlc_dir).get("resource")) as lease:        # #1738
            if lease is not None and hasattr(lease, "reason"):
                # A hold is "could not run now": the existing failed outcome, nothing spawned, never a verdict.
                return failed("held", 1, "reviewer run: a model session is held (%s); no verdict accepted"
                              % lease.reason, str(lease.reason))
            try:
                proc = subprocess.Popen(command, shell=mechanism == "command", stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                        start_new_session=True)
            except OSError as exc:
                return failed("start_failed", 1,
                              f"reviewer run: could not start reviewer process ({type(exc).__name__})")
            try:
                output, _errors = proc.communicate(input=brief, timeout=timeout)
            except subprocess.TimeoutExpired:
                _stop_review_process(proc)
                return failed("timeout", 124,
                              f"reviewer run: timed out after {timeout}s; reviewer process group stopped")
            except BaseException:
                _stop_review_process(proc)
                raise
        if proc.returncode != 0:
            return failed("nonzero_exit", 1,
                          f"reviewer run: reviewer exited with exit {proc.returncode}; no verdict accepted")
        if not output.strip():
            return failed("empty_output", 1, "reviewer run: reviewer returned empty output; no verdict accepted")
        if key is not None:
            after, cause = _content_key(sdlc_dir, goal)
            if after != key:
                return failed("content_changed_during_run", 1,
                              "reviewer run: the checkout changed while the reviewer ran (%s); no verdict accepted. "
                              "If the reviewer writes caches into the checkout, a re-run usually passes once those "
                              "files exist (or git-ignore them)" % (cause or "files differ"), cause or "")
        named = _BRIEF_HEADER.match(brief)
        if not finish(outcome="ok", verdict=lone_verdict(output),
                      brief_sha256=hashlib.sha256(brief.encode("utf-8")).hexdigest(),
                      output_sha256=hashlib.sha256(output.encode("utf-8")).hexdigest(),
                      brief_phase=named.group(1) if named else None, findings=_cap_findings(output)):
            return 1
        sys.stdout.write(output)
        return 0
    except BaseException:
        # Anything after the running write (a Popen ValueError, a crash in the key step or the verdict
        # parse, ^C) finalises not_completed -- unless the final write already landed (e.g. a broken stdout).
        if not finished:
            finish(outcome="failed", failure="not_completed", detail="the runner raised")
        raise


def _finish_receipt(sdlc_dir, goal, phase, run_id, **fields):
    """Write a run's final receipt ONLY if the on-disk receipt is still this run's (`run_id`): a cleared
    receipt (`work.py finish`) is not resurrected, and a newer run's is not clobbered. Reads the RAW file,
    not `_classify`. -> whether it wrote. The check-then-replace window is one atomic rename, unlocked:
    two concurrent runs for one goal+phase are not a supported flow."""
    path = _receipt_path(sdlc_dir, goal, phase)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print("reviewer run: the receipt was cleared while the reviewer ran; this outcome is not recorded",
              file=sys.stderr)
        return False
    except (OSError, ValueError):
        raw = None
    if not isinstance(raw, dict) or raw.get("run_id") != run_id:
        print("reviewer run: a different run (%s) is on record; this outcome is not recorded"
              % (raw.get("run_id") if isinstance(raw, dict) else "unreadable"), file=sys.stderr)
        return False
    _write_receipt(sdlc_dir, goal, phase, run_id=run_id, **fields)
    return True


def _cap_findings(text):
    """The reviewer's output, capped at `_FINDINGS_CAP` ENCODED bytes (as `_write_receipt` dumps it, with
    `ensure_ascii`), keeping the head: findings precede the verdict line, and the verdict is stored apart."""
    if len(json.dumps(text)) <= _FINDINGS_CAP:
        return text
    budget, n = _FINDINGS_CAP - 64, 0
    for ch in text:
        budget -= len(json.dumps(ch)) - 2
        if budget < 0:
            break
        n += 1
    return text[:n] + "...[truncated %d chars]" % (len(text) - n)


USAGE = ("usage: reviewer.py resolve <sdlc_dir> | check <brief_path> [--scratch <path>]... | "
         "run <sdlc_dir> <brief_path> --scratch <path> [--scratch <path>]... "
         "[--goal <goal> --phase plan_review|review] | "
         "accept-inline <sdlc_dir> --goal <goal> --phase plan_review|review --reason <text> | "
         "wait <sdlc_dir> --goal <goal> --phase plan_review|review [--max-seconds N] [--run-id ID] "
         "(exit 0 finished, any verdict; 1 failed; 2 usage or another run; 3 still running. Without --run-id, "
         "a wait issued before the new run's receipt is written reads the previous run's)")


def main(argv):
    if argv in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    if len(argv) >= 2 and argv[0] == "resolve":
        print(json.dumps(resolve(argv[1])))
        return 0
    if len(argv) >= 2 and argv[0] == "check":
        scratch = [argv[i + 1] for i, a in enumerate(argv) if a == "--scratch" and i + 1 < len(argv)]
        ok, findings = check(argv[1], scratch)
        for f in findings:
            print("LEAK: %s" % f, file=sys.stderr)
        return 0 if ok else 1
    if len(argv) >= 2 and argv[0] == "accept-inline":
        opts = argv[2:]
        if len(opts) % 2 or any(opts[i] not in ("--goal", "--phase", "--reason") for i in range(0, len(opts), 2)):
            print(USAGE, file=sys.stderr)
            return 2
        flags = {opts[i]: opts[i + 1] for i in range(0, len(opts), 2)}
        try:
            got = accept_inline(argv[1], flags.get("--goal"), flags.get("--phase"), flags.get("--reason"))
        except (ValueError, OSError) as exc:
            print("reviewer accept-inline: %s" % exc, file=sys.stderr)
            return 2
        print("reviewer accept-inline: recorded for %s %s (the earlier failure, %s, stays on record)"
              % (flags["--goal"], flags["--phase"], got.get("failure")), file=sys.stderr)
        return 0
    if len(argv) >= 3 and argv[0] == "run":
        options = argv[3:]
        if len(options) < 2 or len(options) % 2 or any(
                options[i] not in ("--scratch", "--goal", "--phase") for i in range(0, len(options), 2)):
            print(USAGE, file=sys.stderr)
            return 2
        pairs = [(options[i], options[i + 1]) for i in range(0, len(options), 2)]
        one = lambda flag: next((v for f, v in reversed(pairs) if f == flag), None)
        return run_review(argv[1], argv[2], [v for f, v in pairs if f == "--scratch"],
                          goal=one("--goal"), phase=one("--phase"), hint=True)
    if len(argv) >= 2 and argv[0] == "wait":
        opts = argv[2:]
        if len(opts) % 2 or any(opts[i] not in ("--goal", "--phase", "--max-seconds", "--run-id")
                                for i in range(0, len(opts), 2)):
            print(USAGE, file=sys.stderr)
            return 2
        flags = {opts[i]: opts[i + 1] for i in range(0, len(opts), 2)}
        try:
            bound = int(flags.get("--max-seconds", WAIT_DEFAULT_SECONDS))
        except ValueError:
            print("reviewer wait: --max-seconds must be an integer", file=sys.stderr)
            return 2
        if not flags.get("--goal"):
            print(USAGE, file=sys.stderr)
            return 2
        code, text = wait_review(argv[1], flags["--goal"], flags.get("--phase"), max(1, min(bound, 3600)),
                                 run_id=flags.get("--run-id"))
        print(text, file=sys.stdout if code in (0, 3) else sys.stderr)
        return code
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    # The script-time floor: this run's wall time, recorded to the session resolved at exit.
    raise SystemExit(_load("timing_store").timed_main(main, sys.argv[1:], "reviewer"))
