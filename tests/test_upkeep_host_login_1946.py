"""#1946: `upkeep.conflicts.agent.login` "host" -- the agent level runs on the watcher's own login. Driven only by a FAKE `claude`
(no real model, no network). The recorded real-model run is owner-run after landing.
Controls (each broken once, seen red, restored): the host branch ignored in `build_env`, a GH_TOKEN let through in host mode, the
preflight built from the scrubbed env, the resolver-model cap, the reviewer-model cap, the `login` pass-through in the plan.
Gesture: `python -m pytest tests/test_upkeep_host_login_1946.py -q`."""
import json
import os
import pathlib
import sys

import pytest

import test_doctor_resolver_readiness as R
import test_upkeep_launcher as L
import upkeep_support as S
from test_upkeep_agent_1516 import agent_cfg, wired  # noqa: F401  (wired is a fixture)
from test_upkeep_launcher import rig as lrig  # noqa: F401
from test_upkeep_level2 import rig  # noqa: F401  (rig is a fixture)

HOST_HOME = "host-home"
HOST_CFG = "host-config"
LEAKS = ("GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK", "GIT_ASKPASS", "GIT_SSH_COMMAND", "GITHUB_PAT")

# auth status prints a plan type and an email and a token-looking field; the launcher must name none of the last two
STUB = '''#!%(python)s
import json, os, pathlib, sys
here = pathlib.Path(%(dir)r)
if sys.argv[1:3] == ["auth", "status"]:
    (here / "auth.json").write_text(json.dumps(dict(os.environ)))
    print(json.dumps({"loggedIn": True, "subscriptionType": "max", "email": "someone@example.invalid", "to" + "ken": "not-a-" + "real-value"}))
    raise SystemExit(0)
sys.stdin.read()
(here / "run.json").write_text(json.dumps({"env": dict(os.environ), "argv": sys.argv[1:]}))
print("done")
'''


def host_environ(tmp_path, **extra):
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path / HOST_HOME), "CLAUDE_CONFIG_DIR": str(tmp_path / HOST_CFG)}
    env.update({name: "must-not-travel" for name in LEAKS})
    env.update(extra)
    return env


def stubbed(rig_):
    rig_.binary.write_text(STUB % {"python": sys.executable, "dir": str(rig_.fakedir)})
    rig_.binary.chmod(0o755)


def recorded(rig_, name):
    return json.loads((rig_.fakedir / name).read_text())


# --------------------------------------------------------------------------- the key

@pytest.mark.parametrize("value,ok", [("scrubbed", True), ("host", True), ("Host", False), ("", False), (None, False), (True, False), ("both", False)])
def test_login_accepts_the_two_words_only(value, ok):
    assert (S.gate().check("conflicts.agent.login", value) is None) is ok


def test_login_defaults_to_scrubbed_and_the_template_equals_the_defaults():
    assert S.gate().DEFAULTS["conflicts.agent.login"] == "scrubbed"
    assert S.parse_template()["upkeep"]["conflicts"]["agent"]["login"] == "scrubbed"


# --------------------------------------------------------------------------- the environment

def test_the_scrubbed_environment_is_unchanged_when_login_is_unset_or_scrubbed(tmp_path):
    launcher = L.load()
    scratch = tmp_path / "s"
    environ = host_environ(tmp_path, LANG="C")
    baseline = launcher.build_env(scratch, None, environ)
    assert baseline == launcher.build_env(scratch, None, environ, None) == launcher.build_env(scratch, None, environ, "scrubbed")
    assert baseline["HOME"] == str(scratch / "home") and baseline["CLAUDE_CONFIG_DIR"] == str(scratch / "config")
    assert set(baseline) == {"PATH", "LANG", "TMPDIR", "HOME", "CLAUDE_CONFIG_DIR", "GIT_TERMINAL_PROMPT"}


def test_the_host_environment_carries_the_watchers_home_and_config_and_no_git_gh_or_ssh_variable(tmp_path):
    launcher = L.load()
    scratch = tmp_path / "s"
    env = launcher.build_env(scratch, None, host_environ(tmp_path), "host")
    assert env["HOME"] == str(tmp_path / HOST_HOME) and env["CLAUDE_CONFIG_DIR"] == str(tmp_path / HOST_CFG)
    assert env["TMPDIR"] == str(scratch / "tmp")
    assert not [k for k in env if k in LEAKS or k.startswith(("GIT_CONFIG", "GH_", "SSH_", "GITHUB_"))]
    assert "must-not-travel" not in json.dumps(env)


