"""Integration tests for the sigma-scope skill (epic #902, wave 4 / #921): the five pieces
(brainstorm.py #916, dedup.py #917, compile_plan.py #918, assign.py #919, scope.py #920) wired
together the way `skills/sigma-scope/SKILL.md` actually drives them, end to end. Each piece already
has its own isolation suite (test_brainstorm.py, test_dedup.py, test_compile_plan.py, test_assign.py,
test_scope.py) — this file's job is the composition those suites don't cover: does the REAL output
of one module feed correctly into the next REAL module, all the way from a raw invocation string to
real goal files on disk plus a real wave schedule, with nothing hand-typed in the middle to paper
over a shape mismatch.

Hermetic throughout, matching every sibling test in this family: `discovery.source: local-goals`
(no `gh`, no network — the same scratch-project pattern #920's own validation run used, and the one
issue #921's addendum requires for anything that reaches `compile_plan`/`assign.execute`). No
FakeSource stands in for `compile_plan.compile_plan()`/`assign.execute()` themselves anywhere in
this file — the real `LocalSource` (via `sources.get_source`) is what actually runs, so a genuine
shape mismatch between modules would fail here even though it can't fail in either module's own
isolated suite (which mocks the other side).
"""
import importlib.util
import json
import pathlib
import re

_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCOPE_SCRIPTS = _ROOT / "skills" / "sigma-scope" / "scripts"
LOOP = _ROOT / "skills" / "sigma-loop" / "scripts"


