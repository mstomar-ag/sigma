"""#1573: the agent conflict level as ONE per-unit scratch session. Driven only by a FAKE `claude` (a python script written
into tmp_path) through the harness of tests/test_upkeep_level2.py (Rig, conflict_world) and the shipped factory of
tests/test_upkeep_agent_1516.py (`wired`). No real model, no network; the network wall is only checked as argv text.
Controls (each broken once, seen red, then restored): the symlink read-back check (`test_a_link_...`), the `session` flag
(`test_a_multi_commit_replay_...`), the dropped-line proof (`test_a_dropped_unit_line_...`), the deadline
(`test_the_whole_session_...`). Gesture: `python -m pytest tests/test_upkeep_session_1573.py -q`."""
import json
import os
import pathlib
import sys
import time

import pytest

import test_feature_rebase as base
import test_upkeep_level1 as l1
from test_upkeep_agent_1516 import agent_cfg, wired  # noqa: F401  (wired is a fixture)
from test_upkeep_level2 import APP, FEATURE, UNIT, mk, rig  # noqa: F401  (rig is a fixture)

STUB = '''#!%(python)s
import json, os, pathlib, subprocess, sys
here = pathlib.Path(%(dir)r)
mode = json.loads((here / "mode.json").read_text())
stdin = sys.stdin.read()
reviewer = "independent reviewer" in stdin
calls = json.loads((here / "calls.json").read_text()) if (here / "calls.json").exists() else []
cwd = pathlib.Path(os.getcwd())
ctx = {}
for p in sorted((cwd / ".sigma-context").rglob("*")) if (cwd / ".sigma-context").exists() else []:
    if p.is_file():
        ctx[p.relative_to(cwd).as_posix()] = p.read_text()
remote = subprocess.run(["git", "remote"], capture_output=True, text=True).stdout if not reviewer else ""
call = {"role": "review" if reviewer else "resolve", "cwd": str(cwd), "argv": sys.argv[1:], "stdin": stdin, "ctx": ctx,
        "env": dict(os.environ), "remote": remote, "had_build": (cwd / "build" / "out.bin").exists()}
calls.append(call)
(here / "calls.json").write_text(json.dumps(calls))
if reviewer:
    print(json.dumps({"verdict": "approve", "reasons": ["keeps both sides"]}))
    raise SystemExit(0)
how = mode.get("resolve", "both")
nresolve = len([c for c in calls if c["role"] == "resolve"])
if how == "drop_first":
    how = "drop" if nresolve == 1 else "both"
if how == "both" and nresolve > 1 and "was refused" in stdin and (cwd / ".sigma-context").exists():
    # a retry: the files hold the earlier attempt, so rebuild them as ours + the unit's added lines from the context
    latest = sorted((cwd / ".sigma-context").glob("stop*"))[-1]
    for theirs in latest.glob("*.theirs"):
        stem = theirs.name[:-len(".theirs")]
        ours = latest / (stem + ".ours")
        if not ours.exists():
            continue
        keep = [l if l.endswith("\\n") else l + "\\n" for l in ours.read_text().splitlines(True)]
        keep += [l if l.endswith("\\n") else l + "\\n" for l in theirs.read_text().splitlines(True) if l not in keep]
        (cwd / stem.replace("__", "/")).write_text("".join(keep))
for gone in mode.get("rm", []):
    if (cwd / gone).exists():
        (cwd / gone).unlink()
for p in sorted(cwd.rglob("*")):
    if ".git" in p.parts or ".sigma-context" in p.parts or not p.is_file() or p.is_symlink():
        continue
    if "<<<<<<<" not in p.read_text():
        continue
    keep = [l for l in p.read_text().splitlines(True) if l[:7] not in ("<<<<<<<", "=======", ">>>>>>>")]
    if how == "drop":
        keep = [l for l in keep if "feature side" not in l and "feature two" not in l] + ["resolver extra line\\n"]
    if how == "link":
        outside = pathlib.Path(mode["outside"])
        outside.write_text("".join(keep))
        p.unlink()
        p.symlink_to(outside)
        continue
    p.write_text("".join(keep))
if how == "build":
    (cwd / "build").mkdir(exist_ok=True)
    (cwd / "build" / "out.bin").write_text("artifact")
    (cwd / "ran.txt").write_text(subprocess.run(["echo", "ran"], capture_output=True, text=True).stdout)
print("done")
'''

