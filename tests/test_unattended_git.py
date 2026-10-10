"""#920: the unattended git runner (skills/sigma-loop/scripts/unattended_git.py) injected into the real rebase engine.

PLANNED TESTS of the pull-request red-to-green gate (the first eight tests, which start by asking `unattended_support`
for the module and so fail by AssertionError until it exists). The three tests after them touch only code that already
exists, are green before the module is written, and are NOT listed: they prove that the hostility the planned tests
defeat is real (a runner without the pins really fails), and that the engine's default runner is untouched.

The file is bound by its whole-file hash and is not edited after the red verify. Real git runs in temporary
repositories under a hermetic configuration (the machine's own git configuration is never read); the engine is driven
through its real `_rebase_feature`. A control sits in the test it controls: the same check, run against a copy of the
module with one pin removed, must fail there."""
import hashlib
import http.server
import inspect
import json
import os
import shutil
import threading
import types

import pytest

import unattended_support as support

#: sha256 of the source text of the engine's default runner (feature_sync._run) before this change; it must not move.
DEFAULT_RUNNER_SHA256 = "f99c25618cceeba86a6b1092b9b8d6fd2232fe376b99602beb984a8a94b92ee2"
FALSE = shutil.which("false") or "/usr/bin/false"
HOSTILE = (("signing", "[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = %s\n" % FALSE),
           ("push signing", "[push]\n\tgpgSign = true\n"),
           ("update refs", "[rebase]\n\tupdateRefs = true\n"))
SHIM_ENV = r'''printf 'ARG=%s\n' "$@"
for v in GIT_TERMINAL_PROMPT GIT_EDITOR GIT_ASKPASS SSH_ASKPASS GIT_DIR GIT_INDEX_FILE GIT_WORK_TREE ONLY_HERE; do
  eval "val=\${$v-UNSET}"
  printf '%s=%s\n' "$v" "$val"
done
'''


def hostile(config, text):
    config.write_text(config.read_text(encoding="utf-8") + text, encoding="utf-8")


CONFIG = {"work": {"enabled": True, "base": "main", "remote": "origin", "branch_prefix": "sdlc/",
                   "worktree_dir": ".sdlc/work", "merge_method": "squash"},
          "discovery": {"source": "github", "github": {"repo": "org/repo"}}}


def replay(module, case, monkeypatch, extra="", hooks="off", hook_files=()):
    """One real engine pass (`feature_rebase.upkeep`, the public entry, with the runner injected as `run=`) in a fresh
    world -> (outcome, the remote moved, `inner` stayed put, a hook ran, the fetch record survived)."""
    fr = support.real("feature_rebase")
    registry = support.real("feature_registry")
    config = support.hermetic(case, monkeypatch)
    world = support.World(case)
    marker = case / "hook-ran"
    for name, body in hook_files:
        support.script(world.local / ".git" / "hooks" / name, body.replace("MARKER", str(marker)))
    sdlc = world.local / ".sdlc"
    registry.registry_dir(sdlc).mkdir(parents=True, exist_ok=True)
    registry.write_unit(registry.registry_dir(sdlc), "x",
                        {"open": True, "repos": {"org/repo": {"branch": "feature/x", "goals": []}}})
    stub = types.SimpleNamespace(DEFAULT_PRIORITY="P1", create_tracked_issue=lambda *a, **k: {"issue": "1", "warnings": [],
                                                                                              "duplicate_of": None})
    monkeypatch.setattr(fr, "_HANDOFF", stub)             # a conflict would open a real issue: the filer is injected
    record = world.local / ".git" / "FETCH_HEAD"
    record.write_text("SENTINEL\n", encoding="utf-8")
    hostile(config, extra)
    run = module.make_runner({"default": 60}, hooks=hooks) if module is not None else None
    report = fr.upkeep(str(sdlc), CONFIG, "7", "x", run=run, cwd=str(world.local))
    inner = support.git(world.local, "rev-parse", "inner")
    return (report["outcome"], world.remote_tip() != world.tip, inner == world.inner, marker.exists(),
            record.read_text(encoding="utf-8") == "SENTINEL\n")


