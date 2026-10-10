"""#951 (part B, slice 10): Level 2 end to end, driven ONLY by a FAKE `claude` written into tmp_path.

No real model call, no network, no hosting service: real git in scratch repositories with a local bare remote. Nothing
here proves a real model resolves a conflict, and every model flag beyond the launcher's confirmed table stays
UNVERIFIED (the launcher refuses them). The controls are the tests named in CONTROLS; each was run once with the guarded
line broken, seen red, then restored. The documented gesture: `python -m pytest tests/test_upkeep_level2.py -q`.
"""
import importlib.util
import json
import pathlib
import sys

import pytest

import test_feature_rebase as base
import test_upkeep_level1 as l1

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
UNIT, FEATURE = base.UNIT, base.FEATURE
CONTROLS = ("test_leftover_markers_park", "test_a_reply_outside_the_conflicted_set_is_discarded_parked_and_charged",
            "test_protected_path_parks", "test_a_blocking_reviewer_parks_and_pushes_nothing",
            "test_chat_door_closed_gate_is_unchanged")
RESOLVER_CAP, REVIEW_CAP = 2.0, 1.0
APP = "src/app.txt"


def mk(path):
    path.mkdir(parents=True, exist_ok=True)
    return path

FAKE = '''#!%(python)s
import json, os, pathlib, sys
here = pathlib.Path(%(dir)r)
mode = json.loads((here / "mode.json").read_text())
stdin = sys.stdin.read()
reviewer = "independent reviewer" in stdin
calls = json.loads((here / "calls.json").read_text()) if (here / "calls.json").exists() else []
calls.append({"role": "review" if reviewer else "resolve", "cwd": os.getcwd(), "argv": sys.argv[1:], "stdin": stdin})
(here / "calls.json").write_text(json.dumps(calls))
if reviewer:
    print(json.dumps(mode.get("review", {"verdict": "approve", "reasons": ["keeps both sides"]})))
    raise SystemExit(0)
how = mode.get("resolve", "both")
for p in pathlib.Path(".").rglob("*"):
    if p.is_file() and "<<<<<<<" in p.read_text():
        if how == "both":
            p.write_text("".join(l for l in p.read_text().splitlines(True) if l[:7] not in ("<<<<<<<", "=======", ">>>>>>>")))
if how == "outside":
    pathlib.Path("NOTE.txt").write_text("obeyed the hunk")
if how == "fail":
    raise SystemExit(3)
print("done")
'''


class Rig:
    def __init__(self, tmp, monkeypatch):
        self.tmp, self.mp = tmp, monkeypatch
        self.fake = tmp / "fake"
        self.fake.mkdir(parents=True)
        self.binary = self.fake / "claude"
        self.binary.write_text(FAKE % {"python": sys.executable, "dir": str(self.fake)})
        self.binary.chmod(0o755)
        for name in ("scratch", "home", "state", "resolver", "review"):
            (tmp / name).mkdir()
        self.mode({})
        self.limits = None
        self.host = "claude"

    def mode(self, mapping):
        (self.fake / "mode.json").write_text(json.dumps(mapping))

    def calls(self):
        path = self.fake / "calls.json"
        return json.loads(path.read_text()) if path.exists() else []

    def plan(self, w):
        def launch(cap, model, directory=None):
            out = dict(state_dir=str(self.tmp / "state"), repo_root=str(w.local), binary=str(self.binary), flags=(),
                       credential_var=None, scratch_parent=str(self.tmp / "scratch"), rates=None,
                       ceiling_machine_usd=50.0, ceiling_team_usd=50.0, budget_seconds=5.0, home=str(self.tmp / "home"),
                       timeout=30, cap_usd=cap, model=model, sdlc_dir=str(w.sdlc))
            if directory:
                out["directory"] = directory
            return out
        return dict(sdlc_dir=str(w.sdlc), launch=launch(RESOLVER_CAP, "test-model-id-1"),
                    review_launch=launch(REVIEW_CAP, "test-model-id-2", str(self.tmp / "review")), review_cap_usd=REVIEW_CAP,
                    review_model="test-model-id-2", resolver_root=str(self.tmp / "resolver"), limits=self.limits,
                    host=self.host, environ=None)

    def install(self, m, w):
        self.mp.setattr(m, "LEVEL2_PLAN_FACTORY", lambda config: dict(self.plan(w), config=config))


