#!/usr/bin/env python3
"""Overnight supervisor — OWNS the loop's lifetime so limit exhaustion never means
permanent downtime, with ZERO polling: this process is blocked (0 CPU) while the
session runs and asleep otherwise. On each session exit the classifier
(`supervise_classify.py`) reads the output tail and picks one of four verdicts:
done -> exit · relaunch/sleep -> short pause (or sleep until a stated usage-limit
reset) then relaunch, attempt counter reset · backoff -> capped escalating pause,
attempt counter climbs. Stop it any time: touch <sdlc>/state/supervisor.stop

macOS laptop note: a sleeping MACHINE stops everything — run under `caffeinate -is`
(or keep the device on AC with sleep off); OS one-shot wake timers are the
hardened alternative for sleep-prone machines.

    supervise_daemon.py [sdlc_dir]      (default .sdlc)

Env:
  SIGMA_CLAUDE_CMD          — the session command (default: claude -p /sigma-loop).
  SIGMA_SUPERVISE_MAX_RUNS  — total session launches before giving up (default 48).
  SIGMA_SUPERVISE_SLEEP_SCALE — multiply every wait (tests set a small fraction,
                                    e.g. 0.01, to shrink real pauses to a few seconds;
                                    default 1). A FLOAT here, unlike the bash original,
                                    which only ever accepted a bash integer.
  SIGMA_RUN_ID              — pre-set by an outer launcher to pin the run-id (see
                                    the #498 rationale below); left unset, this daemon
                                    generates one.

This is a straight port of `supervise.sh` (bash) to pure Python — same state files,
same stdout/log routing, same exit codes on the three paths bash had, same anchored
`sleep=` extraction contract. Three deliberate divergences from byte-parity, each
documented at its own site below and in CHANGELOG.md rather than smuggled in quietly:

  (a) SIGMA_SUPERVISE_SLEEP_SCALE is now a float, not a bash integer.
  (b) `shlex.split(cmd)` applies real shell-style quote handling to
      SIGMA_CLAUDE_CMD, where bash's unquoted `$CMD` expansion retained quote
      characters LITERALLY (a variable's expansion is not re-quoted) — so
      `claude -p "hello world"` word-split differently under each: bash produced
      four tokens (quotes as literal characters); `shlex.split` produces three,
      consuming the quotes the way the operator almost certainly meant. This is a
      latent-bug fix, not a regression, but it IS an observable behaviour change.
  (c) A new, additive log/stdout line (`supervisor: pausing ...`, see below) and a
      new exit code 2 for a non-numeric env value (see below) — both are NEW
      observable behaviour, not present in the bash original.

These three are the only OBSERVABLE-OUTCOME divergences. Separately — and NOT counted above,
because the goal there is restoring bash's own outcome, not changing it — two failure modes bash
survived for free (via `set -uo pipefail` WITHOUT `-e`: a failed shell redirection just prints to
stderr and the loop continues) needed EXPLICIT handling here, since Python raises where the shell
merely warns: (1) `SIGMA_CLAUDE_CMD` naming a missing/unusable executable (or splitting to an
empty argv) is caught and turned into a `_CRASH_SIG`-matching diagnostic in `$RUNOUT`, so the
classifier reaches the same crash-backoff verdict bash's own "command not found" line produced;
(2) every write to `supervisor.log`/`supervisor.tail` is tolerant of `OSError` (an unwritable
`state/` dir or a read-only file), warned once per distinct target via `_warn_unwritable_once`
rather than either crashing or going silent about it. Both are RESILIENCY fixes matching bash's
own designed tolerance, not new behaviour to disclose as a divergence.

Windows (#2494, validated against a real windows-latest GitHub Actions runner): plain
`shlex.split(cmd)` (POSIX mode) is GENUINELY BROKEN on win32, and not for the quoting reason this
docstring used to speculate about. POSIX shlex treats a bare backslash as an escape character
EVEN OUTSIDE quotes, so it silently eats every backslash in an ordinary Windows path — splitting
`C` colon backslash `Python312` backslash `python.exe -c 1` returns `C:Python312python.exe`,
`-c`, `1` as the first token — every backslash gone, not a quoting mismatch but outright path
corruption. Since almost every real
`SIGMA_CLAUDE_CMD` on Windows names an executable by its backslash-separated path, this was
not a theoretical/quoting-only risk: it broke the FIRST word of the command on every real
invocation. `_split_claude_cmd` below fixes this: on win32 it splits with `shlex.split(cmd,
posix=False)` (which preserves backslashes verbatim and still groups a `"..."`-quoted argument as
one token) and then strips one matching pair of leading/trailing double quotes off each token —
covering a plain path (unaffected either way) and a simple `"quoted argument"` (the case
`test_windows_real.py::test_supervise_relaunch_stop_file_and_run_id_through_a_quoted_windows_command`
exercises for real). This is NOT a byte-faithful implementation of MSVCRT/CommandLineToArgvW argv
parsing — a quoted argument containing an ESCAPED embedded quote (`\"`) is not specially handled
and is not claimed to be. POSIX (`sys.platform != "win32"`) behaviour is completely unchanged.
"""
import os
import pathlib
import random
import re
import shlex
import subprocess
import sys
import time