def test_runner_keeps_the_engine_contract(tmp_path, monkeypatch):
    """Stripped stdout, a raise with git's own text, stdout kept clean of stderr, and the same answers as the default."""
    ug = support.load("unattended_git")
    support.hermetic(tmp_path, monkeypatch)
    repo = support.make_repo(tmp_path)
    default = support.real("feature_sync")._run
    run = ug.make_runner({"default": 60})
    head = support.git(repo, "rev-parse", "HEAD")
    assert run(repo, ["git", "rev-parse", "HEAD"]) == head == default(repo, ["git", "rev-parse", "HEAD"])
    assert run(repo, ["git", "status", "--porcelain"]) == "" and run(repo, ["echo", "  hi  "]) == "hi"
    monkeypatch.setenv("GIT_TRACE", "1")                    # git writes trace lines to stderr: stdout must stay clean
    assert run(repo, ["git", "rev-parse", "HEAD"]) == head
    monkeypatch.delenv("GIT_TRACE")
    errors = []
    for runner in (run, default):
        with pytest.raises(RuntimeError) as caught:
            runner(repo, ["git", "cat-file", "-t", "no-such-ref-920"])
        errors.append(str(caught.value))
    assert errors[0] == errors[1] and "no-such-ref-920" in errors[0], errors
    with pytest.raises(RuntimeError) as exited:
        run(repo, ["sh", "-c", "exit 4"])
    assert "exited 4" in str(exited.value)
    small = ug.make_runner({"default": 60}, max_out_bytes=1000)
    with pytest.raises(RuntimeError) as flood:
        small(repo, ["sh", "-c", "yes | head -c 5000"])
    assert "output exceeded" in str(flood.value)


def test_pins_reach_git_in_argv_and_environment(tmp_path, monkeypatch):
    """Pins sit right after git, the verb is found past global options, fetch gets its flag, hooks follow the policy."""
    ug = support.load("unattended_git")
    pins = ["-c", "commit.gpgsign=false", "-c", "push.gpgSign=false", "-c", "rebase.updateRefs=false",
            "-c", "maintenance.auto=false"]
    flag = list(ug.FETCH_FLAGS)
    assert flag == ["--no-write-fetch-head"] and len(pins) == 2 * len(ug.CONFIG_PINS)
    table = [(["git", "fetch", "origin", "main"], ["fetch"] + flag + ["origin", "main"]),
             (["git", "-C", "/x", "-c", "a=b", "fetch", "origin"], ["-C", "/x", "-c", "a=b", "fetch"] + flag + ["origin"]),
             (["git", "--git-dir", "/y", "fetch"], ["--git-dir", "/y", "fetch"] + flag),
             (["git", "--no-pager", "fetch"], ["--no-pager", "fetch"] + flag),
             (["/usr/bin/git", "fetch"], ["fetch"] + flag),
             (["git", "push", "origin", "fetch"], ["push", "origin", "fetch"]),
             (["git", "-c", "fetch=x", "status"], ["-c", "fetch=x", "status"]),
             (["git"], [])]
    wrong = []
    for argv, rest in table:
        got = ug.decorate(str(tmp_path), argv, "inherit")
        if got != [argv[0]] + pins + rest:
            wrong.append((argv, got))
    assert wrong == [], wrong
    assert ug.decorate(str(tmp_path), ["echo", "fetch"]) == ["echo", "fetch"] and ug.decorate(str(tmp_path), []) == []
    assert [ug.verb_of(a) for a in (["git", "-C", "p", "log"], ["git"], ["gh", "pr"])] == ["log", None, "gh"]
    first = ug.decorate(str(tmp_path), ["git", "log"])
    second = ug.decorate(str(tmp_path), ["git", "log"])
    where = [a for a in first if a.startswith("core.hooksPath=")]
    assert first[:9] == ["git"] + pins and first[9] == "-c" and len(where) == 1 and first[-1] == "log"
    path = where[0].split("=", 1)[1]
    again = [a for a in second if a.startswith("core.hooksPath=")]
    assert path.startswith(str(tmp_path)) and not os.path.exists(path) and where != again, "fresh and absent every call"
    assert not [a for a in ug.decorate(str(tmp_path), ["git", "log"], "inherit") if "hooksPath" in a]
    with pytest.raises(ValueError):
        ug.decorate(str(tmp_path), ["git", "log"], "maybe")
    support.shim_git(tmp_path, monkeypatch, SHIM_ENV)
    for name, value in (("GIT_EDITOR", "vim"), ("GIT_ASKPASS", "/bin/evil"), ("SSH_ASKPASS", "/bin/evil"),
                        ("GIT_TERMINAL_PROMPT", "1"), ("GIT_DIR", "/nonexistent-920"),
                        ("GIT_INDEX_FILE", "/nonexistent-920"), ("GIT_WORK_TREE", "/nonexistent-920"),
                        ("ONLY_HERE", "from-the-process")):
        monkeypatch.setenv(name, value)

    def through_the_shim(runner, argv):
        seen = runner(tmp_path, argv).splitlines()
        return ([line[4:] for line in seen if line.startswith("ARG=")],
                dict(line.split("=", 1) for line in seen if not line.startswith("ARG=")))

    args, env = through_the_shim(ug.make_runner({"default": 30}), ["git", "status"])
    assert any(a.startswith("core.hooksPath=") for a in args), "hooks must be off unless the caller says otherwise"
    assert env.pop("ONLY_HERE") == "from-the-process", "the base environment is the process's by default"
    args, env = through_the_shim(ug.make_runner({"default": 30}, hooks="inherit"), ["git", "fetch", "origin"])
    assert args == pins + ["fetch", "--no-write-fetch-head", "origin"], args
    assert env.pop("ONLY_HERE") == "from-the-process"
    assert env == {"GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true", "GIT_ASKPASS": "", "SSH_ASKPASS": "",
                   "GIT_DIR": "UNSET", "GIT_INDEX_FILE": "UNSET", "GIT_WORK_TREE": "UNSET"}, env
    given = {"PATH": os.environ["PATH"], "ONLY_HERE": "given", "GIT_EDITOR": "vim"}
    _args, env = through_the_shim(ug.make_runner({"default": 30}, environ=given), ["git", "status"])
    assert (env["ONLY_HERE"], env["GIT_EDITOR"], env["GIT_DIR"]) == ("given", "true", "UNSET"), env


