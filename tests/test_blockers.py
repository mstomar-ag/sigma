"""#1393: blockers.py -- a blocked goal is resolved, routed, or parked for a named reason.

`classify` is pure and tested directly: every verdict decides whether a real label gets written to a
real issue, and a classifier reachable only through a `gh` fake is one whose edge cases go untested.
"""
import json, pathlib, importlib.util

import gqlfake

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def _config(**gh):
    return {"discovery": {"source": "github", "github": {"repo": "acme/widget", **gh}}}


def _st(*labels, assignees=(), closed=False):
    return {"labels": set(labels), "assignees": list(assignees), "closed": closed}


def _classify(state, me="me"):
    b = _mod("blockers")
    return b.classify(state, "sdlc:goal", "sdlc:needs-confirmation", "sdlc:parked",
                      "sdlc:followup", me)


def _view(*labels, assignees=(), state="OPEN", state_reason=None):
    payload = {"labels": [{"name": n} for n in labels],
              "assignees": [{"login": a} for a in assignees], "state": state}
    if state_reason is not None:
        payload["stateReason"] = state_reason
    return json.dumps(payload)


def _runner(views=None, fail_on=(), raw_rest=None):
    """`views` are gh-shape payloads; they are answered REST-shaped on `api .../issues/N` (#895).
    `raw_rest`: {n: REST JSON string} sent verbatim (e.g. a response with no `state`)."""
    calls, label_state, gql, fallbacks, rest_writes = [], set(), [], [], []
    views = views or {}
    raw_rest = raw_rest or {}

    def run(args):
        joined = " ".join(str(a) for a in args)
        for needle in fail_on:
            if needle in joined:
                raise RuntimeError(f"simulated gh failure: {needle}")
        answered = gqlfake.swap(args, labels=label_state, calls=calls,
                               repo_args=("--repo", "acme/widget"))
        if answered is not None:
            gql.append(list(args))
            return answered
        if gqlfake.is_issue_write(args):                # #895 slice 3a: REST writes, legacy-recorded
            rest_writes.append(list(args))
            return gqlfake.rest_write(args, calls=calls, repo_args=("--repo", "acme/widget"))
        calls.append(list(args))
        if args[0] == "project":
            return "{}"
        target = gqlfake.rest_issue_target(args)
        if target is not None and target[0] in raw_rest:
            return raw_rest[target[0]]
        rest = gqlfake.rest_issue(args, lambda n, f: views.get(n, _view()))
        if rest is not None:
            return rest
        if len(args) >= 3 and args[0] == "issue" and args[1] == "view":
            fallbacks.append(list(args))      # #895: the one fallback; REST-first tests assert this empty
            return views.get(str(args[2]), _view())
        if args[:2] == ["api", "user"]:
            return "me"
        return ""
    run.calls = calls
    run.gql = gql
    run.fallbacks = fallbacks
    run.rest_writes = rest_writes
    return run


def _source(b, run):
    return b.sources.GitHubSource(_config(), run=run) if hasattr(b, "sources") else \
        _mod("sources").GitHubSource(_config(), run=run)


# --------------------------------------------------------------------------- classify (pure)


def test_a_blocker_already_in_the_queue_needs_nothing_done_to_it():
    b = _mod("blockers")
    assert _classify(_st("sdlc:goal")) == b.PICKABLE


def test_sigmas_own_unruled_proposal_is_promoted():
    """The approval gate stops SPECULATIVE AI-filed work. An issue that real, already-approved work
    is stalled behind is by definition not speculative -- and the label pair is decisive:
    `sdlc:followup` means Sigma filed it, `sdlc:needs-confirmation` means no human has ever
    ruled on it. There is no human decision here to override.

    Slice 18: the confirmation label is legacy-only, so this is read through the PROVENANCE label
    (ROUTED, which grants membership) and never as a PROMOTED verdict."""
    b = _mod("blockers")
    assert _classify(_st("sdlc:needs-confirmation", "sdlc:followup")) == b.ROUTED


