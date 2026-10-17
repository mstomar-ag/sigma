"""Run state: config, STATE.md counters, goal status transitions, review-queue append. Zero-dep.

Zero third-party dependency, not zero process: since #1897 `done_refusal` shells out to `git` (read
-only: `merge-base`, `diff --name-only`, `ls-files`) to ask what content a verify actually ran
against. A repo where git cannot answer degrades exactly as one with no work record does — see
`content_fingerprint`."""
import contextlib, hashlib, json, math, os, pathlib, importlib.util, re, subprocess, sys, tempfile, time

try:
    import fcntl                    # POSIX only -- see _cursor_lock's docstring
except ImportError:
    fcntl = None

_HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("frontmatter", _HERE / "frontmatter.py")
frontmatter = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(frontmatter)


class ConfigMissing(Exception):
    """Raised by load_config() when `.sdlc/config.json` itself does not exist — this directory was
    never `/sigma-init`'d (or `/sigma-setup`'d), as opposed to `_state_file()`'s per-run runtime state
    (gitignored by design, so ABSENT is the normal fresh-clone case and safe to scaffold on demand —
    see its own docstring). config.json is different: it is the one file that is never safe to
    default, because it carries the actual project choices (discovery source, ledger, verify
    command...) — silently inventing one would run the loop in a mode nobody chose, and could even
    misreport a genuinely never-set-up repo as an empty-but-configured backlog (`next` -> `DONE`)
    instead of surfacing the real problem. This is a distinct type (not a bare FileNotFoundError) so
    a CLI boundary can catch exactly THIS case — a directory needing /sigma-init — without also
    swallowing an unrelated FileNotFoundError raised elsewhere in the same call graph and misreporting
    it with the same "run /sigma-init" advice. See loop.py `main()`'s dispatch wrapper, the ONE place
    this is caught and turned into a clear one-line message instead of a raw traceback — every CLI
    verb here funnels through this same load_config(), so guarding it once covers all of them, not
    just whichever verb a bug report happens to name (#403)."""


class ConfigInvalid(json.JSONDecodeError):
    """`.sdlc/config.json` exists but is not valid JSON (D7 triage). A JSONDecodeError subclass on
    purpose: every caller and test that already catches the bare parse error keeps working, while a
    CLI boundary can catch THIS type and print the fix (`str()` is the whole advice, not the parser's
    bare 'Expecting value: line 1 column 1') instead of a traceback. Deliberately NOT a ConfigMissing:
    that one means 'run /sigma-init', and several callers treat it as 'use the defaults', which a
    corrupt file must never silently become."""

    def __init__(self, sdlc_dir, err):
        super().__init__(err.msg, err.doc, err.pos)
        self.sdlc_dir = str(sdlc_dir)
        path = pathlib.Path(sdlc_dir) / "config.json"
        self.advice = (f"{path} is not valid JSON ({err.msg}, line {err.lineno} column {err.colno}) -- "
                       f"fix that line (a trailing comma is the usual cause; `python3 -m json.tool {path}` "
                       "shows it), or move the file aside and run /sigma-init to scaffold a fresh one")

    def __str__(self):
        return self.advice

    def __reduce__(self):
        return (_rebuild_config_invalid, (self.sdlc_dir, self.msg, self.doc, self.pos))


def _rebuild_config_invalid(sdlc_dir, msg, doc, pos):
    return ConfigInvalid(sdlc_dir, json.JSONDecodeError(msg, doc, pos))


def load_config(sdlc_dir):
    try:
        text = (pathlib.Path(sdlc_dir) / "config.json").read_text()
    except FileNotFoundError:
        raise ConfigMissing(
            f"no config.json under {sdlc_dir} — this .sdlc directory was never initialized. "
            "Run /sigma-init to scaffold it (or /sigma-setup to adopt Sigma into an existing repo)."
        ) from None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigInvalid(sdlc_dir, exc) from None
    if not isinstance(parsed, dict):
        raise ConfigMissing(
            f"config.json under {sdlc_dir} does not contain a JSON object (is {type(parsed).__name__} instead). "
            "Run /sigma-init to scaffold a valid config (or /sigma-setup to adopt Sigma into an existing repo)."
        )
    return parsed


_STATE_TEMPLATE = """# Loop State

<!-- Machine-written by /sigma-loop. Do not hand-edit during a run. -->

iteration: 0
run_iteration: 0
last_run: none

## Items
<!-- per-item status lives in goals/*.md frontmatter -->
"""

_QUEUE_TEMPLATE = """# Morning Review Queue

<!-- Parked items from autonomous runs land here with full context. -->
<!-- Empty = nothing needs you. -->
"""

# At the default 20-goal handoff this is far above one run's phase count. An operator who
# disables handoff still gets a finite STATE.md rather than an ever-growing dedupe list.
_MAX_RUN_TOKEN_CREDITS = 4096


@contextlib.contextmanager
def phase_end_lock(sdlc_dir, phase_attempt):
    """Serialize a phase's cursor credit with its retryable ledger appends.

    Separate from STATE.md.lock, since `add_tokens_once` takes that lock inside this section.
    There are 256 fixed lock stripes. Identical attempts always share one stripe; a rare retry
    scans all event files while only its stripe waits. This repo had 2,361 event files / 9.3 MB
    on 2026-09-16, so that exceptional scan would read about 93 MB at 10x and 930 MB at 100x
    event volume, with constant memory. Refuse if the OS cannot lock.
    """
    stripe = phase_attempt_key(phase_attempt)[:2]
    path = pathlib.Path(sdlc_dir) / "state" / f"phase-end-{stripe}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
            locked = True
        elif os.name == "nt":
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            locked = True
        else:
            raise RuntimeError("phase-end locking is unavailable on this host")
        yield
    finally:
        if locked and fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        elif locked and os.name == "nt":
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        os.close(fd)


def _state_file(sdlc_dir):
    """The run cursor, created on demand.

    `.sdlc/state/` is gitignored by design (it is per-machine runtime), so a teammate who CLONES an
    adopted repo has the committed config and goals but no state files at all — and every entry point
    here used to die on a raw FileNotFoundError before their first goal. Scaffolding on demand costs
    nothing and is the difference between `/sigma-loop` working on a fresh clone and not; since #550
    that scaffold happens only on the LOCKED write path, and `load_cursor` reads the template's own
    defaults instead (see below).

    Exclusive-create (`O_CREAT | O_EXCL`), never a plain `if not exists: write_text(...)` (#531):
    the old check-then-write had its own unlocked TOCTOU gap — this function used to be reachable
    from `load_cursor`, deliberately lock-free, so a lock-free reader could see "not exists", lose a race
    to a locked `add_tokens` that creates AND writes a real value in between, then still go ahead and
    OVERWRITE that value with the blank template (reproduced: a locked writer publishes `run_tokens:
    7`, then this function's old body clobbers it back to the template). `O_EXCL` makes "already
    exists" a clean, atomic `FileExistsError` instead of a race to inspect-then-act — the losing side
    just walks away instead of clobbering the winner.

    ONLY LOCKED CALLERS MAY REACH THIS (#550). `load_cursor` used to call it too, and a scaffold on
    the lock-free read path is by definition an unlocked creator racing the locked writers: the
    reader could win the `O_EXCL` create, still be mid-template-write when a locked `add_tokens` got
    `FileExistsError`, read the empty file and publish a cursor built from nothing. The exclusive
    create bounded that to the first creation and kept it counter-lossless, but did not remove it.
    Publishing the template through `mkstemp` + `os.replace` inside the `else` branch does not fix
    it either — measured, it makes it WORSE: the reader's atomic replace of a blank template then
    lands AFTER the writer's publish and destroys a real counter value (0/5 losses before, 5/5
    after). The fix is to have no unlocked creator at all, so `load_cursor` now reads the template's
    defaults for an absent file instead of creating one.

    Mode 0o644 explicitly: `os.open` with no mode argument defaults to 0o777, so a scaffolded
    STATE.md landed executable (0o755 under a normal umask) until some later publish happened to
    rewrite it."""
    f = pathlib.Path(sdlc_dir) / "state" / "STATE.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(f), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        pass                          # another writer already scaffolded it -- never truncate it
    else:
        with os.fdopen(fd, "w") as fh:
            fh.write(_STATE_TEMPLATE)
    return f


@contextlib.contextmanager
def _cursor_lock(sdlc_dir):
    """Serializes every STATE.md cursor writer (`_patch_cursor`, and therefore `add_tokens` /
    `start_run` / `advance_cursor`) across processes AND threads, with the same kernel-mediated
    `fcntl.flock` primitive that closed the identical unlocked-RMW race in `_try_acquire_claim_lock`
    (loop.py, #387) — flock is atomic at the kernel level, so there is no read-then-act gap of its
    own for a second writer to land in.

    Locks a SIBLING file, `state/STATE.md.lock` — never STATE.md itself. `_patch_cursor` publishes
    via `os.replace`, which swaps the directory entry onto a NEW inode; a writer that flocked the OLD
    inode (opened before the replace) would be locking a file nothing points at anymore, so a lock ON
    STATE.md itself would stop actually excluding anyone the moment the first publish happens. A
    separate, never-replaced lock file has no such lifetime problem.

    BLOCKING (`LOCK_EX`, not `LOCK_NB`): unlike the claim lock (where "someone else already owns
    this goal" is a normal, expected outcome to skip past), the only writers ever contending here are
    cooperating cursor updates that both need to land — cursor writes are milliseconds, and silently
    SKIPPING a budget update is the very bug this closes. There is deliberately no timeout: the
    critical section (`_patch_cursor`'s read + the caller's `patch()` + temp-write + `os.replace`) is
    pure, fast, local file I/O with nothing in it that can hang — the caller-supplied `patch`
    callable must never do I/O of its own beyond computing from the text it is given, or that
    contract breaks.

    No fsync before `os.replace`: a torn publish is impossible either way (replace is atomic), the
    worst a same-instant power loss can do is lose the last write entirely and leave the PREVIOUS
    good file in place — an accepted trade-off already made the same way by `actionlog.py` and
    `work.py`'s own publish paths in this repo. `os.replace` overwrites unconditionally on Windows
    too (unlike bare `os.rename`), so the atomic-publish half of this fix needs no Windows-specific
    branch — only the locking half does.

    Fail-open, exactly mirroring `_try_acquire_claim_lock`'s documented reasoning: yields WITHOUT the
    lock when `fcntl` doesn't exist (Windows), when the lock file can't even be opened, or on any
    other OSError acquiring the flock (e.g. `ENOLCK` on a lock-less NFS mount) — a lock this call
    cannot manage must never be what stops the loop. Crash-release is free: the kernel drops the
    flock the instant the holding process's fd closes, for any reason, crash included."""
    if fcntl is None:
        yield                      # Windows / no fcntl at all -- fail open, identical posture to #387
        return
    lock_path = pathlib.Path(sdlc_dir) / "state" / "STATE.md.lock"
    try:
        refuse_symlinks(sdlc_dir, lock_path, create_parents=True)           # #708
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        yield                      # can't even open the lock file -- fail open, see docstring
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)              # BLOCKING (no LOCK_NB) -- see docstring
    except OSError:
        os.close(fd)
        yield                      # e.g. ENOLCK on a lock-less NFS mount -- fail open, see docstring
        return
    try:
        yield
    finally:
        os.close(fd)               # releases the flock too -- the kernel drops it the instant fd closes


