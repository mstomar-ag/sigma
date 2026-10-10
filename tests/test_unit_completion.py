"""#1478 -- L5 policy: Sigma SURFACES that a unit of work looks finished, and a HUMAN decides.

THE ONE PROPERTY EVERY TEST HERE EXISTS TO DEFEND: "no open issues" is not "the unit is done". A
backlog can empty for a minute while the work is plainly incomplete, so the completion of a unit --
the `feature/<unit>` -> integration-branch pull request, and its merge -- is never Sigma's call.

The assertions are therefore written against the RECORDED gh CALL LIST wherever the claim is about
something NOT happening. Asserting on the returned report instead would pass just as happily for a
function that opened a pull request and then reported that it had not: the outcome field is the
thing under test's own account of itself, and the call list is evidence.

`merge` is asserted absent in EVERY mode, on the same call list, for the same reason. #1474 left
"then they merge back to back" deliberately without an owner (see `work.sibling_gate`'s docstring),
and this goal is explicitly told not to give it one by accident.
"""
import importlib.util
import json
import pathlib

import rest_merge_support
from journal_events import journal_events

_SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


uc = _load("unit_completion")
work = _load("work")
state = _load("state")
cross_repo = _load("cross_repo")
feature_registry = _load("feature_registry")

UNIT = "int-contract"
BRANCH = "feature/int-contract"
NOSLEEP = lambda _: None                                          # noqa: E731 - one-liner test stub


def _config(mode=None, base="main", repo="acme/app", **over):
    cfg = {"work": {"enabled": True, "base": base},
           "discovery": {"source": "github", "github": {"repo": repo}}}
    if mode is not None:
        cfg["work"]["unit_completion"] = mode
    cfg["work"].update(over)
    return cfg


def _runner(handlers):
    """First substring match wins; anything unmatched returns "". Same contract as
    `work._run` -- `(cwd, argv) -> stdout`, an Exception response raises."""
    calls = []

    def run(cwd, argv):
        line = " ".join(str(a) for a in argv)
        calls.append(line)
        for token, resp in handlers:
            if token in line:
                if isinstance(resp, Exception):
                    raise resp
                return resp(line) if callable(resp) else resp
        return ""

    run.calls = calls
    return run


def _sdlc(tmp_path, config=None, ledger_on=False):
    d = tmp_path / ".sdlc"
    (d / "state").mkdir(parents=True)
    cfg = dict(config or _config())
    if ledger_on:
        cfg["ledger"] = {"enabled": True, "actor": "tester"}
    (d / "config.json").write_text(json.dumps(cfg))
    state.start_run(str(d))
    return str(d)


def _started(sdlc_dir, goal="0001-x.md", base=BRANCH):
    """A work record with a base -- the ONE place this feature learns which unit a goal is on. It is
    read, never re-resolved: `start()` already asked the issue exactly once (#1467)."""
    wt = pathlib.Path(sdlc_dir).parent / ".sdlc" / "work" / "0001-x"
    wt.mkdir(parents=True, exist_ok=True)
    work._save(sdlc_dir, goal, {"worktree": str(wt), "branch": "sdlc/0001-x", "base": base,
                                "remote": "origin", "pr": "7"})
    return goal


def _registry(sdlc_dir, unit=UNIT, owner="@unit-owner", repos=None):
    d = feature_registry.registry_dir(sdlc_dir)
    feature_registry.write_unit(d, unit, {"owner": owner, "repos": repos if repos is not None else {
        "acme/app": {"branch": BRANCH}}})
    return d


def _landing(sdlc_dir, goal="0001-x.md", repos=("acme/app", "acme/api"), **over):
    """#1472's pick-time landing decision, written to the path that module owns."""
    record = {"schema": cross_repo.RECORD_SCHEMA, "goal": goal, "unit": UNIT,
              "outcome": cross_repo.TIER_1, "tier": 1, "cross_repo": True,
              "repos": {r: {"verdict": cross_repo.GRANTED, "reason": None, "detail": "",
                            "owner": None} for r in repos},
              "denied": [], "unknown": [], "why": "", "at": "2026-08-22T00:00:00Z"}
    record.update(over)
    path = cross_repo.decision_path(sdlc_dir, goal)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    return record


#: The REST paths this module reads. Written out as the tests' own constants rather than as loose
#: substrings so a transport change cannot pass unnoticed: #1209's doctrine is that these are REST
#: (`gh api repos/<slug>/...`), never the GraphQL-backed `gh issue list` / `gh pr list` /
#: `gh pr create`, and a test keyed on "issue list" would go on matching either.
OPEN_PATH = "repos/acme/app/issues?labels=feature%3Aint-contract&state=open"
CLOSED_PATH = "repos/acme/app/issues?labels=feature%3Aint-contract&state=closed"
PULLS_PATH = "repos/acme/app/pulls?head=acme:feature/int-contract&state=all"
#: The CREATE, told apart from the READ on the same path by the fields only a create carries --
#: `repos/<slug>/pulls` is both endpoints, and a token that matched the prefix alone would count
#: every idempotence read as a pull-request creation.
CREATE_PATH = "repos/acme/app/pulls -f title="


#: The membership read #1570 added -- the OPEN backlog, unfiltered, so the issues that declare this
#: unit in their BODY and have not been picked yet (and therefore carry no label) are counted. It is
#: keyed on the page-number prefix rather than a whole path because the read WALKS pages, and a
#: fixture that could only answer page 1 could not express the one thing the cap exists for.
#: Repo-agnostic on purpose: two tests below drive the `{owner}/{repo}` placeholder path.
BACKLOG_TOKEN = "/issues?state=open&per_page=100&page="


def _declared(number, unit=UNIT, labelled=False, body=None, pull_request=False):
    """One row as `BACKLOG_JQ` projects it: `_row` plus the two fields `features.read` takes.

    `labelled=False` is the DEFAULT because that is the case the label read structurally cannot
    see -- an issue that declares the unit in its body and has not been picked, which is the whole
    of #1570."""
    return {"number": number, "title": "t%s" % number, "pull_request": pull_request,
            "body": ("Feature: %s\n" % unit) if body is None else body,
            "labels": [{"name": "feature:%s" % unit}] if labelled else []}


def _backlog(*pages):
    """The paged open-backlog read, served page by page. No pages == an empty backlog.

    A CALLABLE, not a canned string, because the page number is in the path: a fixture keyed on one
    token that returned one page would answer page 2 with page 1's contents forever, and the cap
    test would then never terminate for the right reason."""
    def serve(line):
        number = int(line.rsplit("page=", 1)[1].split()[0].split("&")[0])
        return json.dumps(list(pages[number - 1])) if 1 <= number <= len(pages) else "[]"
    return [(BACKLOG_TOKEN, serve)]


def _row(number, pull_request=False):
    """One row as `ISSUE_JQ` projects it. `pull_request` is passed THROUGH by the projection and
    filtered in Python, which is the whole reason these tests can see the defect that shipped:
    `per_page` bounds the page on the SERVER and the filter runs on the CLIENT, so a fixture has to
    be able to say "the server returned five rows, three of which are pull requests"."""
    return {"number": number, "title": "t%s" % number, "pull_request": pull_request}


def _page(*rows):
    return json.dumps(list(rows))


def _issues(open_numbers=(), closed_numbers=(12, 13), open_prs=(), closed_prs=(), backlog=()):
    """The one or two issue reads this feature makes: OPEN first (the verdict), CLOSED only when the
    first said none (the tally a human reads).

    `open_prs` / `closed_prs` are pull-request rows the SERVER returns on the same page — the thing
    no earlier fixture could express, and the thing the page cap actually bounds.

    `backlog` is the unfiltered open-issue read #1570 added, as a tuple of PAGES. It defaults to
    empty — no unpicked issue declares this unit — which is what makes an otherwise-complete unit
    still read as complete."""
    return [(OPEN_PATH, _page(*([_row(n) for n in open_numbers] +
                                [_row(n, True) for n in open_prs]))),
            (CLOSED_PATH, _page(*([_row(n) for n in closed_numbers] +
                                  [_row(n, True) for n in closed_prs])))] + _backlog(*backlog)


def _no_landing_pr():
    """Every PR on `feature/int-contract` in OUR repo -- there are none at all."""
    return [(PULLS_PATH, "[]")]


def _landing_pr(state="open", draft=True, merged=False, number=41):
    return [(PULLS_PATH, json.dumps([{
        "number": number, "url": "https://github.com/acme/app/pull/%d" % number,
        "draft": draft, "state": state,
        "merged_at": "2026-08-22T00:00:00Z" if merged else None}]))]


def _created(url="https://github.com/acme/app/pull/99"):
    return [(CREATE_PATH, url)]