def test_the_host_home_falls_back_to_the_request_home_and_leaves_the_config_dir_unset_when_the_watcher_has_none(tmp_path):
    launcher = L.load()
    env = launcher.build_env(tmp_path / "s", None, {"PATH": "/bin"}, "host", "/some/home")
    assert env["HOME"] == "/some/home" and "CLAUDE_CONFIG_DIR" not in env


def test_credential_var_is_still_honoured_in_host_mode(tmp_path):
    launcher = L.load()
    env = launcher.build_env(tmp_path / "s", "MY_LOGIN", host_environ(tmp_path, MY_LOGIN="v"), "host")
    assert env["MY_LOGIN"] == "v"


# --------------------------------------------------------------------------- the launch

def test_a_host_launch_runs_the_preflight_and_the_session_on_the_host_login(lrig):
    stubbed(lrig)
    tmp = lrig.tmp
    result = lrig.launcher.launch(lrig.request(login="host"), environ=host_environ(tmp))
    assert result.outcome in ("ok", "failed") and "not logged in" not in result.reason, result
    for seen in (recorded(lrig, "auth.json"), recorded(lrig, "run.json")["env"]):
        assert seen["HOME"] == str(tmp / HOST_HOME) and seen["CLAUDE_CONFIG_DIR"] == str(tmp / HOST_CFG)
        assert not [k for k in LEAKS if k in seen]
        assert seen["TMPDIR"].startswith(str(tmp / "scratch"))


def test_a_scrubbed_launch_still_gets_a_throwaway_home_in_the_preflight_and_the_run(lrig):
    stubbed(lrig)
    lrig.launcher.launch(lrig.request(), environ=host_environ(lrig.tmp))
    for seen in (recorded(lrig, "auth.json"), recorded(lrig, "run.json")["env"]):
        assert seen["HOME"].startswith(str(lrig.tmp / "scratch")) and seen["CLAUDE_CONFIG_DIR"].startswith(str(lrig.tmp / "scratch"))


def test_a_host_launch_that_is_logged_out_parks_naming_the_host_login(lrig):
    lrig.binary.write_text("#!%s\nimport sys\nraise SystemExit(1 if sys.argv[1:3] == ['auth','status'] else 0)\n" % sys.executable)
    lrig.binary.chmod(0o755)
    result = lrig.launcher.launch(lrig.request(login="host"), environ=host_environ(lrig.tmp))
    assert result.outcome == "refused" and result.reason == lrig.launcher.NOT_LOGGED_IN_HOST and "host" in result.reason