LINES = "l1\nshared2\nl3\nl4\nl5\nshared6\nl7\n"


def two_commit_world(tmp):
    """Two feature commits, each conflicting with the ONE integration commit in the same code file."""
    files = {"CHANGELOG.md": l1.LOG % "", APP: LINES}
    w = l1.world(tmp, feat={APP: LINES.replace("shared2", "feature side")},
                 integ={APP: LINES.replace("shared2", "integration side").replace("shared6", "integration two")}, base_files=files)
    base._git(w.local, "checkout", "-q", FEATURE)
    base._write(w.local / APP, LINES.replace("shared2", "feature side").replace("shared6", "feature two"))
    base._git(w.local, "add", APP)
    l1._commit(w.local, "feat: second step (#12)")
    base._git(w.local, "push", "-q", "origin", FEATURE)
    base._git(w.local, "checkout", "-q", l1.MAIN)
    return w


@pytest.fixture
def srig(rig):  # noqa: F811
    rig.binary.write_text(STUB % {"python": sys.executable, "dir": str(rig.fake)})
    return rig


def install_session(rig, m, w, session=True, net_prefix=None, **extra):
    plan = lambda config: dict(rig.plan(w), config=config, **extra, **({"session": True, "net_prefix": net_prefix} if session else {}))  # noqa: E731
    rig.mp.setattr(m, "LEVEL2_PLAN_FACTORY", plan)


def run_session(rig, tmp_path, world=None, session=True, net_prefix=None, spy=None, extra=None):
    m = l1.mod()
    w = world or two_commit_world(mk(tmp_path / "w"))
    install_session(rig, m, w, session, net_prefix, **(extra or {}))
    l1.verify_ok(m, rig.mp)
    report, spy = l1.run_pass(m, w, l1.cfg(resolve="agent"), spy)
    return m, w, report, spy


@pytest.fixture
def wired_s(wired, monkeypatch):  # noqa: F811
    """The shipped factory, with the host's real network wall switched off so the run is the same on every platform."""
    monkeypatch.setattr(wired._launcher(), "session_wall", lambda platform, which, home, session_dir, extra=(): (None, None))
    return wired


def resolves(rig):
    return [c for c in rig.calls() if c["role"] == "resolve"]


# --------------------------------------------------------------------------- 1: one session across the replay


def test_a_multi_commit_replay_is_resolved_in_one_session_dir_with_context_and_pushed_once(srig, tmp_path, wired_s):
    rig, m = srig, wired_s
    w = two_commit_world(mk(tmp_path / "w"))
    report, spy = l1.run_pass(m, w, agent_cfg())
    assert report["outcome"] == m.REBASED and report["resolved"]["level"] == 2, report.get("why")
    assert len(spy.pushes()) == 1 and report["backup"]
    first, second = resolves(rig)
    assert first["cwd"] == second["cwd"] and first["cwd"].endswith(os.sep + "session")
    assert not os.path.exists(first["cwd"])                                  # removed when the pass ended
    assert second["argv"][-4:] == ["--permission-mode", "acceptEdits", "--allowedTools", "Read,Edit,Write,Glob,Grep"]
    assert ".sigma-context/stop0" in first["stdin"] and ".sigma-context/stop1" in second["stdin"]
    ctx = second["ctx"]
    assert ctx[".sigma-context/stop1/message.txt"].startswith("feat: second step")
    for kind in ("base", "ours", "theirs", "history"):
        assert ".sigma-context/stop1/src__app.txt.%s" % kind in ctx
    assert "integration side" in ctx[".sigma-context/stop0/src__app.txt.ours"] or "integration" in ctx[".sigma-context/stop0/src__app.txt.ours"]
    assert "src/app.txt" in ctx[".sigma-context/stop1/earlier.txt"] and "stop0" in ctx[".sigma-context/stop1/earlier.txt"]
    assert "integration moves" in ctx[".sigma-context/stop0/src__app.txt.history"]
    text = base._git(w.local, "show", "origin/%s:%s" % (FEATURE, APP))
    assert "feature side" in text and "feature two" in text and "integration side" in text and "integration two" in text
    assert "<<<<" not in text