def refuse_symlinks(sdlc_dir, rel, create_parents=False):
    """#708: lstat every component of `rel` below `sdlc_dir`; raise `UnsafeStatePath` (naming the fix)
    if any is a symlink, optionally creating missing parents one level at a time. `rel` may be a
    path under `sdlc_dir` (it is made relative). Returns the checked path. Writers whose call shape
    the write-surface/growth audits read statically call THIS before their unchanged write (a
    residual lstat->open window needs concurrent write access inside the checkout); everything else
    uses `safe_state_open`, which also opens the leaf with O_NOFOLLOW."""
    base = pathlib.Path(sdlc_dir)
    rel_path = pathlib.Path(rel)
    if rel_path.is_absolute():
        try:
            rel_path = rel_path.relative_to(base)
        except ValueError:
            rel_path = rel_path.relative_to(base.resolve())
    parts = rel_path.parts
    if not parts or ".." in parts:
        raise UnsafeStatePath(f"REFUSED: unsafe state path {str(rel)!r}")
    cur = base
    for i, part in enumerate(parts):
        cur = cur / part
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            if not create_parents:
                if i == len(parts) - 1:
                    continue
                return cur
            if i < len(parts) - 1:
                cur.mkdir(parents=True, exist_ok=True)      # the base may not exist yet; races are fine
                if os.path.islink(cur):
                    raise UnsafeStatePath(
                        f"REFUSED: {cur} is a symlink under .sdlc/ (#708). Fix: `git rm` it.")
            continue
        if stat_is_link(st):
            raise UnsafeStatePath(
                f"REFUSED: {cur} is a symlink under .sdlc/ -- Sigma never writes through one "
                f"(#708). Fix: `git rm` the committed symlink; this is not overridable.")
    return cur


_TREE_SCAN_CAP = 50000        # entries; ceiling of the per-invocation walk (~tens of ms at the cap)


def refuse_symlinked_tree(sdlc_dir):
    """#708: raise `UnsafeStatePath` if ANY symlink exists at or below `.sdlc/state` or
    `.sdlc/journey` (a committed one is checked out as-is and every writer there would follow it).
    ONE choke point run at the top of the mutating CLIs (`loop.py`, `work.py`), so a writer added
    later is covered without remembering a per-site call; the per-site `refuse_symlinks` calls stay
    as defence in depth for entry points that skip it. Cost: one `scandir` walk, O(files in those
    two trees), bounded by `_TREE_SCAN_CAP` entries (beyond it the check REFUSES -- a ceiling stated
    here, fail closed, never an unbounded cost or a silent pass). Missing dirs are fine."""
    base = pathlib.Path(sdlc_dir)
    seen = 0
    for top in ("state", "journey"):
        stack = [base / top]
        while stack:
            cur = stack.pop()
            try:
                if cur.is_symlink():
                    raise UnsafeStatePath(f"REFUSED: {cur} is a symlink under .sdlc/ -- Sigma never "
                                          f"writes through one (#708). Fix: `git rm` it.")
                it = os.scandir(cur)
            except UnsafeStatePath:
                raise
            except OSError:
                continue
            with it:
                for entry in it:
                    seen += 1
                    if seen > _TREE_SCAN_CAP:
                        raise UnsafeStatePath(
                            f"REFUSED: more than {_TREE_SCAN_CAP} entries under .sdlc/state and "
                            f".sdlc/journey, so the symlink check cannot finish (#708, fail closed); "
                            f"run `loop.py prune-state` or remove stale files")
                    if entry.is_symlink():
                        raise UnsafeStatePath(f"REFUSED: {entry.path} is a symlink under .sdlc/ -- "
                                              f"Sigma never writes through one (#708). Fix: `git rm` it.")
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(pathlib.Path(entry.path))


def guard_argv(argv, who):
    """#708: CLI entry guard. For every argv element that is a directory holding `state/` or
    `journey/`, run `refuse_symlinked_tree`; print the refusal and return 2, else 0. Used as
    `sys.exit(guard_argv(sys.argv, name) or main(sys.argv))` by every sigma CLI that writes under
    `.sdlc/` (the watchers, sync, kg, log, rebase), so a committed symlink never reaches a writer."""
    for arg in argv[1:]:
        try:
            if os.path.isdir(arg) and (os.path.isdir(os.path.join(arg, "state"))
                                       or os.path.isdir(os.path.join(arg, "journey"))):
                refuse_symlinked_tree(arg)
        except UnsafeStatePath as exc:
            print(f"{who}: {exc}", file=sys.stderr)
            return 2
    return 0


INERT_REF = "refused-invalid-ref-710"


def safe_ref(key, value):
    """#710 (TM-04): the ONE validator for a repository-supplied git remote/branch/prefix that is
    about to reach `git fetch|push|ls-remote|worktree` as a positional argument. A value that is not
    a plain string, starts with `-` (git reads it as an OPTION: `--upload-pack=<cmd>` runs <cmd>) or
    holds whitespace/control characters is REFUSED loudly on stderr and replaced by an inert name git
    rejects harmlessly, so every caller fails closed without needing its own except. Lever: put a
    plain remote/branch name in .sdlc/config.json (not overridable)."""
    if value in (None, ""):
        return value
    if (not isinstance(value, str) or value.startswith("-")
            or any(c.isspace() or ord(c) < 32 for c in value)):
        print(f"sigma: REFUSED {key} {value!r}: it would reach git as an option (#710); set a "
              f"plain name in .sdlc/config.json", file=sys.stderr)
        return INERT_REF
    return value


class UnsafeStatePath(OSError):
    """A path under `.sdlc/` has a symlink component (#708). An OSError so every existing fail-open
    caller keeps its posture, but every writer that catches it SAYS so (see `safe_state_open`)."""


def safe_state_open(sdlc_dir, rel, mode="a", encoding="utf-8"):
    """#708: the ONE way Sigma opens a file under `.sdlc/` that a repository could have pre-seeded.

    A repository can COMMIT a symlink at `.sdlc/state/inbox.md` (or `.sdlc/state` itself, or
    `.sdlc/journey`): git checks it out, `.gitignore` does not apply to tracked files, and a plain
    `open(..., "w")` then truncates or appends to a file OUTSIDE the repo as the user. So: lstat every
    component of `rel` below `sdlc_dir` and REFUSE (raise `UnsafeStatePath`, naming the fix) if any
    is a symlink; missing parents are created one level at a time, re-checked; the leaf opens with
    `O_NOFOLLOW` (closing the lstat->open window for the file itself) and must be a regular file.
    `mode` is "r", "a" or "w" (w truncates only AFTER the regular-file check). Lever: `git rm` the
    symlink. Residual: a directory component swapped for a symlink between the walk and the open is
    not closed on hosts without dir-fd support; that needs write access inside the checkout.
    Returns a text file object; the caller closes it."""
    cur = refuse_symlinks(sdlc_dir, rel, create_parents=mode != "r")
    flags = {"r": os.O_RDONLY, "a": os.O_WRONLY | os.O_APPEND | os.O_CREAT,
             "w": os.O_WRONLY | os.O_CREAT}[mode] | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(cur, flags, 0o644)
    except OSError as exc:
        if getattr(exc, "errno", None) in (40, 62):          # ELOOP (linux, macos): raced symlink
            raise UnsafeStatePath(f"REFUSED: {cur} is a symlink under .sdlc/ (#708)") from exc
        raise
    import stat as _stat
    if not _stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise UnsafeStatePath(f"REFUSED: {cur} is not a regular file (#708)")
    if mode == "w":
        os.ftruncate(fd, 0)
    return os.fdopen(fd, {"r": "r", "a": "a", "w": "w"}[mode], encoding=encoding)


def stat_is_link(st):
    import stat as _stat
    return _stat.S_ISLNK(st.st_mode)


def _patch_cursor(sdlc_dir, patch):
    """The one locked, atomically-published read-modify-write core every STATE.md cursor writer
    (`add_tokens`, `start_run`, `advance_cursor`) goes through, replacing the unlocked
    read-then-write each used to do on its own (#531). `patch(text) -> text` computes the new
    content from the text read UNDER THE LOCK — never from a caller's own separately-read, possibly
    stale text — so two concurrent callers never overwrite each other's increment.

    Lock BEFORE scaffold, deliberately: `_cursor_lock` is acquired before `_state_file` is ever
    called, so the scaffold-on-first-write and the read-modify-write share the same critical
    section — hoisting the scaffold call out from under the lock would reopen exactly the race
    `_state_file`'s own exclusive-create closed, just moved one level up.

    Publishes via a `tempfile.mkstemp` temp file in the SAME directory (so `os.replace` stays on one
    filesystem, the only case it's atomic), then `os.replace` onto STATE.md — a lock-free reader
    (`load_cursor` is deliberately lock-free, see its own docstring) therefore only ever observes the
    fully-old or fully-new file, never a truncated one. This holds even on the fail-open unlocked
    path: `mkstemp` hands out a fresh unique name per call, so two unlocked writers racing here still
    each publish through their OWN temp file, never a shared one — no worse than today's
    last-writer-wins.

    No nested locking: this is the ONLY function that acquires `_cursor_lock`, and it is never called
    from inside another `_cursor_lock` block — `flock` blocks even a SECOND fd from the same
    process, so re-entry here would self-deadlock. Every public writer below is a thin, single call
    into this function."""
    with _cursor_lock(sdlc_dir):
        f = _state_file(sdlc_dir)                   # scaffold call stays INSIDE the lock -- see above
        text = patch(f.read_text())
        fd, tmp = tempfile.mkstemp(dir=str(f.parent))
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, f)


def _read_int(text, key):
    m = re.search(rf"^{key}:\s*(\d+)", text, re.MULTILINE)
    return int(m.group(1)) if m else 0


def _read_float(text, key):
    # `run_started_at` alone needs sub-second precision (F11/#341, see done_refusal below) — a
    # separate reader from `_read_int` because its `\d+` regex would silently truncate the
    # fractional part on read, throwing away the exact precision `start_run` just wrote.
    m = re.search(rf"^{key}:\s*(\d+(?:\.\d+)?)", text, re.MULTILINE)
    return float(m.group(1)) if m else 0.0


def load_cursor(sdlc_dir):
    """Deliberately lock-free, and deliberately NON-CREATING (#550): an absent STATE.md reads as the
    template's own defaults rather than being scaffolded here. A fresh clone still works — every
    counter reads 0, which is exactly what a run that has not started yet means — and the file gets
    created by the first LOCKED writer through `_patch_cursor`. See `_state_file`'s docstring for why
    scaffolding from this path is the one thing that reopens the race its exclusive-create closed."""
    f = pathlib.Path(sdlc_dir) / "state" / "STATE.md"
    try:
        text = f.read_text()
    except FileNotFoundError:
        text = _STATE_TEMPLATE
    return {"iteration": _read_int(text, "iteration"), "run_iteration": _read_int(text, "run_iteration"),
            "run_started_at": _read_float(text, "run_started_at"),  # epoch secs; 0.0 on pre-0.6 STATE files
            "run_tokens": _read_int(text, "run_tokens"),
            "run_codex_raw_tokens": _read_int(text, "run_codex_raw_tokens")}


def load_label_checked(sdlc_dir):
    """#1805: the `--label` run whose start check last passed in this checkout, "" when none.
    Its own reader, not a `load_cursor` key, so the cursor's shape is unchanged for every other caller."""
    f = pathlib.Path(sdlc_dir) / "state" / "STATE.md"
    try:
        text = f.read_text()
    except FileNotFoundError:
        return ""
    m = re.search(r"^label_checked:[ \t]*(.*)$", text, re.MULTILINE)
    return m.group(1).strip() if m else ""


def load_label_checked_at(sdlc_dir):
    """#1832: epoch seconds the `label_checked` label's start check first passed, 0.0 when unrecorded."""
    f = pathlib.Path(sdlc_dir) / "state" / "STATE.md"
    try:
        return _read_float(f.read_text(), "label_checked_at")
    except FileNotFoundError:
        return 0.0


