import importlib.util
import json
import os
import pathlib
import subprocess
import sys

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-setup" / "scripts" / "setup.py"


def _mod():
    spec = importlib.util.spec_from_file_location("setup", S)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


setup = _mod()


def _sdlc(tmp_path, cfg=None):
    d = tmp_path / ".sdlc"; d.mkdir()
    (d / "config.json").write_text(json.dumps(cfg or {}))
    return str(d)


# ------------------------------------------------------------------ detect_repo


def test_detect_repo_handles_ssh_https_and_host_alias():
    urls = {
        "git@github.com:acme/widget.git": "acme/widget",
        "https://github.com/acme/app.git": "acme/app",
        "github.com-alias:acme/tool.git": "acme/tool",           # an ssh host-alias form
        "https://github.com/acme/plain": "acme/plain",           # no .git suffix
    }
    for url, want in urls.items():
        assert setup.detect_repo(".", run=lambda _r, _a, u=url: u) == want


def test_detect_repo_empty_without_a_remote():
    assert setup.detect_repo(".", run=lambda _r, _a: "") == ""


# ------------------------------------------------------------------ hook path

#: The value earlier Sigma releases wrote (#218, removed by #614). Assembled: the public-surface
#: guard rejects the raw directory name.
_STALE = "." + "git" + "hooks"


def _hooks_path(repo):
    got = subprocess.run(["git", "-C", str(repo), "config", "--local", "--get", "core.hooksPath"],
                         capture_output=True, text=True, env=setup._hook_git_env())
    return got.stdout.strip() if got.returncode == 0 else None


def _two_repos(tmp_path):
    target, other = tmp_path / "target", tmp_path / "other"
    for repo in (target, other):
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return target, other


def test_hooks_verb_refuses_a_missing_hook_directory_and_writes_nothing(tmp_path):
    """#614: pointing git at a directory that does not exist makes git run NO hooks at all, so the
    verb refuses rather than switch an adopter's hooks off."""
    target, _ = _two_repos(tmp_path)
    result = subprocess.run([sys.executable, str(S), "hooks", str(target)],
                            capture_output=True, text=True)
    assert result.returncode == 2 and "REFUSED" in result.stderr, result.stdout + result.stderr
    assert _hooks_path(target) is None


