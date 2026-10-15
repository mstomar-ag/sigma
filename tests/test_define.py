"""`sigma-define` -- the skill that OPENS a unit of work (#1662, story #1427, epic #1464).

Every other module on this epic READS a declaration or acts on one. This one is the first that
CREATES the thing being declared, which puts it on the far side of three rules the rest of the kit
states from the other direction, and each test below exists because breaking one of them is a bug
this repo has already shipped and fixed:

  - THE STEP ORDER IS THE FEATURE (§14). Branch, then label, then registry, then declare. A body
    marker whose label does not yet exist REFUSES the pick and sets the goal aside -- measured
    twice, on a repository with no registry at all. So the order is asserted as data (`_STEPS`) AND
    as observed call order, and both directions of "a failing step stops the ones after it" are
    pinned. A guard that only checked the happy path would pass against an implementation that ran
    all three steps regardless and reported the first failure at the end.
  - THE TYPE IS METADATA, NEVER A PREFIX (§2/§5). All three types produce `feature/<name>`, because
    base resolution matches on that one prefix, and a branch carrying a per-kind one instead would
    be invisible to it.
  - THERE IS EXACTLY ONE DEFINITION OF WHAT A UNIT IS CALLED (`features._is_unit_name`). Asserted by
    OBJECT IDENTITY rather than by a behaviour table, because a table can be satisfied by a faithful
    copy -- and a faithful copy is precisely what drifts later. The table is kept as well, but it is
    the identity assertion that makes a second predicate impossible rather than merely unlikely.

THE ONE RULE THIS MODULE IS ALLOWED TO BREAK, PINNED AS A TEST RATHER THAN AS PROSE. Sigma never
creates a `feature:*` label; `sources.GitHubSource._run` refuses that exact invocation at the single
chokepoint every `gh` call in that class passes through. `sigma-define` is the human's own gesture, so
it is the one authorized exception -- and `test_the_label_create_is_exactly_the_shape_the_kit_refuses`
proves the exception is real by feeding this module's own argv to the kit's own refusal predicate. A
comment claiming the exception would be worth nothing; a test that FAILS if the two ever stop
describing the same call is worth something.

Hermetic throughout: no `git`, no `gh`, no network, no live branch/label/issue. The runner is
injected (`run=`), the same `(cwd, argv) -> stdout` contract `feature_sync._run`, `feature_rebase`
and `work._run` all share, so a runner written here could be handed to any of them unchanged.
"""
import importlib.util
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFINE_SCRIPTS = _ROOT / "skills" / "sigma-define" / "scripts"
LOOP = _ROOT / "skills" / "sigma-loop" / "scripts"