def set_label_checked(sdlc_dir, label, now=None):
    """#1805: record the label the start check passed for, beside `run_iteration`, through the one
    locked cursor writer. Every other writer edits its own lines by key, so this line survives them.
    #1832: `label_checked_at` is the drain start the documented gesture (no `start-run`) can know; a
    re-pass for the same label keeps the first time, a different label starts its own."""
    stamp = time.time() if now is None else float(now)

    def patch(text):
        m = re.search(r"^label_checked:[ \t]*(.*)$", text, re.MULTILINE)
        kept = m and m.group(1).strip() == label and _read_float(text, "label_checked_at")
        text = _set_line(text, "label_checked", label)
        return text if kept else _set_line(text, "label_checked_at", stamp)
    _patch_cursor(sdlc_dir, patch)


def _drop_line(text, key):
    """Remove the `key:` line when present; text without it comes back unchanged (no line is added)."""
    return re.sub(rf"^{re.escape(key)}:.*\n?", "", text, flags=re.MULTILINE)


def _set_line(text, key, value):
    line_re = re.compile(rf"^{re.escape(key)}:.*$", re.MULTILINE)
    new = f"{key}: {value}"
    # function replacement: keeps `value` literal (a string repl would expand \1, \g<>, trailing \)
    return line_re.sub(lambda _: new, text) if line_re.search(text) else text.rstrip() + f"\n{new}\n"


# --- Session registry storage (#955, D-5) ----------------------------------------------------------
# `state/sessions/<pid>[-<codex thread>].active`, one JSON marker per managing session. Moved down
# from loop.py verbatim because `done_refusal` (loop.py `record`, work.py `pr` and `merge`) and the
# budget credit writers below must read it, and this module must not import loop.py. Identity
# (`_session_marker_path`), lifecycle, admission and prune stay in loop.py, which keeps a `_session_*`
# alias for every storage name here, so its callers and tests are unchanged.

SESSION_THREAD_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    re.IGNORECASE,
)

_LEDGER_MODULE = []