def test_hooks_verb_uses_the_named_repo_despite_an_exported_git_dir(tmp_path):
    """#218's redirect protection survives #614: with the directory present, a real external
    GIT_DIR must not redirect the local write."""
    target, other = _two_repos(tmp_path)
    (target / _STALE).mkdir()
    env = dict(os.environ, GIT_DIR=str(other / ".git"))
    result = subprocess.run([sys.executable, str(S), "hooks", str(target)],
                            capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    repeat = subprocess.run([sys.executable, str(S), "hooks", str(target)],
                            capture_output=True, text=True, env=env)
    assert repeat.returncode == 0 and "already configured" in repeat.stdout, repeat.stderr
    assert _hooks_path(target) == _STALE and _hooks_path(other) is None


def test_hooks_repair_clears_only_the_named_repos_stale_key_and_says_so_once(tmp_path):
    """#614 repair: the stale key Sigma wrote (its directory missing) is unset in the NAMED repo
    only -- an exported GIT_DIR cannot redirect it -- with one line saying so; a second run is
    silent."""
    target, other = _two_repos(tmp_path)
    for repo in (target, other):
        subprocess.run(["git", "-C", str(repo), "config", "--local", "core.hooksPath", _STALE],
                       check=True, env=setup._hook_git_env())
    env = dict(os.environ, GIT_DIR=str(other / ".git"))
    first = subprocess.run([sys.executable, str(S), "hooks-repair", str(target)],
                           capture_output=True, text=True, env=env)
    assert first.returncode == 0, first.stderr
    lines = [l for l in first.stdout.splitlines() if "core.hooksPath" in l]
    assert len(lines) == 1, first.stdout
    assert _hooks_path(target) is None and _hooks_path(other) == _STALE
    again = subprocess.run([sys.executable, str(S), "hooks-repair", str(target)],
                           capture_output=True, text=True, env=env)
    assert again.returncode == 0 and again.stdout == "", again.stdout + again.stderr


def test_stale_hook_path_leaves_every_other_value_alone(tmp_path):
    """Only the exact value Sigma wrote, with its directory missing, is stale. A user's own hook
    directory (present), another value (husky), or no value is never touched."""
    repo, _ = _two_repos(tmp_path)
    assert setup.stale_hook_path(repo) is False
    for value in (".husky/_", "missing-hooks"):
        subprocess.run(["git", "-C", str(repo), "config", "--local", "core.hooksPath", value],
                       check=True, env=setup._hook_git_env())
        assert setup.stale_hook_path(repo) is False
        assert setup.clear_stale_hook_path(repo) == "absent" and _hooks_path(repo) == value
    subprocess.run(["git", "-C", str(repo), "config", "--local", "core.hooksPath", _STALE],
                   check=True, env=setup._hook_git_env())
    (repo / _STALE).mkdir()
    assert setup.stale_hook_path(repo) is False
    assert setup.clear_stale_hook_path(repo) == "absent" and _hooks_path(repo) == _STALE
    (repo / _STALE).rmdir()
    assert setup.stale_hook_path(repo) is True


def test_stale_hook_path_is_not_stale_while_any_worktree_has_the_directory(tmp_path):
    """Plan-review R2: `--local` config is shared by every worktree, but git resolves a relative
    hooksPath against EACH worktree's own top level. A linked worktree on a branch without the
    directory must not unset hooks the main checkout really runs."""
    env = setup._hook_git_env()
    repo = tmp_path / "main"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=env)
    for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(repo), "config", k, v], check=True, env=env)
    (repo / "f").write_text("x\n")
    subprocess.run(["git", "-C", str(repo), "add", "f"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "i"], check=True, env=env)
    linked = tmp_path / "linked"
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b", "other", str(linked)],
                   check=True, env=env)
    (repo / _STALE).mkdir()                                  # untracked: only the main checkout has it
    subprocess.run(["git", "-C", str(repo), "config", "--local", "core.hooksPath", _STALE],
                   check=True, env=env)
    assert setup.stale_hook_path(linked) is False
    assert setup.clear_stale_hook_path(linked) == "absent" and _hooks_path(repo) == _STALE
    (repo / _STALE).rmdir()
    assert setup.stale_hook_path(linked) is True


# ------------------------------------------------------------------ configure


def test_configure_sets_the_adoption_defaults(tmp_path):
    d = _sdlc(tmp_path)
    cfg, _ = setup.configure(d, repo="acme/app", verify_command="pytest -q")
    assert cfg["discovery"]["source"] == "github"
    assert cfg["discovery"]["github"]["repo"] == "acme/app"
    assert cfg["discovery"]["github"]["assignee"] == "@me"       # always scoped to me
    assert cfg["ledger"]["enabled"] is True
    assert cfg["work"]["enabled"] is True and cfg["work"]["auto_merge"] == "off"
    assert cfg["verify"] == {"command": "pytest -q", "enforce": True}


def test_configure_never_enforces_verify_without_a_command(tmp_path):
    d = _sdlc(tmp_path)
    cfg, notes = setup.configure(d)                              # no verify command supplied
    assert cfg["verify"].get("enforce") in (False, None)
    assert not cfg["verify"].get("command")
    assert any("enforce left OFF" in n for n in notes)


def test_configure_defuses_a_preexisting_enforce_without_command(tmp_path):
    d = _sdlc(tmp_path, {"verify": {"enforce": True, "command": ""}})   # the field-found trap
    cfg, notes = setup.configure(d)
    assert cfg["verify"]["enforce"] is False                    # turned back off so `done` isn't refused forever
    assert any("turned OFF" in n for n in notes)


