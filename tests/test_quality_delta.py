"""Tests for skills/sigma-loop/scripts/quality_delta.py (#1635, PRD-0008 R-01).

Task 1 group (pure functions): number grammar, verdicts, credential filter, memory probe, config
schema. Later groups drive a real git repository with a real bare origin, real linked worktrees and
real process-group control; nothing is mocked that the module's own claim depends on.

Credential-like names below are placeholders; no value here is a real credential, and every value is
the same obvious placeholder string.
"""
import importlib.util
import pathlib
import subprocess
import sys

import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _load():
    spec = importlib.util.spec_from_file_location("quality_delta", SCRIPTS / "quality_delta.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


qd = _load()
D = qd.decimal.Decimal


# ---------------------------------------------------------------------------------- number grammar

@pytest.mark.parametrize("text,want", [
    ("12\n", "12"), ("noise\n 3.5 \n\n", "3.5"), ("\n\n7\n\n", "7"), ("0.25", "0.25"), ("+4", "4"),
])
def test_parse_number_accepts_last_line_decimal(text, want):
    assert qd.parse_number(text) == D(want)


@pytest.mark.parametrize("text,want", [("-3", "-3"), ("-0.5", "-0.5"), ("1e3", "1000"), ("2.5E-2", "0.025"),
                                       ("-1e+2", "-100")])
def test_parse_number_accepts_negative_and_exponent(text, want):
    assert qd.parse_number(text) == D(want)


@pytest.mark.parametrize("text", [
    "nan", "NaN", "inf", "-inf", "Infinity", "", "\n\n", "12 34", "1_000", "٣٤", "1e999", "1e1000", "1e-1000", "1e+0001000",
    "1e", ".5", "5.", "0x10", "12abc", "1,5",
])
def test_parse_number_rejects_each_bad_form(text):
    assert qd.parse_number(text) is None


def test_parse_number_ignores_earlier_lines_only_last_counts():
    assert qd.parse_number("5\nnot a number\n") is None


# ---------------------------------------------------------------------------------------- verdicts

def test_verdict_lower_is_better():
    assert qd.verdict("lower", D(10), D(9)) == "better"
    assert qd.verdict("lower", D(10), D(11)) == "worse"


def test_verdict_higher_is_better():
    assert qd.verdict("higher", D(10), D(11)) == "better"
    assert qd.verdict("higher", D(10), D(9)) == "worse"


def test_verdict_same_is_exact_equality():
    assert qd.verdict("lower", D("10.0"), D("10")) == "same"
    assert qd.verdict("lower", D("10"), D("10.0000001")) == "worse"
    assert qd.verdict("higher", D("10"), D("10.0000001")) == "better"


def test_negative_zero_prints_as_zero():
    assert qd.format_number(qd.parse_number("-0")) == "0"
    assert qd.format_number(qd.parse_number("-0.0")) == "0"
    assert qd.format_delta(qd.parse_number("-0"), qd.parse_number("0")) == "0"
    assert qd.format_number(D("-1")) == "-1"


def test_delta_prints_without_scientific_notation():
    assert qd.format_number(D("1E+3")) == "1000"
    assert qd.format_number(D("2.5E-7")) == "0.00000025"
    assert qd.format_delta(D("1e3"), D("2e3")) == "1000"
    assert qd.format_delta(D("3"), D("1")) == "-2"
    assert qd.format_delta(D("0.1"), D("0.3")) == "0.2"


# ------------------------------------------------------------------------------------ credential env

DROPPED = [
    "GH_TOKEN", "GITHUB_TOKEN", "foo_token", "MY_SECRET", "DB_PASSWORD", "DB_PASSWD", "SIGMA_SMTP_PASS",
    "GH_PAT", "SECRET_KEY", "PRIVATE_KEY", "STRIPE_KEY", "X_API_KEY", "X_CREDENTIALS", "NPM_CONFIG_AUTHTOKEN",
    "npm_config_authtoken", "PGPASSWORD", "MYSQL_PWD", "DATABASE_URL", "KUBECONFIG", "pgpassword", "AWS_X", "ANTHROPIC_X", "OPENAI_X", "SSH_AUTH_SOCK", "SIGMA_OWN_NAME",
]


def _env_with(names):
    env = {name: "placeholder-value" for name in names}
    env.update({"PATH": "/usr/bin", "HOME": "/placeholder-home", "LANG": "C", "PYTHONPATH": "/p"})
    return env


@pytest.mark.parametrize("name", DROPPED)
def test_env_filter_drops_each_rule(name):
    kept = qd.drop_credentials(_env_with([name]), ["SIGMA_OWN_NAME"])
    assert name not in kept, "the credential rule for %s did not fire" % name


def test_env_filter_keeps_path_and_home():
    kept = qd.drop_credentials(_env_with(DROPPED), ["SIGMA_OWN_NAME"])
    assert kept == {"PATH": "/usr/bin", "HOME": "/placeholder-home", "LANG": "C", "PYTHONPATH": "/p"}


def test_env_filter_does_not_mutate_input():
    env = _env_with(DROPPED)
    before = dict(env)
    qd.drop_credentials(env, ["SIGMA_OWN_NAME"])
    assert env == before


@pytest.mark.parametrize("name", ["PATHEXT", "PASSENGER", "KEYBOARD", "TOKENIZERS_PARALLELISM", "LC_ALL", "PATTERN"])
def test_env_filter_keeps_lookalikes(name):
    assert name in qd.drop_credentials(_env_with([name]), [])


# ----------------------------------------------------------------------------------------- the probe

MEMINFO = "MemTotal:       16000000 kB\nMemFree:          100000 kB\nMemAvailable:    8000000 kB\nBuffers: 1 kB\n"
VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                7531.
Pages active:                            446364.
Pages inactive:                          440087.
Pages speculative:                         1000.
Pages wired down:                         93000.
"""


def test_probe_parses_linux_meminfo():
    assert qd.parse_meminfo(MEMINFO) == (8000000 * 1024, 16000000 * 1024)
    assert qd.parse_meminfo("MemTotal: 1 kB\n") is None
    assert qd.parse_meminfo("") is None


def test_probe_parses_macos_vm_stat_sample():
    total = 1 << 34
    assert qd.parse_vm_stat(VM_STAT, total) == ((7531 + 440087 + 1000) * 16384, total)


@pytest.mark.parametrize("text", [
    "", "Pages free: 1.\nPages inactive: 1.\nPages speculative: 1.\n",
    VM_STAT.replace("Pages speculative:                         1000.\n", ""),
    VM_STAT.replace("page size of 16384 bytes", "page size of many bytes"),
])
def test_probe_none_on_missing_header(text):
    assert qd.parse_vm_stat(text, 1 << 34) is None


def test_vm_stat_call_has_timeout(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        return subprocess.CompletedProcess(argv, 0, stdout=VM_STAT, stderr="")

    monkeypatch.setattr(qd.sys, "platform", "darwin")
    monkeypatch.setattr(qd.subprocess, "run", fake_run)
    got = qd.probe_memory()
    assert got is not None and got[0] == (7531 + 440087 + 1000) * 16384
    assert seen["argv"] == ["vm_stat"] and 0 < seen["kwargs"]["timeout"] <= 30
    assert seen["kwargs"]["stdin"] == subprocess.DEVNULL


def test_vm_stat_hang_is_none(monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(qd.sys, "platform", "darwin")
    monkeypatch.setattr(qd.subprocess, "run", fake_run)
    assert qd.probe_memory() is None


def test_floor_default_is_15_percent():
    assert qd.DEFAULT_MEMORY_FLOOR_FRACTION == 0.15
    assert qd.floor_bytes(1000, None) == 150
    assert qd.floor_bytes(1000, 0.5) == 500


@pytest.mark.parametrize("value", [0, -0.1, 0.91, 1, True, "0.2", None, float("nan")])
def test_floor_fraction_out_of_range_refused(value):
    cfg = {"quality": {"metrics": [], "memory_floor_fraction": value}}
    with pytest.raises(qd.ConfigRefused) as caught:
        qd.parse_config(qd.json.dumps(cfg, allow_nan=True))
    assert "memory_floor_fraction" in str(caught.value)


# --------------------------------------------------------------------------------------------- config

GOOD = {"name": "lines", "command": ["wc", "-l"], "direction": "lower", "scope": "repo"}


def _cfg(**override):
    entry = dict(GOOD)
    entry.update(override)
    return qd.json.dumps({"quality": {"metrics": [dict(GOOD, name="first"), entry]}})


def test_config_parses_a_good_block():
    text = qd.json.dumps({"quality": {"metrics": [dict(GOOD, required=True, timeout_seconds=9)],
                                      "memory_floor_fraction": 0.3, "env_drop": ["MY_NAME"]}})
    cfg = qd.parse_config(text)
    assert cfg.floor_fraction == 0.3 and cfg.env_drop == ("MY_NAME",)
    (metric,) = cfg.metrics
    assert (metric.name, metric.command, metric.direction, metric.scope, metric.timeout) == \
        ("lines", ("wc", "-l"), "lower", "repo", 9.0)


def test_config_defaults():
    cfg = qd.parse_config(qd.json.dumps({"quality": {"metrics": [GOOD]}}))
    assert cfg.floor_fraction is None and cfg.env_drop == () and cfg.metrics[0].timeout == 300.0


@pytest.mark.parametrize("field,value", [
    ("name", ""), ("name", 3), ("name", "has space"), ("name", "x\nforged: line"), ("name", "a" * 65),
    ("command", []), ("command", "wc -l"), ("command", ["wc", 1]), ("command", [""]),
    ("direction", "up"), ("direction", None), ("scope", "everything"), ("scope", None),
    ("required", "yes"), ("timeout_seconds", 0), ("timeout_seconds", 3601), ("timeout_seconds", True),
    ("timeout_seconds", "5"), ("unknown_key", 1),
])
def test_config_malformed_names_entry_index(field, value):
    with pytest.raises(qd.ConfigRefused) as caught:
        qd.parse_config(_cfg(**{field: value}))
    assert str(caught.value).startswith("metrics[1]"), str(caught.value)


@pytest.mark.parametrize("missing", ["name", "command", "direction", "scope"])
def test_config_missing_required_field_names_entry_index(missing):
    entry = {k: v for k, v in GOOD.items() if k != missing}
    with pytest.raises(qd.ConfigRefused) as caught:
        qd.parse_config(qd.json.dumps({"quality": {"metrics": [GOOD, entry]}}))
    assert str(caught.value).startswith("metrics[1]") and missing in str(caught.value)


@pytest.mark.parametrize("block", [[], "x", {"metrics": "x"}, {"metrics": [3]}, {"metrics": [], "env_drop": "A"},
                                   {"metrics": [], "env_drop": [""]}, {"metrics": [], "env_drop": [3]},
                                   {"metrics": [], "mystery": 1}])
def test_config_malformed_block_refused(block):
    with pytest.raises(qd.ConfigRefused):
        qd.parse_config(qd.json.dumps({"quality": block}))


def test_config_duplicate_names_refused():
    with pytest.raises(qd.ConfigRefused) as caught:
        qd.parse_config(qd.json.dumps({"quality": {"metrics": [GOOD, GOOD]}}))
    assert str(caught.value).startswith("metrics[1]")


def test_config_decodes_bom():
    raw = b"\xef\xbb\xbf" + qd.json.dumps({"quality": {"metrics": [GOOD]}}).encode()
    assert qd.parse_config(qd.decode_config(raw)).metrics[0].name == "lines"


@pytest.mark.parametrize("raw", [b"{not json", b"[]", b"\xff\xfe\x00", b"3"])
def test_config_undecodable_is_a_decode_error_not_a_crash(raw):
    with pytest.raises(qd.ConfigUnreadable):
        qd.parse_config(qd.decode_config(raw))


@pytest.mark.parametrize("cfg", [{}, {"quality": {}}, {"quality": {"_comment": "x"}}, {"quality": {"metrics": []}},
                                 {"other": 1}])
def test_config_without_metrics_is_empty(cfg):
    assert qd.parse_config(qd.json.dumps(cfg)).metrics == ()


# =============================================================================================
# Tasks 2 and 3: a real git repository with a real bare origin and real linked worktrees.
# =============================================================================================
import json
import os
import signal
import threading
import time

SCRIPT = SCRIPTS / "quality_delta.py"
PLENTY = lambda: (10 ** 12, 10 ** 12)                      # a probe that never gates
WORDS = [sys.executable, "-c", "import sys;print(len(open('data.txt').read().split()))"]


@pytest.fixture(autouse=True)
def _hermetic_git(monkeypatch, tmp_path_factory):
    """No global or system git config (hooks, signing), a fixed identity; inherited by every subprocess."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv("GIT_%s_NAME" % who, "t")
        monkeypatch.setenv("GIT_%s_EMAIL" % who, "t@example.invalid")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)