def _complete(tmp_path, config=None, goal="0001-x.md", landing=True):
    """An adopting repo, one unit, one goal on it -- and, by default, the pick-time landing decision
    #1472 records for EVERY goal it picks once `.sdlc/features/` exists.

    Writing that record is not decoration. Without it `sibling_gate` correctly refuses ("the
    pick-time check never ran"), so a fixture that omitted it would put a refusal into every single
    test here and quietly model a broken repo as the normal one."""
    d = _sdlc(tmp_path, config or _config())
    _started(d, goal)
    _registry(d)
    if landing:
        _landing(d, goal, outcome=cross_repo.NOT_CROSS_REPO, tier=None, cross_repo=False)
    return d, goal


def _pr_creates(calls):
    """Any call that OPENS a pull request, by either transport. Both spellings are checked, so
    moving to REST cannot make a "nothing was opened" assertion pass by going unrecognised."""
    return [c for c in calls if "pr create" in c or CREATE_PATH in c]


def _merges(calls):
    return rest_merge_support.rest_merges(calls)                 # CLI, git, and REST `PUT pulls/<n>/merge` (#935)


# --- the mode itself -----------------------------------------------------------------------------

def test_the_default_mode_is_surface():
    """The issue's own words: configurable, defaulting to human-only. Absent key, absent `work`
    block, and an empty config all have to agree."""
    assert uc.mode({}) == uc.SURFACE
    assert uc.mode({"work": {}}) == uc.SURFACE
    assert uc.mode(_config()) == uc.SURFACE


def test_only_the_exact_literal_selects_draft_mode():
    """A typo must land on the DEFAULT, never on the mode that opens a pull request. This is the
    `_blocking_priority_override` rule, pointed at the setting where being wrong costs something:
    every value that is not exactly `draft-pr` or `off` surfaces and stops."""
    assert uc.mode(_config(mode="draft-pr")) == uc.DRAFT_PR
    for typo in ("draft", "draft_pr", "DRAFT-PR", "Draft-PR", "true", True, 1, None, [], "yes"):
        assert uc.mode(_config(mode=typo)) == uc.SURFACE, typo


def test_off_is_reachable_and_is_the_only_way_to_silence_it():
    assert uc.mode(_config(mode="off")) == uc.OFF


# --- inert: a project that never adopted the branching model pays nothing ------------------------

def test_a_project_with_no_feature_registry_makes_no_call_at_all(tmp_path):
    """Every project on earth today. Zero `gh`, zero output, zero new failure mode."""
    d = _sdlc(tmp_path)
    goal = _started(d)
    run = _runner([])
    report = uc.signal(d, _config(), goal, run=run, sleep=NOSLEEP)
    assert report["outcome"] == uc.NOT_ADOPTED
    assert run.calls == []


def test_the_unit_is_read_off_the_record_and_a_sub_branch_is_not_one():
    """`unit_of` is the whole of this feature's unit resolution: `start()` already asked the issue
    once (#1467) and persisted the answer, so nothing here reads an issue.

    A sub-unit branch (`feature/<name>/<sub>`, which the epic allows to depth 3) is NOT its parent
    unit: `is_unit_name` rejects a `/`, the registry is keyed by unit name, and answering about the
    parent when the goal is on a child would be a guess about which body of work just finished."""
    assert uc.unit_of({"base": BRANCH}) == UNIT
    assert uc.unit_of({"base": "feature/int-contract/retry"}) is None
    assert uc.unit_of({"base": "main"}) is None
    assert uc.unit_of({"base": "feature/"}) is None
    assert uc.unit_of({"base": ["feature/x"]}) is None
    assert uc.unit_of({}) is None
    assert uc.unit_of(None) is None


def test_a_goal_that_declares_no_unit_makes_no_call_at_all(tmp_path):
    """An adopting repo still runs ordinary goals. `base: main` is not a unit, so there is no unit
    whose completion could be in question."""
    d = _sdlc(tmp_path)
    goal = _started(d, base="main")
    _registry(d)
    run = _runner([])
    report = uc.signal(d, _config(), goal, run=run, sleep=NOSLEEP)
    assert report["outcome"] == uc.NO_UNIT
    assert run.calls == []


def test_mode_off_makes_no_call_even_on_a_complete_unit(tmp_path):
    d, goal = _complete(tmp_path, _config(mode="off"))
    run = _runner(_issues())
    report = uc.signal(d, _config(mode="off"), goal, run=run, sleep=NOSLEEP)
    assert report["outcome"] == uc.DISABLED
    assert run.calls == []


# --- THE ACCEPTANCE CRITERION: the default never opens a PR and never merges ---------------------

def test_the_default_configuration_surfaces_and_opens_nothing(tmp_path, capsys):
    """`Done when: the default config surfaces the signal and never opens a feature ->
    integration-branch PR`.

    Asserted on the CALL LIST, not on the outcome field: a function that opened the PR and then
    reported `surfaced` would satisfy an outcome-only assertion perfectly."""
    cfg = _config()
    d, goal = _complete(tmp_path, cfg)
    run = _runner(_issues())
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["outcome"] == uc.COMPLETE
    assert report["action"] == uc.SURFACED
    assert report["open"] == 0 and report["closed"] == 2
    assert _pr_creates(run.calls) == []
    assert _merges(run.calls) == []
    assert not any("--draft" in c for c in run.calls)
    assert UNIT in capsys.readouterr().err


def test_the_default_opens_nothing_even_when_opening_would_have_SUCCEEDED(tmp_path):
    """The sharper form of the criterion above, and the one that is not blind to its own case.

    The previous test proves no `pr create` appears — but its runner has no `pr list` or `pr create`
    handler, so a default that DID try to open one would fail on the read and still record no
    create. Every handler the draft path needs is installed here, so the only reason nothing is
    opened is that the default mode never asks. Same reasoning applied to the merge: `pr merge` is
    handled too, so a mode that reached for it would get an answer rather than an error."""
    cfg = _config()
    d, goal = _complete(tmp_path, cfg)
    run = _runner(_issues() + _no_landing_pr() + _created() +
                  [("pr merge", "merged"), ("pr ready", "ready"), ("-X PUT", "merged")])
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["outcome"] == uc.COMPLETE and report["action"] == uc.SURFACED
    assert report["pr"] is None and report["parked"] is False
    # The whole evidence: the only calls made were the two reads.
    assert [c.split(" --jq")[0] for c in run.calls] == [
        "gh api " + OPEN_PATH + "&per_page=100",
        "gh api repos/acme/app" + BACKLOG_TOKEN + "1",      # #1570: the unpicked backlog
        "gh api " + CLOSED_PATH + "&per_page=100"]


def test_neither_mode_ever_merges_anything(tmp_path):
    """`Done when: neither mode ever merges a feature branch.` The one assertion this whole goal is
    about, made against the evidence rather than the account.

    THE POSITIVE ASSERTIONS ARE LOAD-BEARING, and they are here because the first draft of this test
    did not have them. "No merge appears in the call list" is trivially true of a pass that made no
    calls at all -- and while this file was being written, the skeleton under test made none, so the
    test passed green against a module that did nothing whatsoever. A test written to pin a case has
    to be shown to be able to SEE that case: the outcome and the tally prove the pass really ran and
    really reached the point where a merge was the wrong thing to do."""
    for mode in (uc.SURFACE, uc.DRAFT_PR):
        cfg = _config(mode=mode)
        d, goal = _complete(tmp_path / mode, cfg)
        run = _runner(_issues() + _no_landing_pr() + _created())
        report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
        assert report["outcome"] == uc.COMPLETE, mode        # the pass really got there
        assert report["open"] == 0 and report["closed"] == 2, mode
        assert _merges(run.calls) == [], mode
        assert not any("--admin" in c or "--auto" in c for c in run.calls), mode


# --- draft mode: opens a DRAFT, and parks it ------------------------------------------------------

def test_draft_mode_opens_a_draft_and_parks_it_for_a_human(tmp_path):
    """`Done when: draft mode opens it as a draft and parks it for a human.`

    `--draft` is asserted on the actual argv. A draft pull request cannot be merged by GitHub at
    all, which is what makes the draft ITSELF the park rather than a label that means one."""
    cfg = _config(mode="draft-pr")
    d, goal = _complete(tmp_path, cfg)
    run = _runner(_issues() + _no_landing_pr() + _created())
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["action"] == uc.DRAFTED
    assert report["parked"] is True
    assert report["pr"] == "https://github.com/acme/app/pull/99"
    created = _pr_creates(run.calls)
    assert len(created) == 1
    # `-F draft=true`, never `-f`: gh's `-F` gives the literal `true` its JSON BOOLEAN type, while
    # `-f` would send the string "true" for a field the REST API specifies as a boolean. Asserted on
    # the flag, because that one character is the whole difference between a draft and a ready PR.
    assert "-F draft=true" in created[0]
    assert "-f head=" + BRANCH in created[0]
    assert "-f base=main" in created[0]
    assert _merges(run.calls) == []


