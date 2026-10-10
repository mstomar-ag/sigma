"""#233: the loop mirrors Priority and Phase onto the board's own fields.

Hermetic: every gh call is answered by `boardfake.GitHub` (one in-memory GitHub, the same fake #235's
board tests use), so these drive the REAL `phase_report.py start` hook and the REAL
`sources.GitHubSource` card path against a board shaped like a hand-made one. No network, and no
mutation of real GitHub: the acceptance run the issue describes against a live board is an owner
runbook in `docs/board-fields.md`, not something this suite (or the goal that wrote it) executes.
"""
import importlib.util
import json
import os
import pathlib

import boardfake

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOOP = ROOT / "skills" / "sigma-loop" / "scripts"


def _load(name, alias):
    spec = importlib.util.spec_from_file_location(alias, LOOP / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pr = _load("phase_report", "phase_report_for_233")
src_mod = _load("sources", "sources_for_233")

COLS = ("Backlog", "Ready", "In Progress", "QC", "Done", "Blocked", "Parked")
TOKENS = [t for _k, t in pr.PHASE_TOKENS]
PHASES = [k for k, _t in pr.PHASE_TOKENS]


def _status_field(tag="17"):
    return {"id": "PVTSSF_status_%s" % tag, "name": "Status",
            "options": [{"id": "s_%s" % c.replace(" ", "_").lower(), "name": c, "color": "GRAY",
                         "description": ""} for c in COLS]}


def _world(extra_fields=(), number=17, labels=("sdlc:goal", "priority:P1"), carded=True):
    """A HAND-MADE board (never created by Sigma): Status carries our columns, nothing else unless
    `extra_fields` says so. Issue #11 is a goal with `labels`, carded in In Progress."""
    gh = boardfake.GitHub(owner="acme", repo="widget")
    gh.add_board("sigma — SDLC", number=number,
                 fields=[{"id": "PVTF_title", "name": "Title", "options": None,
                          "data_type": "title"}, _status_field()] + [dict(f) for f in extra_fields])
    board = gh.board(number=number)
    if carded:
        it = gh.add_item(board, 11, Status="In Progress")
        it["labels"] = list(labels)          # `gh project item-list` carries an issue card's labels
    gh.issues = [{"number": 11, "labels": [{"name": n} for n in labels], "title": "t", "body": ""}]
    return gh, board


#: `project.setup_created` as `board_setup.py create` writes it: the board it made, by number AND
#: owner (#233 review block #2 -- a bare number could not tell another owner's board #17 from ours).
OURS = {"number": 17, "owner": "acme"}


def _cfg(**project):
    p = {"enabled": True, "owner": "acme", "number": 17}
    p.update(project)
    return {"discovery": {"source": "github",
                          "github": {"repo": "acme/widget", "goal_label": "sdlc:goal",
                                     "project": p}}}


def _sdlc(tmp_path, cfg=None):
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir(exist_ok=True)
    (sdlc / "config.json").write_text(json.dumps(cfg or _cfg()), encoding="utf-8")
    return sdlc


def _card(board, n=11):
    return next(i for i in board["items"] if i["content"]["number"] == n)


def _start(sdlc, phase, monkeypatch, gh, goal="11"):
    """The DOCUMENTED gesture's own entry point (`phase_report.py start <sdlc> <goal> <phase>
    --model M`), in-process so the fake runner can stand in for `gh`."""
    monkeypatch.setattr(pr, "_BOARD_RUN", gh.loop_run)
    monkeypatch.setattr(pr, "BOARD_RETRY_BASE", 0)
    return pr.cmd_start([str(sdlc), goal, phase, "--model", "sonnet"])


def _field_writes(gh, name):
    board = gh.board(number=17)
    fid = (gh.field(board, name) or {}).get("id")
    return [c for c in gh.calls if c[:2] == ["project", "item-edit"]
            and c[c.index("--field-id") + 1] == fid]


# ---------------------------------------------------------------- acceptance on the fake


def test_one_goal_through_p2_to_p7_advances_phase_and_priority_matches_label(
        tmp_path, monkeypatch, capsys):
    """On the board Sigma created (`project.setup_created` names it), both fields are Sigma's."""
    gh, board = _world()
    sdlc = _sdlc(tmp_path, _cfg(setup_created=OURS))
    seen = []
    for phase in PHASES[1:]:                                     # P2 RESEARCH .. P7 RETRO
        assert _start(sdlc, phase, monkeypatch, gh) == 0
        seen.append(_card(board).get("phase"))
    assert seen == TOKENS[1:]
    assert _card(board).get("priority") == "P1"
    # both fields were created ONCE, single-select, with the fixed shared vocabulary
    assert gh.option_names(board, "Phase") == TOKENS
    assert gh.option_names(board, "Priority") == ["P0", "P1", "P2", "P3", "P4"]
    creates = [c for c in gh.mutations() if any("createProjectV2Field(" in str(a) for a in c)]
    assert len(creates) == 2
    assert _card(board).get("status") == "In Progress"           # Status untouched
    err = capsys.readouterr().err
    assert "sigma:" not in err, err


def test_a_repeated_start_for_the_same_phase_writes_nothing(tmp_path, monkeypatch):
    gh, board = _world()
    sdlc = _sdlc(tmp_path, _cfg(setup_created=OURS))
    _start(sdlc, "research", monkeypatch, gh)
    before, made = len(gh.calls), len(gh.mutations())
    _start(sdlc, "research", monkeypatch, gh)
    again = gh.calls[before:]
    assert not [c for c in again if c[:2] == ["project", "item-edit"]], again
    assert len(gh.mutations()) == made                            # no further creates either


def test_calls_per_boundary_are_bounded(tmp_path, monkeypatch):
    """Steady state: ONE read (this issue's card, over GraphQL) + 1 Phase write -- never the
    whole-board `item-list` (6.5s on board #17's 246 cards), never `project list`/`field-list`."""
    gh, board = _world()
    sdlc = _sdlc(tmp_path)
    _start(sdlc, "research", monkeypatch, gh)
    before = len(gh.calls)
    _start(sdlc, "plan", monkeypatch, gh)
    verbs = [" ".join(c[:2]) for c in gh.calls[before:]]
    assert verbs == ["api graphql", "project item-edit"], verbs
    assert "projectItems(" in " ".join(map(str, gh.calls[before])), gh.calls[before]


def test_unlabelled_goal_leaves_priority_blank_not_p3(tmp_path, monkeypatch):
    """Decision D1 (plan): the kit's default is UNPRIORITISED, not P3 -- writing P3 would reorder
    the board queue and make the field-wins mirror stamp priority labels on every unlabelled goal."""
    gh, board = _world(labels=("sdlc:goal",))
    _start(_sdlc(tmp_path), "research", monkeypatch, gh)
    assert _card(board).get("phase") == "P2 RESEARCH"
    assert _card(board).get("priority") is None
    assert not [c for c in gh.calls if c[:2] == ["issue", "edit"]]


# ---------------------------------------------------------------- a board Sigma did not create


HAND_PHASE = {"id": "PVTSSF_hand_phase", "name": "phase", "options": [
    {"id": "hp_2", "name": "p2 research", "color": "PINK", "description": "theirs"},
    {"id": "hp_x", "name": "Triage", "color": "RED", "description": "a lane they added"},
    {"id": "hp_3", "name": "P3 PLAN", "color": "BLUE", "description": ""}]}
HAND_PRIO = {"id": "PVTSSF_hand_prio", "name": "PRIORITY", "options": [
    {"id": "hq_1", "name": "P1", "color": "ORANGE", "description": "urgent-ish"},
    {"id": "hq_x", "name": "Someday", "color": "GRAY", "description": ""}]}


def test_hand_made_fields_are_adopted_never_renamed_recoloured_or_dropped(
        tmp_path, monkeypatch):
    gh, board = _world(extra_fields=(HAND_PHASE, HAND_PRIO))
    snapshot = json.loads(json.dumps(board["fields"]))
    sdlc = _sdlc(tmp_path)
    _start(sdlc, "research", monkeypatch, gh)
    assert _card(board).get("phase") == "p2 research"          # their spelling, claimed by case
    assert _card(board).get("priority") == "P1"
    _start(sdlc, "plan", monkeypatch, gh)
    assert _card(board).get("phase") == "P3 PLAN"
    assert board["fields"] == snapshot                          # nothing renamed/recoloured/dropped
    assert not gh.mutations()                                   # no create, no option rewrite
    assert not [c for c in gh.calls if c[:2] == ["project", "field-create"]]


def test_a_missing_option_on_a_hand_made_field_warns_once_and_is_never_appended(
        tmp_path, monkeypatch, capsys):
    gh, board = _world(extra_fields=(HAND_PHASE, HAND_PRIO))
    snapshot = json.loads(json.dumps(board["fields"]))
    rc = _start(_sdlc(tmp_path), "implement", monkeypatch, gh)  # HAND_PHASE has no P5 option
    assert rc == 0
    assert board["fields"] == snapshot
    assert _card(board).get("phase") is None
    lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("sigma:")]
    assert len(lines) == 1 and "P5 IMPLEMENT" in lines[0], lines


