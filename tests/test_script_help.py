"""Every shipped script answers `--help` without side effects (#2736).

Every `skills/*/scripts/*.py` is run as a subprocess with `--help` and again with `-h`, from
inside an empty temp git repository, with a fresh temp HOME. The assertion is three-fold: exit
code 0, stdout contains "usage" (case-insensitive), and NOTHING was created or modified in the
repo or in HOME. The "usage" clause exists because several scripts used to pass exit-0/no-side-
effect while printing DATA (`predict.py` → `sonnet`, `north_star.py` → `absent <cwd>`,
`status.py` → a backlog line) — a help request that classifies the word `--help` is a false pass.

Two fixtures, because one cannot see the side effect adopted repositories get:
- `bare`: an empty git repo, nothing else.
- `sdlc`: the same repo with a pre-existing empty `.sdlc/` directory and `CLAUDE_PROJECT_DIR`
  unset. `timing_store.timed_main` records a timing line into any `.sdlc` it finds under the cwd
  AFTER `main` returns, so a guard inside `main` alone cannot stop it; this fixture is what
  catches that write.

Library-only modules — loaded by path, no `if __name__ == "__main__"` block — are allowlisted
in `LIBRARY_ONLY` with one shared reason and held only to exit 0 + no side effects (running a
library file as a script does nothing, and asking it to print usage would mean 30 cosmetic
`__main__` stubs). `test_library_allowlist_matches_ast_both_ways` keeps that allowlist honest:
every allowlisted file must have NO `__main__` block, every other script MUST have one, and a
name that no longer exists on disk fails. A CLI cannot exempt itself by joining the list.

Shell scripts (`skills/*/scripts/*.sh`) are GREP-covered, not executed: each must carry a
`-h|--help)` case arm. Their side effects are not measured here.

Scope: the issue names `skills/*/scripts/*.py`; `hooks/*.py` are OUTSIDE this test's scope and
are not checked — a visible gap, not an oversight.

Environment per run (Decision 5 of the plan): `PYTHONDONTWRITEBYTECODE=1` (Apple CLT python
otherwise writes pycs under `$HOME/Library/Caches`, making the HOME check always red),
`SIGMA_CLAUDE_CMD=true` (before #2736 `supervise_daemon.py --help` LAUNCHED `claude -p` with
`--help` as its sdlc_dir — a quota spend on a real HOME), `CLAUDE_PROJECT_DIR` removed,
`GH_TOKEN`/`GITHUB_TOKEN=invalid`, `stdin=DEVNULL`, a per-script timeout that records a failure
rather than raising. Runs go through a thread pool; the matrix is ~100 scripts x 2 flags x 2
fixtures.
"""
import ast
import concurrent.futures
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = sorted(ROOT.glob("skills/*/scripts/*.py"))
SHELL_SCRIPTS = sorted(ROOT.glob("skills/*/scripts/*.sh"))

FLAGS = ("--help", "-h")
FIXTURES = ("bare", "sdlc")
PER_SCRIPT_TIMEOUT = 30
MAX_WORKERS = 8

REASON = "library, loaded by path; no CLI"
LIBRARY_ONLY = {
    "feature_backup",
    "setup_wizard", "wizard_actions", "actionlog", "blocker_scan", "blockers", "breaker",
    "decompose_goal", "design_goal", "diff_revert", "feature_classify", "feature_doc", "legacy",
    "feature_judge", "feature_labels", "feature_registry", "feature_stamp", "feature_upkeep", "features",
    "flake_check", "frontmatter", "gh_api", "gh_session", "goal_size", "mutation",
    "scrub", "sources", "state", "tamper_scan", "timing_store", "watch_classify", "witness",
    "bounded_run",
    "feature_landed",
    "conflict_state",
    "conflict_proof",
    "unattended_git",
    "merge_queue", "tier_escalation", "board_spec", "red_green", "shell_policy", "logroll",
    "codex_runtime",
    "feature_upkeep_drift",
    "feature_upkeep_state",
    "feature_upkeep_resolution",
}


def _rel(path):
    return path.relative_to(ROOT).as_posix()


def _snapshot(root):
    """Every path under `root` (including `.git`) → (is_dir, mtime_ns, size). A changed mtime
    or size counts as a modification; a new key is a creation; a missing key is a deletion."""
    seen = {}
    root = pathlib.Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = pathlib.Path(dirpath) / name
            try:
                st = p.lstat()
            except OSError:
                continue
            seen[p.relative_to(root).as_posix()] = (p.is_dir(), st.st_mtime_ns, st.st_size)
    return seen


def _diff(before, after):
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
    return added, removed, changed