def test_draft_mode_refuses_to_guess_the_integration_branch(tmp_path):
    """`work.base` empty means "whichever branch the loop was started on" -- which is not an answer
    a pull request's base can be guessed from. Refusing and surfacing is the fail-closed direction;
    opening a PR against whatever branch happens to be default is not."""
    cfg = _config(mode="draft-pr", base="")
    d, goal = _complete(tmp_path, cfg)
    run = _runner(_issues() + _no_landing_pr())
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["action"] == uc.DRAFT_REFUSED
    assert _pr_creates(run.calls) == []
    assert "work.base" in report["why"]


def test_draft_mode_does_not_open_a_second_pull_request(tmp_path):
    """Idempotence, and it is not cosmetic: this fires on the goal that closes a unit, and a re-run
    (a re-picked goal, a resumed loop) must not stack duplicate landing PRs on one branch."""
    cfg = _config(mode="draft-pr")
    d, goal = _complete(tmp_path, cfg)
    run = _runner(_issues() + _landing_pr(state="open", draft=True))
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["action"] == uc.DRAFT_EXISTS
    assert report["pr"] == "https://github.com/acme/app/pull/41"
    assert report["parked"] is True                          # that one IS a draft
    assert _pr_creates(run.calls) == []


def test_a_landing_pr_a_HUMAN_CLOSED_is_never_re_raised(tmp_path):
    """THE REVIEW'S BLOCKING FINDING, pinned. A closed landing PR is a person saying "not yet"
    about this exact unit. Reading only the OPEN pull requests could not see it, so the next goal to
    finish on the unit opened a fresh draft straight over that decision — in the one module whose
    whole subject is that a human decides.

    Written so it cannot pass vacuously: the create handler IS installed, so a `state=all` read that
    ignored the closed row would open a real pull request and this test would see it."""
    cfg = _config(mode="draft-pr")
    d, goal = _complete(tmp_path, cfg)
    run = _runner(_issues() + _landing_pr(state="closed", draft=False) + _created())
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["outcome"] == uc.COMPLETE          # the signal still reaches the human
    assert report["action"] == uc.DRAFT_DECLINED
    assert report["pr"] == "https://github.com/acme/app/pull/41"
    assert report["parked"] is False                 # nothing is waiting on anyone here
    assert "closed without merging" in report["why"]
    assert _pr_creates(run.calls) == []
    assert _merges(run.calls) == []


def test_the_read_that_sees_a_closed_landing_pr_asks_for_state_all(tmp_path):
    """The mechanism behind the test above, asserted directly on the argv — because `state=all` is
    the single character-level difference between seeing a human's decision and overwriting it, and
    an outcome assertion alone would keep passing if the read silently narrowed again."""
    cfg = _config(mode="draft-pr")
    d, goal = _complete(tmp_path, cfg)
    run = _runner(_issues() + _no_landing_pr() + _created())
    uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    reads = [c for c in run.calls if "/pulls?" in c]
    assert len(reads) == 1 and "state=all" in reads[0]
    assert "state=open" not in reads[0]


def test_a_landing_pr_that_already_MERGED_is_not_raised_again_either(tmp_path):
    """The other half of `state=all`. A merged landing PR means the unit already landed, so there is
    nothing left to raise — and the reason says `merged` rather than `closed`, because the two are
    the same decision to a consumer and completely different news to a person."""
    cfg = _config(mode="draft-pr")
    d, goal = _complete(tmp_path, cfg)
    run = _runner(_issues() + _landing_pr(state="closed", draft=False, merged=True) + _created())
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["action"] == uc.DRAFT_DECLINED
    assert "merged" in report["why"] and "closed without merging" not in report["why"]
    assert _pr_creates(run.calls) == []


def test_a_merged_landing_pr_outranks_a_merely_closed_one(tmp_path):
    """A head can carry both — an attempt somebody abandoned and the one that actually LANDED. The
    two were split because they are different news to a person, so which one a person hears must not
    be decided by row order on an endpoint this module sets no `sort` or `direction` on.

    Both orderings are driven, because "take the first ended row" passes one of them by luck."""
    cfg = _config(mode="draft-pr")
    for order, name in (((50, 41), "closed-first"), ((41, 50), "merged-first")):
        d, goal = _complete(tmp_path / name, cfg)
        by_number = {50: {"state": "closed", "merged_at": None},
                     41: {"state": "closed", "merged_at": "2026-08-01T00:00:00Z"}}
        rows = json.dumps([{"number": n, "url": "https://github.com/acme/app/pull/%d" % n,
                            "draft": False, **by_number[n]} for n in order])
        run = _runner(_issues() + [(PULLS_PATH, rows)] + _created())
        report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
        assert report["action"] == uc.DRAFT_DECLINED, name
        assert report["pr"].endswith("/41"), name            # the one that landed
        assert "merged" in report["why"], name
        assert "closed without merging" not in report["why"], name
        assert _pr_creates(run.calls) == [], name


def test_the_report_carries_no_note_field(tmp_path):
    """`_report`'s docstring says in capitals that there is no `note` field, and an earlier revision
    set one three lines of prose away — a function contradicting its own docstring. Nothing consumes
    it: `loop._record` discards the report and the CLI dumps the whole dict."""
    cfg = _config()
    d, goal = _complete(tmp_path, cfg)
    for run, expected in ((_runner(_issues()), uc.COMPLETE),
                          (_runner(_issues(open_numbers=(1,))), uc.INCOMPLETE)):
        report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
        assert report["outcome"] == expected
        assert "note" not in report


def test_an_open_pull_request_outranks_a_closed_one_on_the_same_head(tmp_path):
    """A head can carry both — a landing PR closed last week and a fresh one open now. The live
    state is the open one whatever came before it, so the ordering is not arbitrary."""
    cfg = _config(mode="draft-pr")
    d, goal = _complete(tmp_path, cfg)
    both = json.dumps([
        {"number": 41, "url": "https://github.com/acme/app/pull/41", "draft": False,
         "state": "closed", "merged_at": None},
        {"number": 42, "url": "https://github.com/acme/app/pull/42", "draft": True,
         "state": "open", "merged_at": None}])
    run = _runner(_issues() + [(PULLS_PATH, both)] + _created())
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["action"] == uc.DRAFT_EXISTS
    assert report["pr"].endswith("/42") and report["parked"] is True
    assert _pr_creates(run.calls) == []


# --- cap-then-filter: the defect the second review found, and the harness that can see it ---------
#
# `per_page` is applied by the SERVER; a `--jq` filter runs on the CLIENT. So a cap bounds
# issues-AND-PULL-REQUESTS and any client-side filter then shrinks an already-truncated page. The
# earlier revision filtered inside the `--jq` string, which no test in this file could execute: the
# fake runner returns canned stdout and never runs jq, so the harness was structurally blind to the
# entire class. The filter now runs in `_issue_rows`, in Python, and `_row(n, pull_request=True)`
# lets a fixture state exactly what the SERVER returned as distinct from what survived.

def test_a_page_of_pull_requests_is_not_an_empty_backlog(tmp_path):
    """THE BLOCKING DEFECT, pinned. A full page whose every row is a pull request left no issues
    behind the filter, and the module read that empty remainder as "no open issues" — `complete`,
    and in `draft-pr` a landing pull request opened for a unit still being worked. On the DEFAULT
    path, in the UNSAFE direction, which is the one thing this module exists to forbid.

    The fixture says what no earlier one could: the server returned a FULL page, and every row on it
    was a pull request. Nothing is claimed. Written in `draft-pr` with the create handler installed,
    so a module that concluded `complete` here would open a real pull request and be seen doing it.
    """
    cfg = _config(mode="draft-pr")
    d, goal = _complete(tmp_path, cfg)
    full_page_of_prs = _page(*[_row(n, True) for n in range(1, uc.PAGE_LIMIT + 1)])
    # The CLOSED handler is installed too, so a module that read the empty remainder as "no open
    # issues" would go on to a healthy tally, reach `complete`, and open a real draft — the assertion
    # below would then be watching the failure happen rather than passing on a stalled pass.
    run = _runner([(OPEN_PATH, full_page_of_prs),
                   (CLOSED_PATH, _page(_row(12), _row(13)))] + _no_landing_pr() + _created())
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["outcome"] == uc.INCONCLUSIVE
    assert report["outcome"] in uc.CLAIMS_NOTHING
    assert report["action"] is None
    assert _pr_creates(run.calls) == []
    assert "not knowable from one page" in report["why"]
    # and the closed read was never even reached — nothing is claimed, so nothing is tallied
    assert not any("state=closed" in c for c in run.calls)


