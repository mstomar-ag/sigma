#!/usr/bin/env python3
"""Measure each configured quality metric at the merge-base and at the head (#1635, PRD-0008 R-01).

    python3 quality_delta.py <goal>

For each entry in `quality.metrics`, read from the config AS COMMITTED AT THE MERGE-BASE (never the
head's file: a goal must not be able to loosen the bar it is measured against), run its command once
at the merge-base and once at the head, each in its own scratch worktree, one command at a time, and
print base, head, delta and a verdict: better, same, worse or unmeasured. The full contract (schema,
number grammar, environment, limits, the reclaim lever) is in docs/quality-delta.md.

Two rules the output keeps. `unmeasured` is never `same`: a command that failed, timed out, printed
no number, or ran on a head with uncommitted work says so and never reads as unchanged. And the
header line `base=<sha> head=<sha>` comes first, so a later waiver can be bound to the exact head.

Host-agnostic: git and the standard library only, no hooks. Reuses `bounded_run.run_group` (argv, no
shell, own process group, whole-group kill, bounded capture) and `diff_revert`'s scratch helpers,
both loaded by path. It never imports `work`, never calls gh, never touches the network.
"""
import collections
import decimal
import importlib.util
import json
import math
import os
import pathlib
import re
import subprocess
import sys

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bounded_run = _load("bounded_run")
diff_revert = _load("diff_revert")
north_star = _load("north_star")
state = _load("state")

USAGE = "usage: quality_delta.py <goal>   (also -h / --help)"
BETTER, SAME, WORSE, UNMEASURED = "better", "same", "worse", "unmeasured"

DEFAULT_TIMEOUT_SECONDS = 300.0
MAX_TIMEOUT_SECONDS = 3600.0
#: 15 percent of total memory. A judgement, not a measurement: 25 percent was marginal (a 24 GB Mac
#: reads about 30 percent free+inactive+speculative at rest), so the floor is the operator's lever.
DEFAULT_MEMORY_FLOOR_FRACTION = 0.15
MAX_MEMORY_FLOOR_FRACTION = 0.9
_PROBE_TIMEOUT = 10


# ------------------------------------------------------------------------------------ number grammar

#: The last non-empty stdout line must be exactly one decimal number. ASCII only (so no other
#: script's digits), no underscores, no nan/inf, an exponent of at most three digits.
_NUMBER = re.compile(r"[+-]?\d+(\.\d+)?([eE][+-]?\d{1,3})?", re.ASCII)
_PRECISE = decimal.Context(prec=1000)


def parse_number(text):
    """-> Decimal, or None when the last non-empty line is not one finite number."""
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    if not lines or not _NUMBER.fullmatch(lines[-1]):
        return None
    number = decimal.Decimal(lines[-1])
    # Decimal accepts 1e999; a metric that large is a bug in the command, and float() says so.
    return number if number.is_finite() and math.isfinite(float(number)) else None


def verdict(direction, base, head):
    """better / same / worse from Decimal base and head. `same` is exact equality (tolerance is R-02)."""
    if head == base:
        return SAME
    improved = head < base if direction == "lower" else head > base
    return BETTER if improved else WORSE


def format_number(number):
    """Plain decimal, never scientific; negative zero prints as 0."""
    return "0" if number == 0 else format(number, "f")


def format_delta(base, head):
    return format_number(_PRECISE.subtract(head, base))


# ---------------------------------------------------------------------------------- credential filter

#: Suffix rules cover the exact names too: GH_TOKEN and GITHUB_TOKEN end in _TOKEN, and X_API_KEY
#: ends in _KEY, so neither needs a line of its own (a redundant rule could never be seen red).
#: Names with no credential-shaped suffix that still carry one: database and cluster logins.
_CREDENTIAL_EXACT = frozenset(("SSH_AUTH_SOCK", "PGPASSWORD", "MYSQL_PWD", "DATABASE_URL", "KUBECONFIG"))
_CREDENTIAL_SUFFIXES = ("_TOKEN", "_SECRET", "_PASSWORD", "_PASSWD", "_PASS", "_PAT", "_KEY", "_CREDENTIALS")
_CREDENTIAL_SUBSTRINGS = ("AUTHTOKEN",)
_CREDENTIAL_PREFIXES = ("AWS_", "ANTHROPIC_", "OPENAI_")


