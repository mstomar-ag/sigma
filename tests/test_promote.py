"""#1392: promote.py -- the sanctioned crossing of the `sdlc:needs-confirmation` approval gate.

These drive a REAL `sources.GitHubSource` through an injectable runner (no network, no `gh`), so
the atomic-swap transport is genuinely exercised rather than stubbed -- `gqlfake.swap` answers the
three GraphQL shapes `_swap_labels` issues and models GitHub's own behaviour on top of them.
"""
import json, pathlib, importlib.util

import gqlfake

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def _config(**gh):
    return {"discovery": {"source": "github", "github": {"repo": "acme/widget", **gh}}}


def _board_config(**gh):
    """A board-AUTHORITATIVE repo -- the only shape where "did the card move" is a real question.
    On a label-queue repo `_set_board_status` returns False because there is no board at all, which
    is the honest answer and must never be reported as a stuck card."""
    # the `project` block lives UNDER `discovery.github`, not at the config root
    return _config(project={"enabled": True, "number": 1, "owner": "acme"}, **gh)


def _issue(number, *labels, title="t", body=None):
    issue = {"number": number, "title": title, "labels": [{"name": n} for n in labels]}
    if body is not None:
        issue["body"] = body
    return issue


def _runner(by_label=None, views=None, fail_on=(), board_ok=True):
    """`issue list --label X` -> by_label[X]; `issue view N --json labels,state` -> views[N].
    Everything else records and succeeds. `board_ok=False` makes every `project item-edit` raise,
    which is how `_set_board_status` reports a card that did not move."""
    calls, labels_state, gql_calls, fallbacks, rest_writes = [], set(), [], [], []
    by_label, views = by_label or {}, views or {}
    default_view = json.dumps({"labels": [], "state": "OPEN"})

    def run(args):
        joined = " ".join(str(a) for a in args)
        for needle in fail_on:
            if needle in joined:
                raise RuntimeError(f"simulated gh failure: {needle}")
        gql = gqlfake.swap(args, labels=labels_state, calls=calls,
                           repo_args=("--repo", "acme/widget"))
        if gql is not None:
            gql_calls.append(list(args))
            return gql
        if gqlfake.is_issue_write(args):                # #895 slice 3a: REST writes, legacy-recorded
            rest_writes.append(list(args))
            return gqlfake.rest_write(args, calls=calls, repo_args=("--repo", "acme/widget"))
        calls.append(list(args))
        if args[0] == "project":
            if not board_ok:
                raise RuntimeError("simulated board failure")
            return "{}"
        if len(args) >= 2 and args[0] == "issue" and args[1] == "list":
            label = args[args.index("--label") + 1] if "--label" in args else None
            return by_label.get(label, "[]")
        if len(args) >= 2 and args[0] == "api" and str(args[1]).endswith("/issues"):
            # #2757: the list read goes over REST (`sources.fetch_issues_rest`), a separate budget
            # from the GraphQL one `gh issue list --label` bills against.
            fields = dict(str(a).split("=", 1) for a in args if "=" in str(a))
            if fields.get("page", "1") != "1":
                return "[]"
            return by_label.get(fields.get("labels"), "[]")
        # #895: the target/blocker state read is REST first (`api repos/<o>/<r>/issues/N --method GET`),
        # answered REST-shaped from the same gh-shape `views` (MERGED -> closed + pull_request.merged_at).
        rest = gqlfake.rest_issue(args, lambda n, f: views.get(n, default_view))
        if rest is not None:
            return rest
        if len(args) >= 3 and args[0] == "issue" and args[1] == "view":
            fallbacks.append(list(args))      # the one fallback; REST-first tests assert this empty
            return views.get(str(args[2]), default_view)
        return ""
    run.calls = calls
    run.fallbacks = fallbacks
    run.gql_calls = gql_calls
    run.rest_writes = rest_writes
    return run


def _view(*labels, state="OPEN", author=None, body=None, state_reason=None):
    payload = {"labels": [{"name": n} for n in labels], "state": state}
    if author is not None:
        payload["author"] = {"login": author}
    if body is not None:
        payload["body"] = body
    if state_reason is not None:
        payload["stateReason"] = state_reason
    return json.dumps(payload)


# ------------------------------------------------ the promotion the next pick would undo (#1569)

UNIT = "int-contract"
LABEL = "feature:" + UNIT
REPO = "acme/widget"


def _mod_with(name, old, new):
    """The module rebuilt with a source substitution applied -- `tests/test_feature_owner.py`'s
    `_mod_with`, both guards and both reasons: the target must still exist, and every occurrence is
    replaced so a mutant cannot survive by hiding in a second copy."""
    import types
    path = S / (name + ".py")
    src = path.read_text(encoding="utf-8")
    assert old in src, "mutation target has drifted out of the source: %r" % (old,)
    ns = {"__name__": name + "_variant", "__file__": str(path)}
    exec(compile(src.replace(old, new), str(path), "exec"), ns)          # noqa: S102 - test-only
    return types.SimpleNamespace(**ns)


def _entry(owner="@unit-owner", board="@board-owner", repos=(REPO,), authorized=False):
    return {"title": "Manifest duration contract", "owner": owner, "open": True, "parent": None,
            "tracking_issue": None,
            "repos": {r: {"branch": "feature/" + UNIT, "owner": board,
                          "authorized": authorized, "goals": []} for r in repos}}


def _sdlc(tmp_path, entry=None, adopted=True):
    """A REAL `.sdlc/features/` registry, so the two gate predicates read a real store rather than a
    stub -- the whole defect is that a promotion is decided by state neither label carries."""
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir(parents=True, exist_ok=True)
    if adopted:
        (sdlc / "features").mkdir(exist_ok=True)
        _mod("feature_registry").write_unit(sdlc / "features", UNIT,
                                            _entry() if entry is None else entry)
    return str(sdlc)


def test_promote_refuses_the_issue_the_ownership_gate_would_set_aside_again(tmp_path):
    """#1569. `feature_owner.gate_at_pick` recomputes from the registry and the issue's AUTHOR and
    reads no label, so promoting a goal it held returned it to the board for exactly one pick. The
    operator paid a full cycle to discover a no-op, and the gate's own comment sent them round it."""
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:needs-confirmation", LABEL, author="a-stranger")})
    result = p.promote(_sdlc(tmp_path), _config(), ["5"], run=run)
    assert result["results"][0]["outcome"] == "skipped"
    assert not any(str(a).startswith("query=mutation") for c in run.gql_calls for a in c)


