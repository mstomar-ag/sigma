"""#1468 (L1, epic #1464, story #1427): attach a missing `feature:` label at pick -- and NEVER
create one.

Three halves, and the second is the one that matters most:

  - `feature_labels.attach_at_pick` acts on `features.read`'s `body_only` verdict. The label
    EXISTS -> attach it, before anything else on that goal touches the label. The label does NOT
    exist -> set the goal aside behind the `sdlc:needs-label` OVERLAY and flag it for a human.
  - `gh label create` is never invoked for a `feature:*` label ON ANY PATH THROUGH THAT VERB. Every
    assertion about that is made against the RECORDED gh CALLS, never against a return value: a test
    that checks the outcome cannot tell "we did not create it" from "creating it happened to fail".
  - `feature_labels.resume_needs_label` removes the overlay again the moment the label exists, so the
    human performs exactly ONE gesture -- and it only ever touches goals THIS installation recorded
    blocking, never any `sdlc:needs-label` goal that happens to look fine.
"""
import ast, importlib.util, json, pathlib, re, tempfile

import pytest

import gqlfake

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location(name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


@pytest.fixture(autouse=True)
def _clean_unknown_labels():
    """`gqlfake._UNKNOWN_LABELS` is module state shared by every test in the process -- a name one
    test opts in must not leak into the next."""
    gqlfake._UNKNOWN_LABELS.clear()
    yield
    gqlfake._UNKNOWN_LABELS.clear()


_CONFIG = {"discovery": {"source": "github", "github": {"repo": "o/r"}}}

_DECLARES = "some prose\n\nFeature: voice-interview\nBranch: feature/voice-interview\n"
_RIVALS = "Feature: voice-interview\nFeature: billing\n"
_LABEL = "feature:voice-interview"


def _is_issues_list_call(c):
    """#1829: true for either the old `gh issue list` shape or the new `gh api
    repos/{owner}/{repo}/issues` REST shape `list_needs_label` now constructs, so every
    fake/assertion in this file can keep checking one predicate regardless of transport."""
    return (len(c) > 1 and c[0] == "issue" and c[1] == "list") or (
        len(c) > 1 and c[0] == "api" and str(c[1]).startswith("repos/") and str(c[1]).endswith("/issues"))


def _issues_list_labels(c):
    """The AND-ed label set an issues-list call (either shape) was filtering on."""
    for v in c:
        if isinstance(v, str) and v.startswith("labels="):
            return v[len("labels="):].split(",")
    return [c[i + 1] for i, x in enumerate(c) if x == "--label"]


def _runner(body="", labels=(), comments=(), absent=(), fail_on=()):
    """Fake `gh`, recording every call it is actually asked to make.

    `labels` is the issue's live label set (mutated by a real `_swap_labels` mutation, via
    `gqlfake`). `absent` names labels the REPOSITORY does not have -- the `repository.label(name:)`
    null that `_label_node_ids` reads as "no such label". `fail_on` makes one call shape raise."""
    calls = []
    live = set(labels)
    gqlfake._UNKNOWN_LABELS.update(absent)

    def run(args):
        joined = " ".join(str(a) for a in args)
        for needle in fail_on:
            if needle in joined:
                raise RuntimeError("simulated gh failure: %s" % needle)
        gql = gqlfake.swap(args, labels=live, calls=calls, repo_args=("--repo", "o/r"))
        if gql is not None:
            return gql
        calls.append(list(args))

        def view(_n, fields):
            out = {}
            if "title" in fields:
                out["title"] = "a goal"
            if "body" in fields:
                out["body"] = body
            if "labels" in fields:
                out["labels"] = [{"name": n} for n in sorted(live)]
            if "comments" in fields:
                out["comments"] = [{"body": c, "authorAssociation": "OWNER"} for c in comments]
            return out

        rest = gqlfake.rest_issue(args, view)
        if rest is not None:
            return rest
        if len(args) >= 2 and args[0] == "issue" and args[1] == "view":
            return json.dumps(view(args[2], (args[args.index("--json") + 1] if "--json" in args else "").split(",")))
        if _is_issues_list_call(args):
            # #1468: the needs-label sweep asks for its own label and needs body+labels back.
            # #1829: REST has no field-selection concept, so (unlike `issue view` above) body is
            # always present on a real REST issues-list response, not gated on a `--json` fields
            # string — this fake matches that.
            if any(l in live for l in _issues_list_labels(args)):
                return json.dumps([{"number": 42, "body": body,
                                    "labels": [{"name": n} for n in sorted(live)]}])
            return "[]"
        return ""

    run.calls = calls
    run.labels = live
    return run


def _queue_runner(issues, absent=()):
    """A fake `gh` over a whole BACKLOG: `issue list` reflects each issue's LIVE label set, so a
    label this run writes really does change what the next pick is offered. `issues` is
    `{number: {"body": ..., "labels": {...}}}`."""
    calls = []
    gqlfake._UNKNOWN_LABELS.update(absent)
    live = {str(n): set(spec.get("labels") or {"sdlc:goal"}) for n, spec in issues.items()}
    bodies = {str(n): spec.get("body") or "" for n, spec in issues.items()}
    comments = {str(n): list(spec.get("comments") or []) for n, spec in issues.items()}
    current = {"n": None}

    def run(args):
        # the swap fake needs to mutate the LABEL SET OF THE ISSUE THE MUTATION IS ABOUT, so route
        # it to whichever issue the preceding `issue(number:)` lookup named.
        target = live.get(gqlfake._GQL_LAST_ISSUE["n"]) if args[:2] == ["api", "graphql"] else None
        gql = gqlfake.swap(args, labels=target, calls=calls, repo_args=("--repo", "o/r"))
        if gql is not None:
            return gql
        calls.append(list(args))
        if _is_issues_list_call(args):
            wanted = _issues_list_labels(args)
            # #1829: REST comma-joined `labels=` ANDs (verified live); the old repeated `--label`
            # shape already ANDed too (`_fetch_pending`'s own base label + one extra), so both
            # transports need every wanted label present, not just one.
            out = []
            for n, s in sorted(live.items(), key=lambda kv: int(kv[0])):
                if wanted and not all(w in s for w in wanted):
                    continue
                # REST has no field-selection concept — body always comes back on the real
                # endpoint (verified live), so this fake always includes it too, unlike the old
                # `--json`-gated shape it replaces.
                row = {"number": int(n), "labels": [{"name": x} for x in sorted(s)],
                       "body": bodies.get(n, "")}
                out.append(row)
            return json.dumps(out)

        def view(n, fields):
            n = str(n)
            current["n"] = n
            out = {}
            if "title" in fields:
                out["title"] = "goal %s" % n
            if "body" in fields:
                out["body"] = bodies.get(n, "")
            if "labels" in fields:
                out["labels"] = [{"name": x} for x in sorted(live.get(n, ()))]
            if "comments" in fields:
                out["comments"] = [{"body": c} for c in comments.get(n, [])]
            return out

        rest = gqlfake.rest_issue(args, view)
        if rest is not None:
            return rest
        if args[:2] == ["issue", "view"]:
            return json.dumps(view(args[2], (args[args.index("--json") + 1] if "--json" in args else "").split(",")))
        if args[:2] == ["issue", "comment"]:
            comments.setdefault(str(args[2]), []).append(args[-1])
        return ""

    run.calls = calls
    run.live = live
    return run


def _source(run):
    gh = _mod("sources").GitHubSource(_CONFIG, run=run)
    gh._LABEL_SWAP_RETRIES, gh._LABEL_SWAP_RETRY_BASE = 1, 0
    return gh


def _flat(run):
    return [" ".join(str(a) for a in c) for c in run.calls]


def _label_creates(run):
    """Every recorded `gh label create` invocation, as an arg list. THE call log, not a verdict."""
    return [c for c in run.calls if len(c) >= 2 and c[0] == "label" and c[1] == "create"]


def _views(run, number=None):
    # #895: an issue read is REST-first, so count reads on either path (comment pages are not reads).
    return [c for c in run.calls if gqlfake.is_issue_read(c)
            and (number is None or _read_number(c) == str(number))]


def _read_number(c):
    t = gqlfake.rest_issue_target(c)
    return t[0] if t else str(c[2])


def _sdlc(d, ledger_on=True, max_iterations=10, run_iteration=0):
    base = pathlib.Path(d) / ".sdlc"
    (base / "state").mkdir(parents=True)
    cfg = dict(_CONFIG)
    cfg["ledger"] = {"enabled": ledger_on, "actor": "me", "lease": {"ttl_hours": 0}}
    cfg["budget"] = {"max_iterations": max_iterations}
    (base / "config.json").write_text(json.dumps(cfg))
    (base / "state" / "STATE.md").write_text(
        "iteration: %d\nrun_iteration: %d\nlast_run: none\n" % (run_iteration, run_iteration))
    return str(base), cfg


def _notes(base, goal=None):
    ledger = _mod("ledger")
    return [e for e in ledger.read_all(base)
            if e.get("kind") == "note" and (goal is None or e.get("goal") == str(goal))]


def _tokens(base, goal=None):
    return [str(e.get("why") or "").split(" ", 1)[0] for e in _notes(base, goal)]


# ------------------------------------------------------------------ the never-create guarantee

def test_gh_label_create_is_never_invoked_for_a_feature_label():
    """THE requirement, asserted where it is decidable: the recorded gh call log.

    `create_dependency` runs `gh label create --force` for EVERY label it attaches, and this is live
    TODAY rather than only under L3 -- `handoff.py` passes a free-text `--label` list straight into
    it, so `handoff track --label feature:voice` reaches this loop right now."""
    run = _runner()
    gh = _source(run)
    gh.create_dependency("t", "b", assignee=None, labels=[_LABEL, "area:loop"])
    created = [c[2] for c in _label_creates(run)]
    assert "area:loop" in created                       # an ordinary label is still ensured
    assert not any(str(n).lower().startswith("feature:") for n in created), created


def test_the_label_create_chokepoint_refuses_a_feature_label_outright():
    """`_run` is the single chokepoint every `gh` call in this class passes through. The site that
    makes that placement LOAD-BEARING is `triage._ensure_arbitrary_labels` (below), not
    `create_dependency` -- `create_dependency` lives in this same file, so a per-call-site rule
    would have covered it."""
    run = _runner()
    gh = _source(run)
    gh._run(["label", "create", _LABEL, "--repo", "o/r", "--force"])
    assert _label_creates(run) == []


def test_the_chokepoint_matches_the_prefix_case_insensitively():
    """GitHub label names are case-insensitively unique, so `Feature:Voice` and `feature:voice`
    are ONE name on a repo -- a case-sensitive guard would be bypassed by typing a capital."""
    run = _runner()
    gh = _source(run)
    gh._run(["label", "create", "Feature:Voice-Interview", "--force"])
    assert _label_creates(run) == []


def test_the_chokepoint_sees_a_feature_argument_anywhere_in_the_invocation():
    """The requirement is "never invoked WITH a `feature:*` ARGUMENT", not "never invoked with one
    in position 2" -- `gh` accepts flags before the name."""
    run = _runner()
    gh = _source(run)
    gh._run(["label", "create", "--force", "--color", "d4c5f9", _LABEL])
    assert _label_creates(run) == []


def test_the_chokepoint_still_creates_an_ordinary_label():
    """The guard is scoped to `feature:*` and to `label create` alone. Everything else -- every
    `sdlc:*` label `_ensure_labels` depends on -- must reach `gh` exactly as before."""
    run = _runner()
    gh = _source(run)
    gh._run(["label", "create", "sdlc:goal", "--force"])
    gh._run(["label", "create", "area:loop", "--force"])
    assert [c[2] for c in _label_creates(run)] == ["sdlc:goal", "area:loop"]


def test_the_chokepoint_does_not_block_a_merely_similarly_named_label():
    """The prefix INCLUDES the colon. `featureflag` is an ordinary label a repo may legitimately
    own; refusing it would be this guard quietly deciding what an adopter may name things."""
    run = _runner()
    gh = _source(run)
    gh._run(["label", "create", "featureflag", "--force"])
    gh._run(["label", "create", "feature", "--force"])
    assert [c[2] for c in _label_creates(run)] == ["featureflag", "feature"]


def test_the_chokepoint_does_not_block_reading_labels():
    """Only `label create` is refused. A `label list` naming a feature label is a READ, and reads
    are how the attach half decides anything at all."""
    run = _runner()
    gh = _source(run)
    gh._run(["label", "list", "--search", _LABEL])
    assert _flat(run) == ["label list --search %s" % _LABEL]


def test_the_refusal_is_not_silent():
    """The chokepoint's refusal must SAY so. Deleting the diagnostic and leaving a bare `return ""`
    survived the whole suite when nothing pinned it -- and stderr is the only channel this layer
    has, because it sits under every caller's own `try/except: pass`."""
    run = _runner()
    gh = _source(run)
    import io, contextlib
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        gh._run(["label", "create", _LABEL, "--force"])
    assert _LABEL in err.getvalue() and "never creates one" in err.getvalue()


def test_a_broken_stderr_cannot_break_the_chokepoint(monkeypatch):
    """Same promise `feature_labels._note` makes. The parallel branch there was tested and this one
    was not -- an asymmetry, not a policy."""
    src = _mod("sources")

    class Exploding:
        def write(self, _):
            raise ValueError("stderr is gone")

    monkeypatch.setattr(src.sys, "stderr", Exploding())
    run = _runner()
    gh = src.GitHubSource(_CONFIG, run=run)
    assert gh._run(["label", "create", _LABEL, "--force"]) == ""
    assert _label_creates(run) == []


def test_triage_ensure_arbitrary_labels_never_creates_a_feature_label():
    """triage.py's own `_ensure_arbitrary_labels` calls `source._run(["label", "create", ...])`
    directly -- a DIFFERENT MODULE reaching for this method -- so it inherits the chokepoint rather
    than needing its own copy of the rule. THIS is the site a per-call-site guard would have missed,
    and it is the one that justifies the placement."""
    triage = _mod("triage")
    run = _runner()
    gh = _source(run)
    triage._ensure_arbitrary_labels(gh, [_LABEL, "priority:P1"])
    assert [c[2] for c in _label_creates(run)] == ["priority:P1"]


def test_creates_a_feature_label_predicate_is_exact():
    fl = _mod("feature_labels")
    assert fl.creates_a_feature_label(["label", "create", "feature:x"])
    assert fl.creates_a_feature_label(["label", "create", "--force", "FEATURE:X"])
    assert not fl.creates_a_feature_label(["label", "create", "featureflag"])
    assert not fl.creates_a_feature_label(["label", "list", "feature:x"])
    assert not fl.creates_a_feature_label(["issue", "edit", "1", "--add-label", "feature:x"])
    assert not fl.creates_a_feature_label(["label", "create"])
    assert not fl.creates_a_feature_label([])


# ------------------------------------------------------------------ attach, the automatic half

def test_body_only_with_an_existing_label_attaches_it():
    fl = _mod("feature_labels")
    run = _runner(body=_DECLARES, labels={"sdlc:goal"})
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        decision = fl.attach_at_pick(base, gh, "42", cfg)
    assert decision.proceed is True
    assert decision.outcome == fl.ATTACHED and decision.unit == "voice-interview"
    assert _LABEL in run.labels
    assert any("--add-label %s" % _LABEL in c for c in _flat(run)), _flat(run)


def test_the_attach_never_creates_the_label_it_attaches():
    """Attaching is additive and reversible; creating is neither. Asserted on the call log."""
    fl = _mod("feature_labels")
    run = _runner(body=_DECLARES, labels={"sdlc:goal"})
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        fl.attach_at_pick(base, gh, "42", cfg)
    assert _label_creates(run) == []


def test_the_attach_is_recorded_in_the_ledger():
    """"Every attach is recorded in the ledger, like every other automatic action.\""""
    fl = _mod("feature_labels")
    run = _runner(body=_DECLARES, labels={"sdlc:goal"})
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        fl.attach_at_pick(base, gh, "42", cfg)
        notes = _notes(base, "42")
    assert [n["why"].split(" ", 1)[0] for n in notes] == [fl.ATTACH_TOKEN]
    assert _LABEL in notes[0]["why"]


def test_the_attach_never_sets_the_blocked_overlay():
    """The overlay is for the two refusals a HUMAN must clear. A successful attach must leave the
    goal exactly as pickable as it was."""
    fl = _mod("feature_labels")
    run = _runner(body=_DECLARES, labels={"sdlc:goal"})
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        fl.attach_at_pick(base, gh, "42", cfg)
    assert "sdlc:needs-label" not in run.labels


def test_a_label_that_is_already_attached_is_left_alone():
    """`agree` -- both halves declare the same unit. Nothing to do, and no write to make."""
    fl = _mod("feature_labels")
    run = _runner(body=_DECLARES, labels={"sdlc:goal", _LABEL})
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        decision = fl.attach_at_pick(base, gh, "42", cfg)
    assert decision.proceed is True and decision.outcome == fl.NOTHING_TO_DO
    assert not any("--add-label" in c for c in _flat(run)), _flat(run)


def test_a_goal_declaring_nothing_is_a_silent_no_op():
    """The property that makes adopting this break nothing: a body with no marker costs one read
    and produces ZERO writes -- no label, no comment, no ledger entry."""
    fl = _mod("feature_labels")
    run = _runner(body="ordinary issue text\n", labels={"sdlc:goal"})
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        decision = fl.attach_at_pick(base, gh, "42", cfg)
        assert _notes(base) == []
    assert decision.proceed is True and decision.outcome == fl.NOTHING_TO_DO
    assert all(gqlfake.is_issue_read(c) for c in run.calls), _flat(run)


def test_label_only_attaches_nothing():
    """The label is already the machine-readable truth; there is no missing half to attach."""
    fl = _mod("feature_labels")
    run = _runner(body="no marker here\n", labels={"sdlc:goal", _LABEL})
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        decision = fl.attach_at_pick(base, gh, "42", cfg)
    assert decision.proceed is True and decision.outcome == fl.NOTHING_TO_DO
    assert not any("--add-label" in c for c in _flat(run)), _flat(run)


# ------------------------------------------------------------------ conflict: write nothing, SAY so

def test_a_conflict_writes_no_label():
    """Swapping the label would remove something a human put there, to settle a disagreement this
    level was not asked to settle. No write."""
    fl = _mod("feature_labels")
    run = _runner(body="Feature: voice-interview\n", labels={"sdlc:goal", "feature:billing"})
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        decision = fl.attach_at_pick(base, gh, "42", cfg)
    assert decision.proceed is True
    assert not any("--add-label" in c or "--remove-label" in c for c in _flat(run)), _flat(run)


def test_a_conflict_is_a_distinct_outcome_from_agreement():
    """Folding a conflict into `NOTHING_TO_DO` made it indistinguishable from `agree` -- same
    `proceed`, same `outcome`, same `unit` -- so the level that owns unit ownership could not be
    handed the deferral at all. Deferring a decision means handing the next level something to
    decide on."""
    fl = _mod("feature_labels")
    conflict = _runner(body="Feature: voice-interview\n", labels={"sdlc:goal", "feature:billing"})
    agree = _runner(body="Feature: voice-interview\n", labels={"sdlc:goal", _LABEL})
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        a = fl.attach_at_pick(base, _source(conflict), "42", cfg)
        b = fl.attach_at_pick(base, _source(agree), "43", cfg)
    assert a.outcome == fl.CONFLICT_DECLARED and b.outcome == fl.NOTHING_TO_DO
    assert a.outcome != b.outcome


def test_a_conflict_is_recorded_in_the_ledger_naming_both_sides():
    """The two halves of the system will disagree about which unit this goal is in -- base
    resolution reads the BODY, everything else reads the LABEL. A silent divergence is the failure;
    the trace is what makes it findable."""
    fl = _mod("feature_labels")
    run = _runner(body="Feature: voice-interview\n", labels={"sdlc:goal", "feature:billing"})
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        fl.attach_at_pick(base, gh, "42", cfg)
        notes = _notes(base, "42")
    assert len(notes) == 1 and notes[0]["why"].startswith(fl.CONFLICT_TOKEN)
    assert "voice-interview" in notes[0]["why"] and "billing" in notes[0]["why"]


def test_a_conflict_says_so_on_stderr(capsys):
    fl = _mod("feature_labels")
    run = _runner(body="Feature: voice-interview\n", labels={"sdlc:goal", "feature:billing"})
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        fl.attach_at_pick(base, gh, "42", cfg)
    assert "voice-interview" in capsys.readouterr().err


# ------------------------------------------------------------------ refuse: the overlay + the flag

def test_a_body_declaring_a_nonexistent_label_refuses_the_pick():
    fl = _mod("feature_labels")
    run = _runner(body=_DECLARES, labels={"sdlc:goal"}, absent={_LABEL})
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        decision = fl.attach_at_pick(base, gh, "42", cfg)
    assert decision.proceed is False
    assert decision.outcome == fl.REFUSED_NO_LABEL and decision.unit == "voice-interview"
    assert not any("--add-label %s" % _LABEL in c for c in _flat(run)), _flat(run)


def test_a_refusal_never_falls_back_to_creating_the_label():
    """The whole point. A missing label means a typo or a unit nobody has opened -- both want a
    human, and neither wants Sigma minting a name that outlives the unit."""
    fl = _mod("feature_labels")
    run = _runner(body=_DECLARES, labels={"sdlc:goal"}, absent={_LABEL})
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        fl.attach_at_pick(base, gh, "42", cfg)
    assert not any(is_feature(c[2]) for c in _label_creates(run)), _label_creates(run)


def is_feature(name):
    return str(name).lower().startswith("feature:")


def test_a_refusal_sets_the_blocked_overlay_and_keeps_membership():
    """The state the model already ships: `sdlc:goal` KEPT (every sweep, census, mirror and reclaim
    path queries by it), `sdlc:needs-label` added. No new label is invented, and no membership is given
    up -- which is exactly what separates this from a park."""
    fl = _mod("feature_labels")
    run = _runner(body=_DECLARES, labels={"sdlc:goal"}, absent={_LABEL})
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        fl.attach_at_pick(base, gh, "42", cfg)
    assert "sdlc:goal" in run.labels                    # membership intact
    assert "sdlc:needs-label" in run.labels                 # the overlay, an existing label
    assert "sdlc:parked" not in run.labels              # NOT a park -- no human gesture is owed


def test_a_refused_goal_is_not_reported_as_ready_to_pick():
    """F1. `not_eligible_labels`' own docstring is "pickable iff it carries goal_label and none of
    these", and `/sigma-triage` and `/sigma-status` both read it. Before the overlay, a permanently
    refused goal was still counted as ready -- the exact failure #1393 was filed about, and one a
    compiled drain plan would happily schedule."""
    triage, src = _mod("triage"), _mod("sources")
    gh_cfg = _CONFIG["discovery"]["github"]
    refused = {"number": 42, "title": "poisoned", "state": "OPEN",
               "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:needs-label"}]}
    healthy = {"number": 43, "title": "fine", "state": "OPEN", "labels": [{"name": "sdlc:goal"}]}
    bucket = triage._bucket_enqueued([refused, healthy], gh_cfg, gh_source=_source(_runner()))
    assert [i["number"] for i in bucket["items"]] == [43], bucket
    assert "sdlc:needs-label" in src.GitHubSource(_CONFIG, run=_runner()).not_eligible_labels()