def test_a_short_page_of_pull_requests_IS_an_answer(tmp_path):
    """The other side of the same guard, and the reason it is `page_full` and not "any pull
    requests". A page the server did NOT fill is everything there is, so a remainder of zero issues
    behind two pull requests is a real, complete answer — refusing there would make the signal
    unreachable on every unit whose label a pull request happens to carry."""
    cfg = _config()
    d, goal = _complete(tmp_path, cfg)
    run = _runner([(OPEN_PATH, _page(_row(7, True), _row(8, True))),
                   (CLOSED_PATH, _page(_row(12), _row(13)))] + _backlog())
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["outcome"] == uc.COMPLETE
    assert report["open"] == 0 and report["open_truncated"] is False


def test_the_open_count_counts_ISSUES_not_rows(tmp_path):
    """The measured example from the review: a full page of 5 rows, 3 of them pull requests, was
    reported as "2 open issues" with `open_truncated: false` — a truncated page rendered as an exact
    measurement. The count must describe issues and the flag must describe issues."""
    cfg = _config()
    d, goal = _complete(tmp_path, cfg)
    rows = [_row(n, True) for n in (91, 92, 93)] + [_row(1), _row(2)]
    rows += [_row(n, True) for n in range(100, 100 + uc.PAGE_LIMIT - len(rows))]
    assert len(rows) == uc.PAGE_LIMIT                       # the SERVER filled the page
    run = _runner([(OPEN_PATH, _page(*rows))])
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["outcome"] == uc.INCOMPLETE               # two issues survived: definitive
    assert report["open"] == 2                              # ISSUES, not the 100 rows
    assert report["open_truncated"] is True                 # ...and there may be more behind them
    assert "2+ open issues" in report["why"]


def test_the_closed_tally_counts_ISSUES_not_rows(tmp_path):
    """Same arithmetic on the half that only feeds a courtesy number. A full page holding closed
    pull requests reported an exact-looking count; it now says `+`, and the number it renders is a
    count of issues."""
    cfg = _config()
    d, goal = _complete(tmp_path, cfg)
    rows = [_row(n) for n in range(1, 61)] + [_row(n, True) for n in range(200, 240)]
    assert len(rows) == uc.PAGE_LIMIT
    run = _runner([(OPEN_PATH, "[]"), (CLOSED_PATH, _page(*rows))] + _backlog())
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["outcome"] == uc.COMPLETE
    assert report["closed"] == 60 and report["truncated"] is True
    assert "60+ closed" in report["why"]


def test_a_full_closed_page_of_pull_requests_is_not_an_empty_label(tmp_path):
    """The `empty label` verdict has the same hazard as the `complete` one: a page the server filled
    entirely with closed pull requests is not evidence that a unit has no closed issues."""
    cfg = _config()
    d, goal = _complete(tmp_path, cfg)
    run = _runner([(OPEN_PATH, "[]"),
                   (CLOSED_PATH, _page(*[_row(n, True) for n in range(1, uc.PAGE_LIMIT + 1)]))]
                  + _backlog())
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["outcome"] == uc.INCONCLUSIVE
    assert "closed rows" in report["why"]


def test_the_page_is_asked_for_at_rests_own_ceiling(tmp_path):
    """The over-fetch is half the fix: at `per_page=100` a page that is entirely pull requests is
    vanishingly unlikely, and the guard above covers what is left. Asserted on the argv, because the
    page size and the naming cap are different numbers and conflating them is what caused this."""
    assert uc.PAGE_LIMIT == 100                              # REST's own maximum
    assert uc.NAMED_LIMIT < uc.PAGE_LIMIT                    # a rendering cap, not the page size
    cfg = _config()
    d, goal = _complete(tmp_path, cfg)
    run = _runner(_issues())
    uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    reads = [c for c in run.calls if "/issues?" in c]
    # THREE reads, not two: the two label reads plus #1570's membership read of the open backlog.
    assert len(reads) == 3 and all("per_page=100" in c for c in reads)


def test_the_filter_survives_the_projection_being_dropped(tmp_path):
    """`ISSUE_JQ` projects `pull_request` as a BOOLEAN, and `_is_pull_request` tests truthiness — so
    the guard is still right if the projection is ever changed or removed and gh returns the API's
    own raw objects, where `pull_request` is a dict. A guard that read a derived name like `is_pr`
    would fail OPEN in exactly that case, which is the direction that opens pull requests."""
    assert uc._is_pull_request({"pull_request": True}) is True
    assert uc._is_pull_request({"pull_request": {"url": "https://api/…"}}) is True   # raw REST shape
    assert uc._is_pull_request({"pull_request": None}) is False
    assert uc._is_pull_request({"number": 1}) is False
    assert uc._is_pull_request("not a row") is True          # unreadable -> not counted as an issue
    cfg = _config()
    d, goal = _complete(tmp_path, cfg)
    raw = _page({"number": 9, "title": "landing", "pull_request": {"url": "u"}}, _row(3))
    run = _runner([(OPEN_PATH, raw)])
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["open"] == 1 and report["open_issues"] == ["#3 (t3)"]


def test_the_projection_asks_for_the_field_the_filter_reads(tmp_path):
    """The two halves have to meet: the projection must emit `pull_request` or the Python filter has
    nothing to read, and it must be sent on both reads."""
    assert "pull_request" in uc.ISSUE_JQ
    assert "select(" not in uc.ISSUE_JQ                      # it projects; it does not filter
    cfg = _config()
    d, goal = _complete(tmp_path, cfg)
    run = _runner(_issues())
    assert uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)["outcome"] == uc.COMPLETE
    reads = [c for c in run.calls if "/issues?" in c]
    labelled = [c for c in reads if "labels=" in c]
    assert len(labelled) == 2 and all(uc.ISSUE_JQ in c for c in labelled)
    # The membership read carries its OWN projection, which is the same guard plus the two fields
    # `features.read` needs — see BACKLOG_JQ for why widening ISSUE_JQ instead would be wrong.
    membership = [c for c in reads if "labels=" not in c]
    assert len(membership) == 1 and uc.BACKLOG_JQ in membership[0]
    assert "pull_request" in uc.BACKLOG_JQ and "select(" not in uc.BACKLOG_JQ


# --- #1570: the label is not the membership ---------------------------------------------------------

def test_an_unpicked_issue_that_declares_the_unit_IN_ITS_BODY_holds_the_unit_open(tmp_path):
    """THE DEFECT, in one case. `feature:<unit>` is attached at PICK, so an issue that declares the
    unit in its body and has not been picked carries no label — and the label read cannot see it.
    Everything picked here is closed, so the shipped code reported this unit COMPLETE while its
    backlog still held work. That is the one claim in this module nobody re-checks."""
    cfg = _config()
    d, goal = _complete(tmp_path, cfg)
    run = _runner(_issues(backlog=([_declared(41)],)))
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["outcome"] == uc.INCOMPLETE
    assert report["open"] == 1 and report["open_issues"] == ["#41 (t41)"]
    assert "nothing has picked yet" in report["why"] and "#41" in report["why"]
    assert report["action"] is None                      # and nothing was surfaced or opened


def test_a_unit_spelled_in_a_different_case_is_the_SAME_unit(tmp_path):
    """`features._single` de-duplicates case-insensitively because GitHub label names are
    case-insensitively unique, so `Feature: INT-CONTRACT` and `feature/int-contract` name one unit.
    Comparing exactly here would let a body spelled differently from the branch slip past the
    membership measure and be reported as a completion — the same defect the label read had, in a
    narrower doorway. Caught by mutation testing: an `==` here survived every other test in the
    file."""
    cfg = _config()
    d, goal = _complete(tmp_path, cfg)
    run = _runner(_issues(backlog=([_declared(41, unit=UNIT.upper())],)))
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["outcome"] == uc.INCOMPLETE and report["open_issues"] == ["#41 (t41)"]


def test_an_issue_declaring_ANOTHER_unit_is_not_counted_into_this_one(tmp_path):
    """The measure has to be a MEASURE, not a veto: a backlog full of other units' work must not
    hold this one open forever, or the signal is unreachable on any repository with more than one
    unit in flight."""
    cfg = _config()
    d, goal = _complete(tmp_path, cfg)
    run = _runner(_issues(backlog=([_declared(41, unit="other-unit"), _declared(42, body="no marker")],)))
    assert uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)["outcome"] == uc.COMPLETE


def test_membership_is_answered_by_features_read_and_not_by_a_second_parser(tmp_path):
    """A MARKER INSIDE A FENCE IS NOT A DECLARATION (`features` rule 5), and this is how you tell a
    reader from a substring search. A completion pass that scanned bodies itself would count this
    issue in — and would then be a second answer to "what belongs to this unit", free to drift from
    the one the pick path uses. `features.read` is asked, and only `features.read`."""
    cfg = _config()
    d, goal = _complete(tmp_path, cfg)
    fenced = "```\nFeature: %s\n```\n" % UNIT
    run = _runner(_issues(backlog=([_declared(41, body=fenced)],)))
    assert uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)["outcome"] == uc.COMPLETE