def test_the_refusal_names_the_registry_edit_and_never_another_promote(tmp_path):
    """A refusal inside the tool built to remove dead ends must not itself be one: the route it
    names has to be one the reader can actually perform."""
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:needs-confirmation", LABEL, author="a-stranger")})
    detail = p.promote(_sdlc(tmp_path), _config(), ["5"], run=run)["results"][0]["detail"]
    assert "repos.%s.authorized = true" % REPO in detail
    assert "the next pick sets it aside again" in detail
    assert "owns neither" in detail            # the shared clause, not a fourth spelling of it


def test_the_refusal_says_the_true_thing_to_a_unit_owner(tmp_path):
    """THE FOURTH CHANNEL, and it renders through `feature_owner.refusal_clause` rather than
    branching for itself -- the unit's own owner must not be told they own neither."""
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:needs-confirmation", LABEL, author="unit-owner")})
    detail = p.promote(_sdlc(tmp_path), _config(), ["5"], run=run)["results"][0]["detail"]
    assert "owns neither" not in detail
    assert "owns unit" in detail and "board belongs to somebody else" in detail


def test_promote_proceeds_the_moment_the_registry_says_it_may(tmp_path):
    """The other half of the same property: the refusal has to STOP once the named edit is made, or
    it is a dead end of its own."""
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:needs-confirmation", LABEL, author="a-stranger")})
    sdlc = _sdlc(tmp_path, entry=_entry(authorized=True))
    result = p.promote(sdlc, _config(), ["5"], run=run)
    assert result["results"][0]["outcome"] == "promoted"


def test_promote_is_untouched_for_the_board_owners_own_issue(tmp_path):
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:needs-confirmation", LABEL, author="board-owner")})
    result = p.promote(_sdlc(tmp_path), _config(), ["5"], run=run)
    assert result["results"][0]["outcome"] == "promoted"


def test_promote_refuses_a_scope_expansion_and_names_the_repo_to_add(tmp_path):
    """The second `sdlc:needs-confirmation` gate. Its own flag comment already had the ORDER right
    -- accept the repo first, then promote -- and `_refusal` had no arm for it either."""
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:needs-confirmation", LABEL, author="board-owner")})
    sdlc = _sdlc(tmp_path, entry=_entry(repos=("acme/other",)))
    result = p.promote(sdlc, _config(), ["5"], run=run)
    assert result["results"][0]["outcome"] == "skipped"
    assert "expands the unit's scope" in result["results"][0]["detail"]
    assert "add %s" % REPO in result["results"][0]["detail"]
    # ONE PICK, ONE REASON, and the order is `loop._next`'s: it short-circuits the `and` between the
    # two gates, so a goal refused for scope never also collects an ownership refusal. A goal in an
    # unlisted repo has no board owner recorded there by construction, so asking ownership first
    # would print `not-an-owner` for every one of them -- a reason the pick would not have given.
    assert "unit ownership still holds it" not in result["results"][0]["detail"]


def test_a_propagated_entry_is_never_refused_because_the_pick_would_name_its_board(tmp_path):
    """#1575's other end. A propagated entry names the unit's owner and nobody for THIS board, and
    the pick infers one from the author and proceeds -- so refusing here would be a dead end over a
    hold that does not exist. The two sides ask `_claimable_board`, once."""
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:needs-confirmation", LABEL, author="a-stranger")})
    sdlc = _sdlc(tmp_path, entry=_entry(board=None))
    assert p.promote(sdlc, _config(), ["5"], run=run)["results"][0]["outcome"] == "promoted"


def test_a_project_that_never_adopted_the_model_is_never_refused(tmp_path):
    """FAIL-OPEN. `.sdlc/features/` is what adoption means, and without it there is no gate to
    mirror -- the label on the issue is then just a label somebody made."""
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:needs-confirmation", LABEL, author="a-stranger")})
    sdlc = _sdlc(tmp_path, adopted=False)
    assert p.promote(sdlc, _config(), ["5"], run=run)["results"][0]["outcome"] == "promoted"


def test_an_issue_declaring_no_unit_is_never_refused(tmp_path):
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:needs-confirmation", author="a-stranger")})
    assert p.promote(_sdlc(tmp_path), _config(), ["5"],
                     run=run)["results"][0]["outcome"] == "promoted"


def test_an_author_gh_did_not_name_refuses_nobody(tmp_path):
    """A gate that cannot say WHO must not refuse on their behalf -- the same line `may_file` draws
    for `NO_ACTOR`, held on this side of it too."""
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:needs-confirmation", LABEL)})
    assert p.promote(_sdlc(tmp_path), _config(), ["5"],
                     run=run)["results"][0]["outcome"] == "promoted"


def test_a_demotion_is_never_refused_by_a_branching_model_hold(tmp_path):
    """A demotion takes work OUT of the queue, which is where a held goal is going anyway."""
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:goal", LABEL, author="a-stranger")})
    result = p.promote(_sdlc(tmp_path), _config(), ["5"], run=run, demote=True)
    assert result["results"][0]["outcome"] == "demoted"


def test_a_park_still_wins_over_the_hold(tmp_path):
    """ONE reason, and the order is the judgement: a park is a human's decision and /sigma-unpark is
    its undo, so that message is the one that must be printed even where a unit also holds it."""
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:needs-confirmation", "sdlc:parked", LABEL,
                                    author="a-stranger")})
    detail = p.promote(_sdlc(tmp_path), _config(), ["5"], run=run)["results"][0]["detail"]
    assert "/sigma-unpark" in detail


def test_the_hold_wins_over_needs_label(tmp_path):
    """The other side of that ordering. `sdlc:needs-label` self-heals the moment somebody creates
    the label and the loop clears the overlay itself; a unit hold ends only when a human edits the
    registry. So the stronger claim is printed, exactly as the park beats needs-label."""
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:needs-confirmation", "sdlc:needs-label", LABEL,
                                    author="a-stranger")})
    detail = p.promote(_sdlc(tmp_path), _config(), ["5"], run=run)["results"][0]["detail"]
    assert "authorized = true" in detail and "waiting on a `feature:` label" not in detail


def test_promoting_an_issue_the_next_pick_undoes_is_a_mutant_this_suite_kills(tmp_path):
    """The mutant is the shipped behaviour: no arm, so the swap lands, the card moves, the comment
    is posted -- and one pick later the goal is inert again with nothing to show for it."""
    p = _mod("promote")
    mutant = _mod_with("promote", "    if hold:\n        return hold\n", "")
    view = {"5": _view("sdlc:needs-confirmation", LABEL, author="a-stranger")}
    sdlc = _sdlc(tmp_path)
    bad = mutant.promote(sdlc, _config(), ["5"], run=_runner(views=view))
    assert bad["results"][0]["outcome"] == "promoted"                      # the bug
    good = p.promote(sdlc, _config(), ["5"], run=_runner(views=view))
    assert good["results"][0]["outcome"] == "skipped"