if __name__ == "__main__":
    # On hosts without an external pycache prefix, importing sibling modules
    # otherwise dirties the verified Git install before Codex preflight runs.
    # Keep Claude's historical import/bytecode behavior unchanged.
    _host_hint = os.environ.get("SIGMA_HOST")
    _codex_marker = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")
    # Mirror legacy.getenv's non-empty primary/retired fallback before importing
    # siblings, which would otherwise write bytecode into a verified Codex install.
    _claude_override = os.environ.get("SIGMA_" + "CLAUDE_CMD") or os.environ.get(
        "LOOP" + "SMITH_CLAUDE_CMD")
    if _host_hint == "codex" or (not _host_hint and _codex_marker and not _claude_override):
        sys.dont_write_bytecode = True



def _codex_runtime():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "codex_runtime", pathlib.Path(__file__).resolve().parent / "codex_runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_GUARD = []


def _guard():
    """resource_guard, loaded once (#1603): the one place a model session asks for admission."""
    if not _GUARD:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "resource_guard", pathlib.Path(__file__).resolve().parent / "resource_guard.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _GUARD.append(module)
    return _GUARD[0]


def _load_legacy():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "legacy", pathlib.Path(__file__).resolve().parent / "legacy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_logroll():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "logroll", pathlib.Path(__file__).resolve().parent / "logroll.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: #460: size-capped rotation for supervisor.log.
logroll = _load_logroll()

#: #239: operator env vars may still carry the plugin's previous prefix (`SIGMA_RUN_ID` never does).
legacy = _load_legacy()


def _split_claude_cmd(cmd):
    """Word-split `SIGMA_CLAUDE_CMD` for `subprocess.run`. POSIX: `shlex.split(cmd)`,
    unchanged. win32 (#2494): `shlex.split(cmd, posix=False)` so backslashes in a Windows path
    survive intact (plain POSIX-mode shlex silently eats them — see the module docstring), then
    strip one matching pair of wrapping double quotes off each token so a `"quoted argument"`
    still arrives as one clean token rather than one token WITH the quote characters still in it.
    Only a token that both STARTS and ENDS with `"` (len >= 2) is unwrapped — a bare `"` alone, or
    one only on one end, is left as-is rather than guessed at."""
    tokens = shlex.split(cmd, posix=(sys.platform != "win32"))
    if sys.platform != "win32":
        return tokens
    return [t[1:-1] if len(t) >= 2 and t[0] == '"' and t[-1] == '"' else t for t in tokens]

_HERE = pathlib.Path(__file__).resolve().parent

# #498: the run-id evidence guard. A per-worker id, stable across THIS supervisor's
# own relaunches (so a session's `loop.py verify` and its later `loop.py record`
# share it), and unique BETWEEN concurrent supervisors (two workers = two daemon
# processes = two ids). Every child session — and, unlike the bash original,
# `supervise_classify.py` too, since both now go through the same explicit
# `env=child_env` — inherits it, so state.done_refusal can refuse a CONCURRENT
# sibling's green instead of silently inheriting it. Without a producer here the
# guard degrades to freshness-only (the pre-#498 bug). An id set by an outer
# launcher is respected (never clobbered); `os.getpid()` + `int(time.time())` +
# a bounded random tail keep it practically unique between concurrent supervisors
# — exactness of range is not the guarantee, non-collision-in-practice is (the
# same guarantee bash's `$RANDOM` gave, since it is not cryptographically unique
# either). `random.randint(0, 32767)` mirrors bash `$RANDOM`'s range closely
# enough to preserve that property.
#
# H2 (plan-review, BLOCKING): the bash original `export`ed this into its own
# process environment, and an earlier draft of this port did the same via
# `os.environ[...] = run_id`. That is WRONG in Python: `os.environ` is this
# process's global, interpreter-wide environment, and importing this module (as
# the test suite's `_daemon_mod()` does, to reach `extract_sleep_seconds` and
# `CLASSIFIER_UNAVAILABLE_FALLBACK` directly) must never be able to mutate a
# caller's environment as a side effect — `monkeypatch.delenv` cannot revert a
# raw `os.environ[...] = ...` performed deep inside `main()`, so an in-process
# call would leak `SIGMA_RUN_ID` into whatever test runs next (this bit
# `tests/test_state.py`, which depends on the exact absence/presence of that
# key). The fix: never write to `os.environ` at all. Build `child_env` ONCE —
# a plain dict, `{**os.environ, "SIGMA_RUN_ID": run_id}` — and pass it via
# `env=child_env` explicitly to BOTH subprocess launches below (the session
# command and the classifier). This delivers the identical observable contract
# `export` gave (every child and grandchild of the LAUNCHED subprocess inherits
# one stable id) with zero global mutation.