def conflict_world(tmp_path, path=APP, feat=None, integ=None, base_files=None):
    files = base_files if base_files is not None else {"CHANGELOG.md": l1.LOG % "", path: "line1\nshared\nline3\n"}
    return l1.world(tmp_path, feat=feat if feat is not None else {path: "line1\nfeature side\nline3\n"},
                    integ=integ if integ is not None else {path: "line1\nintegration side\nline3\n"}, base_files=files)


def cfg(**kw):
    return l1.cfg(resolve=kw.pop("resolve", "agent"), **kw)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.delenv("SIGMA_UPKEEP_JOB", raising=False)
    return Rig(tmp_path / "rig", monkeypatch)


def go(rig, tmp_path, config=None, world=None, approve_verify=True):
    m = l1.mod()
    w = world or conflict_world(mk(tmp_path / "w"))
    rig.install(m, w)
    if approve_verify:
        l1.verify_ok(m, rig.mp)
    report, spy = l1.run_pass(m, w, config or cfg())
    return m, w, report, spy


# --------------------------------------------------------------------------- end to end


def test_a_source_conflict_is_resolved_proved_reviewed_and_pushed_once_with_a_backup(rig, tmp_path):
    w = conflict_world(mk(tmp_path / "w"))
    before = w.tip(FEATURE)
    m, w, report, spy = go(rig, tmp_path, world=w)
    assert report["outcome"] == m.REBASED and report["resolved"]["level"] == 2, report.get("why")
    assert len(spy.pushes()) == 1 and report["backup"]
    text = base._git(w.local, "show", "origin/%s:%s" % (FEATURE, APP))
    assert "feature side" in text and "integration side" in text and "<<<<" not in text
    assert before in base._git(w.local, "ls-remote", "origin")
    roles = [c["role"] for c in rig.calls()]
    assert roles == ["resolve", "review"]
    assert "sigma-resolution: 2 " in l1.remote_log(w)
    assert all(c["argv"][:3] == ["-p", "--output-format", "json"] for c in rig.calls())   # confirmed flags only
    got = json.loads(next((w.sdlc / "state" / "upkeep" / "level2").glob("*.json")).read_text())
    assert [a["outcome"] for a in got["attempts"]] == ["resolved"] and got["attempts"][0]["charged_usd"] == RESOLVER_CAP
    record = json.loads(next((w.sdlc / "state" / "upkeep" / "resolutions").glob("*.json")).read_text())
    assert record["level"] == 2 and record["verdict"] == "approve" and record["route"] == "headless"
    assert record["cost_usd"] >= RESOLVER_CAP and record["model"] == "test-model-id-1"
    assert not (rig.tmp / "resolver" / "stop0" / ".git").exists()


def test_the_export_holds_no_agent_files_and_no_git_dir(rig, tmp_path):
    files = {"CHANGELOG.md": l1.LOG % "", APP: "line1\nshared\nline3\n", "CLAUDE.md": "agent text\n",
             ".mcp.json": "{}\n", "docs/AGENTS.md": "x\n"}
    w = conflict_world(mk(tmp_path / "w"), base_files=files)
    go(rig, tmp_path, world=w)
    seen = sorted(p.relative_to(rig.tmp / "resolver" / "stop0").as_posix()
                  for p in (rig.tmp / "resolver" / "stop0").rglob("*") if p.is_file())
    assert APP in seen and "CLAUDE.md" not in seen and ".mcp.json" not in seen and "docs/AGENTS.md" not in seen
    assert not any(s.startswith(".git") for s in seen)


def test_leftover_markers_park(rig, tmp_path):
    rig.mode({"resolve": "none"})                                # the fake leaves the markers in place
    w = conflict_world(mk(tmp_path / "w"))
    before = w.tip(FEATURE)
    m, w, report, spy = go(rig, tmp_path, world=w)
    assert report["outcome"] in (m.CONFLICT, m.PARKED) and w.tip(FEATURE) == before and not spy.pushes()
    assert "leftover-markers" in report["why"] and report["level2_charged"] == RESOLVER_CAP


def test_a_reply_outside_the_conflicted_set_is_discarded_parked_and_charged(rig, tmp_path):
    """The drill variant (D-24), run once against the fake: the fake obeys a hostile instruction and writes a new file."""
    rig.mode({"resolve": "outside"})
    w = conflict_world(mk(tmp_path / "w"))
    before = w.tip(FEATURE)
    m, w, report, spy = go(rig, tmp_path, world=w)
    assert w.tip(FEATURE) == before and not spy.pushes() and "resolver-failed" in report["why"]
    assert "discarded" in report["why"] and report["level2_charged"] == RESOLVER_CAP
    assert [c["role"] for c in rig.calls()] == ["resolve"]       # the reviewer was never asked