def test_time_limits_are_required_and_checked(tmp_path):
    """A runner cannot be made without a default limit, with a bad limit or key, or with an unknown hooks policy."""
    ug = support.load("unattended_git")
    bad = [{}, {"fetch": 5}, 5, None, [("default", 5)], {"default": 0}, {"default": -1}, {"default": True},
           {"default": float("nan")}, {"default": float("inf")}, {"default": "9"}, {"default": None},
           {"default": 10 ** 9}, {"default": 5, "": 3}, {"default": 5, 3: 4}]
    accepted = []
    for limits in bad:
        try:
            ug.make_runner(limits)
        except ValueError:
            continue
        accepted.append(limits)
    assert accepted == [], accepted
    with pytest.raises(ValueError):
        ug.make_runner({"default": 5}, hooks="maybe")
    assert callable(ug.make_runner({"default": 5, "fetch": 120, "push": 90.5}))


def test_overrun_raises_a_typed_error_and_stops_the_group(tmp_path, monkeypatch):
    """Each verb has its own limit; an overrun is a GitTimeout (a RuntimeError) naming argv and limit; the tree is gone."""
    ug = support.load("unattended_git")
    pidfile = tmp_path / "tree.pid"
    sleeper = "sleep 60 &\necho $! > %s\nwait\n" % pidfile
    stubborn = "trap '' TERM\n" + sleeper
    support.shim_git(tmp_path, monkeypatch, sleeper)
    grand = None
    try:
        run = ug.make_runner({"default": 30, "fetch": 2}, term_grace=3)
        with pytest.raises(ug.GitTimeout) as caught:
            run(tmp_path, ["git", "fetch", "origin"])
        grand = support.pid_in(pidfile)
        error = caught.value
        assert isinstance(error, RuntimeError) and error.argv == ["git", "fetch"]
        assert (error.limit, error.escalated) == (2.0, False) and "git fetch: timed out after 2s" in str(error)
        assert error.seconds < 8 and support.gone(grand), "the git tree must be gone after an overrun"
        assert str(error) == "git fetch: timed out after 2s" and "origin" not in str(error), "no argv in the message"
        with pytest.raises(ug.GitTimeout) as leaky:
            run(tmp_path, ["git", "fetch", "leaky-remote-920"])
        assert "leaky-remote-920" not in str(leaky.value) and leaky.value.argv == ["git", "fetch"]
        run = ug.make_runner({"default": 2, "fetch": 30}, term_grace=3)
        with pytest.raises(ug.GitTimeout) as default:
            run(tmp_path, ["git", "status"])
        assert default.value.limit == 2.0 and "git status: timed out" in str(default.value)
        support.shim_git(tmp_path, monkeypatch, stubborn)
        with pytest.raises(ug.GitTimeout) as hard:
            ug.make_runner({"default": 2}, term_grace=1)(tmp_path, ["git", "status"])
        assert hard.value.escalated is True
        support.shim_git(tmp_path, monkeypatch, "echo oops 1>&2\nexit 3\n")
        with pytest.raises(RuntimeError) as failed:
            ug.make_runner({"default": 30})(tmp_path, ["git", "status"])
        assert not isinstance(failed.value, ug.GitTimeout) and "oops" in str(failed.value)
    finally:
        support.reap(grand)