def test_a_proposal_a_human_filed_is_never_promoted():
    """No `sdlc:followup` means a person filed it and deliberately left it awaiting approval. That
    IS a human decision, and promoting it would override one."""
    b = _mod("blockers")
    # Slice 18: no confirmation row any more -- a bare leftover is outside the loop's world, and is
    # surfaced (UNMANAGED), never adopted.
    assert _classify(_st("sdlc:needs-confirmation")) == b.UNMANAGED


def test_a_parked_blocker_outranks_the_promotion_rule():
    """Plan-review PR-6. A Sigma-filed follow-up that a human later PARKED has had a decision
    made on it, and `sdlc:followup` alone cannot know that -- so the parked test must run FIRST."""
    b = _mod("blockers")
    assert _classify(_st("sdlc:parked", "sdlc:needs-confirmation", "sdlc:followup")) == b.CHAINED


def test_a_blocker_assigned_to_someone_else_is_routed_not_ignored():
    b = _mod("blockers")
    assert _classify(_st(assignees=["someone-else"]), me="me") == b.ROUTED


def test_a_blocker_assigned_to_me_is_not_routed_to_myself():
    b = _mod("blockers")
    assert _classify(_st(assignees=["me"]), me="me") == b.UNMANAGED


def test_an_orphan_carrying_only_an_sdlc_annotation_is_still_ours_to_repair():
    """The orphan class: managed by Sigma (it carries an sdlc:* annotation) but with no
    lifecycle label at all. Membership is exactly what it is missing, so granting it IS the repair."""
    b = _mod("blockers")
    assert _classify(_st("sdlc:followup")) == b.ROUTED


def test_a_plain_third_party_issue_is_surfaced_never_adopted():
    """It was never ours, so it cannot be 'lost as an orphan' -- and adopting it would put Sigma
    to work on somebody else's backlog."""
    b = _mod("blockers")
    assert _classify(_st("bug", "area:api")) == b.UNMANAGED


def test_a_closed_blocker_is_not_a_blocker():
    b = _mod("blockers")
    assert _classify(_st("sdlc:goal", closed=True)) == b.STALE


# --------------------------------------------------------------------------- resolve (acts)


def test_the_deadlock_is_closed_end_to_end():
    """THE test. Sigma files a follow-up as a proposal, something blocks on it, and before this
    change nothing could ever pick it while auto_unpark refused to resume the goal until it closed.
    After: the blocker comes out carrying the goal label, so the loop can work it, so it can close,
    so the sweep resumes the goal."""
    b = _mod("blockers")
    run = _runner(views={"7": _view("sdlc:needs-confirmation", "sdlc:followup")})
    result = b.resolve(".sdlc", _config(), _source(b, run), "42", ["7"], run=run)
    assert [r["verdict"] for r in result["results"]] == [b.ROUTED]
    assert result["resolved"] == ["7"] and result["surfaced"] == []
    doc = next(a for c in run.gql for a in c if str(a).startswith("query=mutation"))
    assert "addLabelsToLabelable" in doc and "removeLabelsFromLabelable" in doc
    # and the goal is now parked-behind-moving-work, not parked-forever
    assert b.park_reason(result) is None


def test_resolution_is_one_atomic_swap_never_a_gh_issue_edit():
    b = _mod("blockers")
    run = _runner(views={"7": _view("sdlc:needs-confirmation", "sdlc:followup")})
    b.resolve(".sdlc", _config(), _source(b, run), "42", ["7"], run=run)
    mutations = [c for c in run.gql if any(str(a).startswith("query=mutation") for a in c)]
    assert len(mutations) == 1