def git(cwd, *argv):
    done = subprocess.run(["git", *argv], cwd=str(cwd), capture_output=True, text=True, check=True,
                          stdin=subprocess.DEVNULL)
    return done.stdout.strip()


def metric(name="words", command=WORDS, **extra):
    entry = {"name": name, "command": list(command), "direction": "lower", "scope": "repo"}
    entry.update(extra)
    return entry


class Repo:
    pass


def build(tmp_path, metrics=None, *, quality_extra=None, base_config=None, head_config=None, base_files=None,
          head_files=None, config_kind="file", record=True, goal="1635", head_commit=True):
    """origin (bare) <- main (initial commit on main, pushed) <- goal worktree on sdlc/<goal> with one commit.
    The merge-base config is the one committed in main; the head's may differ (`head_config`)."""
    metrics = [metric()] if metrics is None else metrics
    config = base_config if base_config is not None else {"quality": dict({"metrics": metrics,
                                                                           "memory_floor_fraction": 0.01},
                                                                          **(quality_extra or {}))}
    tmp_path.mkdir(parents=True, exist_ok=True)
    r = Repo()
    r.tmp, r.origin, r.main, r.wt = tmp_path, tmp_path / "origin.git", tmp_path / "main", tmp_path / "wt"
    r.sdlc = r.main / ".sdlc"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(r.origin))
    git(tmp_path, "init", "-q", "-b", "main", str(r.main))
    (r.sdlc).mkdir()
    for rel, text in (base_files or {"data.txt": "a b c\n"}).items():
        (r.main / rel).parent.mkdir(parents=True, exist_ok=True)
        (r.main / rel).write_text(text)
    cfg = r.sdlc / "config.json"
    if config_kind in ("file", "executable"):
        cfg.write_text(json.dumps(config))
    elif config_kind == "symlink":
        (r.main / "other.json").write_text(json.dumps(config))
        os.symlink(json.dumps(config), cfg)       # the link's TARGET text is itself a valid config
    elif config_kind == "directory":
        cfg.mkdir()
        (cfg / "inner.json").write_text(json.dumps(config))
    if config_kind == "executable":
        cfg.write_text(json.dumps(config))
        cfg.chmod(0o755)
    git(r.main, "add", "-A", "-f")
    if config_kind == "gitlink":
        git(r.main, "update-index", "--add", "--cacheinfo", "160000,%s,.sdlc/config.json" % ("1" * 40))
    git(r.main, "commit", "-q", "-m", "base")
    git(r.main, "remote", "add", "origin", str(r.origin))
    git(r.main, "push", "-q", "origin", "main")
    r.fork = git(r.main, "rev-parse", "HEAD")
    git(r.main, "worktree", "add", "-q", "-b", "sdlc/%s" % goal, str(r.wt))
    if head_commit:
        for rel, text in (head_files or {"data.txt": "a b c d e\n"}).items():
            (r.wt / rel).parent.mkdir(parents=True, exist_ok=True)
            (r.wt / rel).write_text(text)
        if head_config is not None:
            (r.wt / ".sdlc" / "config.json").write_text(json.dumps(head_config))
        git(r.wt, "add", "-A")
        git(r.wt, "commit", "-q", "-m", "head")
    r.head = git(r.wt, "rev-parse", "HEAD")
    r.goal = goal
    if record:
        write_record(r)
    return r