def test_a_refused_pick_is_flagged_on_the_issue():
    """"Surface it" has to reach a person. stderr alone is invisible in an unattended run, so the
    flag is a comment on the issue itself, naming the unit and the label that is missing."""
    fl = _mod("feature_labels")
    run = _runner(body=_DECLARES, labels={"sdlc:goal"}, absent={_LABEL})
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        fl.attach_at_pick(base, gh, "42", cfg)
    comments = [c for c in run.calls if c[:2] == ["issue", "comment"]]
    assert len(comments) == 1, _flat(run)
    text = comments[0][-1]
    assert fl.MISSING_LABEL_MARKER in text and _LABEL in text


def test_the_two_flag_markers_are_distinct():
    """One shared marker would mean an issue flagged for a missing label and later hand-edited into
    a self-contradiction is silently set aside with NO second flag -- the human is told about one
    problem and never about the one that replaced it."""
    fl = _mod("feature_labels")
    assert fl.MISSING_LABEL_MARKER != fl.AMBIGUOUS_MARKER
    run = _runner(body=_RIVALS, labels={"sdlc:goal"},
                  comments=["earlier\n" + fl.MISSING_LABEL_MARKER + "\n"])
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        fl.attach_at_pick(base, gh, "42", cfg)
    posted = [c[-1] for c in run.calls if c[:2] == ["issue", "comment"]]
    assert len(posted) == 1 and fl.AMBIGUOUS_MARKER in posted[0], posted


def test_the_flag_is_posted_only_once_per_timeline_read():
    """A flag that posted on every look would bury the issue in identical comments."""
    fl = _mod("feature_labels")
    run = _runner(body=_DECLARES, labels={"sdlc:goal"}, absent={_LABEL},
                  comments=["earlier\n" + fl.MISSING_LABEL_MARKER + "\nmore"])
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        decision = fl.attach_at_pick(base, gh, "42", cfg)
    assert decision.proceed is False
    assert [c for c in run.calls if c[:2] == ["issue", "comment"]] == []


def test_a_refusal_is_recorded_in_the_ledger_even_when_the_comment_was_skipped():
    """The ledger line is the ATTRIBUTION `resume_needs_label` reads, so it is keyed off the overlay
    landing -- not off the comment, which is best-effort and may legitimately be skipped as already
    posted. Gating the record on the comment is how the state used to go missing entirely."""
    fl = _mod("feature_labels")
    run = _runner(body=_DECLARES, labels={"sdlc:goal"}, absent={_LABEL},
                  comments=["already\n" + fl.MISSING_LABEL_MARKER + "\n"])
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        fl.attach_at_pick(base, gh, "42", cfg)
        assert _tokens(base, "42") == [fl.BLOCK_TOKEN]


def test_no_ledger_attribution_when_the_overlay_itself_did_not_land():
    """A record saying we set a goal aside, when we did not, would make `resume_needs_label` act on a
    goal whose state it never created."""
    fl = _mod("feature_labels")
    run = _runner(body=_DECLARES, labels={"sdlc:goal"}, absent={_LABEL}, fail_on=["mutation"])
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        decision = fl.attach_at_pick(base, gh, "42", cfg)
        assert _tokens(base, "42") == []
    assert decision.proceed is False and decision.outcome == fl.REFUSED_NO_LABEL


def test_a_flag_whose_timeline_read_fails_posts_nothing_but_still_records():
    """The idempotency probe is a READ. When it cannot be made, a late comment beats a duplicate --
    but the durable trace must NOT depend on it, or a refusal whose timeline read keeps failing
    leaves no record anywhere."""
    fl = _mod("feature_labels")
    # #895: REST fetches comment pages only when the issue's comment count is non-zero, so one
    # unrelated comment makes the timeline read reach the (failing) page; the body read never does.
    run = _runner(body=_DECLARES, labels={"sdlc:goal"}, absent={_LABEL}, comments=("unrelated",),
                  fail_on=["--json comments", "issues/42/comments"])
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        decision = fl.attach_at_pick(base, gh, "42", cfg)
        assert _tokens(base, "42") == [fl.BLOCK_TOKEN]
    assert decision.proceed is False and decision.outcome == fl.REFUSED_NO_LABEL
    assert [c for c in run.calls if c[:2] == ["issue", "comment"]] == []


def test_a_flag_that_cannot_be_posted_still_refuses_the_pick():
    """Flagging is how a human hears about it, not how the decision is made."""
    fl = _mod("feature_labels")
    run = _runner(body=_DECLARES, labels={"sdlc:goal"}, absent={_LABEL},
                  fail_on=["issue comment"])
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        decision = fl.attach_at_pick(base, gh, "42", cfg)     # must not raise
    assert decision.proceed is False and decision.outcome == fl.REFUSED_NO_LABEL


# ------------------------------------------------------------------ ambiguity

def test_an_issue_contradicting_itself_refuses_only_that_issue():
    """`features.read` raises `AmbiguousUnit` when ONE side of an issue contradicts itself, and its
    own docstring puts the obligation on every caller: catch it PER ISSUE."""
    fl = _mod("feature_labels")
    run = _runner(body=_RIVALS, labels={"sdlc:goal"})
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        decision = fl.attach_at_pick(base, gh, "42", cfg)     # must not raise
    assert decision.proceed is False and decision.outcome == fl.REFUSED_AMBIGUOUS
    assert not any(is_feature(c[2]) for c in _label_creates(run)), _label_creates(run)
    assert "sdlc:needs-label" in run.labels and "sdlc:goal" in run.labels


def test_two_rival_feature_LABELS_also_refuse_only_that_issue():
    """The OTHER `AmbiguousUnit` raise site -- `features.parse_labels`, two distinct `feature:`
    labels hand-added to one issue, which `features.py`'s own docstring calls an observed failure
    mode on this repo. Every earlier ambiguity test drove the body side only."""
    fl = _mod("feature_labels")
    run = _runner(body="no marker at all\n",
                  labels={"sdlc:goal", "feature:voice-interview", "feature:billing"})
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        decision = fl.attach_at_pick(base, gh, "42", cfg)     # must not raise
    assert decision.proceed is False and decision.outcome == fl.REFUSED_AMBIGUOUS
    assert "sdlc:needs-label" in run.labels


def test_an_ambiguous_issue_is_flagged_too():
    fl = _mod("feature_labels")
    run = _runner(body=_RIVALS, labels={"sdlc:goal"})
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        fl.attach_at_pick(base, gh, "42", cfg)
    comments = [c for c in run.calls if c[:2] == ["issue", "comment"]]
    assert len(comments) == 1 and fl.AMBIGUOUS_MARKER in comments[0][-1]


def test_an_ambiguous_hold_is_audited_without_a_unit_to_name():
    """There is no single unit to name, and the audit line says so rather than inventing one. It is
    AUDIT ONLY -- nothing reads it back, which is what lets the whole feature work with the ledger
    turned off."""
    fl = _mod("feature_labels")
    run = _runner(body=_RIVALS, labels={"sdlc:goal"})
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        fl.attach_at_pick(base, gh, "42", cfg)
        why = _notes(base, "42")[0]["why"]
    assert why.startswith(fl.BLOCK_TOKEN) and "ambiguous" in why


# ------------------------------------------------------------------ the transient directions

def test_a_label_lookup_that_could_not_answer_refuses_without_flagging_or_overlaying():
    """A transient failure is not evidence the label is absent. Refuse (redo a little work next
    pass) but do NOT flag a human, and do NOT write a state, for a network blip."""
    fl = _mod("feature_labels")
    run = _runner(body=_DECLARES, labels={"sdlc:goal"}, fail_on=["label(name:"])
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        decision = fl.attach_at_pick(base, gh, "42", cfg)
        assert _notes(base, "42") == []
    assert decision.proceed is False and decision.outcome == fl.REFUSED_UNRESOLVED
    assert [c for c in run.calls if c[:2] == ["issue", "comment"]] == []
    assert "sdlc:needs-label" not in run.labels
    assert _label_creates(run) == []


def test_an_attach_that_did_not_land_refuses_without_flagging_or_overlaying():
    """The SIBLING transient direction, and it was the unpinned one. Every downstream step reads the
    LABEL, so claiming a goal whose attach failed would resolve it against the configured base --
    but the label EXISTS here, so summoning a human would be wrong, and the missing-label comment it
    would post would be factually false."""
    fl = _mod("feature_labels")
    run = _runner(body=_DECLARES, labels={"sdlc:goal"})
    gh = _source(run)
    # ONLY the attach fails. An earlier version of this test used `fail_on=["mutation"]`, which
    # fails EVERY label write including the overlay's own — so a version that wrongly routed this
    # path through `_refuse` still wrote no comment and no overlay, and the test passed. Failing the
    # one write under test is what makes the assertions below mean anything.
    gh.attach_label = lambda goal, name: False
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        decision = fl.attach_at_pick(base, gh, "42", cfg)
        assert _notes(base, "42") == []
    assert decision.proceed is False and decision.outcome == fl.REFUSED_WRITE_FAILED
    assert _LABEL not in run.labels
    assert [c for c in run.calls if c[:2] == ["issue", "comment"]] == []
    assert "sdlc:needs-label" not in run.labels


def test_an_unreadable_issue_fails_open():
    """A body we could not fetch is not a body that declares nothing -- but the safe direction is
    still today's behaviour (no unit, configured base), never a queue that stops."""
    fl = _mod("feature_labels")
    run = _runner(body=_DECLARES, fail_on=["--json body", "issues/42 --method GET"])
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        decision = fl.attach_at_pick(base, gh, "42", cfg)
    assert decision.proceed is True and decision.outcome == fl.UNREADABLE