def _ledger():
    """ledger.py, loaded on first use and cached (the `acceptance_module` idiom). Only the registry's
    liveness check needs it; ledger.py loads no state.py, so there is no import cycle."""
    if not _LEDGER_MODULE:
        spec = importlib.util.spec_from_file_location("ledger", _HERE / "ledger.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _LEDGER_MODULE.append(module)
    return _LEDGER_MODULE[0]


def session_dir(sdlc_dir):
    return pathlib.Path(sdlc_dir) / "state" / "sessions"


def session_lock_path(sdlc_dir, entry_path):
    """A stable, bounded 256-stripe on-disk lock namespace.

    Stripe identity must not depend on a process's current CPU count: two concurrent
    processes could then lock different files for the same admission marker. The byte
    width is a file-format constant, not a host resource budget. Locks are created
    lazily, remain after clean exit, and are capped at 256 per repository.
    """
    slot = hashlib.sha256(entry_path.name.encode()).digest()[0]
    return session_dir(sdlc_dir) / "locks" / f"{slot}.lock"


def session_locked(sdlc_dir, entry_path, fn, require_lock=False):
    """Run `fn()` — a read-modify-write against ONE session's own registry entry — inside an
    exclusive, blocking `flock` on a stable lock stripe for that entry, closing a lost-update race #1239 review
    (finding 3) reproduced deterministically: two genuinely concurrent writers to the SAME
    `<pid>.active` file (e.g. two `next-batch` slots each claiming a different goal under the same
    session, or a parallel `next` refill racing a sibling slot's `record`/`release`) each read the
    same pre-write `in_flight` list and the second write silently clobbers the first's addition —
    10/10 reproductions with a real `multiprocessing.Process` pair lost one goal's registration.

    Mirrors `_try_acquire_claim_lock`'s established `fcntl.flock` pattern in loop.py for the
    identical class of problem — except BLOCKING (`LOCK_EX`, no `LOCK_NB`): a session's own registry
    write is a fast, in-process critical section (read one small JSON file, mutate a list, write it
    back), not a multi-goal claim race where a busy competitor should be skipped rather than waited
    on — losing a moment blocked behind a sibling's write is the right trade here, not a bug.

    `entry_path` is the marker being written. loop.py's `_session_locked` resolves it: for a
    cross-session release or prune, the enumerated entry, so the call locks THAT entry even when the
    caller's Codex thread differs from its owner; otherwise the caller's own PID/thread entry,
    preserving Claude's PID-only behavior.

    Admission and registry updates pass `require_lock=True`: if locking is unavailable, refuse
    rather than risk concurrent writers overrunning the goal limit. Legacy optional callers may
    retain the prior best-effort fallback. The stripe set is bounded as sessions come and go.
    Never called inside `_cursor_lock` (#955): no path holds both."""
    lock_path = session_lock_path(sdlc_dir, entry_path)
    if fcntl is None:
        if require_lock:
            raise RuntimeError("session file locking is unavailable on this host")
        return fn()
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    except OSError:
        if require_lock:
            raise RuntimeError("cannot open session file lock for goal-count admission")
        return fn()
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fn()
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def session_read(path, strict=False):
    """Read one session registry entry. Strict admission reads refuse missing/corrupt counts;
    best-effort inventory reads treat unreadable entries as empty. `run` is the session's own run
    block (#955), normalised by `session_run`: None when absent or malformed, so every reader
    degrades to the checkout-wide STATE.md cursor rather than trusting a bad block."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        if strict:
            raise RuntimeError(f"session admission marker unreadable: {path}") from exc
        return {"in_flight": [], "settled_admissions": None, "valid": False, "run": None}
    if not isinstance(data, dict) or not isinstance(data.get("in_flight", []), list):
        if strict:
            raise RuntimeError(f"session admission marker malformed: {path}")
        return {"in_flight": [], "settled_admissions": None, "valid": False, "run": None}
    if strict and ("in_flight" not in data or
                   any(not isinstance(g, (str, int)) or isinstance(g, bool)
                       for g in data["in_flight"]) or
                   len({str(g) for g in data["in_flight"]}) != len(data["in_flight"])):
        raise RuntimeError(f"session admission goals malformed: {path}")
    settled = data.get("settled_admissions")
    if strict and (isinstance(settled, bool) or not isinstance(settled, int) or settled < 0):
        raise RuntimeError(f"session admission count missing or invalid: {path}")
    return {"in_flight": [str(g) for g in data.get("in_flight", []) if isinstance(g, (str, int))],
            "settled_admissions": settled if isinstance(settled, int) and not isinstance(settled, bool) and settled >= 0 else None,
            "valid": True, "run": session_run(data.get("run"))}


def session_write(path, data):
    """Publish a load-bearing session marker atomically, inside its caller's file lock.

    Admission/claim/release writers predate heartbeat ownership and the run block, and
    intentionally pass only their own fields. #955 D-1: every key of the existing marker that
    `data` does not carry is kept, so none of those routine updates can drop `generation` (which
    would silently turn a later owner-checked `session_end` into a no-op) or `run` (which would
    hand this session's budgets and freshness anchor back to the checkout-wide cursor). An
    unreadable existing marker preserves nothing, as before."""
    try:
        existing = json.loads(path.read_text())
    except (OSError, ValueError):
        existing = None
    if isinstance(existing, dict):
        data = {**{k: v for k, v in existing.items() if k not in data}, **data}
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + f".{os.getpid()}.")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        pathlib.Path(tmp).unlink(missing_ok=True)


def _session_paths(sdlc_dir):
    """`(pid, path)` for every registry file on disk, live or stale alike. Parse the numeric PID
    prefix from both legacy `<pid>` and Codex `<pid>-<thread UUID>` filenames. Stray names are
    skipped rather than raising — this directory is scanned on every `_next()` call."""
    d = session_dir(sdlc_dir)
    if not d.is_dir():
        return
    for path in sorted(d.glob("*.active")):
        try:
            pid_text, separator, thread = path.stem.partition("-")
            pid = int(pid_text)
            if separator and not SESSION_THREAD_ID.fullmatch(thread):
                continue
        except ValueError:
            continue
        yield pid, path


def session_entries(sdlc_dir):
    """Yield `(pid, path, in_flight)` for every session registry file on disk, live or stale alike
    — callers filter liveness themselves (`session_pid_live`) since "is it live" and "what does it
    say" are independent questions here. Parse the numeric PID prefix from both legacy `<pid>` and
    Codex `<pid>-<thread UUID>` filenames. Stray names are skipped rather than raising — this
    directory is scanned on every `_next()` call, so it must degrade on unexpected entries."""
    for pid, path in _session_paths(sdlc_dir):
        yield pid, path, session_read(path)["in_flight"]


def session_pid_live(pid, path, config):
    """The same two-signal liveness check `session_active`/`agent_alive` already apply to their own
    markers (F10.5/#374's own pattern, not reinvented here): `ledger.pid_alive()` first — a
    definitively dead pid needs no TTL to disqualify — then the entry file's own mtime against
    `ledger.lease_ttl_seconds(config)` as the pid-reuse backstop. A budget credit restores the
    marker's mtime after writing it (`_credit_session_runs`), so only the session's own start,
    claim and release writes move this backstop."""
    ledger = _ledger()
    if not ledger.pid_alive(pid):
        return False
    ttl = ledger.lease_ttl_seconds(config)
    if ttl is None:
        return True
    try:
        return (time.time() - path.stat().st_mtime) < ttl
    except OSError:
        return True                          # can't even stat it — fail toward "still active"


def _registry_config(sdlc_dir):
    """The config the liveness lease reads; unreadable or absent -> {} (the default 12 h lease)."""
    try:
        return load_config(sdlc_dir)
    except (ConfigMissing, OSError, ValueError):
        return {}


def live_session_records(sdlc_dir):
    """Yield `(pid, path, session_read(path))` for every LIVE session marker (#955). Reads nothing
    when `state/sessions` is absent, so a checkout that never ran `/sigma-loop` pays one stat."""
    if not session_dir(sdlc_dir).is_dir():
        return
    config = _registry_config(sdlc_dir)
    for pid, path in _session_paths(sdlc_dir):
        if session_pid_live(pid, path, config):
            yield pid, path, session_read(path)


def _goal_key(goal):
    """The goal identity a path component and a registry comparison both use: a local goal's
    `.md` path reduces to its stem, so `.sdlc/goals/0001.md` (as `next` prints it) and `0001`
    name the same goal; anything else is compared as given."""
    return pathlib.Path(goal).stem if str(goal).endswith(".md") else str(goal)


def _goal_holders(records, goal):
    key = _goal_key(goal)
    return [(pid, path, record) for pid, path, record in records
            if key in {_goal_key(g) for g in record["in_flight"]}]


# --- A session's own run block (#955) --------------------------------------------------------------
# A run belongs to the session that started it. Every start path (`loop.begin_run`) stamps a `run`
# block in ITS OWN marker with the same instant it writes to STATE.md, and `start-run
# --session-pid` resets it. Session-scoped readers (the budget gates, the run_stop dedupe) read
# their own block; goal-scoped ones (verify freshness, phase/spend credit) resolve the goal's LIVE
# holder(s). No block, no holder, or a dead holder: the checkout-wide STATE.md cursor, as before.
# No `phase_ends` here: that list only drives phase_report's journal dedupe and stays checkout-wide.

def session_run(raw):
    """A marker's `run` block, normalised, or None when it is absent or malformed. Every field is
    required: `started_at` a finite number > 0 (not a bool), the two counters non-negative ints,
    `token_credits` a list of str, `codex_token_credits` a dict of str to non-negative int."""
    if not isinstance(raw, dict):
        return None

    def count(value):
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    started = raw.get("started_at")
    if (isinstance(started, bool) or not isinstance(started, (int, float))
            or not math.isfinite(started) or started <= 0):
        return None
    credits, codex = raw.get("token_credits"), raw.get("codex_token_credits")
    if (not count(raw.get("tokens")) or not count(raw.get("codex_raw_tokens"))
            or not isinstance(credits, list) or not all(isinstance(k, str) for k in credits)
            or not isinstance(codex, dict)
            or not all(isinstance(k, str) and count(v) for k, v in codex.items())):
        return None
    return {"started_at": float(started), "tokens": raw["tokens"],
            "codex_raw_tokens": raw["codex_raw_tokens"], "token_credits": list(credits),
            "codex_token_credits": dict(codex)}


def fresh_session_run(now):
    return {"started_at": float(now), "tokens": 0, "codex_raw_tokens": 0,
            "token_credits": [], "codex_token_credits": {}}


def reset_session_run(sdlc_dir, entry_path, now):
    """Stamp a fresh run block, started at `now`, into one EXISTING session marker. Returns False,
    writing nothing, when the marker is not a file or not a readable JSON object: a run block never
    creates (or resurrects) a session. Under the marker's own stripe lock; never inside
    `_cursor_lock` (callers write the marker first, STATE.md second)."""
    entry_path = pathlib.Path(entry_path)

    def _reset():
        if not entry_path.is_file():
            return False
        try:
            if not isinstance(json.loads(entry_path.read_text()), dict):
                return False
        except (OSError, ValueError):
            return False
        session_write(entry_path, {"run": fresh_session_run(now)})
        return True

    return session_locked(sdlc_dir, entry_path, _reset, require_lock=True)


def goal_run_started_at(sdlc_dir, goal):
    """The run start a goal's verify evidence must not predate (#955): the checkout's
    `run_started_at`, unless one or more LIVE sessions hold the goal in `in_flight` — then the
    latest of those holders' own run starts (a holder without a run block contributes the checkout
    value). So another session's `start` can no longer void a live session's green, while a goal no
    live session holds (the `/sigma-goal` arming path, a goal awaiting merge after `record review`)
    keeps the checkout's latest start exactly as before, and a dead holder anchors nothing."""
    checkout = load_cursor(sdlc_dir)["run_started_at"]
    anchors = [record["run"]["started_at"] if record["run"] is not None else checkout
               for _pid, _path, record in _goal_holders(live_session_records(sdlc_dir), goal)]
    return max(anchors) if anchors else checkout


def _credit_targets(sdlc_dir, goal):
    """Which session run blocks a spend is charged to, and whether they hold its goal.

    The goal's live holders when there is at least one (a holder with no block is skipped by the
    writer and reads the checkout cursor). Otherwise EVERY live session: spend that names no goal,
    or a goal no live session holds, cannot be attributed, so it over-counts rather than escapes a
    ceiling — exactly what the shared counter did to each of them before."""
    live = list(live_session_records(sdlc_dir))
    if goal is not None:
        holders = _goal_holders(live, goal)
        if holders:
            return [path for _pid, path, _record in holders], True
    return [path for _pid, path, _record in live], False


def _credit_session_runs(sdlc_dir, paths, tokens=None, codex=None, key=None, started_at=None,
                         holders=True):
    """Add one spend to each target marker's run block, ONE stripe lock at a time (never all at once
    and never inside `_cursor_lock`, which every caller has already released).

    Per target, under its lock: not a file, unreadable JSON, or no `run` key -> skip (never
    recreate; a legacy session reads the checkout cursor). `started_at` before the block's own start
    -> skip (the phase predates THIS session's run: the per-session twin of `record_phase_end`'s
    old-run guard). Tokens: no attempt key adds; a key already credited adds nothing (a retried
    phase end self-heals); a new key past `_MAX_RUN_TOKEN_CREDITS` refuses. Codex keeps the highest
    cumulative amount per key and adds the delta, as STATE.md does. Written only when it changed.

    A credit restores the marker's previous atime/mtime after its atomic replace, inside the same
    lock. The marker's mtime is `session_pid_live`'s lease-TTL pid-reuse backstop, and charge-to-all
    spend writes every live marker: without the restore, a dead session whose pid was reused would
    stay live for as long as anyone spent. Only the session's own start/claim/release refresh it.

    A malformed or capped block, or a lock failure: when the targets HOLD the goal, every target is
    still attempted and the first problem is raised as RuntimeError afterwards (loud, as STATE.md's
    own cap is). When they are charge-to-all targets, the problem is one stderr line naming the
    marker and that session is skipped: one session's bad block must not fail spend for the rest."""
    first = None
    for path in paths:
        def _credit(path=path):
            try:
                before = path.stat()
                raw = json.loads(path.read_text())
            except (OSError, ValueError):
                return None
            if not isinstance(raw, dict) or "run" not in raw:
                return None
            run = session_run(raw["run"])
            if run is None:
                return (f"session run block malformed in {path.name}; "
                        "start a fresh /sigma-loop session")
            if started_at is not None and float(started_at) < run["started_at"]:
                return None
            changed = False
            if tokens is not None:
                if key is None:
                    run["tokens"] += tokens
                    changed = True
                elif key not in run["token_credits"]:
                    if len(run["token_credits"]) >= _MAX_RUN_TOKEN_CREDITS:
                        return ("session run token credit limit (4096 phase attempts) reached; "
                                "start a fresh /sigma-loop session")
                    run["token_credits"].append(key)
                    run["tokens"] += tokens
                    changed = True
            if codex is not None and key is not None and codex > run["codex_token_credits"].get(key, 0):
                if (key not in run["codex_token_credits"]
                        and len(run["codex_token_credits"]) >= _MAX_RUN_TOKEN_CREDITS):
                    return ("session run Codex token credit limit (4096 phase attempts) reached; "
                            "start a fresh /sigma-loop session")
                run["codex_raw_tokens"] += codex - run["codex_token_credits"].get(key, 0)
                run["codex_token_credits"][key] = codex
                changed = True
            if changed:
                session_write(path, {"run": run})
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            return None

        try:
            problem = session_locked(sdlc_dir, path, _credit, require_lock=True)
        except (OSError, RuntimeError) as exc:
            problem = str(exc)
        if not problem:
            continue
        if holders:
            first = first or problem
        else:
            print(f"state: {problem} ({path.name}); this spend was not charged to that session",
                  file=sys.stderr)
    if first:
        raise RuntimeError(first)


def start_run(sdlc_dir, now=None):
    """Reset the checkout-wide run cursor in STATE.md at the start of a /sigma-loop invocation.
    `_set_line` appends missing lines, so pre-0.6 STATE.md files upgrade in place.
    `run_started_at` keeps the raw `time.time()` float (F11/#341) — flooring it to a whole second
    (as before) let a verify's own `at` stamp land in the SAME second as a run that started just
    after it, so a stale green from the previous run could tie with (and pass as fresh for) this
    one; sub-second precision closes that window without touching the done_refusal comparison.
    `now` is captured BEFORE `_patch_cursor` (not inside its `patch` callable), so the timestamp
    reflects the instant `start_run` was actually called, not however long a contended lock
    acquisition might delay the write (#531; the callable itself must stay pure text-in/text-out,
    see `_cursor_lock`'s docstring). `loop.begin_run` passes the instant it also stamped into the
    starting session's own run block (#955), so the two anchors agree to the float.

    Since #955 this is the CHECKOUT's run, not every session's: a live session's budgets and its
    goals' verify freshness come from its own run block, which another session's start (and so
    this reset) no longer touches. STATE.md is still written exactly as before, which keeps the
    single-loop path, a goal no live session holds, and every legacy reader unchanged."""
    now = time.time() if now is None else float(now)

    def patch(text):
        text = _set_line(text, "run_iteration", 0)
        text = _set_line(text, "run_started_at", now)
        text = _set_line(text, "run_tokens", 0)
        text = _set_line(text, "run_token_credits", "[]")
        text = _set_line(text, "run_codex_raw_tokens", 0)
        text = _set_line(text, "run_codex_token_credits", "{}")
        text = _set_line(text, "run_phase_ends", "[]")
        text = _drop_line(text, "label_checked_at")     # #1832: its start time goes with it
        return _drop_line(text, "label_checked")        # #1823: a new run has passed no label check yet
    _patch_cursor(sdlc_dir, patch)


def add_tokens(sdlc_dir, n, goal=None):
    """Accumulate a spend signal for this run into the `run_tokens` budget cursor. Two producers
    feed this today (#2515): `loop.py spend`'s CLI verb, the host-integration surface for spend
    measured OUTSIDE this codebase; and `phase_report.py cmd_end`, which calls this directly with
    a real, self-measured cost-equivalent token count (`cost_equivalent_tokens`) the moment a
    phase's transcript can be priced. The loop itself still never measures spend — both producers
    compute the number elsewhere and hand it to this function to accumulate.
    Routes through `_patch_cursor` (#531): the +n increment is computed from the text read UNDER
    THE LOCK, not a separately (unlocked) read text, so concurrent callers — a real shape under
    `parallel.goals`, and now also concurrent phase-end calls — no longer lose increments to each
    other.

    #955: then, with that lock released, the same `n` is charged to the run block of each live
    session holding `goal`, or of EVERY live session when `goal` is None or held by none
    (`_credit_targets`). No attempt key, so a retried `spend` double-counts in a block exactly as
    it does in STATE.md."""
    n = int(n)
    if n < 0:
        raise ValueError(f"token count {n} is negative; a spend report can only add (#632)")
    _patch_cursor(sdlc_dir, lambda text: _set_line(text, "run_tokens", _read_int(text, "run_tokens") + n))
    if n:
        paths, holders = _credit_targets(sdlc_dir, goal)
        _credit_session_runs(sdlc_dir, paths, tokens=n, holders=holders)


def phase_attempt_key(phase_attempt):
    """Stable bounded identifier shared by the budget cursor and retryable phase journal events."""
    return hashlib.sha256(str(phase_attempt).encode("utf-8")).hexdigest()[:24]


def record_phase_end(sdlc_dir, phase_attempt, started_at, budget_tokens=None,
                     codex_raw_tokens=None, goal=None):
    """Atomically mark an end and optional, unit-separated budget credits in this run.

    `start_run` resets the same cursor under the same lock. Comparing the marker's start time
    UNDER that lock keeps a phase from an older run out of the new run's budget, even if a
    parallel goal slot finishes after a HANDOFF. Codex credits keep the highest measured
    cumulative amount for each attempt, adding only the delta when a partial rollout grows on
    retry. Returns (new_end, new_credit_or_delta, old_run), all three about the checkout-wide
    STATE.md cursor, as before (phase_report's journal dedupe reads them).

    #955: after that, with `_cursor_lock` released, the same credit goes to the run block of each
    live session holding `goal` (or of every live session when none does; `_credit_targets`),
    with the same attempt-key dedupe and the same old-run rule measured against THAT session's own
    start. So a phase its owner began before ANOTHER session's start is still charged to its owner,
    even though STATE.md's `old_run` guard now drops it from the checkout cursor.
    """
    key = phase_attempt_key(phase_attempt)
    amount = int(budget_tokens) if budget_tokens is not None else None
    codex_amount = int(codex_raw_tokens) if codex_raw_tokens is not None else None
    if amount is not None and amount < 0:
        raise ValueError("budget_tokens must be nonnegative: a negative amount is a negative token count (#632)")
    if codex_amount is not None and codex_amount < 0:
        raise ValueError("codex_raw_tokens must be nonnegative")
    outcome = [False, False, False]

    def patch(text):
        run_start = _read_float(text, "run_started_at")
        if run_start and float(started_at) < run_start:
            outcome[2] = True
            return text
        ends_match = re.search(r"^run_phase_ends:\s*(\[.*\])$", text, re.MULTILINE)
        credit_match = re.search(r"^run_token_credits:\s*(\[.*\])$", text, re.MULTILINE)
        ends = json.loads(ends_match.group(1)) if ends_match else []
        credits = json.loads(credit_match.group(1)) if credit_match else []
        codex_match = re.search(r"^run_codex_token_credits:\s*(\{.*\})$", text, re.MULTILINE)
        codex_credits = json.loads(codex_match.group(1)) if codex_match else {}
        if not isinstance(codex_credits, dict):
            raise RuntimeError("invalid Codex token credit map; start a fresh /sigma-loop session")
        if key not in ends:
            if len(ends) >= _MAX_RUN_TOKEN_CREDITS:
                raise RuntimeError("run phase-end limit (4096 attempts) reached; "
                                   "start a fresh /sigma-loop session")
            ends.append(key)
            outcome[0] = True
            text = _set_line(text, "run_phase_ends", json.dumps(ends, separators=(",", ":")))
        if amount is not None and key not in credits:
            if len(credits) >= _MAX_RUN_TOKEN_CREDITS:
                raise RuntimeError("run token credit limit (4096 phase attempts) reached; "
                                   "start a fresh /sigma-loop session")
            credits.append(key)
            outcome[1] = True
            text = _set_line(text, "run_tokens", _read_int(text, "run_tokens") + amount)
            text = _set_line(text, "run_token_credits", json.dumps(credits, separators=(",", ":")))
        if codex_amount is not None and codex_amount > codex_credits.get(key, 0):
            if key not in codex_credits and len(codex_credits) >= _MAX_RUN_TOKEN_CREDITS:
                raise RuntimeError("run Codex token credit limit (4096 phase attempts) reached; "
                                   "start a fresh /sigma-loop session")
            delta = codex_amount - codex_credits.get(key, 0)
            codex_credits[key] = codex_amount
            outcome[1] = True
            text = _set_line(text, "run_codex_raw_tokens",
                             _read_int(text, "run_codex_raw_tokens") + delta)
            text = _set_line(text, "run_codex_token_credits",
                             json.dumps(codex_credits, separators=(",", ":")))
        return text

    _patch_cursor(sdlc_dir, patch)
    if amount is not None or codex_amount is not None:
        paths, holders = _credit_targets(sdlc_dir, goal)
        _credit_session_runs(sdlc_dir, paths, tokens=amount, codex=codex_amount, key=key,
                             started_at=started_at, holders=holders)
    return tuple(outcome)


def run_token_credits(sdlc_dir):
    """Credited phase attempts: the checkout run's (STATE.md; absent on pre-#2521 state files),
    united with every session marker's own run block, live or not, since a dead session's credits
    were real (#955). Without the union, `/sigma-doctor` would report a phase credited only to its
    owning session (one that began before another session's start) as uncredited."""
    credits = set()
    path = pathlib.Path(sdlc_dir) / "state" / "STATE.md"
    try:
        text = path.read_text()
    except OSError:
        text = ""
    match = re.search(r"^run_token_credits:\s*(\[.*\])$", text, re.MULTILINE)
    try:
        listed = json.loads(match.group(1)) if match else []
    except (ValueError, TypeError):
        listed = []
    if isinstance(listed, list):
        credits.update(listed)
    for _pid, entry in _session_paths(sdlc_dir):
        run = session_read(entry)["run"]
        if run is not None:
            credits.update(run["token_credits"])
    return credits


def advance_cursor(sdlc_dir, summary):
    """The atomic replacement for `_record`'s old two-call `load_cursor` + `save_cursor`
    read-modify-write (#531; `save_cursor` itself is deleted — this was its only production
    caller). One call reads iteration/run_iteration from the text UNDER THE LOCK and writes both
    +1, plus `last_run`. Collapsing two calls into one closes the COMPOUNDING half of the original
    race: even a per-call-locked `save_cursor` alone would still let two concurrent `_record`s each
    read the SAME pre-increment cursor between their own separate load-then-save pair — a single
    atomic patch has no such gap for a second caller to land in."""
    def patch(text):
        text = _set_line(text, "iteration", _read_int(text, "iteration") + 1)
        text = _set_line(text, "run_iteration", _read_int(text, "run_iteration") + 1)
        return _set_line(text, "last_run", summary)
    _patch_cursor(sdlc_dir, patch)


def atomic_write_text(path, text):
    """Publish `text` to `path` so a crash leaves the whole old file or the whole new one, never a
    torn or empty one (#592, #634): a same-directory temp file, `os.replace` onto the target.
    Modelled on `_patch_cursor`'s publish, with one deliberate difference: this one fsyncs before the
    replace, because a goal file is a user-visible record, not a cursor the next write recomputes.
    The target is resolved through symlinks first (a symlinked goal keeps its link), its mode is
    copied (a new target gets 0644), the temp is removed on any failure and the error re-raised.
    Stated limits: ownership, ACLs and xattrs are not carried over; a hard link to the file is not
    kept; a directory that is not writable now fails where an in-place write used to succeed; a crash
    can leave a dotted `.<name>.tmp` beside the file (discovery globs `*.md`, nothing prunes it); and
    two concurrent writers still race, last one wins, as before."""
    target = os.path.realpath(str(path))
    try:
        mode = os.stat(target).st_mode & 0o7777
    except FileNotFoundError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _set_status(goal_path, status):
    p = pathlib.Path(goal_path)
    atomic_write_text(p, frontmatter.set_field(p.read_text(encoding="utf-8"), "status", status))


#: Mirrors discovery.py's own `_TERMINAL` vocabulary (done/parked/failed) byte-for-byte. state.py
#: cannot import discovery.py for it (a goal-status WRITER importing the status READER/orderer
#: would be backwards — discovery.py already imports frontmatter.py the same sibling-load way this
#: file does), so this is a second, small, hand-kept copy of the same three-value set — the same
#: shape ledger.KINDS/contract/vocabulary.json already are elsewhere in this plugin.
_TERMINAL_STATUSES = ("done", "parked", "failed")


def _status(goal_path):
    return frontmatter.get(pathlib.Path(goal_path).read_text(), "status")


def set_in_progress(sdlc_dir, goal_path):
    _set_status(goal_path, "in_progress")


def release(sdlc_dir, goal_path):
    """#841: undo a `set_in_progress` claim that was never started (`next`/`next-batch` claimed
    it, then nothing ever called complete/park/fail on it — a batch pick that never got dispatched,
    or a later pick `--skip`ped after this one already claimed it). Unlike complete/park/fail this
    is NOT a terminal SDLC outcome: the goal goes back to `pending`, the exact status it had before
    it was ever picked, so `next_pending` offers it again indistinguishably from a goal that was
    never claimed at all. Never touches review-queue.md (see `park`/`fail`'s own `_queue()` call) —
    nothing here needs a human decision.

    POST-REVIEW FIX (PR #1107, Finding 3): refuses — as a clean no-op, goal untouched — when the
    goal is already `done`/`parked`/`failed`. Pre-fix this called `_set_status(goal_path, "pending")`
    unconditionally: reproduced live, a `status: done` goal became `status: pending` after a bare
    `release()` call, no confirmation, no warning, no guard. Release is only meaningful for a goal
    that is still claimed/in-progress — a goal that already reached a terminal state reflects a
    completed DECISION, not a stale claim, and silently un-completing it is a correctness bug, not
    a style choice. Returns True when it actually released a claim, False when it left an
    already-terminal goal untouched — see sources.py's LocalSource.release, the one caller, which
    uses this to journal an honest message either way instead of a misleading "released" note for a
    no-op.

    #1550: it refuses `proposed` too. That status is a human-promotion gate, release is a
    claim-undo, and a proposed goal was never claimed, so release must not be what promotes it."""
    status = _status(goal_path)
    if status in _TERMINAL_STATUSES or status == "proposed":
        return False
    _set_status(goal_path, "pending")
    return True


def complete(sdlc_dir, goal_path):
    _set_status(goal_path, "done")


def park(sdlc_dir, goal_path, reason, tier=None):
    _set_status(goal_path, "parked")
    # #953/#1185: `tier` is OPTIONAL and additive -- omitted entirely by every caller predating
    # decision_tier.py, and by every park loop.py's _record() doesn't offer to decision_tier at
    # all (its reason_class outside _DECISION_TIER_REASON_CLASSES, OR "unknown" but reached via
    # the mechanical "could not compute mergeability" rule rather than genuine free-text
    # fallthrough -- see _record's own comment) or that resolve() itself leaves inert (config not
    # "auto"). Every other park gets a real tier passed through.
    _queue(sdlc_dir, goal_path, reason, "human review", tier=tier)


def fail(sdlc_dir, goal_path, reason):
    """A goal the loop COULD NOT resolve — distinct from parked (which needs a human
    DECISION). Its own queue tag lets the morning read separate decide-this from fix-this."""
    _set_status(goal_path, "failed")
    _queue(sdlc_dir, goal_path, reason, "a fix (the loop could not resolve this)")


def _queue(sdlc_dir, goal_path, reason, needs, tier=None):
    """Append one parked/failed goal to the morning review queue, scaffolding the file on first use.

    `tier` (#953) is OPTIONAL, trailing, and additive: `None` (its default, and every call from
    `fail()` or a pre-#953 `park()`) renders the exact same two-line entry this function has always
    written -- reason then needs, byte-for-byte. Only a `park()` call that was handed a real
    decision_tier.resolve() result adds a third `- tier:` line, so a human skimming the queue sees
    severity at a glance without opening the goal.

    Exclusive-create, for the same reason `_state_file` uses it (#550 — this was the sibling that
    PR's scope left open): `if not q.exists(): q.write_text(TEMPLATE)` is check-then-act, and
    `write_text` TRUNCATES. Two first-parks on a fresh clone — two `parallel.goals` worktree
    siblings, say — can both see "not exists", and the loser's template write then destroys whatever
    the winner already appended. `O_EXCL` makes "someone else got here first" a clean
    `FileExistsError` to walk away from instead of a race to inspect-then-act. Mode 0o644 because
    `os.open` otherwise defaults to 0o777 (an executable markdown file).

    The append itself needs no locking and gets none: `O_APPEND` writes from independent file
    descriptions are atomic at the OS level, so concurrent entries interleave whole, never torn.
    Only the scaffold step was ever racy.

    The scaffold fd carries `O_APPEND` too, which is load-bearing rather than tidy: exclusive-create
    alone still leaves a residual of the very bug this fixes. The winner creates an EMPTY file and
    is not yet holding a position at EOF, so a loser that appends its entry in the gap before the
    template write has that entry overwritten from offset 0 (measured: 2 losses in 8 runs of the
    8-process test below). `O_APPEND` forces the template to EOF instead, so nothing can be
    overwritten — at worst, on a first creation that genuinely raced, the header lands after that
    first entry. A cosmetic ordering quirk on one file, in exchange for never dropping a parked
    goal."""
    q = pathlib.Path(sdlc_dir) / "state" / "review-queue.md"
    refuse_symlinks(sdlc_dir, q, create_parents=True)       # #708: O_EXCL fails on a link, `open("a")` follows it
    q.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(q), os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_APPEND, 0o644)
    except FileExistsError:
        pass                      # another parker already scaffolded it -- never truncate it
    else:
        with os.fdopen(fd, "w") as fh:
            fh.write(_QUEUE_TEMPLATE)
    name = pathlib.Path(goal_path).name
    entry = f"\n## {name}\n- reason: {reason}\n- needs: {needs}\n"
    if tier:
        entry += f"- tier: {tier}\n"
    with q.open("a") as f:
        f.write(entry)


def unsafe_goal_reason(stem):
    """None iff `stem` (an ALREADY goal-stem-reduced value — what a caller is about to embed as a
    single path component, not necessarily the raw caller-supplied goal) is safe, else the reason
    it is not. THE shared validator for every `.../state/.../<stem(goal)>...` path across this
    plugin — `loop.py`'s `_unsafe_thread_reason` (for `thread`, a sibling untrusted value) is the
    proven-correct pattern this mirrors exactly (same character set, same reasoning); this lives in
    `state.py` specifically because it is the one module every affected caller
    (`loop.py`/`work.py`/`actionlog.py`) already imports with zero import-cycle risk (`state.py`
    itself imports only stdlib) — a single implementation, not one per caller, closes the
    hardened-sibling-divergence gap found when only `actionlog.py::log_path()` had this check
    (independent review of #486/PR #487): `loop.py::agent_end()`'s unconditional, ungated
    `shutil.rmtree()` on an unvalidated goal-derived path (reachable from the everyday `record`
    verb, not just the `agent-end` escape hatch), `loop.py::verify_goal()`'s file write, and
    `loop.py::_claim_lock_path()` / `work.py::record_path()` all shared the identical gap.
    `skills/sigma-log/scripts/log.py` needs its OWN local copy (it deliberately does not import
    `skills/sigma-loop/scripts/` at all — format-only coupling, see its own module docstring), kept
    byte-identical to this on purpose.

    `goal` reaches every one of these from either an LLM/agent-typed CLI argument or (rarely) a
    malformed local `.sdlc/goals/*.md` filename — untrusted in both cases, exactly like `thread`.
    pathlib's own `/` join operator RE-PARSES a string argument for separator characters, so a `/`
    or `\\` inside `stem` does not stay one filename, it becomes ADDITIONAL path segments, one of
    which can be a literal `..` — escaping the intended `state/...` subtree entirely (confirmed by
    direct reproduction across all five call sites: file read/write/delete, one of them an
    unconditional `shutil.rmtree`). Checked on the STEM (the reduced value about to be embedded),
    not the raw goal, so a `.md`-suffixed goal already reduced by `pathlib.Path.stem` (which strips
    any directory prefix as a side effect) is not double-penalized, while a non-`.md` goal — where
    stem-reduction is a no-op — is still caught, since the raw string is what's checked in that
    case. Rejecting any separator closes the join's only real danger directly; `..` is kept as an
    explicit belt-and-suspenders check. `:` is rejected too for the same class of risk on Windows
    (a drive-letter-rooted path). Never raises — `str(stem)` handles anything."""
    text = str(stem)
    if any(c in text for c in ("/", "\\", ":")) or ".." in text:
        return "must not contain '/', '\\', ':', or '..' once reduced to a path component"
    return None


def evidence_path(sdlc_dir, goal):
    stem = _goal_key(goal)
    reason = unsafe_goal_reason(stem)
    if reason:
        raise ValueError(f"unsafe goal {goal!r} for verify evidence: {reason}")
    return pathlib.Path(sdlc_dir) / "state" / "verify" / f"{stem}.json"


def run_identity():
    """A stable id for the CURRENT run/session, or None when this run is unattributed.

    #498: sourced from the process environment (`SIGMA_RUN_ID`), because that is the only
    per-worker axis NOT shared through the filesystem. The evidence file, `STATE.md`
    (`run_started_at`) and the `session.active` marker all live in the ONE shared `.sdlc` a second
    worker also writes, so any id persisted there loses to last-writer-wins under the exact
    concurrent-verify race #498 is about — both racing workers would read back the SAME id and a
    sibling's green would still be inherited. An env var, in contrast, is inherited per process
    tree: the launcher that spawns a worker exports one `SIGMA_RUN_ID`, every child `loop.py`
    / `work.py` invocation of THAT worker inherits it (so verify → record → merge agree), and two
    concurrent workers carry different values.

    None (env unset or empty) means "unattributed": `done_refusal` then degrades to the pre-#498
    freshness-only behaviour — honest, with no false confidence, and fully backward-compatible with
    a deployment whose launcher does not (yet) set the variable."""
    return os.environ.get("SIGMA_RUN_ID") or None


def derive_run_id(session_pid):
    """#889: a stable run id derived from a session's own pid, or None when there isn't a usable
    one. The bare-`/sigma-loop` counterpart to `supervise_daemon.py`'s own run-id.

    WHY THIS EXISTS. `run_identity()` below reads `SIGMA_RUN_ID`, and `supervise_daemon.py` is
    its only source — so an interactive `/sigma-loop` was always unattributed, and
    `_emit_run_stop_once`'s attribution gate (loop.py) skipped every terminal event it ever
    reached. Measured cost: 0 `kind='run_stop'` rows across 1,081 real ledger event files, which
    is why a downstream budget-exhaustion rate rendered as an empty view.

    WHY A PID AND NOT A FRESH RANDOM. There is no long-lived shell to export into: every
    `python3 loop.py ...` in an interactive session is a new process, so a minted-per-invocation
    id would be UNSTABLE, and unstable is strictly worse than None on all three consumers —
    `claim_run_stop` is keyed `(run_id, run_started_at)`, so each idle `next` poll would claim a
    fresh slot and emit a DUPLICATE run_stop (the exact bug #905 closed); `done_refusal` would see
    a different id at verify than at record; and the `claimed`/terminal events would each carry a
    one-shot id nothing could correlate. The caller's own session pid is the one axis that is
    stable across a session's invocations AND distinct between concurrent sessions.

    CALLERS MUST PASS THE EXPLICIT `--session-pid`, never `os.getppid()`. Measured 2026-08-22
    across two separate tool-style calls: `$$` differed (74411, 74415) while `$PPID` was 31857
    both times. `os.getppid()` measured from inside `loop.py` is that transient `$$`-level parent
    (loop.py's own `session_start` docstring establishes the distinction), so deriving from it
    would reintroduce exactly the instability the paragraph above rules out. `loop.py:2606`'s
    `session_pid = os.getppid()` fallback is therefore NOT a valid input here — read the flag
    before that fallback runs.

    Returns None for anything that isn't a positive integer, including the literal "true" a
    valueless `--session-pid` flag parses to. None means unattributed, which is the honest,
    already-supported degradation — never a guessed id."""
    try:
        pid = int(str(session_pid))     # int() already tolerates surrounding whitespace
    except ValueError:                  # str() precedes int(), so TypeError is unreachable here
        return None
    if pid <= 0:
        return None
    return f"session-{pid}"


def run_stop_marker_path(sdlc_dir, run_id):
    """Where the #905 run_stop dedupe marker for `run_id` lives. `run_id` is untrusted-ish input —
    it comes from `SIGMA_RUN_ID`, which `supervise_daemon.py`'s own comment says "an id set by
    an outer launcher is respected", i.e. it is not always this codebase's own generated
    `supervise-<pid>-<ts>-<rand>` shape — so it is sanitized exactly like a goal stem before
    becoming a path component (same
    `unsafe_goal_reason` `evidence_path` already uses for the identical reason)."""
    reason = unsafe_goal_reason(run_id)
    if reason:
        raise ValueError(f"unsafe run id {run_id!r} for run_stop marker: {reason}")
    return pathlib.Path(sdlc_dir) / "state" / "run_stop" / f"{run_id}.json"


def claim_run_stop(sdlc_dir, run_id, run_started_at):
    """True the first time `(run_id, run_started_at)` claims a run_stop emission, False on a
    repeat of that exact pair — the #905 dedupe for `_next()`'s terminal-return chokepoint.

    Keyed on the PAIR, not the bare `run_id` alone (plan-review's own Defect 1): `run_id` is stable
    across one `supervise_daemon.py` worker's ENTIRE lifetime, including every budget-stop-then-relaunch
    cycle (`supervise_daemon.py` itself: "budget -> short pause + relaunch"; `supervise_classify.py`:
    "budgets reset on relaunch by design") — a bare-id marker would record only the FIRST terminal
    event ever reached under that id and silently drop every later, genuinely different one (e.g.
    the real eventual backlog-empty after two budget stops). `run_started_at` (the calling
    session's own run start, `loop._session_cursor`, since #955; the checkout's
    `load_cursor(sdlc_dir)["run_started_at"]` without a session) is written fresh by the start at
    the top of every `/sigma-loop` invocation, including every relaunch, so a relaunch's own
    terminal event claims independently while repeated polling WITHIN one unchanged drain (the
    original concern in #905's own issue body — "the polled 'next' verb... would emit a
    duplicate... on every idle poll")
    still dedupes against the same marker file.

    Storing the claimed set (not a bare boolean) keeps this correct across however many distinct
    `run_started_at` values one run id accumulates over its life, without ever needing to reset or
    expire the marker: a fresh `supervise_daemon.py` invocation always mints a fresh `run_id`
    (`supervise-<pid>-<timestamp>-<random>`), so a stale marker for a dead id is inert, harmless
    clutter, never consulted again — no TTL/cleanup needed for correctness.

    Fail-open, like every write this deep in `_next()`'s own call graph (`ledger.safe_append`,
    `_release_claim_lock`): a read/write failure here must never raise into `_next()`'s caller — the
    loop's single most frequently-invoked verb — so it returns `False` ("not yet claimed") on ANY
    failure reading/writing the marker (missing file, malformed or unexpectedly-shaped JSON,
    permissions, disk full) — deliberately broad, not just the I/O-error subset, because a
    journal marker must never be the reason `_next()` crashes.

    PR-review correction: `False` here makes the CALLER (`_emit_run_stop_once`) skip the ledger
    write too, not "attempt it anyway" — an earlier revision of this docstring claimed the
    opposite. Skipping is the deliberately safer choice: under a SUSTAINED fault (e.g. a
    permanently unwritable `.sdlc/state/run_stop/`), every claim attempt would keep returning
    `False` forever, so "attempt the write regardless of the claim" would reopen the exact
    duplicate-emit-on-every-poll problem this dedupe exists to close, just routed around a broken
    marker instead of through a working one. A lost dedupe row's WRITE, not just its claim, is the
    accepted cost — never a crash, and never an unbounded flood of duplicate rows either."""
    path = run_stop_marker_path(sdlc_dir, run_id)   # unsafe run_id -> ValueError, NOT caught below:
                                                     # a real caller bug, not a transient failure
    try:
        try:
            claimed = set(json.loads(path.read_text()).get("claimed", []))
        except Exception:                # noqa: BLE001 - any unreadable/malformed marker degrades
            claimed = set()               # to "no prior claims recorded", never a crash
        if run_started_at in claimed:
            return False
        claimed.add(run_started_at)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"claimed": sorted(claimed)}))
        return True
    except Exception:                    # noqa: BLE001 - fail-open; see docstring
        return False