# --------------------------------------------------------------------------- 2: build output is kept in the session, not the tree


def test_a_build_artifact_in_the_session_is_not_discarded_and_never_reaches_the_repo(srig, tmp_path):
    srig.mode({"resolve": "build"})
    m, w, report, spy = run_session(srig, tmp_path)
    assert report["outcome"] == m.REBASED, report.get("why")
    first, second = resolves(srig)
    assert first["had_build"] is False and second["had_build"] is True       # kept across stops of the one session
    assert len(spy.pushes()) == 1
    tree = base._git(w.local, "ls-tree", "-r", "--name-only", "origin/" + FEATURE)
    assert "build/out.bin" not in tree and "ran.txt" not in tree


# --------------------------------------------------------------------------- 3: no credentials, no remote


def test_the_session_has_no_credentials_no_remote_and_a_throwaway_home(srig, tmp_path, monkeypatch):
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(name, "secret-value")
    m, w, report, spy = run_session(srig, tmp_path)
    assert report["outcome"] == m.REBASED, report.get("why")
    env = resolves(srig)[0]["env"]
    assert not {"GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK", "ANTHROPIC_API_KEY"} & set(env)
    assert "secret-value" not in json.dumps(env)
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["HOME"].startswith(str(srig.tmp / "scratch")) and resolves(srig)[0]["remote"].strip() == ""


# --------------------------------------------------------------------------- 4: a link is refused


def test_a_link_in_place_of_a_conflicted_file_is_refused_and_nothing_is_written(srig, tmp_path):
    outside = tmp_path / "outside.txt"
    srig.mode({"resolve": "link", "outside": str(outside)})
    w = two_commit_world(mk(tmp_path / "w"))
    before = w.tip(FEATURE)
    m, w, report, spy = run_session(srig, tmp_path, world=w)
    assert w.tip(FEATURE) == before and not spy.pushes()
    assert "write-back-failed" in report["why"] and "symbolic link" in report["why"]
    assert [c["role"] for c in srig.calls()] == ["resolve"]


# --------------------------------------------------------------------------- 5: the proofs still hold


def test_a_dropped_unit_line_in_a_session_is_parked_with_the_attempted_diff(srig, tmp_path):
    srig.mode({"resolve": "drop"})
    w = two_commit_world(mk(tmp_path / "w"))
    before = w.tip(FEATURE)
    m, w, report, spy = run_session(srig, tmp_path, world=w)
    assert report["outcome"] in (m.CONFLICT, m.PARKED) and not spy.pushes() and w.tip(FEATURE) == before
    assert "unit-line-dropped" in report["why"]
    brief = report["park"]["brief"]
    assert "attempted resolution" in brief and "-feature side" in brief
    assert set(c["role"] for c in srig.calls()) == {"resolve"}              # refused proof retried in the session (#1692), never reviewed


# --------------------------------------------------------------------------- 6: the whole-session clock