def test_a_broken_stderr_cannot_break_a_pick(monkeypatch):
    """Every diagnostic on this path is a courtesy, and a closed or unwritable stderr (a detached
    daemon, a closed pipe) must cost the caller nothing."""
    fl = _mod("feature_labels")

    class Exploding:
        def write(self, _):
            raise ValueError("stderr is gone")

    monkeypatch.setattr(fl.sys, "stderr", Exploding())
    run = _runner(body=_DECLARES, fail_on=["--json body", "issues/42 --method GET"])
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        decision = fl.attach_at_pick(base, gh, "42", cfg)
    assert decision.proceed is True and decision.outcome == fl.UNREADABLE


# ------------------------------------------------------------------ the source-capability gate

def test_a_source_with_no_label_surface_is_a_no_op():
    """`LocalSource` has no repository label namespace at all -- goals are files."""
    fl, sources = _mod("feature_labels"), _mod("sources")
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        local = sources.LocalSource(base, {})
        decision = fl.attach_at_pick(base, local, "0001.md", cfg)
    assert decision.proceed is True and decision.outcome == fl.NOTHING_TO_DO


def test_a_partial_source_surface_is_a_no_op_not_an_attribute_error():
    """`all(...)`, not `any(...)` or a single-method check. The docstring's promise -- "degrades to
    NOTHING_TO_DO, never to an AttributeError inside the pick path" -- holds for `LocalSource` only
    because it has NONE of the methods. A PARTIAL surface is what the next level produces the moment
    it adds one, and a single-method check would raise inside `_next()`, uncaught."""
    fl = _mod("feature_labels")

    class Partial:
        def fetch_body_labels(self, goal):
            return {"body": _DECLARES, "labels": [{"name": "sdlc:goal"}]}

    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        decision = fl.attach_at_pick(base, Partial(), "42", cfg)      # must not raise
        assert fl.resume_needs_label(base, Partial(), cfg) == []          # same gate, same promise
    assert decision.proceed is True and decision.outcome == fl.NOTHING_TO_DO


def test_the_required_surface_names_every_method_the_module_calls():
    """Both directions, because each catches a different drift.

    FORWARD: every declared name exists on the real source, so the tuple cannot name a method that
    was renamed away. REVERSE, and this is the one that matters: a fake carrying EXACTLY the
    declared surface and nothing else must satisfy BOTH entry points. If the tuple ever stops naming
    a method the module actually calls, that fake raises `AttributeError` inside the gate — which is
    precisely the failure the capability check exists to prevent, arriving through the check's own
    declaration instead. A single-method fake cannot see this: it fails the gate either way."""
    fl, sources = _mod("feature_labels"), _mod("sources")
    for name in fl.REQUIRED_SOURCE_METHODS:
        assert callable(getattr(sources.GitHubSource, name, None)), name

    calls = []

    class ExactlyTheDeclaredSurface:
        def fetch_body_labels(self, goal):
            return {"body": _DECLARES, "labels": [{"name": "sdlc:goal"}]}
        def label_exists(self, name):
            return False
        def attach_label(self, goal, name):
            return True
        def mark_needs_label(self, goal):
            calls.append("mark"); return True
        def clear_needs_label(self, goal):
            calls.append("clear"); return True
        def list_needs_label(self):
            calls.append("list")
            return [{"number": 42, "body": "nothing declared\n", "labels": []}]

    fake = ExactlyTheDeclaredSurface()
    for name in list(vars(ExactlyTheDeclaredSurface)):
        if not name.startswith("_"):
            assert name in fl.REQUIRED_SOURCE_METHODS, "%s is not declared" % name
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        assert fl.attach_at_pick(base, fake, "42", cfg).proceed is False   # must not raise
        assert fl.resume_needs_label(base, fake, cfg) == ["42"]            # must not raise
    assert calls == ["mark", "list", "clear"], calls


# ------------------------------------------------------------------ resume: the self-healing half

def _held(d, ledger_on=True):
    """A `.sdlc` plus a source whose issue #42 has just been held for a missing label."""
    fl = _mod("feature_labels")
    base, cfg = _sdlc(d, ledger_on=ledger_on)
    run = _runner(body=_DECLARES, labels={"sdlc:goal"}, absent={_LABEL})
    gh = _source(run)
    assert fl.attach_at_pick(base, gh, "42", cfg).outcome == fl.REFUSED_NO_LABEL
    return base, cfg, run, gh


def test_resume_clears_the_overlay_once_the_label_exists():
    """The human's ONE gesture. Creating the label is enough -- nothing is un-parked, and no second
    gesture is owed."""
    fl = _mod("feature_labels")
    with tempfile.TemporaryDirectory() as d:
        base, cfg, run, gh = _held(d)
        assert "sdlc:needs-label" in run.labels
        gqlfake._UNKNOWN_LABELS.discard(_LABEL)          # a human creates the label
        gh._label_ids = None                             # a later pick is a fresh process
        released = fl.resume_needs_label(base, gh, cfg)
    assert released == ["42"]
    assert "sdlc:needs-label" not in run.labels and "sdlc:goal" in run.labels


def test_resume_works_with_the_LEDGER_OFF():
    """THE reason the label is the attribution. `ledger.enabled` ships FALSE in `/sigma-init`'s own
    template, and an attribution kept in the ledger meant the overlay landed, nothing was recorded,
    and the goal was stuck permanently behind a comment promising it would resume by itself."""
    fl = _mod("feature_labels")
    with tempfile.TemporaryDirectory() as d:
        base, cfg, run, gh = _held(d, ledger_on=False)
        assert _notes(base) == []                        # nothing recorded anywhere -- by design
        assert "sdlc:needs-label" in run.labels
        gqlfake._UNKNOWN_LABELS.discard(_LABEL)
        gh._label_ids = None
        assert fl.resume_needs_label(base, gh, cfg) == ["42"]
    assert "sdlc:needs-label" not in run.labels


def test_resume_reads_only_its_own_label():
    """`sdlc:blocked` is a SHARED state with its own resumer. The sweep must never see a goal it
    does not hold, and it does not have to be told which those are: it asks for its own label, and
    nothing else writes that label."""
    fl = _mod("feature_labels")
    run = _runner(body=_DECLARES, labels={"sdlc:goal", "sdlc:blocked"})   # a REAL dependency block
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        assert fl.resume_needs_label(base, gh, cfg) == []
    assert [c for c in run.calls if not _is_issues_list_call(c)] == []     # one query, no writes
    assert "sdlc:blocked" in run.labels                                   # left exactly as it was


def test_the_sweep_costs_one_query_and_no_issue_reads():
    """One `gh issue list` per sweep carries every held issue's body and labels, so releasing costs
    no per-issue read, and a still-held goal costs no REST read at all -- one live GraphQL label
    lookup, because `_label_node_ids` caches names it FOUND and never names it did not, which is
    exactly what lets a label created a moment ago be seen."""
    fl = _mod("feature_labels")
    with tempfile.TemporaryDirectory() as d:
        base, cfg, run, gh = _held(d)
        gh._label_ids = None
        mark = len(run.calls)
        assert fl.resume_needs_label(base, gh, cfg) == []       # still held
        after = run.calls[mark:]
    assert [c for c in after if _is_issues_list_call(c)] != []
    assert [c for c in after if gqlfake.is_issue_read(c)] == []


def test_a_released_goal_is_not_examined_again():
    """The label is gone, so the query stops returning it. Nothing else has to remember anything."""
    fl = _mod("feature_labels")
    with tempfile.TemporaryDirectory() as d:
        base, cfg, run, gh = _held(d)
        gqlfake._UNKNOWN_LABELS.discard(_LABEL)
        gh._label_ids = None
        assert fl.resume_needs_label(base, gh, cfg) == ["42"]
        mark = len(run.calls)
        assert fl.resume_needs_label(base, gh, cfg) == []
        after = run.calls[mark:]
    assert all(_is_issues_list_call(c) for c in after), after


def test_a_lookup_that_could_not_answer_does_NOT_release():
    """The three-valued lookup, obeyed on the side that decides whether to UNDO a state. A
    transient failure is not evidence the label exists, and treating it as one would clear the
    overlay and post an audit comment claiming "the label now exists" when it may not."""
    fl = _mod("feature_labels")
    with tempfile.TemporaryDirectory() as d:
        base, cfg, run, gh = _held(d)
        gh._label_ids = None
        gh.label_exists = lambda name: None                     # could not tell
        assert fl.resume_needs_label(base, gh, cfg) == []
        assert [c for c in run.calls if c[:2] == ["issue", "comment"]][1:] == []
    assert "sdlc:needs-label" in run.labels


def test_resume_records_and_comments():
    """A label change an unattended run makes is never silent -- this kit's standing rule."""
    fl = _mod("feature_labels")
    with tempfile.TemporaryDirectory() as d:
        base, cfg, run, gh = _held(d)
        gqlfake._UNKNOWN_LABELS.discard(_LABEL)
        gh._label_ids = None
        fl.resume_needs_label(base, gh, cfg)
        assert _tokens(base, "42") == [fl.BLOCK_TOKEN, fl.RESUME_TOKEN]
    posted = [c[-1] for c in run.calls if c[:2] == ["issue", "comment"]]
    assert len(posted) == 2 and "Auto-resumed" in posted[1]


def test_a_release_whose_write_did_not_land_is_not_recorded_as_released():
    fl = _mod("feature_labels")
    with tempfile.TemporaryDirectory() as d:
        base, cfg, run, gh = _held(d)
        gqlfake._UNKNOWN_LABELS.discard(_LABEL)
        gh._label_ids = None
        gh._swap_labels_best_effort = lambda *a, **k: False
        assert fl.resume_needs_label(base, gh, cfg) == []
        assert _tokens(base, "42") == [fl.BLOCK_TOKEN]


def test_an_ambiguous_hold_releases_once_the_body_is_fixed():
    """The ambiguous hold has no label to look up -- its release is decided from the body the sweep
    query already carried, still with no per-issue read."""
    fl = _mod("feature_labels")
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        run = _runner(body=_RIVALS, labels={"sdlc:goal"})
        gh = _source(run)
        assert fl.attach_at_pick(base, gh, "42", cfg).outcome == fl.REFUSED_AMBIGUOUS
        assert fl.resume_needs_label(base, gh, cfg) == []              # still contradictory
        fixed = _runner(body="Feature: voice-interview\n", labels=set(run.labels))
        assert fl.resume_needs_label(base, _source(fixed), cfg) == ["42"]
        assert "sdlc:needs-label" not in fixed.labels


# ------------------------------------------------------------------ wired into the pick

def test_the_attach_lands_before_the_claim():
    """"This must run first, before any other work on the goal." Asserted on the ORDER of the real
    gh call log, not on a mock's invocation order."""
    lp = _mod("loop")
    run = _queue_runner({42: {"body": _DECLARES}})
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        kind, goal = lp._next(base, gh, cfg)
    assert (kind, goal) == ("goal", "42")
    flat = _flat(run)
    attached = next(i for i, c in enumerate(flat) if "--add-label %s" % _LABEL in c)
    claimed = next(i for i, c in enumerate(flat) if "--add-label sdlc:in-progress" in c)
    assert attached < claimed, flat


def test_a_refused_goal_is_never_claimed_and_the_loop_moves_on():
    """Refusing means exactly that: no `sdlc:in-progress`, no ledger claim, and the next eligible
    goal is picked instead -- one flagged issue must not stall the queue."""
    lp, ledger = _mod("loop"), _mod("ledger")
    run = _queue_runner({42: {"body": _DECLARES}, 43: {"body": "ordinary\n"}}, absent={_LABEL})
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        kind, goal = lp._next(base, gh, cfg)
        claims = [e for e in ledger.read_all(base) if e["kind"] == "claimed"]
    assert (kind, goal) == ("goal", "43")
    assert [e["goal"] for e in claims] == ["43"]
    assert "sdlc:in-progress" not in run.live["42"]
    assert not any(is_feature(c[2]) for c in _label_creates(run))


def test_a_refused_goal_releases_its_claim_lock():
    """The refusal path is the one branch that leaves the pick loop without reaching the
    `try/finally` that normally releases the lock. A refused goal that LEAKED it would be unpickable
    by anyone until something cleared it -- and within one process `next_batch` visits this branch
    once per slot."""
    lp = _mod("loop")
    run = _queue_runner({42: {"body": _DECLARES}, 43: {"body": "ordinary\n"}}, absent={_LABEL})
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        lp._next(base, gh, cfg)
        fd = lp._try_acquire_claim_lock(base, "42")
        assert fd is not None, "the refusal path leaked #42's claim lock"
        lp._release_claim_lock(fd)


def test_a_refused_goal_is_not_re_read_on_the_next_pick():
    """F2's measured cost. `next_batch` folds only PICKED goals into `skip`, so before the overlay a
    poisoned issue was re-evaluated from scratch on every slot -- two `gh issue view` calls each,
    unbounded in time. With the overlay it leaves the pickable queue, so the second pick reads it
    zero times."""
    lp = _mod("loop")
    run = _queue_runner({42: {"body": _DECLARES}, 43: {"body": "a\n"}, 44: {"body": "b\n"}},
                        absent={_LABEL})
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        lp._next(base, gh, cfg)
        first = len(_views(run, 42))
        mark = len(run.calls)
        lp._next(base, gh, cfg, extra_skip={"43"})
        second = [c for c in run.calls[mark:] if gqlfake.is_issue_read(c) and _read_number(c) == "42"]
    assert first >= 1                       # it WAS examined, once, on the pick that set it aside
    assert second == [], second             # and never again


def test_a_budget_halted_pick_mutates_nothing():
    """F4. The budget gate exists to stop the run; nothing may mutate before it. This used to run
    after the attach, so a call returning ('BUDGET', ...) had already sent an `issue edit
    --add-label` and written a ledger note."""
    lp = _mod("loop")
    run = _queue_runner({42: {"body": _DECLARES}})
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d, max_iterations=1, run_iteration=5)
        kind, reason = lp._next(base, gh, cfg)
        assert _notes(base) == []
    assert kind == "BUDGET"
    writes = [c for c in _flat(run) if "--add-label" in c or c.startswith("issue comment")]
    assert writes == [], writes


def test_a_budget_halted_pick_releases_its_claim_lock():
    lp = _mod("loop")
    run = _queue_runner({42: {"body": _DECLARES}})
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d, max_iterations=1, run_iteration=5)
        assert lp._next(base, gh, cfg)[0] == "BUDGET"
        fd = lp._try_acquire_claim_lock(base, "42")
        assert fd is not None, "the budget path leaked #42's claim lock"
        lp._release_claim_lock(fd)


def test_the_unblock_sweep_runs_before_the_pick_and_frees_the_goal_in_the_same_call():
    """No cooldown, deliberately: the overlay is the loop's OWN state, not a human's park, so
    nothing is being overridden and there is no decision for a person to catch up on."""
    lp, fl = _mod("loop"), _mod("feature_labels")
    run = _queue_runner({42: {"body": _DECLARES}}, absent={_LABEL})
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        assert lp._next(base, gh, cfg) == ("DONE", None)      # #42 set aside, nothing else queued
        assert "sdlc:needs-label" in run.live["42"]
        gqlfake._UNKNOWN_LABELS.discard(_LABEL)               # a human creates the label
        gh._label_ids = None
        kind, goal = lp._next(base, gh, cfg)
    assert (kind, goal) == ("goal", "42")
    assert "sdlc:needs-label" not in run.live["42"] and _LABEL in run.live["42"]


# ------------------------------------------------------------------ the fail-open promises

def test_an_overlay_that_does_not_land_falls_back_to_the_original_behaviour():
    """If the label write fails there is no half-state to clean up, because the label is the only
    state there is: the pick is still refused, the goal stays in the queue, and it is refused again
    next pass -- the silence behaviour, which was never unsafe, only noisy."""
    fl = _mod("feature_labels")
    run = _runner(body=_DECLARES, labels={"sdlc:goal"}, absent={_LABEL}, fail_on=["mutation"])
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        decision = fl.attach_at_pick(base, gh, "42", cfg)
        assert _notes(base, "42") == []
    assert decision.proceed is False and decision.outcome == fl.REFUSED_NO_LABEL
    assert "sdlc:needs-label" not in run.labels
    assert [c for c in run.calls if c[:2] == ["issue", "comment"]] == []


