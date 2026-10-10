"""assign.py (#919, sigma-scope skill, wave 2 of epic #902): assignment resolution + the three
execution paths, acting on the SAME just-created plan/report pair #918's `compile_plan.compile_plan`
already produced.

Hermetic like every sibling test in this suite: a FakeSource records what would have been sent to
`gh` and never shells out (mirrors tests/test_compile_plan.py's own FakeSource in spirit, extended
with the `_run`/`_repo_args`/`note`/`next_pending`/`mark_in_progress` surface THIS module needs --
assignment/promotion on an ALREADY-CREATED issue, a hand-off comment, and the self-assigned drain
step, none of which `test_compile_plan.py`'s narrower FakeSource had to cover).
"""
import importlib.util
import json
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCOPE_SCRIPTS = _ROOT / "skills" / "sigma-scope" / "scripts"
LOOP = _ROOT / "skills" / "sigma-loop" / "scripts"


def _mod(name, where):
    spec = importlib.util.spec_from_file_location(name, where / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


assign = _mod("assign", SCOPE_SCRIPTS)
ledger = _mod("ledger", LOOP)
handoff = _mod("handoff", LOOP)


# --------------------------------------------------------------------------------------- fixtures


def _project(tmp_path, codeowners="/engine/    @eng-owner\n"):
    """`.sdlc/` + (optionally) `.github/CODEOWNERS` at the parent -- `owners.owner_of` resolves
    against `sdlc_dir`'s PARENT, exactly `resolve_assignment`'s own docstring. Deliberately NO
    catch-all `*` rule (unlike test_handoff.py's own fixture): several tests here need CODEOWNERS to
    resolve for SOME areas and NOT others, to prove tier 3 (active members) only ever fires when
    tier 2 genuinely comes back empty."""
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir(parents=True)
    if codeowners is not None:
        (tmp_path / ".github").mkdir()
        (tmp_path / ".github" / "CODEOWNERS").write_text(codeowners)
    return sdlc


GITHUB_CONFIG = {"discovery": {"source": "github", "github": {"repo": "acme/widgets"}}}


def _config(ledger_on=True, github=False, actor="amy", extra=None):
    cfg = {"ledger": {"enabled": ledger_on, "actor": actor}}
    if github:
        cfg.update(GITHUB_CONFIG)
    if extra:
        cfg.update(extra)
    return cfg


class FakeSource:
    """Github-shaped fake: `_run`/`_repo_args` (assignment, mirrors `triage._fetch_issue_state`'s
    own precedent for reusing these cross-module), `_swap_labels` (#1392: promotion is ONE atomic
    swap, not a `gh issue edit` -- see `assign._promote_to_goal`), `goal_label`,
    `note` (the hand-off comment channel), `next_pending`/`mark_in_progress` (so `loop._next` -- the
    self-assigned "start the drain" step -- has a real, hermetic backlog to claim against)."""

    def __init__(self, repo="acme/widgets", goal_label="sdlc:goal", pending=None):
        self.repo = repo
        self.goal_label = goal_label
        self.calls = []
        self.notes = []
        self._pending = list(pending or [])
        self.marked_in_progress = []
        self.swaps = []

    def _run(self, args):
        self.calls.append(list(args))
        return ""

    def _swap_labels(self, issue, add=(), remove=()):
        self.swaps.append((str(issue), list(add), list(remove)))
        return True

    def _repo_args(self):
        return ["--repo", self.repo]

    def _issue_add_assignees(self, number, assignees):
        """#895 slice 3a: the REST wrapper; the fake records the legacy-shaped call through `_run`."""
        for a in ([assignees] if isinstance(assignees, str) else assignees):
            self._run(["issue", "edit", str(number), *self._repo_args(), "--add-assignee", a])

    def note(self, issue, text):
        self.notes.append((str(issue), text))

    def next_pending(self, skip=()):
        skip = set(skip)
        for g in self._pending:
            if g not in skip:
                return g
        return None

    def mark_in_progress(self, goal):
        self.marked_in_progress.append(goal)


class NoGhSource:
    """A LocalSource-shaped stand-in: no `_run`/`_repo_args`/`_swap_labels` at all -- proves
    assignment/promotion degrade to a warning instead of an AttributeError."""

    def __init__(self):
        self.notes = []

    def note(self, issue, text):
        self.notes.append((str(issue), text))


def _plan(*issues, epic=None):
    return {"epic": epic, "issues": list(issues)}


def _issue(key, title, priority="P2", blocked_by=(), model=None):
    d = {"key": key, "title": title, "priority": priority, "blocked_by": list(blocked_by)}
    if model:
        d["model"] = model
    return d


def _report(issues, order, epic=None, failed=None, skipped=None):
    return {"epic": epic, "issues": dict(issues), "failed": dict(failed or {}),
            "skipped": dict(skipped or {}), "order": list(order), "warnings": []}


# =================================================================== 1. resolve_assignment


def test_resolve_always_offers_self(tmp_path):
    sdlc = _project(tmp_path, codeowners=None)
    pack = assign.resolve_assignment(str(sdlc), _config(github=False), "unmapped")
    assert pack["current_user"] == "amy"
    assert any(o["choice"] == "self" and o["login"] == "amy" for o in pack["options"])


def test_resolve_offers_codeowners_when_area_resolves(tmp_path):
    sdlc = _project(tmp_path)
    pack = assign.resolve_assignment(str(sdlc), _config(), "engine")
    assert pack["codeowners_owner"] == "eng-owner"
    assert pack["active_members"] == []
    choices = {(o["choice"], o["login"]) for o in pack["options"]}
    assert choices == {("self", "amy"), ("codeowners", "eng-owner")}


def test_resolve_never_queries_active_members_when_codeowners_resolves(tmp_path):
    sdlc = _project(tmp_path)

    def boom(_args):
        raise AssertionError("must not query active members once CODEOWNERS already resolved")

    pack = assign.resolve_assignment(str(sdlc), _config(github=True), "engine", run=boom)
    assert pack["codeowners_owner"] == "eng-owner"


def test_resolve_falls_back_to_active_members_ranked_by_assignment_volume(tmp_path):
    sdlc = _project(tmp_path, codeowners=None)
    calls = []

    def fake_run(args):
        calls.append(args)
        return json.dumps([
            {"assignees": [{"login": "bo"}, {"login": "cy"}]},
            {"assignees": [{"login": "bo"}]},
            {"assignees": [{"login": "dee"}]},
            {"assignees": []},
        ])

    pack = assign.resolve_assignment(str(sdlc), _config(github=True), "unmapped", run=fake_run)
    assert pack["codeowners_owner"] is None
    assert pack["active_members"] == ["bo", "cy", "dee"]      # bo:2, cy/dee tie at 1 -> alpha tiebreak
    assert len(calls) == 1                                    # exactly ONE bounded gh call
    choices = {(o["choice"], o["login"]) for o in pack["options"]}
    assert choices == {("self", "amy"), ("active", "bo"), ("active", "cy"), ("active", "dee")}


def test_resolve_active_members_capped_at_limit(tmp_path):
    sdlc = _project(tmp_path, codeowners=None)

    def fake_run(_args):
        return json.dumps([{"assignees": [{"login": n}]} for n in ("a", "b", "c", "d", "e")])

    pack = assign.resolve_assignment(str(sdlc), _config(github=True), "unmapped", run=fake_run,
                                      active_limit=3)
    assert pack["active_members"] == ["a", "b", "c"]


def test_resolve_active_members_degrade_outside_github_mode(tmp_path):
    sdlc = _project(tmp_path, codeowners=None)
    pack = assign.resolve_assignment(str(sdlc), _config(github=False), "unmapped")
    assert pack["active_members"] == []
    assert any("github" in w for w in pack["warnings"])


def test_resolve_never_raises_on_a_bad_gh_response(tmp_path):
    sdlc = _project(tmp_path, codeowners=None)
    pack = assign.resolve_assignment(str(sdlc), _config(github=True), "unmapped",
                                      run=lambda args: "not json")
    assert pack["active_members"] == []
    assert pack["warnings"]


# =================================================================== 2. _nodes_from_plan / _anchor_issue


def test_nodes_from_plan_carries_priority_title_and_needs():
    plan = _plan(_issue("a", "Foundation", priority="P1"),
                 _issue("b", "Depends on a", priority="P2", blocked_by=["a"]))
    report = _report({"a": "201", "b": "202"}, ["a", "b"])
    nodes = assign._nodes_from_plan(plan, report)
    by_id = {n["id"]: n for n in nodes}
    assert by_id[201] == {"id": 201, "needs": [], "priority": "P1", "model": None, "title": "Foundation"}
    assert by_id[202]["needs"] == [201]
    assert by_id[202]["priority"] == "P2"


def test_nodes_from_plan_defaults_priority_when_unset():
    plan = _plan(_issue("a", "No priority", priority=None))
    report = _report({"a": "9"}, ["a"])
    nodes = assign._nodes_from_plan(plan, report)
    assert nodes[0]["priority"] == handoff.DEFAULT_PRIORITY


def test_nodes_from_plan_ids_are_ints_not_strings():
    plan = _plan(_issue("a", "x"))
    report = _report({"a": "10"}, ["a"])
    nodes = assign._nodes_from_plan(plan, report)
    assert nodes[0]["id"] == 10 and isinstance(nodes[0]["id"], int)


def test_anchor_issue_is_the_epic_when_present():
    report = _report({"a": "300", "b": "301"}, ["a", "b"], epic="299")
    assert assign._anchor_issue(report) == 299


def test_anchor_issue_is_the_first_created_issue_in_order_without_an_epic():
    report = _report({"a": "300", "b": "301"}, ["a", "b"])
    assert assign._anchor_issue(report) == 300


def test_anchor_issue_none_when_nothing_was_created():
    report = _report({}, [])
    assert assign._anchor_issue(report) is None


# =================================================================== 3. execute() -- file-and-stop


def test_file_and_stop_assigns_and_stops(tmp_path):
    sdlc = _project(tmp_path)
    src = FakeSource()
    plan = _plan(_issue("a", "Foundation"), _issue("b", "Second", blocked_by=["a"]))
    report = _report({"a": "201", "b": "202"}, ["a", "b"])

    result = assign.execute(str(sdlc), _config(), plan, report, "engine", "bo",
                            assign.PATH_FILE_AND_STOP, source=src)

    assert result["assigned"] == [201, 202]
    assert result["promoted"] == []
    assert result["workflow"] is None
    assert result["comment_posted"] is False
    assert result["plan_file"] is None
    assert result["drain"] is None
    assert not result["warnings"]
    # only assignment ever happened -- never a label mutation, by either mechanism
    assert all("--add-assignee" in c for c in src.calls)
    assert not any("--add-label" in c or "--remove-label" in c for c in src.calls)
    assert src.swaps == []
    assert not src.notes


def test_file_and_stop_writes_one_ledger_entry_addressed_to_the_assignee(tmp_path):
    sdlc = _project(tmp_path)
    src = FakeSource()
    plan = _plan(_issue("a", "Foundation"))
    report = _report({"a": "201"}, ["a"])

    result = assign.execute(str(sdlc), _config(), plan, report, "engine", "bo",
                            assign.PATH_FILE_AND_STOP, source=src)

    entries = ledger.read_all(str(sdlc))
    assert len(entries) == 1
    assert entries[0]["kind"] == "note"
    assert entries[0]["to"] == "bo"
    assert entries[0]["area"] == "engine"
    assert entries[0]["issue"] == 201             # anchored on the sole created issue
    assert result["ledger_entry"] == entries[0]["id"]


def test_file_and_stop_leaves_every_issue_unassigned_when_no_login_is_chosen(tmp_path):
    sdlc = _project(tmp_path)
    src = FakeSource()
    plan = _plan(_issue("a", "Foundation"))
    report = _report({"a": "201"}, ["a"])

    result = assign.execute(str(sdlc), _config(), plan, report, "engine", None,
                            assign.PATH_FILE_AND_STOP, source=src)

    assert result["assigned"] == []
    assert src.calls == []                        # not one gh call was ever made


def test_file_and_stop_degrades_gracefully_on_a_non_github_source(tmp_path):
    sdlc = _project(tmp_path)
    src = NoGhSource()
    plan = _plan(_issue("a", "Foundation"))
    report = _report({"a": "201"}, ["a"])

    result = assign.execute(str(sdlc), _config(), plan, report, "engine", "bo",
                            assign.PATH_FILE_AND_STOP, source=src)

    assert result["assigned"] == []
    assert any("github discovery mode" in w for w in result["warnings"])


def test_execute_with_no_created_issues_is_a_clean_no_op(tmp_path):
    sdlc = _project(tmp_path)
    src = FakeSource()
    plan = _plan(_issue("a", "Never landed"))
    report = _report({}, [], failed={"a": "gh: boom"})

    result = assign.execute(str(sdlc), _config(), plan, report, "engine", "bo",
                            assign.PATH_FILE_AND_STOP, source=src)

    assert result["assigned"] == [] and result["anchor_issue"] is None
    assert src.calls == [] and not ledger.read_all(str(sdlc))
    assert any("nothing to act on" in w for w in result["warnings"])


def test_execute_rejects_an_unknown_path(tmp_path):
    sdlc = _project(tmp_path)
    plan = _plan(_issue("a", "x"))
    report = _report({"a": "1"}, ["a"])
    with pytest.raises(ValueError):
        assign.execute(str(sdlc), _config(), plan, report, "engine", "bo", "not-a-real-path")


# =================================================================== 4. execute() -- start-now, handoff


def test_start_now_handoff_promotes_assigns_and_computes_the_wave_schedule(tmp_path):
    sdlc = _project(tmp_path)
    src = FakeSource()
    plan = _plan(_issue("a", "Foundation", priority="P1"),
                 _issue("b", "Depends on a", priority="P0", blocked_by=["a"]))
    report = _report({"a": "201", "b": "202"}, ["a", "b"])

    result = assign.execute(str(sdlc), _config(), plan, report, "engine", "bo",
                            assign.PATH_START_HANDOFF, source=src)

    assert result["assigned"] == [201, 202]
    assert result["promoted"] == [201, 202]
    # a (needed by b) must land in an earlier wave than b, regardless of b's higher raw priority
    assert result["workflow"]["waves"] == [[201], [202]]
    # #1392: ONE atomic swap per issue -- and no `gh issue edit` label write anywhere, because
    # that command is four parallel HTTP requests, not one write (see `_promote_to_goal`).
    assert src.swaps == [("201", ["sdlc:goal"], [handoff.proposed_label({})]),
                         ("202", ["sdlc:goal"], [handoff.proposed_label({})])]
    assert not any("--add-label" in c or "--remove-label" in c for c in src.calls)


def test_start_now_handoff_posts_a_ledger_entry_and_a_comment_on_the_anchor_issue(tmp_path):
    sdlc = _project(tmp_path)
    src = FakeSource()
    plan = _plan(_issue("a", "Foundation"), _issue("b", "Second", blocked_by=["a"]),
                 epic={"title": "The epic", "body": "body"})
    report = _report({"a": "201", "b": "202"}, ["a", "b"], epic="199")

    result = assign.execute(str(sdlc), _config(), plan, report, "engine", "bo",
                            assign.PATH_START_HANDOFF, source=src)

    assert result["anchor_issue"] == 199
    assert result["comment_posted"] is True
    assert len(src.notes) == 1
    issue, text = src.notes[0]
    assert issue == "199"
    assert "@bo" in text and "#201" in text and "#202" in text

    entries = ledger.read_all(str(sdlc))
    assert len(entries) == 1 and entries[0]["issue"] == 199 and entries[0]["to"] == "bo"


def test_start_now_handoff_anchors_on_the_first_issue_when_there_is_no_epic(tmp_path):
    sdlc = _project(tmp_path)
    src = FakeSource()
    plan = _plan(_issue("a", "Foundation"), _issue("b", "Second", blocked_by=["a"]))
    report = _report({"a": "201", "b": "202"}, ["a", "b"])

    result = assign.execute(str(sdlc), _config(), plan, report, "engine", "bo",
                            assign.PATH_START_HANDOFF, source=src)

    assert result["anchor_issue"] == 201
    assert src.notes[0][0] == "201"


def test_start_now_handoff_never_writes_a_plan_file_or_starts_the_drain(tmp_path):
    sdlc = _project(tmp_path)
    src = FakeSource()
    plan = _plan(_issue("a", "Foundation"))
    report = _report({"a": "201"}, ["a"])

    result = assign.execute(str(sdlc), _config(), plan, report, "engine", "bo",
                            assign.PATH_START_HANDOFF, source=src)

    assert result["plan_file"] is None
    assert result["drain"] is None
    assert not (sdlc / "plans").exists()


def test_start_now_handoff_dependency_map_never_comma_joins_multiple_blockers():
    """The regex trap named in the issue (`backlog_check._BLOCK_RE` only captures ONE #N per
    trigger-phrase occurrence): the dependency lines this module renders for its own comment/plan-
    file prose must repeat the phrase per blocker, exactly like `compile_plan._blocker_marker_lines`
    already does for the real body markers."""
    plan = _plan(_issue("a", "First"), _issue("b", "Second"), _issue("c", "Third"),
                 _issue("d", "Triple-blocked", blocked_by=["a", "b", "c"]))
    report = _report({"a": "1", "b": "2", "c": "3", "d": "4"}, ["a", "b", "c", "d"])
    nodes = assign._nodes_from_plan(plan, report)
    lines = assign._dependency_lines(nodes)
    d_lines = [l for l in lines if l.startswith("#4")]
    assert len(d_lines) == 3
    assert all("," not in l for l in d_lines)


# =================================================================== 5. execute() -- start-now, self


def test_start_now_self_writes_the_plan_file_matching_the_naming_convention(tmp_path):
    sdlc = _project(tmp_path)
    src = FakeSource(pending=[])
    plan = _plan(_issue("a", "Foundation piece"), _issue("b", "Second", blocked_by=["a"]),
                 epic={"title": "Ship the thing", "body": "body"})
    report = _report({"a": "201", "b": "202"}, ["a", "b"], epic="199")

    result = assign.execute(str(sdlc), _config(), plan, report, "engine", "amy",
                            assign.PATH_START_SELF, source=src)

    assert result["plan_file"] == str(sdlc / "plans" / "199-ship-the-thing.md")
    content = pathlib.Path(result["plan_file"]).read_text(encoding="utf-8")
    assert content.startswith("# Plan — #199: Ship the thing")
    assert "#201" in content and "#202" in content
    assert "@amy" in content
    assert "sigma-triage" in content or "loop.py next" in content


def test_start_now_self_plan_file_table_reflects_the_real_wave_schedule(tmp_path):
    sdlc = _project(tmp_path)
    src = FakeSource(pending=[])
    plan = _plan(_issue("a", "Foundation", priority="P1"),
                 _issue("b", "Depends on a", priority="P0", blocked_by=["a"]))
    report = _report({"a": "201", "b": "202"}, ["a", "b"])

    result = assign.execute(str(sdlc), _config(), plan, report, "engine", "amy",
                            assign.PATH_START_SELF, source=src)

    content = pathlib.Path(result["plan_file"]).read_text(encoding="utf-8")
    # a lands in wave 1, b (which needs a) lands in wave 2 -- table must say so, not raw priority order
    a_row = next(l for l in content.splitlines() if l.startswith("| #201"))
    b_row = next(l for l in content.splitlines() if l.startswith("| #202"))
    assert "| 1 |" in a_row
    assert "| 2 |" in b_row
    assert "#201" in b_row.split("|")[-2]          # "Blocked by" column names #201


def test_start_now_self_starts_the_drain(tmp_path):
    sdlc = _project(tmp_path)
    src = FakeSource(pending=["201", "202"])
    plan = _plan(_issue("a", "Foundation"))
    report = _report({"a": "201"}, ["a"])

    result = assign.execute(str(sdlc), _config(ledger_on=False), plan, report, "engine", "amy",
                            assign.PATH_START_SELF, source=src)

    assert result["drain"]["run_started"] is True
    assert result["drain"]["picked_kind"] == "goal"
    assert result["drain"]["picked"] == "201"
    assert src.marked_in_progress == ["201"]
    assert (sdlc / "state" / "STATE.md").exists()
    markers = list((sdlc / "state" / "sessions").glob("*.active"))
    assert len(markers) == 1
    assert json.loads(markers[0].read_text())["in_flight"] == ["201"]


def test_start_now_self_never_posts_a_comment(tmp_path):
    sdlc = _project(tmp_path)
    src = FakeSource(pending=[])
    plan = _plan(_issue("a", "Foundation"))
    report = _report({"a": "201"}, ["a"])

    result = assign.execute(str(sdlc), _config(), plan, report, "engine", "amy",
                            assign.PATH_START_SELF, source=src)

    assert result["comment_posted"] is False
    assert src.notes == []


def test_start_now_self_writes_a_slugged_filename_without_an_epic(tmp_path):
    sdlc = _project(tmp_path)
    src = FakeSource(pending=[])
    plan = _plan(_issue("a", "Refactor the Widget Factory!!"))
    report = _report({"a": "555"}, ["a"])

    result = assign.execute(str(sdlc), _config(), plan, report, "engine", "amy",
                            assign.PATH_START_SELF, source=src)

    assert result["plan_file"] == str(sdlc / "plans" / "555-refactor-the-widget-factory.md")


def test_start_now_self_ledger_entry_names_the_self_assignment(tmp_path):
    sdlc = _project(tmp_path)
    src = FakeSource(pending=[])
    plan = _plan(_issue("a", "Foundation"))
    report = _report({"a": "201"}, ["a"])

    assign.execute(str(sdlc), _config(), plan, report, "engine", "amy",
                  assign.PATH_START_SELF, source=src)

    entries = ledger.read_all(str(sdlc))
    assert len(entries) == 1
    assert entries[0]["to"] == "amy"
    assert "self-assigned" in entries[0]["why"]


# =================================================================== 6. CLI


def test_cli_resolve_prints_a_pack(tmp_path, capsys):
    sdlc = _project(tmp_path)
    (sdlc / "config.json").write_text(json.dumps(_config()))
    rc = assign.main(["assign.py", "resolve", str(sdlc), "engine"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["codeowners_owner"] == "eng-owner"


def test_cli_execute_reads_plan_and_report_files(tmp_path, capsys):
    """CLI `execute` has no way to inject a FakeSource, so it resolves a REAL source via
    `sources.get_source` -- with no `discovery.source: github` configured, that is a `LocalSource`,
    which cannot assign. This proves the CLI wiring (reads both files, drives `execute()`, prints its
    report) end to end, and that the graceful github-only degrade (proven directly against a
    `NoGhSource` above) also holds through the CLI boundary: warnings present, non-zero exit, but the
    ledger entry still lands."""
    sdlc = _project(tmp_path)
    (sdlc / "config.json").write_text(json.dumps(_config()))
    plan_path = tmp_path / "plan.json"
    report_path = tmp_path / "report.json"
    plan_path.write_text(json.dumps(_plan(_issue("a", "Foundation"))))
    report_path.write_text(json.dumps(_report({"a": "201"}, ["a"])))

    rc = assign.main(["assign.py", "execute", str(sdlc), "--plan", str(plan_path),
                      "--report", str(report_path), "--area", "engine", "--assignee", "bo",
                      "--path", "file-and-stop"])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["assigned"] == []
    assert out["anchor_issue"] == 201
    assert any("github discovery mode" in w for w in out["warnings"])
    assert ledger.read_all(str(sdlc))


def test_cli_usage_error_on_missing_args():
    assert assign.main(["assign.py"]) == 2


# =================================================================== 7. fail-open branches
#
# Review of PR #936 (independent maker != checker pass) found every except/degrade clause backing
# this module's own "FAIL-OPEN, LIKE EVERY SIBLING IN THIS TOOLCHAIN" docstring claim was untested --
# including the exact scenario named in that review, `schedule_waves` raising `ValueError`. Each test
# below proves ONE such clause degrades into `warnings` instead of propagating, mirroring
# test_compile_plan.py's own `test_source_resolution_failure_is_reported_not_raised` pattern
# (`monkeypatch.setattr(<module>.<submodule>, "<func>", boom)` against the SAME cross-loaded
# submodule instances `assign.py` itself calls through -- `assign.owners`, `assign.sources`,
# `assign.state`, `assign.loop` -- not a reimport, so the patch actually lands on the call site).


def test_active_members_degrades_on_non_list_gh_output(tmp_path):
    """#895 2c: the list read is REST first and a non-list page is now a GhApiError raised BEFORE the
    old `isinstance(items, list)` arm, so it lands in the `except` arm ("could not rank active repo
    members: ..."). The old "unexpected gh output" arm was unreachable after the change and is deleted
    (no test can reach it): this test pins the arm that replaced it, and that nothing fell back."""
    sdlc = _project(tmp_path, codeowners=None)
    calls = []

    def run(args):
        calls.append(list(args))
        return json.dumps({"not": "a list"})

    pack = assign.resolve_assignment(str(sdlc), _config(github=True), "unmapped", run=run)
    assert pack["active_members"] == []
    assert any("could not rank active repo members" in w for w in pack["warnings"])
    assert not any("unexpected gh output" in w for w in pack["warnings"])
    assert [c for c in calls if c[:2] == ["issue", "list"]] == []        # a bad page never falls back


def test_active_members_asks_rest_first_newest_first_for_all_states():
    import gqlfake
    calls = []

    def run(args):
        calls.append(list(args))
        return gqlfake.rest_list(args, [gqlfake.rest_item(2, ()), gqlfake.rest_item(1, ())]) or "[]"

    assign._active_members(_config(github=True), run=run, sample_size=50)
    p = gqlfake.rest_list_params(calls[0])
    assert calls[0][1] == "repos/acme/widgets/issues"
    assert p["state"] == "all" and p["per_page"] == "50" and p["direction"] == "desc" and "labels" not in p


def test_active_members_rate_limited_rest_falls_back_to_one_issue_list(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    monkeypatch.delenv("SIGMA_GH_GRAPHQL", raising=False)
    calls = []

    def run(args):
        calls.append(list(args))
        if args[0] == "api":
            exc = RuntimeError("gh api failed")
            exc.hint = "gh: API rate limit exceeded (HTTP 429)"
            raise exc
        return json.dumps([{"assignees": [{"login": "bo"}]}])

    members, warnings = assign._active_members(_config(github=True), run=run)
    assert members == ["bo"] and warnings == []
    assert [c for c in calls if c[0] == "issue"] == [["issue", "list", "--repo", "acme/widgets", "--state", "all",
                                                      "--json", "assignees", "--limit", "50"]]


def test_active_members_threads_sdlc_dir_to_the_breaker(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    monkeypatch.delenv("SIGMA_GH_GRAPHQL", raising=False)

    def run(args):
        if args[0] == "api":
            exc = RuntimeError("gh api failed")
            exc.hint = "gh: Server Error (HTTP 502)"
            raise exc
        return "[]"

    assign._active_members(_config(github=True), run=run, sdlc_dir=str(tmp_path))
    assert json.loads((tmp_path / "state" / "gh-rest-breaker.json").read_text())["consecutive"] == 1


def test_resolve_assignment_passes_its_sdlc_dir_down(tmp_path, monkeypatch):
    sdlc = _project(tmp_path, codeowners=None)
    seen = {}
    monkeypatch.setattr(assign, "_active_members",
                        lambda config, run=None, limit=3, sample_size=50, sdlc_dir=None: seen.update(d=sdlc_dir) or ([], []))
    assign.resolve_assignment(str(sdlc), _config(github=True), "unmapped", run=lambda a: "[]")
    assert seen["d"] == str(sdlc)


def test_active_members_skips_malformed_items_and_missing_logins():
    def fake_run(_args):
        return json.dumps([
            "not-a-dict",
            {"assignees": [{"login": "bo"}, {}, {"login": ""}, "not-a-dict-either"]},
            {"assignees": None},
        ])

    members, warnings = assign._active_members(_config(github=True), run=fake_run)
    assert members == ["bo"]
    assert warnings == []


def test_resolve_reports_codeowners_failure_without_raising(tmp_path, monkeypatch):
    sdlc = _project(tmp_path)

    def boom(project_root, area, config):
        raise RuntimeError("CODEOWNERS parse error")

    monkeypatch.setattr(assign.owners, "owner_of", boom)
    pack = assign.resolve_assignment(str(sdlc), _config(github=True), "engine",
                                      run=lambda args: "[]")
    assert pack["codeowners_owner"] is None
    assert any("could not read CODEOWNERS" in w for w in pack["warnings"])
    # tier 2 came back empty (via the failure), so tier 3 must still be consulted, not skipped
    assert pack["active_members"] == []


def test_apply_assignment_partial_failure_still_assigns_the_rest(tmp_path):
    sdlc = _project(tmp_path)

    class FlakySource(FakeSource):
        def _run(self, args):
            if "202" in args:
                raise RuntimeError("gh: 500")
            return super()._run(args)

    src = FlakySource()
    plan = _plan(_issue("a", "Foundation"), _issue("b", "Second", blocked_by=["a"]))
    report = _report({"a": "201", "b": "202"}, ["a", "b"])

    result = assign.execute(str(sdlc), _config(), plan, report, "engine", "bo",
                            assign.PATH_FILE_AND_STOP, source=src)

    assert result["assigned"] == [201]
    assert any("could not assign @bo to #202" in w for w in result["warnings"])


def test_promote_to_goal_degrades_on_non_github_source_in_handoff_path(tmp_path):
    sdlc = _project(tmp_path)
    src = NoGhSource()
    plan = _plan(_issue("a", "Foundation"))
    report = _report({"a": "201"}, ["a"])

    result = assign.execute(str(sdlc), _config(), plan, report, "engine", "bo",
                            assign.PATH_START_HANDOFF, source=src)

    assert result["promoted"] == []
    assert any("promotion to goal needs github discovery mode" in w for w in result["warnings"])
    # degraded promotion never blocks the rest of the path -- the wave schedule is still computed
    assert result["workflow"] is not None


def test_promote_to_goal_partial_failure_still_promotes_the_rest(tmp_path):
    sdlc = _project(tmp_path)

    class FlakySource(FakeSource):
        def _swap_labels(self, issue, add=(), remove=()):
            if str(issue) == "202":
                raise RuntimeError("gh: 500")
            return super()._swap_labels(issue, add=add, remove=remove)

    src = FlakySource()
    plan = _plan(_issue("a", "Foundation"), _issue("b", "Second", blocked_by=["a"]))
    report = _report({"a": "201", "b": "202"}, ["a", "b"])

    result = assign.execute(str(sdlc), _config(), plan, report, "engine", "bo",
                            assign.PATH_START_HANDOFF, source=src)

    assert result["promoted"] == [201]
    assert any("could not promote #202" in w for w in result["warnings"])


def test_schedule_waves_failure_is_reported_not_raised(tmp_path):
    """The exact scenario the review named: a genuine cycle (`a` blocked by `b`, `b` blocked by `a`)
    strands both nodes, so `triage.schedule_waves` raises `ValueError` -- must degrade into a
    warning, never propagate out of `execute()`."""
    sdlc = _project(tmp_path)
    src = FakeSource()
    plan = _plan(_issue("a", "First", blocked_by=["b"]), _issue("b", "Second", blocked_by=["a"]))
    report = _report({"a": "201", "b": "202"}, ["a", "b"])

    result = assign.execute(str(sdlc), _config(), plan, report, "engine", "bo",
                            assign.PATH_START_HANDOFF, source=src)

    assert result["workflow"] is None
    assert any("could not compute a wave schedule" in w for w in result["warnings"])
    # everything downstream of the failed schedule still degrades gracefully, not raises
    assert result["ledger_entry"] is not None


def test_execute_reports_no_backlog_source_without_raising(tmp_path, monkeypatch):
    sdlc = _project(tmp_path)
    plan = _plan(_issue("a", "Foundation"))
    report = _report({"a": "201"}, ["a"])

    def boom(sdlc_dir, config):
        raise RuntimeError("bad discovery config")

    monkeypatch.setattr(assign.sources, "get_source", boom)
    result = assign.execute(str(sdlc), _config(), plan, report, "engine", "bo",
                            assign.PATH_FILE_AND_STOP)

    assert result["assigned"] == []
    assert any("no backlog source" in w for w in result["warnings"])
    assert result["ledger_entry"] is not None       # the ledger write itself needs no source at all


def test_start_now_handoff_comment_failure_is_reported_not_raised(tmp_path):
    sdlc = _project(tmp_path)

    class MuteSource(FakeSource):
        def note(self, issue, text):
            raise RuntimeError("gh: could not comment")

    src = MuteSource()
    plan = _plan(_issue("a", "Foundation"))
    report = _report({"a": "201"}, ["a"])

    result = assign.execute(str(sdlc), _config(), plan, report, "engine", "bo",
                            assign.PATH_START_HANDOFF, source=src)

    assert result["comment_posted"] is False
    assert any("could not comment on #201" in w for w in result["warnings"])


def test_start_now_self_plan_file_write_failure_is_reported_not_raised(tmp_path):
    """A directory sitting where the plan file needs to be written turns `write_text` into an
    `OSError` (`IsADirectoryError` on POSIX) -- hermetic, no chmod/permission trickery needed."""
    sdlc = _project(tmp_path)
    src = FakeSource(pending=[])
    plan = _plan(_issue("a", "Foundation"))
    report = _report({"a": "201"}, ["a"])
    blocking_dir = sdlc / "plans" / "201-foundation.md"
    blocking_dir.mkdir(parents=True)

    result = assign.execute(str(sdlc), _config(), plan, report, "engine", "amy",
                            assign.PATH_START_SELF, source=src)

    assert result["plan_file"] is None
    assert any("could not write the plan file" in w for w in result["warnings"])
    # the drain step is independent of whether the plan file itself landed
    assert result["drain"] is not None


def test_start_drain_reports_start_run_failure_without_raising(tmp_path, monkeypatch):
    sdlc = _project(tmp_path)
    src = FakeSource(pending=["201"])
    plan = _plan(_issue("a", "Foundation"))
    report = _report({"a": "201"}, ["a"])

    def boom(sdlc_dir):
        raise RuntimeError("could not create state dir")

    monkeypatch.setattr(assign.state, "start_run", boom)
    result = assign.execute(str(sdlc), _config(), plan, report, "engine", "amy",
                            assign.PATH_START_SELF, source=src)

    assert result["drain"]["run_started"] is False
    assert result["drain"]["picked_kind"] is None
    assert any("could not start the run" in w for w in result["drain"]["warnings"])
    assert src.marked_in_progress == []             # never reached loop._next at all


def test_start_drain_reports_next_failure_without_raising(tmp_path, monkeypatch):
    sdlc = _project(tmp_path)
    src = FakeSource(pending=["201"])
    plan = _plan(_issue("a", "Foundation"))
    report = _report({"a": "201"}, ["a"])

    def boom(sdlc_dir, source, config):
        raise RuntimeError("lease already held")

    monkeypatch.setattr(assign.loop, "_next", boom)
    result = assign.execute(str(sdlc), _config(), plan, report, "engine", "amy",
                            assign.PATH_START_SELF, source=src)

    assert result["drain"]["run_started"] is True
    assert result["drain"]["picked_kind"] is None
    assert any("could not pick the first goal" in w for w in result["drain"]["warnings"])


def test_start_drain_tolerates_a_degraded_done_payload(tmp_path):
    """#1084 plan-review finding (§4.5): `_start_drain` stores `_next()`'s raw (kind, payload)
    tuple verbatim into `result["picked_kind"]`/`result["picked"]`, then the whole `result`
    dict is JSON-serialized. A `("DONE", "<degraded reason>")` payload -- new as of #1084 -- must
    be exactly as safe to serialize as the historical `("DONE", None)` shape, not silently
    coerced or dropped."""
    sdlc = _project(tmp_path)

    class DegradedSource:
        """Mirrors the DegradedSource fake at tests/test_loop.py:212-241 -- backlog genuinely
        empty AND the read that established that was itself degraded."""
        def next_pending(self, skip=()): return None
        def read_degraded(self): return True

    result = assign._start_drain(str(sdlc), _config(ledger_on=False), DegradedSource())

    assert result["run_started"] is True
    assert result["picked_kind"] == "DONE"
    assert isinstance(result["picked"], str) and "degraded" in result["picked"]
    json.dumps(result)          # must not raise -- proves the JSON-serialization path tolerates it


# =================================================================== 8. assignee=None rendering
#
# Independent review of PR #936 caught a real bug (not just a coverage gap): `execute()`'s own
# docstring explicitly permits `assignee=None` even on PATH_START_SELF ("a caller-sanctioned choice,
# not a failure"), but `_render_plan_md` unconditionally wrote "Self-assigned to @{assignee}" --
# rendering the literal string "Self-assigned to @None" into a persisted plan file. Fixed to render
# an explicit "left unassigned" line instead; these tests pin that fix.


def test_render_plan_md_never_writes_the_literal_string_at_none():
    content = assign._render_plan_md(201, "Foundation", None, "engine",
                                      [{"id": 201, "needs": [], "priority": "P2", "title": "Foundation"}],
                                      [[{"id": 201, "needs": [], "priority": "P2", "title": "Foundation"}]],
                                      "2026-01-01T00:00:00Z")
    assert "@None" not in content
    assert "unassigned" in content.lower()


def test_start_now_self_with_no_assignee_writes_a_plan_file_without_at_none(tmp_path):
    sdlc = _project(tmp_path)
    src = FakeSource(pending=[])
    plan = _plan(_issue("a", "Foundation"))
    report = _report({"a": "201"}, ["a"])

    result = assign.execute(str(sdlc), _config(), plan, report, "engine", None,
                            assign.PATH_START_SELF, source=src)

    content = pathlib.Path(result["plan_file"]).read_text(encoding="utf-8")
    assert "@None" not in content
    assert "unassigned" in content.lower()


def test_apply_assignment_goes_rest_and_keeps_the_warning_string_on_failure():
    """#895 slice 3a, against a REAL GitHubSource: the assignment is a REST POST /assignees; a dropped
    (non-assignable) login raises inside `gh_api` and surfaces as the unchanged per-issue warning."""
    import sys
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import gqlfake
    sources = _mod("sources", LOOP)
    seen = []

    def run(args):
        seen.append(list(args))
        out = gqlfake.rest_write(args, assignable={"bo"})
        assert out is not None, args
        return out

    src = sources.GitHubSource(GITHUB_CONFIG, run=run)
    assigned, warnings = assign._apply_assignment(src, [201], "bo")
    assert assigned == [201] and warnings == []
    assert seen[0][:4] == ["api", "repos/acme/widgets/issues/201/assignees", "--method", "POST"]
    assigned, warnings = assign._apply_assignment(src, [202], "ghost")
    assert assigned == [] and len(warnings) == 1
    assert warnings[0].startswith("could not assign @ghost to #202: ")
    assert not any(c[0] == "issue" for c in seen)                    # no gh issue edit anywhere


def test_triaged_report_promotes_nothing_by_label_and_never_promotes_a_parked_child(tmp_path):
    """#1006: a report from a triage-on compile carries `parked`; children are already armed, so no
    label write happens, and a parked child is not started by a start-now path."""
    sdlc = _project(tmp_path)
    src = FakeSource()
    plan = _plan(_issue("a", "Fine"), _issue("b", "Held"))
    report = _report({"a": "201", "b": "202"}, ["a", "b"])
    report["parked"] = {"b": "hard-stop word: credentials"}
    result = assign.execute(str(sdlc), _config(), plan, report, "engine", "bo",
                            assign.PATH_START_HANDOFF, source=src)
    assert result["promoted"] == [201] and src.swaps == []
    assert any("#202 stays parked" in w for w in result["warnings"])