def _git_out(root, argv):
    """`git <argv>` in `root` -> stdout BYTES, or None when git could not answer.

    Bytes, not `text=True`: git's `-z` output carries raw path bytes, and a path that is not valid
    UTF-8 (legal on every Linux filesystem) would raise inside `subprocess` before this function
    could decide anything about it. `content_fingerprint` decodes with `surrogateescape`, which
    round-trips back through `os`/`pathlib` unchanged.

    None is "git could not answer", never "the answer was empty" — the two mean opposite things to
    the caller (see `content_fingerprint`'s own `files is None` vs `files == {}` split)."""
    try:
        proc = subprocess.run(["git", *argv], cwd=str(root), capture_output=True)
    except Exception:                    # noqa: BLE001 - no git on PATH, unreadable cwd, anything
        return None
    return proc.stdout if proc.returncode == 0 else None


def _entry(path):
    """One path's content, canonically: what git would store for it, not what `stat` reports.

    `l` + the LINK TARGET's hash for a symlink (that is literally a symlink's blob content in git);
    `x`/`f` + the file's hash, so a `chmod +x` is a change — sha256 of the bytes alone cannot see a
    mode, and "sensitive to any edit" has to be true rather than asserted;
    `/` for a directory, which in a diff means a submodule. ITS GITLINK IS NOT FINGERPRINTED — a
    submodule bumped to a different commit reads identically here. Named as a limit rather than
    silently approximated: Sigma cuts its worktrees with plain `git worktree add`, which does
    not populate submodules at all, so this has no live case in the flow the gate exists for;
    `?` for anything that is not a regular file (a FIFO, a socket, a device) — which also stops
    `open()` blocking forever on a writer-less pipe;
    `-` for absent — a deletion the goal made, and the one entry that is not a hash."""
    try:
        if os.path.islink(path):
            return "l" + hashlib.sha256(
                os.readlink(path).encode("utf-8", "surrogateescape")).hexdigest()
        if os.path.isdir(path):
            return "/"
        if not os.path.isfile(path):
            # NOT REGULAR, so there is no content to hash — and this test is what keeps the gate
            # ALIVE, not merely correct: `open()` on a FIFO with no writer BLOCKS FOREVER, so an
            # untracked pipe anywhere in a goal's tree would hang `record done` and `work.py merge`
            # with no error and no timeout. A hang that looks like slowness is the worst shape a
            # gate can fail in (AGENTS.md: a component that has DIED must be distinguishable).
            # `?` rather than `-` so "unhashable" stays readable apart from "deleted" in the map.
            return "?"
        # CHUNKED, not `fh.read()`: the path set is bounded by the goal's diff, but a single entry
        # in it is not — one added fixture is enough to make a whole-file read the largest memory
        # term in the loop. 1 MiB at a time keeps this O(1) in RAM whatever the file is.
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(block)
        return ("x" if os.access(path, os.X_OK) else "f") + h.hexdigest()
    except Exception:               # noqa: BLE001 - deliberately broad, and DEFENSIVE rather than
        return "-"                  # tested: every reachable failure is already absorbed by the
                                    # `isfile` test above or is an OSError from the read, so the
                                    # breadth only covers a TOCTOU race (the path stops being what
                                    # the stat said between the two calls) that no test can provoke
                                    # deterministically. It stays because this runs AFTER the
                                    # proving command, where a raise would throw away a verify that
                                    # really did run -- but it is not a guard anything has seen red.