def _date_str():
    """Bash `$(date)`'s default locale format, close enough for a log line — no
    test pins its exact byte-format, only its presence (see plan §A item 11)."""
    return time.strftime("%a %b %e %H:%M:%S %Z %Y")


def _append(path, text):
    """Append `text` to `path`, creating it if needed. Does NOT catch OSError itself —
    an unwritable log is a genuine failure the caller should see. The ONE call site
    that needs bash's `cat ... >> "$LOG" 2>/dev/null || true` (item 13) tolerance
    wraps ITS OWN read/write in a try/except OSError explicitly, rather than this
    helper swallowing failures for every caller silently."""
    logroll.append(path, text if text.endswith("\n") else text + "\n")


def _warn_unwritable_once(warned, key, message):
    """Print `message` to stderr the FIRST time `key` is seen in this process, then stay
    silent for every later occurrence of the same `key`. Cycle-2 RESILIENCY fix: bash's
    original degradation here was NOISY (each failed redirection/`tee` printed its own
    "Permission denied" to stderr), and that signal is worth preserving — an operator
    should be told the log/tail is unwritable — but a persistent problem (an unwritable
    state/ dir doesn't heal itself mid-run) would otherwise print that same line on EVERY
    iteration forever, which floods the console/log with no new information. One line per
    DISTINCT failure target is the middle ground: loud enough to notice, not a flood."""
    if key in warned:
        return
    warned.add(key)
    print(message, file=sys.stderr)


def _log_and_print(path, line, warned):
    """Append `line` to the log AND print it to stdout — the `tee -a` idiom. Used
    at every place both destinations get a line: stop-file, max-runs, the verdict,
    and the new pausing line (see the stdout-vs-log routing table in plan §A).

    Cycle-2 RESILIENCY fix: `_append` itself does NOT catch OSError (see its own
    docstring) — an unwritable log used to propagate straight out of here and kill the
    whole daemon (bash survived this for free via `set -uo pipefail` without `-e`; the
    shell's own failed redirections just printed to stderr and the loop kept going). So
    THIS caller catches it, warns once via `_warn_unwritable_once`, and — critically —
    still prints `line` to stdout regardless: stdout and the log file are independent
    destinations, and one being broken must never silence the other."""
    try:
        _append(path, line)
    except OSError as exc:
        _warn_unwritable_once(warned, str(path), f"supervisor: cannot write {path} ({exc}) — continuing without it")
    print(line)


def _bounded_copy(content, cap):
    """What of one session's output goes into `supervisor.log` (#460): all of it when it fits the
    log's cap, else its last `cap` BYTES (an error is the last thing a crashing session prints; a
    byte trim, so multi-byte text cannot exceed the cap) behind a one-line marker. Without this one
    long session could write far past the cap in a single append."""
    raw = content.encode("utf-8", errors="replace")
    if len(raw) <= cap:
        return content
    return ("supervisor: session output truncated to its last %d of %d bytes\n%s"
            % (cap, len(raw), raw[-cap:].decode("utf-8", errors="ignore")))


def _pause(seconds):
    """Thin, named wrapper around `time.sleep` — kept separate (rather than calling
    `time.sleep` inline) so it is a single, monkeypatchable seam for tests that
    need to intercept a pause without actually waiting."""
    time.sleep(seconds)


def _last_n_lines(text, n):
    """The last `n` lines of `text`, joined with `\\n` — the `tail -n 50` idiom
    (item 14). Preserves a trailing newline when `text` had one, matching real
    `tail`'s output byte-for-byte (an earlier draft dropped it — a review caught this
    as a FOURTH, undocumented divergence from the module docstring's claimed three;
    restoring parity here is the fix, not adding a fourth item to that list).
    `splitlines()` itself discards the terminator, so it is reattached explicitly.
    On any read failure the caller writes an empty string, matching bash's
    `: > "$TAILF"` truncation-on-failure."""
    lines = text.splitlines()[-n:]
    out = "\n".join(lines)
    if text.endswith("\n"):
        out += "\n"
    return out