def write_record(r, goal=None, **fields):
    rec = {"worktree": str(r.wt), "branch": "sdlc/%s" % (goal or r.goal), "base": "main", "base_resolved": True,
           "remote": "origin", "pr": ""}
    rec.update(fields)
    path = r.sdlc / "state" / "work" / ("%s.json" % (goal or r.goal))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec))
    return path


def measure(r, goal=None, **kw):
    kw.setdefault("probe", PLENTY)
    return qd.measure(goal or r.goal, r.wt, **kw)


def lines(report):
    return report.out


def scratch_root(r):
    return r.sdlc / "state" / "quality_delta"


def ok_runner(value="3\n", calls=None):
    def run(argv, cwd, timeout, env):
        if calls is not None:
            calls.append({"argv": list(argv), "cwd": pathlib.Path(cwd), "timeout": timeout, "env": dict(env)})
        return qd.bounded_run.Result(qd.bounded_run.OK, 0, value)
    return run


def metric_lines(report):
    return [line for line in report.out if not line.startswith("base=")]


def no_scratch(r):
    root = scratch_root(r)
    return (not root.exists() or not list(root.iterdir())) and "state/quality_delta" not in git(r.main, "worktree", "list")


# ------------------------------------------------------------------------------------------ Task 2

def test_config_read_from_merge_base_not_head(tmp_path):
    """The head's config lists a different metric; only the merge-base's is run."""
    r = build(tmp_path, head_config={"quality": {"metrics": [metric(name="from-head")]}})
    calls = []
    got = measure(r, runner=ok_runner(calls=calls))
    assert [line.split(":")[0] for line in metric_lines(got)] == ["words"], got
    assert len(calls) == 2


def test_config_absent_at_fork_is_no_metrics(tmp_path):
    """ls-tree exits 0 with empty output: the merge-base simply has no .sdlc/config.json."""
    r = build(tmp_path)
    git(r.main, "rm", "-q", "-f", ".sdlc/config.json")
    git(r.main, "commit", "-q", "-m", "drop config")
    git(r.main, "push", "-q", "origin", "main")
    git(r.wt, "rebase", "-q", "main")
    calls = []
    got = measure(r, runner=ok_runner(calls=calls))
    assert got.code == 0 and got.out == ["no metrics configured"] and calls == []


def test_bad_revision_is_unreachable_unmeasured(tmp_path):
    r = build(tmp_path)
    write_record(r, base="no-such-branch")
    calls = []
    got = measure(r, runner=ok_runner(calls=calls))
    assert got.code == 0 and calls == []
    assert got.out[0].startswith("base=- head=%s" % r.head)
    assert any(line.startswith("unmeasured") for line in got.out[1:]) and got.err


def test_git_helper_ignores_inherited_git_dir_and_separates_stderr(tmp_path, monkeypatch):
    r = build(tmp_path)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "does-not-exist"))
    code, out, err = qd._git(r.wt, ["rev-parse", "HEAD"])
    assert (code, out.strip(), err) == (0, r.head, "")
    code, out, err = qd._git(r.wt, ["rev-parse", "--verify", "no-such-ref"])
    assert code != 0 and out == "" and "fatal" in err


def test_git_helper_returns_none_code_on_timeout_or_missing_cwd(tmp_path):
    assert qd._git(tmp_path / "missing", ["status"])[0] is None


def test_record_read_without_work_import():
    source = SCRIPT.read_text(encoding="utf-8")
    assert '_load("work")' not in source and "import work" not in source


def test_record_unsafe_goal_refused(tmp_path):
    r = build(tmp_path)
    for goal in ("a:b", "a\\b", "x..y"):
        got = measure(r, goal=goal)
        assert got.code == 2 and got.out == [] and "REFUSED [unsafe-goal]" in got.err[0], (goal, got)


@pytest.mark.parametrize("fields", [{"base": ""}, {"remote": ""}, {"base_resolved": False}, {"base": None}])
def test_record_empty_base_or_unresolved_is_unmeasured(tmp_path, fields):
    r = build(tmp_path)
    write_record(r, **fields)
    calls = []
    got = measure(r, runner=ok_runner(calls=calls))
    assert got.code == 0 and calls == []
    assert got.out[0] == "base=- head=%s" % r.head and got.out[1].startswith("unmeasured")


@pytest.mark.parametrize("content", [None, "{not json", "[]"])
def test_record_absent_or_unreadable_is_unmeasured(tmp_path, content):
    r = build(tmp_path)
    path = r.sdlc / "state" / "work" / "1635.json"
    path.unlink() if content is None else path.write_text(content)
    calls = []
    got = measure(r, runner=ok_runner(calls=calls))
    assert got.code == 0 and calls == [] and got.out[0] == "base=- head=%s" % r.head
    assert got.out[1].startswith("unmeasured")


