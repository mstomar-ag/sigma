"""#1691: the resolver and the reviewer authenticate with ONE model credential variable the operator names, and a launch whose
scrubbed environment is not logged in parks with that reason. Driven only by a FAKE `claude`; no real model, no network.
UNVERIFIED: the real CLI's `auth status` output shape; the live run is owner-gated (Awaiting).
Controls (each broken once, seen red): drop the preflight in launch, drop the forwarding in the shipped factory."""
import json
import os
import subprocess
import sys

import pytest

import test_upkeep_level1 as l1
import test_upkeep_launcher as L
import upkeep_support as S
from test_upkeep_agent_1516 import agent_cfg, go_wired, wired  # noqa: F401  (wired is a fixture)
from test_upkeep_level2 import conflict_world, mk, rig  # noqa: F401  (rig is a fixture)
from test_upkeep_launcher import rig as lrig  # noqa: F401

import test_doctor_resolver_readiness as R

VAR = "MODEL_LOGIN_VAR"
GATED = '''#!%(python)s
import json, os, pathlib, sys
here = pathlib.Path(%(dir)r)
if sys.argv[1:3] == ["auth", "status"]:
    ok = bool(os.environ.get(%(var)r))
    print(json.dumps({"loggedIn": ok}))
    raise SystemExit(0 if ok else 1)
if not os.environ.get(%(var)r):
    raise SystemExit(7)
calls = json.loads((here / "calls.json").read_text()) if (here / "calls.json").exists() else []
stdin = sys.stdin.read()
calls.append({"role": "review" if "independent reviewer" in stdin else "resolve", "env": dict(os.environ), "argv": sys.argv[1:]})
(here / "calls.json").write_text(json.dumps(calls))
if "independent reviewer" in stdin:
    print(json.dumps({"verdict": "approve", "reasons": ["keeps both sides"]}))
    raise SystemExit(0)
for p in pathlib.Path(".").rglob("*"):
    if p.is_file() and ".git" not in p.parts and "<<<<<<<" in p.read_text():
        p.write_text("".join(l for l in p.read_text().splitlines(True) if l[:7] not in ("<<<<<<<", "=======", ">>>>>>>")))
print("done")
'''


def gated(rig_):
    rig_.binary.write_text(GATED % {"python": sys.executable, "dir": str(getattr(rig_, "fakedir", None) or rig_.fake), "var": VAR})


def poison(monkeypatch):
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK"):
        monkeypatch.setenv(name, "must-not-travel")


# --------------------------------------------------------------------------- the launcher

def test_the_chosen_login_reaches_the_run_and_no_git_gh_or_ssh_credential_does(lrig, monkeypatch):
    gated(lrig)
    monkeypatch.setenv(VAR, "login-value")
    poison(monkeypatch)
    lrig.fakedir.joinpath("mode.json").write_text("{}")
    result = lrig.launcher.launch(lrig.request(credential_var=VAR))
    assert result.outcome in ("ok", "failed") and result.reason != lrig.launcher.NOT_LOGGED_IN, result
    seen = json.loads((lrig.fakedir / "calls.json").read_text())[0]["env"]
    assert seen[VAR] == "login-value"
    assert not [k for k in ("GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK") if k in seen]


def test_a_launch_with_no_login_is_refused_with_the_login_reason_and_spawns_no_run(lrig, monkeypatch):
    gated(lrig)
    monkeypatch.delenv(VAR, raising=False)
    result = lrig.launcher.launch(lrig.request())
    assert result.outcome == "refused" and result.reason == lrig.launcher.NOT_LOGGED_IN and result.charged_usd == 0.0
    assert not (lrig.fakedir / "calls.json").exists()


def test_auth_status_reads_the_exit_code_and_a_false_loggedin(tmp_path):
    launcher = L.load()
    for body, expect in (("raise SystemExit(0)", True), ("raise SystemExit(1)", False),
                         ("print('{\"loggedIn\": false}')", False), ("print('{\"loggedIn\": true}')", True)):
        binary = tmp_path / "claude"
        binary.write_text("#!%s\n%s\n" % (sys.executable, body))
        binary.chmod(0o755)
        assert launcher.auth_status({"PATH": os.environ["PATH"]}, str(binary), str(tmp_path), 10) is expect, body
    assert launcher.auth_status({}, str(tmp_path / "missing"), str(tmp_path), 10) is False