def test_a_same_named_field_that_is_not_single_select_is_skipped_with_one_warning(
        tmp_path, monkeypatch, capsys):
    text_phase = {"id": "PVTF_text_phase", "name": "Phase", "options": None, "data_type": "text"}
    gh, board = _world(extra_fields=(text_phase, HAND_PRIO))
    assert _start(_sdlc(tmp_path), "research", monkeypatch, gh) == 0
    assert [f["name"] for f in board["fields"]].count("Phase") == 1
    lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("sigma:")]
    assert len(lines) == 1 and "single-select" in lines[0], lines
    assert _card(board).get("priority") == "P1"                 # Priority still mirrored


# ---------------------------------------------------------------- fail-open + controls


def test_control_phase_field_removed_and_uncreatable_pick_still_succeeds_one_warning(
        tmp_path, monkeypatch, capsys):
    """The issue's control: take the Phase field away (and make GitHub refuse to create it). The
    pick still succeeds, the start still exits 0, and exactly ONE warning is printed."""
    gh, board = _world()
    sdlc = _sdlc(tmp_path)
    _start(sdlc, "research", monkeypatch, gh)
    board["fields"] = [f for f in board["fields"] if f["name"] != "Phase"]   # removed by a human
    for it in board["items"]:
        it.pop("phase", None)
    gh.fail["createProjectV2Field"] = "GraphQL: Resource not accessible by integration"
    capsys.readouterr()
    src = src_mod.GitHubSource(_cfg(), run=gh.loop_run, sdlc_dir=str(sdlc))
    src._RETRY_BASE = 0
    gh.issues.append({"number": 12, "labels": [{"name": "sdlc:goal"}], "title": "u", "body": ""})
    src.mark_in_progress("12")                                  # the pick's board write
    assert _card(board, 12).get("status") == "In Progress"
    assert _start(sdlc, "plan", monkeypatch, gh, goal="12") == 0
    out, err = capsys.readouterr()
    assert "PHASE START" in out
    lines = [ln for ln in err.splitlines() if ln.startswith("sigma:")]
    assert len(lines) == 1 and "Phase" in lines[0], lines


def test_removed_phase_field_is_recreated_once(tmp_path, monkeypatch):
    gh, board = _world()
    sdlc = _sdlc(tmp_path)
    _start(sdlc, "research", monkeypatch, gh)
    board["fields"] = [f for f in board["fields"] if f["name"] != "Phase"]
    _start(sdlc, "plan", monkeypatch, gh)
    _start(sdlc, "plan_review", monkeypatch, gh)
    assert [f["name"] for f in board["fields"]].count("Phase") == 1
    assert _card(board).get("phase") == "P4 PLAN-REVIEW"


def test_a_phase_write_failure_never_changes_the_start_outcome(tmp_path, monkeypatch, capsys):
    gh, board = _world()
    sdlc = _sdlc(tmp_path)
    _start(sdlc, "research", monkeypatch, gh)
    clean = capsys.readouterr().out
    gh.fail["item-edit"] = "HTTP 502: Bad Gateway"
    assert _start(sdlc, "research", monkeypatch, gh) == 0       # same phase: dedupe, no write
    card = gh.boards[0]["items"][0]                             # force a write that will fail
    card.pop("phase")
    card["values"].pop(gh.field(board, "Phase")["id"])
    rc = _start(sdlc, "research", monkeypatch, gh)
    out, err = capsys.readouterr()
    assert rc == 0
    assert out.splitlines()[-2:] == clean.splitlines()          # banner byte-identical
    assert len([ln for ln in err.splitlines() if ln.startswith("sigma:")]) == 1


def test_a_board_that_raises_anything_never_breaks_start(tmp_path, monkeypatch, capsys):
    def explode(_args):
        raise OSError("gh vanished")
    _sdlc(tmp_path)
    monkeypatch.setattr(pr, "_BOARD_RUN", explode)
    assert pr.cmd_start([str(tmp_path / ".sdlc"), "11", "research", "--model", "sonnet"]) == 0
    assert "PHASE START" in capsys.readouterr().out


# ---------------------------------------------------------------- disabled cleanly


def _no_board_calls(gh):
    return not [c for c in gh.calls if c[:1] == ["project"] or c[:2] == ["api", "graphql"]]


def test_disabled_when_project_enabled_is_off(tmp_path, monkeypatch):
    gh, _board = _world()
    _start(_sdlc(tmp_path, _cfg(enabled=False)), "research", monkeypatch, gh)
    assert _no_board_calls(gh)


def test_disabled_when_no_number_is_pinned(tmp_path, monkeypatch):
    gh, _board = _world()
    cfg = _cfg()
    del cfg["discovery"]["github"]["project"]["number"]
    _start(_sdlc(tmp_path, cfg), "research", monkeypatch, gh)
    assert _no_board_calls(gh)