def _is_credential(name, extra):
    upper = name.upper()
    return (upper in _CREDENTIAL_EXACT or upper in extra
            or upper.endswith(_CREDENTIAL_SUFFIXES)
            or any(part in upper for part in _CREDENTIAL_SUBSTRINGS)
            or upper.startswith(_CREDENTIAL_PREFIXES))


def drop_credentials(env, extra=()):
    """A new environment without credential-shaped names, case-insensitively. A DENYLIST: a credential
    with an unusual name survives, so `quality.env_drop` is the operator's lever (docs say so). HOME
    stays, so ~/.config/gh, ~/.netrc and ~/.ssh remain readable by a metric command; also documented."""
    names = {str(name).upper() for name in extra}
    return {key: value for key, value in env.items() if not _is_credential(key, names)}


# ------------------------------------------------------------------------------------- memory probe

def parse_meminfo(text):
    """(available_bytes, total_bytes) from /proc/meminfo text, else None."""
    found = {}
    for line in str(text).splitlines():
        match = re.match(r"(MemTotal|MemAvailable):\s+(\d+)\s*kB\s*$", line)
        if match:
            found[match.group(1)] = int(match.group(2)) * 1024
    if "MemTotal" in found and "MemAvailable" in found:
        return found["MemAvailable"], found["MemTotal"]
    return None


def parse_vm_stat(text, total):
    """(available_bytes, total) from `vm_stat` text: free + inactive + speculative pages. The header
    must name the page size or the numbers mean nothing, so a missing one is None."""
    lines = str(text).splitlines()
    header = re.match(r"Mach Virtual Memory Statistics: \(page size of (\d+) bytes\)", lines[0]) if lines else None
    if not header:
        return None
    pages = {}
    for line in lines[1:]:
        match = re.match(r"Pages (free|inactive|speculative):\s+(\d+)\.\s*$", line)
        if match:
            pages[match.group(1)] = int(match.group(2))
    if len(pages) != 3:
        return None
    return sum(pages.values()) * int(header.group(1)), total


def probe_memory():
    """(available_bytes, total_bytes) of the HOST, or None when it cannot be read. Host figures, NOT a
    container's cgroup limit (documented). Linux: /proc/meminfo MemAvailable. macOS: vm_stat, run with a
    timeout so a wedged tool cannot hang the run."""
    try:
        if sys.platform.startswith("linux"):
            return parse_meminfo(pathlib.Path("/proc/meminfo").read_text(encoding="ascii", errors="replace"))
        if sys.platform == "darwin":
            total = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
            done = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=_PROBE_TIMEOUT,
                                  stdin=subprocess.DEVNULL, check=False)
            return parse_vm_stat(done.stdout, total) if done.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return None


def floor_bytes(total, fraction):
    return total * (DEFAULT_MEMORY_FLOOR_FRACTION if fraction is None else fraction)


# ------------------------------------------------------------------------------------------- config

class ConfigRefused(ValueError):
    """A malformed `quality` block. str() is `<path>: <detail>` (the path names the entry index)."""


class ConfigUnreadable(ValueError):
    """The config bytes are not a JSON object we can read."""


Metric = collections.namedtuple("Metric", "name command direction scope required timeout")
Config = collections.namedtuple("Config", "metrics floor_fraction env_drop")

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_ENTRY_KEYS = frozenset(("name", "command", "direction", "scope", "required", "timeout_seconds"))
_BLOCK_KEYS = frozenset(("metrics", "memory_floor_fraction", "env_drop"))