def test_reading_the_author_and_body_costs_no_extra_call(tmp_path):
    """It rides on the issue read this function already makes. A second read for it would double
    the cost of every promotion, on every repo, adopted or not. #1888 added `body` (needed to check
    every `Blocked by:` reference the issue names) the same way #1569 added `author` -- one more
    `--json` field on a call this function already makes, never a second call."""
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:needs-confirmation", LABEL, author="a-stranger")})
    p.promote(_sdlc(tmp_path), _config(), ["5"], run=run)
    # #895: REST first -- ONE `api repos/acme/widget/issues/5 --method GET` carries author, body, state,
    # stateReason and labels; no `issue view`, and no read of any other issue.
    reads = [c for c in run.calls if gqlfake.rest_issue_target(c)]
    assert reads == [["api", "repos/acme/widget/issues/5", "--method", "GET"]]
    assert run.fallbacks == []


# --------------------------------------------------------------------------- blocked-by edges (#1888)
#
# #1888: the exact real mistake. Tonight, promoting a batch of `sdlc:needs-confirmation` issues by
# hand used a check that only caught the FIRST `Blocked by:` line per body -- #1773 named TWO
# (`Blocked by: #1766`, closed, AND `Blocked by: #1758`, still genuinely open) and the second one
# was never seen. `apply` must now parse EVERY such reference (reusing `blocker_scan.extract_refs`,
# the same whole-body `finditer`-based scanner `mirror.py` and the pick-time dependency gate already
# read blockers off of -- never a second, ad hoc parser) and refuse unless every one resolves to a
# LIVE, confirmed CLOSED issue -- not "carries sdlc:goal", not "was promoted in this same batch".


def test_apply_refuses_when_a_second_blocked_by_line_is_still_open():
    """The exact real scenario: two `Blocked by:` lines, one closed, one still open."""
    p = _mod("promote")
    body = "Some context.\n\n**Blocked by:** #1766\n**Blocked by:** #1758\n"
    run = _runner(views={
        "5": _view("sdlc:needs-confirmation", body=body),
        "1766": _view(state="CLOSED", state_reason="COMPLETED"),
        "1758": _view(state="OPEN"),
    })
    result = p.promote(".sdlc", _config(), ["5"], run=run)
    assert result["results"][0]["outcome"] == "skipped"
    detail = result["results"][0]["detail"]
    # The refusal names the UNRESOLVED blocker (what's actually wrong, and what to go fix) -- the
    # already-closed one is not repeated here, though `apply --dry-run` on the same issue (below)
    # does show both, per the issue's explicit ask for THAT surface.
    assert "#1758" in detail and "OPEN" in detail
    assert "#1766" not in detail
    assert not any(str(a).startswith("query=mutation") for c in run.gql_calls for a in c)


def test_apply_promotes_once_every_named_blocker_is_actually_closed():
    p = _mod("promote")
    body = "**Blocked by:** #1766\n**Blocked by:** #1758\n"
    run = _runner(views={
        "5": _view("sdlc:needs-confirmation", body=body),
        "1766": _view(state="CLOSED", state_reason="COMPLETED"),
        "1758": _view(state="CLOSED", state_reason="COMPLETED"),
    })
    result = p.promote(".sdlc", _config(), ["5"], run=run)
    assert result["results"][0]["outcome"] == "promoted"
    detail = result["results"][0]["detail"]
    assert "#1766" in detail and "#1758" in detail and "CLOSED" in detail


def test_a_single_blocked_by_line_still_refuses_on_its_own():
    """Guards the N=1 case against a fix that only special-cases N>=2."""
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:needs-confirmation", body="**Blocked by:** #9\n"),
                        "9": _view(state="OPEN")})
    result = p.promote(".sdlc", _config(), ["5"], run=run)
    assert result["results"][0]["outcome"] == "skipped"
    assert "#9" in result["results"][0]["detail"]


def test_apply_refuses_when_only_one_of_three_named_blockers_is_still_open():
    """Coverage guard: every other test here uses exactly 2 blockers, which a hypothetical
    first-TWO-only bug could dodge. The real implementation uses whole-list comprehensions with no
    slicing, so this is a belt-and-braces pin, not an expected failure."""
    p = _mod("promote")
    body = "**Blocked by:** #10\n**Blocked by:** #11\n**Blocked by:** #12\n"
    run = _runner(views={"5": _view("sdlc:needs-confirmation", body=body),
                        "10": _view(state="CLOSED", state_reason="COMPLETED"),
                        "11": _view(state="CLOSED", state_reason="COMPLETED"),
                        "12": _view(state="OPEN")})
    result = p.promote(".sdlc", _config(), ["5"], run=run)
    assert result["results"][0]["outcome"] == "skipped"
    detail = result["results"][0]["detail"]
    assert "#12" in detail and "OPEN" in detail
    assert "#10" not in detail and "#11" not in detail    # only the UNRESOLVED ones are named


def test_an_unreadable_blocker_refuses_rather_than_assuming_it_is_fine():
    """Every OTHER hold in this file fails open on an unreadable state -- the worst case there is an
    autonomous sweep skipping a goal it could safely have touched. This one guards a human's
    explicit "yes, ship this" gesture, so an unconfirmed blocker must read as unresolved, never as
    closed -- the same posture `_state()` itself already takes for the target issue's own state
    (`test_promote_refuses_when_the_current_state_cannot_be_read`), applied consistently here."""
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:needs-confirmation", body="**Blocked by:** #9\n")},
                 fail_on=["issues/9", "issue view 9"])
    result = p.promote(".sdlc", _config(), ["5"], run=run)
    assert result["results"][0]["outcome"] == "skipped"
    assert "#9" in result["results"][0]["detail"]


def test_dry_run_names_every_blocker_it_checked_and_its_resolved_state():
    """The issue's own explicit ask: dry-run output must show every blocker checked and its
    resolved state, so a human can catch a mismatch visually, not just trust the tool."""
    p = _mod("promote")
    body = "**Blocked by:** #1766\n**Blocked by:** #1758\n"
    run = _runner(views={"5": _view("sdlc:needs-confirmation", body=body),
                        "1766": _view(state="CLOSED", state_reason="COMPLETED"),
                        "1758": _view(state="CLOSED", state_reason="COMPLETED")})
    result = p.promote(".sdlc", _config(), ["5"], run=run, apply=False)
    assert result["results"][0]["outcome"] == "would"
    detail = result["results"][0]["detail"]
    assert "#1766" in detail and "CLOSED" in detail and "#1758" in detail
    assert not any(str(a).startswith("query=mutation") for c in run.gql_calls for a in c)