def test_phase_field_false_disables_phase_but_not_priority(tmp_path, monkeypatch):
    gh, board = _world()
    _start(_sdlc(tmp_path, _cfg(phase_field=False, setup_created=OURS)), "research", monkeypatch, gh)
    assert gh.field(board, "Phase") is None
    assert _card(board).get("priority") == "P1"


def test_local_goals_mode_and_non_issue_goals_make_no_calls(tmp_path, monkeypatch):
    gh, _board = _world()
    _start(_sdlc(tmp_path, {"discovery": {"source": "local-goals"}}), "research", monkeypatch, gh)
    assert _no_board_calls(gh)
    other = tmp_path / "b"
    other.mkdir()
    _start(_sdlc(other), "research", monkeypatch, gh, goal="0004-vision")
    assert _no_board_calls(gh)


def test_configured_phase_field_name_is_used(tmp_path, monkeypatch):
    gh, board = _world()
    _start(_sdlc(tmp_path, _cfg(phase_field="SDLC phase")), "review", monkeypatch, gh)
    assert _card(board).get("sdlc phase") == "P6 REVIEW"


def test_an_uncarded_goal_is_never_carded_by_the_phase_hook(tmp_path, monkeypatch):
    """Carding it here would leave a blank-Status card: Status is the status path's job."""
    gh, board = _world(carded=False)
    assert _start(_sdlc(tmp_path), "research", monkeypatch, gh) == 0
    assert not [c for c in gh.calls if c[:2] == ["project", "item-add"]]


# ---------------------------------------------------------------- the Status write carries Priority


def test_a_status_write_also_mirrors_the_moved_goals_priority(tmp_path):
    prio = {"id": "PVTSSF_prio", "name": "Priority", "options": [
        {"id": "p_%d" % i, "name": "P%d" % i, "color": "GRAY", "description": ""}
        for i in range(5)]}
    gh, board = _world(extra_fields=(prio,))
    src = src_mod.GitHubSource(_cfg(), run=gh.loop_run, sdlc_dir=str(_sdlc(tmp_path)))
    src._RETRY_BASE = 0
    src.mark_in_progress("11")
    assert _card(board).get("status") == "In Progress"
    assert _card(board).get("priority") == "P1"


def test_phase_tokens_come_from_the_closed_vocabulary():
    """Imported, never copied: the board options ARE phase_report.PHASE_TOKENS."""
    text = (LOOP / "sources.py").read_text(encoding="utf-8")
    assert "P4 PLAN-REVIEW" not in text
    assert os.path.basename(pr.__file__) == "phase_report.py"


def test_several_failures_in_one_run_still_print_exactly_one_warning(tmp_path, monkeypatch, capsys):
    """Neither field exists and GitHub refuses both creates: two failures, ONE line."""
    gh, board = _world()
    gh.fail["createProjectV2Field"] = "GraphQL: Resource not accessible by integration"
    assert _start(_sdlc(tmp_path, _cfg(setup_created=OURS)), "research", monkeypatch, gh) == 0
    assert len(gh.mutations()) == 2                             # both creates were tried
    lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("sigma:")]
    assert len(lines) == 1, lines
    assert gh.field(board, "Phase") is None and gh.field(board, "Priority") is None


def test_a_repo_without_a_pinned_board_spawns_nothing_at_a_phase_start(tmp_path, monkeypatch):
    """The fence `test_phase_report_loads_no_network_capable_sibling_module` relies on, EXECUTED:
    with the real (bounded) runner in place and every subprocess entry point a raiser, a start on
    a repo whose board is off, or on but unpinned, spawns nothing and loads no `sources`."""
    import subprocess

    def boom(*_a, **_k):
        raise AssertionError("the board hook spawned a process")

    for name in ("run", "Popen"):
        monkeypatch.setattr(subprocess, name, boom)
    monkeypatch.setattr(pr, "_BOARD_RUN", None)
    loaded = []
    real_load = pr._load
    monkeypatch.setattr(pr, "_load", lambda n: loaded.append(n) or real_load(n))
    unpinned = _cfg()
    del unpinned["discovery"]["github"]["project"]["number"]
    for i, cfg in enumerate((_cfg(enabled=False), unpinned, {"discovery": {"source": "local-goals"}})):
        d = tmp_path / str(i)
        d.mkdir()
        assert pr.mirror_phase_to_board(str(_sdlc(d, cfg)), "11", "research") is False
    assert "sources" not in loaded, loaded


def test_the_bounded_runner_stops_calling_gh_once_its_budget_is_spent(monkeypatch):
    import subprocess

    def boom(*_a, **_k):
        raise AssertionError("spawned past the budget")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    run = pr._bounded_gh(budget_s=0)
    try:
        run(["project", "list"])
    except RuntimeError as exc:
        assert "budget (0s)" in str(exc), exc                   # the budget actually passed
    else:
        raise AssertionError("a spent budget must raise")


# ---------------------------------------------------------------- review block #1 (PR #296)


def test_a_label_edit_on_an_adopted_board_is_never_reverted_at_the_next_boundary(
        tmp_path, monkeypatch, capsys):
    """The reviewer's flip repro: a HAND-MADE board with no Priority field. The loop must not
    create one (it is their board), so the source of truth never silently moves from the label to
    a field, and a person's `priority:P1` -> `priority:P0` edit is never reverted."""
    gh, board = _world()
    sdlc = _sdlc(tmp_path)
    _start(sdlc, "research", monkeypatch, gh)
    assert gh.field(board, "Priority") is None                  # never created on their board
    gh.issues[0]["labels"] = [{"name": "sdlc:goal"}, {"name": "priority:P0"}]
    _card(board)["labels"] = ["sdlc:goal", "priority:P0"]
    _start(sdlc, "plan", monkeypatch, gh)
    assert not [c for c in gh.calls if c[:2] == ["issue", "edit"]]
    assert _card(board).get("phase") == "P3 PLAN"


def test_a_card_moved_to_done_prints_no_false_stuck_at_done_warning(tmp_path, capsys):
    """The reviewer's t_done repro: the Status write to Done is not a stranded card."""
    prio = {"id": "PVTSSF_prio", "name": "Priority", "options": [
        {"id": "h", "name": "High", "color": "GRAY", "description": ""}]}
    gh, _board = _world(extra_fields=(prio,))
    src = src_mod.GitHubSource(_cfg(), run=gh.loop_run, sdlc_dir=str(_sdlc(tmp_path)))
    src._RETRY_BASE = 0
    src.mark_in_progress("11")
    capsys.readouterr()
    src.mark_qc("11")
    src._set_board_status("11", src.col["done"])
    err = capsys.readouterr().err
    assert "stuck at" not in err, err


P_ALL = {"id": "PVTSSF_prio", "name": "Priority", "options": [
    {"id": "p_%d" % i, "name": "P%d" % i, "color": "GRAY", "description": ""} for i in range(5)]}


def _relabel(gh, board, *labels):
    gh.issues[0]["labels"] = [{"name": n} for n in labels]
    _card(board)["labels"] = list(labels)