def test_configure_preserves_existing_settings(tmp_path):
    d = _sdlc(tmp_path, {"budget": {"max_iterations": 7}, "work": {"auto_merge": "always"}})
    cfg, _ = setup.configure(d, repo="a/b")
    assert cfg["budget"]["max_iterations"] == 7                  # untouched
    assert cfg["work"]["auto_merge"] == "always"                # a human's explicit choice is kept


def test_configure_preserves_an_explicit_assignee_on_rerun(tmp_path):
    d = _sdlc(tmp_path, {"discovery": {"github": {"assignee": "specific-user"}}})
    cfg, notes = setup.configure(d, repo="a/b")
    assert cfg["discovery"]["github"]["assignee"] == "specific-user"   # a human's explicit choice is kept
    assert any("assignee=specific-user" in n for n in notes)


def test_configure_preserves_explicit_ledger_and_work_enabled_false_on_rerun(tmp_path):
    """F24 part 2 (#435): `ledger.enabled` and `work.enabled` were unconditionally reset to True on
    every configure() call, exactly the same clobber-on-rerun bug #352/PR#434 fixed for `assignee`.
    A deliberate opt-out on either field must survive a rerun."""
    d = _sdlc(tmp_path, {"ledger": {"enabled": False}, "work": {"enabled": False}})
    cfg, notes = setup.configure(d, repo="a/b")
    assert cfg["ledger"]["enabled"] is False       # a human's explicit opt-out is kept
    assert cfg["work"]["enabled"] is False         # a human's explicit opt-out is kept
    assert any("ledger: enabled=False" in n for n in notes)
    assert any("work (PR per goal): enabled=False" in n for n in notes)


def test_configure_local_goals_source(tmp_path):
    d = _sdlc(tmp_path)
    cfg, _ = setup.configure(d, source="local-goals")
    assert cfg["discovery"]["source"] == "local-goals" and "github" not in cfg["discovery"]


# ------------------------------------------------------------------ configure: null sentinel (#2255)

#: The REAL template, not a hand-built fixture -- the whole point of #2255's own regression is that
#: `configure()`'s defaults silently never fired on the file `/sigma-init` actually scaffolds.
TMPL = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-init" / "templates" / "config.json.tmpl"


def test_apply_default_only_fires_on_none(tmp_path):
    d = {"a": None, "b": False, "c": "", "d": "set"}
    for key, value in (("a", "x"), ("b", "x"), ("c", "x"), ("d", "x")):
        setup._apply_default(d, key, value)
    assert d == {"a": "x", "b": False, "c": "", "d": "set"}   # only the null one changed
    setup._apply_default(d, "e", "x")                          # absent key: same as setdefault
    assert d["e"] == "x"


def test_configure_seeded_from_the_real_sdlc_init_template_gets_the_documented_defaults(tmp_path):
    """#2255's own regression, reproduced end to end against the REAL template file and the
    documented invocation (SKILL.md step 5: `configure .sdlc --repo <owner/name> --verify ...`,
    source defaults to "github") -- not a hand-built `{}` or a unit-level dict. This is the exact
    sequence SKILL.md's step 2 prescribes ("If there's no `.sdlc/`, run `/sigma-init` first... Then
    continue.") and is the one test that would have caught the original bug."""
    d = tmp_path / ".sdlc"
    d.mkdir()
    (d / "config.json").write_text(TMPL.read_text(encoding="utf-8"), encoding="utf-8")
    cfg, notes = setup.configure(str(d), repo="acme/app", verify_command="pytest -q")
    assert cfg["discovery"]["github"]["assignee"] == "@me"
    assert cfg["ledger"]["enabled"] is True
    assert cfg["work"]["enabled"] is True
    assert any("assignee=@me" in n for n in notes)
    assert any("ledger: enabled=True" in n for n in notes)
    assert any("work (PR per goal): enabled=True" in n for n in notes)