def test_the_whole_session_shares_one_deadline_and_none_left_parks_as_session_timeout(srig, tmp_path, monkeypatch):
    offset = [0.0]
    real = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real() + offset[0])      # read when the level 2 module is loaded

    def jump(argv):
        if "--continue" in argv:
            offset[0] += 10 ** 5
    m = l1.mod()
    spy = l1.Spy(m, before=jump)
    w = two_commit_world(mk(tmp_path / "w"))
    before = w.tip(FEATURE)
    m, w, report, spy = run_session(srig, tmp_path, world=w, spy=spy)
    assert "session-timeout" in report["why"] and not spy.pushes() and w.tip(FEATURE) == before
    assert len(resolves(srig)) == 1                                           # stop 1 was never spawned


# --------------------------------------------------------------------------- 7: classic path unchanged / network prefix


def test_a_plan_without_the_session_key_runs_the_plain_export_as_before(srig, tmp_path):
    w = l1.world(mk(tmp_path / "w"), feat={APP: "line1\nfeature side\nline3\n"}, integ={APP: "line1\nintegration side\nline3\n"},
                 base_files={"CHANGELOG.md": l1.LOG % "", APP: "line1\nshared\nline3\n"})
    m, w, report, spy = run_session(srig, tmp_path, world=w, session=False)
    assert report["outcome"] == m.REBASED, report.get("why")
    call = resolves(srig)[0]
    assert call["cwd"].endswith(os.sep + "stop0") and "--permission-mode" not in call["argv"] and call["ctx"] == {}
    assert "GIT_CONFIG_NOSYSTEM" not in call["env"]


def test_the_network_prefix_is_put_in_front_of_the_command(srig, tmp_path):
    marker = tmp_path / "wall-ran"
    wall = tmp_path / "wall.py"
    wall.write_text("#!%s\nimport os, sys\nopen(%r, 'w').write('x')\nos.execv(sys.argv[1], sys.argv[1:])\n" % (sys.executable, str(marker)))
    wall.chmod(0o755)
    m, w, report, spy = run_session(srig, tmp_path, net_prefix=[str(wall)])
    assert report["outcome"] == m.REBASED, report.get("why")
    assert marker.exists()


def test_wall_per_platform_and_profile_text(tmp_path):
    launcher = base._load("feature_upkeep_launcher")
    have = lambda name: "/usr/bin/" + name  # noqa: E731
    none = lambda name: None  # noqa: E731
    got, label = launcher.session_wall("darwin", have, "/fakehome", "/tmp/s", ["/tmp/scr"])
    assert label == "sandbox-exec" and got[:2] == ["/usr/bin/sandbox-exec", "-p"]
    prof = got[2]
    for d in (".ssh", ".config/gh", ".git-credentials", ".netrc", "Library/Keychains", ".aws"):
        assert '(subpath "/fakehome/%s")' % d in prof
    assert "(deny file-read*" in prof and '(subpath "/tmp/s")' in prof and '(subpath "/tmp/scr")' in prof
    assert '(remote tcp "*:443")' in prof and "(deny network*)" in prof
    assert launcher.session_wall("darwin", none, "/fakehome", "/tmp/s") == (None, None)
    assert launcher.session_wall("linux", have, "/home/x", "/tmp/s") == (None, None)
    assert launcher.session_wall("win32", have, "/home/x", "/tmp/s") == (None, None)


def test_without_a_wall_the_session_has_no_bash_with_one_it_does(srig, tmp_path):
    m, w, report, spy = run_session(srig, tmp_path)
    assert report["outcome"] == m.REBASED, report.get("why")
    argv = resolves(srig)[0]["argv"]
    assert "Read,Edit,Write,Glob,Grep" in argv and not any("Bash" in a for a in argv)
    wall = tmp_path / "wall.py"
    wall.write_text("#!%s\nimport os, sys\nos.execv(sys.argv[1], sys.argv[1:])\n" % sys.executable)
    wall.chmod(0o755)
    m, w, report, spy = run_session(srig, tmp_path / "b", net_prefix=[str(wall)])
    assert report["outcome"] == m.REBASED, report.get("why")
    argv = resolves(srig)[-1]["argv"]
    assert "Read,Edit,Write,Glob,Grep,Bash" in argv


