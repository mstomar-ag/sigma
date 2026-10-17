"""issue #1540: EXECUTED tests for skills/sigma-loop/scripts/zones.py (`classify`).

Real git repos, a real origin remote and a real linked worktree: the fork-point logic is the whole
point, and a stub would let the classic bug through (newness taken against HEAD or the base tip).
Each guard here was broken once and seen red; the record is `.sdlc/evidence/1540-controls.md`.
"""
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "sigma-loop" / "scripts" / "zones.py"

LEGACY_EXISTING = "legacy: change only what the goal needs; extend by sprout or wrap at one seam"
NEW_RULE = "follow sound design: structure for the forces the goal names"
UNDECLARED_EXISTING = "follow the surrounding code"
UNKNOWN_RULE = "unknown: merge-base unresolved; apply no zone rule"
UNREACHABLE_RULE = "unreachable: config unreadable; apply no zone rule"


def _load():
    spec = importlib.util.spec_from_file_location("zones", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True).stdout


def _repo(path, files=("README.md",)):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    # No background git while a test snapshots the tree: `git commit` otherwise starts auto
    # maintenance, whose objects/maintenance.lock appears mid-test and reads as a write by zones.py
    # (seen in about one run in three under xdist).
    _git(path, "config", "maintenance.auto", "false")
    _git(path, "config", "gc.auto", "0")
    (path / ".gitignore").write_text(".sdlc/\n", encoding="utf-8")
    _git(path, "add", ".gitignore")
    for f in files:
        _commit(path, f, "base\n")
    return path


def _commit(repo, rel, text, msg=None):
    p = pathlib.Path(repo) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    _git(repo, "add", "--", rel)
    _git(repo, "commit", "-q", "-m", msg or "add " + rel)


def _config(repo, obj):
    d = pathlib.Path(repo) / ".sdlc"
    d.mkdir(exist_ok=True)
    (d / "config.json").write_text(json.dumps(obj), encoding="utf-8")


def _zones(*entries):
    return {"design": {"zones": [{"glob": g, "zone": z} for g, z in entries]}}


def _run(cwd, *args, extra=None):
    return subprocess.run([sys.executable, str(SCRIPT), "classify", *args], cwd=str(cwd),
                          capture_output=True, text=True, timeout=60)


def _rows(proc):
    return [ln.split("\t") for ln in proc.stdout.splitlines()]