def content_fingerprint(root, base_ref, bookkeeping=None):
    """WHAT a verify actually ran against: this goal's own change at `root`, as a digest.

    -> `{"fingerprint": <hex>|None, "base_ref": ..., "files": {path: entry}|None, "detail": None|why}`
    Never raises. THREE outcomes, and collapsing any two of them is a bug:
      * `files` is a NON-EMPTY dict  -> measured, and `fingerprint` is its digest.
      * `files == {}`                -> measured, and there is nothing to compare (see LANDED below).
        `fingerprint` is None, deliberately — hashing the empty map yields `sha256("")`, a TRUTHY
        digest that would then mismatch every recorded one.
      * `files is None`              -> could NOT measure (no fork point, no git, no checkout).

    WHY NOT THE OBVIOUS THINGS. `head` (already in the evidence since #1985) cannot be it: SKILL.md's
    order is verify -> commit -> pr -> review -> merge -> record done, so at verify time the whole
    change is UNCOMMITTED and `head` is just the shared base — measured on this repo, goals
    1933/1934/1935/1936/1937/1962 all recorded the SAME `head`. Comparing it after `work.py commit`
    moved HEAD would refuse every goal ever. A whole-tree hash (`GIT_INDEX_FILE` + `write-tree`) is
    wrong for a different reason: it moves on every rebase, costs O(repo), and writes loose objects
    into the operator's repo from inside a read-only gate.

    So the unit is THE GOAL'S OWN CHANGE, relative to `merge-base(HEAD, <remote>/<base>)`. That one
    formulation survives all three mutations the documented flow performs between a green verify and
    `record done` — `git commit` (content unchanged), a bare `git fetch` (the fork point is the old
    one either way, and linked worktrees SHARE `refs/remotes/*`, so a sibling slot's fetch must not
    move it), and a clean rebase (the fork point becomes the new base and the replayed diff is the
    same) — while still moving on any edit, deletion, mode flip or retargeted symlink.

    THE PATH SET IS EXACTLY WHAT THE PR WILL CARRY: `git diff <fork>` (tracked changes, measured
    against the WORKING TREE, so uncommitted work counts) plus `git ls-files --others
    --exclude-standard` (the goal's new files, not yet tracked). That union is what `work.py
    commit`'s own `git add -A` stages, which is what makes a new file hash identically before and
    after it is committed. `--no-renames` so a rename reads as delete+add rather than one path.

    `bookkeeping` (Sigma's own `.sdlc` directory name) is DROPPED from that set, and the reason
    is a live defect this guard's own plan-review caught before it shipped: SKILL.md step 6 copies
    `<sdlc>/plans/<stem>.md` INTO the worktree after verify and before `commit` (`work.py pr`
    refuses to push without it), and `sigma-setup`'s `RUNTIME_IGNORES` covers `state/ ledger/ work/
    knowledge/ events/` and pointedly NOT `plans/`. On an adopter repo that file is
    untracked-and-not-ignored, so counting it refused EVERY goal on the documented path. This repo's
    own `.gitignore` (`.sdlc/*`) hid it — which is exactly why the control for it runs against a
    repo with no `.gitignore` at all. Nothing under the loop's own bookkeeping directory is ever
    what the proving command proved.

    LANDED IS NOT TAMPERED. Once the goal's commits are ancestors of its base, `merge-base` IS HEAD
    and the change measures empty. Two proven routes: with `merge_method: merge` any fetch (a
    sibling worktree's included) does it, and with `squash` any rebase after the land does
    (`skippedCherryPicks`). The caller must read `{}` as "nothing left to compare", never as a
    mismatch — `done_refusal` does.

    COST is the goal's own diff, not the repo: four short git reads plus one `sha256` per changed
    path. MEASURED by calling this function on this repo (2026-09-02, warm cache, one sample each):
    5 changed paths 37.7 ms · 167 paths / 7.1 MB 63.1 ms · 730 paths / 19.4 MB 133.6 ms. So it is
    ~37 ms of fixed cost (the four subprocess spawns) plus ~0.13 ms per changed path — 10x the
    change is well under 2x the call, and 100x the REPO is free, because the repo is never walked.
    The `files` map is the storage term and grows with the same 10x: 0.6 KB of evidence for an
    ordinary goal, 79.8 KB for that 730-path one, into `state/verify/` which nothing prunes (a
    pre-existing bound, not one this adds). Nothing is written to git — no index, no objects, no
    locks: `merge-base`, `diff --name-only`, `ls-files` and `rev-parse` are all reads."""
    out = {"fingerprint": None, "base_ref": base_ref, "files": None, "detail": None}
    if not root or not base_ref:
        out["detail"] = "no work record — the fork point is unknown"
        return out
    fork = _git_out(root, ["merge-base", "HEAD", base_ref])
    if fork is None:
        out["detail"] = f"`git merge-base HEAD {base_ref}` could not be read at {root}"
        return out
    fork = fork.decode("utf-8", "surrogateescape").strip()
    changed = _git_out(root, ["diff", "--no-renames", "--name-only", "-z", fork])
    # `--full-name` is not decoration either: `ls-files` reports paths relative to the CWD while
    # `diff --name-only` reports them relative to the repo TOP, and every path below is joined onto
    # ONE base. `root` is a worktree/project root in every live call, so the two agree today — but
    # if it ever were a subdirectory, the mismatch would resolve every entry to `-` (absent) and the
    # digest would then be CONSTANT regardless of the code: a gate that cannot fail, which is worse
    # than no gate. Anchoring both on `--show-toplevel` makes that unreachable rather than unlikely.
    others = _git_out(root, ["ls-files", "--others", "--exclude-standard", "--full-name", "-z"])
    top = _git_out(root, ["rev-parse", "--show-toplevel"])
    if changed is None or others is None or top is None:
        out["detail"] = f"the changed-path set could not be read at {root}"
        return out
    root = top.decode("utf-8", "surrogateescape").strip() or root
    paths = {p for p in (changed + others).decode("utf-8", "surrogateescape").split("\0") if p}
    files = {p: _entry(os.path.join(root, p)) for p in paths
             if not (bookkeeping and p.split("/", 1)[0] == bookkeeping)}
    out["files"] = files
    if not files:
        out["detail"] = f"no change against {base_ref} — nothing to fingerprint"
        return out
    blob = "\n".join(f"{p}\0{files[p]}" for p in sorted(files))
    out["fingerprint"] = hashlib.sha256(blob.encode("utf-8", "surrogateescape")).hexdigest()
    return out