def _mod(name, where):
    spec = importlib.util.spec_from_file_location(name, where / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


brainstorm = _mod("brainstorm", SCOPE_SCRIPTS)
dedup = _mod("dedup", SCOPE_SCRIPTS)
compile_plan = _mod("compile_plan", SCOPE_SCRIPTS)
scope = _mod("scope", SCOPE_SCRIPTS)
assign = _mod("assign", SCOPE_SCRIPTS)
backlog_check = _mod("backlog_check", LOOP)   # the LIVE _BLOCK_RE — never a hand-copied duplicate
sources = _mod("sources", LOOP)


# ------------------------------------------------------------------------------------- fixture helpers


LOCAL_CONFIG = {"discovery": {"source": "local-goals"}, "ledger": {"enabled": False, "actor": "amy"}}


def _init_sdlc(tmp_path, config=None):
    sdlc = tmp_path / ".sdlc"
    (sdlc / "goals").mkdir(parents=True)
    (sdlc / "state").mkdir()
    cfg = config if config is not None else LOCAL_CONFIG
    (sdlc / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    return sdlc


def _seed_goal(sdlc_dir, gid, title, body, status="pending"):
    """A pre-existing local goal file, written in the exact shape `LocalSource.create_dependency`
    itself writes (frontmatter id/title/lane/status + body) — this is the "board" brainstorm.py's
    fuzzy matcher and dedup.py's corpus builder both read for local-goals discovery."""
    goals = pathlib.Path(sdlc_dir) / "goals"
    goals.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48] or "goal"
    path = goals / f"{gid:04d}-{slug}.md"
    path.write_text(
        f'---\nid: {gid:04d}\ntitle: "{title}"\nlane: auto\nstatus: {status}\n---\n\n{body}\n',
        encoding="utf-8")
    return path


def _goal_files(sdlc_dir):
    return sorted((pathlib.Path(sdlc_dir) / "goals").glob("*.md"))


def _read(path):
    return pathlib.Path(path).read_text(encoding="utf-8")


# ============================================================ 1. brainstorm -> dedup (real handoff)


def test_free_text_target_with_no_board_overlap_flows_into_an_empty_dedup_result():
    """form == "text" (brainstorm's own, real fuzzy-match judgment against a real, if small, local
    corpus) feeds `dedup.find_candidates` its `text` verbatim -- no board content is remotely related,
    so the real scoring pipeline (not a mock) must come back empty, not error or degrade."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        sdlc = _init_sdlc(pathlib.Path(d))
        _seed_goal(sdlc, 1, "Retry backoff jitter for the ledger sync",
                   "Add jitter to the retry backoff so flaky network calls don't thunder-herd.")
        target = brainstorm.resolve_target("add dark mode to the settings page", sdlc_dir=str(sdlc))
        assert target["form"] == "text" and target["confident"] is True

        pack = dedup.find_candidates(str(sdlc), target["text"])
        assert pack["schema"] == "brainstorm-dedup/v1"
        assert pack["candidates"] == []


def test_issue_reference_forms_own_ref_is_excluded_from_its_own_dedup_pass():
    """SKILL.md step 3's own documented case: when the resolved target IS an existing issue, that
    issue's own ref legitimately tops its own dedup pass unless excluded -- exercised here against the
    REAL resolve_target -> find_candidates handoff, not asserted from the docstring alone."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        sdlc = _init_sdlc(pathlib.Path(d))
        _seed_goal(sdlc, 5, "Add a UI for editing config.json",
                   "We keep hand-editing config.json in production. Ship a small settings editor UI.")
        invocation = "check the story issue where we added a story about a config.json settings editor UI"
        target = brainstorm.resolve_target(invocation, sdlc_dir=str(sdlc))
        assert target["form"] == "issue_reference"
        own_ref = target["issue"]["ref"]

        # without exclusion, the target's own issue is free to appear as its own top hit
        raw_pack = dedup.find_candidates(str(sdlc), target["issue"]["body"], title=target["issue"]["title"])
        assert any(c["ref"] == own_ref for c in raw_pack["candidates"])

        # SKILL.md's own discipline: discard it via exclude_refs before presenting the rest
        pack = dedup.find_candidates(str(sdlc), target["issue"]["body"], title=target["issue"]["title"],
                                     exclude_refs=[own_ref])
        assert all(c["ref"] != own_ref for c in pack["candidates"])


def test_ambiguous_target_carries_both_readings_and_dedup_can_be_run_against_either():
    """form == "ambiguous" is the one case SKILL.md step 4 says must be asked about, never guessed --
    prove BOTH readings it carries are independently usable by the next real step: `text` still
    resolves through dedup as free text, and every `candidates` entry is a real ref the caller could
    equally well treat as "this is the same thing"."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        sdlc = _init_sdlc(pathlib.Path(d))
        _seed_goal(sdlc, 9, "Redesign the onboarding checklist widget",
                   "A checklist widget for new-user onboarding, plus a settings toggle.")
        target = brainstorm.resolve_target("maybe add a settings widget somewhere in the app",
                                           sdlc_dir=str(sdlc))
        assert target["form"] == "ambiguous" and target["confident"] is False
        assert target["text"] and target["candidates"]

        pack = dedup.find_candidates(str(sdlc), target["text"])
        assert pack["schema"] == "brainstorm-dedup/v1"     # the free-text reading is a valid dedup query
        assert {c["ref"] for c in target["candidates"]} <= {"9"} | {c["ref"] for c in pack["candidates"]} \
            or pack["candidates"] == []                    # (weak overlap may not clear dedup's own floor)


# ==================================================================== 2. plan -> scope.py -> real files


def test_single_issue_plan_grounded_in_a_resolved_target_becomes_a_real_local_goal_file():
    """The step-6 handoff: a plan drafted from a resolved target's own text, compiled through the real
    `scope.py` CLI entrypoint (never `compile_plan.compile_plan` called directly) against the real
    local-goals source -- proving scope.py's own report shape is what actually lands on disk, not a
    shape convenient for a unit test's own FakeSource."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        sdlc = _init_sdlc(pathlib.Path(d))
        target = brainstorm.resolve_target("add a settings UI for editing config.json", sdlc_dir=str(sdlc))
        assert target["form"] == "text"

        plan = {"epic": None, "issues": [{
            "key": "a", "title": "Config editor UI", "priority": "P2",
            "body": f"Resolved target: {target['text']}",
        }]}
        plan_path = pathlib.Path(d) / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        report_path = pathlib.Path(d) / "report.json"

        rc = scope.main(["scope.py", str(sdlc), "--plan", str(plan_path), "--report", str(report_path)])
        assert rc == 0

        report = json.loads(report_path.read_text())
        assert report["failed"] == {} and report["skipped"] == {}
        assert list(report["issues"].keys()) == ["a"]

        files = _goal_files(sdlc)
        assert len(files) == 1
        text = _read(files[0])
        assert "Config editor UI" in text
        assert "status: pending" in text                   # armed at filing (rulebook slice 17): queued, never the retired proposed state
        assert "Labels: priority:P2" in text and "needs-confirmation" not in text


def test_multi_issue_plan_with_epic_and_dependency_creates_real_linked_files():
    """A plan with an epic + a genuine `blocked_by` edge, compiled for real: verifies the epic's own
    "Tracks #N" back-references, each sub-issue's "Part of epic #N." line, and — using the LIVE
    `backlog_check._BLOCK_RE` regex, never a hand-copied duplicate — that the dependant's real body
    actually carries a marker the rest of this toolchain would recognize as a genuine blocker."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        sdlc = _init_sdlc(pathlib.Path(d))
        plan = {
            "epic": {"title": "Config editor", "body": "Tracking issue.", "priority": "P2"},
            "issues": [
                {"key": "schema", "title": "Add a config schema", "priority": "P2", "body": "schema work"},
                {"key": "ui", "title": "Build the editor UI", "priority": "P2", "body": "ui work",
                 "blocked_by": ["schema"]},
            ],
        }
        plan_path = pathlib.Path(d) / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        report_path = pathlib.Path(d) / "report.json"

        rc = scope.main(["scope.py", str(sdlc), "--plan", str(plan_path), "--report", str(report_path)])
        assert rc == 0
        report = json.loads(report_path.read_text())
        assert report["epic"] is not None
        schema_num, ui_num = report["issues"]["schema"], report["issues"]["ui"]

        files = {f.stem: _read(f) for f in _goal_files(sdlc)}
        ui_text = next(t for name, t in files.items() if "build-the-editor-ui" in name)
        assert f"Part of epic #{report['epic']}." in ui_text
        assert backlog_check._BLOCK_RE.search(ui_text), "the dependant's body must trip the LIVE blocker regex"
        assert f"#{schema_num}" in backlog_check._BLOCK_RE.search(ui_text).group(0)

        epic_text = next(t for name, t in files.items() if "config-editor" in name)
        assert f"Tracks #{schema_num}" in epic_text and f"Tracks #{ui_num}" in epic_text


# ==================================================================== 3. scope.py report -> assign.py


def test_compiled_report_flows_into_resolve_assignment_and_file_and_stop():
    """The real report `scope.py` just wrote is handed straight to `assign.resolve_assignment` +
    `assign.execute(path=file-and-stop)` -- no re-shaping in between. Every issue must stay exactly
    as compile_plan left it (status: pending, queued) since file-and-stop's whole contract is "nothing else
    happens"."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        sdlc = _init_sdlc(pathlib.Path(d))
        plan = {"epic": None, "issues": [{"key": "a", "title": "Config editor UI", "priority": "P2",
                                          "body": "body"}]}
        report = scope.compile_and_report(str(sdlc), plan)
        assert report["failed"] == {} and report["skipped"] == {}

        config = LOCAL_CONFIG
        options = assign.resolve_assignment(str(sdlc), config, "config")
        assert options["options"][0] == {"choice": "self", "login": options["current_user"]}

        result = assign.execute(str(sdlc), config, plan, report, "config",
                                assignee=options["current_user"], path=assign.PATH_FILE_AND_STOP)
        assert result["promoted"] == []
        assert result["assigned"] == []       # LocalSource has no _run/_repo_args -- degrades, doesn't crash
        assert result["ledger_entry"] is None or isinstance(result["ledger_entry"], str)

        files = _goal_files(sdlc)
        assert len(files) == 1
        assert "status: pending" in _read(files[0])         # untouched -- file-and-stop starts nothing (it is queued, as filed)


def test_compiled_report_flows_into_start_now_self_computes_the_real_wave_schedule():
    """`assign.execute(path=start-now-self)` against the SAME real (plan, report) pair a two-issue,
    one-dependency plan just produced: the wave schedule it computes must reflect the REAL dependency
    edge compile_plan actually wired, not a hand-built node list -- wave 1 is the blocker alone, wave 2
    is the dependant alone. Promotion itself degrades under local-goals (no _run/_repo_args), matching
    the same documented behavior #920's own validation run already established -- not a bug, but this
    test still proves the surrounding plan-file + drain wiring runs to completion regardless."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        sdlc = _init_sdlc(pathlib.Path(d))
        plan = {"epic": None, "issues": [
            {"key": "schema", "title": "Add a config schema", "priority": "P2", "body": "b"},
            {"key": "ui", "title": "Build the editor UI", "priority": "P2", "body": "b",
             "blocked_by": ["schema"]},
        ]}
        report = scope.compile_and_report(str(sdlc), plan)
        assert report["failed"] == {} and report["skipped"] == {}

        config = LOCAL_CONFIG
        result = assign.execute(str(sdlc), config, plan, report, "config",
                                assignee="amy", path=assign.PATH_START_SELF)

        schema_num, ui_num = int(report["issues"]["schema"]), int(report["issues"]["ui"])
        assert result["workflow"]["waves"] == [[schema_num], [ui_num]]
        assert result["promoted"] == [schema_num, ui_num]    # the issues were armed at filing (rulebook slice 17); promote reports them
        assert any("github discovery mode" in w for w in result["warnings"])
        assert result["plan_file"] and pathlib.Path(result["plan_file"]).exists()
        plan_md = _read(result["plan_file"])
        assert f"#{schema_num}" in plan_md and f"#{ui_num}" in plan_md
        assert result["drain"] is not None       # _start_drain still ran (state.start_run + loop._next)


def test_compiled_report_flows_into_start_now_handoff_posts_a_real_comment_via_the_fixed_resolve_ref():
    """`assign.execute(path=start-now-handoff)` posts its hand-off comment via `source.note(str(anchor),
    ...)` with `anchor` a BARE numeric id (`_anchor_issue`'s own return, `_as_int`-coerced) -- this is
    the EXACT call shape (`compile_plan.py`'s `_patch_epic_with_subs` -> `LocalSource.note`/
    `append_to_body` chaining a bare `create_dependency()` id straight through) that was broken before
    this issue's own `LocalSource._resolve_ref` fix (see tests/test_sources.py's own isolated
    regression tests for the mechanism in isolation). This test proves the fix holds through the REAL
    composed pipeline -- a real multi-issue plan with a real epic, compiled by the real
    `compile_plan.compile_plan()`, handed to the real `assign.execute()` -- not just the standalone
    `LocalSource.note()` call the isolated suite already covers. Before the fix, this exact sequence
    left `result["comment_posted"] is True` (the call itself doesn't raise -- `execute()`'s own
    try/except swallows it) but silently wrote NOTHING real to disk under the epic's own real stem;
    this test pins that the journey file that lands is actually named after the epic's real slug, not
    a bare-id artifact."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        sdlc = _init_sdlc(pathlib.Path(d))
        plan = {"epic": {"title": "Config editor", "body": "b"}, "issues": [
            {"key": "schema", "title": "Add a config schema", "priority": "P2", "body": "b"},
            {"key": "ui", "title": "Build the editor UI", "priority": "P2", "body": "b",
             "blocked_by": ["schema"]},
        ]}
        report = scope.compile_and_report(str(sdlc), plan)
        assert report["failed"] == {} and report["skipped"] == {}
        assert report["epic"] is not None

        result = assign.execute(str(sdlc), LOCAL_CONFIG, plan, report, "config",
                                assignee="bo", path=assign.PATH_START_HANDOFF)

        # assignment/promotion degrade under local-goals (LocalSource has no _run/_repo_args) --
        # the SAME documented, expected degrade the self-path composition test above already
        # accounts for, not a regression. The comment-posting path this test actually targets does
        # NOT depend on _run/_repo_args at all, so it must succeed regardless of those two degrades.
        assert any("github discovery mode" in w for w in result["warnings"])
        assert result["comment_posted"] is True

        epic_num = int(report["epic"])
        epic_file = next(f for f in _goal_files(sdlc) if f.name.startswith(f"{epic_num:04d}-"))
        journey_file = pathlib.Path(sdlc) / "journey" / (epic_file.stem + ".md")
        assert journey_file.exists(), (
            "note() must resolve the bare epic id to the REAL epic goal file's own stem, not "
            "silently write to a bare-id-named file that doesn't correspond to any real goal")
        journey_text = _read(journey_file)
        assert "@bo" in journey_text
        schema_num, ui_num = int(report["issues"]["schema"]), int(report["issues"]["ui"])
        assert f"#{schema_num}" in journey_text and f"#{ui_num}" in journey_text


def test_structurally_invalid_plan_never_reaches_assign_at_all():
    """A caller-error plan (an unknown `blocked_by` key) must fail BEFORE any issue is created, per
    `compile_plan.compile_plan`'s own contract -- proving the sigma-scope pipeline actually halts there
    rather than assign.py ever being handed a partial or fabricated report."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        sdlc = _init_sdlc(pathlib.Path(d))
        plan = {"epic": None, "issues": [{"key": "a", "title": "x", "blocked_by": ["ghost"]}]}
        try:
            scope.compile_and_report(str(sdlc), plan)
            assert False, "expected ValueError before assign.py is ever reached"
        except ValueError:
            pass
        assert _goal_files(sdlc) == []


def test_partial_failure_report_never_lets_a_skipped_issue_into_the_wave_schedule():
    """One issue's `create_dependency` fails (a real, injected `FakeSource`, mirroring
    test_compile_plan.py's own convention); its dependant is therefore SKIPPED by compile_plan's own
    failure-midway contract. `assign._nodes_from_plan` must build its wave-schedule input from the
    REAL surviving report only -- proving the two modules' failure/skip conventions actually compose,
    a concern neither module's own isolated suite (which never wires the other module in) can catch."""
    class FailingSource:
        def __init__(self):
            self._next = 1

        def create_dependency(self, title, body, assignee, labels=(), goal_label=True):
            if title == "Add a config schema":
                raise RuntimeError("gh: simulated failure")
            number = str(self._next)
            self._next += 1
            return number

        def append_to_body(self, issue, marker):
            pass

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        sdlc = _init_sdlc(pathlib.Path(d))
        plan = {"epic": None, "issues": [
            {"key": "schema", "title": "Add a config schema", "priority": "P2", "body": "b"},
            {"key": "ui", "title": "Build the editor UI", "priority": "P2", "body": "b",
             "blocked_by": ["schema"]},
        ]}
        report = scope.compile_and_report(str(sdlc), plan, source=FailingSource())
        assert "schema" in report["failed"]
        assert "ui" in report["skipped"]
        assert report["issues"] == {}

        nodes = assign._nodes_from_plan(plan, report)
        assert nodes == []                 # neither the failed nor the transitively-skipped issue is a node

        result = assign.execute(str(sdlc), LOCAL_CONFIG, plan, report, "config",
                                assignee=None, path=assign.PATH_FILE_AND_STOP)
        assert "nothing to act on" in " ".join(result["warnings"])