# #744/#745: the verdict-line <-> extractor coupling. `supervise_classify.py`'s
# `main()` prints exactly one line, `f"action={action} sleep={secs} reason={reason}"`
# — this daemon extracts `secs` from that EXACT shape with an ANCHORED regex, never
# an unanchored/greedy search. Why anchoring matters (PR #744 blocking finding,
# #176): `_ABNORMAL`'s `reason` field is the ONE place in `supervise_classify.py`
# where session-authored free text enters the verdict line, and a session can name
# its own `sleep=<n>` inside that free text (e.g. "LOOP STOP: worktree corrupt; env
# dump showed leftover sleep=999999 from a prior test"). An UNANCHORED, greedy
# search (the historical bash `sed -n 's/.*sleep=\([0-9]*\).*/\1/p'`, ported
# naively as `re.findall(r"sleep=(\d*)", verdict)[-1]`) captures the LAST
# `sleep=<digits>` on the line — which may be the session's injected number, not
# the classifier's own fixed-position field — inflating a 300s pause into an
# ~11.5-day one while the log still claims 300s.
#
# Anchor-by-anchor justification against the sed this replaces (H4, plan-review
# non-blocking #4 — corrected here to the BYTE-FAITHFUL translation):
#   ^                — sed's `s/^...` anchors to line start; `re.match` already
#                       anchors to the string's start, equivalent for a single-line
#                       verdict (the classifier prints exactly one `.strip()`-ready
#                       line, so "string start" and "line start" coincide here —
#                       the one place this would diverge, embedded newlines inside
#                       the verdict itself, cannot occur since nothing that reaches
#                       this line contains one).
#   action=[^ ]*      — sed matches literal `action=` then any run of NON-SPACE
#                       characters. H4 corrected an earlier draft here: `[^ ]*` is
#                       ZERO-or-more and admits a literal tab; Python's `\S+` is
#                       ONE-or-more and admits no whitespace at all — NOT the same
#                       set. `[^ ]*` is the byte-faithful translation. (The
#                       divergence is unreachable on real input either way, since
#                       `supervise_classify.py` never emits a tab here — but the
#                       plan's job is to remove the question, not argue it's safe.)
#    sleep=(\d*)      — a literal space, `sleep=`, then a CAPTURED, possibly-EMPTY
#                       digit run — `\d*` (zero-or-more) matches the same empty
#                       string sed's `\([0-9]*\)` does, so the `${secs:-1800}`
#                       fallback (below) still triggers correctly when the field
#                       is genuinely empty.
#    .*               — sed's pattern ends ` .*` (a literal space then any-chars-
#                       to-end), REQUIRING at least one more field (`reason=...`)
#                       to follow. Keeping this trailing ` .*` costs nothing (every
#                       real verdict has a `reason=` field) and keeps the port's
#                       acceptance surface identical to the shell original.
_SLEEP_RE = re.compile(r"^action=[^ ]* sleep=(\d*) .*")


def extract_sleep_seconds(verdict):
    """Pull the `secs` field out of a verdict line, or `""` if the line doesn't
    match the expected shape (empty is the correct extraction-failure signal — the
    caller applies the `${secs:-1800}` fallback on an empty string, exactly as the
    bash original did). Module-level and importable on purpose: tests call THIS
    function directly (never a pasted copy of the regex), which is what proves the
    anchor is the daemon's own production code, not a test-only stand-in."""
    m = _SLEEP_RE.match(verdict)
    return m.group(1) if m else ""


_ACTION_RE = re.compile(r"^action=(\S+)")


def _parse_action(verdict):
    """The first token after `action=` — `${verdict#action=}` then `${action%% *}`
    in the bash original. `\\S+` is fine here (unlike the sleep extractor, this
    doesn't need byte-parity with sed): the action token is drawn from a fixed,
    small vocabulary (`done`/`relaunch`/`sleep`/`backoff`) that never contains
    whitespace, so `[^ ]*` vs `\\S+` cannot diverge on any verdict this daemon
    ever actually sees."""
    m = _ACTION_RE.match(verdict)
    return m.group(1) if m else ""