def reanchor_content(sdlc_dir, goal):
    """Re-record the content fingerprint against the tree as it stands now. -> True if rewritten.

    WHY THIS EXISTS, AND WHY IT IS NOT AN AMNESTY. `work.py merge` calls `done_refusal` (its content
    gate), and only THEN rebases a BEHIND PR via `_reconcile_behind` before landing. A rebase
    rewrites files, and `rebase()`'s CHANGELOG union-merge rescue rewrites CHANGELOG.md
    DETERMINISTICALLY -- its own docstring calls that "the single most frequent conflict in this
    repo". So the fingerprint moved after the gate passed and before `record done` ran, and
    `record done` exited 4 on a PR that had ALREADY merged: the content shipped and only the
    bookkeeping broke, which is the worst of the two directions. Found in review of #1897/PR #2056.

    The gate exists to catch an edit NOBODY VERIFIED. A rebase the loop performed on itself is not
    that: it is verified content replayed onto a moved base, by this same process, between two of
    its own steps. So the loop re-anchors its own rebase and keeps refusing everything else --
    `test_reanchor_does_not_launder_an_edit_made_after_it` is the control that holds that line.

    NO CONTENT BLOCK, NO RE-ANCHOR. Legacy evidence (every one of the 333 files on this repo's disk
    when the gate shipped) carries none, and inventing one here would turn an ungated goal into a
    falsely-gated one on its next check."""
    ev = evidence_path(sdlc_dir, goal)
    try:
        data = json.loads(ev.read_text())
    except (OSError, ValueError):
        return False
    content = data.get("content") or {}
    if not content.get("fingerprint"):
        return False                       # legacy or already-declined evidence: leave it alone
    root = data.get("root")
    if not root or not pathlib.Path(root).is_dir():
        return False
    fresh = content_fingerprint(root, content.get("base_ref"), pathlib.Path(sdlc_dir).name)
    if not fresh.get("fingerprint"):
        return False                       # unreadable now -- keep the old anchor, refuse later
    data["content"] = fresh
    try:
        ev.write_text(json.dumps(data, indent=2))
    except OSError:
        return False
    return True


def enforce_enabled(verify):
    """`verify.enforce` as a bool, read generously (F17/#342): a real bool passes through; a string
    is off only when it spells false/no/off/0/empty; anything else is plain truthiness. `enforce`
    gates a refusal, so a truthy typo (`1`, `"true"`) must never silently leave the gate off.
    Lives here since #312 so `loop.py record done` and `work.py merge` read ONE copy (loop.py's
    `_enforce_enabled` is an alias; doctor.py keeps its standalone restatement, parity-tested)."""
    value = (verify or {}).get("enforce")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "0", "no", "off")
    return bool(value)


