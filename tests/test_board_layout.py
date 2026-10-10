"""#234: the canonical board -- `board_spec.py` (fields + six views, built from the kit's own
vocabulary) and `board_layout.py fields|views|verify|spec`, which applies and checks it.

Hermetic: every gh call is answered by `boardfake.GitHub`, whose view model follows the schema as
introspected read-only on 2026-09-29 (`.sdlc/evidence/234/`): a view's name, layout and visible
fields are settable at create, its filter only by update, and group/sort/column-by not at all. No
network, no mutation of real GitHub."""
import importlib.util
import json
import pathlib

import pytest

import boardfake

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-init" / "scripts"
LOOP = ROOT / "skills" / "sigma-loop" / "scripts"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bl = _load(SCRIPTS / "board_layout.py", "board_layout_under_test")
spec_mod = _load(SCRIPTS / "board_spec.py", "board_spec_under_test")
discovery = _load(LOOP / "discovery.py", "board_layout_discovery")
phase_report = _load(LOOP / "phase_report.py", "board_layout_phase_report")

VIEWS = ["Board · by Status", "Priority table", "In flight", "Needs a human", "v1.0 roadmap",
         "Epics"]


def _sdlc(tmp_path, project=None, github=None):
    sdlc = tmp_path / ".sdlc"
    sdlc.mkdir()
    gh = {"repo": "acme/widget", "goal_label": "sdlc:goal",
          "project": dict({"enabled": True}, **(project or {}))}
    gh.update(github or {})
    cfg = {"verify": {"command": "x", "enforce": True},
           "discovery": {"source": "github", "github": gh}}
    (sdlc / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return sdlc


def _ours(tmp_path, **kw):
    """A board board_setup created (the `setup_created` marker names it) with GitHub's built-ins and
    our Status lanes."""
    gh = boardfake.GitHub()
    board = gh.add_board("widget — SDLC", builtins=True)
    board["fields"][1]["options"] = [
        {"id": "o_%s" % n, "name": n, "color": "GRAY", "description": ""}
        for n in ("Backlog", "Ready", "In Progress", "QC", "Done", "Blocked", "Parked")]
    sdlc = _sdlc(tmp_path, project={"number": board["number"], "owner": "acme",
                                    "setup_created": {"number": board["number"], "owner": "acme"}},
                 **kw)
    return gh, board, sdlc


def _run(gh, verb, sdlc, *flags):
    lines = []
    rc = bl.main(["board_layout.py", verb, str(sdlc), *flags], runner=gh.gh, out=lines.append)
    return rc, "\n".join(lines)


def _names(gh, board, view):
    return gh.field_names(board, view["fields"])


def _set_manual(gh, board):
    """Do by hand what the API cannot: group/sort/column-by, and drag the board view first."""
    ids = {f["name"]: f["id"] for f in board["fields"]}
    for v in board["views"]:
        if v["name"] == "Board · by Status":
            v["column_by"], v["sort"] = [ids["Status"]], [(ids["Priority"], "ASC")]
        elif v["name"] == "Priority table":
            v["group_by"] = [ids["Priority"]]
        elif v["name"] == "In flight":
            v["sort"] = [(ids["Updated"], "DESC")]
        elif v["name"] == "v1.0 roadmap":
            v["group_by"] = [ids["Milestone"]]
    first = next(v for v in board["views"] if v["name"] == "Board · by Status")
    board["views"].remove(first)
    board["views"].insert(0, first)


def _src(sdlc):
    sources = _load(LOOP / "sources.py", "board_layout_sources")
    cfg = json.loads((sdlc / "config.json").read_text(encoding="utf-8"))
    return sources.GitHubSource(cfg, run=lambda _a: "")


# ---------------------------------------------------------------- the spec

def test_spec_reuses_the_kits_vocabulary_and_never_copies_it(tmp_path):
    sdlc = _sdlc(tmp_path, project={"columns": {"in_progress": "Doing"}},
                 github={"parked_label": "on-ice"})
    spec = spec_mod.build(_src(sdlc), discovery.PRIORITIES)
    fields = {f["name"]: f for f in spec["fields"]}
    assert [o for o, _c in fields["Priority"]["options"]] == list(discovery.PRIORITIES)
    assert fields["Phase"]["options"] == [
        (tok, phase_report.PHASE_BOARD_COLORS[k]) for k, tok in phase_report.PHASE_TOKENS]
    assert "Doing" in [o for o, _c in fields["Status"]["options"]]
    assert fields["Area"]["type"] == fields["Model tier"]["type"] == "TEXT"
    views = {v["name"]: v for v in spec["views"]}
    assert list(views) == VIEWS
    assert views["In flight"]["filter"] == 'is:open status:Doing,QC,Blocked'
    assert views["Needs a human"]["filter"] == (
        'is:open label:on-ice,sdlc:blocked')
    assert views["v1.0 roadmap"]["filter"] == "priority:P0,P1"
    assert views["Epics"]["filter"] == "label:epic"
    assert [v["layout"] for v in spec["views"]] == [
        "BOARD_LAYOUT", "TABLE_LAYOUT", "TABLE_LAYOUT", "TABLE_LAYOUT", "ROADMAP_LAYOUT",
        "TABLE_LAYOUT"]


def test_a_field_turned_off_in_config_leaves_every_view_that_names_it(tmp_path):
    sdlc = _sdlc(tmp_path, project={"priority_field": False})
    spec = spec_mod.build(_src(sdlc), discovery.PRIORITIES)
    assert "Priority" not in [f["name"] for f in spec["fields"]]
    for v in spec["views"]:
        assert "Priority" not in v["fields"] and v.get("group_by") != "Priority"
        assert "priority:" not in v["filter"]


def test_filter_values_with_spaces_are_quoted():
    assert spec_mod.value("In Progress") == '"In Progress"'
    assert spec_mod.qualifier("Model tier") == '"model tier":'


# ---------------------------------------------------------------- fields

def test_dry_run_makes_no_mutation(tmp_path):
    gh, board, sdlc = _ours(tmp_path)
    for verb in ("fields", "views"):
        rc, text = _run(gh, verb, sdlc)
        assert rc == 0, text
        assert "dry run" in text
    assert gh.mutations() == []


def test_fields_creates_the_four_once_then_nothing(tmp_path):
    gh, board, sdlc = _ours(tmp_path)
    rc, text = _run(gh, "fields", sdlc, "--yes")
    assert rc == 0, text
    assert gh.option_names(board, "Priority") == list(discovery.PRIORITIES)
    assert gh.option_names(board, "Phase") == [t for _k, t in phase_report.PHASE_TOKENS]
    assert gh.field(board, "Area")["options"] is None
    assert gh.field(board, "Model tier")["options"] is None
    made = len(gh.mutations())
    assert made == 4
    rc, text = _run(gh, "fields", sdlc, "--yes")
    assert rc == 0 and len(gh.mutations()) == made, text


def test_fields_adopts_a_case_variant_and_refuses_a_wrong_type(tmp_path):
    gh, board, sdlc = _ours(tmp_path)
    board["fields"].append({"id": "F_p", "name": "priority", "options": [
        {"id": "o%d" % i, "name": p, "color": "GRAY", "description": ""}
        for i, p in enumerate(discovery.PRIORITIES)]})
    board["fields"].append({"id": "F_a", "name": "Area", "options": [
        {"id": "oa", "name": "core", "color": "GRAY", "description": ""}]})
    rc, text = _run(gh, "fields", sdlc, "--yes")
    assert rc == 2, text
    assert "[REFUSED] Area" in text
    assert gh.field(board, "Priority") is None                 # adopted 'priority', no duplicate
    assert [f["name"] for f in board["fields"]].count("Area") == 1


def test_a_missing_option_is_a_manual_step_never_a_rewrite(tmp_path):
    gh, board, sdlc = _ours(tmp_path)
    board["fields"].append({"id": "F_ph", "name": "Phase", "options": [
        {"id": "o%d" % i, "name": t, "color": "GRAY", "description": ""}
        for i, (_k, t) in enumerate(phase_report.PHASE_TOKENS[:-1])]})
    rc, text = _run(gh, "fields", sdlc, "--yes")
    assert "[manual] Phase" in text and "P7 RETRO" in text, text
    assert not any("updateProjectV2Field" in " ".join(c) for c in gh.mutations())


# ---------------------------------------------------------------- views

def test_views_create_all_six_with_layout_filter_and_columns(tmp_path):
    gh, board, sdlc = _ours(tmp_path)
    assert _run(gh, "fields", sdlc, "--yes")[0] == 0
    rc, text = _run(gh, "views", sdlc, "--yes")
    assert rc == 0, text
    spec = {v["name"]: v for v in spec_mod.build(_src(sdlc), discovery.PRIORITIES)["views"]}
    assert [v["name"] for v in board["views"]] == ["View 1"] + VIEWS
    for name in VIEWS:
        (v,) = gh.view(board, name)
        assert v["layout"] == spec[name]["layout"]
        assert (v["filter"] or "") == spec[name]["filter"]
        assert _names(gh, board, v) == spec[name]["fields"]


def test_views_rerun_makes_no_mutation(tmp_path):
    gh, board, sdlc = _ours(tmp_path)
    _run(gh, "fields", sdlc, "--yes")
    _run(gh, "views", sdlc, "--yes")
    made = len(gh.mutations())
    rc, text = _run(gh, "views", sdlc, "--yes")
    assert rc == 0 and len(gh.mutations()) == made, text


def test_views_never_touch_or_delete_a_view_the_user_made(tmp_path):
    gh, board, sdlc = _ours(tmp_path)
    _run(gh, "fields", sdlc, "--yes")
    mine = gh.add_view(board, "Mine", fields=("Title", "Labels"), filter="label:bug")
    before = json.loads(json.dumps(board["views"][:2]))
    _run(gh, "views", sdlc, "--yes")
    assert board["views"][:2] == before and mine in board["views"]
    assert not any("deleteProjectV2View" in " ".join(c) for c in gh.calls)


def test_a_spec_view_that_drifted_is_corrected_and_keeps_extra_columns(tmp_path):
    gh, board, sdlc = _ours(tmp_path)
    _run(gh, "fields", sdlc, "--yes")
    gh.add_view(board, "In flight", fields=("Title", "Labels"), filter="is:open")
    rc, text = _run(gh, "views", sdlc, "--yes")
    assert rc == 0, text
    (v,) = gh.view(board, "In flight")
    assert v["filter"] == "is:open status:\"In Progress\",QC,Blocked"
    assert _names(gh, board, v) == ["Title", "Phase", "Linked pull requests", "Assignees",
                                    "Updated", "Labels"]


def test_duplicate_view_names_are_refused_and_left_alone(tmp_path):
    gh, board, sdlc = _ours(tmp_path)
    _run(gh, "fields", sdlc, "--yes")
    gh.add_view(board, "Epics", filter="x")
    gh.add_view(board, "epics", filter="y")
    rc, text = _run(gh, "views", sdlc, "--yes")
    assert rc == 2, text
    assert "[REFUSED] view 'Epics'" in text
    assert sorted(v["filter"] for v in board["views"] if v["name"].lower() == "epics") == ["x", "y"]
    assert len(gh.view(board, "In flight")) == 1                # the others still applied


def test_views_need_the_fields_first_and_change_nothing_without_them(tmp_path):
    gh, board, sdlc = _ours(tmp_path)
    rc, text = _run(gh, "views", sdlc, "--yes")
    assert rc == 1, text
    assert "fields" in text and "Priority" in text
    assert gh.mutations() == []


def test_a_pinned_board_this_setup_did_not_create_needs_number(tmp_path):
    gh = boardfake.GitHub()
    board = gh.add_board("hand made", builtins=True)
    sdlc = _sdlc(tmp_path, project={"number": board["number"], "owner": "acme"})
    for verb in ("fields", "views"):
        rc, text = _run(gh, verb, sdlc, "--yes")
        assert rc == 2 and "--number" in text, text
    assert gh.mutations() == []
    rc, text = _run(gh, "fields", sdlc, "--number", str(board["number"]), "--yes")
    assert rc == 0 and gh.mutations(), text


def test_manual_steps_are_printed_only_for_what_still_differs(tmp_path):
    gh, board, sdlc = _ours(tmp_path)
    _run(gh, "fields", sdlc, "--yes")
    rc, text = _run(gh, "views", sdlc, "--yes")
    assert "[manual] view 'Priority table': group by" in text
    assert "[manual] view 'In flight': sort" in text
    assert "[manual] view 'Board · by Status': column by" in text
    assert "[manual] default view" in text
    _set_manual(gh, board)
    rc, text = _run(gh, "views", sdlc, "--yes")
    assert rc == 0 and "group by" not in text and "sort" not in text, text
    assert "[manual] default view" not in text


# ---------------------------------------------------------------- verify

def test_verify_is_read_only_and_names_each_difference(tmp_path):
    gh, board, sdlc = _ours(tmp_path)
    rc, text = _run(gh, "verify", sdlc)
    assert rc == 1, text
    for what in ("field 'Priority'", "field 'Phase'", "field 'Area'", "view 'Epics'"):
        assert what in text, what
    assert gh.mutations() == []
    assert all(c[:2] != ["auth", "status"] for c in gh.calls)   # a read-only token is enough


def test_verify_passes_on_a_finished_board_and_fails_on_one_changed_property(tmp_path):
    gh, board, sdlc = _ours(tmp_path)
    board["workflows"]["Item closed"] = True
    _run(gh, "fields", sdlc, "--yes")
    _run(gh, "views", sdlc, "--yes")
    _set_manual(gh, board)
    rc, text = _run(gh, "verify", sdlc)
    assert rc == 0, text
    gh.view(board, "In flight")[0]["filter"] = "is:open"
    rc, text = _run(gh, "verify", sdlc)
    assert rc == 1 and "view 'In flight': filter" in text, text


def test_verify_fails_until_the_manual_only_properties_are_set(tmp_path):
    """The API-built half is not the whole board: group/sort/column-by and the default view are
    part of acceptance, so verify must stay red until a person has set them."""
    gh, board, sdlc = _ours(tmp_path)
    board["workflows"]["Item closed"] = True
    _run(gh, "fields", sdlc, "--yes")
    _run(gh, "views", sdlc, "--yes")
    rc, text = _run(gh, "verify", sdlc)
    assert rc == 1, text
    for what in ("view 'Priority table': group by", "view 'In flight': sort",
                 "view 'Board · by Status': column by", "default view"):
        assert "[differs] " + what in text, what


def test_verify_reads_any_board_by_number_without_ownership(tmp_path):
    gh = boardfake.GitHub()
    board = gh.add_board("hand made", builtins=True)
    sdlc = _sdlc(tmp_path, project={"number": board["number"], "owner": "acme"})
    rc, text = _run(gh, "verify", sdlc)
    assert rc == 1 and "REFUSED" not in text, text


def test_spec_prints_json(tmp_path):
    gh, board, sdlc = _ours(tmp_path)
    rc, text = _run(gh, "spec", sdlc)
    assert rc == 0 and [v["name"] for v in json.loads(text)["views"]] == VIEWS
    assert gh.calls == []


# ---------------------------------------------------------------- the fake follows the schema

def test_the_fake_rejects_a_filter_at_create_as_the_schema_does(tmp_path):
    """Control for the fake itself: `CreateProjectV2ViewInput` has no `filter`, so a create that
    carries one must fail here as it would on GitHub; otherwise the tests above prove nothing about
    the create/update split."""
    gh, board, _sdlc_ = _ours(tmp_path)
    rc, _out, err = gh.gh(["gh", "api", "graphql", "-f", 'query=mutation { createProjectV2View('
                           'input: {projectId: "%s", name: "x", layout: TABLE_LAYOUT, filter: '
                           '"is:open"}) { projectV2View { id } } }' % board["id"]])
    assert rc == 1 and "filter" in err
    rc, _out, err = gh.gh(["gh", "api", "graphql", "-f", 'query=mutation { createProjectV2View('
                           'input: {projectId: "%s", name: "x", layout: TABLE_LAYOUT, '
                           'configuration: {groupByFieldIds: []}}) { projectV2View { id } } }'
                           % board["id"]])
    assert rc == 1 and "groupByFieldIds" in err


@pytest.mark.parametrize("verb", ["fields", "views", "verify"])
def test_no_board_pinned_is_refused(tmp_path, verb):
    gh = boardfake.GitHub()
    sdlc = _sdlc(tmp_path)
    rc, text = _run(gh, verb, sdlc)
    assert rc == 2 and "board_setup.py create" in text, text