def test_scratch_base_and_head_are_separate_detached_worktrees(tmp_path):
    r = build(tmp_path)
    seen = []

    def runner(argv, cwd, timeout, env):
        cwd = pathlib.Path(cwd)
        seen.append((cwd, git(cwd, "rev-parse", "HEAD"),
                     subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=str(cwd), capture_output=True).returncode,
                     (cwd / "data.txt").read_text()))
        return qd.bounded_run.Result(qd.bounded_run.OK, 0, "1\n")

    measure(r, runner=runner)
    (base_cwd, base_sha, base_sym, base_text), (head_cwd, head_sha, head_sym, head_text) = seen
    assert base_cwd != head_cwd and base_cwd.parent == head_cwd.parent == scratch_root(r).resolve()
    assert (base_sha, head_sha) == (r.fork, r.head) and base_sym == head_sym == 1, "detached"
    assert (base_text, head_text) == ("a b c\n", "a b c d e\n")


def test_scratch_removed_on_success(tmp_path):
    r = build(tmp_path)
    measure(r, runner=ok_runner())
    assert no_scratch(r)


def test_scratch_removed_when_runner_raises(tmp_path):
    r = build(tmp_path)

    def runner(argv, cwd, timeout, env):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        measure(r, runner=runner)
    assert no_scratch(r)


def test_scratch_removed_when_second_worktree_add_fails(tmp_path):
    """A failure after the first worktree exists must not strand it."""
    r = build(tmp_path)
    real = qd._git
    state = {"adds": 0}

    def flaky(root, argv, timeout=30):
        if argv[:2] == ["worktree", "add"]:
            state["adds"] += 1
            if state["adds"] == 2:
                raise OSError("disk full")
        return real(root, argv, timeout)

    with pytest.raises(OSError):
        measure(r, git=flaky, runner=ok_runner())
    assert no_scratch(r)


def test_cleanup_refuses_path_outside_state_quality_delta(tmp_path):
    r = build(tmp_path)
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    assert qd.remove_scratch(qd._git2, r.wt, r.sdlc, outside) is False
    assert (outside / "keep.txt").exists()
    inside = scratch_root(r) / "1635-base"
    inside.mkdir(parents=True)
    assert qd.remove_scratch(qd._git2, r.wt, r.sdlc, inside) is True and not inside.exists()


def test_owner_marker_exists_before_worktree_add(tmp_path):
    r = build(tmp_path)
    real = qd._git
    order = []

    def spy(root, argv, timeout=30):
        if argv[:2] == ["worktree", "add"]:
            scratch = pathlib.Path(argv[-2])
            owner = qd.diff_revert._owner_path(scratch)
            order.append((owner.exists(), owner.read_text().strip() if owner.exists() else None))
        return real(root, argv, timeout)

    measure(r, git=spy, runner=ok_runner())
    assert order == [(True, str(os.getpid()))] * 2, order


def _dead_pid():
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    return done.pid


def test_ac4_reclaims_dead_owner_scratch(tmp_path):
    """AC-4: a previous run died holding both scratch dirs; this run reclaims them and measures."""
    r = build(tmp_path)
    for side in ("base", "head"):
        stale = scratch_root(r) / ("1635-" + side)
        stale.mkdir(parents=True)
        (stale / "STALE").write_text("left by a dead run")
        (scratch_root(r) / ("1635-%s.owner" % side)).write_text(str(_dead_pid()))
    got = measure(r, runner=ok_runner("4\n"))
    assert metric_lines(got) == ["words: base=4 head=4 delta=0 same"], got
    assert no_scratch(r)


def test_ac4_live_owner_abstains_unmeasured(tmp_path):
    r = build(tmp_path)
    stale = scratch_root(r) / "1635-base"
    stale.mkdir(parents=True)
    (stale / "KEEP").write_text("a live sibling is here")
    (scratch_root(r) / "1635-base.owner").write_text(str(os.getpid()))
    calls = []
    got = measure(r, runner=ok_runner(calls=calls))
    assert calls == [] and (stale / "KEEP").exists()
    assert metric_lines(got)[0].startswith("words: unmeasured") and "owner" in metric_lines(got)[0]


def test_scratch_path_is_not_diff_revert_dir(tmp_path):
    r = build(tmp_path)
    base, head = qd.scratch_paths(r.sdlc, "1635")
    assert base.parent == head.parent == (r.sdlc / "state" / "quality_delta").resolve()
    assert qd.diff_revert.scratch_dir(r.sdlc, "1635") not in (base, head)
    assert "diff_revert" not in str(base)


def test_two_goal_ids_run_concurrently_without_collision(tmp_path):
    """Two goals, one repository, at the same moment. Each metric command waits (hard 40 s cap) until the
    other has arrived, so serialised or colliding scratch dirs make this fail, never hang."""
    shared = tmp_path / "barrier"
    shared.mkdir()
    waiter = ("import os,sys,time,glob\n"
              "d=sys.argv[1]; open(os.path.join(d, str(os.getpid())+'.arrive'),'w').close()\n"
              "end=time.time()+40\n"
              "while len(glob.glob(os.path.join(d,'*.arrive')))<2 and time.time()<end: time.sleep(0.05)\n"
              "print(int(len(glob.glob(os.path.join(d,'*.arrive')))>=2))\n")
    r = build(tmp_path / "r", metrics=[metric(command=[sys.executable, "-c", waiter, str(shared)], timeout_seconds=55)])
    write_record(r, goal="g2", branch="sdlc/1635")
    procs = [subprocess.Popen([sys.executable, str(SCRIPT), goal], cwd=str(r.wt), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL) for goal in ("1635", "g2")]
    outs = []
    try:
        for proc in procs:
            outs.append(proc.communicate(timeout=100))
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
    for out, err in outs:
        assert "words: base=1 head=1 delta=0 same" in out.splitlines(), (out, err)
    assert no_scratch(r)


def test_untracked_sdlc_copies_leave_head_measured(tmp_path):
    r = build(tmp_path)
    for rel in ("plans/1635.md", "research/1635.md", "acceptance/1635.md"):
        (r.wt / ".sdlc" / rel).parent.mkdir(parents=True, exist_ok=True)
        (r.wt / ".sdlc" / rel).write_text("copy")
    got = measure(r, runner=ok_runner("3\n"))
    assert metric_lines(got) == ["words: base=3 head=3 delta=0 same"]


@pytest.mark.parametrize("dirty", ["untracked", "untracked-though-status-hides-them", "modified", "staged"])
def test_dirty_goal_makes_head_unmeasured(tmp_path, dirty):
    r = build(tmp_path)
    if dirty.startswith("untracked"):
        if dirty != "untracked":
            git(r.wt, "config", "status.showUntrackedFiles", "no")
        (r.wt / "new_source.py").write_text("x = 1\n")
    elif dirty == "modified":
        (r.wt / "data.txt").write_text("changed but not committed\n")
    else:
        (r.wt / "data.txt").write_text("staged only\n")
        git(r.wt, "add", "data.txt")
    calls = []
    got = measure(r, runner=ok_runner(calls=calls))
    (line,) = metric_lines(got)
    assert line.startswith("words: unmeasured") and "uncommitted" in line and "same" not in line
    assert got.out[0] == "base=%s head=%s" % (r.fork, r.head)


