"""Goal 1558: free text bound for a stored decision record passes the leak scan's secret-shape scrub."""
import importlib.util
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills/sigma-loop/scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name + "_t1558", SCRIPTS / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


DS, DI = _mod("decision_store"), _mod("decision_import")
CFG = {"rulebook": {"records": {"enabled": True, "repo_visibility": "private"},
                    "autonomy": {"enabled": True, "levels": {"l1_min_answers": 1}}}}
TAIL = "Ab3dE5gH7jK9mN1pQ3sT5vX7"


def _shapes():
    """Secret-looking strings assembled at runtime so none is committed."""
    return {
        "anthropic-key": "sk-" + "ant-" + TAIL,
        "github-pat": "github_" + "pat_" + TAIL,
        "jwt": "ey" + "J" + "hbGciOiJIUzI1" + "." + "ey" + "J" + "zdWIiOiIxMjM0NTY3" + "." + "SflKxwRJSMeKKF2QT4",
        "private-key": "-----BEGIN " + "PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END " + "PRIVATE KEY-----",
        "credential-assignment": "pass" + "word = '" + TAIL + "'",
        "url-password": "postgres" + "://admin:" + TAIL + "@db.internal.example",
    }


def _value(name, text):
    return {"private-key": "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC", "jwt": "SflKxwRJSMeKKF2QT4",
            "url-password": TAIL}.get(name, TAIL if name != "jwt" else text)


def _rec(**kw):
    r = {"how": "human", "qkind": "scope", "area": "docs", "choice": "option a", "reason": "why", "status": "answered"}
    r.update(kw)
    return r


def _all_text(sd):
    return "\n".join(p.read_text() for p in pathlib.Path(sd).rglob("*") if p.is_file())


def test_each_shape_scrubbed_in_choice_and_reason_and_origin(tmp_path):
    sd = tmp_path / ".sdlc"
    sd.mkdir()
    for name, secret in _shapes().items():
        rid = DS.append(str(sd), _rec(choice="use " + secret + " now", reason="because " + secret,
                                      origin={"kind": "transcript", "cwd": "/x/" + secret}), CFG)
        body = (sd / "decisions" / (rid + ".json")).read_text()
        assert _value(name, secret) not in body, name
        assert "[REDACTED" in body, name


def test_marker_names_the_shape(tmp_path):
    sd = tmp_path / ".sdlc"
    sd.mkdir()
    rid = DS.append(str(sd), _rec(choice=_shapes()["anthropic-key"]), CFG)
    assert "[REDACTED:anthropic-key]" in (sd / "decisions" / (rid + ".json")).read_text()


def test_scrub_record_counts_and_clean_record_unchanged():
    clean = _rec(origin={"kind": "transcript", "line": 3})
    out, n = DS.scrub_record(clean)
    assert n == 0 and out == clean
    out, n = DS.scrub_record(_rec(choice=_shapes()["github-pat"], reason=_shapes()["anthropic-key"]))
    assert n == 2


def test_clean_record_file_is_byte_identical(tmp_path):
    sd = tmp_path / ".sdlc"
    sd.mkdir()
    now = __import__("datetime").datetime(2026, 10, 10, tzinfo=__import__("datetime").timezone.utc)
    rid = DS.append(str(sd), _rec(id="r1"), CFG, now=now)
    expect = json.dumps(dict(_rec(id="r1"), time=now.isoformat()), sort_keys=True, indent=1) + "\n"
    assert (sd / "decisions" / (rid + ".json")).read_text() == expect


def test_apply_function_counts(tmp_path):
    sd = tmp_path / ".sdlc"
    sd.mkdir()
    c = {"source": "transcript", "when": "2026-10-01T10:00:00Z", "question": "should we merge the docs change",
         "options": [], "choice": "merge " + _shapes()["anthropic-key"], "reason": "because", "issue": 7,
         "id": "c1", "superseded": False}
    out = DI.apply(str(sd), CFG, [c])
    assert out["scrubbed"] == 1 and out["records"] == 1
    assert TAIL not in _all_text(sd)


def test_cli_apply_scrubs_and_prints_count_not_value(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    sd = repo / ".sdlc"
    sd.mkdir()
    (sd / "config.json").write_text(json.dumps(CFG))
    prof = tmp_path / "prof"
    folder = prof / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(repo.resolve()))
    folder.mkdir(parents=True)
    q = "should we merge change one"
    secret = _shapes()["anthropic-key"]
    ask = {"type": "assistant", "timestamp": "2026-10-01T10:00:00Z", "cwd": str(repo.resolve()), "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "AskUserQuestion", "input": {"questions": [
            {"question": q, "options": [{"label": "merge"}, {"label": "wait"}]}]}}]}}
    ans = {"type": "user", "timestamp": "2026-10-01T10:01:00Z", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1",
         "content": 'User has answered your questions: "%s"="merge with %s". You can now continue.' % (q, secret)}]}}
    (folder / "s1.jsonl").write_text(json.dumps(ask) + "\n" + json.dumps(ans) + "\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gh").write_text("#!/bin/sh\nexit 1\n")
    (bindir / "gh").chmod(0o755)
    env = {"PATH": "%s:/usr/bin:/bin" % bindir, "HOME": str(tmp_path)}
    r = subprocess.run([sys.executable, str(SCRIPTS / "decision_import.py"), "apply", str(sd), "--profile", str(prof)],
                       cwd=repo, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "scrubbed 1 value(s)" in r.stdout and TAIL not in r.stdout + r.stderr
    assert TAIL not in _all_text(sd)
    assert "[REDACTED:anthropic-key]" in _all_text(sd / "decisions")
