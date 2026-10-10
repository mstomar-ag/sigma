"""#947 (part B, slice 5): Level 1 on the unit path -- a changelog-only conflict resolved by the heading-aware union,
proved, stamped, continued and pushed only through the atomic backup push.

Offline: real git in scratch repositories (a bare remote and a checkout), a recording runner, a stand-in for the verify
runner, and a stand-in for the issue filer. No model call and no network call. The gate-closed tests assert the new code
adds nothing to a closed pass."""
import copy
import os
import pathlib
import subprocess

import pytest

import test_feature_rebase as base

m_cache = {}
UNIT, FEATURE, MAIN = base.UNIT, base.FEATURE, base.INTEGRATION
LOG = "# Changelog\n\n## Unreleased\n\n%s- base entry\n"
AUTHOR = ("Ada Original", "ada@example.com", "1700000000 +0000")


def mod():
    return base._mod()


def _commit(cwd, subject, author=AUTHOR):
    env = dict(os.environ, GIT_AUTHOR_NAME=author[0], GIT_AUTHOR_EMAIL=author[1], GIT_AUTHOR_DATE=author[2],
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CONFIG_SYSTEM=os.devnull)
    subprocess.run(["git", "commit", "-q", "-m", subject], cwd=str(cwd), env=env, check=True, capture_output=True)


def world(tmp_path, feat=None, integ=None, base_files=None, subject="feat: add the feature entry (#11)"):
    """seed + a changelog on main; `feat` / `integ` are {path: text} edits; defaults make a clean changelog conflict."""
    w = base.World(tmp_path).build(feature_commits=None)
    files = {"CHANGELOG.md": LOG % ""} if base_files is None else base_files
    for name, text in files.items():
        base._write(w.local / name, text)
        base._git(w.local, "add", name)
    base._git(w.local, "commit", "-q", "-m", "changelog")
    base._git(w.local, "push", "-q", "origin", MAIN)
    base._git(w.local, "checkout", "-q", "-b", FEATURE)
    for name, text in (feat if feat is not None else {"CHANGELOG.md": LOG % "- feature entry\n"}).items():
        base._write(w.local / name, text)
        base._git(w.local, "add", name)
    _commit(w.local, subject)
    base._git(w.local, "push", "-q", "origin", FEATURE)
    base._git(w.local, "checkout", "-q", MAIN)
    for name, text in (integ if integ is not None else {"CHANGELOG.md": LOG % "- integration entry\n"}).items():
        base._write(w.local / name, text)
        base._git(w.local, "add", name)
    base._git(w.local, "commit", "-q", "-m", "integration moves")
    base._git(w.local, "push", "-q", "origin", MAIN)
    return w


def cfg(resolve="mechanical", without_verify=False, verify=True, open_=True):
    c = base._cfg()
    if open_:
        c["upkeep"] = {"enabled": True, "conflicts": {"resolve": resolve, "mechanical_without_verify": without_verify}}
    if verify:
        c["verify"] = {"command": "true"}
    return c


class Spy:
    """A recording runner over the real one; `before` lets a test act just before a matching command."""

    def __init__(self, m, before=None):
        self.m, self.calls, self.before = m, [], before

    def __call__(self, cwd, argv):
        self.calls.append([str(a) for a in argv])
        if self.before:
            self.before(argv)
        return self.m._run(cwd, argv)

    def pushes(self):
        return [c for c in self.calls if "push" in c[:3]]


def verify_ok(m, monkeypatch, outcome="ok"):
    real = m._level1_options

    def options(config):
        got = real(config)
        if got is not None:
            got["run_verify"] = lambda command, worktree, timeout: type("R", (), {"outcome": outcome})()
        return got
    monkeypatch.setattr(m, "_level1_options", options)


def run_pass(m, w, config, spy=None):
    spy = spy or Spy(m)
    base._filer(m)
    return m.upkeep(str(w.sdlc), config, "7", UNIT, run=spy), spy


def remote_log(w, ref=FEATURE):
    base._git(w.local, "fetch", "-q", "origin")
    return base._git(w.local, "log", "-1", "--format=%an|%ae|%at|%B", "origin/" + ref)


# --------------------------------------------------------------------------- the gate closed and the level off