def test_header_line_prints_base_and_head_sha(tmp_path):
    r = build(tmp_path)
    got = measure(r, runner=ok_runner())
    assert got.out[0] == "base=%s head=%s" % (r.fork, r.head)


def test_header_dash_when_no_fork_or_record(tmp_path):
    r = build(tmp_path, record=False)
    got = measure(r, runner=ok_runner())
    assert got.out[0] == "base=- head=%s" % r.head and got.code == 0
    (tmp_path / "plain").mkdir()
    assert qd.measure("1635", tmp_path / "plain", probe=PLENTY, runner=ok_runner()).out[0] \
        == "base=- head=-"


@pytest.mark.parametrize("kind", ["symlink", "directory", "gitlink"])
def test_ls_tree_non_blob_entry_is_unreachable(tmp_path, kind):
    r = build(tmp_path, config_kind=kind)
    calls = []
    got = measure(r, runner=ok_runner(calls=calls))
    assert calls == [] and got.code == 0
    assert got.out[0].startswith("base=%s" % r.fork) and got.out[1].startswith("unmeasured")
    assert "no metrics configured" not in got.out


def test_config_with_exec_bit_is_read(tmp_path):
    r = build(tmp_path, config_kind="executable")
    assert git(r.main, "ls-tree", r.fork, "--", ".sdlc/config.json").startswith("100755 blob")
    assert metric_lines(measure(r, runner=ok_runner("3\n"))) == ["words: base=3 head=3 delta=0 same"]


def test_cleanup_with_git2_adapter_removes_scratch_dir(tmp_path):
    """diff_revert.cleanup unpacks (code, out); the adapter gives it that shape. Where it matters is the
    fallback for a directory git no longer knows (a dead run's leftover): `worktree remove` fails, and the
    rmtree after it is reached only if the unpack works. The wrong-shape control hands cleanup the 3-tuple
    helper, whose unpack raises inside cleanup's blanket except, so the leftover stays behind."""
    r = build(tmp_path)
    scratch_root(r).mkdir(parents=True)
    leftover = scratch_root(r) / "leftover"
    leftover.mkdir()
    (leftover / "STALE").write_text("x")
    qd.diff_revert.cleanup(r.wt, leftover, qd._git)
    assert leftover.exists(), "the 3-tuple helper must NOT be accepted by cleanup: it is why _git2 exists"
    qd.diff_revert.cleanup(r.wt, leftover, qd._git2)
    assert not leftover.exists()
    registered = scratch_root(r) / "registered"
    git(r.wt, "worktree", "add", "-q", "--detach", str(registered), r.fork)
    qd.diff_revert.cleanup(r.wt, registered, qd._git2)
    assert not registered.exists() and "registered" not in git(r.main, "worktree", "list")


def test_cli_from_goal_worktree_uses_main_checkout_sdlc(tmp_path):
    r = build(tmp_path)
    done = subprocess.run([sys.executable, str(SCRIPT), "1635"], cwd=str(r.wt), capture_output=True, text=True,
                          timeout=120, stdin=subprocess.DEVNULL)
    assert done.returncode == 0, done.stderr
    assert (r.sdlc / "state" / "quality_delta").is_dir(), "scratch lives in the main checkout's .sdlc"
    assert not (r.wt / ".sdlc" / "state" / "quality_delta").exists()
    assert done.stdout.splitlines()[1] == "words: base=3 head=5 delta=2 worse"


def test_goal_with_md_suffix_gives_same_scratch_stem(tmp_path):
    r = build(tmp_path)
    assert qd._stem("1635.md") == qd._stem("1635") == "1635"
    assert qd._stem("goals/1635.md") == "1635"
    assert qd.scratch_paths(r.sdlc, qd._stem("1635.md")) == qd.scratch_paths(r.sdlc, qd._stem("1635"))
    got = measure(r, goal="1635.md", runner=ok_runner("2\n"))
    assert metric_lines(got) == ["words: base=2 head=2 delta=0 same"]


# ------------------------------------------------------------------------------------------ Task 3

def test_ac1_prints_base_head_delta_and_verdict(tmp_path):
    """AC-1: three metrics on the real runner: more words (worse for lower), fewer (better), equal (same)."""
    r = build(tmp_path, metrics=[
        metric(name="grows", command=WORDS),
        metric(name="shrinks", command=[sys.executable, "-c", "print(10 - len(open('data.txt').read().split()))"]),
        metric(name="flat", command=[sys.executable, "-c", "print(7)"]),
        metric(name="grows-higher", command=WORDS, direction="higher"),
    ])
    got = measure(r)
    assert got.out == ["base=%s head=%s" % (r.fork, r.head),
                       "grows: base=3 head=5 delta=2 worse",
                       "shrinks: base=7 head=5 delta=-2 better",
                       "flat: base=7 head=7 delta=0 same",
                       "grows-higher: base=3 head=5 delta=2 better"], got
    assert got.code == 0


def test_ac1_lower_is_better_improves_when_head_is_smaller(tmp_path):
    r = build(tmp_path, metrics=[metric(name="shrinks", command=[sys.executable, "-c",
                                                                "print(10 - len(open('data.txt').read().split()))"],
                                        direction="higher")])
    assert metric_lines(measure(r)) == ["shrinks: base=7 head=5 delta=-2 worse"]
    r2 = build(tmp_path / "again", metrics=[metric(name="m", command=WORDS, direction="lower")],
               base_files={"data.txt": "a b c d e\n"}, head_files={"data.txt": "a b c\n"})
    assert metric_lines(measure(r2)) == ["m: base=5 head=3 delta=-2 better"]


FAILURES = {
    "exit-1": [sys.executable, "-c", "import sys;print(5);sys.exit(1)"],
    "not-found": ["/no/such/binary-for-quality-delta"],
    "non-numeric": [sys.executable, "-c", "print('lots')"],
    "empty": [sys.executable, "-c", "pass"],
    "over-cap": [sys.executable, "-c", "import sys;sys.stdout.write('9'*(1<<21)+chr(10)+'5'+chr(10))"],
}


@pytest.mark.parametrize("which", ["base", "head"])
@pytest.mark.parametrize("failure", sorted(FAILURES))
def test_ac2_failure_at_either_commit_is_unmeasured_never_same(tmp_path, failure, which):
    """AC-2: the command fails at exactly one commit (decided by the checkout's data.txt); never `same`."""
    bad = FAILURES[failure]
    guard = ("import subprocess,sys\n"
             "n=len(open('data.txt').read().split())\n"
             "sys.exit(subprocess.call(%r)) if n==%d else print(5)\n" % (bad, 3 if which == "base" else 5))
    if failure in ("over-cap", "non-numeric", "empty"):
        guard = ("import subprocess,sys\n"
                 "n=len(open('data.txt').read().split())\n"
                 "if n==%d:\n    sys.stdout.write(subprocess.run(%r,capture_output=True,text=True).stdout)\nelse:\n    print(5)\n"
                 % (3 if which == "base" else 5, bad))
    r = build(tmp_path, metrics=[metric(command=[sys.executable, "-c", guard], timeout_seconds=60)])
    got = measure(r)
    (line,) = metric_lines(got)
    assert line.startswith("words: unmeasured") and "same" not in line and "delta" not in line, line