def test_a_real_replay_survives_a_hostile_configuration(tmp_path, monkeypatch):
    """Hostile signing, push signing and ref updating cannot stop or disturb the engine's real replay; hooks follow policy."""
    ug = support.load("unattended_git")
    fr = support.real("feature_rebase")
    wrong = []
    for label, extra in HOSTILE:
        case = tmp_path / label.replace(" ", "-")
        case.mkdir()
        got = replay(ug, case, monkeypatch, extra)
        if got != (fr.REBASED, True, True, False, True):
            wrong.append((label, got))
    assert wrong == [], wrong
    commit_hook = ("post-commit", "#!/bin/sh\necho x > MARKER\n")
    veto = ("pre-push", "#!/bin/sh\nexit 1\n")
    off = tmp_path / "hooks-off"
    off.mkdir()
    assert replay(ug, off, monkeypatch, hook_files=(commit_hook, veto)) == (fr.REBASED, True, True, False, True)
    inherit = tmp_path / "hooks-inherit"
    inherit.mkdir()
    got = replay(ug, inherit, monkeypatch, hooks="inherit", hook_files=(commit_hook, veto))
    assert got[0] != fr.REBASED and got[3] is True, "the inherit policy must leave the repository's own hooks alone"
    for n, (label, pin) in enumerate((("signing", '"commit.gpgsign=false", '), ("push signing", '"push.gpgSign=false", '),
                                      ("update refs", '"rebase.updateRefs=false", '))):
        broken = support.variant("unattended_git", (pin, ""))
        case = tmp_path / ("unpinned-%d" % n)
        case.mkdir()
        got = replay(broken, case, monkeypatch, dict(HOSTILE)[label])
        assert got[0] != fr.REBASED or got[2] is False, "the %s pin must be what saves it: %r" % (label, got)
    no_policy = support.variant("unattended_git", ('pins += ["-c", "core.hooksPath=" + no_hooks_dir(cwd)]', "pass"))
    lax = tmp_path / "no-policy"
    lax.mkdir()
    assert replay(no_policy, lax, monkeypatch, hook_files=(commit_hook, veto))[3] is True, "the policy is what keeps hooks off"


