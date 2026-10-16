"""#1527: when `gh auth status` fails twice and the REST read fails on the NETWORK (timeout, connection
error: no login, no rate-limit marker, no 401), the gh auth row says "cannot verify" and names the
network failure, with no interactive login command. A real logged-out account keeps its row.
Fake runners only; no network."""
import importlib.util
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
D = ROOT / "skills" / "sigma-doctor" / "scripts" / "doctor.py"


def _pf():
    spec = importlib.util.spec_from_file_location("doctor1527", D)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m._load_init_script("preflight")


def _runner(status_text, user_rc, user_out):
    def runner(argv, cwd=None, timeout=None):
        if argv[:3] == ["gh", "auth", "status"]:
            return 1, status_text
        if argv[:2] == ["gh", "api"]:
            return user_rc, user_out
        return 1, ""
    return runner


@pytest.mark.parametrize("rest", [
    (124, "gh api user: timed out after 15s"),
    (1, "error connecting to api.github.com"),
    (1, "dial tcp: lookup api.github.com: no such host"),
])
def test_network_failure_is_cannot_verify_and_names_it(rest):
    row, _ = _pf().check_gh_auth("github.com", _runner("error connecting to api.github.com", *rest), 5,
                                 sleep=lambda s: None)
    assert row["ok"] is None and row["interactive"] is False and row["commands"] == []
    assert "gh auth login" not in json.dumps(row)
    assert "cannot verify" in row["detail"]
    assert "network" in row["detail"].lower()


def test_rest_401_is_still_logged_out():
    row, _ = _pf().check_gh_auth("github.com", _runner("error connecting", 1, "gh: Bad credentials (HTTP 401)"),
                                 5, sleep=lambda s: None)
    assert row["ok"] is False and row["interactive"] is True and "gh auth login" in json.dumps(row)


def test_login_required_wording_is_still_logged_out():
    row, _ = _pf().check_gh_auth("github.com", _runner("You are not logged into any GitHub hosts. "
                                 "To log in, run: gh auth login", 1, "gh: To get started with GitHub CLI, "
                                 "please run:  gh auth login"), 5, sleep=lambda s: None)
    assert row["ok"] is False and row["interactive"] is True


@pytest.mark.parametrize("rest", [
    (1, "gh: Bad Gateway (HTTP 502)"),
    (1, "gh: Service Unavailable (HTTP 503)"),
    (1, "gh: Server Error (HTTP 500)"),
    (1, 'Post "https://api.github.com": EOF'),
    (1, ""),
])
def test_server_error_eof_or_empty_rest_answer_is_cannot_verify(rest):
    """#1708: only a REST 401 or the login-required wording is logged out."""
    row, _ = _pf().check_gh_auth("github.com", _runner("some failure", *rest), 5, sleep=lambda s: None)
    assert row["ok"] is None and row["interactive"] is False and row["commands"] == []
    assert "gh auth login" not in json.dumps(row)
    assert "cannot verify" in row["detail"]


def test_successful_auth_status_is_unchanged():
    def runner(argv, cwd=None, timeout=None):
        return 0, "github.com\n  Logged in to github.com account octo (keyring)\n  - Active account: true\n"
    row, _ = _pf().check_gh_auth("github.com", runner, 5, sleep=lambda s: None)
    assert row["ok"] is True
