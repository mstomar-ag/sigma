"""`sigma-define` end to end (#1662): open a unit, then fill it with work that BELONGS to it.

The unit suite (`tests/test_define.py`) proves each half against fakes. This file's job is the
composition those cannot cover, and it is the whole point of the skill: **does the real output of
`open_unit` -> `stamp_plan` -> the real `compile_plan.compile_plan()` -> `declare` produce issues
that `features.read` -- the SAME reader the pick path runs -- reads back as declaring the unit?**

If it does not, every issue the skill files is one Sigma either bases on the wrong branch or
refuses outright, and no unit test anywhere would say so: §4a is silent by design, so a marker that
lands somewhere rule 5 refuses to read produces no error at all.

TWO THINGS MAKE THIS A REAL MEASUREMENT RATHER THAN A RESTATEMENT.

  - THE ISSUES ARE FILED BY THE REAL `compile_plan.compile_plan()` AGAINST THE REAL `LocalSource`,
    the same hermetic `discovery.source: local-goals` pattern `test_sdlc_scope_integration.py` uses
    and for the same reason: no FakeSource stands in for the module actually under composition, so a
    shape mismatch between the stamp and the filer fails HERE even though neither module's own suite
    can fail on it. The bodies read back are read off disk, from the files `create_dependency` wrote.
  - THE LABEL USED AT VERIFICATION IS THE ONE THE FLOW CREATED, NEVER ONE THIS FILE TYPED. The fake
    `gh` holds a real label set; `open_unit`'s label step writes into it; `attach_label` refuses any
    name that set does not hold, exactly as `GitHubSource._swap_labels` raises rather than minting.
    So a flow that skipped step 4 comes back `body_only` and
    `test_the_flow_is_non_vacuous_the_label_half_really_is_load_bearing` proves that is what happens.

`LocalSource` deliberately does not define the label surface -- there is no label index over goal
FILES -- so `LabelledLocalSource` below adds exactly the two methods `declare` duck-types on and
delegates everything else to the real object. That is the narrowest possible stand-in: every method
`compile_plan` touches is the real one.
"""
import importlib.util
import json
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFINE_SCRIPTS = _ROOT / "skills" / "sigma-define" / "scripts"
SCOPE_SCRIPTS = _ROOT / "skills" / "sigma-scope" / "scripts"
LOOP = _ROOT / "skills" / "sigma-loop" / "scripts"


