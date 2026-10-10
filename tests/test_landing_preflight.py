"""#974: the doctor's verify-interpreter row and branch-creation ruleset rows (landing_preflight).

The GitHub replies are FAKED from the documented shape; the real reply was not seen live. Names are synthetic."""
import importlib.util
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
MODULE = ROOT / "skills" / "sigma-loop" / "scripts" / "landing_preflight.py"
DOCTOR = ROOT / "skills" / "sigma-doctor" / "scripts" / "doctor.py"
GH_API = ROOT / "skills" / "sigma-loop" / "scripts" / "gh_api.py"
MISSING = "ghost-interp"
IFACE = "interpreter is on PATH"


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path_factory):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path_factory.mktemp("claude-config")))
    for var in ("CODEX_SESSION_ID", "CODEX_THREAD_ID", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _need_module():
    assert MODULE.is_file(), "landing_preflight.py does not exist yet"


def _project(tmp_path, command="%s -m pytest" % MISSING):
    base = tmp_path / "proj" / ".sdlc"
    base.mkdir(parents=True, exist_ok=True)
    cfg = {"work": {"enabled": True}, "verify": {"command": command}}
    (base / "config.json").write_text(json.dumps(cfg))
    return str(base)


def _which(name):
    return None if name == MISSING else "/bin/" + name


def _fake(rules=None, rulesets=None, remote="github.com/o/r.git", fail=False, calls=None):
    def run(args):
        if calls is not None:
            calls.append(list(args))
        if args[0] == "git":
            return remote
        if args[:2] == ["gh", "api"]:
            if fail:
                return ""
            ep = args[2]
            if "/rules/branches/" in ep:
                return json.dumps(rules if rules is not None else [])
            if "/rulesets/" in ep:
                return json.dumps((rulesets or {}).get(ep))
        return ""
    return run


def _rule(rid=7, kind="creation", **extra):
    return dict({"type": kind, "ruleset_source_type": "Repository", "ruleset_source": "o/r",
                 "ruleset_id": rid}, **extra)


def _rows(tmp_path, run, command="%s -m pytest" % MISSING, **kw):
    d = _load(DOCTOR, "doctor_974")
    return d.check(_project(tmp_path, command), run=run, which=_which, **kw)


def _named(rows, word):
    return [r for r in rows if word in r["name"]]


def test_missing_interpreter_is_reported_by_the_doctor_row(tmp_path):
    _need_module()
    hit = _named(_rows(tmp_path, _fake()), IFACE)
    assert len(hit) == 1 and hit[0]["ok"] is False and MISSING in hit[0]["fix"], hit
    assert "absolute interpreter path" in hit[0]["fix"]
    assert not _named(_rows(tmp_path, _fake(), command="python3 -m pytest"), IFACE)


def test_blocking_ruleset_is_named_by_the_doctor_row(tmp_path):
    _need_module()
    sets = {"repos/o/r/rulesets/4242": {"name": "protect-sdlc", "current_user_can_bypass": "never"}}
    rows = _rows(tmp_path, _fake([_rule(4242), _rule(4242)], sets), command="python3 -m pytest")
    hit = _named(rows, "protect-sdlc")
    assert len(hit) == 1 and hit[0]["ok"] is False, hit
    assert "current_user_can_bypass" in hit[0]["fix"] and "bypass actor" in hit[0]["fix"]
    assert "4242" not in hit[0]["name"] + hit[0]["fix"]


def test_bypass_actor_and_healthy_host_stay_silent(tmp_path):
    _need_module()
    for state in ("always", "exempt"):
        sets = {"repos/o/r/rulesets/7": {"name": "protect-sdlc", "current_user_can_bypass": state}}
        rows = _rows(tmp_path, _fake([_rule()], sets), command="python3 -m pytest")
        assert not _named(rows, "protect-sdlc") and not _named(rows, "could not check"), state
    rows = _rows(tmp_path, _fake([]), command="python3 -m pytest")
    assert not _named(rows, "branch creation") and not _named(rows, "could not check")


def test_unreadable_rules_degrade_to_could_not_check(tmp_path):
    _need_module()
    cases = [_fake(fail=True), _fake(remote=""), _fake({"not": "a list"}),
             _fake([_rule()], {"repos/o/r/rulesets/7": {"name": "protect-sdlc"}})]
    for run in cases:
        rows = _rows(tmp_path, run, command="python3 -m pytest")
        hit = _named(rows, "could not check")
        assert len(hit) == 1 and hit[0]["ok"] is True and hit[0]["fix"], hit
        assert not [r for r in _named(rows, "branch creation") if r["ok"] is False]


def test_cheap_only_makes_no_ruleset_calls(tmp_path):
    _need_module()
    calls = []
    _rows(tmp_path, _fake([_rule()], calls=calls), cheap_only=True)
    assert not [c for c in calls if c[0] == "gh" or "get-url" in c], calls


def test_work_disabled_makes_no_ruleset_calls(tmp_path):
    _need_module()
    calls = []
    base = tmp_path / "off" / ".sdlc"
    base.mkdir(parents=True)
    (base / "config.json").write_text(json.dumps({"discovery": {"source": "github"}, "work": {"enabled": False}, "verify": {"command": "python3 x"}}))
    rows = _load(DOCTOR, "doctor_974_off").check(str(base), run=_fake([_rule()], calls=calls), which=_which)
    assert not [c for c in calls if "rules/branches" in " ".join(c) or "rulesets" in " ".join(c) or "get-url" in c], calls
    assert not _named(rows, "branch creation") and not _named(rows, "rulesets")


def test_org_sourced_rule_reads_the_org_endpoint(tmp_path):
    _need_module()
    calls = []
    rule = _rule(ruleset_source_type="Organization", ruleset_source="org1")
    sets = {"orgs/org1/rulesets/7": {"name": "org-guard", "current_user_can_bypass": "never"}}
    rows = _rows(tmp_path, _fake([rule], sets, calls=calls), command="python3 -m pytest")
    assert _named(rows, "org-guard"), rows


def test_interpreter_shapes_stay_silent():
    _need_module()
    lp = _load(MODULE, "landing_preflight_974")
    none = lambda n: None
    for cmd in ("cd x && make", "FOO=1 $PY -m pytest", "~/bin/run", "export A=1", "sudo make", "(make)",
                "./local/run", "echo `x`", "'unterminated", "", "! false", "time make", "env", "env -i ghost x", "env -u A ghost x",
                "FOO=1 env -S 'a b'", "timeout 5 ghost"):
        assert lp.interpreter_missing(cmd, none) is None, cmd
    assert lp.interpreter_missing("FOO=1 env BAR=2 ghost x", none) == "ghost"
    assert lp.interpreter_missing("/nonexistent/dir/py -m x", none) == "/nonexistent/dir/py"
    assert lp.interpreter_missing("python3 x", lambda n: "/bin/" + n) is None


def test_gh_api_ruleset_validates_and_is_read_only():
    _need_module()
    g = _load(GH_API, "gh_api_974")
    seen = []

    def run(args):
        seen.append(args)
        return json.dumps({"name": "x"})
    assert g.ruleset(run, "o/r", 7) == {"name": "x"}
    assert "--method" in seen[0] and seen[0][seen[0].index("--method") + 1] == "GET"
    for bad in ("7", 0, -1, True, None):
        with pytest.raises(Exception):
            g.ruleset(run, "o/r", bad)
    with pytest.raises(Exception):
        g.ruleset(lambda a: "[1]", "o/r", 7)