@pytest.mark.parametrize("config", [cfg(open_=False), cfg(resolve="off")])
def test_a_closed_gate_or_level_off_parks_or_conflicts_exactly_as_before(tmp_path, config):
    m = mod()
    w = world(tmp_path)
    before = w.tip(FEATURE)
    report, spy = run_pass(m, w, config)
    assert report["outcome"] in (m.CONFLICT, m.PARKED) and w.tip(FEATURE) == before
    assert not any("commit" in c[:3] or "core.hooksPath" in " ".join(c) for c in spy.calls)
    assert not (w.sdlc / "state" / "rebase-acks").exists() and not (w.sdlc / "state" / "upkeep").exists()
    assert "resolved" not in report and not m.worktree_path(str(w.sdlc), UNIT).exists()


def test_the_new_code_has_no_effect_under_a_closed_gate():
    assert mod()._level1_options(cfg(open_=False)) is None and mod()._level1_options(cfg(resolve="off")) is None
    assert mod()._level1_options(cfg())["allow_no_verify"] is False


# --------------------------------------------------------------------------- the happy path


def test_a_changelog_only_conflict_is_resolved_stamped_and_pushed_once_with_a_backup(tmp_path, monkeypatch):
    m = mod()
    w = world(tmp_path)
    before = w.tip(FEATURE)
    verify_ok(m, monkeypatch)
    report, spy = run_pass(m, w, cfg())
    assert report["outcome"] == m.REBASED and report["resolved"]["level"] == 1, report.get("why")
    assert len(spy.pushes()) == 1 and report["backup"]
    refs = base._git(w.local, "ls-remote", "origin")
    assert before in refs and report["backup"] in refs and w.tip(FEATURE) != before
    text = base._git(w.local, "show", "origin/%s:CHANGELOG.md" % FEATURE)
    assert "feature entry" in text and "integration entry" in text and "<<<<" not in text
    name, email, stamp, body = remote_log(w).split("|", 3)
    assert (name, email, stamp) == (AUTHOR[0], AUTHOR[1], "1700000000")
    assert body.rstrip().splitlines()[-2:] == ["", "sigma-resolution: 1 " + report["resolved"]["run_id"]]
    assert list((w.sdlc / "state" / "upkeep" / "resolutions").glob("*.json"))
    assert not m.worktree_path(str(w.sdlc), UNIT).exists()
    assert "level 1 resolved" in m.clause(report)


@pytest.mark.parametrize("kind", ["other-file", "two-paths", "no-base-stage"])
def test_anything_that_is_not_a_single_changelog_conflict_parks_and_touches_nothing(tmp_path, monkeypatch, kind):
    m = mod()
    if kind == "other-file":
        w = world(tmp_path, base_files={"a.txt": "a\n"}, feat={"a.txt": "feature\n"}, integ={"a.txt": "integration\n"})
    elif kind == "two-paths":
        w = world(tmp_path, base_files={"a.txt": "a\n", "CHANGELOG.md": LOG % ""},
                  feat={"a.txt": "f\n", "CHANGELOG.md": LOG % "- feature entry\n"},
                  integ={"a.txt": "i\n", "CHANGELOG.md": LOG % "- integration entry\n"})
    else:
        w = world(tmp_path, base_files={"a.txt": "a\n"}, feat={"CHANGELOG.md": LOG % "- feature entry\n"},
                  integ={"CHANGELOG.md": LOG % "- integration entry\n"})
    before = w.tip(FEATURE)
    verify_ok(m, monkeypatch)
    report, spy = run_pass(m, w, cfg())
    assert report["outcome"] == m.PARKED and w.tip(FEATURE) == before and not spy.pushes()
    assert not m.worktree_path(str(w.sdlc), UNIT).exists()


# --------------------------------------------------------------------------- every proof refusal parks


def _refusing(monkeypatch, m, name, result):
    proof = m._prove()._sibling("conflict_proof")
    monkeypatch.setattr(proof, name, lambda *a, **k: result)
    return proof


@pytest.mark.parametrize("name,result,code", [
    ("check_markers", [{"code": "conflict-marker", "detail": "x"}], "conflict-marker"),
    ("new_whitespace_findings", [{"code": "new-whitespace-finding", "detail": "x"}], "new-whitespace-finding"),
    ("only_conflicted_differ", [{"code": "stage0-changed", "detail": "x"}], "stage0-changed"),
    ("multiset_differences", [{"code": "outside-conflicted-lines-changed", "detail": "x"}],
     "outside-conflicted-lines-changed"),
    ("pair_commits", {"pairs": [], "skipped": [], "emptied": [],
                      "refusals": [{"code": "key-collision", "detail": "x"}, {"code": "unaccounted-original", "detail": "y"}]},
     "key-collision"),
])
def test_each_refused_check_parks_with_its_code_and_nothing_is_pushed(tmp_path, monkeypatch, name, result, code):
    m = mod()
    w = world(tmp_path)
    before = w.tip(FEATURE)
    verify_ok(m, monkeypatch)
    _refusing(monkeypatch, m, name, result)
    report, spy = run_pass(m, w, cfg())
    assert report["outcome"] == m.PARKED and code in report["why"], report["why"]
    assert w.tip(FEATURE) == before and not spy.pushes() and not (w.sdlc / "state" / "upkeep").exists()