def _mod(name, where):
    spec = importlib.util.spec_from_file_location(name, where / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


define = _mod("define", DEFINE_SCRIPTS)
compile_plan = _mod("compile_plan", SCOPE_SCRIPTS)
scope = _mod("scope", SCOPE_SCRIPTS)
features = _mod("features", LOOP)
feature_registry = _mod("feature_registry", LOOP)
feature_sync = _mod("feature_sync", LOOP)
backlog_check = _mod("backlog_check", LOOP)     # the LIVE _BLOCK_RE -- never a hand-copied duplicate
sources = _mod("sources", LOOP)

LOCAL_CONFIG = {"discovery": {"source": "local-goals", "github": {"repo": "fixture-org/fixture-repo"}},
                "ledger": {"enabled": False, "actor": "amy"}}
UNIT = "voice-interview"


# ------------------------------------------------------------------------------- the fake world


class FakeHost:
    """`git` and `gh`, with real state: the branches the remote has and the labels the repo has.

    `(cwd, argv) -> stdout`, raising where the real binary would exit non-zero -- the contract
    `feature_sync._run`, `feature_rebase` and `work._run` all share, so this object could be handed
    to any of them unchanged. `permissive` lets a caller under measurement (`sync_at_pick`, which
    catches everything) reach for a command this fake does not model without the failure vanishing
    into that function's own never-raises promise."""

    def __init__(self, head="main", permissive=False):
        self.branches = set()
        self.labels = set()
        self.head = head
        self.permissive = permissive
        self.calls = []

    def __call__(self, cwd, argv):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        if argv[:2] == ["git", "ls-remote"]:
            if "--heads" in argv and argv[-1] == "refs/heads/" + "main":
                return "abc123\t" + argv[-1]
            if "--heads" in argv and argv[-1] in self.branches:
                return "abc123\trefs/heads/%s" % argv[-1]
            if "--heads" in argv and argv[-1].startswith("refs/heads/"):
                return "\n".join("abc123\trefs/heads/%s" % b for b in sorted(self.branches))
            return ""
        if argv[:2] == ["git", "fetch"]:
            return ""
        if argv[:3] == ["gh", "api", "user"]:
            return "tester"
        if argv[:2] == ["git", "rev-parse"]:
            return "f" * 40 if "--verify" in argv else self.head
        if argv[:2] == ["git", "show-ref"]:
            return ""
        if argv[:2] == ["git", "push"]:
            self.branches.add(argv[-1].split(":refs/heads/", 1)[-1])
            return ""
        if argv[:3] == ["gh", "label", "list"]:
            assert "--search" not in argv, "the existence check must enumerate, not search"
            return "\n".join(sorted(self.labels))
        if argv[:3] == ["gh", "label", "create"]:
            if any(n.lower() == argv[3].lower() for n in self.labels):
                raise RuntimeError('label with name "%s" already exists' % argv[3])
            self.labels.add(argv[3])
            return ""
        if self.permissive:
            return ""
        raise AssertionError("the fake host was handed a command it does not model: %r" % (argv,))


class LabelledLocalSource:
    """The real `LocalSource`, plus exactly the two methods `declare` duck-types on.

    `attach_label` NEVER CREATES, and refuses a name the repo does not carry -- the same failure
    `GitHubSource._swap_labels` raises (`unknown label(s) on this repo`) rather than minting one.
    `repo_labels` is the SAME set object the fake `gh` wrote into, which is what makes the label half
    of this test a measurement of `open_unit` rather than of this class."""

    def __init__(self, local, repo_labels):
        self._local = local
        self._repo_labels = repo_labels
        self._on = {}

    def __getattr__(self, name):
        return getattr(self._local, name)          # create_dependency / append_to_body: the real ones

    def attach_label(self, goal, name):
        if name not in self._repo_labels:
            raise RuntimeError("unknown label(s) on this repo: %s" % name)
        self._on.setdefault(str(goal), []).append(name)
        return True

    def fetch_body_labels(self, goal):
        # `str(goal)`, deliberately: `_resolve_ref` is documented to accept "a bare numeric id --
        # exactly what `create_dependency`'s own RETURN VALUE is", but that return value is an `int`
        # and the function opens with `pathlib.Path(goal)`, which raises `TypeError` on one. Every
        # caller in the tree today reaches it through `append_to_body`/`note` after a `str()` has
        # already happened somewhere, so the gap has never shown. Noted rather than worked around
        # silently -- see this goal's hand-back.
        path = self._local._resolve_ref(str(goal))
        return {"body": self._local.fetch_title_body(path)["body"],
                "labels": list(self._on.get(str(goal), []))}


def _init_sdlc(tmp_path):
    sdlc = tmp_path / ".sdlc"
    (sdlc / "goals").mkdir(parents=True)
    (sdlc / "state").mkdir()
    (sdlc / "config.json").write_text(json.dumps(LOCAL_CONFIG), encoding="utf-8")
    return sdlc


def _plan():
    """A plan of the shape `compile_plan.compile_plan()` documents, with a real dependency edge so
    the blocker markers it writes are exercised alongside the marker this skill writes."""
    return {
        "epic": {"title": "Voice interview", "body": "The parent of the voice-interview work.",
                 "priority": "P1"},
        "issues": [
            {"key": "a", "title": "Capture the audio stream", "priority": "P1",
             "body": "Open the mic and buffer to disk."},
            {"key": "b", "title": "Transcribe the buffer", "priority": "P2",
             "body": "Feed the buffer to the transcriber.", "blocked_by": ["a"]},
        ],
    }


def _flow(tmp_path, *, create_label=True):
    """The whole gesture, in the order §14 fixes. Returns everything a test needs to measure it."""
    sdlc = _init_sdlc(tmp_path)
    host = FakeHost()
    opened = define.open_unit(sdlc, UNIT, "feature", config=LOCAL_CONFIG, run=host, cwd=tmp_path)
    if not create_label:
        host.labels.discard(define.label_for(UNIT))     # the ONE step removed, nothing else changed
    stamped, warnings = define.stamp_plan(_plan(), UNIT)
    local = sources.get_source(str(sdlc), LOCAL_CONFIG)
    source = LabelledLocalSource(local, host.labels)
    report = compile_plan.compile_plan(str(sdlc), LOCAL_CONFIG, stamped, source=source)
    numbers = ([report["epic"]] if report["epic"] else []) + list(report["issues"].values())
    declared = define.declare(source, UNIT, numbers)
    return {"sdlc": sdlc, "host": host, "opened": opened, "warnings": warnings,
            "report": report, "source": source, "numbers": numbers, "declared": declared}


# ------------------------------------------------------------------------------- the whole flow


def test_every_issue_the_flow_files_reads_back_as_declaring_the_unit(tmp_path):
    """THE TEST THIS FILE EXISTS FOR. Not "the marker is in the body" -- that is a string search, and
    a fenced marker passes it while declaring nothing. `features.read` is the reader the pick path
    itself runs, so `agree` here means the loop can genuinely pick these issues onto this unit."""
    flow = _flow(tmp_path)
    assert flow["opened"]["ok"], flow["opened"]
    assert flow["warnings"] == []
    assert flow["report"]["failed"] == {} and flow["report"]["skipped"] == {}
    assert len(flow["numbers"]) == 3                      # the epic and both issues

    for number in flow["numbers"]:
        verdict = features.read(flow["source"].fetch_body_labels(number))
        assert verdict.state == features.AGREE, (number, verdict)
        assert verdict.unit == UNIT
        assert verdict.body == UNIT and verdict.label == UNIT   # BOTH halves, not one

    assert flow["declared"]["ok"], flow["declared"]
    assert all(r["outcome"] == define.DECLARED for r in flow["declared"]["issues"])


def test_the_flow_is_non_vacuous_the_label_half_really_is_load_bearing(tmp_path):
    """Remove step 4 and nothing else. If the previous test could pass without the label the flow
    itself created, it would be measuring this file's own typing instead of the skill.

    The degradation is also the RIGHT one, and worth pinning as behaviour rather than only as
    non-vacuity: the issues still exist and still declare the unit where a human reads it
    (`body_only`), which is what makes an absent label recoverable in one gesture (§7a) instead of
    lossy."""
    flow = _flow(tmp_path, create_label=False)
    assert not flow["declared"]["ok"]
    assert all(r["outcome"] == define.FAILED for r in flow["declared"]["issues"])
    for number in flow["numbers"]:
        verdict = features.read(flow["source"].fetch_body_labels(number))
        assert verdict.state == features.BODY_ONLY and verdict.unit == UNIT


def test_the_marker_lands_in_the_file_on_disk_bare_and_at_the_left_margin(tmp_path):
    """The body read back through the source is the body `create_dependency` WROTE, but only the
    file on disk proves the frontmatter block did not swallow the marker -- `LocalSource` writes a
    YAML fence above every body, and a marker inside one is not body text at all."""
    flow = _flow(tmp_path)
    files = sorted((flow["sdlc"] / "goals").glob("*.md"))
    assert len(files) == 3
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "\nFeature: %s\n" % UNIT in text
        assert "\nBranch: feature/%s\n" % UNIT in text
        assert "---\nFeature:" not in text          # never inside the frontmatter fence


def test_the_blocked_by_edges_still_parse_after_stamping(tmp_path):
    """The stamp appends to a body `compile_plan` then appends its own blocker marker to. Both have
    to survive, and the blocker side is checked with `backlog_check`'s LIVE regex rather than a
    hand-copied one -- a marker that stopped matching would silently unblock a dependent goal."""
    flow = _flow(tmp_path)
    dependent = flow["report"]["issues"]["b"]
    body = flow["source"].fetch_body_labels(dependent)["body"]
    assert backlog_check._BLOCK_RE.search(body), body
    assert features.parse_body(body) == UNIT       # and the declaration survived the append


def test_a_plan_whose_issues_are_filed_is_queued_and_never_started(tmp_path):
    """Since the rulebook (slice 17) `compile_plan` arms a plan issue at its own plan priority unless the
    deny-list parks it -- locally, `status: pending`, never the retired `proposed` state. Step 9 of this skill
    ("start now, or leave it on the board") is still a QUESTION: arming queues the goal, it does not start it, so
    opening a unit must not start work by itself."""
    flow = _flow(tmp_path)
    for path in sorted((flow["sdlc"] / "goals").glob("*.md")):
        text = path.read_text(encoding="utf-8")
        # a plan issue is queued (pending); the parent that only groups them stays proposed; nothing is started
        assert "status: pending" in text or "status: proposed" in text
        assert "status: in_progress" not in text and "status: done" not in text


# ------------------------------------------------------------------------------- the registry gate


def test_creating_the_registry_directory_is_what_flips_the_kits_own_adoption_gate(tmp_path):
    """STEP 5, MEASURED RATHER THAN ARGUED -- the evidence behind this skill's registry decision.

    Seven of the kit's passes open with the same `registry_dir(sdlc).is_dir()` test and return
    `not-adopted` before spending anything. `feature_sync.sync_at_pick` is the cheapest of them to
    drive, so it stands in for the set: before `open_unit` it declines, after it it runs. That is
    the whole of what step 5 does, and it is why the step is LAST -- it arms all seven for the WHOLE
    repository, not for this unit."""
    sdlc = _init_sdlc(tmp_path)
    before = feature_sync.sync_at_pick(str(sdlc), LOCAL_CONFIG, "1", UNIT,
                                       run=FakeHost(permissive=True), cwd=tmp_path)
    assert before["outcome"] == feature_sync.NOT_ADOPTED

    opened = define.open_unit(sdlc, UNIT, "feature", config=LOCAL_CONFIG,
                              run=FakeHost(), cwd=tmp_path)
    assert opened["ok"] and opened["steps"][-1]["step"] == "registry"
    assert feature_registry.registry_dir(sdlc).is_dir()

    after = feature_sync.sync_at_pick(str(sdlc), LOCAL_CONFIG, "1", UNIT,
                                      run=FakeHost(permissive=True), cwd=tmp_path)
    assert after["outcome"] != feature_sync.NOT_ADOPTED


def test_the_fresh_registry_reads_as_adopted_and_empty_not_as_broken(tmp_path):
    """`{}` from a registry that exists means "no units yet"; `{}` from one that does not exist means
    "not adopted". §14's own table calls that out as the pair people confuse. The directory this
    skill creates has to be genuinely readable, not merely present."""
    sdlc = _init_sdlc(tmp_path)
    define.open_unit(sdlc, UNIT, "feature", config=LOCAL_CONFIG, run=FakeHost(), cwd=tmp_path)
    features_dir = feature_registry.registry_dir(sdlc)
    assert features_dir.is_dir()
    # #1312: open now records the unit it opened (so other machines can see it). The contract here is
    # adopted-and-readable versus broken, not "zero units": the only entry is the unit just opened.
    assert list(feature_registry.read(features_dir)) == [UNIT]


# ------------------------------------------------------------------------------- the ordered gesture


def test_the_branch_and_the_label_exist_before_any_issue_declares_the_unit(tmp_path):
    """§14's requirement, restated as the only thing that actually matters about it: by the time the
    first body carries a marker, the label that marker's pick will look for is already there.

    Asserted over the observed call log rather than over the report, because the report is this
    module's own account of itself and the call log is what happened."""
    flow = _flow(tmp_path)
    kinds = [a[0] for a in flow["host"].calls
             if a[:2] not in (["git", "status"], ["git", "add"], ["git", "commit"])
             and a[:3] != ["gh", "api", "user"] and a[:3] != ["git", "remote", "get-url"]]   # #1312: record commit is last
    assert kinds[0] == "git" and "gh" in kinds
    assert kinds.index("gh") > max(i for i, k in enumerate(kinds) if k == "git")
    assert "feature/%s" % UNIT in flow["host"].branches
    assert define.label_for(UNIT) in flow["host"].labels
    # and no issue existed until after both
    assert len(sorted((flow["sdlc"] / "goals").glob("*.md"))) == 3


def test_opening_the_same_unit_twice_changes_nothing(tmp_path):
    """A person will run this again -- to add more issues to a unit that already exists. The second
    run must not force the base over a branch that has since accumulated work, and must not attempt
    a label create that GitHub would reject."""
    sdlc = _init_sdlc(tmp_path)
    host = FakeHost()
    first = define.open_unit(sdlc, UNIT, "feature", config=LOCAL_CONFIG, run=host, cwd=tmp_path)
    second = define.open_unit(sdlc, UNIT, "feature", config=LOCAL_CONFIG, run=host, cwd=tmp_path)
    assert first["ok"] and second["ok"]
    assert [s["outcome"] for s in first["steps"]] == [define.CREATED] * 3
    assert [s["outcome"] for s in second["steps"]] == [define.EXISTS] * 3
    assert len([a for a in host.calls if a[:2] == ["git", "push"]]) == 1
    assert len([a for a in host.calls if a[:3] == ["gh", "label", "create"]]) == 1


def test_the_cli_open_verb_reports_the_same_thing_the_function_does(tmp_path, capsys):
    """`SKILL.md` drives the script through its CLI, so the CLI's one JSON line is the real
    interface -- a function-only test would prove the half nothing calls."""
    sdlc = _init_sdlc(tmp_path)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_plan()), encoding="utf-8")
    rc = define.main(["define.py", "stamp", str(plan_path), "--unit", UNIT])
    out = json.loads(capsys.readouterr().out.strip())
    assert rc == 0 and out["ok"] and out["warnings"] == []
    written = json.loads(plan_path.read_text(encoding="utf-8"))
    assert features.parse_body(written["issues"][0]["body"]) == UNIT
    assert features.parse_body(written["epic"]["body"]) == UNIT