def test_no_blocker_marker_costs_nothing_extra_and_behaves_exactly_as_before():
    """The zero-cost common case every other hold in this file already keeps."""
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:needs-confirmation")})     # no body at all
    result = p.promote(".sdlc", _config(), ["5"], run=run)
    assert result["results"][0]["outcome"] == "promoted"
    assert result["results"][0]["detail"] == "+sdlc:goal -sdlc:needs-confirmation"
    views = [c for c in run.calls if gqlfake.rest_issue_target(c)]
    assert len(views) == 1 and run.fallbacks == []     # only #5 itself -- nothing to check, nothing fetched


def test_demote_is_never_held_by_an_open_blocker():
    """Taking work OUT of the queue is safe regardless of its declared blockers -- the same
    direction-asymmetry `_feature_hold` already applies on the demote path."""
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:goal", body="**Blocked by:** #9\n")})
    result = p.promote(".sdlc", _config(), ["5"], run=run, demote=True)
    assert result["results"][0]["outcome"] == "demoted"


def test_reverting_to_a_first_match_only_scan_lets_the_real_mistake_through():
    """#1888's own explicit negative control. Mutate the real multi-match scan down to a
    first-match-only one and confirm the SAME regression scenario above now wrongly promotes --
    modelled directly on this file's own
    `test_promoting_an_issue_the_next_pick_undoes_is_a_mutant_this_suite_kills`. A single-line/
    first-match reading of the body (whatever shape the historical mistake actually took) can never
    see past the first ref; slicing the real scanner's own output to its first element reproduces
    that OBSERVABLE defect exactly, regardless of what regex a from-scratch reimplementation would
    have used."""
    p = _mod("promote")
    mutant = _mod_with("promote",
                       "refs = blocker_scan.extract_refs(body, self_ref=number)",
                       "refs = blocker_scan.extract_refs(body, self_ref=number)[:1]")
    body = "**Blocked by:** #1766\n**Blocked by:** #1758\n"
    views = {"5": _view("sdlc:needs-confirmation", body=body),
             "1766": _view(state="CLOSED", state_reason="COMPLETED"), "1758": _view(state="OPEN")}
    bad = mutant.promote(".sdlc", _config(), ["5"], run=_runner(views=views))
    assert bad["results"][0]["outcome"] == "promoted"           # the historical bug, reproduced
    good = p.promote(".sdlc", _config(), ["5"], run=_runner(views=views))
    assert good["results"][0]["outcome"] == "skipped"


# ----------------------------------------------------- #1892: two untested overlaps + one docstring
#
# Independent pre-PR review of #1891 (this file's own #1888 section above) returned APPROVE with no
# blocking defects, but flagged two SAFE, already-`skipped` interactions that nothing pinned down.
# Both pass today; these tests are the control, not a fix.


def test_an_unresolved_blocker_wins_over_a_branching_model_hold(tmp_path):
    """`_refusal` checks the blocker overlap (#1888) ABOVE `_feature_hold`/`needs_label_label`
    (#1569) -- see its own docstring for the ordering judgement. Unlike the pre-existing
    `hold`-vs-`needs_label` pair, whose underlying conditions are near-mutually-exclusive, "names an
    unresolved `Blocked by:`" and "`_feature_hold` would also refuse" can genuinely co-occur on one
    real issue: this one carries both. Only the blocker message must be shown; the ownership hold is
    computed but discarded. No unsafe write happens either way (`skipped`), but nothing previously
    said which message wins."""
    p = _mod("promote")
    run = _runner(views={
        "5": _view("sdlc:needs-confirmation", LABEL, author="a-stranger",
                   body="**Blocked by:** #9\n"),
        "9": _view(state="OPEN"),
    })
    result = p.promote(_sdlc(tmp_path), _config(), ["5"], run=run)
    assert result["results"][0]["outcome"] == "skipped"
    detail = result["results"][0]["detail"]
    assert "#9" in detail and "not confirmed CLOSED" in detail
    assert "authorized = true" not in detail
    assert "unit ownership still holds it" not in detail
    assert not any(str(a).startswith("query=mutation") for c in run.gql_calls for a in c)


def test_reapplying_an_already_promoted_issue_with_an_open_blocker_still_refuses():
    """`_blocker_status`/`_refusal` run BEFORE the `settled` ("already carries the label -- nothing
    to do") short-circuit in `promote()`, so re-`apply`ing an issue that already crossed the gate but
    still names an open `Blocked by:` reports the blocker refusal, not "nothing to do". Still
    `skipped` either way (no label mutation), and arguably a useful side effect -- it would have
    flagged #1888's own wrongly-promoted issues on re-application -- but nothing previously pinned
    it down as intentional rather than accidental."""
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:goal", body="**Blocked by:** #9\n"),
                        "9": _view(state="OPEN")})
    result = p.promote(".sdlc", _config(), ["5"], run=run)
    assert result["results"][0]["outcome"] == "skipped"
    detail = result["results"][0]["detail"]
    assert "#9" in detail and "not confirmed CLOSED" in detail
    assert "already carries" not in detail
    assert not any(str(a).startswith("query=mutation") for c in run.gql_calls for a in c)


# --------------------------------------------------------------------------- survey / the buckets


def test_survey_splits_awaiting_from_the_half_promoted_drift_bucket():
    """The whole reason this module exists: an issue carrying BOTH labels is not awaiting approval,
    it is a human's half-finished approval, and the repair is the opposite of a promotion."""
    p = _mod("promote")
    run = _runner(by_label={"sdlc:needs-confirmation": json.dumps([
        _issue(1, "sdlc:needs-confirmation"),
        _issue(2, "sdlc:needs-confirmation", "sdlc:goal")])})
    result = p.survey(".sdlc", _config(), run=run)
    assert [r["number"] for r in result["buckets"]["awaiting"]] == ["1"]
    assert [r["number"] for r in result["buckets"]["drift"]] == ["2"]
    assert result["complete"] is True


def test_survey_surfaces_a_blocker_that_nothing_can_ever_pick():
    """The gap this bucket exists for: `sdlc:blocking` is a TIE-BREAK among already-eligible issues
    (`_blocking_priority_pending` reuses `_fetch_pending`, which always prepends `--label
    sdlc:goal`, and `gh issue list --label` ANDs), so a blocker without the goal label is reachable
    by no queue at all -- while `auto_unpark` will not resume what it blocks until it CLOSES."""
    p = _mod("promote")
    run = _runner(by_label={"sdlc:blocking": json.dumps([
        _issue(7, "sdlc:blocking"),                    # unreachable
        _issue(8, "sdlc:blocking", "sdlc:goal")])})    # already pickable -- not a deadlock
    result = p.survey(".sdlc", _config(), run=run)
    assert [r["number"] for r in result["buckets"]["deadlocked"]] == ["7"]
    assert "nothing can pick it" in result["buckets"]["deadlocked"][0]["why"]