@pytest.mark.parametrize("returned", [None, 0, False, "", 1, "ok", ["42"], object()])
def test_an_overlay_that_returns_a_non_bool_is_treated_as_not_landed(returned):
    """`is True`, not `bool(...)` — and the parametrisation is the whole point of this test.

    An earlier version injected `None` alone. `None` is the one non-bool on which `is True` and
    `bool(...)` AGREE, so the test written to pin the distinction could not observe it: swapping the
    check to `bool(...)` left it green. That is the same probe-blindness as choosing a fixture that
    makes the guard under test unreachable — here landing on the very case the test exists for.

    The TRUTHY non-bools are the ones that decide it. Under `bool(...)` a source returning `1`, a
    string, or a list reads as "the overlay landed" when no label was written — so a human is
    flagged and a ledger line recorded for a state that does not exist, and the goal silently leaves
    the queue with nothing holding it there. Every value below must reach the same place: refuse,
    write nothing, leave the goal pickable so the next pass retries."""
    fl = _mod("feature_labels")
    run = _runner(body=_DECLARES, labels={"sdlc:goal"}, absent={_LABEL})
    gh = _source(run)
    gh.mark_needs_label = lambda goal: returned
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        decision = fl.attach_at_pick(base, gh, "42", cfg)
        assert _notes(base, "42") == [], returned
    assert decision.proceed is False
    assert [c for c in run.calls if c[:2] == ["issue", "comment"]] == [], returned
    assert "sdlc:needs-label" not in run.labels, returned


def test_the_sweep_asks_only_for_OPEN_issues():
    """A closed issue that still carries the overlay must not be touched.

    `state=open` was in the query from the start and nothing pinned it. Without it the sweep
    reaches a CLOSED held issue and "releases" it: a label write, an `Auto-resumed` comment, and a
    board move back to `Ready` — on an issue nobody can work, resurrecting a card that was
    deliberately done. It is also the rule `reconcile` relies on to own closed-issue label repair;
    two sweeps writing to the same closed issue is the class of collision this label exists to avoid."""
    run = _runner(body=_DECLARES, labels={"sdlc:goal", "sdlc:needs-label"})
    gh = _source(run)
    gh.list_needs_label()
    lists = [c for c in run.calls if _is_issues_list_call(c)]
    assert len(lists) == 1, run.calls
    args = lists[0]
    assert "state=open" in args, args
    # #1829: the cap is still the shared board ceiling (_BOARD_ITEM_LIMIT), not a local number --
    # this query is the sole resumer, so a goal past the cap is one nothing will ever release.
    # REST's own per-page ceiling (100) sits below that cap, so the first page asks for
    # min(100, _BOARD_ITEM_LIMIT) = 100 -- if the cap had shrunk below REST's page size, this
    # would show a smaller number here.
    assert "per_page=100" in args, args
    assert gh._BOARD_ITEM_LIMIT >= 100, "the cap must not be smaller than REST's own page size"


def test_resume_needs_label_forces_GET_on_its_rest_call():
    """#1829: `gh api` defaults to POST the instant any `-f` field is supplied (verified live
    against a real, safe GET-only endpoint — see .sdlc/research/1829-rest-backlog-pick.md).
    `list_needs_label`'s REST call must force GET explicitly, or a transient omission here would
    attempt to CREATE an issue against the real repo."""
    fl = _mod("feature_labels")
    with tempfile.TemporaryDirectory() as d:
        base, cfg, run, gh = _held(d)
        gh._label_ids = None
        fl.resume_needs_label(base, gh, cfg)
    api_calls = [c for c in run.calls if c and c[0] == "api" and str(c[1]).endswith("/issues")]
    assert api_calls, run.calls
    assert all("--method" in c and c[c.index("--method") + 1] == "GET" for c in api_calls)


def test_an_unreadable_needs_label_query_is_an_empty_sweep():
    """The sweep's one query is also its only source of truth. A transport failure costs one
    deferred release, never a raise into the pick."""
    fl = _mod("feature_labels")
    run = _runner(fail_on=["repos/o/r/issues"])          # #1829: the REST issues-list endpoint
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        assert gh.list_needs_label() == []              # must not raise
        assert fl.resume_needs_label(base, gh, cfg) == []


def test_an_issue_payload_with_no_number_is_skipped():
    fl = _mod("feature_labels")
    run = _runner()
    gh = _source(run)
    gh.list_needs_label = lambda: [{"body": "", "labels": []}, {"number": None}]
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        assert fl.resume_needs_label(base, gh, cfg) == []


def test_one_unrecheckable_goal_does_not_stop_the_sweep():
    """A sweep over many goals must degrade per goal, the same rule `features.read` puts on every
    caller: one bad issue costs itself, never the rest."""
    fl = _mod("feature_labels")
    with tempfile.TemporaryDirectory() as d:
        base, cfg, run, gh = _held(d)
        gqlfake._UNKNOWN_LABELS.discard(_LABEL)
        gh._label_ids = None
        gh.label_exists = lambda name: (_ for _ in ()).throw(RuntimeError("lookup exploded"))
        assert fl.resume_needs_label(base, gh, cfg) == []       # must not raise
    assert "sdlc:needs-label" in run.labels


def test_a_release_whose_audit_comment_fails_still_releases():
    """The comment is the audit trail, not the gate. A `gh` that will not take it must not undo a
    label change that already landed."""
    fl = _mod("feature_labels")
    with tempfile.TemporaryDirectory() as d:
        base, cfg, run, gh = _held(d)
        gqlfake._UNKNOWN_LABELS.discard(_LABEL)
        gh._label_ids = None
        gh.note = lambda goal, text: (_ for _ in ()).throw(RuntimeError("comment refused"))
        assert fl.resume_needs_label(base, gh, cfg) == ["42"]   # must not raise
    assert "sdlc:needs-label" not in run.labels


def test_the_needs_label_sweep_is_fail_open(capsys):
    lp = _mod("loop")

    class Exploding:
        def resume_needs_label(self, *a, **k):
            raise RuntimeError("boom")

    real = lp.feature_labels
    try:
        lp.feature_labels = Exploding()
        with tempfile.TemporaryDirectory() as d:
            base, cfg = _sdlc(d)
            assert lp._feature_needs_label_sweep(base, object(), cfg) == []
    finally:
        lp.feature_labels = real
    assert "needs-label sweep failed non-fatally" in capsys.readouterr().err


def test_a_hold_releases_when_the_human_removes_the_marker_instead():
    """Creating the label is one remedy; deleting the typo'd `Feature:` line is the other, and the
    issue said so ("a typo — correct the `Feature:` line"). Both have to actually release it."""
    fl = _mod("feature_labels")
    with tempfile.TemporaryDirectory() as d:
        base, cfg, run, gh = _held(d)
        corrected = _runner(body="no marker any more\n", labels=set(run.labels), absent={_LABEL})
        assert fl.resume_needs_label(base, _source(corrected), cfg) == ["42"]
        assert "sdlc:needs-label" not in corrected.labels


# ---------------------------------------------------- the sweep collision that chose this label

def _flap_runner(body, closed_refs=("7",)):
    """A fake covering BOTH modules' call shapes at once: the needs-label sweep, the pick, and
    `auto_unpark`'s parked/blocked queries, comment fetches and ref-state probes."""
    calls = []
    live = {"42": {"sdlc:goal"}}
    comments = {"42": []}
    gqlfake._UNKNOWN_LABELS.add(_LABEL)

    def run(args):
        target = live.get(gqlfake._GQL_LAST_ISSUE["n"]) if args[:2] == ["api", "graphql"] else None
        gql = gqlfake.swap(args, labels=target, calls=calls, repo_args=("--repo", "o/r"))
        if gql is not None:
            return gql
        calls.append(list(args))
        if _is_issues_list_call(args):
            wanted = _issues_list_labels(args)
            rows = []
            for n, s in live.items():
                if wanted and not all(w in s for w in wanted):
                    continue
                # REST has no field-selection concept — body always comes back on the real
                # endpoint (verified live), unlike the old `--json`-gated shape this replaces.
                rows.append({"number": int(n), "title": "g",
                            "labels": [{"name": x} for x in sorted(s)], "body": body})
            return json.dumps(rows)

        def view(n, fields):
            n = str(n)
            out = {}
            if "title" in fields:
                out["title"] = "g"
            if "body" in fields:
                out["body"] = body if n == "42" else ""
            if "labels" in fields:
                out["labels"] = [{"name": x} for x in sorted(live.get(n, ()))]
            if "comments" in fields:
                out["comments"] = [{"body": c} for c in comments.get(n, [])]
            if "state" in fields:
                out["state"] = "CLOSED" if n in closed_refs else "OPEN"
            if "stateReason" in fields:
                out["stateReason"] = "COMPLETED" if n in closed_refs else ""
            return out

        rest = gqlfake.rest_issue(args, view)
        if rest is not None:
            return rest
        if args[:2] == ["issue", "view"]:
            return json.dumps(view(args[2], args[args.index("--json") + 1] if "--json" in args else ""))
        if args[:2] == ["issue", "comment"]:
            comments.setdefault(str(args[2]), []).append(args[-1])
        return ""

    run.calls = calls
    run.live = live
    run.comments = comments
    return run


def test_a_held_goal_naming_an_already_closed_dependency_does_not_flap():
    """THE measurement that chose a label of our own over `sdlc:blocked`.

    `auto_unpark.compute_unpark_actions` strips `sdlc:blocked` from any goal whose body names a
    `blocked by #N` whose refs have all closed -- whoever set it, and whyever. Under the earlier
    design that goal was un-blocked by that sweep and re-blocked by this one every cycle: two label
    swaps, two board moves and one FALSE "blocker closed" comment each time. `compile_plan`,
    `handoff` and `triage` all write that marker into goal bodies themselves, and six open issues on
    this repo's own board -- this one included -- carry a closed one today.

    Two modules, driven together, three full cycles."""
    lp, au = _mod("loop"), _mod("auto_unpark")
    body = _DECLARES + "\n**Blocked by:** #7\n"
    run = _flap_runner(body)
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        cfg["discovery"]["auto_unpark"] = {"mode": "on"}
        seen = []
        for _ in range(3):
            lp._next(base, gh, cfg)
            au.sweep_unpark(base, cfg, apply=True, run=run)
            seen.append(sorted(run.live["42"]))
    assert seen == [["sdlc:goal", "sdlc:needs-label"]] * 3, seen
    assert "sdlc:blocked" not in run.live["42"]
    false_claims = [c for c in run.comments["42"] if "Auto-unparked" in c]
    assert false_claims == [], false_claims
    assert len([c for c in run.comments["42"] if fl_marker() in c]) == 1


def fl_marker():
    return _mod("feature_labels").MISSING_LABEL_MARKER


def test_the_needs_label_sweep_does_not_run_on_a_budget_halted_call():
    """"Nothing mutates before the budget gate" has to be true of this commit's own code. The sweep
    WRITES -- removes a label, posts a comment -- so a call that reports it did nothing must not
    have run it. The attach step was moved below the gate for exactly this reason."""
    lp, fl = _mod("loop"), _mod("feature_labels")
    with tempfile.TemporaryDirectory() as d:
        base, cfg, run, gh = _held(d)
        gqlfake._UNKNOWN_LABELS.discard(_LABEL)
        gh._label_ids = None
        cfg["budget"] = {"max_iterations": 1}
        pathlib.Path(base, "state", "STATE.md").write_text(
            "iteration: 5\nrun_iteration: 5\nlast_run: none\n")
        mark = len(run.calls)
        lp._next(base, gh, cfg)
        after = [" ".join(str(a) for a in c) for c in run.calls[mark:]]
    # the sweep was never even asked its question, so it cannot have written
    assert [c for c in after if "--label sdlc:needs-label" in c] == [], after
    assert [c for c in after if "--remove-label" in c or c.startswith("issue comment")] == []
    assert "sdlc:needs-label" in run.labels
    # ...and with the budget restored, the very same state DOES release -- so the assertion above
    # is about the gate, not about a sweep that could never have fired anyway.
    with tempfile.TemporaryDirectory() as d2:
        base2, cfg2, run2, gh2 = _held(d2)
        gqlfake._UNKNOWN_LABELS.discard(_LABEL)
        gh2._label_ids = None
        assert fl.resume_needs_label(base2, gh2, cfg2) == ["42"]


# ---------------------------------------------------- every consumer of the eligibility rule

def test_the_overlay_set_is_what_carries_the_eligibility_consumers():
    """Three consumers read the rule rather than restating it, so ONE edit to `overlay_labels`
    carries all of them. Asserted through the real objects, not by reading the source."""
    src = _mod("sources")
    gh = src.GitHubSource(_CONFIG, run=_runner())
    assert "sdlc:needs-label" in gh.overlay_labels()
    assert "sdlc:needs-label" in gh.not_eligible_labels()
    assert gh._card_is_eligible(42, {"sdlc:goal", "sdlc:needs-label"}) is False
    assert gh._card_is_eligible(43, {"sdlc:goal"}) is True


def test_triage_does_not_report_a_held_goal_as_ready_and_names_its_reason():
    """`_bucket_enqueued` reads the rule; `_bucket_parked` does not, and its reason recovery is
    ledger-only -- so a held goal would render with `reason: ''` on a stock adopter. The label
    carries the meaning, so the reason comes from the label."""
    triage = _mod("triage")
    gh_cfg = _CONFIG["discovery"]["github"]
    held = {"number": 42, "title": "held", "state": "OPEN",
            "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:needs-label"}]}
    healthy = {"number": 43, "title": "fine", "state": "OPEN", "labels": [{"name": "sdlc:goal"}]}
    ready = triage._bucket_enqueued([held, healthy], gh_cfg, gh_source=_source(_runner()))
    assert [i["number"] for i in ready["items"]] == [43], ready
    with tempfile.TemporaryDirectory() as d:
        base, _ = _sdlc(d)
        parked = triage._bucket_parked(base, [held, healthy], [], gh_cfg, gh_source=None)
    item = next(i for i in parked["items"] if i["number"] == 42)
    assert item["state"] == "needs-label"
    assert "feature:" in item["reason"] and item["reason_source"] == "label"