# --------------------------------------------------------------------------- the config key and the shipped factory

@pytest.mark.parametrize("value,ok", [(None, True), (VAR, True), ("_x1", True), ("GH_TOKEN", False), ("GITHUB_TOKEN", False),
                                       ("SSH_AUTH_SOCK", False), ("GIT_ASKPASS", False), ("GIT_SSH_COMMAND", False), ("", False),
                                       ("1BAD", False), ("has space", False), ("a=b", False), (3, False), (True, False), (["X"], False)])
def test_credential_var_accepts_a_variable_name_only(value, ok):
    assert (S.gate().check("conflicts.agent.credential_var", value) is None) is ok


def test_the_key_defaults_to_null_and_the_template_equals_the_defaults():
    assert S.gate().DEFAULTS["conflicts.agent.credential_var"] is None
    assert S.parse_template()["upkeep"]["conflicts"]["agent"]["credential_var"] is None


def test_the_shipped_factory_gives_resolver_and_reviewer_the_same_variable(wired, tmp_path):
    plan = wired._default_level2_plan(agent_cfg(credential_var=VAR), sdlc_dir=str(tmp_path))
    assert plan["launch"]["credential_var"] == VAR and plan["review_launch"]["credential_var"] == VAR
    plan = wired._default_level2_plan(agent_cfg(), sdlc_dir=str(tmp_path))
    assert plan["launch"]["credential_var"] is None and plan["review_launch"]["credential_var"] is None


def test_end_to_end_both_resolver_and_reviewer_get_the_login_and_nothing_else(rig, tmp_path, wired, monkeypatch):
    gated(rig)
    monkeypatch.setenv(VAR, "login-value")
    poison(monkeypatch)
    w, report, spy = go_wired(rig, tmp_path, wired, agent_cfg(credential_var=VAR))
    assert report["outcome"] == wired.REBASED, report.get("why")
    calls = json.loads((rig.fake / "calls.json").read_text())
    assert [c["role"] for c in calls] == ["resolve", "review"]
    for call in calls:
        assert call["env"][VAR] == "login-value"
        assert not [k for k in ("GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK") if k in call["env"]]


def test_end_to_end_no_working_login_parks_naming_the_login_and_pushes_nothing(rig, tmp_path, wired, monkeypatch):
    gated(rig)
    monkeypatch.delenv(VAR, raising=False)
    w = conflict_world(mk(tmp_path / "w"))
    before = w.tip(l1.FEATURE)       # #1713: a real before state, not a guarded None
    assert not os.environ.get(VAR) and json.loads(subprocess.run([str(rig.binary), "auth", "status"], capture_output=True, text=True,
                                                                  env={"PATH": os.environ["PATH"]}).stdout)["loggedIn"] is False
    w, report, spy = go_wired(rig, tmp_path, wired, agent_cfg(credential_var=None), w)
    assert report["outcome"] in (wired.CONFLICT, wired.PARKED) and not spy.pushes()
    assert "not logged in" in report["why"], report["why"]
    assert not (rig.fake / "calls.json").exists()
    assert w.tip(l1.FEATURE) == before


# --------------------------------------------------------------------------- the doctor row

def test_the_resolver_auth_row_is_red_when_logged_out_and_green_when_logged_in(tmp_path):
    d, cfg = R.doctor(), R.config()
    seen = []

    def runner(logged):
        def run(env, binary, cwd, timeout=None, prefix=None):
            seen.append(env)
            return logged
        return run
    sdlc = R.project(tmp_path, cfg)
    red = [c for c in d._resolver_rows(sdlc, cfg, lambda n: "/bin/" + n, False, None, runner(False)) if c["name"].startswith("resolver auth")]
    green = [c for c in d._resolver_rows(sdlc, cfg, lambda n: "/bin/" + n, False, None, runner(True)) if c["name"].startswith("resolver auth")]
    assert len(red) == 1 and len(green) == 1
    assert not red[0]["ok"] and green[0]["ok"] and "route: none" in red[0]["name"] and "Awaiting" in red[0]["fix"]
    assert "HOME" in seen[0] and "GH_TOKEN" not in seen[0]