def test_a_pull_request_in_the_backlog_is_not_an_open_issue(tmp_path):
    """`/issues` returns pull requests too, and a landing PR on `feature/<unit>` legitimately
    carries the unit in its body. Counting one would make every unit permanently incomplete the
    moment `draft-pr` mode opened its own draft."""
    cfg = _config()
    d, goal = _complete(tmp_path, cfg)
    run = _runner(_issues(backlog=([_declared(41, pull_request=True)],)))
    assert uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)["outcome"] == uc.COMPLETE


def test_the_scan_walks_pages_until_the_server_returns_a_short_one(tmp_path):
    """One page is not the backlog. The member here is on page TWO, behind a full first page of
    issues belonging to other units — the exact arrangement a single-page read reports as complete.
    Stopping is keyed on the RAW page being short, for `_issue_rows`' reason: `per_page` bounds the
    page on the server, so only the server's own short page proves there is nothing behind it."""
    cfg = _config()
    d, goal = _complete(tmp_path, cfg)
    first = [_declared(n, unit="other-unit") for n in range(1, uc.PAGE_LIMIT + 1)]
    run = _runner(_issues(backlog=(first, [_declared(41)])))
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["outcome"] == uc.INCOMPLETE and report["open_issues"] == ["#41 (t41)"]
    walked = [c.split(" --jq")[0] for c in run.calls if BACKLOG_TOKEN in c]
    assert [c.rsplit("&", 1)[-1] for c in walked] == ["page=1", "page=2"]


def test_a_backlog_longer_than_the_cap_claims_NOTHING(tmp_path):
    """`PAGE_LIMIT`'s comment refuses `--paginate` because it is an unbounded number of calls. That
    ruling is kept: the walk is capped, and running out is a REFUSAL, never a completion. The unsafe
    direction here would be to conclude from the pages it did manage to read."""
    cfg = _config()
    d, goal = _complete(tmp_path, cfg)
    full = [_declared(n, unit="other-unit") for n in range(1, uc.PAGE_LIMIT + 1)]
    run = _runner(_issues(backlog=tuple([full] * uc.BACKLOG_PAGES)))
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["outcome"] == uc.INCONCLUSIVE
    assert str(uc.BACKLOG_PAGES * uc.PAGE_LIMIT) in report["why"]
    assert len([c for c in run.calls if BACKLOG_TOKEN in c]) == uc.BACKLOG_PAGES


def test_an_issue_that_contradicts_itself_stops_the_claim_instead_of_the_pass(tmp_path):
    """`features.read` PROPAGATES `AmbiguousUnit`, and its docstring requires any sweep over many
    issues to catch it PER ISSUE — one hand-edited issue must never take the whole pass down. It is
    caught, and it still refuses: an issue whose unit cannot be read is not evidence that this unit
    is finished. Reached only on the pass that was about to claim COMPLETE, and it NAMES the issue,
    because one human edit clears it."""
    cfg = _config()
    d, goal = _complete(tmp_path, cfg)
    rival = "Feature: %s\nFeature: other-unit\n" % UNIT
    run = _runner(_issues(backlog=([_declared(41, body=rival)],)))
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["outcome"] == uc.INCONCLUSIVE
    assert report["ambiguous"] == ["#41 (t41)"] and "#41" in report["why"]
    assert report["action"] is None


def test_an_unreadable_backlog_refuses_rather_than_claiming_a_completion(tmp_path):
    """`_rows`' whole reason, on the read #1570 added: an empty answer from a dead `gh` read as an
    empty backlog would declare the unit complete on the strength of a question that failed."""
    cfg = _config()
    d, goal = _complete(tmp_path, cfg)
    run = _runner([(OPEN_PATH, "[]"), (BACKLOG_TOKEN, RuntimeError("gh: HTTP 502"))]
                  + [(CLOSED_PATH, _page(_row(12)))])
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["outcome"] == uc.UNREADABLE and "HTTP 502" in report["why"]


def test_the_membership_read_is_never_paid_while_the_unit_is_plainly_still_worked(tmp_path):
    """THE COST, ASSERTED. The scan is the price of a CLAIM, not of a goal: a unit with anything
    still open under its label stops at the one call it always cost, and the extra pages are spent
    only on the pass that would otherwise have reported completion. §6e's own unit of account."""
    cfg = _config()
    d, goal = _complete(tmp_path, cfg)
    run = _runner(_issues(open_numbers=(7,)))
    assert uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)["outcome"] == uc.INCOMPLETE
    assert [c for c in run.calls if BACKLOG_TOKEN in c] == []


def test_the_scan_reads_bodies_off_the_LIST_PAGE_and_never_one_issue_at_a_time(tmp_path):
    """The other half of the cost, and the reason this is a list walk rather than the obvious
    `gh issue view <n>` per candidate. `body` and `labels` come back INSIDE the page, so widening
    the measure costs PAGES, not ISSUES — a per-issue read is what §6e already charges per goal
    considered, and paying it again per backlog entry is what makes a measure unaffordable."""
    cfg = _config()
    d, goal = _complete(tmp_path, cfg)
    run = _runner(_issues(backlog=([_declared(n) for n in range(1, 40)],)))
    assert uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)["outcome"] == uc.INCOMPLETE
    per_issue = [c for c in run.calls if "/issues/" in c]
    assert per_issue == [], "the membership read paid a call per issue"
    assert len([c for c in run.calls if "/issues?" in c]) == 2   # one label read + one page


def test_a_pull_request_somebody_already_readied_is_not_reported_as_parked(tmp_path):
    """The PR on the head need not be one this feature opened — a human may have raised it, or
    readied a draft from last week. `parked` is the field a reader uses to decide whether anything
    still waits on a person, so claiming it for a ready PR under review would be this module
    reporting a state it neither created nor can see."""
    cfg = _config(mode="draft-pr")
    d, goal = _complete(tmp_path, cfg)
    run = _runner(_issues() + _landing_pr(state="open", draft=False))
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["action"] == uc.DRAFT_EXISTS
    assert report["parked"] is False
    assert _pr_creates(run.calls) == []
    assert _merges(run.calls) == []                          # and it is still not merged


def test_an_unreadable_landing_pr_list_refuses_rather_than_opening_a_duplicate(tmp_path):
    """"Could not ask" is not "there is none" -- the distinction `_sibling_pull_requests` exists to
    keep, one layer over. Reading a dead `gh` as "no PR yet" is how a duplicate gets opened."""
    cfg = _config(mode="draft-pr")
    d, goal = _complete(tmp_path, cfg)
    run = _runner(_issues() + [(PULLS_PATH, RuntimeError("gh: network is unreachable"))])
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["action"] == uc.DRAFT_REFUSED
    assert _pr_creates(run.calls) == []


# --- the tally: what "complete" is measured from --------------------------------------------------

def test_one_open_issue_is_not_a_complete_unit(tmp_path):
    """The cheap verdict, and the common one: an open issue ends the pass at ONE gh call, with no
    closed-issue tally, no sibling read and nothing said."""
    cfg = _config()
    d, goal = _complete(tmp_path, cfg)
    run = _runner(_issues(open_numbers=(51,)))
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["outcome"] == uc.INCOMPLETE
    assert report["open"] == 1
    assert len(run.calls) == 1
    assert "--state closed" not in " ".join(run.calls)


def test_a_unit_with_no_issues_at_all_is_not_complete(tmp_path):
    """Zero open AND zero closed is an empty label, not a finished body of work. Treating it as
    complete would fire the signal on a unit nobody has started."""
    cfg = _config()
    d, goal = _complete(tmp_path, cfg)
    run = _runner(_issues(closed_numbers=()))
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["outcome"] == uc.INCOMPLETE
    assert report["closed"] == 0


def test_an_unreadable_board_claims_nothing(tmp_path):
    """Fails CLOSED, the same posture `sibling_gate` takes: an unreadable answer is the only
    evidence there is, and the act a consumer might take on it opens a pull request."""
    cfg = _config(mode="draft-pr")
    d, goal = _complete(tmp_path, cfg)
    run = _runner([("/issues?", RuntimeError("gh: HTTP 502"))])
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["outcome"] == uc.UNREADABLE
    assert _pr_creates(run.calls) == []


def test_a_reply_that_is_not_a_list_claims_nothing(tmp_path):
    cfg = _config()
    d, goal = _complete(tmp_path, cfg)
    run = _runner([("/issues?", '{"number": 1}')])
    assert uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)["outcome"] == uc.UNREADABLE


def test_a_reply_that_is_not_json_claims_nothing(tmp_path):
    """`gh` can print a warning, an auth prompt or an HTML error page on stdout. None of them is a
    backlog, and none of them is empty either — so the JSON guard is a separate refusal from the
    empty-stdout one, with its own wording."""
    cfg = _config()
    d, goal = _complete(tmp_path, cfg)
    run = _runner([("/issues?", "gh: this looks like plain prose")])
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["outcome"] == uc.UNREADABLE
    assert "not JSON" in report["why"]