def test_status_counts_a_held_goal_as_needs_label_not_as_pending():
    """`_github_counts` restates the rule by hand, so it needs the name explicitly -- and it now
    reads BOTH overlay names from config instead of hardcoding `"sdlc:blocked"`.

    #1833: `_github_counts` migrated its `n(*labels)` fetch off `gh issue list --label ...` onto
    the shared REST helper (`sources.fetch_issues_rest`) -- the fake `run` below now parses the
    `-f labels=a,b` field that call constructs instead of repeated `--label` flags."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "status", S.parent.parent.parent / "skills" / "sigma-status" / "scripts" / "status.py")
    status = importlib.util.module_from_spec(spec); spec.loader.exec_module(status)
    counts = {("sdlc:goal",): 3, ("sdlc:goal", "sdlc:in-progress"): 1,
              ("sdlc:goal", "sdlc:needs-label"): 1}

    def run(args):
        labels = next((v[len("labels="):].split(",") for v in args
                       if isinstance(v, str) and v.startswith("labels=")), [])
        return json.dumps([{"number": n} for n in range(counts.get(tuple(labels), 0))])

    out = status._github_counts({"repo": "o/r"}, run)
    assert out["needs_label"] == 1
    assert out["pending"] == 1                      # 3 goals - 1 in-progress - 1 held


def test_reconcile_treats_it_as_an_overlay_not_a_primary_state():
    rec = _mod("reconcile")
    gh = _mod("sources").GitHubSource(_CONFIG, run=_runner())
    assert "sdlc:needs-label" in rec._overlay_labels(gh)
    assert "sdlc:needs-label" not in rec._primary_labels(gh, _CONFIG)


def test_promote_names_the_right_remedy_and_not_unpark():
    """`/sigma-unpark` is not this state's remedy: the goal is already approved and already carries
    `sdlc:goal`. Sending an operator to a command that does not apply is worse than saying nothing."""
    promote = _mod("promote")
    gh = _mod("sources").GitHubSource(_CONFIG, run=_runner())
    msg = promote._refusal(gh, "42", {"sdlc:goal", "sdlc:needs-label"}, False, False)
    assert msg and "sdlc:needs-label" in msg and "unpark" not in msg.lower()
    assert "Create that label" in msg


def test_the_board_card_never_claims_a_transition_the_labels_did_not_make():
    """Both directions gate the card move on the label write actually landing.

    `mark_blocked` moves the card unconditionally — harmless while nobody read its result, but these
    two return a verdict the caller acts on, so a card parked in `Blocked` after a failed write
    would have the board asserting a state the labels never reached. Decisive under
    `queue_source: "status"`, where the board is what an operator reads."""
    moves = []
    run = _runner(body=_DECLARES, labels={"sdlc:goal"})
    gh = _source(run)
    gh._set_board_status = lambda goal, status: moves.append(status)
    gh._swap_labels_best_effort = lambda *a, **k: False        # neither write lands
    assert gh.mark_needs_label("42") is False
    assert gh.clear_needs_label("42") is False
    assert moves == [], moves

    landed = []
    gh2 = _source(_runner(body=_DECLARES, labels={"sdlc:goal"}))
    gh2._set_board_status = lambda goal, status: landed.append(status)
    gh2._swap_labels_best_effort = lambda *a, **k: True         # both land
    gh2.mark_needs_label("42")
    gh2.clear_needs_label("42")
    assert landed == ["Blocked", "Ready"], landed


def test_the_survey_fallback_tuple_also_refuses_a_held_goal():
    """The TWELFTH consumer, and the one adding a label to `not_eligible_labels()` does not reach.

    `_bucket_enqueued` reads the rule only when `gh_source` is not None; its `else` branch restates
    it by hand, and `survey()` reaches that branch whenever `_gh_source` returns None. Production
    passing `gh_source` is exactly what makes the drift invisible — so both paths are asserted here,
    on the same issue. This bucket feeds `_survey_index`, so the failure is a compiled drain plan
    scheduling a goal the picker will refuse on every pass."""
    triage = _mod("triage")
    gh_cfg = _CONFIG["discovery"]["github"]
    held = {"number": 42, "title": "held", "state": "OPEN",
            "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:needs-label"}]}
    with_source = triage._bucket_enqueued([held], gh_cfg, gh_source=_source(_runner()))
    fallback = triage._bucket_enqueued([held], gh_cfg, gh_source=None)
    assert with_source["count"] == 0, with_source
    assert fallback["count"] == 0, fallback           # the branch that had drifted


def test_a_goal_carrying_BOTH_overlays_is_told_about_the_park_not_the_label():
    """Ordering, and it is a correctness question rather than a cosmetic one.

    The needs-label message promises that creating the label is enough. For a goal that is ALSO
    parked or blocked that promise is false — the human still has to act on the park. The stronger
    claim has to win, so the needs-label branch is reached only when it is the sole thing holding
    the goal, which is when it is true."""
    promote = _mod("promote")
    gh = _mod("sources").GitHubSource(_CONFIG, run=_runner())
    both = promote._refusal(gh, "42", {"sdlc:goal", "sdlc:needs-label", "sdlc:parked"}, False, False)
    assert "unpark" in both.lower() and "Create that label" not in both, both
    blocked_too = promote._refusal(
        gh, "42", {"sdlc:goal", "sdlc:needs-label", "sdlc:blocked"}, False, False)
    assert "unpark" in blocked_too.lower(), blocked_too
    alone = promote._refusal(gh, "42", {"sdlc:goal", "sdlc:needs-label"}, False, False)
    assert "Create that label" in alone and "unpark" not in alone.lower(), alone


# --------------- #1567: one transport must not disarm a gate whose decision is local -------------
#
# `attach_at_pick` reads the declaration over GRAPHQL (`gh issue view --json body,labels`). Three
# lines later `work._declared_unit` reads THE SAME DECLARATION over REST and bases the goal on
# `feature/<unit>`. A GraphQL blip made the first answer `None` and left the second untouched, so
# the goal was cut from the unit's branch while the gate governing work against that unit had been
# told there is no unit -- a gate whose entire decision is LOCAL (the registry on disk plus the repo
# slug) switched off by a remote failure, purely because its input travelled the failing transport.

_OTHER = "o/other"


def _adopt(base, unit=None, entry=None):
    """`.sdlc/features/` -- the branching model turned on for this repo, optionally with one unit
    already recorded. Written through `feature_registry.write_unit`, so this suite pins the file
    shape the gate actually reads rather than a hand-rolled one."""
    features = pathlib.Path(base) / "features"
    features.mkdir(parents=True, exist_ok=True)
    if unit:
        _mod("feature_registry").write_unit(features, unit, entry)
    return features


def _unlisted_entry(*repos):
    return {"title": "voice interview", "owner": "@unit-owner", "open": True,
            "repos": {r: {"branch": "feature/voice-interview", "goals": [7]} for r in repos}}


def _rest(lp, run, payload=None, ok=True):
    """Pin the REST reader (`work._declared_unit`, reached through `cross_repo`) and record every
    call it makes ONTO THE SHARED gh CALL LOG, so the ORDER of a REST read against the rest of the
    pick is assertable and not just its count."""
    cross = lp._cross_repo()

    def fake(args, timeout=None):
        run.calls.append(list(args))
        if not ok:
            return (1, "", "gh: the REST read failed too")
        return (0, json.dumps(payload if payload is not None else {}), "")

    cross._run_gh = fake
    return run.calls


def _blip(run, needle, times=1):
    """Wrap a fake `gh` so exactly `times` calls matching `needle` raise -- ONE un-retried
    `gh issue view`, which is the reachable trigger. Everything else passes through untouched."""
    left = [times]

    needles = (needle,) if isinstance(needle, str) else needle

    def wrapped(args):
        if any(n in " ".join(str(a) for a in args) for n in needles) and left[0] > 0:
            left[0] -= 1
            raise RuntimeError("simulated gh failure: %s" % needle)
        return run(args)

    wrapped.calls, wrapped.live = run.calls, run.live
    return wrapped


# the call shapes `fetch_body_labels` makes: #895 REST-first GET, then the `issue view` fallback
_VIEW_UNIT = ("api repos/o/r/issues/42 --method GET", "--json body,labels")
_REST_READ = "api repos/o/r/issues/42"     # the one call shape `work._declared_unit` makes


def _rest_reads(calls):
    return [" ".join(str(a) for a in c) for c in calls if " ".join(str(a) for a in c) == _REST_READ]


def test_a_gh_issue_view_blip_does_not_disarm_the_scope_gate():
    """THE DEFECT, end to end through the real `_next`. The unit is declared by a body the GraphQL
    read could not fetch; the REST read -- a SEPARATE hourly bucket, which is why it can still
    answer -- resolves it. The scope gate is therefore armed and refuses a goal whose repo the unit
    does not list, instead of waving through a pick that `work.start()` would then cut from
    `feature/voice-interview` anyway."""
    lp = _mod("loop")
    run = _queue_runner({42: {"body": _DECLARES}})
    gh = _source(_blip(run, _VIEW_UNIT))
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base, "voice-interview", _unlisted_entry(_OTHER))
        _rest(lp, run, {"body": _DECLARES, "labels": []})
        kind, goal = lp._next(base, gh, cfg)
    assert (kind, goal) == ("DONE", None)                  # refused, and nothing else was queued
    assert "sdlc:in-progress" not in run.live["42"]
    # #1005: with ai_filed.triage on (default) the scope hold is a PARK, not needs-confirmation.
    assert "sdlc:parked" in run.live["42"]


def test_the_gate_and_the_base_cannot_disagree_about_whether_a_unit_exists():
    """The "done when" stated as one assertion: after the SAME single transport failure, the unit
    handed to the pick-time gates is the unit the BASE reader resolves -- compared against
    `work._declared_unit`'s own answer, never against a literal, so the two cannot drift apart in
    this test either."""
    lp, work = _mod("loop"), _mod("work")
    seen = []
    run = _queue_runner({42: {"body": _DECLARES}})
    gh = _source(_blip(run, _VIEW_UNIT))
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base)
        _rest(lp, run, {"body": _DECLARES, "labels": []})
        lp._feature_propagate = lambda: _recorder(seen)
        lp._feature_owner = lambda: _recorder(seen)
        lp._next(base, gh, cfg)
        base_unit, _, ok = work._declared_unit(
            cfg, "42", lambda cwd, argv: json.dumps({"body": _DECLARES, "labels": []}), d)
    assert ok is True and base_unit == "voice-interview"
    assert seen == [base_unit, base_unit], seen        # both gates, and neither was told "no unit"


def _recorder(seen):
    """A stand-in for a pick-time gate that records the UNIT it was handed and always proceeds."""
    import types as _types
    return _types.SimpleNamespace(
        gate_at_pick=lambda sdlc_dir, source, goal, config, unit, **kw: _types.SimpleNamespace(
            proceed=(seen.append(unit) is None)))


def test_a_read_that_succeeded_adds_no_second_unit_read():
    """The cost rule. A pick whose GraphQL read ANSWERED pays nothing new: the only REST read is the
    landing check's, which #1472 already made, and it lands AFTER the claim. Asserted on the order
    of the real call log, because a count alone cannot tell the pre-existing read from a new one."""
    lp = _mod("loop")
    run = _queue_runner({42: {"body": _DECLARES}})
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base)
        calls = _rest(lp, run, {"body": _DECLARES, "labels": []})
        kind, goal = lp._next(base, gh, cfg)
    assert (kind, goal) == ("goal", "42")
    flat = _flat(run)
    assert len(_rest_reads(calls)) == 1, flat
    claimed = next(i for i, c in enumerate(flat) if "--add-label sdlc:in-progress" in c)
    assert flat.index(_REST_READ) > claimed, flat


def test_a_goal_that_declares_no_unit_is_unchanged():
    """BYTE-IDENTICAL for the adoption case this must not tax: the read answered, and the answer was
    "nothing declared". That is an ANSWER, not ignorance, so no second reader is consulted."""
    lp = _mod("loop")
    run = _queue_runner({42: {"body": "ordinary prose, no declaration\n"}})
    gh = _source(run)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base)
        calls = _rest(lp, run, {"body": "ordinary prose, no declaration\n", "labels": []})
        kind, goal = lp._next(base, gh, cfg)
    assert (kind, goal) == ("goal", "42")
    flat = _flat(run)
    claimed = next(i for i, c in enumerate(flat) if "--add-label sdlc:in-progress" in c)
    assert flat.index(_REST_READ) > claimed, flat      # the landing check's, and only it


def test_a_project_that_never_adopted_units_pays_nothing_for_the_blip():
    """The adoption promise, unchanged. With no `.sdlc/features/` BOTH gates and the landing check
    answer `not-adopted` without ever consulting the unit, so resolving one would be pure cost. The
    failed read degrades exactly as it did before this existed: proceed, no unit, no extra call."""
    lp = _mod("loop")
    run = _queue_runner({42: {"body": _DECLARES}})
    gh = _source(_blip(run, _VIEW_UNIT))
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        calls = _rest(lp, run, {"body": _DECLARES, "labels": []})
        kind, goal = lp._next(base, gh, cfg)
    assert (kind, goal) == ("goal", "42")
    assert _rest_reads(calls) == []


def test_a_second_transport_failure_still_cannot_stop_the_queue():
    """FAIL-OPEN, and the residue is named rather than hidden: when the fallback ALSO fails there is
    no unit to hand anybody, so the gates degrade to exactly what they did before -- proceed. Two
    failures cost a pick its gate; one no longer does."""
    lp = _mod("loop")
    run = _queue_runner({42: {"body": _DECLARES}})
    gh = _source(_blip(run, _VIEW_UNIT))
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base, "voice-interview", _unlisted_entry(_OTHER))
        _rest(lp, run, ok=False)
        kind, goal = lp._next(base, gh, cfg)
    assert (kind, goal) == ("goal", "42")
    assert "sdlc:needs-confirmation" not in run.live["42"]


def test_a_fallback_that_explodes_never_breaks_a_pick():
    """The outer guard, and it is not decoration: this runs INSIDE the claim lock, so an exception
    escaping here leaks the lock and makes the goal unpickable by anyone."""
    lp = _mod("loop")
    run = _queue_runner({42: {"body": _DECLARES}})
    gh = _source(_blip(run, _VIEW_UNIT))
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base)
        lp._cross_repo = lambda: (_ for _ in ()).throw(RuntimeError("cross_repo is gone"))
        kind, goal = lp._next(base, gh, cfg)
        fd = lp._try_acquire_claim_lock(base, "42")
        assert fd is not None, "the fallback path leaked #42's claim lock"
        lp._release_claim_lock(fd)
    assert (kind, goal) == ("goal", "42")


# ------------------------------------------------- #1543: not gated on `.sdlc/features/`, on purpose
#
# `feature_sync._sync_at_pick` and `cross_repo._check_at_pick` both open with
# `feature_registry.registry_dir(sdlc_dir).is_dir()` and answer `not-adopted` before spending
# anything, on a repo that never ran `mkdir -p .sdlc/features`. `attach_at_pick` and
# `resume_needs_label` -- the whole of this module -- never make that call. The module's own
# docstring ("IS ANY OF THE ABOVE GATED...") states the three tests below are what back it: this
# file is deliberately absent from the pinned census, contrasted execution proves the OUTCOME does
# not depend on adoption, and the census itself is pinned against a future consumer nobody reviewed.

_ADOPTION_GATE_PATTERN = re.compile(r"features_dir|registry_dir")


def _is_adoption_gate_call(node):
    """True for a `Call` shaped like `<features_dir-or-registry_dir-ish>.is_dir()` -- the one idiom
    every registry-aware consumer in this tree uses to opt out of an unadopted project
    (`work.py`'s own docstring calls it "the one spelling of that check"). Matched on the UNPARSED
    receiver expression rather than on an exact name, so `feature_registry.registry_dir(sdlc_dir)
    .is_dir()` and a local `features_dir.is_dir()` are both caught by the one rule."""
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "is_dir"
            and bool(_ADOPTION_GATE_PATTERN.search(ast.unparse(node.func.value))))


def _enclosing_function(tree, lineno):
    """The name of the SMALLEST function definition whose span contains `lineno`, or None for
    module-level code. Smallest, not first-found, so a call inside a nested closure is attributed
    to the closure, not to whatever outer function merely contains it -- not exercised by anything
    in this tree today (every known site is a plain top-level function), but cheap to get right."""
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = node.lineno
            end = getattr(node, "end_lineno", None) or max(
                (getattr(n, "lineno", start) for n in ast.walk(node)), default=start)
            if start <= lineno <= end and (best is None or (end - start) < (best[1] - best[0])):
                best = (start, end, node.name)
    return best[2] if best else None


def _adoption_gated_call_sites():
    """Every `file.py:function` under `skills/sigma-loop/scripts/` containing an adoption-gate call,
    per `_is_adoption_gate_call` above. AST-based, never text-based: a mention of `is_dir()` or
    `registry_dir` inside a docstring or comment -- this very module now carries several, see
    `feature_labels.py`'s own module docstring -- can never be mistaken for the real thing, because
    a docstring is an `ast.Constant`, never an `ast.Call`."""
    found = set()
    for path in sorted(S.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if _is_adoption_gate_call(node):
                found.add("%s:%s" % (path.name,
                                      _enclosing_function(tree, node.lineno) or "<module>"))
    return found


#: THE CENSUS, REVIEWED (#1543). A fifteenth call site anywhere under `skills/sigma-loop/scripts/`
#: fails `test_the_set_of_adoption_gated_call_sites_is_pinned` below until it is added here on
#: purpose -- which is the point: whether a future consumer gates on `.sdlc/features/` must be a
#: decision someone writes down, never a side effect nobody reviewed.
_KNOWN_ADOPTION_GATED_CALL_SITES = frozenset({
    "cross_repo.py:_check_at_pick",
    "feature_owner.py:_claim_at_pick",
    "feature_owner.py:_gate_at_pick",
    "feature_owner.py:gate_at_filing",
    "feature_owner.py:would_hold",
    "feature_propagate.py:_gate_at_pick",
    "feature_propagate.py:_propagate_at_pick",
    "feature_rebase.py:_upkeep",
    "feature_sync.py:_sync_at_pick",
    "loop.py:_unit_at_pick",
    # #1005: the scope-expansion hold moved out of promote.py into gate_hold.py (same gate, new home).
    "gate_hold.py:scope_expansion",
    "unit_completion.py:_signal",
    "work.py:_adopted",
    "work.py:_sibling_gate",
    # #2263, added on purpose: D-7 of `.sdlc/design/2253.md` requires the no-dangling-goal pair
    # (`sdlc:needs-unit` set-aside / `core` attribution) to gate on `.sdlc/features/` existing --
    # applying either to an unadopted repo would set aside or silently re-attribute its ENTIRE
    # backlog, unlike the ORIGINAL `feature_labels.py` machinery #1543 is about (attach_at_pick's
    # `BODY_ONLY` handling, `resume_needs_label`), which stays deliberately UNGATED -- see
    # `test_the_label_half_never_reads_the_features_directory`, updated the same way, below.
    "feature_labels.py:_handle_no_unit_at_pick",
    # #2311, added on purpose: D-4 of `.sdlc/design/2289.md` requires the passive drift watcher's
    # own unit enumeration to be INERT -- report nothing, cost nothing -- on a repo with no
    # `.sdlc/features/` directory at all, mirroring `unit_completion.py:_signal`'s own
    # `NOT_ADOPTED` posture rather than inventing a second reading of the same question.
    "drift_watch.py:_open_units",
    # #2363, added on purpose: the FILING-time twin of `feature_labels.py:_handle_no_unit_at_pick`
    # above -- `handoff.create_tracked_issue`'s automatic classification is gated on the SAME two
    # conditions (opted in, registry adopted) for the SAME reason: applying it to an unadopted
    # repo would attribute every unit-less filing to a catch-all nobody configured for that repo.
    "handoff.py:_auto_classify_unit",
    # #2435, added on purpose: `ensure_unit_tracking` is the side-job unit-registry ensure --
    # checking (and, best-effort, repairing) whether a goal already being worked appears in its
    # unit's registry entry. Gating it on adoption is required for the identical reason
    # `_handle_no_unit_at_pick` is gated: a repo with no `.sdlc/features/` has no registry to check
    # a goal against at all, and running the read/repair machinery anyway would be pure waste on
    # every non-adopting repo's every goal-scoped trigger. It sits with the gated majority, not with
    # `feature_labels.py`'s original, deliberately-ungated machinery -- see
    # `test_the_label_half_never_reads_the_features_directory`, unchanged by this addition.
    "feature_labels.py:ensure_unit_tracking",
})


def test_the_set_of_adoption_gated_call_sites_is_pinned():
    """THE FOURTH-CONSUMER GUARD -- #1543's own "done when": pinned by a test that fails if a
    fourth consumer later disagrees. A new module, or a new function in an existing one, that
    starts (or stops) checking whether `.sdlc/features/` exists changes this set, and this
    assertion goes red until a human adds it to `_KNOWN_ADOPTION_GATED_CALL_SITES` above --
    deciding on purpose whether it belongs with the gated majority or with `feature_labels.py`,
    which appears in neither list."""
    assert _adoption_gated_call_sites() == _KNOWN_ADOPTION_GATED_CALL_SITES


def test_the_label_half_never_reads_the_features_directory():
    """STRUCTURAL, on `feature_labels.py`'s ORIGINAL machinery -- `attach_at_pick`'s `BODY_ONLY`
    handling and `resume_needs_label`, #1543's own subject -- which contains zero adoption-gate
    calls, unchanged. Asserted directly, not merely inferred from the census above, because this is
    the exact claim #1543 is about and the one a future edit is most likely to weaken without
    noticing.

    #2263 NARROWS THIS TEST RATHER THAN DELETING IT. The file as a whole now DOES contain one
    adoption-gate call -- inside `_handle_no_unit_at_pick`, D-7's DELIBERATE requirement for the
    no-dangling-goal pair, registered in `_KNOWN_ADOPTION_GATED_CALL_SITES` above on purpose. So the
    claim this test makes is no longer "the whole file", it is "the two functions #1543 was
    actually about" -- which is the claim that still matters: a future edit that gates
    `attach_at_pick`'s `BODY_ONLY` path or `resume_needs_label` on adoption would break the
    #1543 promise this test exists to hold, and this still catches that.

    #2435 ADDS A SECOND, NARROWED THE SAME WAY. `ensure_unit_tracking` is a THIRD function in this
    file that gates on adoption, for `_handle_no_unit_at_pick`'s own reason one level down (a goal
    cannot be checked against a registry that does not exist) -- registered in
    `_KNOWN_ADOPTION_GATED_CALL_SITES` above, and named here so the set this test pins stays
    exactly "the two functions #1543 is actually about", never silently growing to tolerate a third
    one nobody decided on."""
    tree = ast.parse((S / "feature_labels.py").read_text())
    gated_functions = {_enclosing_function(tree, n.lineno)
                       for n in ast.walk(tree) if _is_adoption_gate_call(n)}
    assert gated_functions == {"_handle_no_unit_at_pick", "ensure_unit_tracking"}, gated_functions
    assert "attach_at_pick" not in gated_functions
    assert "resume_needs_label" not in gated_functions


def test_a_project_with_no_registry_at_all_is_refused_and_flagged_exactly_like_an_adopted_one():
    """THE DECISION, EXECUTED -- not merely read off a docstring. Two identical picks, same body
    and same missing label, differ in exactly one thing: whether `.sdlc/features/` exists. #1543's
    answer is that the label half's outcome must not depend on it, so both branches below assert
    the SAME thing: refused, `sdlc:needs-label` written, `sdlc:goal` kept, one flag comment posted
    -- on any repository, adopted or not. This is the exact transcript `docs/branching-model.md`
    §14 measures, run for real on both sides of the line it draws rather than read on one."""
    fl = _mod("feature_labels")

    def _pick(base, cfg, adopt):
        run = _runner(body=_DECLARES, labels={"sdlc:goal"}, absent={_LABEL})
        gh = _source(run)
        if adopt:
            _adopt(base)
        assert (pathlib.Path(base) / "features").is_dir() == adopt
        return fl.attach_at_pick(base, gh, "42", cfg), run

    with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
        base1, cfg1 = _sdlc(d1)
        never_adopted, run1 = _pick(base1, cfg1, adopt=False)
        base2, cfg2 = _sdlc(d2)
        adopted, run2 = _pick(base2, cfg2, adopt=True)

    for decision, run, label in ((never_adopted, run1, "never adopted"),
                                  (adopted, run2, "adopted")):
        assert decision.outcome == fl.REFUSED_NO_LABEL, label
        assert decision.proceed is False, label
        assert "sdlc:needs-label" in run.labels and "sdlc:goal" in run.labels, label
        posted = [c[-1] for c in run.calls if c[:2] == ["issue", "comment"]]
        assert len(posted) == 1 and fl.MISSING_LABEL_MARKER in posted[0], (label, _flat(run))


# =====================================================================================================
# #2263: "no dangling goal" -- a goal declaring NO unit at all (`features.NONE`), opt-in, gated on
# BOTH `discovery.no_dangling_goal.enabled` AND `.sdlc/features/` existing (D-7 of
# `.sdlc/design/2253.md`). Two mutually-exclusive behaviours, selected by whether `core` is
# configured: SET-ASIDE under `sdlc:needs-unit` (the sibling of `sdlc:needs-label`, same shape,
# different reason), or ATTRIBUTION to a `core` sentinel, recorded back onto the issue as a comment
# rather than a real `feature:*` label (D-6: `core` is a sentinel, never a unit with a branch).
# =====================================================================================================

_NO_DECLARATION = "ordinary issue text, nothing declared\n"


def _no_unit_cfg(core=None, repo="o/r"):
    """A FRESH dict every call -- deliberately never derived from the module-level `_CONFIG` via
    `dict(_CONFIG)` (a shallow copy that would share, and let this file's own tests mutate, the
    SAME nested `discovery` dict `_source`'s default `_CONFIG` uses elsewhere in this file)."""
    d = {"discovery": {"source": "github", "github": {"repo": repo},
                       "no_dangling_goal": {"enabled": True}}}
    if core is not None:
        d["discovery"]["no_dangling_goal"]["core"] = core
    return d


def _source_cfg(run, config):
    gh = _mod("sources").GitHubSource(config, run=run)
    gh._LABEL_SWAP_RETRIES, gh._LABEL_SWAP_RETRY_BASE = 1, 0
    return gh


# --------------------------------------------------- the control: byte-identical when off, by construction
#
# THE DANCE, DONE FOR REAL (AGENTS.md: "run the control, or the check is decoration"), against BOTH
# tests immediately below -- one gate each. Each guard in `_handle_no_unit_at_pick` was temporarily
# short-circuited to `if False and <condition>: return None` in turn, forcing that ONE gate past
# regardless of its real answer:
#
#   - `test_..._is_unaffected_when_the_feature_is_off_by_default`: the `no_dangling_goal_enabled`
#     guard forced past. Went RED for the right reason -- `_notes(base)` gained a ledger entry
#     (`assert _notes(base) == []` failed) -- because the fixture deliberately adopts the registry
#     (`_adopt(base)`) so ONLY the config gate stood between "off" and "on".
#   - `test_..._is_unaffected_with_no_registry_even_when_enabled`: the `adopted` guard forced past.
#     Went RED for the identical reason, with the registry genuinely absent this time.
#
# Restoring each guard turned its test green again. Matches #2262's own control test's documented
# dance (`tests/test_sources.py::test_github_feature_priority_off_by_default_is_byte_identical_
# to_before_2262`).

def test_a_goal_declaring_no_unit_is_unaffected_when_the_feature_is_off_by_default():
    """THE CONTROL TEST. `discovery.no_dangling_goal` is UNSET (`_CONFIG`'s own default) -- and the
    registry IS adopted, so the ONLY thing keeping this off is the config gate, not the registry
    gate `test_..._with_no_registry_even_when_enabled` below covers separately. Byte-identical to
    a goal declaring no unit before #2263 existed: one read, zero writes."""
    fl = _mod("feature_labels")
    run = _runner(body=_NO_DECLARATION, labels={"sdlc:goal"})
    gh = _source(run)                                    # _CONFIG: no_dangling_goal unset
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base)                                      # registry adopted -- config is the gate
        decision = fl.attach_at_pick(base, gh, "42", cfg)
        assert _notes(base) == []
    assert decision.proceed is True and decision.outcome == fl.NOTHING_TO_DO and decision.unit is None
    assert all(gqlfake.is_issue_read(c) for c in run.calls), _flat(run)
    assert "sdlc:needs-unit" not in run.labels