def test_on_the_board_sigma_created_the_field_still_wins_as_before(tmp_path, monkeypatch, capsys):
    """Unchanged where #719's rule always applied: Sigma's own board (`setup_created`)."""
    gh, board = _world()
    sdlc = _sdlc(tmp_path, _cfg(setup_created=OURS))
    _start(sdlc, "research", monkeypatch, gh)
    assert _card(board).get("priority") == "P1"                 # created, filled from the label
    _relabel(gh, board, "sdlc:goal", "priority:P0")
    _start(sdlc, "plan", monkeypatch, gh)
    edits = [c for c in gh.calls if c[:2] == ["issue", "edit"]]
    assert any("--add-label" in e and e[e.index("--add-label") + 1] == "priority:P1" for e in edits), edits           # field wins, label corrected
    assert "the field wins" in capsys.readouterr().err


def test_an_adopted_boards_own_priority_field_is_mirrored_label_to_field_only(
        tmp_path, monkeypatch):
    gh, board = _world(extra_fields=(P_ALL,))
    sdlc = _sdlc(tmp_path)
    _start(sdlc, "research", monkeypatch, gh)
    assert _card(board).get("priority") == "P1"                 # a BLANK field is filled
    assert [f["name"] for f in board["fields"]].count("Priority") == 1
    _relabel(gh, board, "sdlc:goal", "priority:P0")             # a person re-prioritises
    before = len(gh.calls)
    _start(sdlc, "plan", monkeypatch, gh)
    assert not [c for c in gh.calls if c[:2] == ["issue", "edit"]]   # never a label rewrite
    assert _card(board).get("priority") == "P1"                 # nor a recognised field overwritten
    writes = [c for c in gh.calls[before:] if c[:2] == ["project", "item-edit"]]
    assert len(writes) == 1 and "PVTSSF_prio" not in writes[0]   # the Phase write only


def test_mirror_priority_opt_in_creates_the_field_on_an_adopted_board(tmp_path, monkeypatch):
    gh, board = _world()
    _start(_sdlc(tmp_path, _cfg(mirror_priority=True)), "research", monkeypatch, gh)
    assert gh.option_names(board, "Priority") == ["P0", "P1", "P2", "P3", "P4"]
    assert _card(board).get("priority") == "P1"


def test_an_uncarded_goal_gets_no_field_write_at_all(tmp_path, monkeypatch, capsys):
    """Survived mutation: without the no-card guard the write went out as `--id None`."""
    gh, board = _world(carded=False)
    assert _start(_sdlc(tmp_path), "research", monkeypatch, gh) == 0
    assert not [c for c in gh.calls if c[:2] == ["project", "item-edit"]], gh.calls
    assert not gh.mutations()                                   # nor a field created for nothing
    lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("sigma:")]
    assert len(lines) == 1 and "no card" in lines[0], lines


def test_a_title_matching_board_never_stands_in_for_an_unreadable_pin(
        tmp_path, monkeypatch, capsys):
    """Survived mutation: the pin check loosened to `not pid` let the owner's board titled
    `widget — SDLC` win when pinned #17 could not be read. The pin is the operator's choice."""
    gh = boardfake.GitHub(owner="acme", repo="widget")
    gh.add_board("widget — SDLC", number=3,
                 fields=[_status_field("3"), dict(P_ALL, id="PVTSSF_prio_3")])
    other = gh.board(number=3)
    gh.add_item(other, 11, Status="In Progress")
    gh.issues = [{"number": 11, "labels": [{"name": "sdlc:goal"}, {"name": "priority:P1"}],
                  "title": "t", "body": ""}]
    assert _start(_sdlc(tmp_path), "research", monkeypatch, gh) == 0     # pinned #17: not there
    assert not [c for c in gh.calls if c[:2] == ["project", "item-edit"]]
    assert not gh.mutations()
    assert gh.field(other, "Phase") is None
    assert len([ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("sigma:")]) == 1


def test_a_same_numbered_board_of_another_owner_is_never_written(tmp_path, monkeypatch):
    gh, board = _world(carded=False)
    theirs = gh.add_board("theirs", number=17, owner="octo", fields=[_status_field("o17")])
    gh.add_item(theirs, 11, Status="In Progress")
    _start(_sdlc(tmp_path), "research", monkeypatch, gh)
    assert not [c for c in gh.calls if c[:2] == ["project", "item-edit"]]
    assert gh.field(theirs, "Phase") is None


def test_a_multi_repo_board_never_writes_another_repos_same_numbered_card(tmp_path, monkeypatch):
    gh, board = _world(extra_fields=(P_ALL,))
    stranger = gh.add_item(board, 11, repo="acme/other", Status="Backlog")
    stranger["labels"] = ["priority:P4"]
    ours = _card(board)
    sdlc = _sdlc(tmp_path)
    _start(sdlc, "research", monkeypatch, gh)
    assert ours.get("phase") == "P2 RESEARCH" and stranger.get("phase") is None
    # the status path too: `_load_items` must key the card by (repository, number)
    src = src_mod.GitHubSource(_cfg(), run=gh.loop_run, sdlc_dir=str(sdlc))
    src._RETRY_BASE = 0
    src.mark_qc("11")
    assert ours.get("status") == "QC", ours
    assert stranger.get("status") == "Backlog" and stranger.get("priority") is None, stranger


def test_a_killed_call_is_never_retried_as_transient(tmp_path, monkeypatch, capsys):
    """`TimeoutExpired`'s text says "timed out", which `_TRANSIENT` matches: without `no_retry` a
    hung item-edit would be re-run 4x and spend the whole boundary budget on one card."""
    gh, board = _world()
    sdlc = _sdlc(tmp_path)
    edits = []

    def run(args):
        if args[:2] == ["project", "item-edit"]:
            edits.append(args)
            raise pr.BoardCallTimeout("Command '['gh', 'project', 'item-edit']' timed out after 20 "
                                      "seconds")
        return gh.loop_run(args)

    monkeypatch.setattr(pr, "_BOARD_RUN", run)
    monkeypatch.setattr(pr, "BOARD_RETRY_BASE", 0)
    assert pr.cmd_start([str(sdlc), "11", "research", "--model", "sonnet"]) == 0
    assert len(edits) == 1, edits
    lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("sigma:")]
    assert len(lines) == 1 and "Phase write failed" in lines[0], lines


def test_duplicate_case_variants_of_phase_are_detected_and_warned_once(
        tmp_path, monkeypatch, capsys):
    ours = {"id": "PVTSSF_ours", "name": "Phase", "options": [
        {"id": "ph_%d" % i, "name": t, "color": "GRAY", "description": ""}
        for i, t in enumerate(TOKENS)]}
    gh, board = _world(extra_fields=(ours, dict(HAND_PHASE)))
    _start(_sdlc(tmp_path), "research", monkeypatch, gh)
    assert _card(board).get("phase") == "P2 RESEARCH"
    assert _card(board)["values"].get("PVTSSF_ours") == "ph_1"             # the EXACT field
    assert "PVTSSF_hand_phase" not in _card(board)["values"]
    lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("sigma:")]
    assert len(lines) == 1 and "'phase'" in lines[0] and "'Phase'" in lines[0], lines


