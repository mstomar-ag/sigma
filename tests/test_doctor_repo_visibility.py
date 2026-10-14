"""The doctor reads repository visibility through a real owner/name slug, not a placeholder gh cannot expand."""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
WARN = "public repository with records on"
PLACEHOLDER = ["gh", "api", "repos/{owner}/{repo}", "--jq", ".private"]


def _doctor():
    p = ROOT / "skills/sigma-doctor/scripts/doctor.py"
    spec = importlib.util.spec_from_file_location("doctor_repovis", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _cfg(slug=None):
    cfg = {"rulebook": {"records": {"enabled": True, "repo_visibility": "private"}}}
    if slug:
        cfg["discovery"] = {"github": {"repo": slug}}
    return cfg


class _Run:
    def __init__(self, gh_reply, origin=""):
        self.gh_reply, self.origin, self.calls = gh_reply, origin, []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[0] == "git":
            return self.origin
        return self.gh_reply


def _gh(run):
    return [c for c in run.calls if c[0] == "gh"]


def test_config_slug_is_used(tmp_path):
    run = _Run("false\n")
    out = _doctor()._rulebook_state(_cfg("acme/widgets"), tmp_path / ".sdlc", run=run)
    assert _gh(run) == [["gh", "api", "repos/acme/widgets", "--jq", ".private"]]
    assert WARN in out and "(read from GitHub)" in out


def test_host_alias_origin_is_parsed(tmp_path):
    run = _Run("false\n", origin="https://github.com-work/acme/widgets.git\n")
    out = _doctor()._rulebook_state(_cfg(), tmp_path / ".sdlc", run=run)
    assert _gh(run) == [["gh", "api", "repos/acme/widgets", "--jq", ".private"]]
    assert WARN in out and "(read from GitHub)" in out


def test_neither_available_keeps_placeholder_call(tmp_path):
    run = _Run("true\n", origin="")
    _doctor()._rulebook_state(_cfg(), tmp_path / ".sdlc", run=run)
    assert _gh(run) == [PLACEHOLDER]


def test_unreadable_reply_is_not_read(tmp_path):
    d = _doctor()
    run = _Run(d._RawFailure("no gh"), origin="https://github.com-work/acme/widgets.git")
    out = d._rulebook_state(_cfg(), tmp_path / ".sdlc", run=run)
    assert "repository visibility not read" in out and "(read from GitHub)" not in out