def test_a_goal_declaring_no_unit_is_unaffected_with_no_registry_even_when_enabled():
    """D-7's SECOND gate, isolated: `no_dangling_goal.enabled` is `True`, but `.sdlc/features/`
    does not exist. Applying the feature to an unadopted repo would set aside its entire backlog
    (every issue declares no unit on a repo that never heard of one) -- so this must stay off too."""
    fl = _mod("feature_labels")
    run = _runner(body=_NO_DECLARATION, labels={"sdlc:goal"})
    gh = _source_cfg(run, _no_unit_cfg())
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        assert not (pathlib.Path(base) / "features").is_dir()
        decision = fl.attach_at_pick(base, gh, "42", cfg)
        assert _notes(base) == []
    assert decision.proceed is True and decision.outcome == fl.NOTHING_TO_DO
    assert "sdlc:needs-unit" not in run.labels


def test_a_goal_declaring_no_unit_is_unaffected_with_core_configured_but_the_feature_off():
    """The `core` key alone is not the opt-in -- `enabled` is. A repo that configured `core` for
    later but never flipped `enabled` must see no behaviour change either."""
    fl = _mod("feature_labels")
    run = _runner(body=_NO_DECLARATION, labels={"sdlc:goal"})
    cfg_dict = {"discovery": {"source": "github", "github": {"repo": "o/r"},
                              "no_dangling_goal": {"core": "core"}}}     # enabled absent
    gh = _source_cfg(run, cfg_dict)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base)
        decision = fl.attach_at_pick(base, gh, "42", cfg)
    assert decision.proceed is True and decision.outcome == fl.NOTHING_TO_DO
    assert "sdlc:needs-unit" not in run.labels
    assert [c for c in run.calls if c[:2] == ["issue", "comment"]] == []


# --------------------------------------------------- the set-aside half: sdlc:needs-unit

def test_a_goal_declaring_no_unit_is_set_aside_when_enabled_and_adopted():
    fl = _mod("feature_labels")
    run = _runner(body=_NO_DECLARATION, labels={"sdlc:goal"})
    gh = _source_cfg(run, _no_unit_cfg())
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base)
        decision = fl.attach_at_pick(base, gh, "42", cfg)
    assert decision.proceed is False
    assert decision.outcome == fl.SET_ASIDE_NO_UNIT
    assert decision.unit is None
    assert "sdlc:needs-unit" in run.labels and "sdlc:goal" in run.labels
    posted = [c[-1] for c in run.calls if c[:2] == ["issue", "comment"]]
    assert len(posted) == 1 and fl.NEEDS_UNIT_MARKER in posted[0], posted


def test_the_set_aside_names_the_clearing_gesture():
    """The issue's own requirement: "a flag comment names the one gesture that clears it"."""
    fl = _mod("feature_labels")
    run = _runner(body=_NO_DECLARATION, labels={"sdlc:goal"})
    gh = _source_cfg(run, _no_unit_cfg())
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base)
        fl.attach_at_pick(base, gh, "42", cfg)
    posted = [c[-1] for c in run.calls if c[:2] == ["issue", "comment"]][0]
    assert "Feature:" in posted and "feature:" in posted        # body marker AND label routes named


def test_the_no_unit_flag_is_posted_only_once_per_timeline_read():
    fl = _mod("feature_labels")
    run = _runner(body=_NO_DECLARATION, labels={"sdlc:goal"},
                  comments=["earlier\n" + fl.NEEDS_UNIT_MARKER + "\nmore"])
    gh = _source_cfg(run, _no_unit_cfg())
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base)
        decision = fl.attach_at_pick(base, gh, "42", cfg)
    assert decision.outcome == fl.SET_ASIDE_NO_UNIT
    assert [c for c in run.calls if c[:2] == ["issue", "comment"]] == []


def test_the_new_markers_are_distinct_from_each_other_and_from_the_siblings():
    fl = _mod("feature_labels")
    markers = {fl.MISSING_LABEL_MARKER, fl.AMBIGUOUS_MARKER, fl.NEEDS_UNIT_MARKER}
    assert len(markers) == 3, markers


def test_a_no_unit_overlay_that_does_not_land_refuses_without_a_flag():
    """Same fail-open promise as the sibling: no half-state to clean up, because the label is the
    only state there is."""
    fl = _mod("feature_labels")
    run = _runner(body=_NO_DECLARATION, labels={"sdlc:goal"}, fail_on=["mutation"])
    gh = _source_cfg(run, _no_unit_cfg())
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base)
        decision = fl.attach_at_pick(base, gh, "42", cfg)
        assert _notes(base, "42") == []
    assert decision.proceed is False and decision.outcome == fl.SET_ASIDE_NO_UNIT
    assert "sdlc:needs-unit" not in run.labels
    assert [c for c in run.calls if c[:2] == ["issue", "comment"]] == []


def test_a_no_unit_pick_is_recorded_in_the_ledger():
    fl = _mod("feature_labels")
    run = _runner(body=_NO_DECLARATION, labels={"sdlc:goal"})
    gh = _source_cfg(run, _no_unit_cfg())
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base)
        fl.attach_at_pick(base, gh, "42", cfg)
        tokens = _tokens(base, "42")
    assert fl.NO_UNIT_BLOCK_TOKEN in tokens, tokens


# --------------------------------------------------- the redirect: "core configured" -> feature_classify
#
# #2363 reverses D-6 of `.sdlc/design/2253.md`: `core` is now a REAL registered unit (bootstrapped
# once, ahead of this feature), so `_handle_no_unit_at_pick`'s "core configured" branch redirects
# to `feature_classify.classify_at_pick` instead of writing a sentinel comment. The 4-tier chain
# itself -- every tier, in detail, with hand-constructed judgments, and the never-creates-a-unit
# guarantee at every tier -- is tested in `test_feature_classify.py`. These tests pin only the
# REDIRECT: that `_handle_no_unit_at_pick` really reaches `feature_classify`, with a real registered
# `core` unit, through the real `GitHubSource`.

def test_core_configured_redirects_to_feature_classify_and_attaches_the_real_label():
    """THE REDIRECT, proved rather than assumed. With `core` bootstrapped as a real, OPEN
    registered unit, `_handle_no_unit_at_pick` reaches `feature_classify.classify_at_pick`'s
    default (judge-less) chain, which abstains into tier 2 and attaches the REAL `feature:core`
    label -- not a comment merely naming a sentinel, which is the whole of what D-6 reverses."""
    fl, fc = _mod("feature_labels"), _mod("feature_classify")
    run = _runner(body=_NO_DECLARATION, labels={"sdlc:goal"})
    gh = _source_cfg(run, _no_unit_cfg(core="core"))
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base, unit="core")                     # entry=None -> open defaults True
        decision = fl.attach_at_pick(base, gh, "42", cfg)
    assert decision.proceed is True
    assert decision.outcome == fc.TIER2_ATTACHED
    assert decision.unit == "core"
    assert "feature:core" in run.labels
    posted = [c[-1] for c in run.calls if c[:2] == ["issue", "comment"]]
    assert len(posted) == 1 and fc.TIER2_MARKER in posted[0], posted