def test_an_unreadable_closed_tally_claims_nothing(tmp_path):
    """The SECOND read can fail on its own — zero open is established and the tally is not. Reporting
    `0 open, 0 closed` there would be an empty-label verdict manufactured from a failed read, which
    is the one way this feature could announce a completion nobody measured."""
    cfg = _config(mode="draft-pr")
    d, goal = _complete(tmp_path, cfg)
    run = _runner([(OPEN_PATH, json.dumps([])),
                   (CLOSED_PATH, RuntimeError("gh: HTTP 502"))])
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["outcome"] == uc.UNREADABLE
    assert report["open"] == 0 and report["closed"] is None
    assert _pr_creates(run.calls) == []


def test_a_refused_pr_create_is_a_note_not_a_crash(tmp_path):
    """`gh pr create` genuinely refuses in ordinary conditions — no commits between the branches, a
    head that was never pushed, insufficient permission. A goal's terminal record must not be lost
    over any of them, and the signal itself still reaches the human."""
    cfg = _config(mode="draft-pr")
    d, goal = _complete(tmp_path, cfg)
    run = _runner(_issues() + _no_landing_pr() +
                  [(CREATE_PATH, RuntimeError("HTTP 422: No commits between main and "
                                              "feature/int-contract"))])
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["outcome"] == uc.COMPLETE
    assert report["action"] == uc.DRAFT_REFUSED
    assert report["parked"] is False
    assert "No commits between" in report["why"]


def test_the_cli_reports_the_signal_and_runs_in_the_projects_own_mode(tmp_path, capsys):
    """`unit_completion.py check <sdlc_dir> <goal>`. There is deliberately no `--surface-only` flag
    — `cross_repo.main`'s and `feature_sync.main`'s rule: the mode is the project's decision, and a
    second way to run the check is a second answer to a question that has one.

    Driven on a goal that declares no unit, so the whole verb runs at ZERO gh calls — the CLI has no
    runner to inject, and a test that reached the network would be testing GitHub."""
    d = _sdlc(tmp_path)
    goal = _started(d, base="main")
    _registry(d)
    assert uc.main(["unit_completion.py", "check", d, goal]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == uc.NO_UNIT
    assert uc.main(["unit_completion.py"]) == 2
    assert "usage:" in capsys.readouterr().err


def test_empty_stdout_is_not_an_empty_backlog(tmp_path):
    """`gh issue list --json` prints `[]` for no results, so EMPTY stdout is not that answer. Reading
    it as "no open issues" would declare every unit complete the moment `gh` went quiet."""
    cfg = _config()
    d, goal = _complete(tmp_path, cfg)
    run = _runner([("/issues?", "   ")])
    assert uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)["outcome"] == uc.UNREADABLE


# --- the inherited both-green check: SURFACED, never acted on -------------------------------------

def test_a_cross_repo_unit_surfaces_the_sibling_verdict(tmp_path):
    """#1474's check is consumed here and nowhere else. A `False` is INFORMATION on the line a human
    reads -- it is not a gate, and it does not stop the signal being surfaced."""
    cfg = _config()
    d, goal = _complete(tmp_path, cfg)
    _landing(d, goal)
    _registry(d, repos={"acme/app": {"branch": BRANCH}, "acme/api": {"branch": BRANCH}})
    run = _runner(_issues() + [("repo view", "acme/app"),
                               ("pr list --repo acme/api", "[]")])
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["outcome"] == uc.COMPLETE
    assert report["sibling"]["ok"] is False
    assert "acme/api" in report["sibling"]["reason"]
    assert _pr_creates(run.calls) == []


def test_a_not_ready_sibling_does_not_stop_draft_mode_opening_the_draft(tmp_path):
    """THE DEADLOCK #1474 TRACED, refused here explicitly. While a unit is being built neither
    repo's landing PR exists, so gating the draft on the sibling means the first repo to finish
    waits for a PR only the second repo could open -- symmetrically, forever. A draft cannot merge,
    so opening one while the sibling is not ready lands nothing; it is what BREAKS the symmetry."""
    cfg = _config(mode="draft-pr")
    d, goal = _complete(tmp_path, cfg)
    _landing(d, goal)
    _registry(d, repos={"acme/app": {"branch": BRANCH}, "acme/api": {"branch": BRANCH}})
    run = _runner(_issues() + _no_landing_pr() + _created() +
                  [("repo view", "acme/app"), ("pr list --repo acme/api", "[]")])
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["sibling"]["ok"] is False
    assert report["action"] == uc.DRAFTED
    assert _merges(run.calls) == []


def test_the_sibling_verdict_reaches_the_draft_body(tmp_path):
    """The person who readies the draft is the person who has to know the other half is not there.
    A verdict recorded only in a returned dict reaches nobody."""
    cfg = _config(mode="draft-pr")
    d, goal = _complete(tmp_path, cfg)
    _landing(d, goal)
    _registry(d, repos={"acme/app": {"branch": BRANCH}, "acme/api": {"branch": BRANCH}})
    run = _runner(_issues() + _no_landing_pr() + _created() +
                  [("repo view", "acme/app"), ("pr list --repo acme/api", "[]")])
    uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    body = _pr_creates(run.calls)[0]
    assert "acme/api" in body


def test_an_ordinary_single_repo_unit_never_asks_about_a_sibling(tmp_path):
    """`sibling_gate` returns `(True, "")` at ZERO API calls when the check does not apply, and the
    report must say `None` rather than inventing a readiness nothing measured.

    `not-cross-repo` is the pick-time decision an adopting single-repo project records for every
    goal -- which is why `_complete` writes one, and why the sibling verdict here is silence."""
    cfg = _config()
    d, goal = _complete(tmp_path, cfg)
    run = _runner(_issues())
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["outcome"] == uc.COMPLETE                  # the pass really reached the check
    assert report["sibling"] is None
    assert not any("repo view" in c for c in run.calls)


def test_a_goal_whose_pick_time_check_never_ran_surfaces_that_refusal(tmp_path):
    """The one `recorded() is None` case that is NOT benign, carried through rather than swallowed.
    It says the pick-time check never ran here, which is exactly the thing a human deciding whether
    to land a unit needs told -- and it is told as prose, not as a park."""
    cfg = _config()
    d, goal = _complete(tmp_path, cfg, landing=False)
    report = uc.signal(d, cfg, goal, run=_runner(_issues()), sleep=NOSLEEP)
    assert report["sibling"]["ok"] is False
    assert "pick-time check never ran" in report["sibling"]["reason"]
    assert report["action"] == uc.SURFACED                   # information, not a gate


# --- the three survivors the review found, pinned -------------------------------------------------
#
# All three were fully LINE-COVERED and completely unpinned, which is the whole lesson: 99% line
# coverage and a loosening mutant that survives the entire suite are perfectly consistent
# measurements of different things.

def _record_with_signal(loop, d, goal, result, source, run):
    """Drive the real `loop._record` with only the signal's network runner substituted, and report
    which goals the signal was asked about."""
    asked = []
    original = loop._signal_unit_completion

    def _offline(sdlc_dir, goal_):
        asked.append(goal_)
        return original(sdlc_dir, goal_, run=run, sleep=NOSLEEP)

    loop._signal_unit_completion = _offline
    try:
        outcome = loop._record(d, source, goal, result)
    finally:
        loop._signal_unit_completion = original
    return outcome, asked


class _Source:
    """A backlog source that records what was asked of it. `complete` can be made to RAISE, which is
    the case `_record`'s own `done` -> `parked` downgrade exists for."""

    def __init__(self, complete_raises=False):
        self.parked, self.completed, self.failed = [], [], []
        self._raises = complete_raises

    def complete(self, goal):
        self.completed.append(goal)
        if self._raises:
            raise RuntimeError("gh issue close: GraphQL quota exhausted")

    def park(self, goal, reason, **kw):
        self.parked.append((goal, reason))

    def fail(self, goal, reason):
        self.failed.append((goal, reason))


def test_only_a_done_fires_the_signal_and_a_park_never_does(tmp_path):
    """`loop._record`'s `outcome == "done"` guard, which survived the ENTIRE suite unpinned.

    It is load-bearing for two separate reasons and the second is the one that bites: a parked
    goal's issue is still OPEN, so the pass would end `INCOMPLETE` and change nothing — but it would
    pay a real API call per park to learn that, on the very budget this module was just made to
    account for. Widening the guard is a cost regression that nothing anywhere would notice."""
    loop = _load("loop")
    cfg = _config()
    run = _runner(_issues())
    for result in ("parked", "failed"):
        d, goal = _complete(tmp_path / result, cfg)
        source = _Source()
        outcome, asked = _record_with_signal(loop, d, goal, result, source, run)
        assert outcome == result, result
        assert asked == [], result                   # never even consulted
    d, goal = _complete(tmp_path / "done", cfg)
    source = _Source()
    outcome, asked = _record_with_signal(loop, d, goal, "done", source, run)
    assert outcome == "done" and asked == [goal]     # and the guard is not simply never true