def decode_config(raw):
    """bytes -> the config text, after checking it is a JSON object. A BOM is accepted; anything else
    unreadable raises ConfigUnreadable."""
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ConfigUnreadable("config is not readable JSON: %s" % type(exc).__name__) from None
    if not isinstance(data, dict):
        raise ConfigUnreadable("config is not a JSON object")
    return raw.decode("utf-8-sig")


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _entry(index, raw, seen):
    where = "metrics[%d]" % index
    if not isinstance(raw, dict):
        raise ConfigRefused("%s: not an object" % where)
    extra = sorted(set(raw) - _ENTRY_KEYS)
    if extra:
        raise ConfigRefused("%s: unknown key %s" % (where, extra[0]))
    for key in ("name", "command", "direction", "scope"):
        if key not in raw:
            raise ConfigRefused("%s: missing %s" % (where, key))
    name, command = raw["name"], raw["command"]
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ConfigRefused("%s: name must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}" % where)
    if name in seen:
        raise ConfigRefused("%s: duplicate name" % where)
    if not (isinstance(command, list) and command and all(isinstance(a, str) for a in command) and command[0]):
        raise ConfigRefused("%s: command must be a non-empty list of strings (an argv, no shell)" % where)
    if raw["direction"] not in ("lower", "higher"):
        raise ConfigRefused("%s: direction must be lower or higher" % where)
    if raw["scope"] not in ("repo", "changed-files"):
        raise ConfigRefused("%s: scope must be repo or changed-files" % where)
    if "required" in raw and not isinstance(raw["required"], bool):
        raise ConfigRefused("%s: required must be true or false" % where)
    timeout = raw.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if not _number(timeout) or not 0 < timeout <= MAX_TIMEOUT_SECONDS:
        raise ConfigRefused("%s: timeout_seconds must be a number above 0 and at most %d"
                            % (where, MAX_TIMEOUT_SECONDS))
    seen.add(name)
    return Metric(name, tuple(command), raw["direction"], raw["scope"], bool(raw.get("required", False)),
                  float(timeout))


def parse_config(text):
    """JSON text -> Config. No `quality` block (or none with metrics) is an empty Config. A malformed
    block raises ConfigRefused, never a guess."""
    data = json.loads(text)
    block = data.get("quality", {}) if isinstance(data, dict) else {}
    if not isinstance(block, dict):
        raise ConfigRefused("quality: not an object")
    unknown = sorted(set(block) - _BLOCK_KEYS - {k for k in block if k.startswith("_")})
    if unknown:
        raise ConfigRefused("quality: unknown key %s" % unknown[0])
    raw_metrics = block.get("metrics", [])
    if not isinstance(raw_metrics, list):
        raise ConfigRefused("quality.metrics: not a list")
    seen = set()
    metrics = tuple(_entry(i, raw, seen) for i, raw in enumerate(raw_metrics))
    fraction = block.get("memory_floor_fraction")
    if "memory_floor_fraction" in block and (
            not _number(fraction) or not 0 < fraction <= MAX_MEMORY_FLOOR_FRACTION):
        raise ConfigRefused("quality.memory_floor_fraction: must be above 0 and at most %g"
                            % MAX_MEMORY_FLOOR_FRACTION)
    drop = block.get("env_drop", [])
    if not (isinstance(drop, list) and all(isinstance(n, str) and n and "=" not in n and "\0" not in n
                                           for n in drop)):
        raise ConfigRefused("quality.env_drop: must be a list of variable names")
    return Config(metrics, None if "memory_floor_fraction" not in block else float(fraction), tuple(drop))


# ------------------------------------------------------------------------------------------------ git