def test_the_classification_redirect_never_creates_the_core_label_only_attaches_it():
    """D-6 reversed: the label really is attached now. What must NEVER happen is CREATION -- the
    never-create guarantee (#1468) applies to this redirected path exactly as it does to every
    other attach in this family, asserted on the recorded gh call log (never a return value), the
    same discipline `test_gh_label_create_is_never_invoked_for_a_feature_label` uses."""
    fl = _mod("feature_labels")
    run = _runner(body=_NO_DECLARATION, labels={"sdlc:goal"})
    gh = _source_cfg(run, _no_unit_cfg(core="core"))
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base, unit="core")
        fl.attach_at_pick(base, gh, "42", cfg)
    assert "feature:core" in run.labels
    assert not any(c[:2] == ["label", "create"] for c in run.calls), run.calls


def test_the_classification_comment_is_posted_only_once_per_timeline_read():
    fl, fc = _mod("feature_labels"), _mod("feature_classify")
    run = _runner(body=_NO_DECLARATION, labels={"sdlc:goal"},
                  comments=["earlier\n" + fc.TIER2_MARKER + "\nmore"])
    gh = _source_cfg(run, _no_unit_cfg(core="core"))
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base, unit="core")
        decision = fl.attach_at_pick(base, gh, "42", cfg)
    assert decision.outcome == fc.TIER2_ATTACHED
    assert [c for c in run.calls if c[:2] == ["issue", "comment"]] == []


def test_the_classification_is_recorded_in_the_ledger():
    fl, fc = _mod("feature_labels"), _mod("feature_classify")
    run = _runner(body=_NO_DECLARATION, labels={"sdlc:goal"})
    gh = _source_cfg(run, _no_unit_cfg(core="core"))
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base, unit="core")
        fl.attach_at_pick(base, gh, "42", cfg)
        tokens = _tokens(base, "42")
    assert fc.TIER2_TOKEN in tokens, tokens


def test_core_configured_selects_classification_over_set_aside():
    """Mutual exclusivity, selected by config, not guessed: with `core` set the goal is classified
    (and attached to the real unit); the `sdlc:needs-unit` overlay is never written for the same
    undeclared goal."""
    fl, fc = _mod("feature_labels"), _mod("feature_classify")
    run = _runner(body=_NO_DECLARATION, labels={"sdlc:goal"})
    gh = _source_cfg(run, _no_unit_cfg(core="core"))
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base, unit="core")
        decision = fl.attach_at_pick(base, gh, "42", cfg)
    assert decision.outcome == fc.TIER2_ATTACHED
    assert "sdlc:needs-unit" not in run.labels


def test_core_left_unconfigured_selects_set_aside_over_classification():
    fl = _mod("feature_labels")
    run = _runner(body=_NO_DECLARATION, labels={"sdlc:goal"})
    gh = _source_cfg(run, _no_unit_cfg())                     # core absent
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base)
        decision = fl.attach_at_pick(base, gh, "42", cfg)
    assert decision.outcome == fl.SET_ASIDE_NO_UNIT
    assert "feature:core" not in run.labels


# --------------------------------------------------- #2380: the live-judge opt-in, wired on top
#
# `feature_judge.py`'s own chain (assign/validate/majority-vote/spend-ceiling) is tested in
# `tests/test_feature_judge.py`. These tests pin only the WIRING: `_handle_no_unit_at_pick` passes
# `judge=None` (byte-identical to today) when `no_dangling_goal_live_judge_enabled` is off/absent,
# and `judge=feature_judge.live_judge` when it is on -- never re-implementing any of that module's
# own logic here.

class _LiveJudgeSource:
    """The minimal surface `_handle_no_unit_at_pick` needs to reach the "core configured" branch,
    with the new #2380 attribute as the one thing each test below varies."""
    no_dangling_goal_enabled = True
    no_dangling_goal_core = "core"

    def __init__(self, live_judge_enabled=False):
        self.no_dangling_goal_live_judge_enabled = live_judge_enabled

    def mark_needs_unit(self, goal):
        return True

    def clear_needs_unit(self, goal):
        return True

    def list_needs_unit(self):
        return []


def test_live_judge_off_by_default_passes_judge_none_byte_identical_to_before_2380(monkeypatch):
    fl = _mod("feature_labels")
    captured = {}

    class FakeClassify:
        @staticmethod
        def classify_at_pick(sdlc_dir, source, goal, config, core, judge=None):
            captured["judge"] = judge
            return "sentinel-decision"

    monkeypatch.setattr(fl, "_feature_classify", lambda: FakeClassify)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base, unit="core")
        result = fl._handle_no_unit_at_pick(base, _LiveJudgeSource(live_judge_enabled=False), "42",
                                            cfg)
    assert result == "sentinel-decision"
    assert "judge" in captured and captured["judge"] is None


def test_live_judge_off_when_the_attribute_is_entirely_absent_also_passes_judge_none(monkeypatch):
    """A source that predates #2380 (no attribute at all, not merely `False`) must degrade
    identically -- `getattr(..., False)`, not a bare attribute access."""
    fl = _mod("feature_labels")
    captured = {}

    class FakeClassify:
        @staticmethod
        def classify_at_pick(sdlc_dir, source, goal, config, core, judge=None):
            captured["judge"] = judge
            return "sentinel-decision"

    class PredatesSlice:
        no_dangling_goal_enabled = True
        no_dangling_goal_core = "core"
        def mark_needs_unit(self, goal): return True
        def clear_needs_unit(self, goal): return True
        def list_needs_unit(self): return []

    monkeypatch.setattr(fl, "_feature_classify", lambda: FakeClassify)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base, unit="core")
        fl._handle_no_unit_at_pick(base, PredatesSlice(), "42", cfg)
    assert captured["judge"] is None


def test_live_judge_on_routes_through_feature_judge_live_judge(monkeypatch):
    fl = _mod("feature_labels")
    captured = {}
    sentinel = object()

    class FakeClassify:
        @staticmethod
        def classify_at_pick(sdlc_dir, source, goal, config, core, judge=None):
            captured["judge"] = judge
            return "sentinel-decision"

    class FakeJudgeModule:
        live_judge = sentinel

    monkeypatch.setattr(fl, "_feature_classify", lambda: FakeClassify)
    monkeypatch.setattr(fl, "_feature_judge", lambda: FakeJudgeModule)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base, unit="core")
        result = fl._handle_no_unit_at_pick(base, _LiveJudgeSource(live_judge_enabled=True), "42",
                                            cfg)
    assert result == "sentinel-decision"
    assert captured["judge"] is sentinel


def test_the_old_core_sentinel_symbols_are_gone():
    """#2363: D-6 reversed -- `core` is a real unit now, never a sentinel string. These symbols
    existed only to support the deleted sentinel design."""
    fl = _mod("feature_labels")
    for name in ("_attribute_to_core", "ATTRIBUTED_CORE", "CORE_ATTRIBUTION_MARKER",
                "CORE_ATTRIBUTION_TOKEN", "_core_attribution_text"):
        assert not hasattr(fl, name), name


# --------------------------------------------------- the surface gate: no source, or a partial one

def test_a_source_with_no_no_dangling_goal_surface_is_a_no_op():
    """A source that has `no_dangling_goal_enabled` set but none of the three new methods --
    exactly the shape the next level produces the moment it adds the config plumbing but not the
    methods -- degrades cleanly rather than raising inside the pick path."""
    fl = _mod("feature_labels")

    class NoSurface:
        no_dangling_goal_enabled = True
        no_dangling_goal_core = None

        def fetch_body_labels(self, goal):
            return {"body": _NO_DECLARATION, "labels": [{"name": "sdlc:goal"}]}
        def label_exists(self, name):
            return False
        def attach_label(self, goal, name):
            return True
        def mark_needs_label(self, goal):
            return True
        def clear_needs_label(self, goal):
            return True
        def list_needs_label(self):
            return []

    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base)
        decision = fl.attach_at_pick(base, NoSurface(), "42", cfg)      # must not raise
        assert fl.resume_needs_unit(base, NoSurface(), cfg) == []       # same gate, same promise
    assert decision.proceed is True and decision.outcome == fl.NOTHING_TO_DO


def test_the_no_dangling_goal_surface_names_every_method_the_module_calls():
    """Both directions, mirroring `test_the_required_surface_names_every_method_the_module_calls`
    for the new surface."""
    fl, sources = _mod("feature_labels"), _mod("sources")
    for name in fl.NO_DANGLING_GOAL_METHODS:
        assert callable(getattr(sources.GitHubSource, name, None)), name

    calls = []

    class ExactlyTheDeclaredSurface:
        no_dangling_goal_enabled = True
        no_dangling_goal_core = None

        def fetch_body_labels(self, goal):
            return {"body": _NO_DECLARATION, "labels": [{"name": "sdlc:goal"}]}
        def label_exists(self, name):
            return False
        def attach_label(self, goal, name):
            return True
        def mark_needs_label(self, goal):
            return True
        def clear_needs_label(self, goal):
            return True
        def list_needs_label(self):
            return []
        def mark_needs_unit(self, goal):
            calls.append("mark"); return True
        def clear_needs_unit(self, goal):
            calls.append("clear"); return True
        def list_needs_unit(self):
            calls.append("list")
            # a unit IS now declared -- the release condition for THIS overlay, the mirror image
            # of the sibling fixture's "no declaration at all" (which is what RELEASES its own,
            # opposite-condition overlay one function up).
            return [{"number": 42, "body": _DECLARES, "labels": [{"name": "sdlc:goal"}]}]
        def fetch_comments_strict(self, goal):
            return {"comments": [], "labels": []}
        def note(self, goal, text):
            pass

    fake = ExactlyTheDeclaredSurface()
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base)
        assert fl.attach_at_pick(base, fake, "42", cfg).proceed is False   # must not raise
        assert fl.resume_needs_unit(base, fake, cfg) == ["42"]             # must not raise
    assert calls == ["mark", "list", "clear"], calls


# --------------------------------------------------- resume: the self-healing half

def _held_no_unit(d, core=None, ledger_on=True):
    fl = _mod("feature_labels")
    base, cfg = _sdlc(d, ledger_on=ledger_on)
    _adopt(base)
    run = _runner(body=_NO_DECLARATION, labels={"sdlc:goal"})
    gh = _source_cfg(run, _no_unit_cfg(core=core))
    assert fl.attach_at_pick(base, gh, "42", cfg).outcome == fl.SET_ASIDE_NO_UNIT
    return base, cfg, run, gh


def test_resume_needs_unit_clears_the_overlay_once_a_unit_is_declared():
    """The human's ONE gesture: declaring a unit is enough -- nothing is un-parked. `_runner`'s
    body text is fixed at construction (no seam to edit it afterward), so the declaration here is
    via LABEL -- attaching an existing `feature:*` label directly to the live set is enough to move
    `features.read` off `NONE`, exactly like `resume_needs_label`'s own resume trigger."""
    fl = _mod("feature_labels")
    with tempfile.TemporaryDirectory() as d:
        base, cfg, run, gh = _held_no_unit(d)
        assert "sdlc:needs-unit" in run.labels
        run.calls.clear()
        run.labels.add("feature:declared-unit")          # a human declares a unit
        released = fl.resume_needs_unit(base, gh, cfg)
    assert released == ["42"]
    assert "sdlc:needs-unit" not in run.labels and "sdlc:goal" in run.labels


def test_resume_needs_unit_stays_held_while_still_undeclared():
    fl = _mod("feature_labels")
    with tempfile.TemporaryDirectory() as d:
        base, cfg, run, gh = _held_no_unit(d)
        released = fl.resume_needs_unit(base, gh, cfg)
    assert released == []
    assert "sdlc:needs-unit" in run.labels


def test_resume_needs_unit_releases_on_an_ambiguous_edit_unlike_the_sibling():
    """THE DELIBERATE DIVERGENCE from `_still_held`, documented in `_still_held_no_unit`'s own
    docstring: `_still_held` KEEPS an ambiguous issue held (its own condition -- a body declaration
    whose label is missing -- is neither fixed nor contradicted by a second, rival declaration).
    This overlay's condition is the OPPOSITE ("nothing is declared anywhere"), and an ambiguous
    edit is proof a declaration now exists, even a broken one -- so this releases, handing the
    goal to `attach_at_pick`'s own, louder `REFUSED_AMBIGUOUS` handling on the very next pick.
    Two distinct `feature:*` LABELS -- `parse_labels` raises `AmbiguousUnit` on its own, independent
    of body text (which `_runner` has no seam to edit after construction)."""
    fl = _mod("feature_labels")
    with tempfile.TemporaryDirectory() as d:
        base, cfg, run, gh = _held_no_unit(d)
        run.labels.update({"feature:alpha", "feature:beta"})    # two rival declarations
        released = fl.resume_needs_unit(base, gh, cfg)
    assert released == ["42"]
    assert "sdlc:needs-unit" not in run.labels


def test_resume_needs_unit_is_ungated_from_the_pick_side_flag():
    """#2426, a real bug found live and fixed here: the sweep used to gate on
    `no_dangling_goal_enabled`, but the pick-side EXCLUSION (`overlay_labels`/
    `not_eligible_labels`) never reads that attribute at all -- so a goal held under
    `sdlc:needs-unit` while the feature was ON, followed by the feature being turned OFF, was
    stranded forever: still excluded from picking (the flag doesn't gate that), with no sweep left
    running that could ever clear it. This is the regression test for exactly that scenario: the
    feature is OFF right now (`_CONFIG`'s own unset `no_dangling_goal`), the issue still carries
    `sdlc:needs-unit` from when it was on, and a human has since declared a real unit -- the sweep
    must release it, exactly as it would with the feature still enabled."""
    fl = _mod("feature_labels")
    run = _runner(body=_DECLARES, labels={"sdlc:goal", "sdlc:needs-unit"})   # unit now declared
    gh = _source(run)                                     # _CONFIG: no_dangling_goal unset (OFF)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base)
        released = fl.resume_needs_unit(base, gh, cfg)
    assert released == ["42"]
    assert "sdlc:needs-unit" not in run.labels


def test_resume_needs_unit_still_pays_nothing_with_no_source_surface():
    """The COST guard that survives the fix: gated only on the source method surface, the SAME
    shape `resume_needs_label` (its sibling) has always used -- a source with none of the three
    methods (`LocalSource`, or a source predating this feature) still costs nothing, no
    `list_needs_unit` call at all."""
    fl = _mod("feature_labels")

    class NoSurface:
        pass

    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base)
        released = fl.resume_needs_unit(base, NoSurface(), cfg)
    assert released == []


def test_resume_needs_unit_records_and_comments():
    fl = _mod("feature_labels")
    with tempfile.TemporaryDirectory() as d:
        base, cfg, run, gh = _held_no_unit(d)
        run.labels.add("feature:declared-unit")
        fl.resume_needs_unit(base, gh, cfg)
        tokens = _tokens(base, "42")
    assert fl.NO_UNIT_RESUME_TOKEN in tokens, tokens
    resumed = [c[-1] for c in run.calls if c[:2] == ["issue", "comment"]]
    assert any("Auto-resumed" in c for c in resumed), resumed


# --------------------------------------------------- loop.py wiring: the sweep runs before the pick

def test_the_no_unit_sweep_runs_before_the_pick_and_frees_the_goal_in_the_same_call():
    """No cooldown, mirroring `test_the_unblock_sweep_runs_before_the_pick_and_frees_the_goal_in_
    the_same_call` for the sibling overlay. A human "declares a unit" here by attaching an existing
    `feature:*` LABEL directly (mutating the fake's live label set), the same mutation style that
    sibling test uses for its own resume trigger -- `_queue_runner` has no seam for editing an
    issue's BODY after construction, only its live labels."""
    lp, fl = _mod("loop"), _mod("feature_labels")
    run = _queue_runner({42: {"body": _NO_DECLARATION}})
    gh = _source_cfg(run, _no_unit_cfg())
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base)
        assert lp._next(base, gh, cfg) == ("DONE", None)       # #42 set aside, nothing else queued
        assert "sdlc:needs-unit" in run.live["42"]
        run.live["42"].add("feature:declared-unit")             # a human declares a unit
        kind, goal = lp._next(base, gh, cfg)
    assert (kind, goal) == ("goal", "42"), (kind, goal, run.live)
    assert "sdlc:needs-unit" not in run.live["42"]