def test_the_shipped_plan_takes_its_prefix_from_the_wall(wired, monkeypatch, tmp_path):
    launcher = wired._launcher()
    monkeypatch.setattr(launcher, "session_wall", lambda platform, which, home, session_dir, extra=(): (["/w/wrap", "-p", "x"], "sandbox-exec"))
    assert wired._level1_options(agent_cfg(), str(tmp_path), str(tmp_path))["level2"]["net_prefix"] == ["/w/wrap", "-p", "x"]
    monkeypatch.setattr(launcher, "session_wall", lambda platform, which, home, session_dir, extra=(): (None, None))
    assert wired._level1_options(agent_cfg(), str(tmp_path), str(tmp_path))["level2"]["net_prefix"] is None


# --------------------------------------------------------------------------- #1692: every shape a person could resolve

SHAPES = ("package.json", "requirements.txt", "uv.lock", ".github/workflows/ci.yml", "db/migrations/0001.sql")


def _one_conflict(tmp, path):
    return l1.world(mk(tmp), feat={path: "line1\nfeature side\nline3\n"}, integ={path: "line1\nintegration side\nline3\n"},
                    base_files={"CHANGELOG.md": l1.LOG % "", path: "line1\nshared\nline3\n"})


@pytest.mark.parametrize("path", SHAPES)
def test_1692_a_session_conflict_in_a_manifest_lockfile_workflow_or_migration_is_resolved_and_pushed_once(srig, tmp_path, path):
    w = _one_conflict(tmp_path / "w", path)
    m, w, report, spy = run_session(srig, tmp_path, world=w)
    assert report["outcome"] == m.REBASED and report["resolved"]["level"] == 2, report.get("why")
    assert len(spy.pushes()) == 1 and report["backup"] and len(resolves(srig)) == 1
    text = base._git(w.local, "show", "origin/%s:%s" % (FEATURE, path))
    assert "feature side" in text and "integration side" in text and "<<<<" not in text


def test_1692_the_plain_export_still_parks_a_protected_path_before_any_spawn(srig, tmp_path):
    """Legacy (no `session` key) is unchanged: the same package.json conflict is refused and the resolver never runs."""
    w = _one_conflict(tmp_path / "w", "package.json")
    m, w, report, spy = run_session(srig, tmp_path, world=w, session=False)
    assert "protected-path" in report["why"] and resolves(srig) == [] and not spy.pushes()


def test_1692_an_agent_file_the_session_copy_cannot_hold_is_parked_with_the_reason(srig, tmp_path):
    w = _one_conflict(tmp_path / "w", "CLAUDE.md")
    m, w, report, spy = run_session(srig, tmp_path, world=w)
    assert "protected-path" in report["why"] and "session copy" in report["why"] and resolves(srig) == []


def _delete_in_base(w, path):
    base._git(w.local, "checkout", "-q", l1.MAIN)
    base._git(w.local, "rm", "-q", path)
    l1._commit(w.local, "chore: remove the file")
    base._git(w.local, "push", "-q", "origin", l1.MAIN)


def test_1692_a_delete_modify_stop_reaches_the_resolver_and_the_unit_edit_is_kept(srig, tmp_path):
    w = l1.world(mk(tmp_path / "w"), feat={APP: "line1\nfeature side\nline3\n"}, integ={"other.txt": "x\n"},
                 base_files={"CHANGELOG.md": l1.LOG % "", APP: "line1\nshared\nline3\n"})
    _delete_in_base(w, APP)
    m, w, report, spy = run_session(srig, tmp_path, world=w)
    assert "structural-conflict" not in str(report.get("why")) and len(resolves(srig)) == 1
    assert report["outcome"] == m.REBASED, report.get("why")
    assert "feature side" in base._git(w.local, "show", "origin/%s:%s" % (FEATURE, APP))
    stages = resolves(srig)[0]["ctx"]
    assert ".sigma-context/stop0/src__app.txt.theirs" in stages and ".sigma-context/stop0/src__app.txt.ours" not in stages