def _git(root, argv, timeout=30):
    """-> (code, stdout, stderr), stdout and stderr kept apart. Runs git with the unattended environment
    (no repository-location variables, no prompts), LC_ALL=C and stdin closed. code is None when git could
    not run or timed out. Own helper, NOT diff_revert._git: that one inherits GIT_DIR, skips the
    unattended environment and merges stderr into stdout, which would corrupt a blob or a name list."""
    env = bounded_run.unattended_env()
    env["LC_ALL"] = "C"
    try:
        done = subprocess.run(["git", *argv], cwd=str(root), env=env, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return None, "", "git %s timed out after %ds" % (argv[0], timeout)
    except (OSError, ValueError) as exc:
        return None, "", "%s: %s" % (type(exc).__name__, exc)
    return (done.returncode, done.stdout.decode("utf-8", "surrogateescape"),
            done.stderr.decode("utf-8", "surrogateescape"))


def _git2_for(git):
    """Adapt a 3-tuple git helper to the (code, out) shape diff_revert.cleanup and _reclaim_stale unpack.
    They MUST always be given this adapter: handed the 3-tuple helper, `code, _out = git_cmd(...)` raises
    inside cleanup's blanket except and the scratch is silently left behind (a test pins that)."""
    def git2(root, argv, timeout=30):
        code, out, err = git(root, argv, timeout)
        return code, (out + err).strip()
    return git2


_git2 = _git2_for(_git)


# ----------------------------------------------------------------------------------------- scratch

def _stem(goal):
    """The goal's identity as a path component: the file stem for a `.md` goal, else the goal itself.
    This is the rule work.stem applies, repeated here (not imported: work arms claims). It is NOT the
    plan's draft rule of rewriting separators: a goal with a separator or `..` is REFUSED by
    state.unsafe_goal_reason instead of silently renamed, so `1635` and `1635.md` stay one stem and no
    two different goals can collapse onto one scratch path."""
    path = pathlib.Path(str(goal))
    return path.stem if path.suffix == ".md" else str(goal)


def _scratch_root(sdlc):
    return (pathlib.Path(sdlc) / "state" / "quality_delta").resolve()


def scratch_paths(sdlc, stem):
    """(base, head) scratch worktree paths: per goal, so parallel goals never share one, and not
    diff_revert's own state/diff_revert/<goal>, so a live diff-revert never makes this abstain."""
    root = _scratch_root(sdlc)
    return root / (stem + "-base"), root / (stem + "-head")


def remove_scratch(git2, root, sdlc, scratch):
    """Tear down one scratch worktree through diff_revert.cleanup, but only a DIRECT child of
    <sdlc>/state/quality_delta/ (resolved, so a symlink out is refused). -> True when removed."""
    path = pathlib.Path(scratch).resolve()
    if path.parent != _scratch_root(sdlc):
        return False
    diff_revert.cleanup(root, path, git2)
    return True


# ------------------------------------------------------------------------------------------ the run

Report = collections.namedtuple("Report", "code out err")

_FILES_NAME = ".sigma-quality-files"
#: `git worktree add` checks out a whole tree, so it gets its own bounded ceiling (seconds), not the 30 s of a
#: read. A tree that needs longer than this is `unmeasured` with git's timeout in the reason; documented.
_WORKTREE_ADD_TIMEOUT = 600
_MAX_OUT_BYTES = 1 << 20
_TERM_GRACE = 2
_LS_TREE = re.compile(r"(\d{6}) \w+ ([0-9a-f]{40,64})\t")


def _run_group(argv, cwd, timeout, env):
    """The one runner: an argv with no shell, in its own process group, the whole group killed on timeout,
    stdout bounded at 1 MiB (the last line is lost if it is cut, so a cut is `unmeasured`)."""
    return bounded_run.run_group(argv, cwd, timeout, env=env, merge=False, max_out_bytes=_MAX_OUT_BYTES,
                                 term_grace=_TERM_GRACE)


def _one_line(text, limit=200):
    return " ".join(str(text).split())[:limit]


def _everything_unmeasured(base, head, reason):
    reason = _one_line(reason)
    return Report(0, ["base=%s head=%s" % (base or "-", head or "-"), "unmeasured: %s" % reason],
                  ["quality_delta.py: unmeasured: %s" % reason])


def _all_metrics(base, head, cfg, reason):
    reason = _one_line(reason)
    return Report(0, ["base=%s head=%s" % (base, head)] + ["%s: unmeasured (%s)" % (m.name, reason)
                                                           for m in cfg.metrics], [])


def read_record(sdlc, stem):
    """-> (ref, reason): `<remote>/<base>` from the goal's work record, read directly (never via work.py,
    which arms claims), or (None, why) when there is no usable one."""
    path = pathlib.Path(sdlc) / "state" / "work" / (stem + ".json")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "no readable work record for this goal", None
    if not isinstance(record, dict):
        return None, "the work record is not an object", None
    base, remote = record.get("base"), record.get("remote")
    if record.get("base_resolved") is not True:
        return None, "the work record's base is not resolved", None
    for part in (base, remote):
        if not isinstance(part, str) or not part:
            return None, "the work record has no usable base or remote", None
    return "%s/%s" % (remote, base), "", record


def _wrong_worktree(git, wt, record):
    """A reason when `cwd` is not the goal's own worktree on the goal's branch, else "". Without this,
    `quality_delta.py 1635` run from goal 2000's worktree would measure 2000's head as 1635's."""
    recorded = record.get("worktree")
    if isinstance(recorded, str) and recorded and os.path.realpath(recorded) != os.path.realpath(wt):
        return "this is not the goal's worktree (the work record names a different one)"
    branch = record.get("branch")
    code, out, _err = git(wt, ["rev-parse", "--abbrev-ref", "HEAD"])
    if isinstance(branch, str) and branch and (code != 0 or out.strip() != branch):
        return "the worktree is not on the goal's branch %s" % branch
    return ""


def _memory_ok(reading, fraction):
    if reading is None:
        return False, "memory probe unreadable"
    available, total = reading
    floor = floor_bytes(total, fraction)
    if available < floor:
        return False, "available memory %d MiB is below the floor of %d MiB" % (available >> 20, floor // (1 << 20))
    return True, ""


def _read_config_at(git, wt, fork):
    """-> (Config|None, Report|None). Exactly one is set. The config is the file AS COMMITTED at the
    merge-base."""
    code, out, err = git(wt, ["ls-tree", fork, "--", ".sdlc/config.json"])
    if code != 0:
        return None, "could not list .sdlc/config.json at the merge-base: %s" % _one_line(err or "git failed")
    if not out.strip():
        return None, ""
    entry = _LS_TREE.match(out)
    if not entry or entry.group(1) not in ("100644", "100755"):    # the mode fixes the type: both are blobs
        return None, ".sdlc/config.json at the merge-base is not a regular file"
    code, blob, err = git(wt, ["cat-file", "blob", entry.group(2)])
    if code != 0:
        return None, "could not read .sdlc/config.json at the merge-base: %s" % _one_line(err or "git failed")
    try:
        return parse_config(decode_config(blob.encode("utf-8", "surrogateescape"))), None
    except ConfigUnreadable as exc:
        return None, str(exc)


def _changed_files(git, wt, fork):
    """-> NUL-joined bytes of the paths changed between fork and HEAD that exist at BOTH (so base and head
    measure the same files), or None if git could not list them. b"" when there are none."""
    code, out, _err = git(wt, ["diff", "--name-only", "-z", "--no-renames", "--diff-filter=MT", fork, "HEAD"])
    if code != 0:
        return None
    names = [name for name in out.split("\0") if name]
    return "".join(name + "\0" for name in names).encode("utf-8", "surrogateescape")


def _side(runner, label, metric, scratch, env, files):
    """-> (Decimal|None, reason) for one command at one commit."""
    if files is not None:
        env = dict(env, SIGMA_QUALITY_FILES=str(scratch / _FILES_NAME))
    result = runner(list(metric.command), scratch, metric.timeout, env)
    if result.outcome != bounded_run.OK:
        why = result.detail or ("exited %s" % result.code if result.outcome == bounded_run.FAILED
                                else result.outcome)
        return None, "%s command %s" % (label, why)
    if result.truncated:
        return None, "%s output passed the %d byte cap" % (label, _MAX_OUT_BYTES)
    number = parse_number(result.out)
    if number is None:
        return None, "%s printed no number on its last line" % label
    return number, ""


def measure(goal, cwd=".", *, probe=None, git=None, runner=None, pid_alive=None):
    """-> Report(code, stdout lines, stderr lines). Exit 0 for every measured or unmeasured outcome, 2 for a
    refusal (empty stdout). `probe`, `git`, `runner` and `pid_alive` are injectable for the tests."""
    probe = probe_memory if probe is None else probe
    git = _git if git is None else git
    runner = _run_group if runner is None else runner
    pid_alive = diff_revert._pid_alive if pid_alive is None else pid_alive
    refusal = bounded_run.group_refusal()
    if refusal:
        return Report(2, [], ["quality_delta.py: REFUSED [not-supported-here]: not supported here (%s)" % refusal])
    stem = _stem(goal)
    unsafe = state.unsafe_goal_reason(stem) or (None if stem else "must not be empty")
    if unsafe:
        return Report(2, [], ["quality_delta.py: REFUSED [unsafe-goal]: %s" % unsafe])
    code, top, _err = git(cwd, ["rev-parse", "--show-toplevel"])
    wt = top.strip() if code == 0 and top.strip() else None
    code, sha, _err = git(wt, ["rev-parse", "HEAD"]) if wt else (None, "", "")
    head = sha.strip() if code == 0 and sha.strip() else None
    root, why = north_star.project_root(cwd)
    if root is None or wt is None:
        return _everything_unmeasured(None, head, why or "not inside a git working tree")
    sdlc = root / ".sdlc"
    ref, why, record = read_record(sdlc, stem)
    if ref is None:
        return _everything_unmeasured(None, head, why)
    why = _wrong_worktree(git, wt, record)
    if why:
        return _everything_unmeasured(None, head, why)
    code, out, err = git(wt, ["merge-base", "HEAD", ref])
    fork = out.strip() if code == 0 and out.strip() else None
    if fork is None:
        return _everything_unmeasured(None, head, "no merge-base with %s: %s" % (ref, _one_line(err or "git failed")))
    try:
        cfg, problem = _read_config_at(git, wt, fork)
    except ConfigRefused as exc:
        return Report(2, [], ["quality_delta.py: REFUSED [bad-metric] %s" % exc])
    if cfg is None and problem == "":
        return Report(0, ["no metrics configured"], [])
    if cfg is None:
        return _everything_unmeasured(fork, head, problem)
    if not cfg.metrics:
        return Report(0, ["no metrics configured"], [])
    if head is None:
        return _everything_unmeasured(fork, None, "the goal worktree has no commit")
    code, dirty, err = git(wt, ["status", "--porcelain", "--untracked-files=normal", "--", ".", ":(exclude).sdlc"])
    if code != 0:
        return _all_metrics(fork, head, cfg, "could not read the goal worktree status: %s" % _one_line(err))
    if dirty.strip():
        return _all_metrics(fork, head, cfg, "uncommitted work in the goal worktree is not measured: commit first")
    ok, why = _memory_ok(probe(), cfg.floor_fraction)
    if not ok:
        return _all_metrics(fork, head, cfg, why)
    wants_files = any(m.scope == "changed-files" for m in cfg.metrics)
    files = _changed_files(git, wt, fork) if wants_files else None
    git2 = _git2_for(git)
    scratch_paths_pair = scratch_paths(sdlc, stem)
    _scratch_root(sdlc).mkdir(parents=True, exist_ok=True)
    for scratch in scratch_paths_pair:
        if not diff_revert._reclaim_stale(wt, scratch, pid_alive, git2):
            return _all_metrics(fork, head, cfg, "the scratch worktree has a live owner (another run); abstaining")
    created = []
    try:
        for scratch, rev in zip(scratch_paths_pair, (fork, head)):
            diff_revert._owner_path(scratch).write_text(str(os.getpid()), encoding="ascii")
            created.append(scratch)
            code, _out, err = git(wt, ["worktree", "add", "-q", "--detach", str(scratch), rev], _WORKTREE_ADD_TIMEOUT)
            if code != 0:
                return _all_metrics(fork, head, cfg, "could not create a scratch worktree: %s" % _one_line(err))
            if files is not None:
                (scratch / _FILES_NAME).write_bytes(files)
        return Report(0, ["base=%s head=%s" % (fork, head)] + _measure_all(
            cfg, probe, runner, scratch_paths_pair, files), [])
    finally:
        for scratch in created:
            remove_scratch(git2, wt, sdlc, scratch)


def _measure_all(cfg, probe, runner, scratches, files):
    env = drop_credentials(bounded_run.unattended_env(), cfg.env_drop)
    env.pop("SIGMA_QUALITY_FILES", None)
    rows, dipped = [], None
    for metric in cfg.metrics:
        if dipped:
            rows.append("%s: unmeasured (%s)" % (metric.name, dipped))
            continue
        if metric.scope == "changed-files" and not files:
            rows.append("%s: unmeasured (no changed files present at both commits)" % metric.name)
            continue
        scoped = files if metric.scope == "changed-files" else None
        values = []
        for label, scratch in zip(("base", "head"), scratches):
            ok, why = _memory_ok(probe(), cfg.floor_fraction)
            if not ok:
                dipped, number, reason = why, None, why
            else:
                number, reason = _side(runner, label, metric, scratch, env, scoped)
            if number is None:
                rows.append("%s: unmeasured (%s)" % (metric.name, _one_line(reason)))
                break
            values.append(number)
        else:
            base, head = values
            rows.append("%s: base=%s head=%s delta=%s %s" % (
                metric.name, format_number(base), format_number(head), format_delta(base, head),
                verdict(metric.direction, base, head)))
    return rows


def main(argv):
    args = argv[1:]
    if args in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    if len(args) != 1 or not args[0] or args[0].startswith("-"):
        print(USAGE, file=sys.stderr)
        return 2
    report = measure(args[0], ".")
    for line in report.err:
        print(line, file=sys.stderr)
    for line in report.out:
        print(line)
    return report.code


if __name__ == "__main__":
    # The script-time floor, as north_star.py does: this run's wall time, recorded to the session at exit.
    _ts = _load("timing_store")
    sys.exit(_ts.timed_main(main, sys.argv, "quality_delta"))