def test_a_failed_verify_parks(tmp_path, monkeypatch):
    m = mod()
    w = world(tmp_path)
    verify_ok(m, monkeypatch, outcome="failed")
    report, spy = run_pass(m, w, cfg())
    assert report["outcome"] == m.PARKED and "verify-failed" in report["why"] and not spy.pushes()


def test_pairing_is_by_authorship_key_so_a_changed_context_still_pairs(tmp_path, monkeypatch):
    """The context-drift fixture: a second commit whose surrounding lines moved on the integration side is still paired."""
    m = mod()
    w = world(tmp_path, base_files={"CHANGELOG.md": LOG % "", "o.txt": "1\n2\n3\n4\n5\n"})
    base._git(w.local, "checkout", "-q", FEATURE)
    base._write(w.local / "o.txt", "1\n2\n3\n4\n5\nsix\n")
    base._git(w.local, "add", "o.txt")
    _commit(w.local, "feat: second commit (#12)", ("Bob Second", "bob@example.com", "1700000100 +0000"))
    base._git(w.local, "push", "-q", "origin", FEATURE)
    base._git(w.local, "checkout", "-q", MAIN)
    base._write(w.local / "o.txt", "0\n1\n2\n3\n4\n5\n")
    base._git(w.local, "add", "o.txt")
    base._git(w.local, "commit", "-q", "-m", "integration shifts the file")
    base._git(w.local, "push", "-q", "origin", MAIN)
    verify_ok(m, monkeypatch)
    report, _ = run_pass(m, w, cfg())
    assert report["outcome"] == m.REBASED, report.get("why")


# --------------------------------------------------------------------------- verify, descriptor, lease


def test_no_verify_command_parks_unless_the_operator_allowed_it(tmp_path, monkeypatch):
    m = mod()
    w = world(tmp_path)
    before = w.tip(FEATURE)
    report, spy = run_pass(m, w, cfg(verify=False))
    assert report["outcome"] == m.PARKED and "no-verify-command" in report["why"] and w.tip(FEATURE) == before
    report, spy = run_pass(m, w, cfg(verify=False, without_verify=True))
    assert report["outcome"] == m.REBASED and len(spy.pushes()) == 1


def test_a_resolved_replay_without_a_backup_descriptor_is_refused_and_never_pushed(tmp_path, monkeypatch):
    m = mod()
    w = world(tmp_path)
    verify_ok(m, monkeypatch)
    base._git(w.local, "fetch", "-q", "origin")
    sha = w.tip(FEATURE)
    spy = Spy(m)
    report = base_report(m)
    out = m._rebase_feature(spy, str(w.local), m.worktree_path(str(w.sdlc), UNIT), FEATURE, "origin/" + MAIN, sha,
                            "origin", report, strict=True, backup=None, level1=m._level1_options(cfg()))
    assert out == m.CONFLICT and not spy.pushes() and w.tip(FEATURE) == sha
    assert "no-backup-descriptor" in report["why"]


def base_report(m):
    r = m._report("7", UNIT, cfg())
    r.update(branch=FEATURE, base=MAIN)
    return r


def test_a_stale_lease_a_taken_backup_name_and_a_failed_push_write_nothing(tmp_path, monkeypatch):
    m = mod()
    w = world(tmp_path)
    verify_ok(m, monkeypatch)
    fb = m._backup()
    clock = lambda: 1.8e9                                   # noqa: E731
    monkeypatch.setattr(m, "_WALL", clock)
    taken = fb.ref_name(UNIT, fb.stamp_of(fb._now(clock)))
    base._git(w.local, "push", "-q", "origin", "%s:refs/%s" % (MAIN, taken.removeprefix("refs/")))
    before = w.tip(FEATURE)
    report, spy = run_pass(m, w, cfg())
    assert report["outcome"] == m.FAILED and w.tip(FEATURE) == before
    assert "resolved" not in report and not (w.sdlc / "state" / "upkeep").exists()
    assert not (w.sdlc / "state" / "rebase-acks").exists()

    def other_writer(argv):
        if "push" in list(argv)[:3]:
            base._git(w.local, "checkout", "-q", FEATURE)
            base._write(w.local / "other.txt", "x\n")
            base._git(w.local, "add", "other.txt")
            base._git(w.local, "commit", "-q", "-m", "other writer (#99)")
            base._git(w.local, "push", "-q", "origin", FEATURE)
            base._git(w.local, "checkout", "-q", MAIN)
    monkeypatch.setattr(m, "_WALL", None)
    report, _ = run_pass(m, w, cfg(), Spy(m, before=other_writer))
    assert report["outcome"] == m.LEASE_REFUSED and "resolved" not in report
    assert not (w.sdlc / "state" / "upkeep").exists()