def test_survey_never_double_counts_an_issue_that_is_both_blocking_and_awaiting():
    p = _mod("promote")
    both = json.dumps([_issue(9, "sdlc:needs-confirmation", "sdlc:blocking")])
    run = _runner(by_label={"sdlc:needs-confirmation": both, "sdlc:blocking": both})
    result = p.survey(".sdlc", _config(), run=run)
    assert [r["number"] for r in result["buckets"]["deadlocked"]] == ["9"]
    assert result["buckets"]["awaiting"] == []


def test_survey_reports_incomplete_rather_than_empty_when_a_query_fails():
    """A failed read must never render as "nothing to approve" -- the same fail-open-but-say-so
    contract `reconcile._fetch_by_label` states, for the same reason."""
    p = _mod("promote")
    run = _runner(fail_on=["labels=sdlc:needs-confirmation"])
    result = p.survey(".sdlc", _config(), run=run)
    assert result["complete"] is False
    assert any("could not read" in n for n in result["degraded"])


def test_survey_scope_defaults_to_the_configured_assignee_and_says_so():
    p = _mod("promote")
    run = _runner()
    scoped = p.survey(".sdlc", _config(assignee="@me"), run=run)
    assert scoped["assignee"] == "@me"
    assert "@me" in p.render_survey(scoped)
    # ...and when the key is unset (the SHIPPED default) the scope is stated out loud, so an empty
    # result is never mistaken for "nothing is waiting on me"
    unscoped = p.survey(".sdlc", _config(), run=run)
    assert unscoped["assignee"] is None
    assert "discovery.github.assignee is not set" in p.render_survey(unscoped)


def test_survey_annotates_a_row_that_declares_a_blocker_with_no_extra_gh_call():
    """#1888: `list` gets the PARSING half (reusing the exact scanner `apply` verifies against) at
    ZERO additional network cost -- never live verification, which would turn survey's O(1)-batched
    shape into O(N) `gh` calls across a whole bucket. `apply --dry-run` is where live-verified state
    belongs; this is just enough for a human to know to check."""
    p = _mod("promote")
    body = "Some prose.\n\n**Blocked by:** #42\n"
    run = _runner(by_label={"sdlc:needs-confirmation": json.dumps([
        _issue(1, "sdlc:needs-confirmation", body=body)])})
    result = p.survey(".sdlc", _config(), run=run)
    assert result["buckets"]["awaiting"][0]["blockers"] == ["42"]
    assert "declares blocker" in p.render_survey(result)
    assert not any(c[:2] == ["issue", "view"] for c in run.calls)      # no per-issue live check


def test_survey_degrades_on_a_source_with_no_labels_at_all():
    p = _mod("promote")

    class Local:
        pass
    result = p.survey(".sdlc", {"discovery": {"source": "local-goals"}}, source=Local())
    assert result["complete"] is False
    assert any("github discovery mode" in n for n in result["degraded"])


# --------------------------------------------------------------------------- promote / demote


def test_promote_is_one_atomic_swap_and_never_a_gh_issue_edit():
    """The defect this whole release exists to remove: `gh issue edit --add-label X --remove-label
    Y` is FOUR parallel HTTP requests, not one write, so a partial application leaves either both
    labels or neither."""
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:needs-confirmation")})
    result = p.promote(".sdlc", _config(), ["5"], run=run)
    assert [r["outcome"] for r in result["results"]] == ["promoted"]
    mutations = [c for c in run.gql_calls
                 if any(str(a).startswith("query=mutation") for a in c)]
    assert len(mutations) == 1
    doc = next(a for a in mutations[0] if str(a).startswith("query=mutation"))
    assert "a: addLabelsToLabelable" in doc and "r: removeLabelsFromLabelable" in doc
    # ADD is aliased first on purpose: a partial application must leave BOTH labels (recoverable),
    # never neither (zero-label limbo, invisible to every label query the kit makes)
    assert doc.index("addLabelsToLabelable") < doc.index("removeLabelsFromLabelable")


def _board_source(promote_mod, run, moved=True):
    """A real `GitHubSource` on a board-authoritative config with ONLY `_set_board_status` stubbed.
    The board mechanics themselves (item resolution, field ids, the `gh project` call shapes) are
    `test_github_project.py`'s subject; what belongs HERE is that promote calls it with the right
    column and reacts honestly to the bool it returns."""
    src = promote_mod.sources.GitHubSource(_board_config(), run=run)
    src.board_calls = []

    def _set(goal, status_name):
        src.board_calls.append((str(goal), status_name))
        return moved
    src._set_board_status = _set
    return src


def test_promote_moves_the_board_card_to_ready():
    """#1391 step 4's lesson: fixing the label and leaving the card where it sits manufactures a
    permanently-unpickable goal on a board-authoritative repo."""
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:needs-confirmation")})
    src = _board_source(p, run)
    p.promote(".sdlc", _board_config(), ["5"], source=src)
    assert src.board_calls == [("5", "Ready")]


def test_demote_sends_the_card_back_to_backlog_not_ready():
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:goal")})
    src = _board_source(p, run)
    p.promote(".sdlc", _board_config(), ["5"], source=src, demote=True)
    assert src.board_calls == [("5", "Backlog")]


def test_promote_reports_a_card_that_did_not_move_without_calling_the_promotion_failed():
    """The label DID land, so the issue is genuinely across the gate on a label-queue repo -- but on
    a board-authoritative one the card is now what holds it back, and only an honest bool can say so."""
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:needs-confirmation")})
    result = p.promote(".sdlc", _board_config(), ["5"],
                       source=_board_source(p, run, moved=False))
    assert result["results"][0]["outcome"] == "promoted"
    assert "board card did not move" in result["results"][0]["detail"]


def test_a_label_queue_repo_is_never_warned_about_a_card_it_does_not_have():
    """`_set_board_status` returns False for a board-DISABLED repo too -- honest, but not a problem
    there. Warning on every promotion would be pure noise for every label-queue adopter."""
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:needs-confirmation")})
    result = p.promote(".sdlc", _config(), ["5"], run=run)          # no `project` block at all
    assert result["results"][0]["detail"] == "+sdlc:goal -sdlc:needs-confirmation"