def test_ambiguous_case_variants_with_no_exact_name_are_never_guessed(
        tmp_path, monkeypatch, capsys):
    upper = dict(HAND_PHASE, id="PVTSSF_upper", name="PHASE")
    gh, board = _world(extra_fields=(dict(HAND_PHASE), upper))
    _start(_sdlc(tmp_path), "research", monkeypatch, gh)
    assert not [c for c in gh.calls if c[:2] == ["project", "item-edit"]]
    assert not gh.mutations()                                   # nor a third one created
    lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("sigma:")]
    assert len(lines) == 1 and "PHASE" in lines[0], lines


def test_the_sync_adopts_a_case_variant_priority_by_the_same_rule_label_to_field_only(tmp_path):
    """One helper for both paths: the sync's `_ensure_priority_field` adopts `PRIORITY` exactly as
    the phase path does -- and, not being an exact-name field the sync ever read before, it is
    mirrored label -> field only: a disagreeing field never rewrites a label."""
    prio = dict(P_ALL, id="PVTSSF_upper_prio", name="PRIORITY")
    gh, board = _world(extra_fields=(prio,))
    gh.add_item(board, 12, Status="In Progress", PRIORITY="P3")
    gh.issues.append({"number": 12, "labels": [{"name": "sdlc:goal"}, {"name": "priority:P0"}],
                      "title": "u", "body": ""})
    src = src_mod.GitHubSource(_cfg(), run=gh.loop_run, sdlc_dir=str(_sdlc(tmp_path)))
    src._RETRY_BASE = 0
    src.mark_qc("11")                                           # a board touch: the backlog sync
    assert _card(board).get("priority") == "P1"                 # blank -> filled from the label
    assert _card(board, 12).get("priority") == "P3"
    assert not [c for c in gh.calls if c[:2] == ["issue", "edit"] and "12" in c]


def test_the_sync_mirrors_nothing_when_priority_case_variants_collide(tmp_path, capsys):
    """`item-list` flattens both columns under `priority`: one value overwrites the other, so
    whatever the sync reads may be the wrong field's. It writes nothing and says so once."""
    gh, board = _world(extra_fields=(P_ALL, dict(P_ALL, id="PVTSSF_lower", name="priority")))
    src = src_mod.GitHubSource(_cfg(), run=gh.loop_run, sdlc_dir=str(_sdlc(tmp_path)))
    src._RETRY_BASE = 0
    src.mark_qc("11")
    assert _card(board).get("status") == "QC"
    assert not [c for c in gh.calls if c[:2] == ["project", "item-edit"]
                and "PVTSSF_prio" in c or "PVTSSF_lower" in c]
    assert "differing only in case" in capsys.readouterr().err


# ---------------------------------------------------------------- the bounded runner, for real


def _hanging_gh(tmp_path, grandchild_pidfile, sleep=15):
    """A stand-in `gh` (a local stub, never the real CLI) that spawns a grandchild, recording its
    pid, then hangs."""
    gh = tmp_path / "hanging-gh"
    gh.write_text("#!/bin/sh\nsleep %d &\necho $! > %s\nsleep %d\n"
                  % (sleep, grandchild_pidfile, sleep), encoding="utf-8")
    gh.chmod(0o755)
    return str(gh)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # a zombie still answers kill(0); `ps` tells a reaped/defunct one apart
    import subprocess
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return bool(stat.stdout.strip()) and not stat.stdout.strip().startswith("Z")


import pytest  # noqa: E402


def _clock_starts_when_ready(monkeypatch, pidfile):
    """The seam of the flaky timing (#1073): `communicate(timeout=1)` starts its clock at spawn, so on a loaded machine the
    shell had not yet run `echo $! > pidfile` when the kill landed, and the test read a missing or empty pidfile. Wrap
    Popen so a call waits (bounded, generously, NOT a raised timeout) until the stub reports its grandchild, and only then
    starts the real clock. Returns the list of spawns."""
    import time
    real, spawned = pr.subprocess.Popen, []

    class Ready(real):
        def communicate(self, input=None, timeout=None):
            ceiling = time.monotonic() + 30
            while not (pidfile.exists() and pidfile.read_text().strip()) and time.monotonic() < ceiling:
                time.sleep(0.02)
            return super().communicate(input, timeout)

    def popen(*a, **kw):
        spawned.append(a)
        return Ready(*a, **kw)
    monkeypatch.setattr(pr.subprocess, "Popen", popen)
    return spawned


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX; Windows uses taskkill /T")
def test_a_hung_gh_is_killed_with_its_whole_process_group(tmp_path, monkeypatch):
    import time
    pidfile = tmp_path / "grandchild.pid"
    monkeypatch.setattr(pr, "BOARD_GH", _hanging_gh(tmp_path, pidfile))
    spawned = _clock_starts_when_ready(monkeypatch, pidfile)
    run = pr._bounded_gh(budget_s=30, call_timeout_s=1)
    t0 = time.monotonic()
    with pytest.raises(pr.BoardCallTimeout):
        run(["project", "item-edit"])
    assert time.monotonic() - t0 < 8
    grandchild = int(pidfile.read_text().strip())
    deadline = time.monotonic() + 3
    while _alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(grandchild), "the hung gh's own child outlived the kill"
    with pytest.raises(pr.BoardCallTimeout, match="earlier"):  # nothing else runs this boundary
        run(["project", "item-edit"])
    assert len(spawned) == 1, "the second call must raise without spawning"     # not a wall-clock bound


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX; Windows uses taskkill /T")
def test_control_a_slow_starting_gh_is_what_made_the_kill_test_flaky(tmp_path, monkeypatch):
    """Deterministic control at the seam: a stub that takes 2s to report its grandchild (a loaded machine, made exact)
    against the 1s call timeout. Un-gated, the kill lands first and no pid was ever recorded; gated, the pid is there."""
    import time
    gh = tmp_path / "slow-gh"
    pidfile = tmp_path / "pid"
    gh.write_text("#!/bin/sh\nsleep 2\nsleep 15 &\necho $! > %s\nsleep 15\n" % pidfile, encoding="utf-8")
    gh.chmod(0o755)
    monkeypatch.setattr(pr, "BOARD_GH", str(gh))
    run = pr._bounded_gh(budget_s=30, call_timeout_s=1)
    with pytest.raises(pr.BoardCallTimeout):
        run(["project", "item-edit"])
    assert not (pidfile.exists() and pidfile.read_text().strip()), "un-gated, the kill precedes the pid"
    monkeypatch.undo()
    monkeypatch.setattr(pr, "BOARD_GH", str(gh))
    _clock_starts_when_ready(monkeypatch, pidfile)
    with pytest.raises(pr.BoardCallTimeout):
        pr._bounded_gh(budget_s=30, call_timeout_s=1)(["project", "item-edit"])
    pid = int(pidfile.read_text().strip())
    deadline = time.monotonic() + 3
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(pid)