def test_a_host_launch_is_charged_the_cap_not_a_transcript_priced_from_the_shared_store(lrig):
    stubbed(lrig)
    config = pathlib.Path(lrig.tmp / HOST_CFG / "projects" / "x")
    config.mkdir(parents=True)
    usage = {"input_tokens": 100000, "output_tokens": 0, "cache_read_input_tokens": 0,
             "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0}}
    (config / "other.jsonl").write_text(json.dumps({"type": "assistant", "timestamp": "2026-09-01T00:00:00Z", "message": {
        "role": "assistant", "model": "claude-opus-5", "id": "m1", "usage": usage}}) + "\n")
    result = lrig.launcher.launch(lrig.request(login="host"), environ=host_environ(lrig.tmp))
    assert result.charged_usd == pytest.approx(L.CAP)          # not METERED: another session's transcript is never priced


def test_auth_info_names_only_the_account_type(tmp_path):
    launcher = L.load()
    binary = tmp_path / "claude"
    binary.write_text(STUB % {"python": sys.executable, "dir": str(tmp_path)})
    binary.chmod(0o755)
    got = launcher.auth_info({"PATH": os.environ["PATH"]}, str(binary), str(tmp_path), 10)
    assert got == (True, "max")
    assert "example.invalid" not in repr(got) and "tok-secret" not in repr(got)
    binary.write_text("#!%s\nprint('{\"loggedIn\": true, \"subscriptionType\": \"a@b.c/xyz\"}')\n" % sys.executable)
    assert launcher.auth_info({"PATH": os.environ["PATH"]}, str(binary), str(tmp_path), 10) == (True, None)


# --------------------------------------------------------------------------- the shipped plan

def test_the_shipped_plan_passes_host_login_to_resolver_and_reviewer_and_none_by_default(wired, tmp_path):
    plan = wired._default_level2_plan(agent_cfg(login="host"), sdlc_dir=str(tmp_path))
    assert plan["launch"]["login"] == "host" and plan["review_launch"]["login"] == "host"
    plan = wired._default_level2_plan(agent_cfg(), sdlc_dir=str(tmp_path))
    assert plan["launch"]["login"] is None and plan["review_launch"]["login"] is None


def _plan_models(wired, tmp_path, ceiling, **agent):
    cfg = agent_cfg(**agent)
    if ceiling is not None:
        cfg["model_selection_max_tier"] = ceiling
    plan = wired._default_level2_plan(cfg, sdlc_dir=str(tmp_path))
    ids = wired._launcher().CATALOG
    inv = {v: k for k, v in ids.items()}
    return inv.get(plan["launch"]["model"], plan["launch"]["model"]), inv.get(plan["review_launch"]["model"], plan["review_launch"]["model"])


def test_a_sonnet_ceiling_never_escalates_the_resolver_or_the_reviewer(wired, tmp_path, monkeypatch):
    cat = wired._launcher().CATALOG
    monkeypatch.setitem(cat, "sonnet", "claude-sonnet-x")
    monkeypatch.setitem(cat, "opus", "claude-opus-x")
    got = _plan_models(wired, tmp_path, "sonnet", model="opus", reviewer={"enabled": True, "model": "opus"})
    assert got == ("sonnet", "sonnet")


def test_a_lower_choice_is_kept_and_the_default_ceiling_is_opus(wired, tmp_path, monkeypatch):
    cat = wired._launcher().CATALOG
    monkeypatch.setitem(cat, "opus", "claude-opus-x")
    monkeypatch.setitem(cat, "haiku", "claude-haiku-x")
    assert _plan_models(wired, tmp_path, "sonnet", model="haiku", reviewer={"enabled": True, "model": "haiku"}) == ("haiku", "haiku")
    assert _plan_models(wired, tmp_path, None, model="opus", reviewer={"enabled": True, "model": "opus"}) == ("opus", "opus")
    assert _plan_models(wired, tmp_path, "fable", model="opus", reviewer={"enabled": True, "model": "opus"}) == ("opus", "opus")
    assert _plan_models(wired, tmp_path, "not-a-tier", model="fable", reviewer={"enabled": True, "model": "fable"}) == ("opus", "opus")


def test_a_sonnet_ceiling_caps_the_resolver_alone_and_the_reviewer_alone(wired, tmp_path, monkeypatch):
    cat = wired._launcher().CATALOG
    monkeypatch.setitem(cat, "opus", "claude-opus-x")
    assert _plan_models(wired, tmp_path, "sonnet", model="opus", reviewer={"enabled": True, "model": "sonnet"}) == ("sonnet", "sonnet")
    assert _plan_models(wired, tmp_path, "sonnet", model="sonnet", reviewer={"enabled": True, "model": "opus"}) == ("sonnet", "sonnet")


# --------------------------------------------------------------------------- the doctor

def _auth_rows(tmp_path, login, runner):
    d, cfg = R.doctor(), R.config()
    cfg["upkeep"]["conflicts"]["agent"]["login"] = login
    return d._resolver_rows(R.project(tmp_path, cfg), cfg, lambda n: "/bin/" + n, False, None, runner)


def test_the_doctor_rows_say_host_login_logged_in_as_the_account_type_and_print_no_credential(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "must-not-travel")
    seen = []

    def runner(env, binary, cwd, timeout=None, prefix=None):
        seen.append(env)
        return True, "max"
    rows = _auth_rows(tmp_path, "host", runner)
    auth = [c for c in rows if c["name"].startswith("resolver auth")][0]
    configured = [c for c in rows if c["name"].startswith("resolver configured")][0]
    assert auth["ok"] and "host login: logged in as max" in auth["name"]
    assert "host login: logged in as max" in configured["name"]
    assert seen[0]["HOME"] == os.environ.get("HOME", seen[0]["HOME"]) and "GH_TOKEN" not in seen[0]
    assert "must-not-travel" not in json.dumps(rows)


def test_the_doctor_host_row_is_red_when_logged_out_and_scrubbed_rows_are_unchanged(tmp_path):
    out = [c for c in _auth_rows(tmp_path, "host", lambda *a, **k: (False, None)) if c["name"].startswith("resolver auth")][0]
    assert not out["ok"] and "host login: not logged in" in out["name"]
    scrubbed = _auth_rows(tmp_path / "s", "scrubbed", lambda *a, **k: True)
    row = [c for c in scrubbed if c["name"].startswith("resolver auth")][0]
    assert row["name"] == "resolver auth: logged in in the scrubbed environment (route: none)"
    assert not [c for c in scrubbed if "host login" in c["name"]]