def test_promote_refuses_a_parked_issue_and_names_the_route_out():
    """A refusal inside the tool built to remove dead ends must not itself be one."""
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:needs-confirmation", "sdlc:parked")})
    result = p.promote(".sdlc", _config(), ["5"], run=run)
    assert result["results"][0]["outcome"] == "skipped"
    assert "/sigma-unpark" in result["results"][0]["detail"]
    assert not any(str(a).startswith("query=mutation") for c in run.gql_calls for a in c)


def test_promote_refuses_a_closed_issue():
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:needs-confirmation",
                                    state="CLOSED", state_reason="COMPLETED")})
    result = p.promote(".sdlc", _config(), ["5"], run=run)
    assert result["results"][0]["outcome"] == "skipped"
    assert "closed" in result["results"][0]["detail"]


def test_promote_refuses_when_the_current_state_cannot_be_read():
    """Every refusal above is a SAFETY check, and a check that fails open is not a check."""
    p = _mod("promote")
    run = _runner(fail_on=["issues/5", "issue view 5"])
    result = p.promote(".sdlc", _config(), ["5"], run=run)
    assert result["results"][0]["outcome"] == "failed"
    assert not any(str(a).startswith("query=mutation") for c in run.gql_calls for a in c)


def test_promote_repairs_the_drift_case_with_a_remove_only_swap():
    """The half-promoted issue already carries `sdlc:goal`; the repair is removing the OTHER label,
    which is a valid one-sided swap (`_swap_labels` emits only the root mutations it needs)."""
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:goal", "sdlc:needs-confirmation")})
    result = p.promote(".sdlc", _config(), ["5"], run=run)
    assert result["results"][0]["outcome"] == "promoted"
    doc = next(a for c in run.gql_calls for a in c if str(a).startswith("query=mutation"))
    assert "removeLabelsFromLabelable" in doc and "addLabelsToLabelable" not in doc


def test_promote_is_a_no_op_on_an_issue_already_across_the_gate():
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:goal")})
    result = p.promote(".sdlc", _config(), ["5"], run=run)
    assert result["results"][0]["outcome"] == "skipped"
    assert "nothing to do" in result["results"][0]["detail"]
    assert not any(str(a).startswith("query=mutation") for c in run.gql_calls for a in c)


def test_one_failure_never_stops_the_rest():
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:needs-confirmation"),
                         "6": _view("sdlc:needs-confirmation")},
                  fail_on=["issue(number: 5)"])
    result = p.promote(".sdlc", _config(), ["5", "6"], run=run)
    assert [r["outcome"] for r in result["results"]] == ["failed", "promoted"]


def test_dry_run_writes_nothing_but_names_the_exact_swap():
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:needs-confirmation")})
    result = p.promote(".sdlc", _config(), ["5"], run=run, apply=False)
    assert result["results"][0]["outcome"] == "would"
    assert result["results"][0]["detail"] == "+sdlc:goal -sdlc:needs-confirmation"
    assert not any(str(a).startswith("query=mutation") for c in run.gql_calls for a in c)


def test_demote_is_the_inverse_and_clears_the_overlays_with_membership():
    """Demotion gives up MEMBERSHIP, so the overlays go with it: an overlay is only ever legitimate
    alongside `sdlc:goal`, and leaving `sdlc:in-progress` on an issue that is no longer a goal is
    the orphan shape the census exists to find."""
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:goal", "sdlc:in-progress")})
    result = p.promote(".sdlc", dict(_config(), ai_filed={"triage": {"enabled": False}}), ["5"], run=run,
                       demote=True)
    assert result["results"][0]["outcome"] == "demoted"
    assert result["results"][0]["detail"] == "+sdlc:needs-confirmation -sdlc:goal -sdlc:in-progress"


def test_demote_parks_with_reason():
    """#1006: with triage on (the default) a demotion writes the park label, never the legacy one, and the
    comment carries the reason and the declared question kind."""
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:goal", "sdlc:in-progress")})
    result = p.promote(".sdlc", _config(), ["5"], run=run, demote=True)
    assert result["results"][0]["outcome"] == "demoted"
    assert result["results"][0]["detail"] == "+sdlc:parked -sdlc:goal -sdlc:in-progress"
    comment = next(c[c.index("--body") + 1] for c in run.calls if c[:2] == ["issue", "comment"])
    assert "Parked via /sigma-promote" in comment and "sigma-qkind: needs_decision" in comment
    assert "needs-confirmation" not in comment


def test_demote_refuses_a_parked_issue():
    """#1393, reversing this test's own earlier position: it used to assert demotion was allowed on
    anything, on the grounds that taking work OUT of the queue is always safe. It is not safe here.
    A park is a human's membership decision, and writing `sdlc:needs-confirmation` onto a parked
    issue leaves TWO membership labels at once -- Sigma overwriting one human-gated state with a
    different one, which is nobody's idea of an undo. The undo for a park is /sigma-unpark."""
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:parked")})
    result = p.promote(".sdlc", _config(), ["5"], run=run, demote=True)
    assert result["results"][0]["outcome"] == "skipped"
    assert "/sigma-unpark" in result["results"][0]["detail"]
    assert not any(str(a).startswith("query=mutation") for c in run.gql_calls for a in c)


def test_promote_leaves_an_audit_comment_that_cannot_read_as_a_blocker():
    """`_BLOCK_RE` triggers on needs/after/requires/waiting-on within 40 chars of a `#N` -- the
    exact trap Sigma's own park boilerplate fell into (#1186). This prose must not repeat it."""
    p = _mod("promote")
    backlog_check = _mod("backlog_check")
    run = _runner(views={"5": _view("sdlc:needs-confirmation")})
    p.promote(".sdlc", _config(), ["5"], run=run)
    comment = next(c[c.index("--body") + 1] for c in run.calls if c[:2] == ["issue", "comment"])
    assert backlog_check._BLOCK_RE.search(comment + " #123") is None


def test_a_failed_audit_comment_never_undoes_a_landed_promotion():
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:needs-confirmation")}, fail_on=["issues/5/comments"])
    result = p.promote(".sdlc", _config(), ["5"], run=run)
    assert result["results"][0]["outcome"] == "promoted"


# --------------------------------------------------------------------------- CLI


def test_cli_list_exits_nonzero_when_the_census_is_incomplete(capsys, tmp_path):
    p = _mod("promote")
    (tmp_path / "config.json").write_text(json.dumps(_config()))
    run = _runner(fail_on=["labels=sdlc:needs-confirmation"])
    assert p.main(["promote.py", "list", str(tmp_path)], run=run) == 1
    assert "could not read" in capsys.readouterr().out


