import json, os, subprocess, sys, pathlib, importlib.util, tempfile, shutil

SCAFFOLDER = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-init" / "scripts" / "sdlc_init.py"
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _offline_runner(argv, cwd=None, timeout=None):
    """#229: real git (local, no remote in these fixtures), and a logged-out `gh` -- never the network."""
    if argv[0] == "gh":
        return 1, "You are not logged into any GitHub hosts."
    return _PF.real_runner(argv, cwd, timeout)


def _load():
    spec = importlib.util.spec_from_file_location("sdlc_init", SCAFFOLDER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # safe: __name__ != "__main__", so main() does not run
    mod.PREFLIGHT_RUNNER = _offline_runner
    return mod


_PF = _load()._preflight()


import contextlib


@contextlib.contextmanager
def _git_tmpdir():
    """#229: /sigma-init REFUSES a directory that is not a git repository, so every scaffold here
    runs in one (a fresh `git init`, no commit -- the normal first-run state, which init accepts)."""
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "init", "-q", tmp], check=True)
        yield tmp


def _load_setup():
    path = REPO_ROOT / "skills" / "sigma-setup" / "scripts" / "setup.py"
    spec = importlib.util.spec_from_file_location("setup", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_scaffold_gitignores_the_runtime_dirs_including_events(tmp_path):
    """#2626: a bare /sigma-init, on its own, must leave every RUNTIME_IGNORES target git-ignored --
    an init-only install is a real, supported install, not conditional on also running
    /sigma-setup. Runs the real CLI subprocess (the documented /sigma-init gesture), turns the
    journal on the documented way, crosses one real phase boundary via phase_report.py's own CLI
    (mirroring the issue's live repro byte for byte), then checks every RUNTIME_IGNORES target
    (read from setup.py's own live tuple, not hand-copied) via the real git-visibility gesture
    (git add -A / git status --porcelain, plus check-ignore) -- never a string check on
    .gitignore's contents. Non-vacuity (probe.exists() on disk) is asserted last and separately."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t.test"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    result = subprocess.run([sys.executable, str(SCAFFOLDER), str(tmp_path)],
                             capture_output=True, text=True)
    assert result.returncode == 0, (
        f"the documented /sigma-init CLI gesture must succeed on a healthy target\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}")

    cfg_path = tmp_path / ".sdlc" / "config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["journal"]["enabled"] = True
    cfg_path.write_text(json.dumps(cfg) + "\n")
    phase_report = REPO_ROOT / "skills" / "sigma-loop" / "scripts" / "phase_report.py"
    result = subprocess.run(
        [sys.executable, str(phase_report), "start", str(tmp_path / ".sdlc"), "2626", "research",
         "--model", "sonnet"],
        capture_output=True, text=True, cwd=str(tmp_path))
    assert result.returncode == 0, f"phase_report.py start failed: {result.stderr}"
    journal_files = list((tmp_path / ".sdlc" / "events").glob("*.jsonl"))
    assert journal_files, "the journal file must genuinely exist on disk before the git check"
    marker_files = list((tmp_path / ".sdlc" / "state" / "phase").glob("*.json"))
    assert marker_files, "phase_report.py start must also have written its own state marker"

    setup = _load_setup()
    probes = []
    for target in setup.RUNTIME_IGNORES:
        probe_dir = tmp_path / target
        probe_dir.mkdir(parents=True, exist_ok=True)
        probe = probe_dir / "probe.jsonl"
        probe.write_text('{"probe": true}\n', encoding="utf-8")
        probes.append((target, probe))

    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    porcelain = subprocess.run(
        ["git", "-C", str(tmp_path), "status", "--porcelain"],
        capture_output=True, text=True, check=True).stdout
    for target, probe in probes:
        assert target not in porcelain, (
            f"{target} must be git-ignored by a bare /sigma-init; git status showed:\n{porcelain}")
        check = subprocess.run(
            ["git", "-C", str(tmp_path), "check-ignore", "--quiet", str(probe)])
        assert check.returncode == 0, f"{probe} must be reported git-ignored by check-ignore"
    for target, probe in probes:                      # non-vacuity, checked last and separately
        assert probe.exists(), f"the probe under {target} must still exist on disk"


def _local_hooks_path(repo):
    got = subprocess.run(["git", "-C", str(repo), "config", "--local", "--get", "core.hooksPath"],
                         capture_output=True, text=True)
    return got.stdout.strip() if got.returncode == 0 else None


def test_documented_init_never_writes_a_hook_path(tmp_path):
    """#614 (inverts #218's pin): adoption never writes `core.hooksPath` -- the directory it named
    ships nowhere, so git ran NO hooks in an adopted repository."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    result = subprocess.run([sys.executable, str(SCAFFOLDER), str(tmp_path)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert _local_hooks_path(tmp_path) is None
    assert "core.hooksPath" not in result.stdout + result.stderr


def test_documented_init_adopts_and_keeps_a_different_existing_hook_path(tmp_path):
    """#614 (inverts #218's refusal): a user's hook directory (husky, an org scanner) is theirs --
    adoption completes and leaves the value exactly as it was."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    existing = "my-hooks"
    subprocess.run(["git", "-C", str(tmp_path), "config", "--local", "core.hooksPath", existing],
                   check=True)
    result = subprocess.run([sys.executable, str(SCAFFOLDER), str(tmp_path)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "REFUSED" not in result.stderr
    assert _local_hooks_path(tmp_path) == existing
    assert (tmp_path / ".sdlc" / "config.json").is_file()


def test_documented_init_unsets_the_stale_hook_path_and_says_so(tmp_path):
    """#614 AC-4 on the bare scaffolder: the key an earlier Sigma init wrote is removed, in one line."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "--local", "core.hooksPath",
                    "." + "git" + "hooks"], check=True)
    result = subprocess.run([sys.executable, str(SCAFFOLDER), str(tmp_path)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert _local_hooks_path(tmp_path) is None
    assert len([l for l in result.stdout.splitlines() if "core.hooksPath" in l]) == 1, result.stdout


def test_scaffold_gitignore_fix_does_not_make_probe_files_stop_existing(tmp_path):
    """Partner to the test above: the fix must make git stop SEEING each RUNTIME_IGNORES target's
    files, not make the files stop EXISTING. Tuple-derived and driven through the real CLI, same
    reasoning as Step 1.1."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    result = subprocess.run([sys.executable, str(SCAFFOLDER), str(tmp_path)],
                             capture_output=True, text=True)
    assert result.returncode == 0, f"a healthy /sigma-init must exit 0: {result.stderr}"
    setup = _load_setup()
    for target in setup.RUNTIME_IGNORES:
        probe_dir = tmp_path / target
        probe_dir.mkdir(parents=True, exist_ok=True)
        probe = probe_dir / "probe.jsonl"
        probe.write_text('{"kind": "probe"}\n', encoding="utf-8")
        result = subprocess.run(
            ["git", "-C", str(tmp_path), "check-ignore", "--quiet", str(probe)])
        assert result.returncode == 0, f"{target} must be git-ignored after a bare /sigma-init"
        assert probe.exists()   # the ignore rule must not have deleted or moved the file


def test_scaffold_cli_exits_nonzero_when_the_sibling_setup_script_is_missing(tmp_path):
    """Produces a REAL missing-script condition (never a monkeypatch): copy only skills/sigma-init/
    into an isolated tree with no sibling skills/sigma-setup/ beside it, so SETUP_SCRIPT's own
    unmodified relative-path resolution lands on a path that genuinely does not exist, then run the
    copied sdlc_init.py via the real CLI subprocess. Companion for the wizard path:
    test_run_scaffold_reports_ok_false_when_the_sibling_setup_script_is_missing in
    test_wizard_actions.py."""
    isolated_root = tmp_path / "isolated_skills" / "skills"
    shutil.copytree(REPO_ROOT / "skills" / "sigma-init", isolated_root / "sigma-init")
    isolated_sdlc_init = isolated_root / "sigma-init" / "scripts" / "sdlc_init.py"
    assert not (isolated_root / "sigma-setup").exists(), (
        "the isolation must be real: no sibling sigma-setup skill present")

    target = tmp_path / "cli_target"
    target.mkdir()
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    result = subprocess.run([sys.executable, str(isolated_sdlc_init), str(target)],
                             capture_output=True, text=True)
    assert result.returncode != 0, (
        f"a missing sibling setup.py must fail the CLI loudly, not print success and exit 0\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}")
    assert (target / ".sdlc" / "config.json").exists(), (
        "the already-written templates must survive an ignore-write failure")


def test_scaffold_cli_exits_nonzero_when_the_ignore_write_fails(tmp_path):
    """The portable failure trigger is a DIRECTORY named .gitignore, not a chmod'd file (chmod is
    ineffective as root, since permission bits aren't enforced for that UID). setup.py is
    UNMODIFIED by this goal (Rev 8): its existing ensure_ignore() already fails here without any
    new guard -- _read(dest) on a directory silently returns "" (it catches OSError), so the
    failure surfaces one line later, at dest.write_text(...): pathlib's own open() on a directory
    path raises IsADirectoryError, regardless of UID. Measured live against the actual unmodified
    setup.py (scratchpad p2626c/measure_unchanged_setup.py): the direct ensure_ignore() call, the
    real `setup.py ignore` CLI subprocess (exit 1, Python's default on an uncaught exception), and
    this exact _ignore_runtime_dirs() shape all confirmed the failure -- no setup.py change needed.
    Companion for the wizard path: test_run_scaffold_reports_ok_false_when_the_ignore_write_fails in
    test_wizard_actions.py."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").mkdir()
    result = subprocess.run([sys.executable, str(SCAFFOLDER), str(tmp_path)],
                             capture_output=True, text=True)
    assert result.returncode != 0, (
        f"a failed ignore-write must fail the CLI loudly, not print success and exit 0\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}")
    assert (tmp_path / ".sdlc" / "config.json").exists(), (
        "the already-written templates must survive an ignore-write failure")


def test_scaffold_failure_message_prints_no_runnable_command_with_the_target_path(tmp_path):
    """A failed ignore-write's recovery text must not hand the user a copy-paste shell command
    built from the target path (PR #2674 review): a directory named `foo;id`, passed safely as one
    argv element, would become `... ignore foo;id` -- a command that runs `id`. The message names
    the fix gesture in words (rerun this same /sigma-init) and quotes nothing runnable."""
    target = tmp_path / "foo;id"
    target.mkdir()
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    (target / ".gitignore").mkdir()
    result = subprocess.run([sys.executable, str(SCAFFOLDER), str(target)],
                             capture_output=True, text=True)
    assert result.returncode != 0
    assert "rerun this same /sigma-init" in result.stderr, result.stderr   # non-vacuity: the message printed
    assert "`python3" not in result.stderr and "by hand" not in result.stderr, result.stderr


def test_scaffold_creates_full_tree():
    mod = _load()
    with _git_tmpdir() as tmp:
        created, skipped = mod.scaffold(tmp)
        base = pathlib.Path(tmp) / ".sdlc"
        for rel in ["project.md", "config.json", "goals/README.md",
                    "goals/0001-example.md", "state/STATE.md", "state/review-queue.md"]:
            assert (base / rel).exists(), f"missing {rel}"
        assert skipped == []
        assert "config.json" in created


def test_config_is_valid_json_with_expected_keys():
    mod = _load()
    with _git_tmpdir() as tmp:
        mod.scaffold(tmp)
        cfg = json.loads((pathlib.Path(tmp) / ".sdlc" / "config.json").read_text())
        for key in ["mode", "discovery", "budget", "gates", "verify"]:
            assert key in cfg
        assert cfg["gates"]["on_block"] == "park"
        assert cfg["verify"]["command"] == ""


def test_project_name_substituted():
    mod = _load()
    with _git_tmpdir() as tmp:
        named = pathlib.Path(tmp) / "myrepo"; named.mkdir()
        mod.scaffold(str(named))
        assert "myrepo" in (named / ".sdlc" / "project.md").read_text()


def test_idempotent_skip_preserves_edits():
    mod = _load()
    with _git_tmpdir() as tmp:
        mod.scaffold(tmp)
        state = pathlib.Path(tmp) / ".sdlc" / "state" / "STATE.md"
        state.write_text("LIVE PROGRESS — do not clobber")
        created, skipped = mod.scaffold(tmp)            # second run
        assert created == []
        assert "state/STATE.md" in skipped
        assert state.read_text() == "LIVE PROGRESS — do not clobber"


def test_config_discovery_supports_local_and_github():
    mod = _load()
    with _git_tmpdir() as tmp:
        mod.scaffold(tmp)
        cfg = json.loads((pathlib.Path(tmp) / ".sdlc" / "config.json").read_text())
        assert cfg["discovery"]["source"] == "local-goals"      # default stays local (zero-dep)
        assert cfg["discovery"]["github"]["goal_label"]          # github knobs present for opt-in


def test_config_knowledge_graph_off_by_default():
    mod = _load()
    with _git_tmpdir() as tmp:
        mod.scaffold(tmp)
        kg = json.loads((pathlib.Path(tmp) / ".sdlc" / "config.json").read_text())["knowledge_graph"]
        assert kg["enabled"] is False                # opt-in only
        assert kg["scope"] == "full" and kg["builder"] == "graphify"


def test_scaffold_github_creates_pm_files():
    mod = _load()
    with _git_tmpdir() as tmp:
        created, skipped = mod.scaffold_github(tmp)
        gh = pathlib.Path(tmp) / ".github"
        for rel in ["ISSUE_TEMPLATE/epic.md", "ISSUE_TEMPLATE/task.md", "ISSUE_TEMPLATE/bug.md",
                    "ISSUE_TEMPLATE/config.yml", "workflows/add-to-project.yml",
                    "CRITICAL_INSIGHT_TEMPLATE.md", "LABELS.md"]:
            assert (gh / rel).exists(), f"missing {rel}"
        assert skipped == []


def test_scaffold_github_idempotent_preserves_edits():
    mod = _load()
    with _git_tmpdir() as tmp:
        mod.scaffold_github(tmp)
        epic = pathlib.Path(tmp) / ".github" / "ISSUE_TEMPLATE" / "epic.md"
        epic.write_text("MY EDITS")
        created, skipped = mod.scaffold_github(tmp)
        assert created == [] and "ISSUE_TEMPLATE/epic.md" in skipped
        assert epic.read_text() == "MY EDITS"          # never clobbered


def test_issue_templates_wellformed():
    mod = _load()
    with _git_tmpdir() as tmp:
        mod.scaffold_github(tmp)
        it = pathlib.Path(tmp) / ".github" / "ISSUE_TEMPLATE"
        epic = (it / "epic.md").read_text()
        assert epic.startswith("---") and "labels:" in epic and "Tasks" in epic and "Definition of done" in epic
        assert "Parent epic" in (it / "task.md").read_text()


def test_add_to_project_workflow_self_activates():
    mod = _load()
    with _git_tmpdir() as tmp:
        mod.scaffold_github(tmp)
        wf = (pathlib.Path(tmp) / ".github" / "workflows" / "add-to-project.yml").read_text()
        assert "add-to-project" in wf and "issues:" in wf
        assert "SDLC_PROJECT_URL" in wf and "ADD_TO_PROJECT_PAT" in wf   # guarded by repo var + secret


def test_add_to_project_workflow_comment_says_optional_not_required():
    # #1561: the header comment used to describe this PAT/secret setup as a required activation
    # step. `_sync_backlog` already boards every sdlc:goal-labelled issue for free, with no PAT --
    # the comment must say so, while the workflow's own activation guard stays unchanged (the
    # assertion above still holds: SDLC_PROJECT_URL/ADD_TO_PROJECT_PAT are still there, gating it).
    mod = _load()
    with _git_tmpdir() as tmp:
        mod.scaffold_github(tmp)
        wf = (pathlib.Path(tmp) / ".github" / "workflows" / "add-to-project.yml").read_text()
        assert "_sync_backlog" in wf or "already" in wf or "for free" in wf


def test_main_github_flag_scaffolds_dotgithub_and_sdlc():
    mod = _load()
    with _git_tmpdir() as tmp:
        assert mod.main(["sdlc_init.py", tmp, "--github"]) == 0
        assert (pathlib.Path(tmp) / ".github" / "workflows" / "add-to-project.yml").exists()
        assert (pathlib.Path(tmp) / ".sdlc" / "config.json").exists()   # still scaffolds .sdlc too


def test_main_github_flag_prints_no_pat_instruction(capsys):
    # #1561: sdlc_init.py --github used to print an instruction to create ADD_TO_PROJECT_PAT and
    # SDLC_PROJECT_URL to "enable auto-add of new issues to the Backlog" -- redundant, since
    # _sync_backlog already boards every sdlc:goal-labelled issue for free, with no PAT needed.
    # Nothing should tell the user to go set this up.
    mod = _load()
    with _git_tmpdir() as tmp:
        assert mod.main(["sdlc_init.py", tmp, "--github"]) == 0
        out = capsys.readouterr().out
        assert "ADD_TO_PROJECT_PAT" not in out
        assert "SDLC_PROJECT_URL" not in out
        assert "enable auto-add" not in out


def test_main_without_github_flag_skips_dotgithub():
    mod = _load()
    with _git_tmpdir() as tmp:
        mod.main(["sdlc_init.py", tmp])
        assert not (pathlib.Path(tmp) / ".github").exists()             # opt-in only


def test_readme_does_not_instruct_pat_setup_to_enable_auto_add():
    # #1561: README.md's "Sprint / PM scaffolding" paragraph used to end "Enable auto-add by
    # setting the repo variable SDLC_PROJECT_URL and an ADD_TO_PROJECT_PAT secret." -- the same
    # redundant instruction removed from sdlc_init.py's print and corrected in the workflow
    # template's comment. All three user-facing surfaces must agree.
    readme = (pathlib.Path(__file__).resolve().parent.parent / "README.md").read_text()
    assert "Enable auto-add by setting" not in readme


def test_demo_flag_queues_runnable_goal():
    mod = _load()
    with _git_tmpdir() as tmp:
        assert mod.main(["sdlc_init.py", tmp, "--demo"]) == 0
        demo = pathlib.Path(tmp) / ".sdlc" / "goals" / "0000-demo.md"
        assert demo.exists()
        t = demo.read_text()
        assert "status: pending" in t and "done_when" in t and "sigma-demo.md" in t


def test_no_demo_flag_no_demo_goal():
    mod = _load()
    with _git_tmpdir() as tmp:
        mod.main(["sdlc_init.py", tmp])
        assert not (pathlib.Path(tmp) / ".sdlc" / "goals" / "0000-demo.md").exists()   # opt-in only


def test_scaffold_demo_idempotent_preserves_edits():
    mod = _load()
    with _git_tmpdir() as tmp:
        assert mod.scaffold_demo(tmp) is True
        d = pathlib.Path(tmp) / ".sdlc" / "goals" / "0000-demo.md"; d.write_text("EDITED")
        assert mod.scaffold_demo(tmp) is False and d.read_text() == "EDITED"


def test_scaffold_survives_non_utf8_locale():
    # templates + the demo goal contain non-ASCII (em-dashes); scaffolding must read/write them as
    # UTF-8, not the locale default, so it doesn't crash under a C/POSIX locale (bare CI/containers).
    with _git_tmpdir() as tmp:
        env = {**os.environ, "LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0"}
        r = subprocess.run([sys.executable, str(SCAFFOLDER), tmp, "--github", "--demo"],
                           capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stderr
        assert (pathlib.Path(tmp) / ".sdlc" / "goals" / "0000-demo.md").exists()
        assert (pathlib.Path(tmp) / ".github" / "ISSUE_TEMPLATE" / "epic.md").exists()


def test_vision_flag_scaffolds_north_star():
    mod = _load()
    with _git_tmpdir() as tmp:
        assert mod.main(["sdlc_init.py", tmp, "--vision"]) == 0
        ns = pathlib.Path(tmp) / ".sdlc" / "context" / "north-star.md"
        assert ns.exists()
        t = ns.read_text()
        for section in ("Vision", "Strategy", "Non-goals", "Design", "Architecture"):
            assert section in t, f"north-star missing {section}"


def test_no_vision_flag_stays_drop_in():
    mod = _load()
    with _git_tmpdir() as tmp:
        mod.main(["sdlc_init.py", tmp])
        assert not (pathlib.Path(tmp) / ".sdlc" / "context").exists()   # opt-in only; drop-in default


def test_scaffold_vision_idempotent_preserves_edits():
    mod = _load()
    with _git_tmpdir() as tmp:
        assert mod.scaffold_vision(tmp) is True
        ns = pathlib.Path(tmp) / ".sdlc" / "context" / "north-star.md"; ns.write_text("MY VISION")
        assert mod.scaffold_vision(tmp) is False and ns.read_text() == "MY VISION"


def test_cursor_flag_scaffolds_always_apply_rule():
    mod = _load()
    with _git_tmpdir() as tmp:
        assert mod.main(["sdlc_init.py", tmp, "--cursor"]) == 0
        rule = pathlib.Path(tmp) / ".cursor" / "rules" / "sdlc.mdc"
        assert rule.exists()
        t = rule.read_text()
        assert "alwaysApply: true" in t                       # Cursor's hook analog: always in context
        for phase in ("Goal", "Research", "Plan-Review", "Implement", "Review", "Retrospective"):
            assert phase in t, f"cursor rule missing {phase}"
        assert "irreversible" in t and "portable" in t.lower()
        # companions pinned off so the portable executors run without a failing companion probe
        cfg = json.loads((pathlib.Path(tmp) / ".sdlc" / "config.json").read_text())
        assert cfg["companions"] == "off"


def test_no_cursor_flag_stays_host_default():
    mod = _load()
    with _git_tmpdir() as tmp:
        mod.main(["sdlc_init.py", tmp])
        assert not (pathlib.Path(tmp) / ".cursor").exists()   # opt-in only
        cfg = json.loads((pathlib.Path(tmp) / ".sdlc" / "config.json").read_text())
        assert cfg["companions"] == "auto"                     # untouched default (Claude path)


def test_scaffold_cursor_idempotent_preserves_edits():
    mod = _load()
    with _git_tmpdir() as tmp:
        assert mod.scaffold_cursor(tmp) is True
        rule = pathlib.Path(tmp) / ".cursor" / "rules" / "sdlc.mdc"; rule.write_text("MY RULES")
        assert mod.scaffold_cursor(tmp) is False and rule.read_text() == "MY RULES"   # never clobbered


def test_codex_flag_scaffolds_standing_rules_without_changing_claude_defaults():
    mod = _load()
    with _git_tmpdir() as tmp:
        assert mod.main(["sdlc_init.py", tmp, "--codex"]) == 0
        rules = (pathlib.Path(tmp) / "AGENTS.md").read_text()
        assert "<!-- sigma:codex:start -->" in rules
        assert "<!-- sigma:codex:end -->" in rules
        assert "CLAUDE_SKILL_DIR" in rules
        assert "Goal" in rules and "Plan-Review" in rules and "Retrospective" in rules
        assert "host-model codex" in rules
        assert "<tier-or-off>" in rules
        assert "model_host_overrides.codex" in rules
        assert "tier-or-off" in rules
        assert _RULE in rules
        cfg = json.loads((pathlib.Path(tmp) / ".sdlc" / "config.json").read_text())
        assert cfg["companions"] == "auto"
        assert not (pathlib.Path(tmp) / ".cursor").exists()


def test_codex_scaffold_preserves_existing_agents_file_and_is_idempotent():
    mod = _load()
    with _git_tmpdir() as tmp:
        dest = pathlib.Path(tmp) / "AGENTS.md"
        dest.write_text("# Team rules\n\nKeep this rule.\n")
        assert mod.scaffold_codex(tmp) is True
        first = dest.read_text()
        assert first.startswith("# Team rules\n\nKeep this rule.\n")
        assert mod.scaffold_codex(tmp) is False
        assert dest.read_text() == first


def test_codex_scaffold_refreshes_only_its_managed_block():
    mod = _load()
    with _git_tmpdir() as tmp:
        dest = pathlib.Path(tmp) / "AGENTS.md"
        dest.write_text("# Team rules\n\n<!-- sigma:codex:start -->\nold adapter\n"
                        "<!-- sigma:codex:end -->\n\n## Team footer\nKeep this.\n")
        assert mod.scaffold_codex(tmp) is True
        updated = dest.read_text()
        assert updated.startswith("# Team rules\n\n")
        assert updated.endswith("\n## Team footer\nKeep this.\n")
        assert "old adapter" not in updated
        assert "Codex does not set `CLAUDE_SKILL_DIR`" in updated
        assert mod.scaffold_codex(tmp) is False


def test_codex_scaffold_repairs_an_incomplete_managed_block():
    mod = _load()
    with _git_tmpdir() as tmp:
        dest = pathlib.Path(tmp) / "AGENTS.md"
        dest.write_text("# Team rules\n\n<!-- sigma:codex:start -->\npartial")
        assert mod.scaffold_codex(tmp) is True
        text = dest.read_text()
        assert text.startswith("# Team rules\n")
        assert text.count("<!-- sigma:codex:start -->") == 1
        assert text.count("<!-- sigma:codex:end -->") == 1
        assert "partial" not in text


def test_codex_scaffold_refuses_reversed_managed_markers_without_changing_rules():
    mod = _load()
    with _git_tmpdir() as tmp:
        dest = pathlib.Path(tmp) / "AGENTS.md"
        original = "# Team rules\n<!-- sigma:codex:end -->\n<!-- sigma:codex:start -->\n"
        dest.write_text(original)
        try:
            mod.scaffold_codex(tmp)
        except ValueError:
            pass
        else:
            assert False, "expected malformed managed block refusal"
        assert dest.read_text() == original


def test_codex_scaffold_replacement_failure_keeps_existing_rules(monkeypatch):
    mod = _load()
    with tempfile.TemporaryDirectory() as tmp:
        dest = pathlib.Path(tmp) / "AGENTS.md"
        dest.write_text("# Team rules\n")
        monkeypatch.setattr(mod.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("failed")))
        try:
            mod.scaffold_codex(tmp)
        except OSError:
            pass
        else:
            assert False, "expected replace failure"
        assert dest.read_text() == "# Team rules\n"
        assert sorted(p.name for p in pathlib.Path(tmp).iterdir()) == ["AGENTS.md"]


def test_plain_scaffold_does_not_write_codex_rules():
    mod = _load()
    with _git_tmpdir() as tmp:
        assert mod.main(["sdlc_init.py", tmp]) == 0
        assert not (pathlib.Path(tmp) / "AGENTS.md").exists()


# ---------------------------------------------------------------- the no-direct-commits rule (#1481)
#
# The rule that makes rebasing a shared `feature/*` branch safe travels with the kit, so an adopter
# gets it without having to know it exists. It is GENERATED text -- `_CURSOR_RULE` is a string
# literal in the scaffolder and `project.md` is a template -- and generated output nothing asserts
# about drifts. These assert on what lands in the adopter's tree, never on the source that made it.
#
# `_RULE` IS the contract sentence, and that is a measured claim rather than a plausible one: the
# same bytes appear in `AGENTS.md`, in `docs/branching-model.md` §3 (quoted there, deliberately, not
# restated), in `feature_rebase.py`'s module docstring, and in both files generated below. An
# earlier version of this comment asserted exactly that about a DIFFERENT string -- "Nobody commits
# directly to a `feature/*` branch" -- which appeared in none of the three, and review caught it.
# One sentence, quoted everywhere, is what makes an anchor an anchor; four paraphrases would have
# meant these tests pinned only their own wording. `tests/test_docs.py::RULE` is the same constant
# for the same reason.
#
# The other two anchors are the honest limits, and they are pinned because an adopter who reads the
# rule as a guarantee is worse off than one who never read it: the check is SILENT when it does not
# run, and the obvious prevention (protecting `feature/*`) switches the check off.

_RULE = "Nobody commits directly to a feature branch. All work reaches it through `sdlc/*` goal"


def _generated(tmp, *flags):
    mod = _load()
    assert mod.main(["sdlc_init.py", tmp, *flags]) == 0
    return mod


def _assert_carries_the_rule(text):
    """What every adopter-facing copy must carry, whatever else it says."""
    assert _RULE in text                                      # the contract sentence, not a paraphrase
    assert "sdlc/<goal-id>" in text and "pull request" in text        # the route work takes instead
    assert "only on a pick" in text                           # when the check runs at all
    assert "silence is not evidence" in text                  # and that a skip says NOTHING
    assert "branch protection" in text                        # what could actually prevent a commit
    assert "grant upkeep's actor a bypass" in text            # ...and what protecting it costs


_ROUTE = ("To rebase, catch up or land a unit (a `feature/<name>` branch), use the `sigma-rebase` skill; "
          "do not run a raw rebase, force-push or merge by hand.")


def _flat(text):
    return " ".join(text.split())


def test_every_host_copy_carries_the_same_unit_routing_sentence():
    """#940: 'rebase/land unit X' routes to the request skill, identically in all three adopter-facing copies."""
    with _git_tmpdir() as tmp:
        _generated(tmp, "--cursor", "--codex")
        base = pathlib.Path(tmp)
        for rel in (".sdlc/project.md", ".cursor/rules/sdlc.mdc", "AGENTS.md"):
            text = (base / rel).read_text(encoding="utf-8")
            assert _ROUTE in _flat(text), rel


def test_scaffolded_project_md_carries_the_no_direct_commits_rule():
    with _git_tmpdir() as tmp:
        _generated(tmp)                                       # no flags: every adopter gets this file
        text = (pathlib.Path(tmp) / ".sdlc" / "project.md").read_text(encoding="utf-8")
        _assert_carries_the_rule(text)
        assert "cannot prevent" in text                       # detection is not prevention


def test_scaffolded_cursor_rule_carries_the_no_direct_commits_rule():
    with _git_tmpdir() as tmp:
        _generated(tmp, "--cursor")
        text = (pathlib.Path(tmp) / ".cursor" / "rules" / "sdlc.mdc").read_text(encoding="utf-8")
        _assert_carries_the_rule(text)
        assert "cannot stop a direct commit" in text          # detection is not prevention


def test_the_scaffolder_source_is_ascii_so_a_string_literal_cannot_smuggle_one_in():
    """The guard is the read, not the run. `sdlc_init.py` is ASCII end to end -- `_CURSOR_RULE` uses
    `->` and ` -- ` rather than arrows and em-dashes -- and adding prose to a string literal is
    exactly how that stops being true. `test_scaffold_survives_non_utf8_locale` above already proves
    a C-locale scaffold works, so what this adds is the ASCII assertion on the source plus the
    `--cursor` branch, which that test does not exercise. Not the locale: the subprocess half here
    only fails in COMBINATION with a missing explicit encoding, so it is a second line rather than
    the point."""
    SCAFFOLDER.read_text(encoding="ascii")                    # raises if a non-ASCII byte crept in
    with _git_tmpdir() as tmp:
        env = {**os.environ, "LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0"}
        r = subprocess.run([sys.executable, str(SCAFFOLDER), tmp, "--cursor"],
                           capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stderr
        assert _RULE in (pathlib.Path(tmp) / ".cursor" / "rules" / "sdlc.mdc").read_text(encoding="utf-8")


# ------------------------------------------------------- the output contract travels too (#2114)
#
# `--cursor` writes a SECOND always-apply rule, `output-contract.mdc`, that points status output at
# `render.py` instead of at prose to imitate (design 2030 PC-13: before #2114 the adapter carried
# the contract neither as a document nor as a rule). Same discipline as the block above: asserted
# on what lands in the adopter's tree. The one addition is deliberate -- this repo's OWN
# `.cursor/rules/output-contract.mdc` is pinned byte for byte to what the scaffolder writes, so the
# hand-maintained copy that reached no adopter stops existing as a separate thing.

_OUTPUT_RULE_REL = pathlib.Path(".cursor") / "rules" / "output-contract.mdc"


def test_cursor_flag_also_scaffolds_the_output_contract_rule(capsys):
    with _git_tmpdir() as tmp:
        _generated(tmp, "--cursor")
        assert "`.cursor/rules/output-contract.mdc`" in capsys.readouterr().out   # the message names it
        rule = pathlib.Path(tmp) / _OUTPUT_RULE_REL
        assert rule.exists()
        t = rule.read_text(encoding="utf-8")
        assert "alwaysApply: true" in t                                   # always in context
        assert "skills/sigma-loop/scripts/render.py" in t                  # the command, not the prose
        assert "status|event|decision" in t
        assert "docs/output-contract.md" in t                             # ...and the specification
        assert "log.py slots .sdlc" in t and "phase_report.py end" in t   # the two existing callers
        assert "REFUSED [<code>] at <path>" in t and "exit 2" in t        # the refusal, as observed
        assert "follow it exactly" not in t                               # the imitation model is gone
        assert "not guaranteed" in t                                      # the D-5 relay limit, stated
        assert _RULE not in t                                             # one home for that rule (test_docs)
        t.encode("ascii")                                                 # no glyph, separator or arrow


#: The two always-loaded rule files an agent working THIS repo reads before its first status
#: emission: `AGENTS.md` (imported by `CLAUDE.md`) and the detail doc it links from its own
#: `## Output` section. The scaffolded `.mdc` above is asserted free of the imitation instruction;
#: these are the files a rebase actually brought it back into (post-PR review of #2598 found it at
#: `docs/agent-rules-detail.md:193` after main moved AGENTS.md's long Output section there).
_IMITATION_FREE_RULE_FILES = ("AGENTS.md", "docs/agent-rules-detail.md")


def test_this_repos_own_agent_rules_carry_no_imitation_instruction():
    """#2114 plan DoD item 7: `git grep -n 'follow it exactly' -- ':!tests'` -> 0 hits. The
    contract is the renderer's specification -- a block is CONSTRUCTED by `render.py`, not written
    to match a shape -- so no rule file may still send a model to "follow it exactly". Pinned on the
    two files that are in context on every turn rather than on the whole tree, because a tree-wide
    grep in a test would re-match its own plan and research notes under `.sdlc/`."""
    for rel in _IMITATION_FREE_RULE_FILES:
        path = REPO_ROOT / rel
        assert path.is_file(), f"{rel} must exist -- it is a standing rule file of this repo"
        t = path.read_text(encoding="utf-8")
        assert "follow it exactly" not in t, f"{rel} still carries the imitation instruction"
        assert "## Output" in t, f"{rel} lost its Output section; the absence above would be vacuous"


def test_scaffold_cursor_writes_each_rule_independently_and_clobbers_neither(capsys):
    mod = _load()
    with _git_tmpdir() as tmp:
        rules = pathlib.Path(tmp) / ".cursor" / "rules"; rules.mkdir(parents=True)
        (rules / "sdlc.mdc").write_text("MY RULES")
        assert mod.scaffold_cursor(tmp) is True                           # output-contract.mdc was missing
        assert (rules / "output-contract.mdc").exists()
        assert (rules / "sdlc.mdc").read_text() == "MY RULES"            # the existing one untouched
        (rules / "output-contract.mdc").write_text("MY OUTPUT RULES")
        assert mod.scaffold_cursor(tmp) is False                          # both present: nothing written
        assert (rules / "output-contract.mdc").read_text() == "MY OUTPUT RULES"
        assert mod.scaffold_cursor_rules(tmp) == ([], ["sdlc.mdc", "output-contract.mdc"])
        assert mod.main(["sdlc_init.py", tmp, "--cursor"]) == 0
        assert "Cursor rules already present (kept)" in capsys.readouterr().out
        (rules / "output-contract.mdc").unlink()
        assert mod.main(["sdlc_init.py", tmp, "--cursor"]) == 0
        out = capsys.readouterr().out
        assert "`.cursor/rules/output-contract.mdc`" in out and "= .cursor/rules/sdlc.mdc (exists, kept)" in out


def test_this_repos_own_cursor_output_rule_is_what_the_scaffolder_writes_byte_for_byte():
    """Single source (#2114). The repo's rule used to be hand-maintained and reached no adopter; now
    it IS the scaffolder's output, so `<sigma>` reads as `.` here and an edit to either side
    without the other goes red. Compared as bytes, so a stray em dash is a failure too. Line endings
    are normalised on both sides first: `write_text` emits `os.linesep`, so on a Windows checkout
    with `autocrlf=false` the generated file is CRLF against an LF repo file -- the platform, not a
    defect (Plan-Review finding 9)."""
    with _git_tmpdir() as tmp:
        _generated(tmp, "--cursor")
        generated = (pathlib.Path(tmp) / _OUTPUT_RULE_REL).read_bytes()
    ours = (REPO_ROOT / _OUTPUT_RULE_REL).read_bytes()
    assert ours.replace(b"\r\n", b"\n") == generated.replace(b"\r\n", b"\n")


def test_main_errors_on_missing_target():
    mod = _load()
    assert mod.main(["sdlc_init.py", "/no/such/dir/really"]) == 1