# The exact fallback string bash's `|| echo '...'` printed on a failed/missing
# classifier subprocess (item 15). Module-level so tests import the constant by
# name rather than re-typing (and risking a silent drift from) the literal.
CLASSIFIER_UNAVAILABLE_FALLBACK = "action=backoff sleep=1800 reason=classifier unavailable"


USAGE = "usage: supervise_daemon.py [sdlc_dir]"


def _capture_codex(argv_cmd, child_env, repo, cap, final_path=None):
    """Drain a Codex worker while retaining at most one log-cap of output in RAM.

    The CLI's last-message side channel separates authored output from echoed
    prompts and tool text. Its single transient file is capped and removed.
    """
    recent = bytearray()
    total = 0
    final_file = pathlib.Path(final_path) if final_path is not None else None
    if final_file is not None:
        final_file.unlink(missing_ok=True)
    with subprocess.Popen(argv_cmd, env=child_env, cwd=repo, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as proc:
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            total += len(chunk)
            recent.extend(chunk)
            if len(recent) > cap:
                del recent[:-cap]
        exit_code = proc.wait()
    if total > cap:
        # The ring begins mid-stream and may begin mid-LINE. Never promote a
        # marker substring into a fresh line-start classifier marker.
        first_newline = recent.find(b"\n")
        recent = recent[first_newline + 1:] if first_newline >= 0 else bytearray()
    output = recent.decode("utf-8", errors="replace")
    if total > cap:
        output = f"supervisor: Codex output retained last {cap} of {total} bytes\n" + output
    final = None
    if final_file is not None and final_file.exists():
        try:
            if final_file.stat().st_size > cap:
                raise ValueError("Codex final message exceeded capture limit")
            final = final_file.read_text(encoding="utf-8", errors="replace") or None
        finally:
            final_file.unlink(missing_ok=True)
    return output, exit_code, final


def _codex_main(sdlc_dir, max_runs, scale, run_id):
    """Codex-only supervised path. Claude's historical loop below is untouched."""
    codex_runtime = _codex_runtime()
    state = pathlib.Path(sdlc_dir) / "state"
    state.mkdir(parents=True, exist_ok=True)
    try:
        held_lock = codex_runtime.lock_file(state / "codex-supervisor.lock")
        executable, skill = codex_runtime.installed_skill()
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"supervisor: Codex preflight refused: {exc}", file=sys.stderr)
        return 2
    # Keep the file descriptor alive until this function returns. The OS releases
    # its lock after crash or kill; the file's existence is not a liveness test.
    with held_lock:
        child_env = codex_runtime.codex_environment({**os.environ, "SIGMA_RUN_ID": run_id})
        repo = pathlib.Path(sdlc_dir).resolve().parent
        log = state / "supervisor.log"
        stopf = state / "supervisor.stop"
        warned = set()
        attempt = 0
        runs = 0
        while runs < max_runs:
            if stopf.exists():
                _log_and_print(log, "supervisor: stop-file present — exiting", warned)
                return 0
            try:
                # The registry is a second admission layer. It also catches a
                # pre-existing Claude worker before Codex starts, though a new
                # Claude worker can still race because Claude does not take this lock.
                active = subprocess.run([sys.executable, str(_HERE / "loop.py"),
                                         "session-active", str(sdlc_dir)],
                                        env=child_env, capture_output=True, text=True,
                                        timeout=20, check=False)
            except (OSError, subprocess.TimeoutExpired) as exc:
                print(f"supervisor: session admission failed: {exc}", file=sys.stderr)
                return 2
            if active.returncode or active.stdout.strip() not in ("ACTIVE", "FREE"):
                print("supervisor: session admission could not be verified: " + active.stderr,
                      file=sys.stderr)
                return 2
            if active.stdout.strip() == "ACTIVE":
                _log_and_print(log, "supervisor: another Sigma session is active — waiting", warned)
                _pause(60 * scale)
                continue
            runs += 1
            try:
                # Revalidate on every launch: cache replacement/disablement
                # after startup must not turn a stale path into a silent retry.
                current_executable, current_skill = codex_runtime.installed_skill(child_env)
                if current_executable != executable or current_skill != skill:
                    raise ValueError("enabled Codex Sigma install changed during supervision")
            except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
                print(f"supervisor: Codex install changed: {exc}", file=sys.stderr)
                return 2
            override = os.environ.get("SIGMA_CODEX_CMD")
            final_path = state / "supervisor.agent-last"
            if override is not None:
                try:
                    argv_cmd = _split_claude_cmd(override)
                except ValueError as exc:
                    print(f"supervisor: invalid SIGMA_CODEX_CMD: {exc}", file=sys.stderr)
                    return 2
                if not argv_cmd:
                    print("supervisor: SIGMA_CODEX_CMD is empty", file=sys.stderr)
                    return 2
                argv_cmd.extend(["--output-last-message", str(final_path),
                                 codex_runtime.build_codex_command(executable, repo, skill)[-1]])
            else:
                argv_cmd = codex_runtime.build_codex_command(executable, repo, skill, final_path)
            _log_and_print(log, f"supervisor: Codex run #{runs + 1} — {_date_str()}", warned)
            try:
                with _guard().session_lease("supervisor", run_id, repo=str(repo)) as lease:     # #1603
                    held = lease if lease is not None and hasattr(lease, "reason") else None
                    if held is None:
                        output, exit_code, final = _capture_codex(
                            argv_cmd, child_env, repo, logroll.cap_bytes(log.parent), final_path)
                if held is not None:
                    _log_and_print(log, f"supervisor: session held ({held.reason}) — pausing", warned)
                    _pause(min(held.retry_after, 3600.0) * scale)
                    continue
            except ValueError as exc:
                print(f"supervisor: Codex final message refused: {exc}", file=sys.stderr)
                return 2
            except OSError as exc:
                output, exit_code, final = f"codex command not found: {exc}", 127, None
            try:
                logroll.append(log, _bounded_copy(output, logroll.cap_bytes(log.parent)))
            except OSError as exc:
                _warn_unwritable_once(warned, str(log), f"supervisor: cannot write {log} ({exc})")
            if (final and "CODEX_SIGMA_SKILL_UNAVAILABLE" in
                    {line.strip() for line in final.splitlines()}) or not skill.is_file():
                print("supervisor: verified Sigma skill became unavailable; refusing", file=sys.stderr)
                return 2
            if final_path is not None and exit_code == 0 and final is None:
                print("supervisor: Codex returned no final agent message; refusing", file=sys.stderr)
                return 2
            try:
                _, post_skill = codex_runtime.installed_skill(child_env)
                if post_skill != skill:
                    raise ValueError("installed skill path changed")
            except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
                print(f"supervisor: Codex install changed after run: {exc}", file=sys.stderr)
                return 2
            classifier = _HERE / "supervise_classify.py"
            try:
                tailf = state / "supervisor.tail"
                tailf.write_text(_last_n_lines(final if final is not None else output, 50))
                verdict_result = subprocess.run([sys.executable, str(classifier),
                                                 str(tailf), str(attempt)],
                                                env=child_env, capture_output=True, text=True,
                                                timeout=20, check=False)
                verdict = (verdict_result.stdout.strip() if verdict_result.returncode == 0
                           else CLASSIFIER_UNAVAILABLE_FALLBACK)
            except (OSError, subprocess.TimeoutExpired):
                verdict = CLASSIFIER_UNAVAILABLE_FALLBACK
            if exit_code and _parse_action(verdict) in ("done", "relaunch"):
                verdict = "action=backoff sleep=300 reason=Codex exited nonzero despite a clean marker"
            _log_and_print(log, f"supervisor: Codex exit={exit_code} {verdict}", warned)
            action = _parse_action(verdict)
            if action == "done":
                return 0
            attempt = 0 if action in ("relaunch", "sleep") and exit_code == 0 else attempt + 1
            secs = extract_sleep_seconds(verdict)
            sleep_for = (int(secs) if secs else 1800) * scale
            if sleep_for > 0:
                _log_and_print(log, f"supervisor: pausing {sleep_for:.1f}s", warned)
                _pause(sleep_for)
        _log_and_print(log, f"supervisor: max runs ({max_runs}) reached — exiting", warned)
        return 1