def test_a_done_downgraded_to_parked_does_not_fire_the_signal(tmp_path):
    """The exact case `_record`'s own comment names: `source.complete()` raised, so the issue was
    NOT closed and the goal is recorded `parked` instead. Nothing new can have become complete, so
    asking is spending a call to be told so. This is why the guard reads `outcome`, not `result`."""
    loop = _load("loop")
    cfg = _config()
    d, goal = _complete(tmp_path, cfg)
    source = _Source(complete_raises=True)
    outcome, asked = _record_with_signal(loop, d, goal, "done", source, _runner(_issues()))
    assert outcome == "parked"
    assert asked == []


def test_the_closed_tally_says_100_plus_at_the_page_boundary(tmp_path):
    """A count capped at REST's own `per_page` ceiling and printed as a bare number reads as a
    measurement. The boundary is `>=`, and `>` survived every test until this one: at exactly
    `PAGE_LIMIT` rows the page is full, so there may be more."""
    cfg = _config()
    limit = uc.PAGE_LIMIT
    for count, truncated, shown in ((limit, True, "%d+" % limit), (limit - 1, False, str(limit - 1))):
        d, goal = _complete(tmp_path / str(count), cfg)
        run = _runner(_issues(closed_numbers=tuple(range(1, count + 1))))
        report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
        assert report["closed"] == count and report["truncated"] is truncated, count
        assert "%s closed" % shown in report["why"], count


def test_the_open_count_is_truncated_out_loud_too(tmp_path):
    """The same objection, on the read a verdict hangs off. It also NAMES what is open, which is
    what the page size's own comment claimed all along — capped at `NAMED_LIMIT` for the LINE, with
    the overflow counted rather than dropped, because a line that stopped silently would read the
    same for five open issues and for a hundred."""
    cfg = _config()
    d, goal = _complete(tmp_path, cfg)
    run = _runner(_issues(open_numbers=tuple(range(1, uc.PAGE_LIMIT + 1))))
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["outcome"] == uc.INCOMPLETE
    assert report["open"] == uc.PAGE_LIMIT and report["open_truncated"] is True
    assert "%d+ open issues" % uc.PAGE_LIMIT in report["why"]
    assert len(report["open_issues"]) == uc.PAGE_LIMIT       # the report keeps every one it saw
    assert report["open_issues"][0] == "#1 (t1)"
    assert "#1 (t1)" in report["why"] and "#%d " % uc.PAGE_LIMIT not in report["why"]
    assert "+%d more" % (uc.PAGE_LIMIT - uc.NAMED_LIMIT) in report["why"]


def test_the_named_overflow_is_counted_not_dropped(tmp_path):
    """`feature_sync._clause`'s rule, and the reason the naming cap is not the page size: at exactly
    `NAMED_LIMIT` open issues there is no overflow to report, and at one more there is."""
    cfg = _config()
    for n, tail in ((uc.NAMED_LIMIT, None), (uc.NAMED_LIMIT + 1, "+1 more")):
        d, goal = _complete(tmp_path / str(n), cfg)
        run = _runner(_issues(open_numbers=tuple(range(1, n + 1))))
        why = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)["why"]
        assert ("more" in why) is bool(tail), n
        if tail:
            assert tail in why


def test_one_open_issue_reads_as_singular_and_is_not_truncated(tmp_path):
    """The other side of the same boundary — without it, `>=` could be replaced by `>= 1` and the
    test above would not notice."""
    cfg = _config()
    d, goal = _complete(tmp_path, cfg)
    report = uc.signal(d, cfg, goal, run=_runner(_issues(open_numbers=(51,))), sleep=NOSLEEP)
    assert report["open_truncated"] is False
    assert "1 open issue:" in report["why"] and "1+ " not in report["why"]


def test_the_surfaced_signal_reaches_the_units_owner_through_the_ledger(tmp_path):
    """The whole ledger half of `surface` mode — what the config doc, the module docstring and the
    PR body all advertise the default as DOING — and deleting the append outright survived every
    test. A note in a stream nobody reads is not surfacing; an addressee is what makes it reach a
    person (§7.3), so both the entry and its `to` are asserted."""
    cfg = _config()
    d = _sdlc(tmp_path, cfg, ledger_on=True)
    goal = _started(d)
    _registry(d, owner="@unit-owner")
    _landing(d, goal, outcome=cross_repo.NOT_CROSS_REPO, tier=None, cross_repo=False)
    report = uc.signal(d, cfg, goal, run=_runner(_issues()), sleep=NOSLEEP)
    assert report["action"] == uc.SURFACED
    notes = [e for e in _load("ledger").read_all(d) if e.get("kind") == "note"]
    assert len(notes) == 1
    # #1574: the registry spells its owner `@unit-owner`; the LEDGER's own namespace is a bare,
    # case-folded login, because that is what every consumer of `to` compares against. Written raw,
    # this note was correct and undeliverable.
    assert notes[0]["to"] == "unit-owner"
    assert notes[0]["area"] == "features" and notes[0]["ref"] == "features/int-contract"
    assert "looks complete" in notes[0]["why"] and "0 open issues, 2 closed" in notes[0]["why"]


def test_the_ledger_being_off_costs_the_note_and_never_the_signal(tmp_path):
    """`feature_sync`'s own pairing: the ledger is opt-in and every call to it is fail-open, so a
    project with it off still gets the stderr line and the same verdict."""
    cfg = _config()
    d, goal = _complete(tmp_path, cfg)
    report = uc.signal(d, cfg, goal, run=_runner(_issues()), sleep=NOSLEEP)
    assert report["action"] == uc.SURFACED
    assert [e for e in _load("ledger").read_all(d) if e.get("kind") == "note"] == []


def test_an_incomplete_unit_says_nothing_to_anybody(tmp_path):
    """The counterpart that keeps the two tests above from passing for the wrong reason: if `_tell`
    fired on every pass, the ledger assertions would be satisfied by noise."""
    cfg = _config()
    d = _sdlc(tmp_path, cfg, ledger_on=True)
    goal = _started(d)
    _registry(d)
    _landing(d, goal, outcome=cross_repo.NOT_CROSS_REPO, tier=None, cross_repo=False)
    report = uc.signal(d, cfg, goal, run=_runner(_issues(open_numbers=(51,))), sleep=NOSLEEP)
    assert report["outcome"] == uc.INCOMPLETE
    assert [e for e in _load("ledger").read_all(d) if e.get("kind") == "note"] == []


# --- the inherited reason_class obligation: a `False` never parks ---------------------------------

def test_a_not_ready_sibling_never_parks_the_goal_that_closed_the_unit(tmp_path):
    """#1474 measured that 18 of `sibling_gate`'s 20 reachable reasons classify as `unknown` and
    none as mechanical, and handed the remedy to THIS goal because it depends on how a `False` is
    surfaced. The decision is that nothing here parks, so no classifier entry is owed.

    THIS IS THAT DECISION PINNED, not promised. It drives the real `loop._record` done path with a
    refusing sibling and asserts the goal was still recorded `done` and `source.park` was never
    called. Anyone who later routes one of these reasons into a park fails here, and inherits the
    classification obligation at the moment they create it."""
    loop = _load("loop")
    cfg = _config()
    d, goal = _complete(tmp_path, cfg)
    _landing(d, goal)                                        # tier 1, so the sibling is consulted
    _registry(d, repos={"acme/app": {"branch": BRANCH}, "acme/api": {"branch": BRANCH}})

    class Source:
        def __init__(self):
            self.parked, self.completed = [], []

        def complete(self, g):
            self.completed.append(g)

        def park(self, g, reason, **kw):
            self.parked.append((g, reason))

    source = Source()
    run = _runner(_issues() + [("repo view", "acme/app"), ("pr list --repo acme/api", "[]")])
    seen = {}
    original = loop._signal_unit_completion

    def _offline(sdlc_dir, goal_):
        """The REAL signal, with only its network runner substituted — so what `_record` gets back
        is a genuine `sibling_gate` refusal rather than a hand-written stand-in for one."""
        seen["report"] = original(sdlc_dir, goal_, run=run, sleep=NOSLEEP)
        return seen["report"]

    loop._signal_unit_completion = _offline
    try:
        outcome = loop._record(d, source, goal, "done")
    finally:
        loop._signal_unit_completion = original
    # The wire exists at all: an empty `seen` would mean `_record` never asked.
    assert seen["report"]["sibling"]["ok"] is False
    assert "acme/api" in seen["report"]["sibling"]["reason"]
    assert outcome == "done"
    assert source.parked == []
    assert source.completed == [goal]


