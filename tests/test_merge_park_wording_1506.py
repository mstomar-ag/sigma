"""#1506: a merge parked for a missing approval names the approval, not GraphQL; budget keeps its wording."""
import test_work as tw

_APPROVAL = {"work": {"enabled": True, "require_review": "approval"}}


def test_missing_approval_without_graphql_names_the_approval(tmp_path, monkeypatch):
    monkeypatch.setenv("SIGMA_GH_GRAPHQL", "off")
    d = tw._sdlc(tmp_path, _APPROVAL); g = tw._started(d)
    ok, why = tw.work.review_gate(d, _APPROVAL, g, run=tw._runner(tw._review(decision="REVIEW_REQUIRED")))
    assert ok is False and "not approved" in why and "approve" in why and "GraphQL" not in why, why


def test_budget_failure_keeps_its_wording(tmp_path):
    d = tw._sdlc(tmp_path, _APPROVAL); g = tw._started(d)
    block = RuntimeError("GraphQL: API rate limit already exceeded (HTTP 403)")
    run = tw._runner([("reviewDecision", block)] + tw._review(decision=None))
    ok, why = tw.work.review_gate(d, _APPROVAL, g, run=run)
    assert ok is False and "could not read PR #7's review decision" in why and "GraphQL" in why
