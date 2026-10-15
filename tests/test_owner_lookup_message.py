"""#1400: a refused `gh project` owner lookup always names the GraphQL rate limit as the usual cause.

Every project call goes through `GitHubSource._run`, so its rewrite is the one place the bare
"unknown owner type" can be explained; the applier tick path has no second printing site that
bypasses it (it prints the exception `_run` raised)."""
import importlib.util
import pathlib
import sys

import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "skills"


def _source(text):
    rel = "sigma-loop/scripts/sources.py"
    spec = importlib.util.spec_from_file_location("src1400", SCRIPTS / rel)
    m = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SCRIPTS / rel).rsplit("/", 1)[0])
    spec.loader.exec_module(m)

    def run(args):
        raise RuntimeError(text)
    gh = m.GitHubSource({"discovery": {"source": "github"}}, run=run)
    gh._RETRY_BASE = 0
    return gh


def _err(text, args):
    gh = _source(text)
    with pytest.raises(Exception) as ei:
        gh._run(args)
    return ei.value


PROJ = ["project", "view", "3", "--owner", "acme"]


def test_bare_owner_lookup_failure_names_the_graphql_rate_limit():
    e = _err("unknown owner type", PROJ)
    msg = str(e)
    assert "unknown owner type" in msg           # the text is kept; the doctor no longer reads it as board-gone (#1485)
    assert "GraphQL rate limit" in msg and "gh api rate_limit" in msg and "retry" in msg
    assert getattr(e, "hint", None) == msg


def test_rate_limit_text_message_is_unchanged():
    msg = str(_err("gh: unknown owner type (API rate limit exceeded)", PROJ))
    assert msg == ("GitHub API rate limit reached on `gh project view` (gh may report it as "
                   "'unknown owner type'); wait for the limit to reset, then retry")


def test_unrelated_error_propagates_unchanged():
    e = _err("boom: no such thing", PROJ)
    assert str(e) == "boom: no such thing" and not hasattr(e, "hint")


def test_non_project_call_is_untouched():
    assert str(_err("unknown owner type", ["issue", "view", "1"])) == "unknown owner type"