@pytest.mark.skipif(os.name != "posix", reason="exercised through the POSIX spawn")
def test_the_reap_after_a_kill_is_bounded_even_when_the_kill_misses(tmp_path, monkeypatch):
    """A tree the kill could not reach (Windows' `taskkill` failing, a grandchild that left the
    group) keeps the pipe open: the reap must give up after BOARD_REAP_S, never hang the start."""
    import time
    import signal
    pidfile = tmp_path / "p"
    monkeypatch.setattr(pr, "BOARD_GH", _hanging_gh(tmp_path, pidfile, sleep=12))
    monkeypatch.setattr(pr, "_kill_tree", lambda proc: None)
    monkeypatch.setattr(pr, "BOARD_REAP_S", 1)
    run = pr._bounded_gh(budget_s=30, call_timeout_s=1)
    t0 = time.monotonic()
    try:
        with pytest.raises(pr.BoardCallTimeout):
            run(["project", "item-edit"])
        assert time.monotonic() - t0 < 6
    finally:                                                    # the tree this test left alive
        try:
            os.killpg(os.getpgid(int(pidfile.read_text().strip())), signal.SIGKILL)
        except (OSError, ValueError):
            pass


def test_windows_kill_is_a_bounded_tree_kill(monkeypatch):
    """Windows has no process group to signal: `taskkill /T /F` walks the tree, itself bounded."""
    import subprocess
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["timeout"] = argv, kw.get("timeout")
        raise subprocess.TimeoutExpired(argv, kw.get("timeout"))

    class Proc:
        pid = 4242
        killed = False

        def kill(self):
            Proc.killed = True

    monkeypatch.setattr(pr, "_on_windows", lambda: True)
    monkeypatch.setattr(pr.subprocess, "run", fake_run)
    pr._kill_tree(Proc())
    assert seen["argv"][:3] == ["taskkill", "/T", "/F"] and seen["timeout"] == pr.BOARD_REAP_S
    assert Proc.killed                                          # and the direct child, at least


def test_a_card_moved_to_done_with_a_matching_option_prints_no_stuck_warning(tmp_path, capsys):
    """Its Priority field is still blank, so a mirror WOULD write -- and #1206 would call our own
    Done write a stranded card."""
    gh, board = _world(extra_fields=(P_ALL,))
    src = src_mod.GitHubSource(_cfg(), run=gh.loop_run, sdlc_dir=str(_sdlc(tmp_path)))
    src._RETRY_BASE = 0
    assert src._set_board_status("11", src.col["done"]) is True
    assert _card(board).get("status") == "Done"
    assert "stuck at" not in capsys.readouterr().err


def test_a_stranded_card_with_no_matching_option_is_not_reported_as_skipped(tmp_path, capsys):
    """#1206's warning names a write it skipped; with no P<n> option there was no write to skip."""
    high = {"id": "PVTSSF_prio", "name": "Priority", "options": [
        {"id": "h", "name": "High", "color": "GRAY", "description": ""}]}
    gh, board = _world(extra_fields=(high,), carded=False)
    gh.add_item(board, 11, Status="Done")
    gh.issues.append({"number": 12, "labels": [{"name": "sdlc:goal"}], "title": "u", "body": ""})
    src = src_mod.GitHubSource(_cfg(), run=gh.loop_run, sdlc_dir=str(_sdlc(tmp_path)))
    src._RETRY_BASE = 0
    src.mark_in_progress("12")                                  # the sync visits #11
    assert "stuck at" not in capsys.readouterr().err


def test_a_status_write_on_an_adopted_board_never_rewrites_the_label_from_its_field(tmp_path):
    gh, board = _world(extra_fields=(P_ALL,), carded=False)
    gh.add_item(board, 11, Status="Ready", Priority="P3")       # label says P1
    src = src_mod.GitHubSource(_cfg(), run=gh.loop_run, sdlc_dir=str(_sdlc(tmp_path)))
    src._RETRY_BASE = 0
    src.mark_in_progress("11")
    assert _card(board).get("status") == "In Progress"
    assert not [c for c in gh.calls if c[:2] == ["issue", "edit"] and "11" in c
                and any("priority:" in str(a) for a in c)]


# ---------------------------------------------------------------- review block #2 (PR #296)


def _two_boards_numbered_5(viewer="swapnil"):
    """The reviewer's t_me repro: issue #11 carded on org acme's project #5 AND on the user's own
    project #5. The org's card comes FIRST in the read, as it did in the repro."""
    gh = boardfake.GitHub(owner="acme", repo="widget")
    gh.viewer = viewer
    org = gh.add_board("org board", number=5, owner="acme", fields=[_status_field("a5")])
    mine = gh.add_board("my board", number=5, owner="swapnil", fields=[_status_field("s5")])
    a, b = gh.add_item(org, 11, Status="In Progress"), gh.add_item(mine, 11, Status="In Progress")
    gh.issues = [{"number": 11, "labels": [{"name": "sdlc:goal"}], "title": "t", "body": ""}]
    return gh, org, mine, a, b


def test_at_me_owner_writes_the_viewers_own_board_never_an_orgs_same_numbered_one(
        tmp_path, monkeypatch):
    gh, org, mine, a, b = _two_boards_numbered_5()
    sdlc = _sdlc(tmp_path, _cfg(owner="@me", number=5))
    assert _start(sdlc, "research", monkeypatch, gh) == 0
    assert a.get("phase") is None and gh.field(org, "Phase") is None, a     # the org's: untouched
    assert b.get("phase") == "P2 RESEARCH", b                               # the pinned one: written


def test_an_explicit_user_owner_matches_its_board_whatever_the_case(tmp_path, monkeypatch):
    gh, org, mine, a, b = _two_boards_numbered_5()
    _start(_sdlc(tmp_path, _cfg(owner="Swapnil", number=5)), "research", monkeypatch, gh)
    assert b.get("phase") == "P2 RESEARCH" and a.get("phase") is None


def test_an_explicit_org_owner_still_writes_the_orgs_board(tmp_path, monkeypatch):
    gh, org, mine, a, b = _two_boards_numbered_5()
    _start(_sdlc(tmp_path, _cfg(owner="acme", number=5)), "research", monkeypatch, gh)
    assert a.get("phase") == "P2 RESEARCH" and b.get("phase") is None
    assert gh.field(mine, "Phase") is None


def test_at_me_with_an_unresolvable_viewer_writes_nothing_and_warns_once(
        tmp_path, monkeypatch, capsys):
    gh, org, mine, a, b = _two_boards_numbered_5(viewer=None)
    before = len(gh.calls)
    assert _start(_sdlc(tmp_path, _cfg(owner="@me", number=5)), "research", monkeypatch, gh) == 0
    assert a.get("phase") is None and b.get("phase") is None
    assert gh.field(org, "Phase") is None and gh.field(mine, "Phase") is None
    assert [c[:2] for c in gh.calls[before:]] == [["api", "graphql"]]       # the one read, no write
    lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("sigma:")]
    assert len(lines) == 1 and "could not resolve project.owner '@me'" in lines[0], lines