# --- the repo reference: an explicit slug, or gh's own placeholders --------------------------------

def test_with_no_declared_repo_it_uses_ghs_own_placeholders(tmp_path):
    """THE REAL INSTALL PATH, not a theoretical one: the shipped config template leaves
    `discovery.github.repo` empty. `feature_sync.repo_slug` then falls back to parsing the git
    remote, and where even that answers nothing, `gh api repos/{owner}/{repo}/...` resolves the
    checkout's own remote — the same pattern `work.pr()` and `work.protection()` already use, so
    this is not a second convention.

    Asserted on the argv, because a placeholder that silently became the literal string
    `{owner}/{repo}` would 404 rather than fail loudly."""
    cfg = _config(repo="")
    d = _sdlc(tmp_path, cfg)
    goal = _started(d)
    _registry(d)
    _landing(d, goal, outcome=cross_repo.NOT_CROSS_REPO, tier=None, cross_repo=False)
    open_path = "repos/{owner}/{repo}/issues?labels=feature%3Aint-contract&state=open"
    closed_path = open_path.replace("state=open", "state=closed")
    run = _runner([(open_path, "[]"), (closed_path, json.dumps([{"number": 3, "title": "t"}]))]
                  + _backlog())
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["outcome"] == uc.COMPLETE
    assert all("repos/{owner}/{repo}/" in c for c in run.calls if "gh api" in c)


def test_the_placeholder_owner_survives_into_the_head_filter(tmp_path):
    """`head=<owner>:<branch>` is REST's own fork-aware filter, so the owner half has to be a
    placeholder too when the repo half is — and it must not be percent-encoded into uselessness,
    which is why `quote` is given `safe="{}"` there."""
    cfg = _config(mode="draft-pr", repo="")
    d = _sdlc(tmp_path, cfg)
    goal = _started(d)
    _registry(d)
    _landing(d, goal, outcome=cross_repo.NOT_CROSS_REPO, tier=None, cross_repo=False)
    open_path = "repos/{owner}/{repo}/issues?labels=feature%3Aint-contract&state=open"
    run = _runner(_backlog() +
                  [(open_path, "[]"),
                   ("state=closed", json.dumps([{"number": 3, "title": "t"}])),
                   ("/pulls?", "[]"), (CREATE_PATH.replace("acme/app", "{owner}/{repo}"),
                                       "https://github.com/acme/app/pull/7")])
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["action"] == uc.DRAFTED
    reads = [c for c in run.calls if "/pulls?" in c]
    assert len(reads) == 1 and "head={owner}:" + BRANCH in reads[0]


def test_an_entry_that_is_not_an_issue_is_named_rather_than_crashing(tmp_path):
    """`gh` answered with a LIST, but nothing says its elements are objects — `work._sibling_pr_state`
    keeps the same guard one layer over. A `TypeError` out of a naming helper would cost the whole
    signal, and the report would be `FAILED` for a reason nobody could read."""
    assert uc._named("not a dict") == "(an entry that is not an issue)"
    # A title with no number keeps the title: the number is how you go and look, the title is how
    # you recognise it, and losing the half that survived would make the entry unidentifiable.
    assert uc._named({"title": "t"}) == "(an issue with no number) (t)"
    assert uc._named({}) == "(an issue with no number)"
    assert uc._named({"number": True}) == "(an issue with no number)"   # bools are ints in Python
    assert uc._named({"number": 3}) == "#3"
    assert uc._named({"number": 3, "title": "  a  b  "}) == "#3 (a b)"
    # A row that is not an object never reaches `_named` from `_signal` any more — `_is_pull_request`
    # treats it as a pull request and drops it, which is the fail-closed direction: an uncounted row
    # can only make a unit look LESS finished. `_named` keeps its own guard because it is a naming
    # helper, and a helper that raises would cost the whole signal.
    cfg = _config()
    d, goal = _complete(tmp_path, cfg)
    run = _runner([(OPEN_PATH, json.dumps(["nonsense", {"number": 4, "title": "real"}]))])
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["outcome"] == uc.INCOMPLETE
    assert report["open_issues"] == ["#4 (real)"]
    assert report["open"] == 1


def test_a_pull_request_row_that_is_not_an_object_is_skipped_not_trusted(tmp_path):
    """Same guard on the landing read. Skipping is right and returning `DRAFTED` on it would not be:
    a row this cannot read is a row whose `state` is unknown, and an unknown state must never be
    treated as 'there is nothing here'."""
    cfg = _config(mode="draft-pr")
    d, goal = _complete(tmp_path, cfg)
    rows = json.dumps(["nonsense", {"number": 41, "url": "u", "draft": True, "state": "open",
                                    "merged_at": None}])
    run = _runner(_issues() + [(PULLS_PATH, rows)] + _created())
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["action"] == uc.DRAFT_EXISTS and report["pr"] == "u"
    assert _pr_creates(run.calls) == []


# --- totality -------------------------------------------------------------------------------------

def test_it_never_raises_on_a_config_it_cannot_read(tmp_path):
    """A signal is a courtesy. Losing it is bad; losing the goal's own terminal bookkeeping because
    the courtesy blew up would be a real loss -- so this is total, like every other module in this
    epic, and `FAILED` claims nothing rather than guessing."""
    d = _sdlc(tmp_path)
    goal = _started(d)
    _registry(d)
    for bad in (None, "config", 7, [], {"work": "not-a-mapping"}):
        assert uc.mode(bad) == uc.SURFACE, bad               # the mode reader is total on its own
        report = uc.signal(d, bad, goal, run=_runner([]), sleep=NOSLEEP)
        assert report["outcome"] in uc.OUTCOMES, bad
        assert report["action"] is None, bad                 # nothing was done on a broken read


def test_a_registry_shard_that_is_garbage_does_not_stop_the_signal(tmp_path):
    """`registry.read` degrades a broken unit file to "that one unit is not known", so the owner
    lookup finds nothing. An unaddressed note beats a crashed loop record."""
    cfg = _config()
    d, goal = _complete(tmp_path, cfg)
    (feature_registry.registry_dir(d) / "units" / (UNIT + ".json")).write_text("{ not json")
    report = uc.signal(d, cfg, goal, run=_runner(_issues()), sleep=NOSLEEP)
    assert report["outcome"] == uc.COMPLETE
    assert report["action"] == uc.SURFACED


# --- Round-8 review finding: the already-merged branch was never wired to merge observation ------

def _canonical_merged_pr(number=41, branch=BRANCH, sha="a" * 40):
    return json.dumps({"number": number, "node_id": "PR_%s" % number,
                       "created_at": "2026-01-01T00:00:00Z", "merged_at": "2026-08-22T00:00:00Z",
                       "merge_commit_sha": sha, "head": {"ref": branch},
                       "base": {"ref": "main", "repo": {"node_id": "R_1"}}})


def test_an_already_merged_unit_landing_is_recorded_when_the_ledger_wants_it(tmp_path):
    """Plan-committed, flagged blocking in plan-review rounds R8/R9, shipped unwired: the branch
    that fires when `_draft` finds the unit's landing PR was already merged (a human ran
    /sigma-rebase, or merged it by hand) built only a human-readable `why` string -- the ledger
    `merged` entry and `merge_observed` journal event this whole goal exists to write were never
    recorded for a unit landing discovered this way. Now wired through the same
    ledger.enabled/journal_on gate every other merge-observation call site uses."""
    cfg = _config(mode="draft-pr")
    cfg["ledger"] = {"enabled": True, "actor": "t"}
    cfg["journal"] = {"enabled": True}
    d, goal = _complete(tmp_path, cfg)
    run = _runner(_issues() + _landing_pr(state="closed", draft=False, merged=True)
                 + [("api repos/acme/app/pulls/41", _canonical_merged_pr())])
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["action"] == uc.DRAFT_DECLINED
    assert "already merged" in report["why"]
    entries = [e for e in uc.ledger.read_all(d) if e.get("kind") == "merged"]
    events = [e for e in journal_events(uc.ledger, d) if e.get("kind") == "merge_observed"]
    assert len(entries) == 1 and entries[0]["pr"] == "41"
    assert len(events) == 1 and events[0]["pr"] == 41


def test_an_already_merged_unit_landing_writes_nothing_with_both_sinks_off(tmp_path):
    """Non-vacuity, and the shipped default: neither sink on means no gh api call, no write."""
    cfg = _config(mode="draft-pr")
    d, goal = _complete(tmp_path, cfg)
    run = _runner(_issues() + _landing_pr(state="closed", draft=False, merged=True))
    report = uc.signal(d, cfg, goal, run=run, sleep=NOSLEEP)
    assert report["action"] == uc.DRAFT_DECLINED
    assert not any("pulls/41" in c for c in run.calls), "fetched canonical facts with both sinks off"
    assert uc.ledger.read_all(d) == []