def acceptance_module():
    spec = importlib.util.spec_from_file_location("acceptance", _HERE / "acceptance.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def declared_verify_commands(goal, config, sdlc_dir=None):
    """The ordered proving commands `loop.py verify` runs, each in its own shell: goal frontmatter
    `verify_command` (local mode, goal given as a `.md` path), else config `verify.command`.
    With acceptance context, the repository command runs first, then the recorded command
    (or local fallback) in a separate subprocess. Without a record, retain legacy precedence.
    Shared by `verify_goal`, the `record done` refusal and the merge gate so none of them can
    disagree about whether there is anything to run (#228, #312)."""
    cmd = None
    goal_path = pathlib.Path(str(goal))
    if goal_path.suffix == ".md" and goal_path.exists():
        cmd = frontmatter.get(goal_path.read_text(), "verify_command")
        # `verify_command: ''` (or `""`, or a lone quote) declares nothing: the flat parser strips
        # only `"`, so `''` would otherwise reach the shell as a command (#228).
        if cmd is not None and not cmd.strip().strip("'\"").strip():
            cmd = None
    repo_cmd = ((config or {}).get("verify") or {}).get("command") or None
    if sdlc_dir is not None:
        try:
            acceptance = acceptance_module().read(sdlc_dir, goal)
        except FileNotFoundError:
            acceptance = None
        if acceptance is not None:
            goal_cmd = acceptance["verify_command"] or cmd
            commands = list(dict.fromkeys(c for c in (repo_cmd, goal_cmd) if c))
            return commands
    return [cmd or repo_cmd] if cmd or repo_cmd else []


def verify_command_label(commands):
    """Legacy single-command label; ordered JSON array when there are multiple commands.
    The label is evidence, not executable shell syntax. Separate shell invocations preserve
    `set -e`, `exit`, and platform-native command semantics for every proving command."""
    return commands[0] if len(commands) == 1 else json.dumps(commands) if commands else None


def declared_verify_command(goal, config, sdlc_dir=None):
    return verify_command_label(declared_verify_commands(goal, config, sdlc_dir))


def verify_required(config, goal, sdlc_dir=None):
    """#312: THE one rule for "must this goal carry THIS run's passing verify evidence before it may
    land?" -> the reason it must (a short phrase), or None when there is nothing to prove.

    Required when `verify.enforce` is on (the operator asked for machine-checked done), OR when a
    verify command is declared for the goal (there is something to run, so a merge without having
    run it is a merge nobody verified -- the pre-#312 merge contract, unchanged). Neither -> None:
    `loop.py verify` would exit 3 NO-COMMAND and write no evidence, so demanding evidence would
    park every merge forever on a condition no gesture can satisfy -- the defect #312 found on the
    documented "decline the verify command" path.

    Readers: `work.merge` gates on this whole rule. `loop.py record done` gates on its `enforce`
    half only (`enforce_enabled`), as it always has -- with `work.enabled` and a PR, `record done`
    additionally requires the PR MERGED, and the merge already passed this rule, so the command
    half reaches `done` through the merge. By construction the merge never requires LESS than
    `record done` does."""
    if enforce_enabled((config or {}).get("verify")):
        return "verify.enforce is on"
    if declared_verify_command(goal, config, sdlc_dir):
        return "a verify command is declared"
    return None


def verify_set_hint():
    """The one gesture every "no verify command" message names (#228), so the loop, the merge
    gate, /sigma-init and /sigma-doctor all point at the same fix. `python3`, else `python`, else
    `py` -- a PATH lookup, never an execution -- so the printed gesture runs where it is read."""
    import shutil
    py = next((n for n in ("python3", "python", "py") if shutil.which(n)), "python3")
    return (f"set one: {py} <installed-sigma>/skills/sigma-init/scripts/verify_detect.py "
            "detect . lists candidates with ids, `... confirm .sdlc <n> <id>` sets candidate n if "
            "its id still matches (enforce ON), "
            "or put your command in config verify.command; or `... decline .sdlc` to turn "
            "verify.enforce off")


#: THE ENFORCEMENT REGISTRY (#2740). Every gate this module implements, one entry each, read (never
#: imported) by skills/sigma-doctor/scripts/enforcement_table.py to render docs/enforcement.md.
#: A module-level function whose name ends `_refusal`/`_gate`/`_hold`/`_guard`, is `gate`, or
#: contains `blocked_by` must be listed here or in ENFORCEMENT_EXEMPT, or
#: tests/test_enforcement_table.py fails. After editing: regenerate the doc (command in its header).
#: Pure literals only (str/tuple/bool/None): the reader is `ast.literal_eval`, so it cannot follow a
#: name or a call. Text fields carry no `|` and no newline -- they land in a Markdown table cell.
ENFORCEMENT_GATES = (
    {"control": "Verify evidence before `record done`", "function": "done_refusal",
     "kind": "python-gate", "hosts": "all", "enabled_by": ("verify.enforce",), "settings": (),
     "mechanism": "`loop.py record done` exits 4 (REFUSED) without this run's passing verify "
                  "evidence; the command run is the local goal's `verify_command` frontmatter, else the repo-wide `verify.command` (GitHub-mode goals always use the latter)",
     "readme": "Machine-checked done"},
)
ENFORCEMENT_EXEMPT = ()


def done_refusal(sdlc_dir, goal):
    """None when fresh passing evidence exists for this goal, else the reason to refuse.
    Fresh = produced at/after this run's start (a stale green from yesterday proves nothing) AND,
    #498, by THIS run (a concurrent sibling's green, though fresh, must not be inherited). Since
    #955 "this run's start" is the start of the live session holding the goal, or the checkout's
    latest start when no live session holds it (`goal_run_started_at`).
    Lives here, not in loop.py, so the merge gate can require the same evidence without the two
    modules having to import each other."""
    ev = evidence_path(sdlc_dir, goal)
    if not ev.exists():
        return "no verify evidence for this goal"
    try:
        data = json.loads(ev.read_text())
    except Exception:             # noqa: BLE001 - unreadable evidence proves nothing either
        return "verify evidence is unreadable"
    if data.get("exit") != 0:
        return f"last verify FAILED (exit {data.get('exit')})"
    # Sub-second float compare (F11/#341): `at` and `run_started_at` used to be floored to whole
    # seconds by `int(time.time())` on both sides, so a verify at T-0.4s from a PRIOR run and a run
    # starting at T+0.3s both rounded to the same integer second — the floor made a stale green
    # indistinguishable from a fresh one (`at == run_started_at` -> `<` false -> wrongly accepted).
    # `load_cursor`/`start_run`/`verify_goal` now keep the raw float, so this comparison closes that
    # window on its own; a verify milliseconds after start (the normal verify-then-record sequence)
    # still compares strictly greater and stays accepted.
    # #955: "this run" is the run of the LIVE session(s) holding the goal, else the checkout's
    # latest start (`goal_run_started_at`), so another session's `start` cannot void this green.
    if data.get("at", 0) < goal_run_started_at(sdlc_dir, goal):
        return "verify evidence predates this run"
    # #498: run-id attribution, checked AFTER (never instead of) the exit-code and freshness guards
    # above — a concurrent sibling's green is exit-0 and NEWER than this run's start, so it slips
    # past freshness by design (F11/#341 only rejects a STALE green). `run` identifies the run that
    # WROTE this evidence; refuse whenever it is present and differs from THIS run's id — including
    # when this run is unattributed (`run_identity()` is None), so an un-id'd worker cannot inherit
    # an id-stamped sibling's green either. Legacy evidence from a pre-#498 loop.py has NO `run`
    # key, so `ev_run is None` and this check is skipped: such evidence stays governed by freshness
    # alone (graceful backward-compatibility, never a hard crash on a missing id).
    ev_run = data.get("run")
    if ev_run is not None and ev_run != run_identity():
        return (f"verify evidence was produced by a different run ({ev_run!r} != this run "
                f"{run_identity()!r}) — re-run verify in this run. #889: this now also fires for "
                f"two BARE sessions (ids like 'session-<pid>'), which were both unattributed "
                f"before and so silently inherited each other's green; the earlier wording named "
                f"a 'concurrent sibling', a cause this check cannot actually distinguish from the "
                f"same person's restarted session.")
    # #1897: WHAT was verified, checked LAST — after exit, freshness and attribution, for the same
    # reason #498's id check sits after them: a FAILED verify must report "last verify FAILED", not
    # a content mismatch, however much the tree also moved since.
    #
    # The five checks above are all about WHEN and WHO. None of them could see a file edit, so the
    # review-fix cycle SKILL.md itself prescribes ("fix them in the worktree ... re-run `loop.py
    # verify`") had nothing enforcing its second half: a green verify, an edit, and a later verify
    # that refused WITHOUT writing evidence (#1890's exit 4 is exactly that shape) left the earlier
    # green on disk, still fresh, still correctly attributed, and still sufficient.
    try:
        acceptance_hash = acceptance_module().digest(sdlc_dir, goal)
    except (OSError, ValueError):
        return "acceptance record is unreadable or invalid; re-run verify after repair"
    if data.get("acceptance_sha256") != acceptance_hash:
        return "acceptance record changed since verify; re-run verify against the recorded intent"
    content = data.get("content") or {}
    ev_fp = content.get("fingerprint")
    if not ev_fp:
        # No `content` key at all (every one of the 333 evidence files on this repo's disk when
        # #1897 shipped), or a run that could not measure one (no work record — `work.enabled: false`
        # — or a project-root fallback). Governed by the five checks above alone, exactly the
        # graceful degradation pre-#498 evidence without a `run` key already gets. Not a bypass in
        # practice: freshness already refuses anything written before this run started, so the only
        # evidence that reaches here is what this run's own `verify_goal` wrote.
        return None
    root = data.get("root")
    if not root or not pathlib.Path(root).is_dir():
        # The worktree is GONE, and Sigma is the one that removes it: `loop.py::_release_checkout`
        # runs inside `_record` AFTER this gate, and `work.finish` refuses a tree that still holds
        # work. So a missing root means a `done` already succeeded — a second `record done` (after a
        # crash, a compaction, or an agent repeating a terminal gesture) must stay the harmless
        # no-op it has always been, never a new exit 4.
        return None
    now = content_fingerprint(root, content.get("base_ref"), pathlib.Path(sdlc_dir).name)
    if now["files"] is None:
        # The tree is still THERE — git simply could not answer about it. "Could not check" must
        # never read as "checked and fine", so this is the one branch that fails CLOSED.
        return (f"verify evidence records what it verified, but that tree could not be re-read "
                f"({now['detail']}) — re-run `loop.py verify` for this goal")
    if not now["files"]:
        # Measured, and empty: the goal's change is no longer distinguishable from its base, which
        # is what a LANDED goal looks like (see content_fingerprint's own LANDED note). There is
        # nothing to compare, so the content gate declines rather than stranding the bookkeeping for
        # work that already shipped.
        return None
    if now["fingerprint"] != ev_fp:
        was, is_now = content.get("files") or {}, now["files"]
        moved = sorted(set(was) ^ set(is_now)
                       | {p for p in set(was) & set(is_now) if was[p] != is_now[p]})
        named = ", ".join(moved[:3]) + (f" (+{len(moved) - 3} more)" if len(moved) > 3 else "")
        return (f"the verified code changed after verify passed — {len(moved)} path(s) differ "
                f"({named}) — re-run `loop.py verify` for this goal")
    return None