def test_fetch_keeps_the_record_and_starts_no_maintenance(tmp_path, monkeypatch):
    """A fetch from the main checkout or a linked worktree leaves the fetch record and starts no background child."""
    ug = support.load("unattended_git")
    support.hermetic(tmp_path, monkeypatch)
    world = support.World(tmp_path)
    other = tmp_path / "other"
    support.git(tmp_path, "clone", "-q", str(world.remote), str(other))
    linked = support.add_worktree(world.local, tmp_path / "linked")
    trace = tmp_path / "trace.json"
    record = world.local / ".git" / "FETCH_HEAD"

    def one_fetch(module, cwd):
        support.commit_file(other, "n%s.txt" % os.urandom(3).hex(), "n\n", "upstream moves")
        support.git(other, "push", "-q", "origin", "main")
        record.write_text("SENTINEL\n", encoding="utf-8")
        trace.write_text("", encoding="utf-8")
        monkeypatch.setenv("GIT_TRACE2_EVENT", str(trace))
        module.make_runner({"default": 60})(cwd, ["git", "fetch", "origin", "main"])
        monkeypatch.delenv("GIT_TRACE2_EVENT")
        return record.read_text(encoding="utf-8") == "SENTINEL\n", support.auto_children(trace)

    for label, cwd in (("main checkout", world.local), ("linked worktree", linked)):
        assert one_fetch(ug, cwd) == (True, 0), label
    assert support.git(world.local, "rev-parse", "origin/main") == support.git(other, "rev-parse", "HEAD")
    no_flag = support.variant("unattended_git", ("FETCH_FLAGS = (\"--no-write-fetch-head\",)", "FETCH_FLAGS = ()"))
    assert one_fetch(no_flag, world.local) == (False, 0), "the fetch flag is what keeps the record"
    no_switch = support.variant("unattended_git", ('"rebase.updateRefs=false", "maintenance.auto=false")',
                                                   '"rebase.updateRefs=false")'))
    kept, children = one_fetch(no_switch, world.local)
    assert kept is True and children >= 1, "the config switch is what stops the maintenance child"


def test_a_hung_credential_helper_cannot_stall_git(tmp_path, monkeypatch):
    """A 401 from a server plus hostile askpass helpers (env, config, ssh) fails fast; without the pin git hangs."""
    ug = support.load("unattended_git")
    sleeper = support.script(tmp_path / "askpass.sh", "sleep 60\n")
    support.hermetic(tmp_path, monkeypatch, "[core]\n\taskPass = %s\n" % sleeper)
    monkeypatch.setenv("GIT_ASKPASS", str(sleeper))
    monkeypatch.setenv("SSH_ASKPASS", str(sleeper))

    class Refuse(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="r"')
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            return None

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Refuse)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = "http://127.0.0.1:%d/r.git" % server.server_address[1]
    try:
        with pytest.raises(RuntimeError) as quick:
            ug.make_runner({"default": 30}, term_grace=2)(tmp_path, ["git", "ls-remote", url])
        assert not isinstance(quick.value, ug.GitTimeout) and quick.value.__class__ is RuntimeError, str(quick.value)
        unpinned = support.variant("bounded_run", ('("GIT_ASKPASS", ""), ', ""))
        broken = support.variant("unattended_git")
        broken.bounded_run = unpinned
        with pytest.raises(broken.GitTimeout):
            broken.make_runner({"default": 2}, term_grace=2)(tmp_path, ["git", "ls-remote", url])
    finally:
        server.shutdown()
        server.server_close()