def test_ac2_unmeasured_never_printed_as_same_when_both_sides_fail(tmp_path):
    r = build(tmp_path, metrics=[metric(command=FAILURES["exit-1"])])
    (line,) = metric_lines(measure(r))
    assert line.startswith("words: unmeasured") and "same" not in line


def test_ac2_timeout_alone_is_unmeasured(tmp_path):
    r = build(tmp_path, metrics=[metric(command=[sys.executable, "-c",
                                        "import time;print(5, flush=True);time.sleep(60)"], timeout_seconds=1)])
    (line,) = metric_lines(measure(r))
    assert line.startswith("words: unmeasured") and "same" not in line


def test_ac2_truncated_output_is_unmeasured(tmp_path):
    def runner(argv, cwd, timeout, env):
        return qd.bounded_run.Result(qd.bounded_run.OK, 0, "5\n", truncated=True)

    r = build(tmp_path)
    (line,) = metric_lines(measure(r, runner=runner))
    assert line.startswith("words: unmeasured") and "same" not in line


@pytest.mark.parametrize("outcome", ["failed", "timeout", "error", "stopped", "refused"])
def test_ac2_each_non_ok_outcome_is_unmeasured(tmp_path, outcome):
    def runner(argv, cwd, timeout, env):
        return qd.bounded_run.Result(outcome, 1, "5\n")

    r = build(tmp_path)
    (line,) = metric_lines(measure(r, runner=runner))
    assert line.startswith("words: unmeasured") and "same" not in line