def test_a_routed_blocker_gets_membership_and_a_comment_naming_the_goal():
    b = _mod("blockers")
    run = _runner(views={"7": _view(assignees=["someone-else"])})
    result = b.resolve(".sdlc", _config(), _source(b, run), "42", ["7"], run=run)
    assert result["results"][0]["verdict"] == b.ROUTED
    assert "@someone-else" in result["results"][0]["detail"]
    comment = next(c[c.index("--body") + 1] for c in run.calls if c[:2] == ["issue", "comment"])
    assert "#42" in comment and "sdlc:goal" in comment


def test_needs_human_and_chained_write_no_labels_at_all():
    """The two verdicts that must never mutate: promoting a human's proposal would override a real
    decision, and un-parking is `auto_unpark`'s job on evidence this module does not have."""
    b = _mod("blockers")
    for labels in (("sdlc:needs-confirmation",), ("sdlc:parked",)):
        run = _runner(views={"7": _view(*labels)})
        result = b.resolve(".sdlc", _config(), _source(b, run), "42", ["7"], run=run)
        assert result["results"][0]["verdict"] in (b.UNMANAGED, b.CHAINED)
        assert not any(str(a).startswith("query=mutation") for c in run.gql for a in c), labels
        assert result["surfaced"] == ["7"]


def test_an_unmanaged_third_party_issue_is_never_adopted():
    b = _mod("blockers")
    run = _runner(views={"7": _view("bug")})
    result = b.resolve(".sdlc", _config(), _source(b, run), "42", ["7"], run=run)
    assert result["results"][0]["verdict"] == b.UNMANAGED
    assert not any(str(a).startswith("query=mutation") for c in run.gql for a in c)


def test_every_refusal_names_the_route_out():
    """A refusal inside the machinery built to remove dead ends must not itself be one."""
    b = _mod("blockers")
    for labels, needle in ((("sdlc:parked",), "/sigma-unpark"),
                           (("bug",), "outside the loop")):
        run = _runner(views={"7": _view(*labels)})
        result = b.resolve(".sdlc", _config(), _source(b, run), "42", ["7"], run=run)
        assert needle in result["results"][0]["detail"], labels


def test_an_unreadable_blocker_is_reported_unresolved_never_assumed_fine():
    b = _mod("blockers")
    run = _runner(fail_on=["issues/7", "issue view 7"])
    result = b.resolve(".sdlc", _config(), _source(b, run), "42", ["7"], run=run)
    assert result["surfaced"] == ["7"]
    assert "could not read" in result["results"][0]["detail"]


def test_state_reads_rest_first_and_asks_for_nothing_else():
    """#895: one `api repos/acme/widget/issues/7 --method GET`; no `issue view`; labels/assignees mapped."""
    b = _mod("blockers")
    run = _runner(views={"7": _view("bug", assignees=["dana"])})
    state = b._state(_source(b, run), "7")
    assert state == {"labels": {"bug"}, "assignees": ["dana"], "closed": False}
    assert [c for c in run.calls if c[0] != "project"] == [["api", "repos/acme/widget/issues/7", "--method", "GET"]]
    assert run.fallbacks == []


def test_a_response_with_no_state_is_refused_not_guessed():
    b = _mod("blockers")
    run = _runner(raw_rest={"7": json.dumps({"number": 7, "labels": [], "assignees": [], "comments": 0})})
    assert b._state(_source(b, run), "7") is None
    assert run.fallbacks == []