# --------------------------------------------------------------------------- acks and the pull-request rule


def test_acks_are_recomputed_into_the_runtime_file_and_the_tracked_store_is_untouched(tmp_path, monkeypatch):
    m = mod()
    w = world(tmp_path, subject="feat: add the feature entry")      # no pull-request trace: direct until acked
    verify_ok(m, monkeypatch)
    spy = Spy(m)
    assert m.ack(str(w.sdlc), cfg(), UNIT, [], all_=True, run=spy, cwd=str(w.local))["ok"]
    tracked = m.ack_path(str(w.sdlc), UNIT)
    snapshot = tracked.read_bytes()
    report, _ = run_pass(m, w, cfg())
    assert report["outcome"] == m.REBASED, report.get("why")
    assert tracked.read_bytes() == snapshot
    runtime = m.runtime_ack_path(str(w.sdlc), UNIT)
    entries = m._read_acks(runtime.read_text())
    new = report["resolved"]["new_tip"]
    assert [e["sha"] for e in entries] == [new] and entries[0]["patch_id"]
    base._git(w.local, "fetch", "-q", "origin")
    found = m.direct_commits(m._run, str(w.local), "origin/" + MAIN, "origin/" + FEATURE)
    assert found and m._unacked(str(w.sdlc), m._run, str(w.local), UNIT, "origin/" + MAIN, found) == ([], found)


def test_a_stamp_in_the_body_leaves_the_commit_arrived_and_one_joined_to_the_subject_reads_direct(tmp_path, monkeypatch):
    m = mod()
    w = world(tmp_path)
    verify_ok(m, monkeypatch)
    report, _ = run_pass(m, w, cfg())
    assert report["outcome"] == m.REBASED
    base._git(w.local, "fetch", "-q", "origin")
    assert m.direct_commits(m._run, str(w.local), "origin/" + MAIN, "origin/" + FEATURE) == []
    assert m.arrived_through_a_pull_request("feat: x (#11)") is True
    assert m.arrived_through_a_pull_request("feat: x (#11) sigma-resolution: 1 0123456789ab") is False


# --------------------------------------------------------------------------- the budget lever


def test_the_budget_refusal_names_the_operator_variable_and_its_value(tmp_path, monkeypatch):
    m = mod()
    clock = [1000.0]
    monkeypatch.setattr(m, "_now", lambda: clock[0])
    monkeypatch.delenv("SIGMA_WATCH_CALL_TIMEOUT", raising=False)
    deadline = m.guard_deadline()
    clock[0] += 130
    with pytest.raises(RuntimeError) as slow:
        m._git_read(str(tmp_path), ["--version"], deadline)
    assert "SIGMA_WATCH_CALL_TIMEOUT is 120s" in str(slow.value)
    monkeypatch.setenv("SIGMA_WATCH_CALL_TIMEOUT", "1000")
    clock[0] = 1000.0
    deadline = m.guard_deadline()
    clock[0] += 130
    assert "git version" in m._git_read(str(tmp_path), ["--version"], deadline)


# --------------------------------------------------------------------------- the continuation


def test_the_continuation_survives_signing_and_runs_no_repository_hook(tmp_path, monkeypatch):
    m = mod()
    w = world(tmp_path)
    verify_ok(m, monkeypatch)
    ran = tmp_path / "hook-ran"
    for hook in ("post-rewrite", "post-commit"):
        path = w.local / ".git" / "hooks" / hook
        path.parent.mkdir(exist_ok=True)
        path.write_text("#!/bin/sh\necho x >> '%s'\nexit 1\n" % ran)
        path.chmod(0o755)
    base._git(w.local, "config", "commit.gpgsign", "true")
    base._git(w.local, "config", "gpg.program", "/usr/bin/false")
    report, _ = run_pass(m, w, cfg())
    assert report["outcome"] == m.REBASED, report.get("why")
    assert ran.read_text().count("x") == 1                         # only the stamping commit's own post-commit hook
    base._git(w.local, "config", "--unset", "commit.gpgsign")