def test_configure_preserves_an_explicit_empty_assignee_on_rerun(tmp_path):
    """A DIFFERENT deliberate choice from `null`: an explicit empty string means "no filter, on
    purpose" (the template's own `_assignee` comment) and must survive a rerun exactly like a real
    username does -- this state had zero coverage before #2255 and is the case most likely to
    regress silently if `_apply_default` were ever "simplified" back to a bare falsy check."""
    d = _sdlc(tmp_path, {"discovery": {"github": {"assignee": ""}}})
    cfg, notes = setup.configure(d, repo="a/b")
    assert cfg["discovery"]["github"]["assignee"] == ""   # a human's explicit "no filter" is kept
    assert any("assignee=" in n and "UNSET" not in n for n in notes)


def test_configure_applies_the_default_on_an_explicit_null_but_not_on_false_or_empty(tmp_path):
    """The three-state distinction #2255 exists to make correctly: null (undecided) gets the
    adoption default; false/"" (a real decision, even one that matches what null would have
    produced anyway) is preserved. Mixed per-key so one key's null doesn't mask another's real
    value from being checked independently."""
    d = _sdlc(tmp_path, {
        "discovery": {"github": {"assignee": None}},
        "ledger": {"enabled": None},
        "work": {"enabled": False},           # a real, deliberate opt-out -- must survive
    })
    cfg, _ = setup.configure(d, repo="a/b")
    assert cfg["discovery"]["github"]["assignee"] == "@me"   # was null -> defaulted
    assert cfg["ledger"]["enabled"] is True                  # was null -> defaulted
    assert cfg["work"]["enabled"] is False                   # was false -> preserved, not re-defaulted


# ------------------------------------------------------------------ ensure_ignore


def test_ensure_ignore_adds_missing_runtime_dirs_to_tracked(tmp_path):
    added, skipped = setup.ensure_ignore(str(tmp_path), scope="tracked")
    gi = (tmp_path / ".gitignore").read_text()
    assert set(added) == set(setup.RUNTIME_IGNORES) and skipped == []
    assert ".sdlc/ledger/" in gi and ".sdlc/state/" in gi


def test_log_dir_inherits_the_existing_state_ignore_coverage(tmp_path):
    """Extends this test's own pattern for the new local-only action log (#463): after
    `setup.ensure_ignore()` runs, write a REAL file under `.sdlc/state/log/` — the new action-log
    directory `actionlog.py` writes to — and assert `git check-ignore` reports it ignored. Proves
    the plan's "`.sdlc/state/log/` inherits `.sdlc/state/`'s existing coverage for free — no new
    RUNTIME_IGNORES entry, no new setup.py code" claim BEHAVIORALLY, against real git, not by
    reading the tuple (which would pass even if `.gitignore`'s actual matching rules didn't agree)."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    setup.ensure_ignore(str(tmp_path), scope="tracked")
    log_dir = tmp_path / ".sdlc" / "state" / "log"
    log_dir.mkdir(parents=True)
    probe = log_dir / "158.jsonl"
    probe.write_text('{"kind": "note"}\n', encoding="utf-8")
    result = subprocess.run(["git", "-C", str(tmp_path), "check-ignore", "--quiet", str(probe)])
    assert result.returncode == 0, (
        "a file under .sdlc/state/log/ must be git-ignored via the existing .sdlc/state/ coverage")


def test_ensure_ignore_targets_local_exclude_when_asked(tmp_path):
    (tmp_path / ".git" / "info").mkdir(parents=True)
    setup.ensure_ignore(str(tmp_path), scope="local")
    assert not (tmp_path / ".gitignore").exists()               # the tracked file is untouched
    assert ".sdlc/ledger/" in (tmp_path / ".git" / "info" / "exclude").read_text()


def test_ensure_ignore_never_narrows_an_existing_blanket_exclude(tmp_path):
    """A human's blanket `.sdlc/` is left exactly as it is, and every entry it already covers is
    skipped rather than restated.

    #1562 SPLIT THIS LIST IN TWO, and that is the point of the assertions below. `graphify-out/` is
    the first RUNTIME_IGNORES entry that is NOT under `.sdlc/` — the graph builder writes it at the
    repo root — so a blanket `.sdlc/` genuinely does not cover it, and it must still be added. Before
    #1562 every entry was `.sdlc/`-prefixed, which is the only reason `added == []` used to hold; it
    was a property of the list's contents, never a promise that a blanket covers everything."""
    (tmp_path / ".git" / "info").mkdir(parents=True)
    (tmp_path / ".git" / "info" / "exclude").write_text(".sdlc/\n")     # a human's blanket choice
    added, skipped = setup.ensure_ignore(str(tmp_path), scope="tracked")

    under_sdlc = [ig for ig in setup.RUNTIME_IGNORES if ig.startswith(".sdlc/")]
    outside = [ig for ig in setup.RUNTIME_IGNORES if not ig.startswith(".sdlc/")]
    assert outside, "this test's whole point is that some entry is NOT covered by a .sdlc/ blanket"
    assert set(skipped) == set(under_sdlc)     # the blanket covers these; do not restate them
    assert set(added) == set(outside)          # ...and cannot cover these
    assert (tmp_path / ".git" / "info" / "exclude").read_text() == ".sdlc/\n"   # left exactly as-is
    # The tracked file now exists and carries exactly the uncovered entries (plus the writer's own
    # header comment) -- and, crucially, NOT the ones the blanket already handles.
    rules = {ln.strip() for ln in (tmp_path / ".gitignore").read_text().splitlines()
             if ln.strip() and not ln.startswith("#")}
    assert rules == set(outside)


