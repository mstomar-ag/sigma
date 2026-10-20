"""D7 'every refusal prints its fix', GitHub mode (#1290): a gh failure the code can classify
(`gh_api.classify`: auth, not_found, permission, rate_limit, transport, server) prints the raw reason
FIRST and UNCHANGED, then ONE fix line. Before: `GH_TOKEN=bad loop.py next` printed bare
`gh: Bad credentials (HTTP 401)` and `DONE (degraded read ...)`, and `promote.py list` said it
could not resolve assignee @me (the token was bad, @me was fine).

The 401 text below is the real stderr of `gh api rate_limit` with an invalid GH_TOKEN, captured
for docs/launch/evidence/d7-github-mode-refusals-*.md. The 403 and 404 texts are INFERRED wording."""
import importlib.util
import json
import os
import pathlib
import signal
import subprocess
import sys

import pytest

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"
BAD = "gh: Bad credentials (HTTP 401)"
FIX_MARK = "fix: "


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _err(text):
    exc = RuntimeError("gh api user failed: " + text)
    exc.hint = text
    return exc


KINDS = [
    (BAD, "auth", "gh auth login"),
    ("gh: Not Found (HTTP 404)", "not_found", "discovery.github.repo"),
    ("gh: Resource not accessible by personal access token (HTTP 403)", "permission", "repo and workflow"),
    ("gh: API rate limit exceeded for user (HTTP 403)", "rate_limit", "gh api rate_limit"),
    ("dial tcp: lookup api.github.com: no such host", "transport", "network"),
]


@pytest.mark.parametrize("text,kind,needle", KINDS)
def test_with_remedy_appends_the_fix_after_the_untouched_reason(text, kind, needle):
    api = _mod("gh_api")
    out = api.with_remedy(_err(text))
    assert out.startswith(str(_err(text))), "the raw reason must stay first and unchanged"
    assert FIX_MARK in out and needle in out
    assert api.classify(_err(text))[1] == kind


def test_with_remedy_is_idempotent_and_silent_on_unclassified_kinds():
    api = _mod("gh_api")
    once = api.with_remedy(_err(BAD))
    assert api.with_remedy(_err(BAD), once) == once
    plain = RuntimeError("something else entirely")
    assert api.with_remedy(plain) == "something else entirely"


def test_gh_api_error_carries_its_remedy():
    api = _mod("gh_api")
    assert "gh auth login" in api.GhApiError("x", BAD, 401, "auth").remedy
    assert api.GhApiError("x").remedy is None


def _run_401(args):
    raise _err(BAD)


def test_backlog_read_failure_prints_the_fix(capsys):
    src = _mod("sources")
    gh = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "OWNER/NAME"}}}, run=_run_401)
    gh._BACKLOG_READ_RETRY_BASE = 0
    assert gh.next_pending() is None and gh.read_degraded() is True
    err = capsys.readouterr().err
    assert "sources.py: could not read the backlog (" in err      # the pinned prefix survives
    assert "gh auth login" in err


def test_unresolvable_me_prints_the_fix(capsys):
    src = _mod("sources")
    gh = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "OWNER/NAME",
                                                                          "assignee": "@me"}}}, run=_run_401)
    gh._assignee_login()
    err = capsys.readouterr().err
    assert "gh could not read the signed-in user (" in err and "gh auth login" in err


@pytest.mark.parametrize("method,sweep", [("list_needs_label", "needs-label"), ("list_needs_unit", "needs-unit")])
def test_sweep_failures_print_the_fix(capsys, method, sweep):
    src = _mod("sources")
    gh = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "OWNER/NAME"}}}, run=_run_401)
    assert getattr(gh, method)() == []
    err = capsys.readouterr().err
    assert "sigma: could not list " in err and "skipping the %s sweep this pass" % sweep in err
    assert "gh auth login" in err


def test_promote_list_with_a_bad_token_does_not_blame_the_assignee():
    p = _mod("promote")
    src = _mod("sources")
    source = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "OWNER/NAME",
                                                                              "assignee": "@me"}}}, run=_run_401)
    issues, ok, why = p._fetch(source, ["sdlc:proposed"], "@me")
    assert ok is False and issues == []
    assert "could not resolve assignee" not in why
    assert "Bad credentials" in why and "gh auth login" in why


