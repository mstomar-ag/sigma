"""#1398: claim, define open and the merge review read survive a GraphQL refusal through REST.

Each test pins one behaviour. The healthy-GraphQL tests pin that nothing changed when GraphQL works.
"""
import importlib.util
import json
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "tests"))

import test_define as td          # noqa: E402  (Runner, _sdlc, define)
import test_sources as ts         # noqa: E402  (_graphql_runner and friends)
import test_work as tw            # noqa: E402  (_runner, _review, _sdlc, _started)

RATE = "GraphQL: API rate limit already exceeded for user (HTTP 403)"


def _refusing_runner(rest_log):
    """GraphQL always raises a rate limit; REST label calls are recorded and succeed."""
    calls = []

    def run(args):
        calls.append(list(args))
        if "graphql" in args:
            raise RuntimeError(RATE)
        rest_log.append(list(args))
        return "[]"
    run.calls = calls
    return run


def _src(run):
    gh = ts._swap_source(run)
    gh._CLAIM_LABEL_RETRY_BASE = 0
    return gh


def test_swap_falls_back_to_rest_when_graphql_refuses_without_burning_retries(capsys):
    rest = []
    run = _refusing_runner(rest)
    gh = _src(run)
    assert gh._swap_labels("42", add=["sdlc:in-progress"], remove=["sdlc:goal"]) is True
    graphql_calls = [c for c in run.calls if "graphql" in c]
    assert len(graphql_calls) == 1                       # one lookup, no retry ladder
    assert any("issues/42/labels" in " ".join(c) and "POST" in c for c in rest), rest
    assert any("DELETE" in c and "sdlc%3Agoal" in " ".join(c) for c in rest), rest
    assert "rate limit" in capsys.readouterr().err


def test_claim_lands_through_rest_under_a_graphql_refusal():
    rest = []
    gh = _src(_refusing_runner(rest))
    assert gh.mark_in_progress("42") is True
    assert any("issues/42/labels" in " ".join(c) for c in rest)


def test_unknown_label_still_raises_and_never_goes_to_rest():
    run = ts._graphql_runner(labels={"sdlc:goal": "L_goal"})
    gh = ts._swap_source(run)
    with pytest.raises(RuntimeError, match="unknown label"):
        gh._swap_labels("42", add=["sdlc:nope"])
    assert not any("issues/42/labels" in " ".join(c) for c in run.calls)


def test_healthy_graphql_swap_makes_no_rest_call_and_the_same_calls():
    run = ts._graphql_runner()
    gh = ts._swap_source(run)
    assert gh._swap_labels("42", add=["sdlc:in-progress"], remove=["sdlc:goal"]) is True
    assert all(c[:2] == ["api", "graphql"] for c in run.calls), run.calls
    assert len(ts._mutation_docs(run)) == 1
    assert len(run.calls) == 3          # node id, label ids, one mutation: byte-identical shape


class _RateLimitedList(td.Runner):
    def __call__(self, cwd, argv):
        argv = [str(a) for a in argv]
        if argv[:3] == ["gh", "label", "list"]:
            self.calls.append(argv)
            raise RuntimeError(RATE)
        if argv[:2] == ["gh", "api"]:
            self.calls.append(argv)
            return "\n".join(sorted(self.labels))
        return super().__call__(cwd, argv)


def test_define_open_lists_labels_through_rest_when_the_graphql_list_is_refused(tmp_path):
    run = _RateLimitedList(labels={"feature:voice"})
    report = td.define.open_unit(td._sdlc(tmp_path), "voice", "feature", run=run, cwd=tmp_path)
    assert report["ok"], report
    assert any(c[:2] == ["gh", "api"] and "labels" in " ".join(c) for c in run.calls)


def test_define_open_failure_names_rate_limit_not_the_gh_login(tmp_path):
    class Both(_RateLimitedList):
        def __call__(self, cwd, argv):
            if [str(a) for a in argv][:2] == ["gh", "api"]:
                raise RuntimeError(RATE)
            return super().__call__(cwd, argv)
    report = td.define.open_unit(td._sdlc(tmp_path), "voice", "feature", run=Both(), cwd=tmp_path)
    assert not report["ok"]
    assert "rate limit" in report["why"].lower() and "logged in" not in report["why"], report["why"]


def test_define_open_failure_without_rate_limit_keeps_the_login_hint(tmp_path):
    run = td.Runner(fail_on=lambda a: "not logged in" if a[:3] == ["gh", "label", "list"] else None)
    report = td.define.open_unit(td._sdlc(tmp_path), "voice", "feature", run=run, cwd=tmp_path)
    assert not report["ok"] and "gh auth status" in report["why"]


_APPROVAL = {"work": {"enabled": True, "require_review": "approval"}}
_BLOCK = RuntimeError("GraphQL: API rate limit already exceeded (HTTP 403)")


def test_merge_gate_approval_passes_on_a_directive_when_the_decision_is_unreadable(tmp_path):
    d = tw._sdlc(tmp_path, _APPROVAL); g = tw._started(d)
    run = tw._runner([("reviewDecision", _BLOCK)] + tw._review(decision=None, comments=["sigma:approve"]))
    assert tw.work.review_gate(d, _APPROVAL, g, run=run) == (True, "")


def test_merge_gate_approval_still_parks_with_no_directive_and_unreadable_decision(tmp_path):
    d = tw._sdlc(tmp_path, _APPROVAL); g = tw._started(d)
    run = tw._runner([("reviewDecision", _BLOCK)] + tw._review(decision=None))
    ok, why = tw.work.review_gate(d, _APPROVAL, g, run=run)
    assert ok is False and "could not read PR #7's review decision" in why


def test_merge_gate_a_block_directive_still_wins_over_an_unreadable_decision(tmp_path):
    d = tw._sdlc(tmp_path, _APPROVAL); g = tw._started(d)
    run = tw._runner([("reviewDecision", _BLOCK)] + tw._review(decision=None, comments=["sigma:block"]))
    ok, why = tw.work.review_gate(d, _APPROVAL, g, run=run)
    assert ok is False and "sigma:block" in why