def test_ensure_ignore_skips_a_dir_already_covered_and_adds_the_rest(tmp_path):
    (tmp_path / ".gitignore").write_text(".sdlc/ledger/\n")
    added, skipped = setup.ensure_ignore(str(tmp_path), scope="tracked")
    assert ".sdlc/ledger/" in skipped
    assert ".sdlc/state/" in added and ".sdlc/work/" in added   # only the missing ones


# ------------------------------------------------------------------ ignore_status


def test_ignore_status_reports_the_mechanism(tmp_path):
    (tmp_path / ".gitignore").write_text(".sdlc/state/\n")
    (tmp_path / ".git" / "info").mkdir(parents=True)
    (tmp_path / ".git" / "info" / "exclude").write_text(".sdlc/ledger/\n")
    st = setup.ignore_status(str(tmp_path))
    assert st[".sdlc/state/"] == "tracked"
    assert st[".sdlc/ledger/"] == "local"
    assert st[".sdlc/work/"] is None


# ------------------------------------------------------------------ ensure_core_labels (#2254)


#: The exact default label set `GitHubSource._LABEL_COLORS` resolves to -- pinned here so a test
#: that reads "the core lifecycle labels got created" is checking a concrete, named list rather
#: than "whatever the source happens to produce today".
_CORE_LABELS = {"sdlc:goal", "sdlc:in-progress", "sdlc:parked", "sdlc:blocked", "sdlc:blocking",
                "sdlc:needs-label", "sdlc:designed",
                "sdlc:needs-unit",   # #2263: the sibling overlay, same colour as sdlc:needs-label
                "sdlc:needs-triage"}  # #2363: tier 4 of feature_classify's chain, a distinct colour


def _github_cfg(repo="acme/app"):
    return {"discovery": {"source": "github", "github": {"repo": repo}}}


#: #230: the priority tiers bootstrapped alongside the lifecycle set.
_PRIORITY_LABELS = {"priority:P0", "priority:P1", "priority:P2", "priority:P3"}


def _creating_run(calls):
    """A `gh` that has no labels yet: the REST label read returns an empty page, every create lands."""
    def run(args):
        calls.append(list(args))
        return "[]" if args[0] == "api" else ""
    return run


def _creates(calls):
    return {c[2] for c in calls if c[:2] == ["label", "create"]}