def _run(script, flag, fixture):
    """One matrix cell: fresh repo + HOME, run, snapshot-diff both. Never raises — a timeout
    or a crash is a recorded failure like any other."""
    base = pathlib.Path(tempfile.mkdtemp(prefix="help2736-"))
    repo = base / "repo"
    home = base / "home"
    repo.mkdir()
    home.mkdir()
    try:
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        if fixture == "sdlc":
            (repo / ".sdlc").mkdir()
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(home),
            "TMPDIR": str(home),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "SIGMA_CLAUDE_CMD": "true",
            "GH_TOKEN": "invalid",
            "GITHUB_TOKEN": "invalid",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        before_repo, before_home = _snapshot(repo), _snapshot(home)
        try:
            r = subprocess.run([sys.executable, str(script), flag], cwd=str(repo), env=env,
                               stdin=subprocess.DEVNULL, capture_output=True, text=True,
                               timeout=PER_SCRIPT_TIMEOUT)
            rc, out, err = r.returncode, r.stdout, r.stderr
        except subprocess.TimeoutExpired as exc:
            rc = f"TIMEOUT>{PER_SCRIPT_TIMEOUT}s"
            out = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            err = (exc.stderr or b"").decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        repo_diff = _diff(before_repo, _snapshot(repo))
        home_diff = _diff(before_home, _snapshot(home))
        return (script, flag, fixture, rc, out, err, repo_diff, home_diff)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _describe(result):
    script, flag, fixture, rc, out, err, repo_diff, home_diff = result
    first_out = (out.strip().splitlines() or [""])[0][:100]
    first_err = (err.strip().splitlines() or [""])[0][:100]
    parts = [f"{_rel(script)} {flag} [{fixture}]: rc={rc}", f"stdout={first_out!r}",
             f"stderr={first_err!r}"]
    for label, (added, removed, changed) in (("repo", repo_diff), ("HOME", home_diff)):
        if added:
            parts.append(f"{label} created: {', '.join(added)}")
        if removed:
            parts.append(f"{label} removed: {', '.join(removed)}")
        if changed:
            parts.append(f"{label} modified: {', '.join(changed)}")
    return " | ".join(parts)


def _is_failure(result):
    script, flag, fixture, rc, out, err, repo_diff, home_diff = result
    if rc != 0:
        return True
    if any(repo_diff) or any(home_diff):
        return True
    if script.stem not in LIBRARY_ONLY and "usage" not in out.lower():
        return True
    return False


def test_every_script_answers_help_without_side_effects():
    """`<script> --help` and `<script> -h`, in a bare repo and in one with an empty `.sdlc/`:
    rc 0, "usage" on stdout (CLIs only), nothing created or modified in the repo or HOME."""
    assert len(SCRIPTS) >= 90, f"expected the shipped script set, found {len(SCRIPTS)}"
    cells = [(s, f, x) for s in SCRIPTS for f in FLAGS for x in FIXTURES]
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        results = list(pool.map(lambda c: _run(*c), cells))
    failures = [_describe(r) for r in results if _is_failure(r)]
    assert not failures, f"{len(failures)} of {len(results)} help runs failed:\n" + "\n".join(failures)


def _has_main_block(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return any(isinstance(node, ast.If) and "__name__" in ast.unparse(node.test)
               for node in tree.body)


def test_library_allowlist_matches_ast_both_ways():
    """Allowlisted ⇒ no `__main__` block; not allowlisted ⇒ has one; every allowlisted name
    exists on disk. Reason for every entry: %s.""" % REASON
    assert len(SCRIPTS) >= 90
    on_disk = {p.stem for p in SCRIPTS}
    stale = sorted(LIBRARY_ONLY - on_disk)
    assert not stale, f"LIBRARY_ONLY names no script on disk: {stale}"
    problems = []
    for script in SCRIPTS:
        has_main = _has_main_block(script)
        if script.stem in LIBRARY_ONLY and has_main:
            problems.append(f"{_rel(script)} is allowlisted as '{REASON}' but has a __main__ block")
        if script.stem not in LIBRARY_ONLY and not has_main:
            problems.append(f"{_rel(script)} has no __main__ block and is not allowlisted")
    assert not problems, "\n".join(problems)


def test_shell_scripts_have_a_help_handler():
    """Grep-covered, not executed: each `.sh` carries a `-h|--help)` case arm."""
    assert SHELL_SCRIPTS, "no skills/*/scripts/*.sh found — glob or layout changed"
    missing = [_rel(p) for p in SHELL_SCRIPTS
               if not re.search(r"-h\|--help\)|--help\|-h\)", p.read_text(encoding="utf-8"))]
    assert not missing, "shell scripts without a -h|--help) handler: " + ", ".join(missing)


def test_loop_usage_names_precheck():
    """`loop.py precheck <dir> <goal>` is prescribed by sigma-loop/SKILL.md; its usage must
    name it."""
    result = _run(ROOT / "skills" / "sigma-loop" / "scripts" / "loop.py", "--help", "bare")
    assert not _is_failure(result), _describe(result)
    assert "precheck <dir> <goal>" in result[4], result[4]