def test_a_resolver_that_fails_parks_and_is_charged_the_cap(rig, tmp_path):
    rig.mode({"resolve": "fail"})
    m, w, report, spy = go(rig, tmp_path)
    assert not spy.pushes() and report["level2_charged"] == RESOLVER_CAP and "resolver-failed" in report["why"]


def test_a_blocking_reviewer_parks_and_pushes_nothing(rig, tmp_path):
    rig.mode({"review": {"verdict": "block", "reasons": ["drops the integration side"]}})
    w = conflict_world(mk(tmp_path / "w"))
    before = w.tip(FEATURE)
    m, w, report, spy = go(rig, tmp_path, world=w)
    assert w.tip(FEATURE) == before and not spy.pushes() and "reviewer-blocked" in report["why"]
    assert [c["role"] for c in rig.calls()] == ["resolve", "review"]
    assert report["level2_charged"] >= RESOLVER_CAP


def test_a_failing_proof_never_reaches_the_reviewer(rig, tmp_path, monkeypatch):
    m = l1.mod()
    w = conflict_world(mk(tmp_path / "w"))
    rig.install(m, w)
    l1.verify_ok(m, monkeypatch, outcome="failed")
    report, spy = l1.run_pass(m, w, cfg())
    assert not spy.pushes() and "verify" in report["why"] and [c["role"] for c in rig.calls()] == ["resolve"]


# --------------------------------------------------------------------------- eligibility and limits


def test_protected_path_parks(rig, tmp_path):
    w = conflict_world(mk(tmp_path / "w"), path="package.json")
    m, w, report, spy = go(rig, tmp_path, world=w)
    assert "protected-path" in report["why"] and rig.calls() == [] and not spy.pushes()


def test_structural_add_add_parks(rig, tmp_path):
    w = l1.world(mk(tmp_path / "w"), feat={"src/new.txt": "ours\n"}, integ={"src/new.txt": "theirs\n"})
    m, w, report, spy = go(rig, tmp_path, world=w)
    assert "structural-conflict" in report["why"] and rig.calls() == [] and not spy.pushes()


def test_too_many_files_hunks_and_lines_park(rig, tmp_path):
    two = {"CHANGELOG.md": l1.LOG % "", "a.txt": "1\nx\n3\n", "b.txt": "1\nx\n3\n"}
    for limits, code, sub in (({"max_files": 1}, "too-many-files", "a"), ({"max_hunks": 1}, "too-many-hunks", "b"),
                              ({"max_lines": 3}, "too-many-conflict-lines", "c")):
        rig.limits = limits
        w = l1.world(mk(tmp_path / sub), feat={"a.txt": "1\nF\n3\n", "b.txt": "1\nF\n3\n"},
                     integ={"a.txt": "1\nI\n3\n", "b.txt": "1\nI\n3\n"}, base_files=two)
        m, w, report, spy = go(rig, tmp_path, world=w)
        assert code in report["why"], (code, report["why"])
    assert rig.calls() == []


def test_a_merge_commit_stop_is_never_resolved():
    level2 = base._load("feature_upkeep_level2")

    def run(cwd, argv):
        if "MERGE_HEAD" in argv:
            return "abc123"
        raise AssertionError("nothing may be read after a merge stop: %r" % (argv,))
    got = level2.eligibility(run, "/nowhere", None)
    assert (got.ok, got.code) == (False, "merge-commit-stop")


def test_non_claude_host_parks_without_a_spawn(rig, tmp_path):
    rig.host = "codex"
    m, w, report, spy = go(rig, tmp_path)
    assert "not-a-claude-host" in report["why"] and rig.calls() == [] and not spy.pushes()


def test_attempts_are_bounded_and_the_last_one_parks_without_a_spawn(rig, tmp_path):
    w = conflict_world(mk(tmp_path / "w"))
    level2 = base._load("feature_upkeep_level2")
    for n in range(2):
        level2.begin_attempt(str(w.sdlc), UNIT, "run%d" % n, 1000 + n)
        level2.settle_attempt(str(w.sdlc), UNIT, "run%d" % n, "markers", 2.0)
    assert level2.attempts_used(str(w.sdlc), UNIT) == 2 and level2.total_charged(str(w.sdlc), UNIT) == 4.0
    m, w, report, spy = go(rig, tmp_path, world=w)
    assert "attempts-exhausted" in report["why"] and rig.calls() == []


