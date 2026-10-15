"""#1485: a refused GitHub read (shared GraphQL budget at 0) must read "(cannot verify)" on the doctor's
`gh auth`, `gh project scope` and `pinned board #N reachable` rows -- never "not logged in", "does not
exist", an interactive `gh auth login`, or a create-a-board command. Fake runners only; no network."""
import importlib.util
import json
import pathlib
import shutil
import subprocess
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
D = ROOT / "skills" / "sigma-doctor" / "scripts" / "doctor.py"


def _doc():
    spec = importlib.util.spec_from_file_location("doctor1485", D)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def _pf():
    return _doc()._load_init_script("preflight")


GH = {"repo": "acme/widget", "project": {"enabled": True, "number": 13}}


def _fail(d, text):
    return d._RawFailure(text)


def _board_run(d, view_text, owner_answer):
    """`gh project view` fails with view_text; `gh api users/acme` answers owner_answer (a type, or a
    failure text starting 'ERR:')."""
    def run(args):
        if args[:3] == ["gh", "project", "view"]:
            return _fail(d, view_text)
        if args[:3] == ["gh", "api", "users/acme"]:
            return _fail(d, owner_answer[4:]) if owner_answer.startswith("ERR:") else owner_answer
        return ""
    return run


RATE = "GraphQL: API rate limit already exceeded for user ID 1. unknown owner type"


@pytest.mark.parametrize("text", ["unknown owner type", RATE])
def test_refused_owner_lookup_with_existing_owner_is_cannot_verify(text):
    d = _doc()
    run = _board_run(d, text, "Organization")
    state, fix = d._pinned_board_state(GH, run)
    if "rate limit" in text:
        assert state == "unverifiable"          # a rate-limit marker never reaches "gone"
    else:
        assert state == "refused" and "GraphQL rate limit" in fix and "wait for the reset" in fix
    assert state != "gone"


def test_refused_state_emits_a_cannot_verify_row_and_no_create_advice():
    d = _doc()
    run = _board_run(d, "unknown owner type", "Organization")
    with tempfile.TemporaryDirectory() as t:
        base = pathlib.Path(t) / ".sdlc"; base.mkdir()
        (base / "config.json").write_text(json.dumps({"discovery": {"source": "github", "github": GH}}))
        rows = {c["name"]: c for c in d.check(base, run=run)}
    row = rows["pinned board #13 reachable (cannot verify)"]
    blob = json.dumps(list(rows.values()))
    assert row["ok"] is False and "wait for the reset" in row["fix"]
    assert "does not exist" not in blob and "board_setup.py create" not in row["fix"]
    assert "pinned board #13 reachable" not in rows


def test_could_not_resolve_projectv2_stays_gone():
    d = _doc()
    run = _board_run(d, "GraphQL: Could not resolve to a ProjectV2 with the number 13.", "Organization")
    state, fix = d._pinned_board_state(GH, run)
    assert state == "gone" and "does not exist" in fix


def test_unknown_owner_type_with_rest_404_is_gone():
    d = _doc()
    run = _board_run(d, "unknown owner type", "ERR:gh: Not Found (HTTP 404)")
    assert d._pinned_board_state(GH, run)[0] == "gone"


def test_unknown_owner_type_when_rest_also_fails_is_unverifiable():
    d = _doc()
    run = _board_run(d, "unknown owner type", "ERR:error connecting to api.github.com")
    assert d._pinned_board_state(GH, run)[0] == "unverifiable"


# --- check_gh_auth -------------------------------------------------------------------------------

def _auth_runner(status_text, user_rc, user_out, calls):
    def runner(argv, cwd=None, timeout=None):
        calls.append(list(argv))
        if argv[:3] == ["gh", "auth", "status"]:
            return 1, status_text
        if argv[:3] == ["gh", "api", "user"] or "user" in argv[:5] and argv[:2] == ["gh", "api"]:
            return user_rc, user_out
        return 1, ""
    return runner


@pytest.mark.parametrize("status_text", ["The token in keyring is invalid", "API rate limit exceeded"])
def test_status_fails_but_rest_answers_is_cannot_verify_not_a_login(status_text):
    pf = _pf(); calls = []; naps = []
    row, _ = pf.check_gh_auth("github.com", _auth_runner(status_text, 0, "octo\n", calls), 5, sleep=naps.append)
    assert row["ok"] is None and row["interactive"] is False and row["commands"] == []
    assert "gh auth login" not in json.dumps(row) and "octo" in row["detail"]
    assert "could not confirm" in row["detail"]
    assert sum(1 for c in calls if c[:3] == ["gh", "auth", "status"]) == 2   # exactly one retry
    assert len(naps) == 1


def test_rate_limit_text_with_rest_failing_is_still_cannot_verify():
    pf = _pf()
    row, _ = pf.check_gh_auth("github.com", _auth_runner("API rate limit exceeded", 1, "rate limit", []), 5,
                              sleep=lambda s: None)
    assert row["ok"] is None and "gh auth login" not in json.dumps(row)


def test_only_a_rest_401_keeps_not_logged_in():
    pf = _pf()
    row, _ = pf.check_gh_auth("github.com", _auth_runner("not logged in", 1, "gh: Bad credentials (HTTP 401)", []),
                              5, sleep=lambda s: None)
    assert row["ok"] is False and row["interactive"] is True and "gh auth login" in json.dumps(row)


def test_retry_that_succeeds_passes():
    pf = _pf(); n = []
    def runner(argv, cwd=None, timeout=None):
        n.append(1)
        return (1, "boom") if len(n) == 1 else (0, "github.com\n  - Active account: true\n")
    row, _ = pf.check_gh_auth("github.com", runner, 5, sleep=lambda s: None)
    assert row["ok"] is True


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_scope_row_becomes_cannot_verify_without_a_login_command(tmp_path):
    d = _doc()
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", "https://github.com/acme/widget.git"],
                   check=True)
    base = tmp_path / ".sdlc"; base.mkdir()
    cfg = {"discovery": {"source": "github", "github": GH}}
    def run(args):
        if args[:3] == ["gh", "auth", "status"]:
            return _fail(d, "GraphQL: API rate limit already exceeded")
        if args[:3] == ["gh", "api", "user"]:
            return "octo\n"
        return ""
    pf = d._load_init_script("preflight")
    pf_sleep = pf.time.sleep
    pf.time.sleep = lambda s: None            # the doctor loads its own preflight copy; patch the shared time module
    try:
        rows = {c["name"]: c for c in d._preflight_rows(base, cfg, run, shutil.which, True, False)}
    finally:
        pf.time.sleep = pf_sleep
    auth = rows["gh auth (cannot verify)"]
    scope = rows["gh project scope (cannot verify)"]
    assert "gh auth login" not in json.dumps([auth, scope])