def test_1692_a_resolver_that_deletes_the_file_is_parked_with_the_proof_reason_not_a_shape_code(srig, tmp_path):
    srig.mode({"rm": [APP]})
    w = l1.world(mk(tmp_path / "w"), feat={APP: "line1\nfeature side\nline3\n"}, integ={"other.txt": "x\n"},
                 base_files={"CHANGELOG.md": l1.LOG % "", APP: "line1\nshared\nline3\n"})
    _delete_in_base(w, APP)
    before = w.tip(FEATURE)
    m, w, report, spy = run_session(srig, tmp_path, world=w)
    assert not spy.pushes() and w.tip(FEATURE) == before
    assert "unit-line-dropped" in report["why"] and "structural-conflict" not in report["why"], report["why"]


def test_1692_an_add_add_stop_reaches_the_resolver_and_both_sides_are_kept(srig, tmp_path):
    w = l1.world(mk(tmp_path / "w"), feat={"src/new.txt": "ours line\n"}, integ={"src/new.txt": "theirs line\n"})
    m, w, report, spy = run_session(srig, tmp_path, world=w)
    assert report["outcome"] == m.REBASED, report.get("why")
    assert len(resolves(srig)) == 1 and len(spy.pushes()) == 1
    text = base._git(w.local, "show", "origin/%s:src/new.txt" % FEATURE)
    assert "ours line" in text and "theirs line" in text


def test_1692_a_refused_proof_is_fed_back_to_the_same_session_and_the_retry_resolves(srig, tmp_path):
    srig.mode({"resolve": "drop_first"})
    w = _one_conflict(tmp_path / "w", APP)
    m, w, report, spy = run_session(srig, tmp_path, world=w)
    assert report["outcome"] == m.REBASED, report.get("why")
    first, second = resolves(srig)
    assert first["cwd"] == second["cwd"] and "was refused" not in first["stdin"]
    assert "was refused" in second["stdin"] and "unit-line-dropped" in second["stdin"]
    assert len(spy.pushes()) == 1
    assert "feature side" in base._git(w.local, "show", "origin/%s:%s" % (FEATURE, APP))


def test_1692_a_proof_still_refused_after_the_retries_parks_and_names_why_the_last_try_failed(srig, tmp_path):
    level2 = base._load("feature_upkeep_level2")
    srig.mode({"resolve": "drop"})
    w = _one_conflict(tmp_path / "w", APP)
    before = w.tip(FEATURE)
    m, w, report, spy = run_session(srig, tmp_path, world=w)
    assert not spy.pushes() and w.tip(FEATURE) == before
    assert len(resolves(srig)) == 1 + level2.SESSION_RETRIES and [c["role"] for c in srig.calls()].count("review") == 0
    assert "unit-line-dropped" in report["why"] and "try %d of %d" % (1 + level2.SESSION_RETRIES, 1 + level2.SESSION_RETRIES) in report["why"]


def test_1692_no_time_left_for_a_retry_parks_with_the_last_reason(srig, tmp_path, monkeypatch):
    level2 = base._load("feature_upkeep_level2")
    srig.mode({"resolve": "drop"})
    real, calls = time.monotonic, srig.fake / "calls.json"
    in_engine = lambda: sys._getframe(2).f_globals.get("__file__", "").endswith("feature_upkeep_level2.py")  # noqa: E731
    monkeypatch.setattr(time, "monotonic", lambda: real() + (10 ** 6 if calls.exists() and in_engine() else 0))   # only the session clock jumps
    w = _one_conflict(tmp_path / "w", APP)
    m, w, report, spy = run_session(srig, tmp_path, world=w)
    assert len(resolves(srig)) == 1 and "unit-line-dropped" in report["why"] and not spy.pushes()
    assert level2.SESSION_RETRIES >= 1