def test_promote_assignee_message_survives_when_there_is_no_gh_failure():
    """No exception to blame (gh answered, but with no login): the old wording is still the truth."""
    p = _mod("promote")
    src = _mod("sources")
    source = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "OWNER/NAME"}}},
                              run=lambda a: "\n")
    _, ok, why = p._fetch(source, ["sdlc:proposed"], "@me")
    assert ok is False and "could not resolve assignee '@me' to a login" in why


def test_promote_why_adds_the_fix_for_every_kind_and_keeps_the_rate_limit_prefix():
    p = _mod("promote")
    assert p._why(_err("gh: Not Found (HTTP 404)")).count(FIX_MARK) == 1
    rl = p._why(_err("gh: API rate limit exceeded (HTTP 403)"))
    assert rl.startswith("rate-limited") and "gh api rate_limit" in rl


def test_loop_next_with_a_bad_token_prints_the_fix_and_keeps_the_done_tokens(tmp_path):
    """End to end through a fake `gh` that answers like the real one with an invalid token."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "gh"
    fake.write_text("#!/bin/sh\necho 'gh: Bad credentials (HTTP 401)' >&2\nexit 1\n")
    fake.chmod(0o755)
    proj = tmp_path / "proj"
    (proj / ".sdlc").mkdir(parents=True)
    (proj / ".sdlc" / "config.json").write_text(json.dumps(
        {"discovery": {"source": "github", "github": {"repo": "OWNER/NAME", "assignee": "@me"}}}))
    env = dict(os.environ, PATH=str(bindir) + os.pathsep + os.environ["PATH"], HOME=str(tmp_path))
    proc = subprocess.Popen([sys.executable, str(S / "loop.py"), "next", ".sdlc"], cwd=proj, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True)
    try:
        out, err = proc.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)         # the group, so a regression fails instead of hanging
        proc.communicate()
        pytest.fail("loop.py next hung")
    assert out.strip() == "DONE"                                       # supervise_classify's stdout token
    assert "DONE (degraded read — backlog state unknown)" in err       # and its stderr line, unchanged
    assert "gh auth login" in err
    assert BAD in err


def test_unpark_resolve_with_a_bad_token_prints_the_fix():
    u = _mod("unpark")
    cfg = {"discovery": {"source": "github", "github": {"repo": "OWNER/NAME"}}}
    out = u.resolve(".sdlc", cfg, 5, {"q": "a"}, "unpark", run=_run_401)
    assert out["outcome"] == "failed"
    assert out["detail"].startswith("could not read the current state of #5:")   # prefix unchanged
    assert BAD in out["detail"] and "gh auth login" in out["detail"]


def test_unpark_brief_with_a_bad_token_prints_the_fix(tmp_path, capsys):
    u = _mod("unpark")
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    (sdlc / "config.json").write_text(json.dumps(
        {"discovery": {"source": "github", "github": {"repo": "OWNER/NAME"}}}))
    rc = u.main(["unpark.py", "brief", str(sdlc), "5"], run=_run_401)
    err = capsys.readouterr().err
    assert rc == 1 and "unpark.py brief: could not read #5 — " in err and "gh auth login" in err


def _fake_proc(monkeypatch, stderr, rc=1):
    monkeypatch.setattr(subprocess, "run", lambda argv, **k: subprocess.CompletedProcess(argv, rc, "", stderr))


def test_work_run_gh_failure_keeps_the_raw_text_and_adds_the_fix(monkeypatch, tmp_path):
    w = _mod("work")
    _fake_proc(monkeypatch, BAD)
    with pytest.raises(RuntimeError) as ei:
        w._run(tmp_path, ["gh", "api", "user"])
    text = str(ei.value)
    assert text.startswith("gh api user: " + BAD) and "gh auth login" in text


def test_work_run_git_failure_gets_no_gh_remedy(monkeypatch, tmp_path):
    w = _mod("work")
    _fake_proc(monkeypatch, "fatal: Bad credentials (HTTP 401)")
    with pytest.raises(RuntimeError) as ei:
        w._run(tmp_path, ["git", "fetch"])
    assert "gh auth login" not in str(ei.value)


def test_work_adapter_hint_still_classifies_to_the_same_kind(monkeypatch, tmp_path):
    """The remedy rides AFTER gh's text, so `classify` of the adapted hint still says `auth`."""
    w = _mod("work")
    api = _mod("gh_api")
    _fake_proc(monkeypatch, BAD)
    with pytest.raises(RuntimeError) as ei:
        w._run(tmp_path, ["gh", "api", "user"])
    exc = ei.value
    exc.hint = str(exc)[len("gh api user: "):]
    assert api.classify(exc) == (401, "auth")