def test_the_resolver_auth_row_names_the_route_and_is_absent_when_the_level_is_off(tmp_path, monkeypatch):
    d = R.doctor()
    monkeypatch.setenv(VAR, "v")
    cfg = R.config()
    cfg["upkeep"]["conflicts"]["agent"]["credential_var"] = VAR
    rows = d._resolver_rows(R.project(tmp_path, cfg), cfg, lambda n: "/bin/" + n, False, None, lambda *a, **k: True)
    row = [c for c in rows if c["name"].startswith("resolver auth")][0]
    assert row["ok"] and "route: " + VAR in row["name"] and "v" != row["name"]
    off = R.config(resolve="mechanical")
    assert d._resolver_rows(R.project(tmp_path / "o", off), off, lambda n: "/bin/" + n, False, None, lambda *a, **k: True) == []


# --------------------------------------------------------------------------- #1713: the default parks, and the probe is walled

def test_the_docs_and_the_doctor_fix_state_that_the_default_parks_until_credential_var_is_set():
    doc = (S.ROOT / "docs" / "upkeep-part-b.md").read_text(encoding="utf-8") if hasattr(S, "ROOT") else \
        open(os.path.join(os.path.dirname(__file__), "..", "docs", "upkeep-part-b.md"), encoding="utf-8").read()
    assert "parks until `upkeep.conflicts.agent.credential_var` is set" in " ".join(doc.split())
    d = R.doctor()
    row = d._resolver_auth_row(L.load(), "/bin/claude", None, lambda *a, **k: False)
    assert "parks until" in row["fix"] and "credential_var" in row["fix"], row["fix"]


def test_auth_status_runs_through_the_wall_prefix_when_given_and_plain_otherwise(tmp_path):
    launcher = L.load()
    log = tmp_path / "argv.txt"
    wrapper = tmp_path / "wall"
    wrapper.write_text("#!%s\nimport sys, subprocess\nopen(%r, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
                       "raise SystemExit(subprocess.call(sys.argv[1:]))\n" % (sys.executable, str(log)))
    wrapper.chmod(0o755)
    binary = tmp_path / "claude"
    binary.write_text("#!%s\nraise SystemExit(0)\n" % sys.executable)
    binary.chmod(0o755)
    env = {"PATH": os.environ["PATH"]}
    assert launcher.auth_status(env, str(binary), str(tmp_path), 10) is True
    assert not log.exists()
    assert launcher.auth_status(env, str(binary), str(tmp_path), 10, prefix=[str(wrapper)]) is True
    assert log.read_text().split() == [str(binary), "auth", "status"]


def test_the_launch_preflight_probes_through_the_request_wall(lrig, monkeypatch):
    gated(lrig)
    monkeypatch.setenv(VAR, "login-value")
    seen = []
    real = lrig.launcher.auth_status
    monkeypatch.setattr(lrig.launcher, "auth_status", lambda *a, **k: (seen.append(k.get("prefix")), real(*a, **k))[1])
    lrig.fakedir.joinpath("mode.json").write_text("{}")
    lrig.launcher.launch(lrig.request(credential_var=VAR, net_prefix=["/nonexistent-wall"]))
    assert seen == [["/nonexistent-wall"]], seen


def test_the_doctor_probe_is_walled_only_where_a_wall_exists(tmp_path, monkeypatch):
    d = R.doctor()
    launcher = L.load()
    got = []
    monkeypatch.setattr(launcher, "session_wall", lambda *a, **k: (["/w"], "sandbox-exec"))
    d._resolver_auth_row(launcher, "/bin/claude", None, lambda env, b, c, timeout=None, prefix=None: got.append(prefix) or True)
    monkeypatch.setattr(launcher, "session_wall", lambda *a, **k: (None, None))
    d._resolver_auth_row(launcher, "/bin/claude", None, lambda env, b, c, timeout=None, prefix=None: got.append(prefix) or True)
    assert got == [["/w"], None], got