def test_rest_5xx_falls_back_once_to_issue_view(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    monkeypatch.delenv("SIGMA_GH_GRAPHQL", raising=False)
    b = _mod("blockers")
    inner = _runner(views={"7": _view("bug")})

    def flaky(args):
        if gqlfake.rest_issue_target(args):
            raise RuntimeError("gh: HTTP 502 Bad Gateway")
        return inner(args)
    state = b._state(_source(b, flaky), "7")
    assert state["labels"] == {"bug"}
    assert [c[:3] for c in inner.fallbacks] == [["issue", "view", "7"]]


def test_one_unresolvable_blocker_never_stops_the_others_being_resolved():
    b = _mod("blockers")
    run = _runner(views={"7": _view("sdlc:parked"),
                         "8": _view("sdlc:needs-confirmation", "sdlc:followup")})
    result = b.resolve(".sdlc", _config(), _source(b, run), "42", ["7", "8"], run=run)
    assert [r["verdict"] for r in result["results"]] == [b.CHAINED, b.ROUTED]
    assert result["resolved"] == ["8"] and result["surfaced"] == ["7"]


def test_park_reason_names_what_actually_survived():
    """A park must never be a bare 'blocked' that leaves a human to investigate from scratch."""
    b = _mod("blockers")
    run = _runner(views={"7": _view("sdlc:parked")})
    result = b.resolve(".sdlc", _config(), _source(b, run), "42", ["7"], run=run)
    reason = b.park_reason(result)
    assert reason and "#7" in reason and "/sigma-unpark" in reason


def test_dry_run_writes_nothing():
    b = _mod("blockers")
    run = _runner(views={"7": _view("sdlc:needs-confirmation", "sdlc:followup")})
    b.resolve(".sdlc", _config(), _source(b, run), "42", ["7"], run=run, apply=False)
    assert not any(str(a).startswith("query=mutation") for c in run.gql for a in c)


def test_a_local_source_degrades_instead_of_raising():
    b = _mod("blockers")

    class Local:
        pass
    result = b.resolve(".sdlc", {}, Local(), "42", ["7"])
    assert result["results"][0]["verdict"] == b.UNMANAGED
    assert "github discovery mode" in result["results"][0]["detail"]


def test_routing_writes_a_note_never_a_handoff(tmp_path):
    """DELIBERATE, and the reason matters: `backlog_check._ledger_signals` treats an outstanding
    `kind="handoff"` as a confident block settled only by an explicit `ack`. A resolver-written one
    that nobody acked would park the filer even after the blocker CLOSED, defeating `auto_unpark` --
    a routing signal must not carry park-the-filer semantics."""
    import json as _json
    b = _mod("blockers")
    (tmp_path / "config.json").write_text(_json.dumps(_config()))
    cfg = dict(_config())
    cfg["ledger"] = {"enabled": True}
    run = _runner(views={"7": _view(assignees=["someone-else"])})
    b.resolve(str(tmp_path), cfg, _source(b, run), "42", ["7"], run=run)
    entries = _mod("ledger").read_all(str(tmp_path))
    assert entries, "the routing entry was not written at all"
    assert [e["kind"] for e in entries] == ["note"]
    assert entries[0]["to"] == "someone-else" and entries[0]["issue"] == 7
    assert "#42" in entries[0]["why"]


def test_routing_survives_a_ledger_that_is_switched_off():
    """`ledger.enabled` ships FALSE, so `safe_append` degrades to a no-op -- which is exactly why
    the ledger is enrichment here and the comment is the always-on channel. Routing must still
    grant membership and comment on a stock config."""
    b = _mod("blockers")
    run = _runner(views={"7": _view(assignees=["someone-else"])})
    result = b.resolve(".sdlc", _config(), _source(b, run), "42", ["7"], run=run)
    assert result["results"][0]["verdict"] == b.ROUTED and result["results"][0]["acted"] is True
    assert any(c[:2] == ["issue", "comment"] for c in run.calls)


def test_a_failed_comment_never_undoes_a_landed_resolution():
    b = _mod("blockers")
    run = _runner(views={"7": _view("sdlc:needs-confirmation", "sdlc:followup")},
                  fail_on=["issues/7/comments"])
    result = b.resolve(".sdlc", _config(), _source(b, run), "42", ["7"], run=run)
    assert result["results"][0]["verdict"] == b.ROUTED and result["results"][0]["acted"] is True


def test_a_failed_label_write_is_reported_and_the_blocker_stays_surfaced():
    """The blocker did NOT become workable, so it must not be counted as resolved -- otherwise the
    park says "everything is moving" about work that is still stuck."""
    b = _mod("blockers")
    run = _runner(views={"7": _view("sdlc:needs-confirmation", "sdlc:followup")},
                  fail_on=["addLabelsToLabelable"])
    result = b.resolve(".sdlc", _config(), _source(b, run), "42", ["7"], run=run)
    assert "could not make #7 pickable" in result["results"][0]["detail"]
    assert result["results"][0]["acted"] is False


def test_render_names_every_blocker_and_its_verdict():
    b = _mod("blockers")
    run = _runner(views={"7": _view("sdlc:parked"), "8": _view("sdlc:goal")})
    text = b.render(b.resolve(".sdlc", _config(), _source(b, run), "42", ["7", "8"], run=run))
    assert "#7" in text and "#8" in text and b.CHAINED in text and b.PICKABLE in text
    assert "no blockers named" in b.render({"results": []})


def test_park_reasons_is_the_closed_set_the_rule_names():
    """The user's rule, made checkable: never skip a block knowingly unless it is going to be parked
    for insufficient permissions, insufficient knowledge, or a known blocker."""
    b = _mod("blockers")
    assert b.PARK_REASONS == ("permissions", "knowledge", "known-blocker")


def test_the_sweeps_own_blocking_label_is_never_proof_of_ownership():
    """A self-reinforcing bug an adversarial audit found. `compute_blocking_actions` writes
    `sdlc:blocking` to a third-party blocker while DELIBERATELY withholding membership. If "any
    sdlc:* label" counted as proof Sigma owns the issue, one pass later that very label would
    flip the verdict to ROUTED and grant membership after all -- Sigma's own annotation becoming
    its own evidence, silently reversing the decision it had just made."""
    b = _mod("blockers")
    assert _classify(_st("sdlc:blocking")) == b.UNMANAGED
    assert _classify(_st("sdlc:blocking", "bug")) == b.UNMANAGED


def test_only_filing_time_provenance_proves_the_issue_is_ours():
    """`sdlc:followup` is stamped on every issue create_tracked_issue files; `sdlc:dependency` on
    every cross-area hand-off. Both are written by Sigma, at filing time, about an issue
    Sigma created. A DERIVED annotation can never say who owns something."""
    b = _mod("blockers")
    assert b._PROVENANCE == ("sdlc:followup", "sdlc:dependency")
    for prov in b._PROVENANCE:
        assert _classify(_st(prov)) == b.ROUTED, prov


def test_the_withheld_membership_decision_survives_a_second_pass():
    """End-to-end version of the same thing: resolve twice over an issue the first pass refused to
    adopt, with the label the first pass would have written now present."""
    b = _mod("blockers")
    run = _runner(views={"7": _view("bug", "sdlc:blocking")})
    result = b.resolve(".sdlc", _config(), _source(b, run), "42", ["7"], run=run)
    assert result["results"][0]["verdict"] == b.UNMANAGED
    assert not any(str(a).startswith("query=mutation") for c in run.gql for a in c)


# --- cloud-review findings (bug_001, bug_002, bug_004) -------------------------------------------


def test_the_legacy_drift_shape_is_routed_so_the_leftover_is_stripped():
    """Slice 18 (review bug_004). `{goal, needs-confirmation}` is refused by the queue's
    `not_eligible_labels`, so reading it PICKABLE deadlocked it: never worked, never stripped. It is
    ROUTED, and `_act` strips the leftover through the remove-only swap."""
    b = _mod("blockers")
    assert _classify(_st("sdlc:goal", "sdlc:needs-confirmation", "sdlc:followup")) == b.ROUTED
    assert _classify(_st("sdlc:goal", "sdlc:needs-confirmation")) == b.ROUTED
    run = _runner(views={"7": _view("sdlc:goal", "sdlc:needs-confirmation")})
    result = b.resolve(".sdlc", _config(), _source(b, run), "42", ["7"], run=run)
    assert result["results"][0]["verdict"] == b.ROUTED
    doc = next(a for c in run.gql for a in c if str(a).startswith("query=mutation"))
    assert "removeLabelsFromLabelable" in doc


def test_granting_membership_still_strips_a_leftover_label():
    """The removal is kept legacy-inert: a Sigma-filed leftover that is granted membership leaves
    without the confirmation label, so no {goal, leftover} drift is manufactured."""
    b = _mod("blockers")
    run = _runner(views={"7": _view("sdlc:needs-confirmation", "sdlc:followup")})
    result = b.resolve(".sdlc", _config(), _source(b, run), "42", ["7"], run=run)
    assert result["results"][0]["verdict"] == b.ROUTED
    doc = next(a for c in run.gql for a in c if str(a).startswith("query=mutation"))
    assert "removeLabelsFromLabelable" in doc and "addLabelsToLabelable" in doc


def test_a_blocker_that_is_itself_blocked_says_so_rather_than_claiming_it_will_be_picked():
    """`{goal, blocked}` is not in any queue right now. It IS self-resolving (the sweep resumes it
    when its own blocker closes), so it stays in `resolved` -- but the park text is the operator's
    only window into why their goal is waiting, and "the loop will pick it" is not true of it."""
    b = _mod("blockers")
    run = _runner(views={"7": _view("sdlc:goal", "sdlc:blocked")})
    result = b.resolve(".sdlc", _config(), _source(b, run), "42", ["7"], run=run)
    assert result["results"][0]["verdict"] == b.PICKABLE
    assert "itself blocked by something else" in result["results"][0]["detail"]


def test_a_blocker_being_worked_right_now_says_so():
    b = _mod("blockers")
    run = _runner(views={"7": _view("sdlc:goal", "sdlc:in-progress")})
    result = b.resolve(".sdlc", _config(), _source(b, run), "42", ["7"], run=run)
    assert "being worked right now" in result["results"][0]["detail"]


def test_a_failed_swap_is_surfaced_not_counted_as_resolved():
    """Review bug_001. `_act` returns `acted=False` when `_swap_labels` raises (its documented
    contract: retry, then RAISE), and the verdict is deliberately not downgraded -- so bucketing on
    the verdict filed a blocker whose label write had FAILED as resolved, `park_reason` returned
    None, and the park claimed everything was workable about an issue no queue can serve. That
    silently recreates the exact deadlock this module exists to close."""
    b = _mod("blockers")
    run = _runner(views={"7": _view("sdlc:needs-confirmation", "sdlc:followup")},
                  fail_on=["addLabelsToLabelable"])
    result = b.resolve(".sdlc", _config(), _source(b, run), "42", ["7"], run=run)
    assert result["results"][0]["acted"] is False
    assert result["surfaced"] == ["7"] and result["resolved"] == []
    reason = b.park_reason(result)
    assert reason and "could not make #7 pickable" in reason


def test_pickable_and_stale_still_count_as_resolved_without_acting():
    """They need nothing done, so they are named explicitly rather than inferred from `acted`."""
    b = _mod("blockers")
    for labels, extra in ((("sdlc:goal",), {}), (("sdlc:goal",), {"state": "CLOSED"})):
        run = _runner(views={"7": _view(*labels, **extra)})
        result = b.resolve(".sdlc", _config(), _source(b, run), "42", ["7"], run=run)
        assert result["resolved"] == ["7"], (labels, extra)
        assert b.park_reason(result) is None


def test_a_co_assigned_blocker_is_routed_to_the_human_not_back_to_sigma(tmp_path):
    """Review bug_002. Sigma self-assigns same-area follow-ups and a person later joins, so
    `assignees == [me, someone-else]` is a realistic shape. ROUTED was decided BECAUSE of the human,
    but the ledger recipient was picked with a bare "first non-empty assignee" -- addressing the
    note back at Sigma. `ledger.addressed_to` matches `to` exactly, so the real owner's
    `ledger.mine` never surfaced a blocker routed for them."""
    import json as _json
    b = _mod("blockers")
    (tmp_path / "config.json").write_text(_json.dumps(_config()))
    cfg = dict(_config())
    cfg["ledger"] = {"enabled": True, "actor": "me"}
    run = _runner(views={"7": _view(assignees=["me", "someone-else"])})
    result = b.resolve(str(tmp_path), cfg, _source(b, run), "42", ["7"], run=run)
    assert result["results"][0]["verdict"] == b.ROUTED
    entries = _mod("ledger").read_all(str(tmp_path))
    assert [e["to"] for e in entries] == ["someone-else"]
    # ...and the rendered detail names the real owner only, not the actor
    assert result["results"][0]["detail"].startswith("#7 belongs to @someone-else")


# --- #2532: blockers.py's own independent copy of the MERGED-PR-reads-as-open defect -----------
# Reachable via `handoff._resolve_reused_blocker` and `loop._resolve_blockers_for_park` --
# `_state()` used to compute `closed = state == "CLOSED"`, which reads a MERGED PR as still open
# (permanently parking a goal behind work that already landed) and, in the OTHER direction, a PR
# closed WITHOUT merging as resolved. `blocker_scan.closed_state` is the one shared fix.


def test_classify_reads_a_merged_pr_blocker_as_stale():
    b = _mod("blockers")
    run = _runner(views={"7": _view(state="MERGED")})
    state = b._state(_source(b, run), "7")
    assert state["closed"] is True
    assert _classify(state) == b.STALE


def test_classify_does_not_read_a_closed_without_merge_pr_as_stale():
    """The case the naive MERGED-collapse gets wrong: no stateReason -- real GitHub never sets one
    on a PR, any state -- so this must NOT classify STALE the way a genuinely-closed issue would."""
    b = _mod("blockers")
    run = _runner(views={"7": _view(state="CLOSED")})   # no state_reason
    state = b._state(_source(b, run), "7")
    assert state["closed"] is False
    assert _classify(state) != b.STALE


def test_resolve_correctly_handles_a_merged_pr_via_the_park_resume_path():
    """Integration-shaped, through `blockers.resolve` -- the function `loop._resolve_blockers_for_
    park` and `handoff._resolve_reused_blocker` actually call, not just the pure `classify()` unit."""
    b = _mod("blockers")
    run = _runner(views={"7": _view(state="MERGED")})
    result = b.resolve(".sdlc", _config(), _source(b, run), "42", ["7"], run=run)
    assert [r["verdict"] for r in result["results"]] == [b.STALE]
    # STALE means "closed, discard the reference" -- it correctly counts as resolved, the same
    # outcome a genuinely-closed issue blocker already gets (mirrors the module's own
    # test_the_deadlock_is_closed_end_to_end shape for a PROMOTED verdict).
    assert result["resolved"] == ["7"] and result["surfaced"] == []


def test_the_routing_comment_is_a_rest_post_and_its_failure_is_swallowed():
    """#895 slice 3a: the comment goes through `gh_api` REST, not `gh issue comment`; a failing POST is
    swallowed (best-effort audit trail) and the ROUTED verdict still stands."""
    b = _mod("blockers")
    run = _runner(views={"7": _view(assignees=["someone-else"])})
    b.resolve(".sdlc", _config(), _source(b, run), "42", ["7"], run=run)
    assert [c[1] for c in run.rest_writes] == ["repos/acme/widget/issues/7/comments"]
    bad = _runner(views={"7": _view(assignees=["someone-else"])}, fail_on=["issues/7/comments"])
    result = b.resolve(".sdlc", _config(), _source(b, bad), "42", ["7"], run=bad)
    assert result["results"][0]["verdict"] == b.ROUTED and result["results"][0]["acted"] is True