def test_promote_label_write_failure_prints_the_fix(monkeypatch):
    """`label write did not land` keeps its prefix and gains the fix."""
    p = _mod("promote")
    src = _mod("sources")
    source = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "OWNER/NAME"}}}, run=_run_401)

    def boom(*a, **k):
        raise _err(BAD)
    monkeypatch.setattr(source, "_swap_labels", boom)
    monkeypatch.setattr(p, "_read_state", lambda s, n: (({"sdlc:proposed"}, False, "", "", "t"), ""))
    monkeypatch.setattr(p, "_blocker_status", lambda *a, **k: [])
    out = p.promote(".sdlc", {"discovery": {"source": "github"}}, [7], source=source)
    detail = out["results"][0]["detail"]
    assert out["results"][0]["outcome"] == "failed"
    assert detail.startswith("label write did not land: ") and "gh auth login" in detail


# ---------------------------------------------------------------- review fixes (defects 1-5)

def test_a_fix_colon_title_in_argv_does_not_suppress_the_remedy(monkeypatch):
    """Defect 1: the 'already applied' test was the substring 'fix: ', which a house-style issue
    title in the argv ('fix: parser crash') contains."""
    src = _mod("sources")
    exc = None
    monkeypatch.setattr(subprocess, "run", lambda argv, **k: subprocess.CompletedProcess(argv, 1, "", BAD))
    try:
        src._run_gh(["issue", "create", "--title", "fix: parser crash"])
    except RuntimeError as e:
        exc = e
    api = _mod("gh_api")
    out = api.with_remedy(exc)
    assert "fix: parser crash" in out and "gh auth login" in out


def test_a_message_that_already_carries_the_remedy_is_not_doubled():
    api = _mod("gh_api")
    once = api.with_remedy(_err(BAD))
    assert api.with_remedy(_err(BAD), once).count("gh auth login") == once.count("gh auth login")
    twice = api.with_remedy(_err(BAD), api.with_remedy(_err(BAD), "x"))
    assert twice.count("run `gh auth login`") == 1


def test_not_found_advice_is_neutral_and_names_both_checks():
    sess = _mod("gh_session")
    fix = sess.REMEDIES["not_found"]
    assert "does not exist" in fix and "cannot see" in fix
    assert "issue number" in fix and "discovery.github.repo" in fix and "gh auth status" in fix


@pytest.mark.parametrize("exc", [
    RuntimeError("gh issue edit 5 --repo me/timeout-svc: boom"),
    ValueError("the report timed out waiting for the rate_limit sweep, unauthorized"),
    RuntimeError("gh api repos/me/unauthorized-app/issues failed: boom"),
])
def test_remedy_is_not_classified_from_argv_or_foreign_exceptions(exc):
    """Defect 3: no .hint means no gh stderr to classify; str(exc) embeds argv."""
    api = _mod("gh_api")
    assert api.with_remedy(exc) == str(exc)


def test_unresolvable_me_on_a_bad_token_leads_with_the_real_cause(capsys):
    """Defect 4."""
    src = _mod("sources")
    gh = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "OWNER/NAME",
                                                                          "assignee": "@me"}}}, run=_run_401)
    gh._assignee_login()
    err = capsys.readouterr().err
    assert err.startswith("sources.py: gh could not read the signed-in user (")
    assert "could not resolve `@me`" not in err


def test_unresolvable_me_for_a_non_auth_failure_keeps_the_old_wording(capsys):
    src = _mod("sources")

    def run(args):
        raise _err("dial tcp: lookup api.github.com: no such host")
    gh = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "OWNER/NAME",
                                                                          "assignee": "@me"}}}, run=run)
    gh._assignee_login()
    assert "could not resolve `@me` to a login (" in capsys.readouterr().err


def test_the_retrying_line_carries_no_remedy_only_the_final_line_does(capsys):
    """Defect 5: a transient error is already being retried, so 'retry in a minute' on that line is noise."""
    src = _mod("sources")

    def run(args):
        raise _err("gh: Bad Gateway (HTTP 502)")
    gh = src.GitHubSource({"discovery": {"source": "github", "github": {"repo": "OWNER/NAME"}}}, run=run)
    gh._BACKLOG_READ_RETRY_BASE = 0
    gh.next_pending()
    lines = [l for l in capsys.readouterr().err.splitlines() if "could not read the backlog" in l]
    assert len(lines) >= 2
    assert all("fix: " not in l for l in lines if "retrying" in l)
    assert "githubstatus" in lines[-1]