def test_ac2_one_sided_unmeasured_is_not_a_measured_row(tmp_path):
    answers = iter([qd.bounded_run.Result("ok", 0, "5\n"), qd.bounded_run.Result("failed", 1, "5\n")])
    r = build(tmp_path)
    (line,) = metric_lines(measure(r, runner=lambda *a: next(answers)))
    assert line.startswith("words: unmeasured") and "base=" not in line.split(":", 1)[1].split("(")[0]


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_gone(pid, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


GRANDCHILD = ("import os,subprocess,sys,time\n"
              "kid=subprocess.Popen(['sleep','300'])\n"
              "open(sys.argv[1],'w').write(str(kid.pid))\n"
              "time.sleep(300)\n")


def test_ac3_timeout_kills_process_group_and_removes_scratch(tmp_path):
    """AC-3. Hard deadlines throughout: the wall time of measure() is bounded by T + 2 s grace + slack, the
    grandchild must be gone as measure() returns, and the leftover is SIGKILLed in `finally`."""
    pidfile = tmp_path / "grandchild.pid"
    r = build(tmp_path / "r", metrics=[metric(command=[sys.executable, "-c", GRANDCHILD, str(pidfile)],
                                              timeout_seconds=1.5)])
    pid = None
    try:
        started = time.monotonic()
        got = measure(r)
        elapsed = time.monotonic() - started
        pid = int(pidfile.read_text())
        assert metric_lines(got)[0].startswith("words: unmeasured") and "timed out" in metric_lines(got)[0]
        assert elapsed < 1.5 + 2 + 4, "run_group must stop the group inside its grace, took %.1fs" % elapsed
        assert _wait_gone(pid, 1.0), "the grandchild survived the group kill"
        assert no_scratch(r)
    finally:
        if pid and _alive(pid):
            os.kill(pid, signal.SIGKILL)


def test_setsid_grandchild_escapes_group_kill(tmp_path):
    """A documented limit, not a fix: a grandchild that calls setsid leaves the process group, so the group
    kill cannot reach it. docs/quality-delta.md says so. The escapee is SIGKILLed in `finally`."""
    pidfile = tmp_path / "escapee.pid"
    code = ("import os,subprocess,sys,time\n"
            "kid=subprocess.Popen(['sleep','300'],start_new_session=True)\n"
            "open(sys.argv[1],'w').write(str(kid.pid))\n"
            "time.sleep(300)\n")
    r = build(tmp_path / "r", metrics=[metric(command=[sys.executable, "-c", code, str(pidfile)],
                                              timeout_seconds=1.5)])
    pid = None
    try:
        measure(r)
        pid = int(pidfile.read_text())
        assert _alive(pid), "if this ever fails, the documented limit no longer holds and the doc must change"
    finally:
        if pid and _alive(pid):
            os.kill(pid, signal.SIGKILL)


def test_sigkill_of_quality_delta_kills_metric_group_and_rerun_reclaims(tmp_path):
    """The lifeline: SIGKILL the CLI mid-command; the metric group still dies; a rerun reclaims the dead-owner
    scratch and measures. Everything is deadline-polled and every leftover is killed in `finally`."""
    pidfile = tmp_path / "metric.pid"
    code = ("import os,sys,time\n"
            "open(sys.argv[1],'w').write(str(os.getpid()))\n"
            "time.sleep(300)\n")
    r = build(tmp_path / "r", metrics=[metric(command=[sys.executable, "-c", code, str(pidfile)],
                                              timeout_seconds=200)])
    proc = subprocess.Popen([sys.executable, str(SCRIPT), "1635"], cwd=str(r.wt), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    pid = None
    try:
        end = time.monotonic() + 30
        while not pidfile.exists() and time.monotonic() < end:
            time.sleep(0.05)
        assert pidfile.exists(), "the metric command never started"
        time.sleep(0.2)
        pid = int(pidfile.read_text())
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
        assert _wait_gone(pid, 20), "the lifeline did not stop the metric group after SIGKILL of quality_delta"
        assert (scratch_root(r) / "1635-base.owner").exists(), "a SIGKILLed run leaves its owner marker"
        pidfile.unlink()
        again = measure(r, runner=ok_runner("6\n"))
        assert metric_lines(again) == ["words: base=6 head=6 delta=0 same"]
        assert no_scratch(r), (list(scratch_root(r).iterdir()), git(r.main, "worktree", "list"))
    finally:
        if proc.poll() is None:
            proc.kill()
        for stream in (proc.stdout, proc.stderr):
            stream.close()
        if pid and _alive(pid):
            os.kill(pid, signal.SIGKILL)


def test_ac5_no_metrics_at_merge_base_starts_no_process_even_when_head_lists_metrics(tmp_path):
    marker = tmp_path / "ran"
    touch = [sys.executable, "-c", "import sys;open(sys.argv[1],'w').write('x');print(1)", str(marker)]
    r = build(tmp_path / "r", base_config={"quality": {"_comment": "none"}},
              head_config={"quality": {"metrics": [metric(command=touch)]}})
    got = measure(r)
    assert got.out == ["no metrics configured"] and got.code == 0 and not marker.exists()


@pytest.mark.parametrize("bad", [{"metrics": [{"name": "x"}]}, {"metrics": "x"}, {"metrics": [metric(direction="up")]}])
def test_ac5_malformed_block_refuses_exit2_empty_stdout(tmp_path, bad):
    r = build(tmp_path, base_config={"quality": bad})
    done = subprocess.run([sys.executable, str(SCRIPT), "1635"], cwd=str(r.wt), capture_output=True, text=True,
                          timeout=120, stdin=subprocess.DEVNULL)
    assert done.returncode == 2 and done.stdout == ""
    assert done.stderr.startswith("quality_delta.py: REFUSED [bad-metric] "), done.stderr


def test_ac5_refusal_names_entry_index(tmp_path):
    r = build(tmp_path, base_config={"quality": {"metrics": [metric(), metric(name="two", direction="sideways")]}})
    got = measure(r)
    assert got.code == 2 and got.out == [] and got.err == [
        "quality_delta.py: REFUSED [bad-metric] metrics[1]: direction must be lower or higher"]


def test_ac6_runs_argv_without_shell(tmp_path):
    marker = tmp_path / "shell-ran"
    arg = "$(touch %s);x" % marker
    r = build(tmp_path / "r", metrics=[metric(command=[sys.executable, "-c",
                                                      "import sys;print(len(sys.argv[1]))", arg])])
    got = measure(r)
    assert metric_lines(got) == ["words: base=%d head=%d delta=0 same" % (len(arg), len(arg))], got
    assert not marker.exists()


def test_ac6_command_environment_has_no_credentials(tmp_path, monkeypatch):
    probe = ("import os;names=['GH_TOKEN','GITHUB_TOKEN','MY_API_KEY','SSH_AUTH_SOCK','SIGMA_EXTRA_NAME'];"
             "print(sum(n in os.environ for n in names))")
    r = build(tmp_path, metrics=[metric(command=[sys.executable, "-c", probe])], quality_extra={"env_drop": ["SIGMA_EXTRA_NAME"]})
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "MY_API_KEY", "SSH_AUTH_SOCK", "SIGMA_EXTRA_NAME"):
        monkeypatch.setenv(name, "placeholder-value")
    assert metric_lines(measure(r)) == ["words: base=0 head=0 delta=0 same"]


def test_ac6_command_environment_starts_from_unattended_env(tmp_path, monkeypatch):
    probe = ("import os;print(int('GIT_INDEX_FILE' not in os.environ and os.environ.get('GIT_TERMINAL_PROMPT')=='0'))")
    r = build(tmp_path, metrics=[metric(command=[sys.executable, "-c", probe])])
    monkeypatch.setenv("GIT_INDEX_FILE", "/placeholder/index")
    assert metric_lines(measure(r)) == ["words: base=1 head=1 delta=0 same"]


def test_ac7_probe_unreadable_runs_no_command(tmp_path):
    r = build(tmp_path)
    calls = []
    got = measure(r, probe=lambda: None, runner=ok_runner(calls=calls))
    assert calls == [] and scratch_root(r).exists() is False
    assert metric_lines(got) == ["words: unmeasured (memory probe unreadable)"]


def test_ac7_below_floor_runs_no_command(tmp_path):
    r = build(tmp_path, quality_extra={"memory_floor_fraction": 0.5})
    calls = []
    got = measure(r, probe=lambda: (499, 1000), runner=ok_runner(calls=calls))
    assert calls == [] and "memory" in metric_lines(got)[0] and metric_lines(got)[0].startswith("words: unmeasured")


def test_ac7_exactly_at_floor_runs(tmp_path):
    r = build(tmp_path, quality_extra={"memory_floor_fraction": 0.5})
    calls = []
    got = measure(r, probe=lambda: (500, 1000), runner=ok_runner(calls=calls))
    assert len(calls) == 2 and metric_lines(got) == ["words: base=3 head=3 delta=0 same"]


def test_ac7_default_floor_is_applied_when_unconfigured(tmp_path):
    r = build(tmp_path, quality_extra={"memory_floor_fraction": None})
    cfg = json.loads((r.sdlc / "config.json").read_text())
    del cfg["quality"]["memory_floor_fraction"]
    (r.main / ".sdlc" / "config.json").write_text(json.dumps(cfg))
    git(r.main, "commit", "-q", "-am", "no floor")
    git(r.main, "push", "-q", "origin", "main")
    git(r.wt, "rebase", "-q", "main")
    calls = []
    assert measure(r, probe=lambda: (149, 1000), runner=ok_runner(calls=calls)).out[1].startswith("words: unmeasured")
    assert calls == []
    assert measure(r, probe=lambda: (150, 1000), runner=ok_runner(calls=calls)).out[1].startswith("words: base=")
    assert len(calls) == 2


def test_ac7_midrun_dip_marks_this_and_later_metrics_unmeasured_keeps_earlier(tmp_path):
    r = build(tmp_path, metrics=[metric(name="m1"), metric(name="m2"), metric(name="m3")],
              quality_extra={"memory_floor_fraction": 0.5})
    reads = iter([(900, 1000), (900, 1000), (900, 1000), (900, 1000), (10, 1000)])
    # up-front probe, then one probe per command: m1 base, m1 head, m2 base fine; m2 head dips
    calls = []
    got = measure(r, probe=lambda: next(reads, (10, 1000)), runner=ok_runner(calls=calls))
    out = metric_lines(got)
    assert out[0] == "m1: base=3 head=3 delta=0 same"
    assert out[1].startswith("m2: unmeasured") and "memory" in out[1]
    assert out[2].startswith("m3: unmeasured") and "memory" in out[2]
    assert len(calls) == 3


def test_probe_checked_before_each_command(tmp_path):
    r = build(tmp_path, metrics=[metric(name="m1"), metric(name="m2")])
    count = []
    measure(r, probe=lambda: count.append(1) or (10 ** 12, 10 ** 12), runner=ok_runner())
    assert len(count) == 1 + 4


def test_windows_refuses_not_supported_here(tmp_path, monkeypatch):
    r = build(tmp_path)
    calls = []
    monkeypatch.setattr(qd.bounded_run, "group_refusal", lambda: "no process groups here")
    got = measure(r, runner=ok_runner(calls=calls))
    assert got.code == 2 and got.out == [] and "not supported here" in got.err[0] and calls == []


def test_changed_files_scope_passes_nul_list_in_env_file(tmp_path):
    r = build(tmp_path, metrics=[metric(scope="changed-files")],
              base_files={"a.txt": "1\n", "b dir/space name.txt": "1\n", "c.txt": "1\n", "data.txt": "a b c\n"},
              head_files={"a.txt": "2\n", "b dir/space name.txt": "2\n", "data.txt": "a b c\n"})
    calls, listing = [], []

    def runner(argv, cwd, timeout, env):
        path = pathlib.Path(env["SIGMA_QUALITY_FILES"])
        calls.append((pathlib.Path(cwd), path, argv))
        listing.append(path.read_bytes())
        return qd.bounded_run.Result("ok", 0, "1\n")

    measure(r, runner=runner)
    for (cwd, path, argv), raw in zip(calls, listing):
        assert path == cwd / ".sigma-quality-files" and path.is_absolute()
        assert raw == b"a.txt\0b dir/space name.txt\0", raw
        assert not any(str(path) in part for part in argv), "the list travels in the environment, never argv"
    assert no_scratch(r)


def test_changed_files_excludes_path_missing_at_one_commit(tmp_path):
    r = build(tmp_path, metrics=[metric(scope="changed-files")],
              base_files={"keep.txt": "1\n", "gone.txt": "1\n", "data.txt": "a\n"},
              head_files={"keep.txt": "2\n", "added.txt": "1\n"})
    git(r.wt, "rm", "-q", "gone.txt")
    git(r.wt, "commit", "-q", "-m", "drop")
    r.head = git(r.wt, "rev-parse", "HEAD")
    listing = []

    def runner(argv, cwd, timeout, env):
        listing.append(pathlib.Path(env["SIGMA_QUALITY_FILES"]).read_bytes())
        return qd.bounded_run.Result("ok", 0, "1\n")

    measure(r, runner=runner)
    assert listing == [b"keep.txt\0"] * 2, listing


def test_changed_files_empty_is_unmeasured(tmp_path):
    r = build(tmp_path, metrics=[metric(scope="changed-files")], head_commit=False)
    calls = []
    got = measure(r, runner=ok_runner(calls=calls))
    assert calls == [] and metric_lines(got)[0].startswith("words: unmeasured") and "no changed files" in metric_lines(got)[0]


def test_repo_scope_has_no_files_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGMA_QUALITY_FILES", "/stale/inherited/value")
    r = build(tmp_path)
    calls = []
    measure(r, runner=ok_runner(calls=calls))
    assert all("SIGMA_QUALITY_FILES" not in c["env"] for c in calls) and len(calls) == 2


def test_command_that_writes_does_not_dirty_goal_worktree(tmp_path):
    r = build(tmp_path, metrics=[metric(command=[sys.executable, "-c",
                                                 "open('scratch-output.txt','w').write('x');print(1)"])])
    before = git(r.wt, "status", "--porcelain", "--untracked-files=normal")
    measure(r)
    assert git(r.wt, "status", "--porcelain", "--untracked-files=normal") == before
    assert not (r.wt / "scratch-output.txt").exists()
    assert git(r.wt, "rev-parse", "HEAD") == r.head


def test_cli_requires_goal_and_help_has_no_side_effects(tmp_path):
    for argv in ([], ["a", "b"], ["--bogus"]):
        done = subprocess.run([sys.executable, str(SCRIPT), *argv], cwd=str(tmp_path), capture_output=True, text=True,
                              timeout=60, stdin=subprocess.DEVNULL)
        assert done.returncode == 2 and done.stdout == "" and "usage" in done.stderr.lower(), argv
    for flag in ("-h", "--help"):
        done = subprocess.run([sys.executable, str(SCRIPT), flag], cwd=str(tmp_path), capture_output=True, text=True,
                              timeout=60, stdin=subprocess.DEVNULL)
        assert done.returncode == 0 and "usage" in done.stdout.lower()
    assert list(tmp_path.iterdir()) == []


def test_runner_receives_timeout_and_argv_exactly(tmp_path):
    r = build(tmp_path, metrics=[metric(timeout_seconds=7)])
    calls = []
    measure(r, runner=ok_runner(calls=calls))
    assert [c["timeout"] for c in calls] == [7.0, 7.0] and calls[0]["argv"] == WORDS


def test_default_runner_bounds_output_and_grace(tmp_path, monkeypatch):
    seen = {}

    def fake(command, cwd, timeout, **kwargs):
        seen.update(kwargs)
        return qd.bounded_run.Result("ok", 0, "1\n")

    monkeypatch.setattr(qd.bounded_run, "run_group", fake)
    qd._run_group(["x"], "/tmp", 5, {"A": "b"})
    assert seen["merge"] is False and seen["max_out_bytes"] == 1 << 20 and seen["term_grace"] == 2
    assert seen["env"] == {"A": "b"} and not seen.get("shell")


def test_required_flag_is_validated_but_does_not_change_the_row(tmp_path):
    r = build(tmp_path, metrics=[metric(required=True)])
    assert metric_lines(measure(r, runner=ok_runner("3\n"))) == ["words: base=3 head=3 delta=0 same"]


# ------------------------------------------------------------------------- review send-back (#1635)

def _second_goal(r, goal="2000"):
    wt2 = r.tmp / ("wt" + goal)
    git(r.main, "worktree", "add", "-q", "-b", "sdlc/%s" % goal, str(wt2))
    (wt2 / "data.txt").write_text("a b c d e f g\n")
    git(wt2, "add", "-A")
    git(wt2, "commit", "-q", "-m", "other goal")
    write_record(r, goal=goal, worktree=str(wt2), branch="sdlc/%s" % goal)
    return wt2


def test_wrong_goal_worktree_is_unmeasured(tmp_path):
    """From goal 2000's worktree, `quality_delta.py 1635` must not measure 2000's head as 1635's."""
    r = build(tmp_path)
    wt2 = _second_goal(r)
    calls = []
    got = qd.measure("1635", wt2, probe=PLENTY, runner=ok_runner(calls=calls))
    assert calls == [] and got.out[0].startswith("base=- head=")
    assert got.out[1].startswith("unmeasured") and "worktree" in got.out[1]
    ok = qd.measure("2000", wt2, probe=PLENTY, runner=ok_runner("3\n"))
    assert metric_lines(ok) == ["words: base=3 head=3 delta=0 same"]


def test_wrong_branch_in_the_right_worktree_is_unmeasured(tmp_path):
    r = build(tmp_path)
    git(r.wt, "checkout", "-q", "-b", "somewhere-else")
    calls = []
    got = measure(r, runner=ok_runner(calls=calls))
    assert calls == [] and got.out[1].startswith("unmeasured") and "branch" in got.out[1]


def test_worktree_add_has_its_own_bounded_timeout(tmp_path):
    r = build(tmp_path)
    real, seen = qd._git, []

    def spy(root, argv, timeout=30):
        if argv[:2] == ["worktree", "add"]:
            seen.append(timeout)
        return real(root, argv, timeout)

    measure(r, git=spy, runner=ok_runner())
    assert seen == [qd._WORKTREE_ADD_TIMEOUT] * 2 and 30 < qd._WORKTREE_ADD_TIMEOUT <= 3600


def test_negative_zero_metric_prints_zero(tmp_path):
    r = build(tmp_path, metrics=[metric(command=[sys.executable, "-c", "print('-0')"])])
    assert metric_lines(measure(r)) == ["words: base=0 head=0 delta=0 same"]


def test_right_branch_but_other_worktree_path_is_unmeasured(tmp_path):
    r = build(tmp_path)
    write_record(r, worktree=str(tmp_path / "elsewhere"))
    calls = []
    got = measure(r, runner=ok_runner(calls=calls))
    assert calls == [] and got.out[1].startswith("unmeasured") and "worktree" in got.out[1]