def main(argv):
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    sdlc_dir = argv[1] if len(argv) > 1 else ".sdlc"

    try:
        # An explicit Claude command is an existing operator choice. A Codex
        # desktop marker in the parent shell must not repurpose that command.
        if legacy.getenv("SIGMA_CLAUDE_CMD") and not os.environ.get("SIGMA_HOST"):
            codex_marker = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")
            claude_marker = os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_SESSION_ID")
            if codex_marker and claude_marker:
                raise ValueError("both Claude and Codex host markers are present; set SIGMA_HOST")
            host = "claude"
        else:
            host = _codex_runtime().detect_host()
    except ValueError as exc:
        print(f"supervisor: {exc}", file=sys.stderr)
        return 2

    cmd = legacy.getenv("SIGMA_CLAUDE_CMD", "claude -p /sigma-loop")

    # H5 (plan-review, non-blocking #5): bash's `$(( secs * SCALE ))` silently
    # coerces a non-numeric SCALE/MAX_RUNS to 0 — which, for MAX_RUNS, means an
    # immediate "0 runs allowed" exit, and for SCALE, disables every pause and
    # turns the supervisor into a hot relaunch loop bounded only by MAX_RUNS.
    # Python's `int()`/`float()` raise instead — and AGENTS.md SAFETY says the
    # code must REFUSE LOUDLY rather than proceed weakly on a guarantee it can't
    # make. So: catch the ValueError, name the bad variable and value on stderr,
    # and return a NEW exit code (2) that no existing caller/test relied on.
    raw_max_runs = legacy.getenv("SIGMA_SUPERVISE_MAX_RUNS", "48")
    try:
        max_runs = int(raw_max_runs)
    except ValueError:
        print(f"supervisor: SIGMA_SUPERVISE_MAX_RUNS is not a valid integer: {raw_max_runs!r}",
              file=sys.stderr)
        return 2

    raw_scale = legacy.getenv("SIGMA_SUPERVISE_SLEEP_SCALE", "1")
    try:
        scale = float(raw_scale)
    except ValueError:
        print(f"supervisor: SIGMA_SUPERVISE_SLEEP_SCALE is not a valid number: {raw_scale!r}",
              file=sys.stderr)
        return 2

    run_id = os.environ.get("SIGMA_RUN_ID") or (
        f"supervise-{os.getpid()}-{int(time.time())}-{random.randint(0, 32767)}")
    if host == "codex":
        return _codex_main(sdlc_dir, max_runs, scale, run_id)
    # Never written to this process's own os.environ (H2 above) — built once and
    # handed explicitly to every subprocess launch below.
    child_env = {**os.environ, "SIGMA_RUN_ID": run_id}

    state = pathlib.Path(sdlc_dir) / "state"
    repo_dir = pathlib.Path(sdlc_dir).resolve().parent
    log = state / "supervisor.log"
    tailf = state / "supervisor.tail"
    stopf = state / "supervisor.stop"
    runout = state / "supervisor.run.out"
    state.mkdir(parents=True, exist_ok=True)
    # Cycle-2 RESILIENCY fix: distinct-failure-target dedupe for `_warn_unwritable_once` below —
    # an unwritable state/ dir (or a specific file inside it) doesn't heal itself mid-run, so
    # without this every one of up to `max_runs` iterations would re-print the same stderr line.
    warned = set()

    attempt = 0
    runs = 0
    while True:
        if stopf.exists():
            _log_and_print(log, "supervisor: stop-file present — exiting", warned)
            return 0
        if runs >= max_runs:
            _log_and_print(log, f"supervisor: max runs ({max_runs}) reached — exiting", warned)
            return 1
        runs += 1
        # Log-only, never stdout — see the stdout-vs-log routing table in the
        # module docstring / plan §A. A per-run marker so the log reads as a
        # timeline even though nothing about this line is interesting live.
        # Cycle-2 RESILIENCY fix: this is the FIRST write of every iteration, and bash survived
        # an unwritable state/ dir here for free (a failed shell redirection is not an exception);
        # an unhandled OSError from `_append` used to kill the whole daemon before it even tried
        # to launch a session. Caught and warned-once exactly like `_log_and_print` does.
        try:
            _append(log, f"supervisor: run #{runs} — {_date_str()}")
        except OSError as exc:
            _warn_unwritable_once(warned, str(log), f"supervisor: cannot write {log} ({exc}) — continuing without it")

        # Per-run capture: classify THIS session's output only — a cumulative-log
        # tail could bleed a previous run's stop text into the verdict.
        # `_split_claude_cmd` is the documented Python analogue of shell word-splitting for a
        # command-line string (see divergence (b) in the module docstring, and #2494's fix for
        # POSIX-mode shlex silently eating backslashes in a Windows path).
        argv_cmd = _split_claude_cmd(cmd)
        try:
            if not argv_cmd:
                # An empty/whitespace-only SIGMA_CLAUDE_CMD splits to `[]`, and
                # `subprocess.run([])` raises IndexError -- the same crash-by-another-route as a
                # missing executable, so it is routed through the identical handling below rather
                # than left as a second, uncaught path.
                raise IndexError("SIGMA_CLAUDE_CMD is empty after word-splitting")
            with _guard().session_lease("supervisor", run_id, repo=str(repo_dir)) as lease:   # #1603
                held = lease if lease is not None and hasattr(lease, "reason") else None
                if held is None:
                    with open(runout, "wb") as f:
                        subprocess.run(argv_cmd, stdout=f, stderr=subprocess.STDOUT,
                                        env=child_env, check=False)
            if held is not None:        # a crash-backoff pause: skip this launch, then relaunch
                _log_and_print(log, f"supervisor: session held ({held.reason}) — pausing", warned)
                _pause(min(held.retry_after, 3600.0) * scale)
                continue
        except (OSError, IndexError) as exc:
            # The bash original got this survival for free from the SHELL: a failed exec of a
            # missing/unusable command is a 127 exit code plus a "command not found" message on
            # stderr, not an exception -- so $RUNOUT already contained a `_CRASH_SIG`-matching
            # line ("command not found") and the loop simply continued into the ordinary
            # crash-backoff ladder. Python's `subprocess.run` has no such free lunch: a
            # nonexistent/unusable executable raises `FileNotFoundError`/`PermissionError`/
            # `NotADirectoryError` (all `OSError`), and an empty argv (above) raises `IndexError`
            # -- either one, left uncaught, kills this WHOLE daemon process with a traceback
            # instead of surviving the failed launch, which is exactly the overnight failure mode
            # (`claude` not on PATH in a headless/cron env) this component exists to survive
            # (AGENTS.md RESILIENCY: "every failure mode has a stated recovery"). So: catch it,
            # and manufacture the same `_CRASH_SIG`-matching signal the shell gave away free, in
            # `$RUNOUT`, so the classifier reads a genuine crash signature exactly as it would
            # from the shell's own message.
            try:
                runout.write_text(f"supervise_daemon.py: {cmd!r}: command not found ({exc})\n")
            except OSError:
                pass

        # cat "$RUNOUT" >> "$LOG" 2>/dev/null || true — never fatal. Cycle-2 RESILIENCY fix:
        # bash's own `2>/dev/null` was silent about a read failure but NOISY about a write
        # failure (the `>>` redirection itself prints "Permission denied" to stderr) — warn
        # once on OSError rather than swallowing it completely, matching that asymmetry.
        try:
            content = runout.read_text(errors="replace")
            logroll.append(log, _bounded_copy(content, logroll.cap_bytes(log.parent)))
        except OSError as exc:
            _warn_unwritable_once(warned, str(log), f"supervisor: cannot write {log} ({exc}) — continuing without it")

        # tail -n 50 "$RUNOUT" > "$TAILF" 2>/dev/null || : > "$TAILF"
        try:
            text = runout.read_text(errors="replace")
            tailf.write_text(_last_n_lines(text, 50))
        except OSError as exc:
            # Cycle-2 RESILIENCY fix, the SHARPER instance the review caught: this fallback
            # (bash's `: > "$TAILF"` truncation-on-failure) must not ITSELF be capable of
            # raising uncaught — a recovery path that can crash is not a recovery path. If the
            # primary write failed because the file/dir is unwritable, this truncation attempt
            # fails for the identical reason, so it gets its OWN try/except rather than trusting
            # the outer one (which has already been left by the time this line runs).
            try:
                tailf.write_text("")
            except OSError:
                pass
            _warn_unwritable_once(warned, str(tailf), f"supervisor: cannot write {tailf} ({exc}) — continuing without it")

        classifier = _HERE / "supervise_classify.py"
        result = subprocess.run(
            [sys.executable, str(classifier), str(tailf), str(attempt)],
            capture_output=True, text=True, env=child_env)
        # stderr is deliberately never read — matches bash's `2>/dev/null` on the
        # classifier call (discarded, not logged).
        verdict = result.stdout.strip() if result.returncode == 0 else CLASSIFIER_UNAVAILABLE_FALLBACK

        action = _parse_action(verdict)
        secs = extract_sleep_seconds(verdict)
        _log_and_print(log, f"supervisor: {verdict}", warned)

        if action == "done":
            return 0
        elif action in ("relaunch", "sleep"):
            attempt = 0
        else:
            attempt += 1

        secs_val = int(secs) if secs else 1800
        sleep_for = secs_val * scale
        if sleep_for > 0:
            # D5 (plan §A item 21): a new, additive oracle line — not present in
            # the bash original — closing the #745 gap where the log's CLAIMED
            # pause and the process's REAL pause could diverge silently (a future
            # bug between `secs_val` and what's actually passed to `_pause` would
            # otherwise be invisible). `:.1f` so `300 * 0.01` renders `3.0`, not
            # IEEE 754's `2.9999999999999996`.
            _log_and_print(log, f"supervisor: pausing {sleep_for:.1f}s (extracted sleep={secs_val})", warned)
            _pause(sleep_for)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