def test_cli_apply_exits_nonzero_on_a_failed_write(capsys, tmp_path):
    p = _mod("promote")
    (tmp_path / "config.json").write_text(json.dumps(_config()))
    run = _runner(fail_on=["issues/5", "issue view 5"])
    assert p.main(["promote.py", "apply", str(tmp_path), "5"], run=run) == 1


def test_cli_usage_on_a_bogus_verb(capsys):
    p = _mod("promote")
    assert p.main(["promote.py", "bogus"]) == 2
    assert "promote.py list" in capsys.readouterr().err


# --- #2532: a merged PR named as a blocker refuses promotion forever -- a permanent deadlock ----
# `_state()` used to compute `closed = state == "CLOSED"`, which reads a MERGED PR as still open
# (never resolves) and, in the OTHER direction, a PR closed WITHOUT merging as resolved (resolves
# immediately) -- both backwards. `blocker_scan.closed_state` is the one shared fix, also used by
# blockers.py and auto_unpark.py's own _ref_is_open (the pattern this rule was extracted from).


def test_apply_promotes_when_the_named_blocker_is_a_merged_pr():
    """The issue's own live repro: #3491 blocked by #3483, a merged PR."""
    p = _mod("promote")
    run = _runner(views={
        "3491": _view("sdlc:needs-confirmation", body="**Blocked by:** #3483\n"),
        "3483": _view(state="MERGED"),
    })
    result = p.promote(".sdlc", _config(), ["3491"], run=run)
    assert result["results"][0]["outcome"] == "promoted"


def test_a_single_merged_pr_blocker_still_promotes_on_its_own():
    """Guards the N=1 case, mirroring test_a_single_blocked_by_line_still_refuses_on_its_own's own
    stated purpose in the opposite direction."""
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:needs-confirmation", body="**Blocked by:** #9\n"),
                        "9": _view(state="MERGED")})
    result = p.promote(".sdlc", _config(), ["5"], run=run)
    assert result["results"][0]["outcome"] == "promoted"


def test_a_mix_of_closed_and_merged_blockers_all_resolve():
    p = _mod("promote")
    body = "**Blocked by:** #1766\n**Blocked by:** #1758\n"
    run = _runner(views={
        "5": _view("sdlc:needs-confirmation", body=body),
        "1766": _view(state="CLOSED", state_reason="COMPLETED"),
        "1758": _view(state="MERGED"),
    })
    result = p.promote(".sdlc", _config(), ["5"], run=run)
    assert result["results"][0]["outcome"] == "promoted"


def test_a_genuinely_open_pr_blocker_still_refuses():
    """False-negative control: an OPEN blocker (not yet merged) still refuses exactly as before."""
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:needs-confirmation", body="**Blocked by:** #9\n"),
                        "9": _view(state="OPEN")})
    result = p.promote(".sdlc", _config(), ["5"], run=run)
    assert result["results"][0]["outcome"] == "skipped"


def test_a_pr_closed_without_merging_still_refuses():
    """THE case the naive MERGED-collapse gets wrong: a PR closed WITHOUT merging reports
    state=CLOSED with no stateReason (real GitHub never sets one on a PR, any state) -- must NOT
    read as resolved just because it shares the bare CLOSED state a genuinely-closed issue has."""
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:needs-confirmation", body="**Blocked by:** #9\n"),
                        "9": _view(state="CLOSED")})   # no state_reason -- a closed-without-merge PR
    result = p.promote(".sdlc", _config(), ["5"], run=run)
    assert result["results"][0]["outcome"] == "skipped"
    assert "#9" in result["results"][0]["detail"]


def test_blocker_note_reports_a_merged_pr_as_closed():
    """The dry-run text names it CLOSED, not a new MERGED label -- the deliberate collapse-not-
    taxonomy design choice, round-tripped into a test rather than left as prose."""
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:needs-confirmation", body="**Blocked by:** #9\n"),
                        "9": _view(state="MERGED")})
    result = p.promote(".sdlc", _config(), ["5"], run=run)
    detail = result["results"][0]["detail"]
    assert "#9 CLOSED" in detail
    assert "MERGED" not in detail


def test_promoting_the_issue_itself_is_unaffected_by_the_merged_widening():
    """The second call site: an ordinary CLOSED issue (with a real stateReason) being promoted
    still refuses exactly as before."""
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:needs-confirmation",
                                    state="CLOSED", state_reason="COMPLETED")})
    result = p.promote(".sdlc", _config(), ["5"], run=run)
    assert result["results"][0]["outcome"] == "skipped"
    assert "is closed" in result["results"][0]["detail"]


def test_promoting_a_merged_pr_number_by_mistake_now_refuses_instead_of_proceeding():
    """A human/agent typos a PR number into `promote.py apply`. Pre-fix this would have proceeded
    past the closed-check with a merged PR's data; post-fix it refuses loudly."""
    p = _mod("promote")
    run = _runner(views={"3483": _view(state="MERGED")})
    result = p.promote(".sdlc", _config(), ["3483"], run=run)
    assert result["results"][0]["outcome"] == "skipped"
    assert "is closed" in result["results"][0]["detail"]


# --------------------------------------------------------------- an unread queue is not an empty one (#2757)

RATE_LIMITED = "GraphQL: API rate limit already exceeded for user ID 248840983."


def _failing_list(message, label="sdlc:needs-confirmation", **kw):
    inner = _runner(**kw)

    def run(args):
        joined = " ".join(str(a) for a in args)
        if "labels=" + label in joined or ("--label " + label) in joined:
            raise RuntimeError("gh %s failed: %s" % (joined, message))
        return inner(args)
    run.calls = inner.calls
    return run


def test_survey_reads_the_queues_over_rest_not_graphql():
    """#2757 fix 3: `gh issue list --label` routes through GraphQL `search()`, the budget several
    concurrent loops exhaust; REST (`gh api repos/{o}/{r}/issues`) is a separate budget."""
    p = _mod("promote")
    run = _runner(by_label={"sdlc:needs-confirmation": json.dumps([_issue(1, "sdlc:needs-confirmation")])})
    result = p.survey(".sdlc", _config(), run=run)
    assert [r["number"] for r in result["buckets"]["awaiting"]] == ["1"]
    assert not any(c[:2] == ["issue", "list"] for c in run.calls), run.calls
    assert any(c[:2] == ["api", "repos/acme/widget/issues"] for c in run.calls), run.calls