def _mod(name, where):
    spec = importlib.util.spec_from_file_location(name, where / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


define = _mod("define", DEFINE_SCRIPTS)
features = _mod("features", LOOP)
feature_labels = _mod("feature_labels", LOOP)
feature_stamp = _mod("feature_stamp", LOOP)
feature_registry = _mod("feature_registry", LOOP)
sources_mod = _mod("sources", LOOP)   # for a REAL GitHubSource in the bump-priority tests below


# ------------------------------------------------------------------------------- the fake world


class Runner:
    """A `git`/`gh` stand-in with the real runner's exact contract: `(cwd, argv) -> stdout`, raising
    on what the real binary would exit non-zero for.

    It holds real STATE -- the set of branches the remote has and the set of labels the repo has --
    rather than replaying canned strings, so idempotency and refusal are exercised against something
    that actually changes when the code under test writes to it. `calls` is the observed argv order,
    which is what pins the step order."""

    def __init__(self, branches=(), labels=(), head="main", fail_on=None, refs=None):
        self.refs = refs                     # None: every ref exists; else the set of refs show-ref finds
        self.branches = set(branches)
        self.labels = set(labels)
        self.head = head
        self.fail_on = fail_on or (lambda argv: None)
        self.calls = []

    def __call__(self, cwd, argv):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        boom = self.fail_on(argv)
        if boom:
            raise RuntimeError(boom)
        if argv[:2] == ["git", "ls-remote"]:
            wanted = argv[-1]
            if wanted.startswith("refs/heads/") and wanted[len("refs/heads/"):] not in self.branches:
                return "abc123\t" + wanted       # #1504: the base-existence probe -- every base exists
            return "abc123\trefs/heads/%s" % wanted if wanted in self.branches else ""
        if argv[:2] == ["git", "fetch"]:
            return ""
        if argv[:2] == ["git", "rev-parse"]:
            if "--verify" in argv:
                return "f" * 40               # #1504: the fetched `<remote>/<base>` commit
            return self.head
        if argv[:3] == ["gh", "api", "user"]:
            return "tester"
        if argv[:2] == ["git", "show-ref"]:
            if self.refs is not None and argv[-1] not in self.refs:
                raise RuntimeError("git exited 1")
            return ""
        if argv[:2] == ["git", "push"]:
            self.branches.add(argv[-1].split(":refs/heads/", 1)[-1])
            return ""
        if argv[:3] == ["gh", "label", "list"]:
            assert "--search" not in argv, (
                "the existence check must ENUMERATE -- `gh label list --search` is an eventually-"
                "consistent index that returned an unrelated label for a name created seconds "
                "earlier, measured on the live repo")
            return "\n".join(sorted(self.labels))
        if argv[:3] == ["gh", "label", "create"]:
            if any(n.lower() == argv[3].lower() for n in self.labels):
                # `gh`'s real message, measured: it says this for a name differing only in case too.
                raise RuntimeError('label with name "%s" already exists; use `--force` to update '
                                   'its color and description' % argv[3])
            self.labels.add(argv[3])
            return ""
        raise AssertionError("the fake runner was handed a command it does not model: %r" % (argv,))

    def kinds(self):
        """The observed call sequence reduced to what the step order is ABOUT -- `git` vs `gh` --
        so an assertion on order does not also pin the exact flags of each call."""
        return [a[0] for a in self.calls]


def _sdlc(tmp_path):
    sdlc = tmp_path / ".sdlc"
    (sdlc / "state").mkdir(parents=True)
    return sdlc


def _open(tmp_path, name="voice-interview", kind="feature", **kw):
    run = kw.pop("run", None) or Runner()
    report = define.open_unit(_sdlc(tmp_path), name, kind, run=run, cwd=tmp_path, **kw)
    return report, run


# ------------------------------------------------------------------------------- one rule, borrowed


def test_the_name_predicate_IS_features_own_object_not_a_copy_of_it():
    """The assertion that makes a second rule impossible rather than merely unlikely.

    A behaviour table (below) is satisfied by a faithful copy, and a faithful copy is exactly what
    drifts once `features.py` corrects a rule -- which it has already done twice (the `.lock` suffix
    being case-SENSITIVE, and `/` being refused for ambiguity rather than for safety). Identity
    cannot drift. `feature_registry` borrows the same object the same way, so this is the
    established gesture, not a new one.

    IDENTITY IS ASSERTED AGAINST THE MODULE'S OWN LOADED SIBLING, not against this file's. Every
    module in this family loads its siblings by FILE PATH (`spec_from_file_location`), so this test
    file's `features` and `define.py`'s `features` are two distinct module objects of the same
    source -- `feature_registry.is_unit_name is features._is_unit_name` fails here for the identical
    reason, and it is borrowing correctly. What the binding has to prove is that `define` did not
    define a predicate of its OWN, and `define._is_unit_name is define.features._is_unit_name` is
    exactly that claim. The source check beneath it closes the wrapper route: a function that merely
    CALLED `features._is_unit_name` and added one clause would satisfy neither."""
    assert define._is_unit_name is define.features._is_unit_name
    source = (DEFINE_SCRIPTS / "define.py").read_text(encoding="utf-8")
    assert "re.compile" not in source and "\nimport re" not in source, (
        "this module has grown a pattern of its own -- what a unit may be called is "
        "`features.py`'s single definition, measured against `git check-ref-format`")


def test_the_measured_name_table_still_holds_through_this_module():
    """§5's own table, re-run through this module's entry point rather than through the predicate,
    so a wrapper that added its own extra filter would be caught too. Every row is `git
    check-ref-format`-measured, and the last three are the ones a hand-written rule always gets
    wrong."""
    for good in ("voice-interview", "int-contract", "billing2", "v1.2", "voice.LOCK", "a.lockfile"):
        assert define.validate(good, "feature")[0], good
    for bad in ("a/b", ".hidden", "TBD.", "v1..2", "voice.lock", "", "  ", "-leading"):
        assert not define.validate(bad, "feature")[0], bad


def test_the_marker_composer_and_the_label_projection_are_borrowed_too():
    """Constraint 5, for the other two derived strings. `feature_stamp.marker` builds the two lines
    from `features.BODY_KEY`/`BRANCH_KEY`/`BRANCH_PREFIX`; `feature_labels.label_for` builds the
    label from `features.LABEL_PREFIX`. This module must produce those, not its own spelling."""
    assert define.branch_for("voice") == features.BRANCH_PREFIX + "voice"
    assert define.label_for is define.feature_labels.label_for   # bound, never wrapped
    assert define.label_for("voice") == feature_labels.label_for("voice") == "feature:voice"


# ------------------------------------------------------------------------------- the type is metadata


def test_all_three_types_and_only_three():
    assert define.TYPES == ("feature", "bug", "refactor")
    for kind in define.TYPES:
        assert define.validate("voice", kind)[0], kind
    for kind in ("chore", "epic", "spike", "", "Feature "):
        assert not define.validate("voice", kind)[0], kind


def test_every_type_produces_the_SAME_branch_prefix(tmp_path):
    """§2/§5: the prefix is `feature/` for a feature, a shared bug AND a refactor. Giving a kind a
    branch prefix of its own would break base resolution, which matches on that one prefix -- so the
    branch such a unit owned would be invisible to the very mechanism the unit exists for."""
    seen = set()
    for kind in define.TYPES:
        report, run = _open(tmp_path / kind, name="voice", kind=kind)
        assert report["ok"], report
        assert report["branch"] == "feature/voice"
        seen |= run.branches
    assert seen == {"feature/voice"}


def test_the_type_survives_as_metadata_on_the_label(tmp_path):
    """It is not dropped either -- a type that changed nothing anywhere would be a question asked
    for no reason. It lands in the label's DESCRIPTION, which is metadata's place."""
    report, run = _open(tmp_path, name="voice", kind="refactor")
    create = [a for a in run.calls if a[:3] == ["gh", "label", "create"]][0]
    assert "refactor" in create[create.index("--description") + 1]
    assert report["kind"] == "refactor"


# ------------------------------------------------------------------------------- the step order


def test_the_step_order_is_declared_as_data():
    """§14 fixes it, so it is a constant a reader can check in one line and a test can assert
    directly -- not three statements whose order a reader has to reconstruct."""
    assert define._STEPS == ("branch", "label", "registry")


def test_the_observed_call_order_matches_the_declared_one(tmp_path):
    """The declared order proving nothing on its own is the whole point of this second assertion:
    a constant is only a comment unless the executor is genuinely driven by it."""
    sdlc = _sdlc(tmp_path)
    run = Runner()
    report = define.open_unit(sdlc, "voice", "feature", run=run, cwd=tmp_path)
    assert report["ok"], report
    assert [s["step"] for s in report["steps"]] == list(define._STEPS)
    # git (branch) strictly before gh (label); the registry directory is written by neither.
    kinds = [a[0] for a in run.calls if a[:2] not in (["git", "status"], ["git", "add"], ["git", "commit"])
             and a[:3] != ["gh", "api", "user"] and a[:3] != ["git", "remote", "get-url"]]
    assert kinds.index("gh") > max(i for i, k in enumerate(kinds) if k == "git")   # #1312: the record commit is last
    assert feature_registry.registry_dir(sdlc).is_dir()


def test_a_failing_BRANCH_step_never_reaches_the_label_or_the_registry(tmp_path):
    """The measured bug, from the top. A label created for a unit whose branch does not exist is a
    label that outlives nothing -- and feature labels are never deleted, so there is no cleanup
    path."""
    sdlc = _sdlc(tmp_path)
    run = Runner(fail_on=lambda a: "no such remote" if a[:2] == ["git", "push"] else None)
    report = define.open_unit(sdlc, "voice", "feature", run=run, cwd=tmp_path)
    assert not report["ok"]
    assert [s["step"] for s in report["steps"]] == ["branch"]
    assert report["steps"][0]["outcome"] == define.FAILED
    assert "gh" not in run.kinds()
    assert not feature_registry.registry_dir(sdlc).is_dir()


def test_a_failing_LABEL_step_never_reaches_the_registry(tmp_path):
    """The other direction, and the one an implementation that ran every step and reported failures
    at the end would fail. Creating the registry directory is what makes the registry-backed half
    LIVE for the whole repository (§14); doing it on the way out of a half-opened unit would arm
    every registry consumer on the strength of a gesture that did not complete."""
    sdlc = _sdlc(tmp_path)
    run = Runner(fail_on=lambda a: "bad token" if a[:3] == ["gh", "label", "create"] else None)
    report = define.open_unit(sdlc, "voice", "feature", run=run, cwd=tmp_path)
    assert not report["ok"]
    assert [s["step"] for s in report["steps"]] == ["branch", "label"]
    assert not feature_registry.registry_dir(sdlc).is_dir()


def test_a_refused_name_runs_no_step_at_all(tmp_path):
    sdlc = _sdlc(tmp_path)
    run = Runner()
    report = define.open_unit(sdlc, "a/b", "feature", run=run, cwd=tmp_path)
    assert not report["ok"] and report["steps"] == [] and run.calls == []
    assert "a/b" in report["why"]
    assert not feature_registry.registry_dir(sdlc).is_dir()


def test_a_refused_TYPE_runs_no_step_at_all(tmp_path):
    run = Runner()
    report = define.open_unit(_sdlc(tmp_path), "voice", "chore", run=run, cwd=tmp_path)
    assert not report["ok"] and report["steps"] == [] and run.calls == []


# ------------------------------------------------------------------------------- idempotency


def test_an_existing_remote_branch_is_not_pushed_over(tmp_path):
    """Opening a unit twice must be safe: the second run force-pushing `main` over a feature branch
    that already carries work is the destructive shape this has to not have."""
    run = Runner(branches={"feature/voice"})
    report, _ = _open(tmp_path, name="voice", run=run)
    assert report["ok"]
    assert report["steps"][0]["outcome"] == define.EXISTS
    assert not [a for a in run.calls if a[:2] == ["git", "push"]]


def test_an_existing_label_is_not_created_again(tmp_path):
    run = Runner(labels={"feature:voice"})
    report, _ = _open(tmp_path, name="voice", run=run)
    assert report["ok"]
    assert report["steps"][1]["outcome"] == define.EXISTS
    assert not [a for a in run.calls if a[:3] == ["gh", "label", "create"]]


def test_a_label_that_only_LOOKS_like_ours_does_not_count_as_ours(tmp_path):
    """`gh label list --search` matches substrings, so `feature:voicemail` comes back for a search
    of `feature:voice`. Reading that as "the label exists" would leave the real one uncreated and
    every issue this flow then filed refused at pick."""
    run = Runner(labels={"feature:voicemail"})
    report, _ = _open(tmp_path, name="voice", run=run)
    assert report["ok"]
    assert report["steps"][1]["outcome"] == define.CREATED
    assert "feature:voice" in run.labels


def test_the_existence_check_enumerates_rather_than_searching(tmp_path):
    """MEASURED ON THE LIVE REPO, and it is the reason this is a test rather than a preference.

    `gh label list --search feature:define-smoke-1662`, run immediately after that label was
    created, returned `["enhancement"]` -- an unrelated label, and not the one just created. A plain
    `gh label list` returned it on the first call. `--search` is an eventually-consistent index over
    names AND descriptions; the existence check needs a lookup.

    Read as "absent", that lag makes the create run against a label that already exists, which `gh`
    fails -- so the step fails and the whole gesture stops on a unit that was already correctly
    open."""
    run = Runner()
    define.open_unit(_sdlc(tmp_path), "voice", "feature", run=run, cwd=tmp_path)
    lists = [a for a in run.calls if a[:3] == ["gh", "label", "list"]]
    assert lists and all("--search" not in a for a in lists), lists
    assert any(a[a.index("--limit") + 1] == str(define._LABEL_PAGE) for a in lists)


def test_a_create_that_finds_the_label_already_there_is_EXISTS_not_a_failure(tmp_path):
    """The residual race the enumeration narrows but cannot close: another actor creates the label
    between the list and the create, or the repository carries more labels than the enumeration's
    own `--limit`. Both end at `gh` refusing the create for a label that IS there -- which is the
    same answer the list would have given, learned one call later.

    Failing the step for that would stop the gesture on a unit that is correctly open, and would
    stop it BEFORE the registry step, so the report would also be wrong about what the repository
    now has."""
    run = Runner(fail_on=lambda a: (
        'label with name "feature:voice" already exists; use `--force` to update its color'
        if a[:3] == ["gh", "label", "create"] else None))
    report, _ = _open(tmp_path, name="voice", run=run)
    assert report["ok"]
    assert report["steps"][1]["outcome"] == define.EXISTS
    assert [s["step"] for s in report["steps"]] == list(define._STEPS)   # the registry step ran


def test_a_create_that_fails_for_ANY_OTHER_reason_still_stops_the_gesture(tmp_path):
    """The tolerance above is scoped to one measured message. A permissions failure, a rate limit or
    a bad token must not be read as "the label is already there" -- that would report a unit as open
    while every issue it goes on to file is refused at pick."""
    sdlc = _sdlc(tmp_path)
    run = Runner(fail_on=lambda a: ("HTTP 403: Resource not accessible by integration"
                                    if a[:3] == ["gh", "label", "create"] else None))
    report = define.open_unit(sdlc, "voice", "feature", run=run, cwd=tmp_path)
    assert not report["ok"]
    assert report["steps"][-1]["outcome"] == define.FAILED
    assert not feature_registry.registry_dir(sdlc).is_dir()


def test_a_DIFFERENT_CASING_of_our_label_is_refused_rather_than_created(tmp_path):
    """GitHub label names are case-insensitively unique, so the create would fail anyway -- but a
    refusal that NAMES the existing spelling is what stops a second casing entering the system on
    the retry. Two casings of one unit is #1638's remaining exposure, and this is the door it would
    come through."""
    sdlc = _sdlc(tmp_path)
    run = Runner(labels={"feature:Voice"})
    report = define.open_unit(sdlc, "voice", "feature", run=run, cwd=tmp_path)
    assert not report["ok"]
    assert report["steps"][-1]["outcome"] == define.REFUSED

    # #1673: THE WORDING IS PINNED, NOT JUST THE OUTCOME. This message carried "three derived keys"
    # long after two of them were folded, and it survived two sweeps of the fix precisely because
    # it is a runtime string with no assertion on it -- the only site of that stale count that a
    # USER could read. Pinning the shape is what makes a fourth one fail here rather than ship.
    detail = str(report["steps"][-1].get("detail", ""))
    assert "three" not in detail, (
        "the refusal is counting the derived keys again; the count belongs to the inventory in "
        "tests/test_feature_registry.py, not to a sentence a user reads: %s" % detail)
    assert "feature:Voice" in report["steps"][-1]["detail"]
    assert not [a for a in run.calls if a[:3] == ["gh", "label", "create"]]
    assert not feature_registry.registry_dir(sdlc).is_dir()


def test_an_existing_registry_directory_is_reported_not_recreated(tmp_path):
    sdlc = _sdlc(tmp_path)
    feature_registry.registry_dir(sdlc).mkdir(parents=True)
    report = define.open_unit(sdlc, "voice", "feature", run=Runner(), cwd=tmp_path)
    assert report["ok"] and report["steps"][2]["outcome"] == define.EXISTS


# ------------------------------------------------------------------------------- the base


def test_the_base_precedence_is_the_loops_own(tmp_path):
    """Explicit, else `work.base`, else the branch we are on -- exactly `sigma-loop/SKILL.md`'s own
    precedence for a goal's base, borrowed rather than invented, so a repo does not have to hold two
    different answers to "based on what?"."""
    run = Runner(head="develop")
    define.open_unit(_sdlc(tmp_path / "a"), "u1", "feature", run=run, cwd=tmp_path,
                     base="release/2")
    assert [a for a in run.calls if a[:2] == ["git", "push"]][0][-1].startswith("f" * 40 + ":")
    assert ["git", "fetch", "origin", "release/2"] in run.calls

    run = Runner(head="develop")
    define.open_unit(_sdlc(tmp_path / "b"), "u2", "feature", run=run, cwd=tmp_path,
                     config={"work": {"base": "integration"}})
    assert [a for a in run.calls if a[:2] == ["git", "push"]][0][-1].startswith("f" * 40 + ":")
    assert ["git", "fetch", "origin", "integration"] in run.calls

    run = Runner(head="develop")
    define.open_unit(_sdlc(tmp_path / "c"), "u3", "feature", run=run, cwd=tmp_path, config={})
    assert [a for a in run.calls if a[:2] == ["git", "push"]][0][-1].startswith("f" * 40 + ":")
    assert ["git", "fetch", "origin", "develop"] in run.calls


def test_a_detached_checkout_with_no_base_is_REFUSED_rather_than_guessed(tmp_path):
    """`git rev-parse --abbrev-ref HEAD` answers the literal string `HEAD` in a detached checkout,
    and `HEAD:refs/heads/feature/x` is a push git would happily accept -- which is exactly why this
    has to refuse rather than fall through.

    A detached checkout means nobody chose a base: it is a worktree mid-rebase, a checked-out tag, a
    bisect. Opening a unit on whatever commit that happens to be, under a name a human will then
    treat as the unit's trunk, is a wrong answer that looks like a right one forever afterwards. The
    fix is one flag, so asking for it costs nothing."""
    for head in ("HEAD", ""):
        run = Runner(head=head)
        report = define.open_unit(_sdlc(tmp_path / ("h" + head)), "voice", "feature",
                                  run=run, cwd=tmp_path, config={})
        assert not report["ok"], head
        assert report["steps"][0]["outcome"] == define.REFUSED
        assert not [a for a in run.calls if a[:2] == ["git", "push"]]


def test_an_explicit_base_makes_a_detached_checkout_fine(tmp_path):
    """The refusal above is about a base nobody chose, never about the checkout's shape -- a
    detached worktree with `--base` named is an ordinary, correct way to open a unit."""
    run = Runner(head="HEAD")
    report = define.open_unit(_sdlc(tmp_path), "voice", "feature", run=run, cwd=tmp_path,
                              base="main")
    assert report["ok"]
    assert [a for a in run.calls if a[:2] == ["git", "push"]][0][-1] == \
        "f" * 40 + ":refs/heads/feature/voice"


def test_an_empty_configured_base_falls_through_rather_than_pushing_an_empty_ref(tmp_path):
    """`sigma-init`'s own template ships `work.base: ""`, which MEANS "the branch the loop was
    started on" -- so treating the empty string as a base would push `:refs/heads/feature/x`, which
    git reads as a DELETE."""
    run = Runner(head="develop")
    define.open_unit(_sdlc(tmp_path), "voice", "feature", run=run, cwd=tmp_path,
                     config={"work": {"base": "   "}})
    pushed = [a for a in run.calls if a[:2] == ["git", "push"]][0][-1]
    assert pushed == "f" * 40 + ":refs/heads/feature/voice"


# ------------------------------------------------------------------------------- the authorized exception


def test_the_label_create_is_exactly_the_shape_the_kit_refuses_everywhere_else():
    """THE ONE RULE THIS MODULE IS ALLOWED TO BREAK, pinned mechanically.

    `sources.GitHubSource._run` refuses `gh label create` carrying a `feature:*` argument at the
    single chokepoint every `gh` call in that class passes through, and RETURNS `""` -- the SUCCESS
    shape. So routing this step through that class would report a created label for one that does
    not exist, and every issue the flow then filed would be refused at pick with `sdlc:needs-label`:
    the measured §14 bug, arrived at from the opposite direction.

    Feeding this module's own argv to the kit's own refusal predicate proves two things at once:
    that the call really is the refused shape (so the exception is real, not notional), and that the
    two descriptions cannot drift apart without this failing."""
    argv = define._label_create_argv("voice", "feature")
    assert argv[0] == "gh"
    assert feature_labels.creates_a_feature_label(argv[1:])


def test_nothing_in_this_module_opens_an_issue_itself():
    """Issues come from `compile_plan` -> `GitHubSource.create_dependency`, which
    `tests/test_issue_creation_boundary.py` pins as the one place this repo opens an issue. This
    module supplies the unit; it must never grow a second filing path."""
    text = (DEFINE_SCRIPTS / "define.py").read_text(encoding="utf-8")
    assert '"issue", "create"' not in text and "'issue', 'create'" not in text


# ------------------------------------------------------------------------------- stamping


def _plan():
    return {"epic": {"key": "e", "title": "Voice interview", "body": "The parent."},
            "issues": [{"key": "a", "title": "One", "body": "Do the thing.", "priority": "P1"},
                       {"key": "b", "title": "Two", "body": "Do the other.", "priority": "P2"}]}


def test_every_issue_and_the_epic_come_back_declaring_the_unit():
    """Constraint 4, checked through the REAL reader rather than by looking for the two lines: what
    matters is not that the marker is present, it is that `features.parse_body` reads it."""
    out, warnings = define.stamp_plan(_plan(), "voice-interview")
    assert warnings == []
    assert features.parse_body(out["epic"]["body"]) == "voice-interview"
    for item in out["issues"]:
        assert features.parse_body(item["body"]) == "voice-interview"


def test_the_marker_is_bare_and_is_the_composer_the_kit_already_ships():
    """§4a: at the left margin, its own line, no fence, no indent, no list marker, no blockquote.
    Asserted against `feature_stamp.marker`'s output rather than against a re-typed literal -- a
    literal here would be the second definition constraint 5 forbids, and it would be in exactly
    the place the two would silently drift."""
    out, _ = define.stamp_plan(_plan(), "voice-interview")
    block = feature_stamp.marker("voice-interview")
    for line in block.split("\n"):
        assert ("\n" + line + "\n") in ("\n" + out["issues"][0]["body"] + "\n")
        assert not line.startswith((" ", "\t", ">", "-"))


def test_the_callers_plan_is_never_mutated():
    """The caller writes the plan to `.sdlc/plans/scope/<slug>.plan.json` and hands the SAME file to
    `assign.py execute` afterwards, so a plan mutated in place would put the marker into an artifact
    a later step re-reads as the human's own text."""
    original = _plan()
    before = original["issues"][0]["body"]
    define.stamp_plan(original, "voice-interview")
    assert original["issues"][0]["body"] == before


def test_a_body_already_declaring_a_RIVAL_unit_is_left_exactly_as_written():
    """`feature_stamp`'s rule 2, inherited: a second declaration is the one state nothing downstream
    can resolve. The body is untouched AND the caller is told."""
    plan = _plan()
    plan["issues"][0]["body"] = "Feature: billing\nBranch: feature/billing\n\nDo the thing."
    out, warnings = define.stamp_plan(plan, "voice-interview")
    assert out["issues"][0]["body"] == plan["issues"][0]["body"]
    assert features.parse_body(out["issues"][0]["body"]) == "billing"
    assert any("billing" in w for w in warnings)


def test_a_body_already_declaring_the_SAME_unit_is_not_declared_twice():
    plan = _plan()
    plan["issues"][0]["body"] = feature_stamp.marker("voice-interview") + "\n\nDo the thing."
    out, warnings = define.stamp_plan(plan, "voice-interview")
    assert out["issues"][0]["body"].count(features.BODY_KEY + ":") == 1
    assert warnings == []


def test_a_body_ending_inside_an_unterminated_fence_still_reads_back():
    """The silent case, and the reason the writer verifies through the reader. `_visible` blanks an
    unterminated fence TO EOF, so an appended marker is invisible -- `stamp_body` falls back to the
    top of the body, where no construct can yet be open."""
    plan = _plan()
    plan["issues"][0]["body"] = "Here is how it looks:\n\n```\nsome example\n"
    out, warnings = define.stamp_plan(plan, "voice-interview")
    assert features.parse_body(out["issues"][0]["body"]) == "voice-interview"
    assert warnings == []


def test_a_plan_with_no_epic_is_stamped_anyway():
    plan = _plan()
    plan["epic"] = None
    out, warnings = define.stamp_plan(plan, "voice-interview")
    assert warnings == []
    assert all(features.parse_body(i["body"]) == "voice-interview" for i in out["issues"])


# ------------------------------------------------------------------------------- declaring


class FakeSource:
    """The three-method surface `feature_labels.attach_at_pick` duck-types on, minus the ones this
    path does not use. `attach_label` NEVER creates -- it refuses a label the repo does not carry,
    exactly as `GitHubSource._swap_labels` raises `unknown label(s) on this repo`."""

    def __init__(self, bodies, labels=(), repo_labels=()):
        self.bodies = dict(bodies)
        self.labels = {k: list(v) for k, v in dict(labels).items()}
        self.repo_labels = set(repo_labels)
        self.attached = []

    def attach_label(self, goal, name):
        if name not in self.repo_labels:
            raise RuntimeError("unknown label(s) on this repo: %s" % name)
        self.attached.append((goal, name))
        self.labels.setdefault(goal, []).append(name)
        return True

    def fetch_body_labels(self, goal):
        return {"body": self.bodies[goal],
                "labels": [{"name": n} for n in self.labels.get(goal, [])]}


def _stamped(unit="voice-interview"):
    return feature_stamp.marker(unit) + "\n\nDo the thing."


def test_declare_attaches_the_label_and_verifies_through_the_real_reader():
    src = FakeSource({1: _stamped(), 2: _stamped()}, repo_labels={"feature:voice-interview"})
    report = define.declare(src, "voice-interview", [1, 2])
    assert report["ok"], report
    assert src.attached == [(1, "feature:voice-interview"), (2, "feature:voice-interview")]
    assert [r["state"] for r in report["issues"]] == [features.AGREE, features.AGREE]
    assert all(r["outcome"] == define.DECLARED for r in report["issues"])


def test_declare_attaches_nothing_but_the_unit_label():
    src = FakeSource({1: _stamped()}, repo_labels={"feature:voice-interview"})
    define.declare(src, "voice-interview", [1])
    assert {n for _, n in src.attached} == {"feature:voice-interview"}


def test_declare_reports_an_unattachable_label_rather_than_creating_it():
    """The never-create rule holds here BY CONSTRUCTION: `attach_label` resolves the name first and
    raises rather than minting. This module's job on that raise is to say so, not to reach for the
    create it is allowed to make at `open_unit` time -- by then the issues exist and a label minted
    from a body would be minted from text a human may have edited."""
    src = FakeSource({1: _stamped()}, repo_labels=set())
    report = define.declare(src, "voice-interview", [1])
    assert not report["ok"]
    assert report["issues"][0]["outcome"] == define.FAILED
    assert not report["issues"][0]["attached"]


def test_declare_records_a_CASE_DRIFT_rather_than_reporting_agreement():
    """`features.read` compares the two halves case-INSENSITIVELY and returns the BODY's spelling,
    so a body saying `Voice-Interview` against label `feature:voice-interview` is a legitimate
    `agree`. It is still a second casing of one unit -- one thing under two names, which is what
    every derived key had to be taught to fold away (#1638, closed by #1672 and #1673). This is the
    door it would enter, so this is where it is named."""
    src = FakeSource({1: _stamped("Voice-Interview")}, repo_labels={"feature:voice-interview"})
    report = define.declare(src, "voice-interview", [1])
    assert not report["ok"]
    assert report["issues"][0]["state"] == features.AGREE
    assert report["issues"][0]["outcome"] == define.CASE_DRIFT
    assert report["issues"][0]["unit"] == "Voice-Interview"


def test_declare_reports_an_issue_that_never_got_its_marker():
    src = FakeSource({1: "no marker at all"}, repo_labels={"feature:voice-interview"})
    report = define.declare(src, "voice-interview", [1])
    assert not report["ok"]
    assert report["issues"][0]["state"] == features.LABEL_ONLY
    assert report["issues"][0]["outcome"] == define.NOT_DECLARED


def test_one_self_contradicting_issue_does_not_stop_the_others():
    """`features.read`'s own stated obligation on every caller that sweeps: catch `AmbiguousUnit`
    PER ISSUE, treat that one as unresolvable, carry on. A sweep that let it propagate would turn
    one hand-edited issue into a total outage of the flow."""
    src = FakeSource({1: "Feature: voice-interview\nFeature: billing\n", 2: _stamped()},
                     repo_labels={"feature:voice-interview"})
    report = define.declare(src, "voice-interview", [1, 2])
    assert not report["ok"]
    assert report["issues"][0]["outcome"] == define.AMBIGUOUS
    assert report["issues"][1]["outcome"] == define.DECLARED


def test_declare_does_not_TRUST_an_attach_that_says_it_did_not_land():
    """`attach_label` returns True iff the write actually landed, and a False is not an exception --
    `_swap_labels_best_effort` retries, then fails loudly WITHOUT raising, exactly as every other
    lifecycle write in that class does.

    So a report built on the return value alone would call a lost write a declaration. Everything
    here is decided by READING THE ISSUE BACK instead, which is why this comes out `not-declared`
    rather than `declared`: the label genuinely is not on the issue, whatever the write said."""
    class Silent(FakeSource):
        def attach_label(self, goal, name):
            return False                  # the write did not land; nothing raised

    src = Silent({1: _stamped()}, repo_labels={"feature:voice-interview"})
    report = define.declare(src, "voice-interview", [1])
    assert not report["ok"]
    assert report["issues"][0]["attached"] is False
    assert report["issues"][0]["state"] == features.BODY_ONLY
    assert report["issues"][0]["outcome"] == define.NOT_DECLARED


def test_declare_degrades_on_a_source_without_the_label_surface():
    """`LocalSource` deliberately does not define these three methods, so the whole label half must
    degrade to a stated no-op rather than raise -- the same posture `feature_labels.attach_at_pick`
    takes for the same reason."""
    class Bare:
        pass
    report = define.declare(Bare(), "voice-interview", [1])
    assert report["outcome"] == define.NOT_SUPPORTED
    assert not report["ok"] and report["why"]


def test_declare_over_no_issues_is_not_a_success():
    """Vacuous truth is the wrong answer here: "every issue declares the unit" over zero issues
    would report a unit that nothing belongs to as fully declared."""
    src = FakeSource({}, repo_labels={"feature:voice-interview"})
    assert not define.declare(src, "voice-interview", [])["ok"]


# ---------------------------------------------------------------- recording a unit's OWN priority (#2266)
#
# #2266 (epic #2260, slice 6): THIS is now the answer to "prioritise this feature" -- one value on
# the unit's own registry entry, never a write to any member issue. `set_priority` goes through
# `feature_sync.amend`, exactly the write surface #2261 built and proved
# (`tests/test_feature_sync.py::test_amend_is_the_write_surface_for_a_units_priority`) but never
# wired a CLI verb onto; these tests are that wiring's own coverage, hermetic and local -- no `gh`,
# no network, no injected runner, because the write never leaves the filesystem.


def _priority_sdlc(tmp_path):
    """A `.sdlc` with the registry directory present -- `set_priority` needs `.sdlc/features/` to
    exist the same way any other registry write does; `_sdlc` alone does not create it."""
    sdlc = _sdlc(tmp_path)
    (sdlc / "features").mkdir(parents=True, exist_ok=True)
    return sdlc


def _seed_unit(sdlc, name, **entry):
    """Put a unit in the registry through the real write surface, the way `open_unit`'s own
    registry step would have -- never a hand-written JSON file, so these tests exercise the same
    shard shape a live pick would leave behind."""
    base = {"title": "Voice interview", "owner": "@unit-owner", "open": True, "parent": None,
            "tracking_issue": None, "priority": None, "repos": {}}
    base.update(entry)
    return feature_registry.write_unit(sdlc / "features", name, base)


def test_set_priority_rejects_a_bad_priority_before_touching_the_registry(tmp_path):
    """Step 1, mirroring `bump_priority`'s own: `discovery.priority_rank` is the one validator, and
    a value it cannot resolve is refused before any read or write -- proven by NOT seeding a unit
    at all, so a write that reached the registry would create a bogus entry rather than merely
    mutate one, and the assertion on `_read` below would catch either."""
    sdlc = _priority_sdlc(tmp_path)
    for bad in ("P9", "critical", "", "p"):
        report = define.set_priority(str(sdlc), "voice", bad)
        assert not report["ok"], bad
        assert repr(bad) in report["why"], bad
        assert report["changed"] is None and report["written"] is None
    assert feature_registry.read(sdlc / "features") == {}


def test_set_priority_accepts_every_real_tier_case_insensitively(tmp_path):
    sdlc = _priority_sdlc(tmp_path)
    for tier in ("P0", "p1", "P2", "P3", "P4"):
        _seed_unit(sdlc, "voice")
        report = define.set_priority(str(sdlc), "voice", tier)
        assert report["ok"], (tier, report)
        assert report["canon"] == tier.upper()
        assert feature_registry.read_unit(sdlc / "features", "voice")["priority"] == tier.upper()


def test_set_priority_writes_through_amend_never_a_second_write_path(tmp_path):
    """The deliverable: the registry entry's OTHER fields survive untouched, on disk, in the shard
    the write surface owns -- not merely in the report. A one-field write (bypassing `amend`'s
    read-modify-write) would erase `repos`/`tracking_issue` exactly as
    `tests/test_feature_sync.py::test_amend_is_the_write_surface_for_a_units_priority` warns."""
    sdlc = _priority_sdlc(tmp_path)
    _seed_unit(sdlc, "voice", tracking_issue="org/repo#3100",
              repos={"org/repo": {"branch": "feature/voice", "owner": "@unit-owner",
                                  "authorized": True, "goals": [42]}})
    report = define.set_priority(str(sdlc), "voice", "P0")
    assert report["ok"] and report["changed"] is True and report["written"] is True
    entry = feature_registry.read_unit(sdlc / "features", "voice")
    assert entry["priority"] == "P0"
    assert entry["tracking_issue"] == "org/repo#3100"
    assert entry["repos"]["org/repo"]["goals"] == [42]
    shard = __import__("json").loads(
        (sdlc / "features" / "units" / "voice.json").read_text(encoding="utf-8"))
    assert shard["features"]["voice"]["priority"] == "P0"


def test_set_priority_never_touches_a_member_issues_own_priority_label(tmp_path):
    """The structural difference from `bump_priority`: nothing here can reach a `gh` call at all --
    the function signature does not even take a `source`. A GitHub-backed board's member issues are
    unreachable by construction, not merely unaffected in this one test's fixture."""
    import inspect
    params = list(inspect.signature(define.set_priority).parameters)
    assert "source" not in params and "cap" not in params
    assert params == ["sdlc_dir", "unit", "priority"]


def test_set_priority_reports_no_change_when_the_priority_is_already_that_value(tmp_path):
    """Same `amend`-level guarantee `test_amend_reports_no_change_when_the_priority_written_is_the_
    one_already_there` pins directly: re-stating a unit's current priority costs no write, so the
    registry directory stays out of the git history of a re-run."""
    sdlc = _priority_sdlc(tmp_path)
    _seed_unit(sdlc, "voice", priority="P1")
    before = (sdlc / "features" / "units" / "voice.json").read_bytes()
    report = define.set_priority(str(sdlc), "voice", "P1")
    assert report["ok"] and report["changed"] is False and report["written"] is True
    assert (sdlc / "features" / "units" / "voice.json").read_bytes() == before


def test_the_cli_set_priority_verb_rejects_a_bad_priority_without_touching_the_registry(tmp_path,
                                                                                        capsys):
    sdlc = _priority_sdlc(tmp_path)
    rc = define.main(["define.py", "set-priority", str(sdlc), "--unit", "voice",
                      "--priority", "urgent"])
    report = __import__("json").loads(capsys.readouterr().out.strip())
    assert rc == 1 and "urgent" in report["why"]
    assert feature_registry.read(sdlc / "features") == {}


def test_the_cli_set_priority_verb_records_it_on_the_unit(tmp_path, capsys):
    sdlc = _priority_sdlc(tmp_path)
    _seed_unit(sdlc, "voice")
    rc = define.main(["define.py", "set-priority", str(sdlc), "--unit", "voice",
                      "--priority", "P2"])
    report = __import__("json").loads(capsys.readouterr().out.strip())
    assert rc == 0 and report["ok"]
    assert feature_registry.read_unit(sdlc / "features", "voice")["priority"] == "P2"


# ------------------------------------------------------------------------------- bumping priority (#2162)


sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import gqlfake  # noqa: E402


class PriorityRunner:
    """A `gh` stand-in modelling only the two calls `bump_priority` can ever issue through a REAL
    `sources.GitHubSource`: the REST issues-list fetch (`fetch_issues_rest`'s own shape) and the
    priority label edit (`_write_priority_label`'s own shape) -- both the KIT'S OWN code, never a
    re-implementation of either. Any other call is a bug (a blocker walk, a comment, a second write
    path) and fails loudly rather than being silently modelled away."""

    def __init__(self, issues, fail_edit_for=()):
        self.issues = list(issues)
        self.fail_edit_for = set(fail_edit_for)
        self.calls = []

    def __call__(self, args):
        import json as _json
        args = [str(a) for a in args]
        self.calls.append(args)
        if args and args[0] == "api" and args[1].endswith("/issues"):
            return _json.dumps(self.issues)
        if gqlfake.is_issue_write(args):         # #895 slice 3a: the label write is REST (add, then removes)
            n = int(gqlfake.rest_write_target(args)[2])
            if n in self.fail_edit_for:
                raise RuntimeError("HTTP 500: server error")
            return gqlfake.rest_write(args, repo_args=())
        raise AssertionError("PriorityRunner was handed a call bump_priority must never make: %r"
                             % (args,))

    def edits(self):
        """The legacy-shaped `issue edit <n> --add-label|--remove-label X` record of each REST label write,
        in the order they were made."""
        return [l for c in self.calls for l in gqlfake.legacy_of(c, repo_args=())]


def _gh_source(issues, **kw):
    run = PriorityRunner(issues, **kw)
    src = sources_mod.GitHubSource({"discovery": {"source": "github"}}, run=run)
    return src, run


def test_bump_priority_rejects_a_bad_priority_before_touching_the_source():
    """Step 1 of #2162: `discovery.priority_rank` is the ONE validator, and a value it cannot
    resolve to a real tier is refused before any read or write -- not guessed at, not normalised."""
    class Explodes:
        repo = "o/r"
        def _run(self, args):
            raise AssertionError("a bad priority must never reach a read")
        def _write_priority_label(self, *a, **k):
            raise AssertionError("a bad priority must never reach a write")

    for bad in ("P9", "critical", "", "p"):
        report = define.bump_priority(Explodes(), "voice", bad)
        assert not report["ok"], bad
        assert report["issues"] == [], bad
        assert repr(bad) in report["why"], bad


def test_bump_priority_accepts_every_real_tier_case_insensitively():
    for tier in ("P0", "p1", "P2", "P3", "P4"):
        src, _ = _gh_source([])
        report = define.bump_priority(src, "voice", tier)
        assert report["ok"], (tier, report)
        assert report["canon"] == tier.upper()


def test_bump_priority_enumerates_members_via_the_shared_fetch_primitive():
    """#1833's own primitive, called with the ONE thing this slice adds -- the `feature:<unit>`
    label -- not a second, hand-rolled `gh issue list` path. Monkeypatches the exact module-level
    function `define.py` is required to reuse directly, so this pins the call shape rather than
    just the eventual outcome."""
    calls = []

    def fake_fetch(run, repo, labels, cap, **kw):
        calls.append((repo, labels, cap))
        return [{"number": 3, "labels": [{"name": "feature:voice"}]}]

    class Src:
        repo = "o/r"
        proposed_label = "sdlc:needs-confirmation"
        priority_prefix = "priority:"
        priority_aliases = {}
        def _run(self, args):
            return ""
        def _write_priority_label(self, n, canon, labels):
            self.written = (n, canon)

    real_fetch = define.sources.fetch_issues_rest
    define.sources.fetch_issues_rest = fake_fetch
    try:
        report = define.bump_priority(Src(), "voice", "P2")
    finally:
        define.sources.fetch_issues_rest = real_fetch
    assert calls == [("o/r", ["feature:voice"], define._MEMBER_FETCH_CAP)]
    assert [r["issue"] for r in report["issues"]] == [3]


def test_bump_priority_excludes_a_needs_confirmation_member_without_writing_to_it():
    """Step 3: `sdlc:needs-confirmation` (#233's human-approval gate) means the issue is not yet a
    confirmed member of the cluster -- excluded from the bump entirely, and NEVER reaches the write
    chokepoint (the fake runner would raise on an unmodelled call if it did)."""
    issues = [
        {"number": 1, "labels": [{"name": "feature:voice"}, {"name": "sdlc:needs-confirmation"}]},
        {"number": 2, "labels": [{"name": "feature:voice"}]},
    ]
    src, run = _gh_source(issues)
    report = define.bump_priority(src, "voice", "P1")
    by_issue = {r["issue"]: r["outcome"] for r in report["issues"]}
    assert by_issue[1] == define.EXCLUDED_NEEDS_CONFIRMATION
    assert by_issue[2] == define.BUMPED
    assert [c[2] for c in run.edits()] == ["2"]      # #1 never got an edit call at all


def test_bump_priority_writes_the_exact_match_through_the_real_chokepoint():
    """Step 4: the write is `_write_priority_label` (sources.py's OWN "one place a priority label
    is ever written") -- proven here by exercising a REAL `GitHubSource`, not a hand-rolled fake
    that merely claims to be the chokepoint. Removes the stale `priority:P3` and adds `priority:P1`
    in the SAME call, exactly as that chokepoint already does for `_promote_blockers`."""
    issues = [{"number": 7, "labels": [{"name": "feature:voice"}, {"name": "priority:P3"}]}]
    src, run = _gh_source(issues)
    report = define.bump_priority(src, "voice", "P1")
    add, remove = run.edits()                                # #895 slice 3a: add FIRST, then the stale remove
    assert add[2] == "7" and remove[2] == "7"
    assert add[add.index("--add-label") + 1] == "priority:P1"
    assert remove[remove.index("--remove-label") + 1] == "priority:P3"
    assert report["issues"][0]["outcome"] == define.BUMPED
    assert report["issues"][0]["before"] == "P3"


def test_bump_priority_reports_already_at_or_above_but_still_writes_unconditionally():
    """The one place #2162 explicitly diverges from `_promote_blockers`'s own `min()` rule: this
    routine is exact-match, ALWAYS -- even when the member already carries a MORE urgent priority
    than the feature's own value. The write still happens (the edit call is still observed); only
    the reported OUTCOME distinguishes "improved" from "already there or better"."""
    issues = [{"number": 9, "labels": [{"name": "feature:voice"}, {"name": "priority:P0"}]}]
    src, run = _gh_source(issues)
    report = define.bump_priority(src, "voice", "P2")
    row = report["issues"][0]
    assert row["outcome"] == define.ALREADY_AT_OR_ABOVE
    assert row["before"] == "P0"
    edit = run.edits()[0]
    assert edit[edit.index("--add-label") + 1] == "priority:P2"   # unconditional: written anyway


def test_bump_priority_reports_a_write_failure_per_issue_without_stopping_the_others():
    """One issue's write failure is one issue's row -- matching `declare`'s own `AmbiguousUnit`
    per-issue isolation -- and makes the whole report not-`ok`, the same convention `declare`
    already uses (`ok` iff every row succeeded)."""
    issues = [{"number": 1, "labels": [{"name": "feature:voice"}]},
              {"number": 2, "labels": [{"name": "feature:voice"}]}]
    src, run = _gh_source(issues, fail_edit_for={1})
    report = define.bump_priority(src, "voice", "P1")
    by_issue = {r["issue"]: r["outcome"] for r in report["issues"]}
    assert by_issue[1] == define.WRITE_FAILED
    assert by_issue[2] == define.BUMPED
    assert report["issues"][0]["why"]
    assert not report["ok"]


def test_bump_priority_degrades_on_a_source_with_no_priority_label_surface(tmp_path):
    """`LocalSource` has frontmatter priority, never a priority LABEL -- there is no chokepoint to
    route through, so this must degrade to one clear report row, exactly like `declare` does for
    the same source."""
    src = sources_mod.LocalSource(str(tmp_path))
    report = define.bump_priority(src, "voice", "P1")
    assert not report["ok"]
    assert report["outcome"] == define.NOT_SUPPORTED
    assert report["why"]


def test_bump_priority_never_walks_blockers_or_touches_anything_but_the_two_calls():
    """A structural control for the epic's own hard boundary: this routine enumerates and writes
    ONLY the feature's direct members. `PriorityRunner` raises on any call shape it does not
    model, so a blocker-walk, a comment post, or a second write path would fail this test loudly
    rather than silently pass."""
    issues = [{"number": 1, "labels": [{"name": "feature:voice"}]}]
    src, run = _gh_source(issues)
    define.bump_priority(src, "voice", "P1")
    assert [c[0] for c in run.calls] == ["api", "api"]       # the list read, then ONE REST label POST
    assert run.calls[1][3] == "POST" and run.calls[1][1].endswith("/issues/1/labels")


def test_define_py_never_references_blocker_walking_machinery():
    """#2162's own stated boundary, pinned mechanically rather than left to review: this module
    must never grow a second copy of `_promote_blockers`. If this fails, stop -- the blocker walk
    belongs to `GitHubSource._promote_blockers` alone.

    Checks the two IMPLEMENTATION names a blocker walk would need (an `extract_refs` call, or
    loading `blocker_scan.py`), not the bare English phrase `blocked_by` -- this module's own
    docstring legitimately DISCUSSES `_promote_blockers` in prose (explaining why it is reused
    rather than duplicated), and a bare substring ban would fail on that explanation itself."""
    text = (DEFINE_SCRIPTS / "define.py").read_text(encoding="utf-8")
    for forbidden in ("extract_refs", "blocker_scan"):
        assert forbidden not in text, forbidden


# ------------------------------------------------------------------------------- the real runner and the CLI


def test_the_default_runner_holds_the_contract_every_sibling_shares():
    """`(cwd, argv) -> stdout`, RAISING on a non-zero exit. Both arms matter and they are not
    symmetric: every caller here reads an exception as "the command did not happen" and an empty
    string as "the answer is nothing". Collapsing them is what turns an unreachable remote into
    "the branch does not exist yet", which would push the base over a live feature branch."""
    import sys as _sys
    assert define._run_default(".", [_sys.executable, "-c", "print('hi')"]) == "hi"
    try:
        define._run_default(".", [_sys.executable, "-c", "import sys; sys.exit(3)"])
    except RuntimeError:
        pass
    else:
        raise AssertionError("a non-zero exit must raise, not return an empty string")


def test_the_note_channel_never_raises():
    """Same shape and same reason as every sibling on this epic: a diagnostic must never be the
    thing that breaks the gesture."""
    define._note("sigma: define: a harmless line\n")


def test_the_cli_open_verb_reports_a_failed_step_rather_than_raising(tmp_path):
    """Driven against a directory that is not a git checkout, so the very first command fails --
    offline, instantly, and without touching any remote. What is under test is that the CLI turns
    that into the documented one-line report with a non-zero exit, never a traceback."""
    import io
    import contextlib
    sdlc = _sdlc(tmp_path)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = define.main(["define.py", "open", str(sdlc), "--name", "voice", "--type", "feature"])
    report = __import__("json").loads(buf.getvalue().strip())
    assert rc == 1 and not report["ok"]
    assert report["steps"][0]["step"] == "branch"
    assert report["steps"][0]["outcome"] == define.FAILED
    assert not feature_registry.registry_dir(sdlc).is_dir()


def test_the_cli_open_verb_refuses_a_bad_name_without_running_anything(tmp_path):
    import io
    import contextlib
    sdlc = _sdlc(tmp_path)
    (sdlc / "config.json").write_text('{"work": {"base": "main"}}', encoding="utf-8")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = define.main(["define.py", "open", str(sdlc), "--name", "a/b", "--type", "feature"])
    report = __import__("json").loads(buf.getvalue().strip())
    assert rc == 1 and report["steps"] == [] and "a/b" in report["why"]


def test_the_cli_open_verb_survives_an_unreadable_config(tmp_path, capsys):
    """A config that will not parse is not a base -- it must degrade to the remaining precedence
    (a `--base`, or the branch we are on) with one stderr line, never take the whole verb down."""
    sdlc = _sdlc(tmp_path)
    (sdlc / "config.json").write_text("{not json", encoding="utf-8")
    rc = define.main(["define.py", "open", str(sdlc), "--name", "voice", "--type", "feature"])
    assert rc == 1                                     # the branch step still fails: no git here
    assert "config.json" in capsys.readouterr().err


def test_the_cli_declare_verb_degrades_on_a_local_backlog(tmp_path, capsys):
    """`LocalSource` has no label surface, so the verb must say `not-supported` and exit non-zero
    rather than raise -- the body marker landed either way, which is the half that matters locally."""
    sdlc = _sdlc(tmp_path)
    (sdlc / "goals").mkdir(exist_ok=True)
    (sdlc / "config.json").write_text(
        '{"discovery": {"source": "local-goals"}, "ledger": {"enabled": false}}', encoding="utf-8")
    rc = define.main(["define.py", "declare", str(sdlc), "--unit", "voice", "--issues", "1,2"])
    report = __import__("json").loads(capsys.readouterr().out.strip())
    assert rc == 1 and report["outcome"] == define.NOT_SUPPORTED and report["why"]


def test_the_cli_prints_usage_for_an_unknown_verb(capsys):
    assert define.main(["define.py"]) == 2
    out = capsys.readouterr().out
    for verb in ("open", "stamp", "declare", "bump-priority"):
        assert "define.py %s" % verb in out


def test_the_cli_bump_priority_verb_degrades_on_a_local_backlog(tmp_path, capsys):
    """`LocalSource` has no label surface -- the verb must say `not-supported` and exit non-zero
    rather than raise, mirroring `test_the_cli_declare_verb_degrades_on_a_local_backlog` exactly."""
    sdlc = _sdlc(tmp_path)
    (sdlc / "goals").mkdir(exist_ok=True)
    (sdlc / "config.json").write_text('{"discovery": {"source": "local-goals"}}', encoding="utf-8")
    rc = define.main(["define.py", "bump-priority", str(sdlc), "--unit", "voice",
                      "--priority", "P1"])
    report = __import__("json").loads(capsys.readouterr().out.strip())
    assert rc == 1 and report["outcome"] == define.NOT_SUPPORTED and report["why"]


def test_the_cli_bump_priority_verb_rejects_a_bad_priority_without_any_gh_call(tmp_path, capsys):
    """Driven against a `github`-configured repo with no injected runner at all -- if this reached
    a real `gh` call it would hang or fail on missing auth; reaching the JSON report instead proves
    the priority is validated before any source method is ever touched."""
    sdlc = _sdlc(tmp_path)
    (sdlc / "config.json").write_text(
        '{"discovery": {"source": "github", "github": {"repo": "o/r"}}}', encoding="utf-8")
    rc = define.main(["define.py", "bump-priority", str(sdlc), "--unit", "voice",
                      "--priority", "urgent"])
    report = __import__("json").loads(capsys.readouterr().out.strip())
    assert rc == 1 and not report["issues"] and "urgent" in report["why"]


def test_the_cli_stamp_verb_reports_a_body_it_could_not_stamp(tmp_path, capsys):
    """Non-zero on a warning, deliberately: a plan compiled from a body that declares a rival unit
    would file an issue nothing downstream can resolve, and the caller has to be made to look."""
    import json as _json
    path = tmp_path / "plan.json"
    path.write_text(_json.dumps({"epic": None, "issues": [
        {"key": "a", "title": "t", "body": "Feature: billing\nBranch: feature/billing"}]}),
        encoding="utf-8")
    rc = define.main(["define.py", "stamp", str(path), "--unit", "voice"])
    report = _json.loads(capsys.readouterr().out.strip())
    assert rc == 1 and not report["ok"] and any("billing" in w for w in report["warnings"])


def test_the_cli_stamp_verb_can_write_somewhere_other_than_the_input(tmp_path, capsys):
    import json as _json
    src = tmp_path / "plan.json"
    dst = tmp_path / "stamped.json"
    src.write_text(_json.dumps({"epic": None, "issues": [
        {"key": "a", "title": "t", "body": "Do it."}]}), encoding="utf-8")
    before = src.read_text(encoding="utf-8")
    rc = define.main(["define.py", "stamp", str(src), "--unit", "voice", "--out", str(dst)])
    capsys.readouterr()
    assert rc == 0
    assert src.read_text(encoding="utf-8") == before      # the input is left exactly as it was
    assert features.parse_body(_json.loads(dst.read_text())["issues"][0]["body"]) == "voice"


# ------------------------------------------------------------------------------- `<name> <priority>` shorthand (#2331)


def test_registered_unit_finds_an_existing_unit_case_insensitively(tmp_path):
    """The shorthand's own existence check, and the door #1638 already watches for every other
    reader of this registry: a human typing the shorthand must not be refused over a spelling that
    differs only in case from the one already on record, and the CANONICAL spelling -- the
    registry's own -- comes back, not the caller's."""
    sdlc = _priority_sdlc(tmp_path)
    _seed_unit(sdlc, "Voice-Interview")
    assert define._registered_unit(str(sdlc), "voice-interview") == "Voice-Interview"
    assert define._registered_unit(str(sdlc), "VOICE-INTERVIEW") == "Voice-Interview"


def test_registered_unit_returns_none_for_an_unknown_name(tmp_path):
    sdlc = _priority_sdlc(tmp_path)
    _seed_unit(sdlc, "voice")
    assert define._registered_unit(str(sdlc), "billing") is None


def test_registered_unit_returns_none_rather_than_raising_on_an_illegal_name(tmp_path):
    """A name `is_unit_name` would reject cannot be a registered one -- answered as `None`,
    matching `read_unit`'s own posture, never as an exception a CLI caller would have to guard."""
    sdlc = _priority_sdlc(tmp_path)
    for bad in ("a/b", "..", "", None, 42):
        assert define._registered_unit(str(sdlc), bad) is None, bad


def test_registered_unit_degrades_on_a_registry_that_does_not_exist_yet(tmp_path):
    """No `.sdlc/features/` at all -- a repository that has never opened a unit -- must answer
    `None` rather than raise, exactly as `feature_registry.read` itself degrades."""
    sdlc = _sdlc(tmp_path)
    assert define._registered_unit(str(sdlc), "voice") is None


def test_priority_shorthand_routes_an_existing_unit_to_set_priority_unchanged(tmp_path):
    """The deliverable: for a unit already in the registry, this is EXACTLY `set_priority`'s own
    report -- the shorthand adds nothing to the found path but the lookup."""
    sdlc = _priority_sdlc(tmp_path)
    _seed_unit(sdlc, "voice")
    got = define.priority_shorthand(str(sdlc), "voice", "P2")
    want = define.set_priority(str(sdlc), "voice", "P2")
    # `set_priority` above already changed the registry to P2, so a THIRD call (idempotent) has to
    # agree in shape with the first -- compare field-for-field rather than the whole dict, since the
    # first call's `changed` is True and the second's is False by construction.
    assert got["ok"] and want["ok"]
    assert set(got) == set(want) == {"unit", "priority", "canon", "ok", "changed", "written",
                                     "landed", "why"}
    assert got["canon"] == want["canon"] == "P2"
    assert feature_registry.read_unit(sdlc / "features", "voice")["priority"] == "P2"


def test_priority_shorthand_routes_case_insensitively_to_the_registered_spelling(tmp_path):
    sdlc = _priority_sdlc(tmp_path)
    _seed_unit(sdlc, "Voice-Interview")
    report = define.priority_shorthand(str(sdlc), "voice-interview", "P0")
    assert report["ok"] and report["canon"] == "P0"
    assert feature_registry.read_unit(sdlc / "features", "Voice-Interview")["priority"] == "P0"


def test_priority_shorthand_refuses_a_name_that_is_not_yet_a_unit_without_writing_anything(tmp_path):
    """The safety rule the issue itself calls out: NEVER silently attempt unit-creation from the
    two-arg shorthand. A bad priority alongside the unknown name does not change the outcome --
    existence is checked first, and the registry is untouched either way."""
    sdlc = _priority_sdlc(tmp_path)
    report = define.priority_shorthand(str(sdlc), "ghost", "P1")
    assert not report["ok"]
    assert "ghost" in report["why"]
    assert "open" in report["why"] and "declare" in report["why"]
    assert report["changed"] is None and report["written"] is None and report["landed"] is None
    assert feature_registry.read(sdlc / "features") == {}


def test_priority_shorthand_refusal_report_matches_set_priority_shape(tmp_path):
    """Both outcomes of the shorthand answer in the SAME shape as `set_priority`'s own report, so a
    caller reading one field off either does not have to branch on which path was taken."""
    sdlc = _priority_sdlc(tmp_path)
    report = define.priority_shorthand(str(sdlc), "ghost", "P1")
    assert set(report) == {"unit", "priority", "canon", "ok", "changed", "written", "landed", "why"}


def test_priority_shorthand_never_calls_open_unit_or_declare(tmp_path):
    """A structural control mirroring `test_define_py_never_references_blocker_walking_machinery`:
    the refusal path for an unknown name must never reach `open_unit` or `declare` -- it prints a
    message directing the human there instead."""
    sdlc = _priority_sdlc(tmp_path)

    def _boom(*a, **k):
        raise AssertionError("priority_shorthand must never open or declare a unit itself")

    real_open, real_declare = define.open_unit, define.declare
    define.open_unit = _boom
    define.declare = _boom
    try:
        report = define.priority_shorthand(str(sdlc), "ghost", "P1")
    finally:
        define.open_unit, define.declare = real_open, real_declare
    assert not report["ok"]


def test_the_cli_shorthand_form_routes_to_set_priority_for_an_existing_unit(tmp_path, capsys):
    sdlc = _priority_sdlc(tmp_path)
    _seed_unit(sdlc, "voice")
    rc = define.main(["define.py", str(sdlc), "voice", "P2"])
    report = __import__("json").loads(capsys.readouterr().out.strip())
    assert rc == 0 and report["ok"]
    assert report["canon"] == "P2"
    assert feature_registry.read_unit(sdlc / "features", "voice")["priority"] == "P2"


def test_the_cli_shorthand_form_refuses_for_a_nonexistent_unit_without_writing(tmp_path, capsys):
    sdlc = _priority_sdlc(tmp_path)
    rc = define.main(["define.py", str(sdlc), "ghost", "P1"])
    report = __import__("json").loads(capsys.readouterr().out.strip())
    assert rc == 1 and not report["ok"]
    assert "ghost" in report["why"]
    assert feature_registry.read(sdlc / "features") == {}


def test_the_cli_shorthand_does_not_shadow_a_recognised_verb_at_the_same_argument_count(tmp_path):
    """Four `argv` entries total -- the same length the shorthand triggers on -- but `argv[1]` is a
    REAL verb (`open`), so this must take the `open` branch exactly as it always did, never be
    reinterpreted as `<sdlc_dir> <name> <priority>`."""
    import io
    import contextlib
    sdlc = _sdlc(tmp_path)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = define.main(["define.py", "open", str(sdlc), "extra"])
    report = __import__("json").loads(buf.getvalue().strip())
    assert rc == 1
    assert "steps" in report and "canon" not in report   # open_unit's shape, never set_priority's


def test_the_cli_shorthand_does_not_trigger_on_the_wrong_argument_count(tmp_path, capsys):
    """Neither too few (no priority at all) nor too many (a trailing token) may be silently
    reinterpreted -- both fall through to the usage line, never a guess, and the registry stays
    exactly as seeded."""
    sdlc = _priority_sdlc(tmp_path)
    _seed_unit(sdlc, "voice")

    rc = define.main(["define.py", str(sdlc), "voice"])
    out = capsys.readouterr().out
    assert rc == 2 and "usage" in out
    assert feature_registry.read_unit(sdlc / "features", "voice")["priority"] is None

    rc = define.main(["define.py", str(sdlc), "voice", "P1", "extra"])
    out = capsys.readouterr().out
    assert rc == 2 and "usage" in out
    assert feature_registry.read_unit(sdlc / "features", "voice")["priority"] is None


def test_the_cli_usage_mentions_the_shorthand_form():
    assert define.main(["define.py"]) == 2
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        define.main(["define.py"])
    assert "<name> <P0..P4>" in buf.getvalue()


def _pushed(run):
    return [a for a in run.calls if a[:2] == ["git", "push"]][0][-1]


def test_a_fresh_clone_cuts_from_the_fetched_remote_base(tmp_path):
    """#1397 / #1504: no local branch of the base's name is needed -- the unit branch is cut from the
    fetched `<remote>/<base>` commit, and the report and the output name that commit."""
    run = Runner(refs={"refs/remotes/origin/main"})
    report = define.open_unit(_sdlc(tmp_path), "voice", "feature", run=run, cwd=tmp_path,
                              base="main", remote="origin")
    assert report["ok"]
    assert _pushed(run) == "f" * 40 + ":refs/heads/feature/voice"
    assert report["base_commit"] == "f" * 40
    assert "origin/main@" + "f" * 12 in report["steps"][0]["detail"]


def test_a_local_base_is_no_longer_the_source(tmp_path):
    """#1504: a local `main` that exists is NOT what the branch is cut from."""
    run = Runner(refs={"refs/heads/main", "refs/remotes/origin/main"})
    define.open_unit(_sdlc(tmp_path), "voice", "feature", run=run, cwd=tmp_path, base="main",
                     remote="origin")
    assert not any(a[:2] == ["git", "show-ref"] for a in run.calls)
    assert _pushed(run) == "f" * 40 + ":refs/heads/feature/voice"


