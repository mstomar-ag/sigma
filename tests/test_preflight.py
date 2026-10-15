"""#229: preflight -- git, remote, base, gh and scopes checked up front, with per-host remediation.

Hermetic: every git/gh call goes through an injected runner and `which`; no network. The two tests
that run real processes use real `git` in a tmp dir (no remote, so no network) and `sys.executable`
for the timeout tree-kill. Portable: nothing shells out to bash."""
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PREFLIGHT = ROOT / "skills" / "sigma-init" / "scripts" / "preflight.py"


def _load():
    spec = importlib.util.spec_from_file_location("preflight_under_test", PREFLIGHT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pf = _load()

WORK_ON = {"work": {"enabled": True}}
CLASSIC_OK = ("github.com\n  ✓ Logged in to github.com account alice (keyring)\n"
              "  - Active account: true\n  - Git operations protocol: https\n"
              "  - Token: gho_************************************\n"
              "  - Token scopes: 'gist', 'read:org', 'repo', 'workflow'\n")


def fake(answers, calls=None):
    """answers: list of (argv-prefix tuple, (rc, out)); first match wins; default (1, '')."""
    def runner(argv, cwd=None, timeout=None):
        if calls is not None:
            calls.append(list(argv))
        for prefix, result in answers:
            if tuple(argv[:len(prefix)]) == tuple(prefix):
                return result
        return 1, ""
    return runner


def healthy(auth=CLASSIC_OK, owner_type="User", remotes="origin\n",
            url="git@github.com:alice/app.git", ls_remote="abc\trefs/heads/main\n"):
    return [
        (("git", "rev-parse", "--is-inside-work-tree"), (0, "true\n")),
        (("git", "rev-parse", "--verify", "-q", "HEAD"), (0, "abc\n")),
        (("git", "rev-parse", "--abbrev-ref", "HEAD"), (0, "main\n")),
        (("git", "rev-parse", "--verify", "-q", "refs/heads/main"), (0, "abc\n")),
        (("git", "remote", "get-url"), (0, url + "\n")),
        (("git", "remote"), (0, remotes)),
        (("git", "ls-remote"), (0, ls_remote)),
        (("gh", "auth", "status"), (0, auth)),
        (("gh", "api"), (0, owner_type + "\n") if owner_type else (1, "HTTP 404")),
    ]


def has(names):
    return lambda n: f"/usr/bin/{n}" if n in names else None


def by_id(checks):
    return {c["id"]: c for c in checks}


# ------------------------------------------------------------------ parsing

@pytest.mark.parametrize("text,kind,scopes", [
    (CLASSIC_OK, "classic", {"gist", "read:org", "repo", "workflow"}),
    ("✓ Logged in to github.com as bob (oauth_token)\n✓ Token: ghp_****\n"
     "✓ Token scopes: gist, read:org, repo\n", "classic", {"gist", "read:org", "repo"}),
    ("✓ Logged in to github.com account c (GH_TOKEN)\n- Token: github_pat_11AB****\n"
     "- Token scopes: none\n", "fine-grained", None),
    ("✓ Logged in to github.com account c (GH_TOKEN)\n- Token: github_pat_11AB****\n",
     "fine-grained", None),
    ("✓ Logged in to github.com account app (GH_TOKEN)\n- Token: ghs_****\n", "app", None),
    ("Logged in. Token scopes: 'repo', 'workflow'", "unknown", {"repo", "workflow"}),
])
def test_parse_auth_status_shapes(text, kind, scopes):
    got = pf.parse_auth_status(text)
    assert got["token_kind"] == kind
    assert got["scopes"] == scopes


def test_parse_auth_status_reads_the_active_account_not_the_first():
    text = ("github.com\n  ✓ Logged in to github.com account old (keyring)\n"
            "  - Active account: false\n  - Token: gho_***\n  - Token scopes: 'repo'\n"
            "  ✓ Logged in to github.com account new (keyring)\n  - Active account: true\n"
            "  - Token: gho_***\n  - Token scopes: 'repo', 'workflow'\n")
    assert pf.parse_auth_status(text)["scopes"] == {"repo", "workflow"}


@pytest.mark.parametrize("url,expect", [
    ("git@github.com:Acme/app.git", ("github.com", "Acme", "app")),
    ("https://github.com/Acme/app.git", ("github.com", "Acme", "app")),
    ("https://token@github.example.com/Acme/app", ("github.example.com", "Acme", "app")),
    ("ssh://git@github.com:22/Acme/app.git", ("github.com", "Acme", "app")),
    ("/some/local/path.git", (None, None, None)),
])
def test_parse_remote_url(url, expect):
    assert pf.parse_remote_url(url) == expect


def test_admin_org_implies_read_org():
    assert pf.missing_scopes({"admin:org", "repo"}, ["repo", "read:org"]) == []
    assert pf.missing_scopes({"read:project"}, ["project"]) == ["project"]


def test_timeout_is_derived_from_the_existing_bound():
    assert pf.call_timeout({}) == 120
    assert pf.call_timeout({"SIGMA_WATCH_CALL_TIMEOUT": "30"}) == 30
    assert pf.call_timeout({"SIGMA_WATCH_CALL_TIMEOUT": "junk"}) == 120
    assert pf.call_timeout({"SIGMA_WATCH_CALL_TIMEOUT": "-1"}) == 120


# ------------------------------------------------------------------ the checks

def test_all_green():
    checks = pf.preflight("/r", WORK_ON, runner=fake(healthy()), which=has({"git", "gh"}))
    assert [c["ok"] for c in checks] == [True] * 6, checks
    assert pf.blocking(checks) == []


def test_a_not_a_git_repository():
    runner = fake([(("git", "rev-parse", "--is-inside-work-tree"),
                    (128, "fatal: not a git repository"))])
    c = by_id(pf.preflight("/r", WORK_ON, runner=runner, which=has({"git", "gh"})))
    assert c["git"]["ok"] is False
    assert "git init" in c["git"]["commands"]
    assert c["remote"]["note"] == "skipped" and c["base"]["note"] == "skipped"


def test_git_repo_without_a_commit():
    runner = fake([(("git", "rev-parse", "--is-inside-work-tree"), (0, "true\n"))])
    c = by_id(pf.preflight("/r", WORK_ON, runner=runner, which=has({"git", "gh"})))
    assert c["git"]["ok"] is False and "no commit" in c["git"]["detail"]
    assert c["remote"]["note"] != "skipped"       # review block #1: still a work tree


def test_gh_auth_line_mentions_pushing_still_works():
    answers = [(("gh", "auth", "status"), (1, "You are not logged into any GitHub hosts.")),
               (("gh", "api", "user"), (1, "HTTP 401"))] + healthy()
    c = by_id(pf.preflight("/r", WORK_ON, runner=fake(answers), which=has({"git", "gh"})))
    assert "pushing works without" in c["gh-auth"]["meanwhile"]


def test_b_no_remote_names_the_other_remotes():
    answers = healthy(remotes="upstream\n")
    c = by_id(pf.preflight("/r", WORK_ON, runner=fake(answers), which=has({"git", "gh"})))
    assert c["remote"]["ok"] is False
    assert "upstream" in c["remote"]["detail"]
    assert c["base"]["note"] == "skipped"


def test_c_gh_absent_never_says_gh_auth_login_as_the_fix():
    checks = pf.preflight("/r", WORK_ON, runner=fake(healthy()), which=has({"git"}))
    c = by_id(checks)
    assert c["gh-installed"]["ok"] is False
    assert c["gh-auth"]["note"] == "skipped"
    text = "\n".join(pf.failure_lines(c["gh-installed"]))
    assert "cli.github.com" in text
    # the fix for a missing gh is installing it; login is only named as the step AFTER install
    assert not any("gh auth login" in cmd for cmd in c["gh-installed"]["commands"])


def test_d_token_missing_workflow():
    auth = CLASSIC_OK.replace("'workflow'", "'user'")
    c = by_id(pf.preflight("/r", WORK_ON, runner=fake(healthy(auth=auth)), which=has({"git", "gh"})))
    assert c["scopes"]["ok"] is False
    assert c["scopes"]["commands"] == ["gh auth refresh -s workflow -h github.com"]
    assert ".github/workflows" in c["scopes"]["meanwhile"]


def test_org_owner_needs_read_org_and_mentions_sso():
    auth = CLASSIC_OK.replace("'read:org', ", "")
    c = by_id(pf.preflight("/r", WORK_ON, runner=fake(healthy(auth=auth, owner_type="Organization")),
                           which=has({"git", "gh"})))
    assert c["scopes"]["ok"] is False and "read:org" in c["scopes"]["detail"]
    assert "SSO" in c["scopes"]["note"]


def test_board_needs_project():
    cfg = {"work": {"enabled": True},
           "discovery": {"source": "github", "github": {"project": {"enabled": True}}}}
    c = by_id(pf.preflight("/r", cfg, runner=fake(healthy()), which=has({"git", "gh"})))
    assert c["scopes"]["ok"] is False
    assert "gh auth refresh -s project -h github.com" in c["scopes"]["commands"]


def test_fine_grained_token_is_cannot_verify_never_a_pass():
    auth = ("✓ Logged in to github.com account c (GH_TOKEN)\n- Active account: true\n"
            "- Token: github_pat_11AB****\n- Token scopes: none\n")
    checks = pf.preflight("/r", WORK_ON, runner=fake(healthy(auth=auth)), which=has({"git", "gh"}))
    c = by_id(checks)
    assert c["scopes"]["ok"] is None
    assert "cannot verify" in c["scopes"]["detail"]
    assert any("CANNOT VERIFY" in l for l in pf.report_lines(checks))


def test_classic_token_with_no_scopes_is_a_failure_not_unverifiable():
    auth = "✓ Logged in to github.com account c\n- Token: ghp_****\n- Token scopes: none\n"
    c = by_id(pf.preflight("/r", WORK_ON, runner=fake(healthy(auth=auth)), which=has({"git", "gh"})))
    assert c["scopes"]["ok"] is False


def test_unknown_owner_type_without_read_org_is_cannot_verify():
    auth = CLASSIC_OK.replace("'read:org', ", "")
    c = by_id(pf.preflight("/r", WORK_ON, runner=fake(healthy(auth=auth, owner_type=None)),
                           which=has({"git", "gh"})))
    assert c["scopes"]["ok"] is None


def test_base_not_pushed():
    c = by_id(pf.preflight("/r", WORK_ON, runner=fake(healthy(ls_remote="")),
                           which=has({"git", "gh"})))
    assert c["base"]["ok"] is False
    assert c["base"]["commands"] == ["git push -u origin main"]


def test_ls_remote_failure_is_cannot_verify():
    answers = [(("git", "ls-remote"), (124, "git ls-remote: timed out after 120s"))] + healthy()
    c = by_id(pf.preflight("/r", WORK_ON, runner=fake(answers), which=has({"git", "gh"})))
    assert c["base"]["ok"] is None


def test_gh_logged_out():
    answers = [(("gh", "auth", "status"), (1, "You are not logged into any GitHub hosts.")),
               (("gh", "api", "user"), (1, "HTTP 401"))] + healthy()
    c = by_id(pf.preflight("/r", WORK_ON, runner=fake(answers), which=has({"git", "gh"})))
    assert c["gh-auth"]["ok"] is False
    assert c["gh-auth"]["commands"][0].startswith("gh auth login")
    assert c["scopes"]["note"] == "skipped"


def test_gh_auth_status_targets_the_remotes_host():
    calls = []
    pf.preflight("/r", WORK_ON, runner=fake(healthy(url="git@ghe.acme.io:x/y.git"), calls),
                 which=has({"git", "gh"}))
    assert ["gh", "auth", "status", "--active", "--hostname", "ghe.acme.io"] in calls


def test_local_only_config_checks_git_alone():
    calls = []
    checks = pf.preflight("/r", {"work": {"enabled": False}}, runner=fake(healthy(), calls),
                          which=has({"git"}))
    assert [c["id"] for c in checks] == ["git"]
    assert not any(c[0] == "gh" for c in calls)


def test_every_failure_has_one_line_per_host_and_a_meanwhile():
    checks = pf.preflight("/r", WORK_ON, runner=fake(healthy(remotes="")), which=has({"git"}))
    for c in checks:
        # a check with no command to run (gh absent with no package manager on PATH: install from
        # the URL in its detail) prints no per-host line -- there is nothing to run anywhere
        if c["ok"] is False and c["commands"]:
            lines = pf.failure_lines(c)
            for host in pf.HOSTS:
                assert sum(l.strip().startswith(host + ":") for l in lines) == 1, (host, lines)
            assert any("Meanwhile:" in l for l in lines), lines


def test_repository_text_is_printed_escaped():
    answers = healthy(remotes="up\x1b[2Jstream\n")
    c = by_id(pf.preflight("/r", WORK_ON, runner=fake(answers), which=has({"git", "gh"})))
    assert "\x1b" not in c["remote"]["detail"] and "\\x1b" in c["remote"]["detail"]


# ------------------------------------------------------------------ work.py's message + gestures

def test_no_remote_message_is_none_when_remote_exists():
    assert pf.no_remote_message("/r", "origin", "/r/.sdlc", runner=fake(healthy())) is None


def test_no_remote_message_carries_the_decision_and_the_config_line():
    msg = pf.no_remote_message("/r", "origin", "/r/.sdlc", runner=fake([(("git", "remote"), (0, ""))]))
    assert "no remote named 'origin'" in msg
    assert '{"work": {"enabled": false}}' in msg
    assert "local-only" in msg
    for host in pf.HOSTS:
        assert host + ":" in msg


def _sdlc(tmp_path, cfg):
    d = tmp_path / ".sdlc"
    d.mkdir()
    (d / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    return d


def test_local_only_gesture_flips_only_work_enabled(tmp_path):
    d = _sdlc(tmp_path, {"work": {"enabled": True, "base": "main"}, "other": 1})
    assert pf.main(["preflight.py", "local-only", str(d)]) == 0
    cfg = json.loads((d / "config.json").read_text())
    assert cfg["work"]["enabled"] is False and cfg["work"]["base"] == "main" and cfg["other"] == 1
    assert "_enabled_why" in cfg["work"]


def test_local_only_refuses_a_symlinked_sdlc(tmp_path):
    real = _sdlc(tmp_path, {"work": {"enabled": True}})
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert pf.main(["preflight.py", "local-only", str(link)]) == 2
    assert json.loads((real / "config.json").read_text())["work"]["enabled"] is True


@pytest.mark.skipif(not shutil.which("git"), reason="git not installed")
def test_use_remote_only_accepts_an_existing_remote(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "upstream", "https://x/y/z.git"],
                   check=True)
    d = _sdlc(tmp_path, {"work": {"enabled": True}})
    assert pf.main(["preflight.py", "use-remote", str(d), "nope"]) == 2
    assert pf.main(["preflight.py", "use-remote", str(d), "upstream"]) == 0
    assert json.loads((d / "config.json").read_text())["work"]["remote"] == "upstream"


@pytest.mark.skipif(not shutil.which("git"), reason="git not installed")
def test_real_git_repo_without_remote(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=a@b", "-c", "user.name=a",
                    "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    c = by_id(pf.preflight(str(tmp_path), WORK_ON, which=lambda n: shutil.which(n) if n == "git" else None))
    assert c["git"]["ok"] is True
    assert c["remote"]["ok"] is False and "no remote at all" in c["remote"]["detail"]


def test_real_runner_kills_the_whole_tree_on_timeout(tmp_path):
    """A child that spawns a grandchild holding the pipe. The GRANDCHILD must be dead afterwards and
    the call must return well inside the post-kill drain grace: with only the direct child killed
    (`proc.kill()` in place of the group kill -- review block #2's mutation) the grandchild lives on
    and the drain waits out `_KILL_GRACE`, so both assertions go red. Elapsed alone could not tell:
    the bounded drain returns before any loose ceiling either way."""
    script = tmp_path / "spawn.py"
    pidfile = tmp_path / "grandchild.pid"
    script.write_text("import subprocess, sys, time\n"
                      "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                      f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
                      "time.sleep(30)\n", encoding="utf-8")
    start = time.monotonic()
    rc, out = pf.real_runner([sys.executable, str(script)], timeout=2)
    elapsed = time.monotonic() - start
    grandchild = int(pidfile.read_text())

    def alive(pid):
        if os.name == "nt":
            r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True)
            return str(pid) in r.stdout
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    try:
        deadline = time.monotonic() + 3              # reparented to init, which reaps it
        while alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert rc == 124 and "timed out" in out
        assert not alive(grandchild), "the grandchild survived: only the direct child was killed"
        assert elapsed < 2 + pf._KILL_GRACE, elapsed
    finally:
        if alive(grandchild):
            try:
                os.kill(grandchild, 9)
            except OSError:
                pass


def test_real_runner_reports_a_missing_binary():
    rc, out = pf.real_runner(["definitely-not-a-binary-229"], timeout=5)
    assert rc == 127 and "not found" in out


# ------------------------------------------------------------------ work.py start (#229)

def _work():
    spec = importlib.util.spec_from_file_location(
        "work_229", ROOT / "skills" / "sigma-loop" / "scripts" / "work.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _start_with(tmp_path, remotes):
    work = _work()
    cfg = {"work": {"enabled": True, "base": "main", "remote": "origin"},
           "discovery": {"source": "local-goals"}}
    sdlc = tmp_path / ".sdlc"
    (sdlc / "goals").mkdir(parents=True)
    (sdlc / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    calls = []

    def run(cwd, argv):
        calls.append(list(argv))
        if argv[:2] == ["git", "fetch"]:
            raise RuntimeError("git fetch origin main: fatal: 'origin' does not appear to be a git "
                               "repository")
        if argv == ["git", "remote"]:
            return remotes
        return ""
    with pytest.raises(RuntimeError) as err:
        work.start(str(sdlc), cfg, "0001", run=run)
    return str(err.value), calls


def test_work_start_without_origin_raises_the_preflight_message(tmp_path):
    msg, _ = _start_with(tmp_path, "")
    assert "does not appear to be a git repository" not in msg
    assert "no remote named 'origin'" in msg
    assert '{"work": {"enabled": false}}' in msg
    for host in pf.HOSTS:
        assert host + ":" in msg


def test_work_start_with_origin_keeps_gits_own_error(tmp_path):
    """The remote exists, so the fetch failed for another reason: the old message, unchanged."""
    msg, calls = _start_with(tmp_path, "origin")
    assert "does not appear to be a git repository" in msg and "came from" in msg
    assert ["git", "remote"] in calls


def test_a_board_counts_only_under_github_discovery():
    """The template ships `project.enabled: true` with `source: local-goals`; no board is mirrored
    there, so `project` is not a required scope."""
    local = {"work": {"enabled": True},
             "discovery": {"source": "local-goals", "github": {"project": {"enabled": True}}}}
    assert pf.requirements(local)["board"] is False
    c = by_id(pf.preflight("/r", local, runner=fake(healthy()), which=has({"git", "gh"})))
    assert c["scopes"]["ok"] is True


# ------------------------------------------------------------------ init + suite guards (#229)

INIT = ROOT / "skills" / "sigma-init" / "scripts" / "sdlc_init.py"


def test_init_refuses_a_non_git_directory_and_writes_nothing(tmp_path):
    """The documented gesture (`python3 sdlc_init.py`), run as a real process in a real non-git
    directory: refused, exit 2, and the directory is still empty."""
    target = tmp_path / "plain"
    target.mkdir()
    r = subprocess.run([sys.executable, str(INIT), str(target)], capture_output=True, text=True)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "REFUSED - nothing written" in r.stderr and "`git init`" in r.stderr
    assert list(target.iterdir()) == []


@pytest.mark.skipif(not shutil.which("git"), reason="git not installed")
def test_init_in_a_repo_without_remote_prints_the_decision(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=a@b", "-c", "user.name=a",
                    "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    r = subprocess.run([sys.executable, str(INIT), str(tmp_path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "[FAIL] git remote 'origin'" in r.stdout
    assert "DECISION: work.enabled is ON" in r.stdout
    assert json.loads((tmp_path / ".sdlc" / "config.json").read_text())["work"]["enabled"] is True


@pytest.mark.skipif(not shutil.which("git"), reason="git not installed")
def test_init_in_a_fresh_git_init_with_no_commit_prints_the_decision(tmp_path):
    """Review block #1: plan D4 calls a fresh `git init` the normal first run. The documented gesture
    (`sdlc_init.py .`) must still check the remote there and print the DECISION -- never skip it as
    'not a git repository'."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    r = subprocess.run([sys.executable, str(INIT), "."], cwd=str(tmp_path), capture_output=True,
                       text=True)
    assert r.returncode == 0, r.stderr
    assert "not a git repository" not in r.stdout
    assert "[FAIL] git remote 'origin'" in r.stdout
    assert "DECISION: work.enabled is ON" in r.stdout


def test_no_commit_still_checks_the_remote_and_says_no_commit_for_base():
    runner = fake([(("git", "rev-parse", "--is-inside-work-tree"), (0, "true\n")),
                   (("git", "remote", "get-url"), (0, "git@github.com:a/b.git\n")),
                   (("git", "remote"), (0, "origin\n"))])
    c = by_id(pf.preflight("/r", WORK_ON, runner=runner, which=has({"git", "gh"}), network=False))
    assert c["remote"]["ok"] is True
    assert c["base"]["note"] == "skipped" and "no commit yet" in c["base"]["detail"]
    runner = fake([(("git", "rev-parse", "--is-inside-work-tree"), (0, "true\n")),
                   (("git", "remote"), (0, ""))])
    c = by_id(pf.preflight("/r", WORK_ON, runner=runner, which=has({"git", "gh"})))
    assert c["remote"]["ok"] is False and c["remote"]["note"] == "no-remote"


# ------------------------------------------------------------------ review block #1 non-blockers

def test_network_calls_get_the_small_derived_bound():
    assert pf.network_timeout({}) == 15
    assert pf.network_timeout({"SIGMA_WATCH_CALL_TIMEOUT": "4"}) == 4
    seen = {}

    def runner(argv, cwd=None, timeout=None):
        seen[tuple(argv[:2])] = timeout
        return fake(healthy())(argv, cwd, timeout)
    pf.preflight("/r", WORK_ON, runner=runner, which=has({"git", "gh"}), timeout=120)
    assert seen[("git", "ls-remote")] <= 15 and seen[("gh", "auth")] <= 15 and seen[("gh", "api")] <= 15
    assert seen[("git", "remote")] == 120                       # local calls keep the fleet bound


def test_deep_false_never_runs_ls_remote_or_owner_lookup():
    calls = []
    checks = pf.preflight("/r", WORK_ON, runner=fake(healthy(), calls), which=has({"git", "gh"}),
                          deep=False)
    assert not any(a[:2] in (["git", "ls-remote"], ["gh", "api"]) for a in calls), calls
    c = by_id(checks)
    assert c["base"]["note"] == "skipped" and "/sigma-doctor" in c["base"]["detail"]
    assert c["base"]["ok"] is None


def test_gh_auth_status_asks_for_the_active_account_only():
    calls = []
    pf.preflight("/r", WORK_ON, runner=fake(healthy(), calls), which=has({"git", "gh"}))
    assert ["gh", "auth", "status", "--active", "--hostname", "github.com"] in calls


def test_gh_auth_older_gh_without_active_falls_back():
    answers = [(("gh", "auth", "status", "--active"), (1, "unknown flag: --active\n"))] + healthy()
    c = by_id(pf.preflight("/r", WORK_ON, runner=fake(answers), which=has({"git", "gh"})))
    assert c["gh-auth"]["ok"] is True


def test_a_stale_inactive_account_does_not_fail_a_valid_active_one():
    text = ("github.com\n  X Failed to log in to github.com account old (keyring)\n"
            "  - Active account: false\n  - The token in keyring is invalid.\n"
            "  ✓ Logged in to github.com account new (keyring)\n  - Active account: true\n"
            "  - Token: gho_***\n  - Token scopes: 'repo', 'workflow', 'read:org'\n")
    answers = [(("gh", "auth", "status", "--active"), (1, "unknown flag: --active")),
               (("gh", "auth", "status"), (1, text))] + healthy()
    c = by_id(pf.preflight("/r", WORK_ON, runner=fake(answers), which=has({"git", "gh"})))
    assert c["gh-auth"]["ok"] is True, c["gh-auth"]


def test_non_github_remote_gets_no_gh_login_advice():
    calls = []
    checks = pf.preflight("/r", WORK_ON, runner=fake(healthy(url="git@gitlab.com:a/b.git"), calls),
                          which=has({"git", "gh"}))
    c = by_id(checks)
    assert c["gh-auth"]["ok"] is False
    assert "GitHub" in c["gh-auth"]["detail"] and "gitlab.com" in c["gh-auth"]["detail"]
    assert not any("gh auth login" in x for x in c["gh-auth"]["commands"])
    assert "local-only" in c["gh-auth"]["meanwhile"]
    assert not any(a[:3] == ["gh", "auth", "status"] for a in calls)


def test_unparseable_remote_url_does_not_default_to_github_com():
    calls = []
    c = by_id(pf.preflight("/r", WORK_ON, runner=fake(healthy(url="/srv/git/app.git"), calls),
                           which=has({"git", "gh"})))
    assert not any("github.com" in x for a in calls for x in a), calls
    assert c["gh-auth"]["ok"] is None and "host" in c["gh-auth"]["detail"]


def test_url_userinfo_is_redacted_from_echoed_text():
    answers = [(("git", "ls-remote"), (128, "fatal: unable to access "
                                            "'https://bob:s3" "cret@github.com/a/b.git/': 403"))]
    answers += healthy(url="https://bob:s3" "cret@github.com/a/b.git")
    c = by_id(pf.preflight("/r", WORK_ON, runner=fake(answers), which=has({"git", "gh"})))
    assert "s3cret" not in c["remote"]["detail"] and "s3cret" not in c["base"]["detail"]
    assert "github.com" in c["base"]["detail"]


def test_sso_link_names_the_actual_host():
    auth = CLASSIC_OK.replace("github.com", "ghe.acme.io").replace("'read:org', ", "")
    c = by_id(pf.preflight("/r", WORK_ON, runner=fake(healthy(auth=auth, owner_type="Organization",
                                                              url="git@ghe.acme.io:x/y.git")),
                           which=has({"git", "gh"})))
    assert "https://ghe.acme.io/settings/tokens" in c["scopes"]["note"]
    assert "https://github.com/" not in c["scopes"]["note"]


def test_brew_is_suggested_only_when_brew_exists(monkeypatch):
    monkeypatch.setattr(pf.sys, "platform", "darwin")
    monkeypatch.setattr(pf.os, "name", "posix")
    no_brew = pf.check_gh_installed(has({"git"}))
    assert not any("brew" in x for x in no_brew["commands"])
    assert "cli.github.com" in no_brew["detail"]
    with_brew = pf.check_gh_installed(has({"git", "brew"}))
    assert with_brew["commands"] == ["brew install gh"]


def test_decision_does_not_claim_gh_is_needed_to_push():
    text = "\n".join(pf.decision_lines("/r/.sdlc", why="no-gh"))
    assert "pushed or opened" not in text
    assert "pushing works without" in text
    gh = pf.check_gh_installed(has({"git"}))
    assert "pushing works without" in gh["meanwhile"]


def test_kill_tree_on_windows_bounds_taskkill(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw)
        raise subprocess.TimeoutExpired(argv, kw.get("timeout"))

    class Proc:
        pid = 4242
        killed = False

        def kill(self):
            Proc.killed = True
    monkeypatch.setattr(pf.os, "name", "nt")
    monkeypatch.setattr(pf.subprocess, "run", fake_run)
    pf._kill_tree(Proc())
    assert seen.get("timeout") and Proc.killed


def test_real_runner_bounds_the_post_kill_drain(monkeypatch):
    class Stuck:
        pid = 4242
        returncode = None

        def __init__(self, *a, **kw):
            pass

        def communicate(self, timeout=None):
            if timeout is None:
                raise AssertionError("post-kill communicate() must be bounded")
            raise subprocess.TimeoutExpired("x", timeout)

        def kill(self):
            pass
    monkeypatch.setattr(pf.subprocess, "Popen", Stuck)
    monkeypatch.setattr(pf, "_kill_tree", lambda proc: None)
    rc, out = pf.real_runner([sys.executable, "-c", "pass"], timeout=0.01)
    assert rc == 124 and "timed out" in out


def test_the_suite_guards_popen_against_live_gh():
    """preflight's runner uses Popen, so conftest guards Popen too (#1495's seam, extended)."""
    with pytest.raises(RuntimeError, match="un-injected live `gh` call"):
        subprocess.Popen(["gh", "--version"])


def test_child_processes_get_an_offline_gh():
    assert os.environ.get("GH_CONFIG_DIR")
    assert not os.environ.get("GH_TOKEN") and not os.environ.get("GITHUB_TOKEN")


# ------------------------------------------------------------------ review block #2 (PR #249)

def _alias_repo(tmp_path, monkeypatch, ssh_body, url="git@github-work:o/r.git"):
    """A real repo whose origin is an ssh ALIAS, with a stub `ssh` first on PATH (its body is the
    script's behaviour) and the real git behind it. gh is never real: the returned runner sends git
    and ssh to the REAL runner (so the stub is what answers `ssh -G`) and gh to `gh_answers`."""
    real_git = shutil.which("git")
    subprocess.run([real_git, "-c", "init.defaultBranch=main", "init", "-q", str(tmp_path)], check=True)
    subprocess.run([real_git, "-C", str(tmp_path), "-c", "user.email=a@b", "-c", "user.name=a",
                    "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    subprocess.run([real_git, "-C", str(tmp_path), "remote", "add", "origin", url], check=True)
    stub = tmp_path / "bin"
    stub.mkdir()
    if ssh_body is not None:
        ssh = stub / "ssh"
        ssh.write_text(f"#!{sys.executable}\nimport sys\n{ssh_body}\n", encoding="utf-8")
        ssh.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(stub), os.path.dirname(real_git)]))
    calls = []

    def runner(argv, cwd=None, timeout=None):
        calls.append(list(argv))
        if argv[0] == "ssh" and ssh_body is None:     # no ssh at all (the real one may sit
            return 127, "ssh: not found on PATH"      # beside git, so PATH alone cannot hide it)
        if argv[0] in ("git", "ssh"):
            if argv[:2] == ["git", "ls-remote"]:
                return 0, "abc\trefs/heads/main\n"
            return pf.real_runner(argv, cwd, timeout)
        host = argv[argv.index("--hostname") + 1] if "--hostname" in argv else "github.com"
        if argv[:3] == ["gh", "auth", "status"]:
            if host == "github.com":
                return 0, CLASSIC_OK
            return 1, f"You are not logged into any accounts on {host}\n"
        return (0, "User\n") if argv[:2] == ["gh", "api"] else (1, "")
    return runner, calls


def _which_with_gh(name):
    """Real lookup, except gh: _alias_repo rebuilds PATH from a stub dir + git's dir, and gh need not
    sit beside git (Homebrew). Assumption: the fake "/fake/gh" is safe because gh is never executed
    here (the fake runner answers it); only the gh-installed check consults `which`. ssh/git still
    resolve for real, so the stub ssh is found."""
    return "/fake/gh" if name == "gh" else shutil.which(name)


_POSIX_GIT = pytest.mark.skipif(os.name == "nt" or not shutil.which("git"),
                                reason="POSIX stub ssh on PATH; needs git")


@_POSIX_GIT
def test_ssh_alias_remote_is_resolved_through_ssh_G(tmp_path, monkeypatch):
    """BLOCKING 1: `Host github-work` -> `HostName github.com`. gh is asked about github.com -- never
    about the alias, which gh cannot log in to."""
    runner, calls = _alias_repo(tmp_path, monkeypatch,
                                "if sys.argv[1:] == ['-G', 'github-work']:\n"
                                "    print('user git\\nhostname github.com\\nport 22')\n"
                                "else:\n    sys.exit(255)")
    checks = pf.preflight(str(tmp_path), WORK_ON, runner=runner, which=_which_with_gh)
    c = by_id(checks)
    assert ["ssh", "-G", "github-work"] in calls
    assert ["gh", "auth", "status", "--active", "--hostname", "github.com"] in calls
    assert not any("github-work" in x for a in calls if a[0] == "gh" for x in a), calls
    assert c["gh-auth"]["ok"] is True and "github-work" in c["gh-auth"]["detail"]
    assert c["scopes"]["ok"] is True and c["scopes"]["host"] == "github.com"
    assert pf.blocking(checks) == []


@_POSIX_GIT
@pytest.mark.parametrize("ssh_body", ["sys.exit(255)", None], ids=["ssh-G-fails", "no-ssh"])
def test_unresolvable_ssh_alias_is_cannot_verify_never_a_login(tmp_path, monkeypatch, ssh_body):
    """No resolution (ssh -G fails, or no ssh at all) and the host is not a hostname: CANNOT VERIFY,
    no command, no `gh auth login -h <alias>`, not blocking, and gh is never asked about the alias."""
    runner, calls = _alias_repo(tmp_path, monkeypatch, ssh_body)
    checks = pf.preflight(str(tmp_path), WORK_ON, runner=runner, which=_which_with_gh)
    c = by_id(checks)
    assert c["gh-auth"]["ok"] is None and "github-work" in c["gh-auth"]["detail"]
    assert c["gh-auth"]["commands"] == []
    assert not any(a[0] == "gh" for a in calls), calls
    text = "\n".join(pf.report_lines(checks))
    assert "CANNOT VERIFY" in text and "gh auth login" not in text
    assert "Sigma runs nothing that needs gh" not in text
    assert pf.blocking(checks) == []


@_POSIX_GIT
def test_ghe_fqdn_is_still_checked_as_itself(tmp_path, monkeypatch):
    runner, calls = _alias_repo(tmp_path, monkeypatch,
                                "print('hostname ghe.acme.io')", url="git@ghe.acme.io:x/y.git")
    pf.preflight(str(tmp_path), WORK_ON, runner=runner, which=_which_with_gh)
    assert ["gh", "auth", "status", "--active", "--hostname", "ghe.acme.io"] in calls


def test_ghe_fqdn_without_ssh_resolution_is_still_checked():
    calls = []
    pf.preflight("/r", WORK_ON, runner=fake(healthy(url="git@ghe.acme.io:x/y.git"), calls),
                 which=has({"git", "gh"}))
    assert ["ssh", "-G", "ghe.acme.io"] in calls                       # asked, failed (fake rc 1)
    assert ["gh", "auth", "status", "--active", "--hostname", "ghe.acme.io"] in calls


def test_ssh_over_443_hostname_reads_as_github_com():
    calls = []
    answers = [(("ssh", "-G"), (0, "hostname ssh.github.com\nport 443\n"))] + healthy(
        url="git@gh443:alice/app.git")
    pf.preflight("/r", WORK_ON, runner=fake(answers, calls), which=has({"git", "gh"}))
    assert ["gh", "auth", "status", "--active", "--hostname", "github.com"] in calls


def test_alias_resolving_to_gitlab_is_non_github():
    answers = [(("ssh", "-G"), (0, "hostname gitlab.com\n"))] + healthy(url="git@work:a/b.git")
    c = by_id(pf.preflight("/r", WORK_ON, runner=fake(answers), which=has({"git", "gh"})))
    assert c["gh-auth"]["note"] == "non-github" and "gitlab.com" in c["gh-auth"]["detail"]


@pytest.mark.parametrize("host,fqdn", [("github.com", True), ("ghe.acme.io", True),
                                       ("10.0.0.5", True), ("github-work", False),
                                       ("github.com-work", False), ("work", False)])
def test_looks_like_fqdn(host, fqdn):
    assert pf.looks_like_fqdn(host) is fqdn


def test_ghu_token_is_cannot_verify_not_no_scopes():
    auth = ("✓ Logged in to github.com account c (GH_TOKEN)\n- Active account: true\n"
            "- Token: ghu_****\n")
    c = by_id(pf.preflight("/r", WORK_ON, runner=fake(healthy(auth=auth)), which=has({"git", "gh"})))
    assert c["scopes"]["ok"] is None
    assert "GitHub App user token" in c["scopes"]["detail"]
    assert "no scopes)" not in c["scopes"]["detail"]


def test_gh_absent_decision_without_a_package_manager_names_the_url():
    text = "\n".join(pf.decision_lines("/r/.sdlc", why="no-gh", commands_printed=False))
    assert "commands above" not in text and "https://cli.github.com" in text
    assert "commands above" in "\n".join(pf.decision_lines("/r/.sdlc", why="no-gh"))
    assert "commands above" not in "\n".join(pf.decision_lines("/r/.sdlc", why="non-github"))


def test_init_report_gh_absent_without_brew_never_says_commands_above(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("sdlc_init_229b", INIT)
    init = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(init)
    monkeypatch.setattr(pf.sys, "platform", "linux")
    _sdlc(tmp_path, WORK_ON)
    lines = init.preflight_report(str(tmp_path), runner=fake(healthy()), which=has({"git"}))
    text = "\n".join(lines)
    assert "DECISION" in text and "commands above" not in text and "cli.github.com" in text


@pytest.mark.parametrize("url", ["https://bitbucket.acme.com/scm/o/r.git",
                                 "ssh://git@code.acme.com:7999/scm/o/r.git",
                                 "git@gitlab.acme.io:grp/sub/r.git",
                                 "https://acme.visualstudio.example/org/proj/_git/repo"])
def test_more_than_two_path_segments_is_the_non_github_decision(url):
    calls = []
    checks = pf.preflight("/r", WORK_ON, runner=fake(healthy(url=url), calls),
                          which=has({"git", "gh"}))
    c = by_id(checks)
    assert c["gh-auth"]["ok"] is False and c["gh-auth"]["note"] == "non-github", c["gh-auth"]
    assert "owner/repo" in c["gh-auth"]["detail"]
    assert not any(a[:3] == ["gh", "auth", "status"] for a in calls)


def test_init_report_prints_the_decision_for_a_bitbucket_server_remote(tmp_path):
    spec = importlib.util.spec_from_file_location("sdlc_init_229c", INIT)
    init = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(init)
    _sdlc(tmp_path, WORK_ON)
    lines = init.preflight_report(str(tmp_path), runner=fake(
        healthy(url="https://bitbucket.acme.com/scm/o/r.git")), which=has({"git", "gh"}))
    assert any("DECISION: work.enabled is ON" in l for l in lines), lines