# --------------------------------------------------- cross-module registration surfaces

def test_status_counts_a_held_no_unit_goal_as_needs_unit_not_as_pending():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "status", S.parent.parent.parent / "skills" / "sigma-status" / "scripts" / "status.py")
    status = importlib.util.module_from_spec(spec); spec.loader.exec_module(status)
    counts = {("sdlc:goal",): 3, ("sdlc:goal", "sdlc:in-progress"): 1,
              ("sdlc:goal", "sdlc:needs-unit"): 1}

    def run(args):
        labels = next((v[len("labels="):].split(",") for v in args
                       if isinstance(v, str) and v.startswith("labels=")), [])
        return json.dumps([{"number": n} for n in range(counts.get(tuple(labels), 0))])

    out = status._github_counts({"repo": "o/r"}, run)
    assert out["needs_unit"] == 1
    assert out["pending"] == 1                       # 3 goals - 1 in-progress - 1 held


def test_reconcile_treats_needs_unit_as_an_overlay_not_a_primary_state():
    rec = _mod("reconcile")
    gh = _mod("sources").GitHubSource({"discovery": {"source": "github", "github": {"repo": "o/r"}}},
                                      run=_runner())
    assert "sdlc:needs-unit" in rec._overlay_labels(gh)
    assert "sdlc:needs-unit" not in rec._primary_labels(gh, _CONFIG)


def test_promote_names_the_right_remedy_for_needs_unit_and_not_unpark():
    promote = _mod("promote")
    gh = _mod("sources").GitHubSource({"discovery": {"source": "github", "github": {"repo": "o/r"}}},
                                      run=_runner())
    msg = promote._refusal(gh, "42", {"sdlc:goal", "sdlc:needs-unit"}, False, False)
    assert msg and "sdlc:needs-unit" in msg and "unpark" not in msg.lower()
    assert "Declare one" in msg


def test_a_goal_carrying_needs_unit_and_parked_is_told_about_the_park():
    promote = _mod("promote")
    gh = _mod("sources").GitHubSource({"discovery": {"source": "github", "github": {"repo": "o/r"}}},
                                      run=_runner())
    both = promote._refusal(gh, "42", {"sdlc:goal", "sdlc:needs-unit", "sdlc:parked"}, False, False)
    assert "unpark" in both.lower() and "Declare one" not in both, both


def test_triage_does_not_report_a_held_no_unit_goal_as_ready_and_names_its_reason():
    triage = _mod("triage")
    gh_cfg = {"repo": "o/r"}
    held = {"number": 42, "title": "held", "state": "OPEN",
            "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:needs-unit"}]}
    healthy = {"number": 43, "title": "fine", "state": "OPEN", "labels": [{"name": "sdlc:goal"}]}
    ready = triage._bucket_enqueued([held, healthy], gh_cfg, gh_source=_source(_runner()))
    assert [i["number"] for i in ready["items"]] == [43], ready
    with tempfile.TemporaryDirectory() as d:
        base, _ = _sdlc(d)
        parked = triage._bucket_parked(base, [held, healthy], [], gh_cfg, gh_source=None)
    item = next(i for i in parked["items"] if i["number"] == 42)
    assert item["state"] == "needs-unit"
    assert item["reason_source"] == "label" and "unit" in item["reason"]


def test_the_survey_fallback_tuple_also_refuses_a_held_no_unit_goal():
    triage = _mod("triage")
    gh_cfg = {"repo": "o/r"}
    held = {"number": 42, "title": "held", "state": "OPEN",
            "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:needs-unit"}]}
    with_source = triage._bucket_enqueued([held], gh_cfg, gh_source=_source(_runner()))
    fallback = triage._bucket_enqueued([held], gh_cfg, gh_source=None)
    assert with_source["count"] == 0, with_source
    assert fallback["count"] == 0, fallback


def test_doctor_blocked_label_scan_never_flags_needs_unit_as_a_repo_convention():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "doctor", S.parent.parent.parent / "skills" / "sigma-doctor" / "scripts" / "doctor.py")
    doctor = importlib.util.module_from_spec(spec); spec.loader.exec_module(doctor)
    issues = [{"number": 42, "labels": [{"name": "sdlc:goal"}, {"name": "sdlc:needs-unit"}]}]
    run = lambda args: json.dumps(issues)
    flagged = doctor._blocked_label_scan({"repo": "o/r"}, run)
    assert flagged == [] or flagged is None, flagged


# =================================================================================================
# #2435: ensure_unit_tracking -- the side-job unit-registry ensure.
#
# `loop._ensure_unit_tracking`'s own cooldown/config-gating wrapper is covered in tests/test_loop.py
# (it needs `loop.py`'s own fixtures); these tests pin the real check-and-repair pass this module
# owns, called directly with a hand-rolled fake source -- the same style `_LiveJudgeSource` and
# `ExactlyTheDeclaredSurface` above already use for a narrow, duck-typed surface.

class _UnitTrackingSource:
    """The minimal surface `ensure_unit_tracking` needs: the config-resolved gate attribute, the
    REST read (`UNIT_TRACKING_METHODS`), and `_flag`'s own two methods -- a warning is posted
    through the SAME idempotent-comment primitive every other refusal in this module already uses,
    so a fake for it needs the same two methods `ExactlyTheDeclaredSurface` above defines."""

    def __init__(self, body="", labels=(), enabled=True, comments=(), fail_read=False):
        self.no_dangling_goal_enabled = enabled
        self._body = body
        self._labels = list(labels)
        self._fail_read = fail_read
        self.comments = list(comments)      # mutated by note() -- what `_flag` reads back next call

    def fetch_body_labels_rest(self, goal):
        if self._fail_read:
            raise RuntimeError("simulated transport failure")
        return {"body": self._body, "labels": [{"name": n} for n in self._labels]}

    def fetch_comments_strict(self, goal):
        return {"comments": [{"body": c} for c in self.comments]}

    def note(self, goal, text):
        self.comments.append(text)
        return True


_UNIT_TRACKING_CFG = {"discovery": {"source": "github", "github": {"repo": "o/r"}}}


def _tracking_entry(goals=(), repo="o/r", authorized=True):
    return {"title": "core", "owner": "me", "open": True,
            "repos": {repo: {"branch": "feature/core", "authorized": authorized,
                             "goals": list(goals)}}}


def test_ensure_unit_tracking_off_by_default_does_not_read_the_issue():
    """THE CONTROL. `no_dangling_goal_enabled` False -- degrades before the REST read, matching
    every sibling gate in this module."""
    fl = _mod("feature_labels")
    src = _UnitTrackingSource(body="Feature: core\n", labels=["feature:core"], enabled=False)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base, unit="core", entry=_tracking_entry(goals=[]))
        outcome = fl.ensure_unit_tracking(base, src, "42", _UNIT_TRACKING_CFG)
    assert outcome == fl.UNIT_TRACKING_OK
    assert src.comments == []


def test_ensure_unit_tracking_is_a_noop_with_no_registry_even_when_enabled():
    fl = _mod("feature_labels")
    src = _UnitTrackingSource(body="Feature: core\n", labels=["feature:core"])
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        assert not (pathlib.Path(base) / "features").is_dir()
        outcome = fl.ensure_unit_tracking(base, src, "42", _UNIT_TRACKING_CFG)
    assert outcome == fl.UNIT_TRACKING_OK
    assert src.comments == []


def test_ensure_unit_tracking_is_a_noop_with_no_configured_repo():
    """No `discovery.github.repo` -> no address to look `repos{}` up by -- degrades to `ok`, the
    same "an unanswered question is not an answer" rule `feature_sync.NO_REPO` states for the real
    pick-time sync, which still owns reporting that gap loudly."""
    fl = _mod("feature_labels")
    src = _UnitTrackingSource(body="Feature: core\n", labels=["feature:core"])
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base, unit="core", entry=_tracking_entry(goals=[]))
        outcome = fl.ensure_unit_tracking(base, src, "42", {"discovery": {"source": "github"}})
    assert outcome == fl.UNIT_TRACKING_OK
    assert src.comments == []


def test_ensure_unit_tracking_is_a_noop_on_a_source_missing_the_rest_method():
    """`LocalSource`, or any source predating this slice, has no `fetch_body_labels_rest` at all --
    degrades cleanly instead of raising inside a goal-scoped verb."""
    fl = _mod("feature_labels")

    class NoRestMethod:
        no_dangling_goal_enabled = True

    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base, unit="core", entry=_tracking_entry(goals=[]))
        outcome = fl.ensure_unit_tracking(base, NoRestMethod(), "42", _UNIT_TRACKING_CFG)
    assert outcome == fl.UNIT_TRACKING_OK


def test_ensure_unit_tracking_is_a_noop_when_the_issue_read_fails():
    """A transient transport failure is not evidence of a gap -- degrades to `ok`, silently, exactly
    like `attach_at_pick`'s own `UNREADABLE` direction."""
    fl = _mod("feature_labels")
    src = _UnitTrackingSource(fail_read=True)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base, unit="core", entry=_tracking_entry(goals=[]))
        outcome = fl.ensure_unit_tracking(base, src, "42", _UNIT_TRACKING_CFG)
    assert outcome == fl.UNIT_TRACKING_OK
    assert src.comments == []


def test_ensure_unit_tracking_is_a_noop_when_the_issue_declares_no_unit():
    """Not this function's job -- `_handle_no_unit_at_pick`'s, at pick time."""
    fl = _mod("feature_labels")
    src = _UnitTrackingSource(body=_NO_DECLARATION, labels=["sdlc:goal"])
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base, unit="core", entry=_tracking_entry(goals=[]))
        outcome = fl.ensure_unit_tracking(base, src, "42", _UNIT_TRACKING_CFG)
    assert outcome == fl.UNIT_TRACKING_OK
    assert src.comments == []


def test_ensure_unit_tracking_is_a_noop_on_a_self_contradicting_issue():
    """`AmbiguousUnit` -- already flagged elsewhere (`attach_at_pick`'s own `REFUSED_AMBIGUOUS`);
    this function must not pile a second, unrelated warning onto an issue a human is already asked
    to fix."""
    fl = _mod("feature_labels")
    src = _UnitTrackingSource(body=_RIVALS, labels=[])
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base, unit="core", entry=_tracking_entry(goals=[]))
        outcome = fl.ensure_unit_tracking(base, src, "42", _UNIT_TRACKING_CFG)
    assert outcome == fl.UNIT_TRACKING_OK
    assert src.comments == []


def test_ensure_unit_tracking_is_ok_when_the_goal_is_already_recorded():
    """THE STEADY STATE -- the common case, and it must cost no write."""
    fl = _mod("feature_labels")
    src = _UnitTrackingSource(body="", labels=["feature:core"])
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base, unit="core", entry=_tracking_entry(goals=[42]))
        outcome = fl.ensure_unit_tracking(base, src, "42", _UNIT_TRACKING_CFG)
        entry_after = _mod("feature_registry").read(pathlib.Path(base) / "features")["core"]
    assert outcome == fl.UNIT_TRACKING_OK
    assert entry_after["repos"]["o/r"]["goals"] == [42]     # unchanged
    assert src.comments == []


def test_ensure_unit_tracking_repairs_a_missing_goal():
    """THE REPAIR -- the exact gap #2435 exists to close: the label says `core`, the registry's
    `repos["o/r"].goals` does not carry this goal yet, and this IS authorized to append it."""
    fl = _mod("feature_labels")
    src = _UnitTrackingSource(body="", labels=["feature:core"])
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base, unit="core", entry=_tracking_entry(goals=[7]))
        outcome = fl.ensure_unit_tracking(base, src, "42", cfg)
        entry_after = _mod("feature_registry").read(pathlib.Path(base) / "features")["core"]
        ledger_notes = _tokens(base, "42")
    assert outcome == fl.UNIT_TRACKING_REPAIRED
    assert entry_after["repos"]["o/r"]["goals"] == [7, 42]
    assert src.comments == []                                # a REPAIR is quiet, not a warning
    assert any(t == "unit-tracking-repaired" for t in ledger_notes), ledger_notes


def test_ensure_unit_tracking_warns_when_the_unit_is_not_registered_at_all():
    """The label names a unit `.sdlc/features/` has never heard of -- cannot self-heal (creating a
    unit is not this function's call to make), so it warns instead."""
    fl = _mod("feature_labels")
    src = _UnitTrackingSource(body="", labels=["feature:ghost"])
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base, unit="core", entry=_tracking_entry(goals=[]))   # "ghost" is not registered
        outcome = fl.ensure_unit_tracking(base, src, "42", cfg)
        ledger_notes = _tokens(base, "42")
    assert outcome == fl.UNIT_TRACKING_WARNED
    assert len(src.comments) == 1 and fl.UNIT_TRACKING_MARKER in src.comments[0], src.comments
    assert any(t == "unit-tracking-warned" for t in ledger_notes), ledger_notes


def test_ensure_unit_tracking_warns_and_never_widens_an_unauthorized_repo():
    """THE ROOT CAUSE FOUND LIVE ON 2026-09-11, reproduced as a regression test: a unit registered
    with `repos: {}` (or simply missing this repo) must never be silently widened to include it --
    `_record_goal`'s own one-directional rule, respected here too. Warns; writes nothing."""
    fl = _mod("feature_labels")
    src = _UnitTrackingSource(body="", labels=["feature:core"])
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base, unit="core", entry={"title": "core", "owner": "me", "open": True, "repos": {}})
        outcome = fl.ensure_unit_tracking(base, src, "42", _UNIT_TRACKING_CFG)
        entry_after = _mod("feature_registry").read(pathlib.Path(base) / "features")["core"]
    assert outcome == fl.UNIT_TRACKING_WARNED
    assert entry_after["repos"] == {}                        # never widened
    assert len(src.comments) == 1 and fl.UNIT_TRACKING_MARKER in src.comments[0], src.comments


def test_ensure_unit_tracking_warning_is_posted_at_most_once_per_issue():
    """Idempotent against the issue's own timeline, the same contract `_flag` already gives every
    other caller in this module -- a second pass over the same still-broken goal must not re-post."""
    fl = _mod("feature_labels")
    src = _UnitTrackingSource(body="", labels=["feature:ghost"])
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base, unit="core", entry=_tracking_entry(goals=[]))
        first = fl.ensure_unit_tracking(base, src, "42", _UNIT_TRACKING_CFG)
        second = fl.ensure_unit_tracking(base, src, "42", _UNIT_TRACKING_CFG)
    assert first == fl.UNIT_TRACKING_WARNED and second == fl.UNIT_TRACKING_WARNED
    assert len(src.comments) == 1, src.comments


def test_ensure_unit_tracking_never_raises_on_an_unexpected_exception(monkeypatch):
    """The whole-function guard: whatever goes wrong inside, the caller gets `warned` back, never an
    exception -- `loop._ensure_unit_tracking`'s own fail-open wrapper depends on this being total,
    not merely likely. Exercised by making the REPAIR attempt itself explode -- a realistic failure
    (`feature_sync.amend` raising something its own internals did not resolve to a report), and one
    that lands OUTSIDE every inner try/except this function already has."""
    fl = _mod("feature_labels")
    src = _UnitTrackingSource(body="", labels=["feature:core"])

    class ExplodingSync:
        @staticmethod
        def amend(sdlc_dir, name, mutate):
            raise RuntimeError("boom")

    monkeypatch.setattr(fl, "_feature_sync", lambda: ExplodingSync)
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base, unit="core", entry=_tracking_entry(goals=[7]))
        outcome = fl.ensure_unit_tracking(base, src, "42", cfg)
    assert outcome == fl.UNIT_TRACKING_WARNED


def test_ensure_unit_tracking_case_insensitive_unit_match():
    """A label's casing and the registry's own spelling need not agree -- `_resolve_registry_key`'s
    own contract, mirrored from `sources.GitHubSource._unit_priority_rank`."""
    fl = _mod("feature_labels")
    src = _UnitTrackingSource(body="", labels=["feature:Core"])
    with tempfile.TemporaryDirectory() as d:
        base, cfg = _sdlc(d)
        _adopt(base, unit="core", entry=_tracking_entry(goals=[]))
        outcome = fl.ensure_unit_tracking(base, src, "42", _UNIT_TRACKING_CFG)
    assert outcome == fl.UNIT_TRACKING_REPAIRED
    assert src.comments == []