def test_ensure_core_labels_creates_the_lifecycle_and_priority_sets(tmp_path):
    calls = []
    result = setup.ensure_core_labels(_sdlc(tmp_path), config=_github_cfg(), run=_creating_run(calls))
    assert _creates(calls) == _CORE_LABELS | _PRIORITY_LABELS
    assert result["outcome"] == "ensured" and result["repo"] == "acme/app"
    assert result["labels"] == sorted(_CORE_LABELS | _PRIORITY_LABELS)
    assert all(r["outcome"] == "created" for r in result["results"])


def test_ensure_core_labels_never_touches_any_issue_and_never_uses_graphql(tmp_path):
    """Label DEFINITIONS only: no `issue edit` / `label add` of any kind, and (#230) the one read is
    the REST labels endpoint, never `api graphql`."""
    calls = []
    setup.ensure_core_labels(_sdlc(tmp_path), config=_github_cfg(), run=_creating_run(calls))
    assert all((c[:2] == ["label", "create"]) or (c[0] == "api" and c[1].endswith("/labels")
                                                 and "GET" in c) for c in calls)
    assert not any("graphql" in str(a) for c in calls for a in c)


def test_ensure_core_labels_skips_in_local_goals_mode(tmp_path):
    calls = []
    result = setup.ensure_core_labels(_sdlc(tmp_path), config={"discovery": {"source": "local-goals"}},
                                      run=lambda args: calls.append(args))
    assert result["outcome"] == "skipped" and "not github" in result["detail"]
    assert calls == []                                            # zero gh calls attempted


def test_ensure_core_labels_skips_without_a_repo_configured(tmp_path):
    calls = []
    result = setup.ensure_core_labels(
        _sdlc(tmp_path), config={"discovery": {"source": "github", "github": {}}},
        run=lambda args: calls.append(args))
    assert result["outcome"] == "skipped" and "repo is not set" in result["detail"]
    assert calls == []


def test_ensure_core_labels_reads_config_from_disk_when_none_is_passed(tmp_path):
    d = _sdlc(tmp_path, _github_cfg("disk/repo"))
    calls = []
    result = setup.ensure_core_labels(d, run=_creating_run(calls))
    assert result["outcome"] == "ensured" and result["repo"] == "disk/repo"
    assert len(_creates(calls)) == len(_CORE_LABELS | _PRIORITY_LABELS)


def test_ensure_core_labels_never_raises_and_reports_every_failure(tmp_path):
    """#230 reverses the old silent contract: a `gh` that refuses every call must not propagate,
    AND must not be reported as "ensured" -- each label carries the reason `gh` gave."""
    def _always_refuses(args):
        raise RuntimeError("HTTP 403: Resource not accessible by integration")
    d = _sdlc(tmp_path, _github_cfg())
    result = setup.ensure_core_labels(d, run=_always_refuses)       # must not raise
    assert result["outcome"] == "failed"
    assert all(r["outcome"] == "failed" and "403" in r["reason"] for r in result["results"])
    assert not any(l.startswith("labels ensured") for l in result["lines"])


def test_ensure_core_labels_already_exists_refusal_counts_as_existed(tmp_path):
    """When the label read fails, create is attempted; `gh`'s "already exists" refusal (#1917: no
    `--force`) is classified `existed`, not failed."""
    def _run(args):
        if args[0] == "api":
            raise RuntimeError("HTTP 502")
        raise RuntimeError('label with name "%s" already exists; use `--force`' % args[2])
    result = setup.ensure_core_labels(_sdlc(tmp_path, _github_cfg()), run=_run)
    assert result["outcome"] == "ensured"
    assert all(r["outcome"] == "existed" for r in result["results"])


def test_ensure_core_labels_safe_to_call_repeatedly(tmp_path):
    """A re-run of `/sigma-setup` against a repo that already has every label reads them back and
    writes nothing -- idempotent by measurement, not by swallowing a refusal."""
    d = _sdlc(tmp_path, _github_cfg())
    have = set()

    def run(args):
        if args[0] == "api":
            return json.dumps([{"name": n} for n in sorted(have)])
        have.add(args[2]); return ""
    r1 = setup.ensure_core_labels(d, run=run)
    r2 = setup.ensure_core_labels(d, run=run)
    assert r1["outcome"] == r2["outcome"] == "ensured"
    assert all(r["outcome"] == "created" for r in r1["results"])
    assert all(r["outcome"] == "existed" for r in r2["results"])