def test_an_unread_bucket_renders_unknown_with_its_reason_first_never_zero():
    """#2757's done-when: with the read failing, `list` prints UNKNOWN for the affected buckets with
    the reason at the TOP, and never `— 0` / `none` for a bucket it could not read. Observed live:
    "Awaiting approval — 0 / none" while 43 issues were waiting."""
    p = _mod("promote")
    run = _failing_list(RATE_LIMITED, by_label={"sdlc:blocking": json.dumps([_issue(7, "sdlc:blocking")])})
    result = p.survey(".sdlc", _config(), run=run)
    text = p.render_survey(result)
    lines = text.splitlines()
    assert lines[1].startswith("NOTE:"), text
    assert "rate limit" in lines[1].lower(), text
    awaiting = [l for l in lines if l.startswith("Awaiting approval")]
    assert awaiting and "UNKNOWN" in awaiting[0] and "rate" in awaiting[0].lower(), text
    drift = [l for l in lines if l.startswith("Half-promoted")]
    assert drift and "UNKNOWN" in drift[0], text
    assert "Awaiting approval — 0" not in text and "Half-promoted (carries both labels) — 0" not in text
    # the bucket that WAS read still reads as a real count
    assert "Blocking other work, unpickable — 1" in text
    i = lines.index(awaiting[0])
    assert lines[i + 1] != "  none", text


def test_the_json_output_carries_each_buckets_read_status():
    p = _mod("promote")
    result = p.survey(".sdlc", _config(), run=_failing_list(RATE_LIMITED))
    assert result["complete"] is False
    assert result["read"] == {"awaiting": False, "drift": False, "deadlocked": True}
    assert "rate limit" in result["errors"]["awaiting"].lower()


def test_cli_list_on_a_failed_read_exits_nonzero_and_never_prints_zero(capsys, tmp_path):
    p = _mod("promote")
    (tmp_path / "config.json").write_text(json.dumps(_config()))
    assert p.main(["promote.py", "list", str(tmp_path)], run=_failing_list(RATE_LIMITED)) == 1
    out = capsys.readouterr().out
    assert "UNKNOWN" in out and "Awaiting approval — 0" not in out


def test_apply_says_rate_limited_rather_than_unreadable_when_that_is_what_happened():
    """#2757's second done-when: `apply` reported "could not read the current state" for five
    issues that then promoted unchanged once the GraphQL quota reset."""
    p = _mod("promote")
    inner = _runner()

    def run(args):
        t = gqlfake.rest_issue_target(args)
        if t == ("5", False) or (args[:2] == ["issue", "view"] and str(args[2]) == "5"):
            raise RuntimeError("gh issue view 5 failed: " + RATE_LIMITED)
        if t == ("6", False) or (args[:2] == ["issue", "view"] and str(args[2]) == "6"):
            raise RuntimeError("gh issue view 6 failed: Could not resolve to an issue")
        return inner(args)
    result = p.promote(".sdlc", _config(), ["5", "6"], run=run)
    by = {r["number"]: r for r in result["results"]}
    assert by["5"]["outcome"] == "rate-limited", by
    assert "rate limit" in by["5"]["detail"].lower()
    assert by["6"]["outcome"] == "failed"
    assert "Could not resolve" in by["6"]["detail"]


def test_cli_apply_exits_nonzero_when_rate_limited(tmp_path):
    p = _mod("promote")
    (tmp_path / "config.json").write_text(json.dumps(_config()))
    inner = _runner()

    def run(args):
        if args[:2] == ["issue", "view"] or gqlfake.rest_issue_target(args):
            raise RuntimeError(RATE_LIMITED)
        return inner(args)
    assert p.main(["promote.py", "apply", str(tmp_path), "5"], run=run) == 1


# --- #895 slice 2b: the state read is REST first --------------------------------------------------


def test_a_response_with_no_state_is_refused_not_guessed():
    """REST answers 200 with no `state` at all: promote refuses (unreadable response), never promotes."""
    p = _mod("promote")
    inner = _runner(views={"5": _view("sdlc:needs-confirmation")})

    def run(args):
        if gqlfake.rest_issue_target(args) == ("5", False):
            return json.dumps({"number": 5, "labels": [{"name": "sdlc:needs-confirmation"}], "comments": 0,
                               "body": ""})
        return inner(args)
    result = p.promote(".sdlc", _config(), ["5"], run=run)
    assert result["results"][0]["outcome"] == "failed"
    assert not any(str(a).startswith("query=mutation") for c in inner.gql_calls for a in c)
    assert inner.fallbacks == []


def test_a_blocker_with_no_state_is_unresolved():
    p = _mod("promote")
    inner = _runner(views={"5": _view("sdlc:needs-confirmation", body="**Blocked by:** #9\n")})

    def run(args):
        if gqlfake.rest_issue_target(args) == ("9", False):
            return json.dumps({"number": 9, "labels": [], "comments": 0})
        return inner(args)
    result = p.promote(".sdlc", _config(), ["5"], run=run)
    assert result["results"][0]["outcome"] == "skipped"
    assert "#9" in result["results"][0]["detail"]


def test_a_merged_pr_blocker_reads_resolved_through_the_rest_mapping():
    """REST says `closed` + pull_request.merged_at; the mapping turns that into MERGED, which resolves."""
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:needs-confirmation", body="**Blocked by:** #9\n"),
                         "9": _view(state="MERGED")})
    result = p.promote(".sdlc", _config(), ["5"], run=run)
    assert result["results"][0]["outcome"] == "promoted", result
    assert run.fallbacks == []


def test_rest_5xx_falls_back_once_to_issue_view(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    monkeypatch.delenv("SIGMA_GH_GRAPHQL", raising=False)
    p = _mod("promote")
    inner = _runner(views={"5": _view("sdlc:needs-confirmation")})

    def run(args):
        if gqlfake.rest_issue_target(args):
            raise RuntimeError("gh: HTTP 502 Bad Gateway")
        return inner(args)
    result = p.promote(".sdlc", _config(), ["5"], run=run)
    assert result["results"][0]["outcome"] == "promoted", result
    assert [c[:3] for c in inner.fallbacks] == [["issue", "view", "5"]]


def test_the_audit_comment_is_a_rest_post_not_gh_issue_comment():
    """#895 slice 3a: promote's comment goes through `gh_api` REST; the failure arm stays a swallow."""
    p = _mod("promote")
    run = _runner(views={"5": _view("sdlc:needs-confirmation")})
    p.promote(".sdlc", _config(), ["5"], run=run)
    assert [c[1] for c in run.rest_writes] == ["repos/acme/widget/issues/5/comments"]
    bad = _runner(views={"5": _view("sdlc:needs-confirmation")}, fail_on=["issues/5/comments"])
    res = p.promote(".sdlc", _config(), ["5"], run=bad)
    assert res["results"][0]["outcome"] == "promoted"