def test_a_stopped_replay_is_told_apart_and_continues(tmp_path, monkeypatch):
    """Unmerged paths name a conflict and are empty for a failed signature; --continue survives hostile editors."""
    ug = support.load("unattended_git")
    fr = support.real("feature_rebase")
    sleeper = support.script(tmp_path / "editor.sh", "sleep 60\n")

    def conflicted(case, module):
        case.mkdir()
        support.hermetic(case, monkeypatch, "[core]\n\teditor = %s\n" % sleeper)
        for name in ("GIT_EDITOR", "VISUAL", "EDITOR"):
            monkeypatch.setenv(name, str(sleeper))
        repo = support.make_repo(case)
        support.commit_file(repo, "c.txt", "base\n", "base")
        support.git(repo, "checkout", "-q", "-b", "feature")
        support.commit_file(repo, "c.txt", "feature\n", "feature edits c")
        support.git(repo, "checkout", "-q", "main")
        support.commit_file(repo, "c.txt", "main\n", "main edits c")
        support.git(repo, "checkout", "-q", "feature")
        run = module.make_runner({"default": 60})
        with pytest.raises(RuntimeError):
            run(repo, ["git", "rebase", "main"])
        return run, repo

    run, repo = conflicted(tmp_path / "ok", ug)
    assert ug.unmerged_paths(run, repo) == ["c.txt"]
    (repo / "c.txt").write_text("resolved\n", encoding="utf-8")
    run(repo, ["git", "add", "c.txt"])
    run(repo, ["git", "rebase", "--continue"])
    assert support.git(repo, "log", "-1", "--format=%s") == "feature edits c" and not (repo / ".git" / "rebase-merge").exists()
    unpinned = support.variant("bounded_run", ('("GIT_EDITOR", "true"), ', ""))
    broken = support.variant("unattended_git")
    broken.bounded_run = unpinned
    run2, repo2 = conflicted(tmp_path / "hang", broken)
    (repo2 / "c.txt").write_text("resolved\n", encoding="utf-8")
    run2(repo2, ["git", "add", "c.txt"])
    with pytest.raises(broken.GitTimeout):
        broken.make_runner({"default": 3}, term_grace=2)(repo2, ["git", "rebase", "--continue"])
    case = tmp_path / "signing"
    case.mkdir()
    config = support.hermetic(case, monkeypatch)
    plain = support.make_repo(case)
    support.git(plain, "checkout", "-q", "-b", "side")
    support.commit_file(plain, "s.txt", "s\n", "side")
    support.git(plain, "checkout", "-q", "main")
    support.commit_file(plain, "m.txt", "m\n", "main moves")
    support.git(plain, "checkout", "-q", "side")
    hostile(config, HOSTILE[0][1])
    default = fr._run
    with pytest.raises(RuntimeError):
        default(plain, ["git", "rebase", "main"])
    assert fr.rebase_stopped(default, plain) is True, "the engine still files this stop as a conflict"
    assert ug.unmerged_paths(default, plain) == [], "a failed signature has no unmerged path"


def test_the_default_runner_is_untouched():
    """Green before the module exists: the engine's default runner keeps the exact source this change found."""
    source = inspect.getsource(support.real("feature_sync")._run)
    assert hashlib.sha256(source.encode("utf-8")).hexdigest() == DEFAULT_RUNNER_SHA256


def test_the_hostility_is_real_for_the_default_runner(tmp_path, monkeypatch):
    """Green before the module exists: under each hostile configuration the default runner fails or is disturbed."""
    fr = support.real("feature_rebase")
    outcomes = {}
    for label, extra in HOSTILE:
        case = tmp_path / label.replace(" ", "-")
        case.mkdir()
        outcomes[label] = replay(None, case, monkeypatch, extra)
    assert outcomes["signing"][0] != fr.REBASED and outcomes["push signing"][0] != fr.REBASED, outcomes
    assert outcomes["update refs"][0] == fr.REBASED and outcomes["update refs"][2] is False, outcomes
    assert all(got[4] is False for got in outcomes.values() if got[0] == fr.REBASED), "the default fetch rewrites the record"


def test_a_plain_fetch_rewrites_the_record_and_starts_maintenance(tmp_path, monkeypatch):
    """Green before the module exists: the default runner's fetch rewrites the fetch record and starts maintenance."""
    support.hermetic(tmp_path, monkeypatch)
    world = support.World(tmp_path)
    other = tmp_path / "other"
    support.git(tmp_path, "clone", "-q", str(world.remote), str(other))
    support.commit_file(other, "n.txt", "n\n", "upstream moves")
    support.git(other, "push", "-q", "origin", "main")
    record, trace = world.local / ".git" / "FETCH_HEAD", tmp_path / "trace.json"
    record.write_text("SENTINEL\n", encoding="utf-8")
    trace.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_TRACE2_EVENT", str(trace))
    support.real("feature_sync")._run(world.local, ["git", "fetch", "origin", "main"])
    assert record.read_text(encoding="utf-8") != "SENTINEL\n" and support.auto_children(trace) >= 1