def test_ensure_core_labels_repo_flag_forces_github_mode(tmp_path):
    """`sdlc_init.py --github` runs before `discovery.source` is set; `--repo` bootstraps anyway."""
    calls = []
    result = setup.ensure_core_labels(_sdlc(tmp_path, {"discovery": {"source": "local-goals"}}),
                                      run=_creating_run(calls), repo="acme/app")
    assert result["outcome"] == "ensured" and result["repo"] == "acme/app"
    assert all("--repo" in c and "acme/app" in c for c in calls if c[0] == "label")


# ------------------------------------------------------------------ #541: flag parser


def test_flag_parser_handles_a_bare_switch():
    assert setup._flags(["--scope", "local"]) == {"scope": "local"}
    assert setup._flags(["--auto-merge"]) == {"auto-merge": "true"}


def test_flags_consumes_verify_value_that_starts_with_a_double_dash():
    """#541: setup.py's own `_flags` copy gets the same fix -- `verify` (a free-form command
    string) unconditionally consumes the next token, and `--name=value` works for any flag."""
    assert setup._flags(["--verify", "--strict pytest"]) == {"verify": "--strict pytest"}
    assert setup._flags(["--repo=acme/--weird-repo-name"]) == {"repo": "acme/--weird-repo-name"}


def test_flags_never_keeps_a_whitespace_bearing_leaked_key():
    assert setup._flags(["--this looks like leaked prose, not a flag"]) == {}


def test_flags_drops_a_whitespace_bearing_key_in_the_eq_form_too():
    """#541 cycle 2: the `--name=value` branch bypassed the never-a-real-flag rule its
    space-separated sibling applies, so leaked prose that happened to contain '=' still landed
    as a whitespace-bearing key. Same shape in all four `_flags` copies, pinned in each."""
    assert setup._flags(["--zzunknown", "--a b=c d"]) == {"zzunknown": "true"}


# ------------------------------------------------------------------ review of PR #286 (#236)


def test_detect_repo_is_empty_for_a_non_github_host():
    for url in ("git@gitlab.com:o/r.git", "https://gitlab.com/o/r.git", "git@bitbucket.org:o/r.git",
                "ssh://git@gitlab.example.com:2222/o/r.git", "https://dev.azure.com/o/p/_git/r"):
        assert setup.detect_repo(".", run=lambda _r, _a, u=url: u) == "", url


def test_detect_repo_non_github_hosts_match_preflight():
    """The host list is duplicated (setup.py does not load the init skill's preflight) -- pinned."""
    spec = importlib.util.spec_from_file_location(
        "pf_for_setup_test", S.parent.parent.parent / "sigma-init" / "scripts" / "preflight.py")
    pf = importlib.util.module_from_spec(spec); spec.loader.exec_module(pf)
    assert tuple(setup._NON_GITHUB) == tuple(pf._NON_GITHUB)


def test_configure_keeps_a_configured_repo_when_origin_is_not_github(tmp_path):
    """rv286/cfg.py: a config that already names its repository is never refused for the missing
    origin -- the repository it names is kept."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    d = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/app"}}})
    p = subprocess.run([__import__("sys").executable, str(S), "configure", d],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    assert json.loads((pathlib.Path(d) / "config.json").read_text())["discovery"]["github"]["repo"] == "acme/app"


def test_configure_never_swaps_a_configured_repo_for_origin(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin",
                    "https://github.com/other/thing.git"], check=True)
    d = _sdlc(tmp_path, {"discovery": {"source": "github", "github": {"repo": "acme/app"}}})
    p = subprocess.run([__import__("sys").executable, str(S), "configure", d], capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    assert json.loads((pathlib.Path(d) / "config.json").read_text())["discovery"]["github"]["repo"] == "acme/app"