def _with_origin(repo, tmp_path):
    """Add a bare origin and push main so `origin/main` exists."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "fetch", "-q", "origin")
    return bare


def _worktree(repo, goal="1540", branch=None, record=True, base="main"):
    """A real linked worktree under .sdlc/work/<goal> on a new branch, plus the work record."""
    branch = branch or "sdlc/%s" % goal
    at = pathlib.Path(repo) / ".sdlc" / "work" / goal
    _git(repo, "worktree", "add", "-q", "-b", branch, str(at), "HEAD")
    _git(at, "config", "user.email", "t@example.com")
    _git(at, "config", "user.name", "t")
    if record:
        d = pathlib.Path(repo) / ".sdlc" / "state" / "work"
        d.mkdir(parents=True, exist_ok=True)
        (d / ("%s.json" % goal)).write_text(json.dumps(
            {"worktree": str(at), "branch": branch, "base": base, "remote": "origin"}),
            encoding="utf-8")
    return at


# --- Task 1: CLI skeleton, normalisation, config read ------------------------------------------


def test_help_prints_usage():
    p = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True)
    assert p.returncode == 0 and "usage" in p.stdout.lower()
    p = subprocess.run([sys.executable, str(SCRIPT), "classify", "--help"], capture_output=True, text=True)
    assert p.returncode == 0 and "usage" in p.stdout.lower()


def test_ac2_absent_zones_prints_undeclared_for_every_path(tmp_path):
    repo = _repo(tmp_path / "p", ("a.py", "b/c.py"))
    _config(repo, {"mode": {"default": "goal"}})
    p = _run(repo, "a.py", "b/c.py", "zzz.py")
    assert p.returncode == 0, p.stderr
    rows = _rows(p)
    assert [r[0] for r in rows] == ["a.py", "b/c.py", "zzz.py"]
    assert all(r[1] == "undeclared" and len(r) == 4 for r in rows)


def test_design_present_without_zones_is_undeclared(tmp_path):
    repo = _repo(tmp_path / "p", ("a.py",))
    _config(repo, {"design": {}})
    p = _run(repo, "a.py")
    assert p.returncode == 0 and _rows(p)[0][1] == "undeclared"


def test_missing_config_is_unreachable_not_undeclared(tmp_path):
    repo = _repo(tmp_path / "p", ("a.py",))
    p = _run(repo, "a.py")
    assert p.returncode == 0, p.stderr
    r = _rows(p)[0]
    assert r[1] == "unreachable" and r[3] == UNREACHABLE_RULE


def test_unreadable_config_is_unreachable(tmp_path):
    repo = _repo(tmp_path / "p", ("a.py",))
    d = repo / ".sdlc"
    d.mkdir()
    (d / "config.json").write_text("{not json", encoding="utf-8")
    assert _rows(_run(repo, "a.py"))[0][1] == "unreachable"
    (d / "config.json").write_text("[1, 2]", encoding="utf-8")
    assert _rows(_run(repo, "a.py"))[0][1] == "unreachable"
    (d / "config.json").write_text("{}", encoding="utf-8")
    (d / "config.json").chmod(0)
    try:
        if os.geteuid() != 0:
            assert _rows(_run(repo, "a.py"))[0][1] == "unreachable"
    finally:
        (d / "config.json").chmod(0o644)


def test_path_normalisation_forms(tmp_path):
    repo = _repo(tmp_path / "p", ("a/b.py",))
    _config(repo, {})
    p = _run(repo, "./a/b.py", str(repo / "a" / "b.py"), "a//b.py", "a/./b.py")
    assert p.returncode == 0, p.stderr
    assert [r[0] for r in _rows(p)] == ["a/b.py"] * 4


def test_path_outside_repo_refused(tmp_path):
    repo = _repo(tmp_path / "p")
    _config(repo, {})
    outside = tmp_path / "elsewhere.py"
    outside.write_text("x")
    p = _run(repo, "README.md", str(outside))
    assert p.returncode == 2 and p.stdout == "" and "outside" in p.stderr


def test_run_from_subdirectory_joins_cwd(tmp_path):
    repo = _repo(tmp_path / "p", ("src/m.py",))
    _config(repo, _zones(("src/*", "legacy")))
    p = _run(repo / "src", "m.py", "../README.md")
    assert p.returncode == 0, p.stderr
    rows = _rows(p)
    assert rows[0][:2] == ["src/m.py", "legacy"] and rows[1][:2] == ["README.md", "undeclared"]


def test_symlinked_absolute_path_resolves(tmp_path):
    repo = _repo(tmp_path / "p", ("a.py",))
    _config(repo, {})
    link = tmp_path / "link"
    os.symlink(str(repo), str(link))
    p = _run(repo, str(link / "a.py"))
    assert p.returncode == 0, p.stderr
    assert _rows(p)[0][0] == "a.py"


def test_path_containing_colon(tmp_path):
    repo = _repo(tmp_path / "p", ("w:x.py",))
    _config(repo, {})
    p = _run(repo, "w:x.py")
    assert p.returncode == 0 and _rows(p)[0][0] == "w:x.py"


def test_dotdot_escape_refused_and_dotdot_back_inside_accepted(tmp_path):
    repo = _repo(tmp_path / "p", ("a.py",))
    _config(repo, {})
    bad = _run(repo, "../p2/a.py")
    assert bad.returncode == 2 and bad.stdout == ""
    ok = _run(repo, "../p/a.py")
    assert ok.returncode == 0 and _rows(ok)[0][0] == "a.py"


def test_newline_or_cr_in_path_refused(tmp_path):
    repo = _repo(tmp_path / "p")
    _config(repo, {})
    for bad in ("a\nb.py", "a\rb.py"):
        p = _run(repo, "README.md", bad)
        assert p.returncode == 2 and p.stdout == "" and "newline" in p.stderr.lower()


def test_leading_dash_path_needs_double_dash(tmp_path):
    repo = _repo(tmp_path / "p", ("-weird.py",))
    _config(repo, {})
    p = _run(repo, "--", "-weird.py")
    assert p.returncode == 0, p.stderr
    assert _rows(p)[0][0] == "-weird.py"


def test_duplicate_paths_print_one_row_each(tmp_path):
    repo = _repo(tmp_path / "p", ("a.py",))
    _config(repo, {})
    p = _run(repo, "a.py", "a.py", "./a.py")
    assert p.returncode == 0 and len(_rows(p)) == 3


# --- Task 2: zone matching ---------------------------------------------------------------------


def _zone_of(tmp_path, entries, path, files=None):
    repo = _repo(tmp_path / "z", files or (path,))
    _config(repo, _zones(*entries))
    p = _run(repo, path)
    assert p.returncode == 0, p.stderr
    return _rows(p)[0][1]


def test_ac1_last_match_wins(tmp_path):
    entries = [("src/*", "legacy"), ("src/new/*", "modern"), ("src/new/old_*", "legacy")]
    assert _zone_of(tmp_path / "1", entries, "src/new/x.py") == "modern"
    assert _zone_of(tmp_path / "2", entries, "src/new/old_x.py") == "legacy"
    assert _zone_of(tmp_path / "3", entries, "src/a.py") == "legacy"
    # reverse order of the same entries: the later (broader) one now wins
    rev = list(reversed(entries))
    assert _zone_of(tmp_path / "4", rev, "src/new/x.py") == "legacy"


def test_single_match_and_no_match_undeclared(tmp_path):
    assert _zone_of(tmp_path / "1", [("lib/*", "modern")], "lib/a.py") == "modern"
    assert _zone_of(tmp_path / "2", [("lib/*", "modern")], "other.py") == "undeclared"


def test_star_crosses_slash(tmp_path):
    assert _zone_of(tmp_path, [("src/*", "legacy")], "src/a/b/c.py") == "legacy"


def test_match_is_case_sensitive(tmp_path):
    assert _zone_of(tmp_path, [("SRC/*", "legacy")], "src/a.py") == "undeclared"


_BAD = [
    ("not-a-list", {"design": {"zones": "src/*"}}, "design.zones"),
    ("entry-not-dict", {"design": {"zones": ["src/*"]}}, "design.zones[0]"),
    ("empty-glob", {"design": {"zones": [{"glob": "a/*", "zone": "legacy"}, {"glob": "", "zone": "legacy"}]}}, "design.zones[1]"),
    ("glob-not-str", {"design": {"zones": [{"glob": 5, "zone": "legacy"}]}}, "design.zones[0]"),
    ("bad-zone", {"design": {"zones": [{"glob": "a", "zone": "ancient"}]}}, "design.zones[0]"),
    ("extra-key", {"design": {"zones": [{"glob": "a", "zone": "legacy", "why": "x"}]}}, "design.zones[0]"),
    ("design-not-dict", {"design": ["zones"]}, "design"),
    ("zones-null", {"design": {"zones": None}}, "got null"),
    ("design-null", {"design": None}, "got null"),
]


@pytest.mark.parametrize("name,cfg,needle", _BAD, ids=[b[0] for b in _BAD])
def test_malformed_zones_refused_naming_index(tmp_path, name, cfg, needle):
    repo = _repo(tmp_path / "p", ("a",))
    _config(repo, cfg)
    p = _run(repo, "a")
    assert p.returncode == 2 and p.stdout == ""
    assert needle in p.stderr


# --- Task 3: newness at the fork point ---------------------------------------------------------

zones = _load()


def _fork_fixture(tmp_path, zone_entries=(("*", "legacy"),)):
    """origin/main has old.py. A goal worktree is cut from it; the branch adds new.py; main then
    advances (adds main_only.py, deletes gone.py) and origin/main is fetched, so HEAD, the fork
    point and the base tip are three DIFFERENT commits."""
    repo = _repo(tmp_path / "p", ("old.py", "gone.py"))
    _config(repo, _zones(*zone_entries))
    _with_origin(repo, tmp_path)
    wt = _worktree(repo)
    _commit(wt, "new.py", "n\n")
    _commit(repo, "main_only.py", "m\n")
    _git(repo, "rm", "-q", "gone.py")
    _git(repo, "commit", "-q", "-m", "delete gone")
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "fetch", "-q", "origin")
    return repo, wt


def test_ac3_path_absent_at_merge_base_prints_new_and_new_code_rule(tmp_path):
    repo, wt = _fork_fixture(tmp_path)
    p = _run(wt, "new.py", "main_only.py")
    assert p.returncode == 0, p.stderr
    rows = _rows(p)
    # new.py exists at HEAD but not at the fork point: new. main_only.py exists on the base TIP
    # but not at the fork point: also new (the fork point is the reference, not the tip).
    assert rows[0] == ["new.py", "legacy", "new", NEW_RULE]
    assert rows[1] == ["main_only.py", "legacy", "new", NEW_RULE]


def test_existing_path_prints_existing_with_zone_rule(tmp_path):
    repo, wt = _fork_fixture(tmp_path)
    assert _rows(_run(wt, "old.py"))[0] == ["old.py", "legacy", "existing", LEGACY_EXISTING]


def test_new_path_in_legacy_zone_keeps_zone_with_new_rule(tmp_path):
    repo, wt = _fork_fixture(tmp_path)
    r = _rows(_run(wt, "new.py"))[0]
    assert r[1] == "legacy" and r[2] == "new" and r[3] == NEW_RULE


def test_modern_new_and_existing_same_rule(tmp_path):
    repo, wt = _fork_fixture(tmp_path, (("*", "modern"),))
    rows = _rows(_run(wt, "old.py", "new.py"))
    assert rows[0][1:] == ["modern", "existing", NEW_RULE]
    assert rows[1][1:] == ["modern", "new", NEW_RULE]


def test_undeclared_existing_rule(tmp_path):
    repo, wt = _fork_fixture(tmp_path, (("nothing/*", "legacy"),))
    assert _rows(_run(wt, "old.py"))[0][1:] == ["undeclared", "existing", UNDECLARED_EXISTING]


def test_rename_counts_as_new(tmp_path):
    repo = _repo(tmp_path / "p", ("old.py",))
    _config(repo, _zones(("*", "legacy")))
    _with_origin(repo, tmp_path)
    wt = _worktree(repo)
    _git(wt, "mv", "old.py", "renamed.py")
    _git(wt, "commit", "-q", "-m", "rename")
    rows = _rows(_run(wt, "renamed.py", "old.py"))
    assert rows[0][2] == "new"
    assert rows[1][2] == "existing"      # the old name still exists at the fork point


def test_no_record_and_no_base_is_unknown(tmp_path):
    repo = _repo(tmp_path / "p", ("old.py",))
    _config(repo, _zones(("*", "legacy")))
    _with_origin(repo, tmp_path)
    wt = _worktree(repo, record=False)
    r = _rows(_run(wt, "old.py"))[0]
    assert r == ["old.py", "legacy", "unknown", UNKNOWN_RULE]


def test_unresolvable_base_ref_is_unknown_never_new(tmp_path):
    repo, wt = _fork_fixture(tmp_path)
    p = _run(wt, "--base", "origin/does-not-exist", "new.py", "old.py")
    assert p.returncode == 0
    assert all(r[2] == "unknown" and r[3] == UNKNOWN_RULE for r in _rows(p))


def test_detached_head_without_base_is_unknown(tmp_path):
    repo, wt = _fork_fixture(tmp_path)
    _git(wt, "checkout", "-q", "--detach")
    assert _rows(_run(wt, "old.py"))[0][2] == "unknown"
    # but an explicit --base still gives a fork point
    assert _rows(_run(wt, "--base", "origin/main", "old.py"))[0][2] == "existing"


def test_shallow_clone_merge_base_failure_is_unknown(tmp_path):
    repo, wt = _fork_fixture(tmp_path)
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", "file://%s" % (tmp_path / "origin.git"),
                    str(shallow)], check=True)
    _git(shallow, "checkout", "-q", "-b", "side")
    _commit(shallow, "s.py", "s\n")
    _config(shallow, _zones(("*", "legacy")))
    # an unrelated base the shallow history cannot reach a merge-base with
    other = _repo(tmp_path / "other", ("o.py",))
    _git(shallow, "fetch", "-q", str(other), "main:refs/remotes/other/main")
    p = _run(shallow, "--base", "other/main", "s.py")
    assert p.returncode == 0 and _rows(p)[0][2] == "unknown"


def test_path_present_at_fork_then_deleted_on_main_is_existing(tmp_path):
    repo, wt = _fork_fixture(tmp_path)
    assert not (repo / "gone.py").exists()
    assert _rows(_run(wt, "gone.py"))[0][2] == "existing"


def test_record_found_by_branch_with_custom_prefix(tmp_path):
    repo = _repo(tmp_path / "p", ("old.py",))
    _config(repo, _zones(("*", "legacy")))
    _with_origin(repo, tmp_path)
    wt = _worktree(repo, branch="work/x-1540")
    _commit(wt, "new.py", "n\n")
    rows = _rows(_run(wt, "old.py", "new.py"))
    assert [r[2] for r in rows] == ["existing", "new"]


def test_record_for_other_branch_ignored(tmp_path):
    repo = _repo(tmp_path / "p", ("old.py",))
    _config(repo, _zones(("*", "legacy")))
    _with_origin(repo, tmp_path)
    wt = _worktree(repo, goal="1540", record=False)
    other = _worktree(repo, goal="9999")        # a record exists, for a different branch
    assert other.exists()
    assert _rows(_run(wt, "old.py"))[0][2] == "unknown"


def test_base_flag_pins_ref(tmp_path):
    repo, wt = _fork_fixture(tmp_path)
    # pinned to the local branch main (the tip), main_only.py is absent at the merge-base either way
    assert _rows(_run(wt, "--base", "main", "main_only.py"))[0][2] == "new"


def test_runs_from_linked_worktree_reads_main_config(tmp_path):
    repo, wt = _fork_fixture(tmp_path)
    assert not (wt / ".sdlc" / "config.json").exists()
    assert _rows(_run(wt, "old.py"))[0][1] == "legacy"


def _fake_git(real, mode):
    def fake(args, cwd, stdin=None):
        if "cat-file" in args:
            if mode == "rc128":
                return 128, ""
            if mode == "timeout":
                return None, ""
            if mode == "short":
                return 0, ""
            n = len((stdin or "").splitlines())
            if mode == "rc_with_full_output":      # a failing git that still printed plausible lines
                return 1, "".join("%s blob 1\n" % ("a" * 40) for _ in range(n))
            if mode == "partial":                  # one line fewer than the input
                return 0, "".join("%s blob 1\n" % ("a" * 40) for _ in range(n - 1))
            if mode == "extra":                    # one line more than the input
                return 0, "".join("%s blob 1\n" % ("a" * 40) for _ in range(n + 1))
            if mode == "garbage":
                return 0, "\n".join("nonsense" for _ in (stdin or "").splitlines()) + "\n"
        return real(args, cwd, stdin)
    return fake


@pytest.mark.parametrize("mode", ["rc128", "timeout", "short", "garbage", "rc_with_full_output", "partial", "extra"])
def test_batch_check_nonzero_rc_is_unknown(tmp_path, monkeypatch, mode):
    repo, wt = _fork_fixture(tmp_path)
    monkeypatch.setattr(zones, "_git", _fake_git(zones._git, mode))
    rows = zones.classify(["old.py", "new.py"], str(wt))
    assert [r[2] for r in rows] == ["unknown", "unknown"]
    assert all(r[3] == UNKNOWN_RULE for r in rows)


def _oid_for(repo, absent):
    """A gitlink target: a real commit (batch-check prints `<oid> commit`) or an oid missing from the
    object store (it prints `<oid> submodule`); both shapes must be handled."""
    return "1" * 40 if absent else _git(repo, "rev-parse", "HEAD").strip()


@pytest.mark.parametrize("absent", [False, True])
def test_gitlink_path_is_existing(tmp_path, absent):
    repo = _repo(tmp_path / "p", ("old.py",))
    _config(repo, _zones(("*", "legacy")))
    _with_origin(repo, tmp_path)
    oid = _oid_for(repo, absent)
    _git(repo, "update-index", "--add", "--cacheinfo", "160000,%s,vendor/lib" % oid)
    _git(repo, "commit", "-q", "-m", "gitlink")
    _git(repo, "push", "-q", "origin", "main")
    wt = _worktree(repo)
    r = _rows(_run(wt, "vendor/lib"))[0]
    assert r[2] == "existing", r


@pytest.mark.parametrize("absent", [False, True])
def test_path_inside_submodule_is_unknown_never_new(tmp_path, absent):
    repo = _repo(tmp_path / "p", ("old.py",))
    _config(repo, _zones(("*", "legacy")))
    _with_origin(repo, tmp_path)
    oid = _oid_for(repo, absent)
    _git(repo, "update-index", "--add", "--cacheinfo", "160000,%s,vendor/lib" % oid)
    _git(repo, "commit", "-q", "-m", "gitlink")
    _git(repo, "push", "-q", "origin", "main")
    wt = _worktree(repo)
    rows = _rows(_run(wt, "vendor/lib/deep/x.py", "old.py"))
    assert rows[0][2] == "unknown" and rows[0][3] == UNKNOWN_RULE
    assert rows[1][2] == "existing"


def test_duplicate_paths_get_same_state(tmp_path):
    repo, wt = _fork_fixture(tmp_path)
    rows = _rows(_run(wt, "new.py", "./new.py", "old.py", "old.py"))
    assert [r[2] for r in rows] == ["new", "new", "existing", "existing"]


def test_unreachable_row_prints_ahead_of_unknown_fallback(tmp_path):
    repo = _repo(tmp_path / "p", ("old.py",))        # no config AND no record: both gaps at once
    r = _rows(_run(repo, "old.py"))[0]
    assert r[1] == "unreachable" and r[3] == UNREACHABLE_RULE


def test_raw_cat_file_e_returns_128_for_a_missing_path(tmp_path):
    """Why zones.py does not use `cat-file -e`: a MISSING path is rc 128, so rc alone cannot tell
    missing from broken. Recorded in the evidence file, asserted here so the premise is checked."""
    repo = _repo(tmp_path / "p", ("old.py",))
    p = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", "HEAD:nope.py"],
                       capture_output=True, text=True)
    assert p.returncode == 128


def test_repo_root_itself_is_refused(tmp_path):
    repo = _repo(tmp_path / "p")
    _config(repo, {})
    for arg in (".", str(repo), "src/..", "..", ""):
        p = _run(repo, arg)
        assert p.returncode == 2 and p.stdout == "", arg


def test_directory_argument_is_classified_by_the_string(tmp_path):
    repo, wt = _fork_fixture(tmp_path)
    _commit(wt, "pkg/new_in_pkg.py", "x\n")
    assert _rows(_run(wt, "pkg"))[0][2] == "new"        # the directory itself is new at the fork


# --- Task 4: read-only, no gh or network, exit codes -------------------------------------------


def _snapshot(*roots):
    import hashlib
    snap = {}
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(str(root)):
            if os.sep + "work" + os.sep in dirpath + os.sep and dirpath.startswith(str(root / ".sdlc" / "work")):
                dirnames[:] = []          # the linked worktree is a second checkout, snapshotted below
            if dirpath.endswith(os.path.join(".sdlc", "state", "time")) or \
                    (os.path.join(".sdlc", "state", "time") + os.sep) in dirpath + os.sep:
                dirnames[:] = []          # the kit-wide script-time floor (timing_store.timed_main)
                continue
            for f in filenames:
                p = os.path.join(dirpath, f)
                try:
                    snap[p] = hashlib.sha256(open(p, "rb").read()).hexdigest()
                except OSError:
                    snap[p] = "unreadable"
    return snap


def test_fixture_repos_disable_background_maintenance(tmp_path):
    """The writes-nothing snapshot is only meaningful if no git housekeeping runs beside it."""
    repo = _repo(tmp_path / "q")
    assert _git(repo, "config", "--get", "maintenance.auto").strip() == "false"
    assert _git(repo, "config", "--get", "gc.auto").strip() == "0"


def test_zones_writes_nothing(tmp_path):
    """Snapshot every file under the repo (including .git and .sdlc) and under the linked worktree
    before and after a classify run: nothing may appear, change or vanish. The ONE exclusion is
    `.sdlc/state/time`, the kit-wide script-time floor every script writes through
    `timing_store.timed_main` (a gitignored per-run timing record, not zones output). The mutation control writes
    a file INSIDE the repo (the checked tree, outside that directory), so a write is seen."""
    repo, wt = _fork_fixture(tmp_path)
    before = _snapshot(repo, wt)
    p = _run(wt, "old.py", "new.py", "missing.py")
    assert p.returncode == 0, p.stderr
    p2 = _run(repo, "old.py")
    assert p2.returncode == 0, p2.stderr
    after = _snapshot(repo, wt)
    assert after == before, sorted(set(after.items()) ^ set(before.items()))[:4]


def test_zones_has_no_gh_or_network():
    import ast
    src = SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(src)
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                docs.add(id(body[0].value))
    banned = {"urllib", "http", "socket", "requests", "ssl", "ftplib", "smtplib"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not banned & {a.name.split(".")[0] for a in node.names}, node.names
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in banned, node.module
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            assert not re.search(r"\bgh\b|\burllib\b|http\.client|\bsocket\b|\brequests\b", node.value), node.value
    assert "gh" not in {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}


def test_exit_codes(tmp_path):
    repo = _repo(tmp_path / "p", ("a.py",))
    _config(repo, _zones(("*", "legacy")))
    ok = _run(repo, "a.py")                     # state unknown (no record): still exit 0
    assert ok.returncode == 0 and ok.stdout.strip()
    bad = _run(repo, "a.py", "../nowhere.py")   # one good path, one refusal: nothing printed
    assert bad.returncode == 2 and bad.stdout == "" and "REFUSED" in bad.stderr
    _config(repo, {"design": {"zones": "x"}})
    bad = _run(repo, "a.py")
    assert bad.returncode == 2 and bad.stdout == ""
    notrepo = tmp_path / "plain"
    notrepo.mkdir()
    bad = _run(notrepo, "a.py")
    assert bad.returncode == 2 and bad.stdout == ""


# --- Task 5: template and docs (AC-5) ----------------------------------------------------------

DOC = ROOT / "docs" / "change-zones.md"
TEMPLATE = ROOT / "skills" / "sigma-init" / "templates" / "config.json.tmpl"


def test_template_declares_design_key_and_zones_absent():
    cfg = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    assert cfg["design"] == {}, "design present, zones absent by default"
    assert "design.zones" in cfg["_design"] and "docs/change-zones.md" in cfg["_design"]


def test_configuration_md_has_design_zones_row():
    rows = [ln for ln in (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8").splitlines()
            if ln.startswith("| `design.zones")]
    assert len(rows) == 1 and "change-zones.md" in rows[0] and "off" in rows[0]


def test_change_zones_doc_states_required_terms():
    t = DOC.read_text(encoding="utf-8")
    for term in ("design.zones", "fnmatch", "`*` crosses `/`", "last match", "sprout", "wrap",
                 "per-machine", ".sdlc/config.json", "nothing infers", "`new`", "--",
                 LEGACY_EXISTING, NEW_RULE, UNDECLARED_EXISTING, UNKNOWN_RULE, UNREACHABLE_RULE):
        assert term in t, term
    for rule in (LEGACY_EXISTING, NEW_RULE, UNDECLARED_EXISTING, UNKNOWN_RULE, UNREACHABLE_RULE):
        assert rule in SCRIPT.read_text(encoding="utf-8"), rule


def _doc_gesture():
    for ln in DOC.read_text(encoding="utf-8").splitlines():
        if ln.strip().startswith("python3 skills/sigma-loop/scripts/zones.py classify"):
            return ln.strip()
    raise AssertionError("no runnable gesture in docs/change-zones.md")


def test_change_zones_doc_gesture_runs(tmp_path):
    g = _doc_gesture()
    assert "zones.py classify" in g
    repo = _repo(tmp_path / "p", ("src/legacy/a.py",))
    _config(repo, _zones(("src/legacy/*", "legacy")))
    argv = g.split()
    argv[1] = str(SCRIPT)
    p = subprocess.run(argv, cwd=str(repo), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    row = p.stdout.splitlines()[0].split("\t")
    assert len(row) == 4 and row[0] == "src/legacy/a.py" and row[1] == "legacy"


def test_doc_leading_dash_gesture_is_documented_and_needed(tmp_path):
    assert "classify -- -" in DOC.read_text(encoding="utf-8")
    repo = _repo(tmp_path / "p", ("-odd.py",))
    _config(repo, {})
    without = _run(repo, "-odd.py")
    assert without.returncode == 2 and without.stdout == ""     # argparse reads it as a flag
    assert _run(repo, "--", "-odd.py").returncode == 0


# --- Task 6: sigma-vision may only suggest (AC-4) ----------------------------------------------

_WRITE_VERB = re.compile(r"\b(write|writes|writing|set|sets|add|adds|put|puts|edit|edits|fill|fills|"
                         r"populate|populates|declare|declares)\b", re.I)
_NEGATION = re.compile(r"\b(never|not|no|only suggest|suggest)\b", re.I)


def _vision_violations(files):
    bad = []
    for f in files:
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if "design.zones" not in line:
                continue
            if "suggest" not in line.lower():
                bad.append("%s:%d no 'suggest': %s" % (f, n, line))
            if _WRITE_VERB.search(line) and not re.search(r"\b(never|not)\b", line, re.I):
                bad.append("%s:%d instructs a write: %s" % (f, n, line))
    return bad


def test_ac4_sigma_vision_never_writes_design_zones():
    files = [p for p in (ROOT / "skills" / "sigma-vision").rglob("*") if p.is_file() and p.suffix in (".md", ".py", ".json")]
    assert files
    assert any("design.zones" in p.read_text(encoding="utf-8") for p in files), "the suggest-only line must exist"
    assert _vision_violations(files) == []


def test_ac4_checker_rejects_a_violating_line(tmp_path):
    f = tmp_path / "x.md"
    f.write_text("Write `design.zones` to .sdlc/config.json.\n", encoding="utf-8")
    assert _vision_violations([f])
    g = tmp_path / "y.md"
    g.write_text("May suggest candidate legacy paths; never write `design.zones`.\n", encoding="utf-8")
    assert _vision_violations([g]) == []
    h = tmp_path / "z.md"
    h.write_text("Suggest and then write `design.zones` for the owner.\n", encoding="utf-8")
    assert _vision_violations([h])


def test_ac4_checker_requires_the_word_suggest(tmp_path):
    f = tmp_path / "n.md"
    f.write_text("The owner owns `design.zones`; never write it.\n", encoding="utf-8")
    assert _vision_violations([f])


# --- review round 1: spaces, non-UTF-8, symlinks, scale, `..` ----------------------------------


def test_spaced_new_path_next_to_existing_path_in_one_call(tmp_path):
    """git echoes the key, so a `missing` line has as many tokens as the path has spaces; one such
    line must not turn every row unknown."""
    repo = _repo(tmp_path / "p", ("old file.py", "plain.py"))
    _config(repo, _zones(("*", "legacy")))
    _with_origin(repo, tmp_path)
    wt = _worktree(repo)
    _commit(wt, "new file.py", "n\n")
    rows = _rows(_run(wt, "new file.py", "plain.py", "old file.py", "src/sub dir/x y.py"))
    assert [r[2] for r in rows] == ["new", "existing", "existing", "new"], rows
    assert rows[0][3] == NEW_RULE and rows[1][3] == LEGACY_EXISTING


def test_spaced_colon_and_dash_paths_together(tmp_path):
    repo = _repo(tmp_path / "p", ("a b:c.py", "-d e.py"))
    _config(repo, _zones(("*", "legacy")))
    _with_origin(repo, tmp_path)
    wt = _worktree(repo)
    rows = _rows(_run(wt, "--", "a b:c.py", "-d e.py", "z z:y.py"))
    assert [r[2] for r in rows] == ["existing", "existing", "new"], rows


def test_non_utf8_argument_is_classified_and_does_not_poison_the_batch(tmp_path):
    repo = _repo(tmp_path / "p", ("ok.py",))
    _config(repo, _zones(("*", "legacy")))
    _with_origin(repo, tmp_path)
    wt = _worktree(repo)
    bad = b"src/\xff.py"
    p = subprocess.run([sys.executable, str(SCRIPT), "classify", "ok.py", bad], cwd=str(wt),
                       capture_output=True, timeout=60)
    assert p.returncode == 0, p.stderr
    rows = [ln.split(b"\t") for ln in p.stdout.split(b"\n") if ln]
    assert rows[0][0] == b"ok.py" and rows[0][2] == b"existing"
    assert rows[1][0] == bad and rows[1][2] == b"new"


def test_tracked_symlink_is_classified_as_itself(tmp_path):
    repo = _repo(tmp_path / "p", ("src/a.py",))
    _config(repo, _zones(("src/*", "legacy")))
    os.symlink("a.py", str(repo / "src" / "link.py"))
    os.symlink("/etc/hosts", str(repo / "src" / "out.py"))
    _git(repo, "add", "src/link.py", "src/out.py")
    _git(repo, "commit", "-q", "-m", "links")
    _with_origin(repo, tmp_path)
    wt = _worktree(repo)
    rows = _rows(_run(wt, "src/link.py", "src/out.py"))
    assert [r[0] for r in rows] == ["src/link.py", "src/out.py"], rows
    assert [r[1:3] for r in rows] == [["legacy", "existing"]] * 2


def test_newly_added_symlink_to_existing_target_is_new(tmp_path):
    repo = _repo(tmp_path / "p", ("src/a.py",))
    _config(repo, _zones(("*", "legacy")))
    _with_origin(repo, tmp_path)
    wt = _worktree(repo)
    os.symlink("a.py", str(wt / "src" / "fresh.py"))
    _git(wt, "add", "src/fresh.py")
    _git(wt, "commit", "-q", "-m", "link")
    rows = _rows(_run(wt, "src/fresh.py", "src/a.py"))
    assert [r[0:3] for r in rows] == [["src/fresh.py", "legacy", "new"], ["src/a.py", "legacy", "existing"]]


def test_path_count_scaling_is_not_quadratic(tmp_path):
    """The key de-duplication must be hashed, not a list scan. Wall clock under load is unreliable, so
    this compares the SAME call at N and 4N paths (best of 3 each, same process, same load): linear
    gives a ratio near 4, a list scan near 9 at these sizes (measured: 3.9 and 9.1). The bound sits
    between them; the absolute cap only guards against a hang."""
    repo = _repo(tmp_path / "p", ("a.py",))
    _config(repo, _zones(("*", "legacy")))
    _with_origin(repo, tmp_path)
    wt = _worktree(repo)
    import time

    def best(n):
        paths = ["d%d/e%d/f.py" % (i, i) for i in range(n)]       # unique parents: 2n keys
        t_best = None
        for _ in range(3):
            t0 = time.perf_counter()
            p = _run(wt, *paths)
            dt = time.perf_counter() - t0
            assert p.returncode == 0, p.stderr
            assert len(p.stdout.splitlines()) == n
            t_best = dt if t_best is None else min(t_best, dt)
        return t_best

    small, large = best(SCALE_N), best(4 * SCALE_N)
    assert large < SCALE_HANG_CAP_S, large
    assert large / small < SCALE_RATIO_MAX, (small, large, large / small)


SCALE_N = 5000
SCALE_RATIO_MAX = 6.0
SCALE_HANG_CAP_S = 60.0


def test_control_characters_other_than_newline_do_not_split_rows(tmp_path):
    repo = _repo(tmp_path / "p", ("ok.py",))
    _config(repo, _zones(("*", "legacy")))
    _with_origin(repo, tmp_path)
    wt = _worktree(repo)
    p = _run(wt, "ok.py", "a\x0bb.py", "c\x0cd.py", "ok.py")
    rows = [ln.split("\t") for ln in p.stdout.split("\n") if ln]       # not splitlines(): \x0b is a path byte
    assert [r[2] for r in rows] == ["existing", "new", "new", "existing"], rows


def test_backslash_in_a_posix_name_is_not_a_separator(tmp_path):
    repo = _repo(tmp_path / "p", ("back\\slash.py", "back/slash.py"))
    _config(repo, _zones(("*", "legacy")))
    _with_origin(repo, tmp_path)
    wt = _worktree(repo)
    rows = _rows(_run(wt, "back\\slash.py", "back/slash.py"))
    assert [r[:3] for r in rows] == [["back\\slash.py", "legacy", "existing"],
                                     ["back/slash.py", "legacy", "existing"]]


def test_tab_in_a_path_is_refused_like_newline(tmp_path):
    repo = _repo(tmp_path / "p")
    _config(repo, {})
    p = _run(repo, "README.md", "a\tb.py")
    assert p.returncode == 2 and p.stdout == "" and "tab" in p.stderr.lower()


def test_base_value_starting_with_dash_is_refused_not_passed_to_git(tmp_path):
    repo, wt = _fork_fixture(tmp_path)
    for base in ("--base=--all", "-x"):
        argv = ["--base=--all"] if base == "--base=--all" else ["--base", base]
        p = _run(wt, *argv, "old.py")
        assert p.returncode == 2 and p.stdout == "" and "base" in p.stderr.lower(), (argv, p.stderr)


def test_documented_limit_nfd_and_case_spellings_print_new(tmp_path):
    """PINS the limits docs/change-zones.md states: git looks names up byte for byte, so a different
    Unicode spelling (NFD of an NFC name) or a different-case spelling of a tracked file prints `new`
    even where the filesystem would open it. If this changes, update the docs section too."""
    nfc = "caf\u00e9.py"
    nfd = "cafe\u0301.py"
    repo = _repo(tmp_path / "p", (nfc, "Readme.py"))
    _config(repo, _zones(("*", "legacy")))
    _with_origin(repo, tmp_path)
    wt = _worktree(repo)
    rows = _rows(_run(wt, nfc, nfd, "Readme.py", "readme.py"))
    assert [r[2] for r in rows] == ["existing", "new", "existing", "new"], rows


def test_doc_states_the_nfd_and_case_limits_and_refusals():
    t = DOC.read_text(encoding="utf-8")
    for term in ("Unicode normalisation", "NFD", "NFC", "Case (case-insensitive", "may be wrong",
                 "pass the spelling git", "carriage return or tab", "never a separator"):
        assert term in t, term