def test_the_attempt_list_is_bounded(tmp_path):
    level2 = base._load("feature_upkeep_level2")
    for n in range(level2.MAX_ATTEMPT_ENTRIES + 7):
        level2.begin_attempt(str(tmp_path), UNIT, "r%d" % n, n)
    assert len(level2.read_attempts(str(tmp_path), UNIT)) == level2.MAX_ATTEMPT_ENTRIES


def test_an_unreadable_attempt_store_fails_towards_parking(tmp_path):
    level2 = base._load("feature_upkeep_level2")
    path = level2.store_path(str(tmp_path), UNIT)
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert level2.attempts_used(str(tmp_path), UNIT) >= level2.DEFAULT_LIMITS["max_attempts"]


# --------------------------------------------------------------------------- the gate closed


def test_gate_closed_or_level_not_agent_touches_nothing(rig, tmp_path):
    tripped = []
    m = l1.mod()
    w = conflict_world(mk(tmp_path / "w"))
    rig.mp.setattr(m, "LEVEL2_PLAN_FACTORY", lambda config: tripped.append(1))
    for config in (cfg(open_=False), cfg(resolve="off"), cfg(resolve="mechanical")):
        assert (m._level1_options(config) or {}).get("level2") is None
        report, spy = l1.run_pass(m, w, config)
        assert report["outcome"] in (m.CONFLICT, m.PARKED) and "resolved" not in report
    assert tripped == [] and rig.calls() == []
    assert not (w.sdlc / "state" / "upkeep" / "level2").exists() and not any((rig.tmp / "resolver").iterdir())


def test_the_shipped_engine_has_no_level2_plan_so_agent_level_alone_changes_nothing():
    m = l1.mod()
    assert m.LEVEL2_PLAN_FACTORY is None and m._level1_options(cfg())["level2"] is None


# --------------------------------------------------------------------------- the chat door ruling (D-23)


def _chat(monkeypatch, config):
    spec = importlib.util.spec_from_file_location("slack_commands_listen_t", SCRIPTS / "slack_commands_listen.py")
    sc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sc)
    seen = {}

    def dispatch(*args, **kwargs):
        seen["kwargs"] = kwargs
        return type("R", (), {"state": "done", "detail": "ok", "holder_actor": None})()
    monkeypatch.setattr(sc, "dispatch", dispatch)
    sc._rebase_reply("/nowhere", config, "voice")
    return sc, seen["kwargs"]


def test_chat_door_closed_gate_is_unchanged(monkeypatch):
    sc, kwargs = _chat(monkeypatch, {})
    assert kwargs == {"run_drive": None, "run": None, "extra_prompt": sc._REBASE_EXTRA_PROMPT, "session_pid": None}
    assert "reported, never resolved" in sc._REBASE_EXTRA_PROMPT or "do NOT resolve it" in sc._REBASE_EXTRA_PROMPT


def test_chat_door_open_gate_says_the_engine_owns_resolution_and_bounds_the_lock(monkeypatch):
    sc, kwargs = _chat(monkeypatch, {"upkeep": {"enabled": True}})
    assert kwargs["extra_prompt"] == sc._REBASE_EXTRA_PROMPT + sc.GATED_REBASE_PROMPT_SUFFIX
    assert "upkeep engine" in kwargs["extra_prompt"] and "never resolve" in kwargs["extra_prompt"]
    assert kwargs["timeout"] == sc.GATED_REBASE_TIMEOUT_SECONDS < 2 * 3600


# --------------------------------------------------------------------------- the module itself


def test_the_launcher_still_refuses_every_unverified_flag_and_nothing_here_adds_one():
    launcher = base._load("feature_upkeep_launcher")
    src = (SCRIPTS / "feature_upkeep_level2.py").read_text()
    for flag in launcher.UNVERIFIED_FLAGS:
        assert flag not in src.replace(launcher.__name__, ""), flag
    assert "UNVERIFIED" in src and "PROVISIONAL" in src and "NO REAL MODEL CALL" in src


def test_count_conflicts_and_protected_table():
    level2 = base._load("feature_upkeep_level2")
    assert level2.count_conflicts("a\n<<<<<<< x\nb\n=======\nc\n>>>>>>> y\nd\n") == (1, 5)
    for name in (".github/workflows/ci.yml", "a/migrations/001.sql", "Cargo.lock", "x/CLAUDE.md", "evals/a.py", ".sdlc/x.md"):
        assert level2.is_protected(name), name
    assert not level2.is_protected("src/app.txt")