def test_a_failure_before_the_board_code_runs_is_fail_open(tmp_path, monkeypatch, capsys):
    """Review (a): the outer guard in `mirror_phase_to_board` covers what happens BEFORE
    `set_board_phase` too -- here `sources` cannot be imported. Exit 0, the banner, one line."""
    gh, _board = _world()
    sdlc = _sdlc(tmp_path)
    real_load = pr._load

    def load(name):
        if name == "sources":
            raise ImportError("sources.py is broken")
        return real_load(name)

    monkeypatch.setattr(pr, "_load", load)
    assert _start(sdlc, "research", monkeypatch, gh) == 0
    out, err = capsys.readouterr()
    assert "PHASE START" in out
    lines = [ln for ln in err.splitlines() if "board Phase/Priority" in ln]
    assert len(lines) == 1 and "sources.py is broken" in lines[0], err
    assert not gh.calls


def test_an_unreadable_config_that_names_a_board_warns_once(tmp_path, monkeypatch, capsys):
    gh, _board = _world()
    sdlc = _sdlc(tmp_path)
    text = (sdlc / "config.json").read_text(encoding="utf-8")
    (sdlc / "config.json").write_text(text[:-1], encoding="utf-8")         # truncated: not JSON
    assert pr.mirror_phase_to_board(str(sdlc), "11", "research", run=gh.loop_run) is False
    lines = [ln for ln in capsys.readouterr().err.splitlines() if "board Phase/Priority" in ln]
    assert len(lines) == 1, lines
    assert not gh.calls


def test_an_unreadable_config_with_no_board_says_nothing(tmp_path, monkeypatch, capsys):
    """Review (e): a repo with no board must never read "board ... not written"."""
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    (sdlc / "config.json").write_text('{"discovery": {"source": "local-goals"', encoding="utf-8")
    monkeypatch.setattr(pr, "_BOARD_RUN", lambda _a: (_ for _ in ()).throw(AssertionError("gh")))
    assert pr.mirror_phase_to_board(str(sdlc), "11", "research") is False
    assert "board Phase/Priority" not in capsys.readouterr().err


def test_a_board_created_in_this_run_keeps_field_wins_on_the_status_path(tmp_path):
    """Review (b): `_created_board_now` is what makes a board the loop created THIS run Sigma's on
    the Status path (`_set_board_status` passes `_priority_owned()`). No `setup_created`, no opt-in:
    the field wins on that board, and the moved goal's label is corrected from it."""
    gh = boardfake.GitHub(owner="acme", repo="widget")
    gh.issues = [{"number": 11, "labels": [{"name": "sdlc:goal"}, {"name": "priority:P1"}],
                  "title": "t", "body": ""}]
    cfg = _cfg()
    del cfg["discovery"]["github"]["project"]["number"]
    src = src_mod.GitHubSource(cfg, run=gh.loop_run, sdlc_dir=str(_sdlc(tmp_path, cfg)))
    src._RETRY_BASE = 0
    src._ensure_board(exclude="11")                         # creates the board; #11 left uncarded
    board = gh.boards[0]
    assert src._created_board_now and gh.field(board, "Priority") is not None
    it = gh.add_item(board, 11, Status="Ready", Priority="P3")     # a human set P3 on the board
    src._items[11], src._item_status[11], src._item_priority[11] = it["id"], "Ready", "P3"
    src._issue_labels[11] = gh.issues[0]["labels"]
    assert src._set_board_status("11", src.col["qc"]) is True
    assert _card(board).get("status") == "QC"
    edits = [c for c in gh.calls if c[:2] == ["issue", "edit"]]
    assert any("--add-label" in e and e[e.index("--add-label") + 1] == "priority:P3" for e in edits), edits      # field wins on Sigma's own board


def test_setup_created_names_a_board_by_number_and_owner(tmp_path, monkeypatch):
    """Review (c): a hand-made board that REUSES the number board_setup's board had, under another
    owner, is not Sigma's -- and the older bare-number marker reads as not ours either."""
    for marker in ({"number": 17, "owner": "octo"}, 17, {"number": 17}, {"number": "x", "owner": "acme"}):
        gh, board = _world()
        d = tmp_path / str(len(list(tmp_path.iterdir())))
        d.mkdir()
        _start(_sdlc(d, _cfg(setup_created=marker)), "research", monkeypatch, gh)
        assert gh.field(board, "Priority") is None, marker        # never created: not Sigma's
        assert _card(board).get("phase") == "P2 RESEARCH"
    gh, board = _world()
    (tmp_path / "ours").mkdir()
    _start(_sdlc(tmp_path / "ours", _cfg(setup_created={"number": 17, "owner": "ACME"})),
           "research", monkeypatch, gh)
    assert gh.field(board, "Priority") is not None                # ours (owner by case): created


class _InterruptedProc:
    pid = 424242

    def __init__(self, *_a, **_k):
        pass

    def communicate(self, timeout=None):
        raise KeyboardInterrupt()


def test_ctrl_c_during_a_board_call_kills_the_gh_group_then_propagates(monkeypatch):
    """Review (d): `gh` runs in its own session, so the terminal's SIGINT never reaches it. The
    runner must kill its group before letting the interrupt through, or `gh` is left orphaned."""
    import subprocess
    killed = []
    monkeypatch.setattr(subprocess, "Popen", _InterruptedProc)
    monkeypatch.setattr(pr, "_kill_tree", lambda proc: killed.append(proc.pid))
    run = pr._bounded_gh(budget_s=30, call_timeout_s=10)
    try:
        run(["project", "item-edit"])
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("the interrupt must propagate")
    assert killed == [424242], killed


# ---------------------------------------------------------------- #308: a renamed repo, a race


def _renamed(tmp_path, old_urls_resolve):
    """#308's repro: the repo was renamed `acme/old` -> `acme/widget` and the config still says
    `acme/old`. The board's cards name the CURRENT repo (as GitHub reports them): #11 In Progress,
    #12 Blocked, a same-numbered #11 of ANOTHER repo, and #13 is a goal with no card yet."""
    gh, board = _world()
    gh.add_item(board, 12, Status="Blocked")
    stranger = gh.add_item(board, 11, repo="acme/other", Status="Backlog")
    gh.issues = [{"number": n, "labels": [{"name": "sdlc:goal"}], "title": "t", "body": ""}
                 for n in (11, 12, 13)]
    gh.renames = {"acme/old": "acme/widget"}
    gh.old_urls_resolve = old_urls_resolve
    cfg = _cfg()
    cfg["discovery"]["github"]["repo"] = "acme/old"
    src = src_mod.GitHubSource(cfg, run=gh.loop_run, sdlc_dir=str(_sdlc(tmp_path, cfg)))
    src._RETRY_BASE = 0
    return gh, board, stranger, src


def _status(board, n, repo="acme/widget"):
    return [i.get("status") for i in board["items"]
            if i["content"]["number"] == n and i["content"]["repository"] == repo]


def _check_renamed(gh, board, stranger):
    assert _status(board, 11) == ["QC"], board["items"]            # the transition landed
    assert _status(board, 12) == ["Blocked"], board["items"]       # sync never clobbers
    assert _status(board, 13) == ["Ready"], board["items"]         # the uncarded goal is carded
    assert stranger.get("status") == "Backlog", stranger           # another repo's card: untouched
    added = [c[c.index("--url") + 1] for c in gh.calls if c[:2] == ["project", "item-add"]]
    assert added == ["https://github.com/acme/widget/issues/13"], added
    reads = [c for c in gh.calls if c[:2] == ["api", "repos/acme/old"]]
    assert len(reads) == 1, reads                                  # ONE resolve read per run


def test_a_renamed_repo_with_a_stale_config_keeps_mirroring_the_board(tmp_path):
    """#308 (1) as GitHub behaves (measured, `.sdlc/evidence/308/`): an issue URL under the old name
    does not resolve, so treating every card as uncarded made `item-add` fail and the sync stop."""
    gh, board, stranger, src = _renamed(tmp_path, old_urls_resolve=False)
    src.mark_qc("11")
    _check_renamed(gh, board, stranger)


def test_a_renamed_repo_never_resets_in_flight_cards_to_ready(tmp_path):
    """#308 (1), the #233 reviewer's worse case: were the old URL to resolve, `item-add` hands back
    the EXISTING card, `was_new` is True, and the sync reset In Progress/Blocked cards to Ready."""
    gh, board, stranger, src = _renamed(tmp_path, old_urls_resolve=True)
    src.mark_qc("11")
    _check_renamed(gh, board, stranger)


def test_a_board_of_only_our_repo_costs_no_resolve_read(tmp_path):
    """The resolve read happens only when a card names another repository."""
    gh, board = _world()
    src = src_mod.GitHubSource(_cfg(), run=gh.loop_run, sdlc_dir=str(_sdlc(tmp_path)))
    src._RETRY_BASE = 0
    src.mark_qc("11")
    assert _card(board).get("status") == "QC"
    assert not [c for c in gh.calls if c[:2] == ["api", "repos/acme/widget"]]


def test_an_unresolvable_repo_name_still_never_writes_another_repos_card(tmp_path):
    """The resolve read failing is the strict #233 rule again: another repo's card is not ours."""
    gh, board = _world()
    stranger = gh.add_item(board, 11, repo="acme/other", Status="Backlog")

    def run(args):
        if args[:2] == ["api", "repos/acme/widget"]:
            raise RuntimeError("gh: Not Found (HTTP 404)")
        return gh.loop_run(args)

    src = src_mod.GitHubSource(_cfg(), run=run, sdlc_dir=str(_sdlc(tmp_path)))
    src._RETRY_BASE = 0
    src.mark_qc("11")
    assert _card(board).get("status") == "QC"
    assert stranger.get("status") == "Backlog", stranger


def test_a_stale_config_on_a_board_without_our_cards_adds_under_the_current_name(tmp_path):
    """#308 review (a): no card on the board names the repo yet, so nothing triggers the resolve
    from the item-list read. The first `item-add` still resolves the configured name once (cached)
    and adds by a URL under the CURRENT name -- one under the old name does not resolve."""
    gh, board = _world(carded=False)
    gh.renames = {"acme/old": "acme/widget"}
    cfg = _cfg()
    cfg["discovery"]["github"]["repo"] = "acme/old"
    src = src_mod.GitHubSource(cfg, run=gh.loop_run, sdlc_dir=str(_sdlc(tmp_path, cfg)))
    src._RETRY_BASE = 0
    src.mark_qc("11")
    assert _status(board, 11) == ["QC"], board["items"]
    added = [c[c.index("--url") + 1] for c in gh.calls if c[:2] == ["project", "item-add"]]
    assert added and set(added) == {"https://github.com/acme/widget/issues/11"}, added
    src.mark_in_progress("11")
    assert len([c for c in gh.calls if c[:2] == ["api", "repos/acme/old"]]) == 1


def test_a_failed_repo_resolve_is_retried_after_its_ttl_not_cached_forever(tmp_path, monkeypatch):
    """#308 review (b): a long-lived GitHubSource must not keep one failed resolve for its whole
    life. Within `_REPO_RETRY_S` the failure is cached (bounded: no read per card); after it, the
    next need reads again and the recovered name counts."""
    gh, _board = _world()
    gh.renames = {"acme/old": "acme/widget"}
    down = [True]
    now = [1000.0]
    monkeypatch.setattr(src_mod.time, "monotonic", lambda: now[0])
    attempts = []

    def run(args):
        if args[:2] == ["api", "repos/acme/old"]:
            attempts.append(now[0])
            if down[0]:
                raise RuntimeError("gh: HTTP 502")
        return gh.loop_run(args)

    cfg = _cfg()
    cfg["discovery"]["github"]["repo"] = "acme/old"
    src = src_mod.GitHubSource(cfg, run=run, sdlc_dir=str(_sdlc(tmp_path, cfg)))
    src._RETRY_BASE = 0
    assert src._resolved_repo() == ""
    failed_attempts = len(attempts)
    assert failed_attempts > 0
    down[0] = False
    now[0] = 1299.0
    assert src._resolved_repo() == ""                  # inside the TTL: cached, no second read
    assert len(attempts) == failed_attempts
    now[0] = 1300.0                                    # the documented five minutes have elapsed
    assert src._resolved_repo() == "acme/widget"
    assert attempts[failed_attempts:] == [1300.0]
    now[0] = 1900.0
    assert src._resolved_repo() == "acme/widget"       # a success is kept
    assert attempts[failed_attempts:] == [1300.0]


def test_concurrent_first_starts_the_loser_adopts_the_winners_phase_field(
        tmp_path, monkeypatch, capsys):
    """#308 (3): two first phase starts race to create Phase. The fake rejects the second create
    ("Name has already been taken", as GitHub rejects a duplicate field name); the loser re-reads
    the card, adopts the field the winner made and writes its Phase -- no warning, no skip."""
    gh, board = _world()
    raced = []

    def run(args):
        if any(str(a).startswith("query=mutation") and "createProjectV2Field(" in str(a)
               and 'name: "Phase"' in str(a) for a in args) and not raced:
            raced.append(gh.loop_run(args))          # the OTHER start's create lands first
        return gh.loop_run(args)

    monkeypatch.setattr(pr, "_BOARD_RUN", run)
    monkeypatch.setattr(pr, "BOARD_RETRY_BASE", 0)
    assert pr.cmd_start([str(_sdlc(tmp_path)), "11", "research", "--model", "sonnet"]) == 0
    assert raced, "the race never happened"
    assert [f["name"] for f in board["fields"]].count("Phase") == 1
    assert _card(board).get("phase") == "P2 RESEARCH"
    assert not [ln for ln in capsys.readouterr().err.splitlines() if ln.startswith("sigma:")]


def test_the_fake_rejects_a_duplicate_phase_field(tmp_path, monkeypatch):
    """The premise of the race test: a second create of the same name is refused."""
    gh, board = _world()
    src = src_mod.GitHubSource(_cfg(), run=gh.loop_run)
    doc = src._create_field_mutation(board["id"], "Phase", [("P1 GOAL", "GRAY")])
    gh.loop_run(["api", "graphql", "-f", doc])
    rc, _out, err = gh.gh(["gh", "api", "graphql", "-f", doc])
    assert rc == 1 and "already been taken" in err, err
