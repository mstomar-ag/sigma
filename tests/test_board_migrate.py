"""#696: the opt-in migration that adds a `Ready` lane to an EXISTING board.

The danger this whole module exists to avoid was established empirically on a throwaway project
before a line of it was written:

    [2] append option WITH ids -> card keeps Status:   PASS (status='In Progress')
    [3] rewrite WITHOUT ids    -> card LOSES Status:   PASS (status=None)

`ProjectV2SingleSelectFieldOptionInput` accepts an `id`. Re-send every existing option WITH its id
and cards keep their Status; omit the ids and every card's Status is wiped to null. `sources.py`'s
own `_options_mutation` builds options WITHOUT ids — correct for a board the kit creates, and
catastrophic for an adopted one — which is why this has its own builder and does not reuse it.
"""
import json, pathlib, re, importlib.util

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-doctor" / "scripts"


def _mod():
    spec = importlib.util.spec_from_file_location("board_migrate", S / "board_migrate.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


OPTIONS = [{"id": "o_backlog", "name": "Backlog", "color": "GRAY", "description": ""},
           {"id": "o_ip", "name": "In Progress", "color": "YELLOW", "description": "wip"},
           {"id": "o_done", "name": "Done", "color": "GREEN", "description": ""}]


def _field_payload(options=None, name="Status"):
    return json.dumps({"data": {"repositoryOwner": {"projectV2": {"id": "PVT_x", "field": {
        "id": "F_status", "name": name, "options": options if options is not None else OPTIONS}}}}})


def _world(options=None, statuses=None):
    """Fake gh: the field read, the mutation, and the post-condition card re-read."""
    state = {"options": list(options if options is not None else OPTIONS),
             "statuses": dict(statuses or {1: "In Progress", 2: "Done"})}
    calls = []

    def run(args):
        calls.append(list(args))
        q = next((a for a in args if a.startswith("query=")), "")
        if "archiveProjectV2Item" in q:
            return "{}"
        if "updateProjectV2Field" in q:
            state["mutated"] = q
            return json.dumps({"data": {"updateProjectV2Field": {"projectV2Field": {"id": "F_status"}}}})
        if "items(" in q:
            nodes = [{"id": "PVTI_%s" % n, "content": {"__typename": "Issue", "number": n},
                      "fieldValueByName": ({"name": s} if s else None)}
                     for n, s in state["statuses"].items()]
            return json.dumps({"data": {"repositoryOwner": {"projectV2": {"items": {
                "nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}})
        return _field_payload(state["options"])

    run.calls = calls
    run.state = state
    return run


def _mutations(run):
    return [c for c in run.calls if any("updateProjectV2Field" in a for a in c)]


# --- the safety property this module exists for ---------------------------------------------------

def test_the_mutation_carries_an_id_for_every_pre_existing_option():
    m = _mod()
    q = m.build_options_mutation("F_status", OPTIONS, "Ready")
    for opt in OPTIONS:
        assert 'id: "%s"' % opt["id"] in q, opt["name"]


def test_the_new_option_is_the_only_one_without_an_id():
    m = _mod()
    q = m.build_options_mutation("F_status", OPTIONS, "Ready")
    assert q.count("id: ") == len(OPTIONS)          # the new option contributes none
    assert 'name: "Ready"' in q


def test_every_existing_option_survives_by_name_colour_and_description():
    """Additive only: the migration must never remove or rename an option, because either would
    orphan cards on a board sigma does not own. Colour and description ride along too — they
    are part of the option and re-sending without them would silently reset the board's appearance."""
    m = _mod()
    q = m.build_options_mutation("F_status", OPTIONS, "Ready")
    for opt in OPTIONS:
        assert 'name: "%s"' % opt["name"] in q
        assert "color: %s" % opt["color"] in q
    assert 'description: "wip"' in q


def test_ready_is_appended_after_the_existing_options_not_inserted():
    m = _mod()
    q = m.build_options_mutation("F_status", OPTIONS, "Ready")
    assert q.index('name: "Ready"') > q.index('name: "Done"')


# --- it must not run unless a human asks it to ----------------------------------------------------

def test_dry_run_is_the_default_and_makes_no_mutating_call():
    m = _mod()
    run = _world()
    assert m.main(["board_migrate.py", "--owner", "acme", "--project", "8"], run=run) == 0
    assert _mutations(run) == []


def test_refuses_without_an_explicitly_identified_project():
    m = _mod()
    for argv in (["x"], ["x", "--owner", "acme"], ["x", "--project", "8"]):
        run = _world()
        assert m.main(argv, run=run) != 0
        assert run.calls == []


def test_apply_is_required_to_write():
    m = _mod()
    run = _world()
    assert m.main(["x", "--owner", "acme", "--project", "8", "--apply"], run=run) == 0
    assert len(_mutations(run)) == 1


def test_a_board_that_already_has_ready_is_a_no_op():
    m = _mod()
    run = _world(options=OPTIONS + [{"id": "o_r", "name": "Ready", "color": "BLUE", "description": ""}])
    assert m.main(["x", "--owner", "acme", "--project", "8", "--apply"], run=run) == 0
    assert _mutations(run) == []


# --- the post-condition: abort loudly if any card lost its Status ---------------------------------

def test_apply_verifies_no_card_lost_its_status_and_succeeds_when_none_did():
    m = _mod()
    run = _world(statuses={1: "In Progress", 2: "Done"})
    assert m.main(["x", "--owner", "acme", "--project", "8", "--apply"], run=run) == 0


def test_apply_fails_loudly_when_a_card_lost_its_status(capsys):
    """The exact failure mode rehearsal step [3] produced. If it ever happens, the operator must be
    told which cards, not left to discover a silently blanked board later."""
    m = _mod()
    run = _world(statuses={1: "In Progress", 2: "Done"})
    real = run
    def losing(args):
        # Blank card 1 as a side effect of the WRITE, so the "before" snapshot still sees its real
        # Status and only the post-write re-read shows the damage — the actual shape of failure [3].
        out = real(args)
        if any("updateProjectV2Field" in a for a in args):
            real.state["statuses"] = {1: None, 2: "Done"}
        return out
    losing.calls = real.calls
    losing.state = real.state
    rc = m.main(["x", "--owner", "acme", "--project", "8", "--apply"], run=losing)
    assert rc != 0
    assert "1" in capsys.readouterr().out


def test_a_card_with_no_status_before_the_write_is_not_reported_as_lost():
    """A blank card stays blank — that is not damage, and reporting it would cry wolf."""
    m = _mod()
    run = _world(statuses={1: None, 2: "Done"})
    assert m.main(["x", "--owner", "acme", "--project", "8", "--apply"], run=run) == 0


# --- #707: the migration must SEED the lane it creates, or the board deadlocks -------------------
# Adding `Ready` makes Status authoritative while no card is in Ready, so next_pending() returns
# None -> loop reports DONE -> _sync_backlog (the only thing that would promote the cards) never
# runs, because it fires from a status WRITE which needs a goal to have been picked. Permanent.

def _seedable(number, status="Backlog", labels=("sdlc:goal",)):
    return {"id": "PVTI_%d" % number, "status": status, "labels": list(labels),
            "content": {"number": number}}


def _world2(options, cards):
    """Adds `item-list --query` and `item-edit` to the fake, which seeding needs."""
    state = {"options": list(options), "cards": [dict(c) for c in cards]}
    calls = []

    def _a(args, f):
        return args[args.index(f) + 1] if f in args else None

    def run(args):
        calls.append(list(args))
        if args[:3] == ["gh", "project", "item-edit"]:
            for c in state["cards"]:
                if c["id"] == _a(args, "--id"):
                    c["status"] = "Ready" if _a(args, "--single-select-option-id") == "o_ready" else "?"
            return ""
        if args[:3] == ["gh", "project", "item-list"]:
            # `--query` filters SERVER-SIDE (verified live), so the fake must too or seeding looks
            # correct here while moving the wrong cards for real.
            q, out = _a(args, "--query") or "", state["cards"]
            # Parse rather than substring-match: the real query quotes the value (`status:"Backlog"`,
            # needed for multi-word columns like "In Progress"), and a naive `"status:Backlog" in q`
            # silently matched NOTHING and let every card through — which is how the first version of
            # this fake made a broken filter look correct. Both forms verified live, same 45 items.
            for m in re.finditer(r'(\w+):(?:"([^"]*)"|(\S+))', q):
                key, val = m.group(1), m.group(2) if m.group(2) is not None else m.group(3)
                if key == "status":
                    out = [c for c in out if c.get("status") == val]
                elif key == "label":
                    out = [c for c in out if val in (c.get("labels") or [])]
                elif key == "is" and val == "open":
                    out = [c for c in out if c.get("state", "OPEN") == "OPEN"]
            return json.dumps({"items": out})
        q = next((a for a in args if a.startswith("query=")), "")
        if "updateProjectV2Field" in q:
            state["options"] = state["options"] + [{"id": "o_ready", "name": "Ready",
                                                    "color": "BLUE", "description": ""}]
            return json.dumps({"data": {"updateProjectV2Field": {"projectV2Field": {"id": "F_status"}}}})
        if "items(" in q:
            nodes = [{"id": c["id"], "content": {"__typename": "Issue", "number": c["content"]["number"]},
                      "fieldValueByName": ({"name": c["status"]} if c.get("status") else None)}
                     for c in state["cards"]]
            return json.dumps({"data": {"repositoryOwner": {"projectV2": {"items": {
                "nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}})
        return _field_payload(state["options"])

    run.calls = calls
    run.state = state
    return run


def _seeds(run):
    return [c for c in run.calls if len(c) > 2 and c[:3] == ["gh", "project", "item-edit"]]


def test_apply_seeds_the_lane_it_creates():
    m = _mod()
    run = _world2(OPTIONS, [_seedable(1), _seedable(2)])
    assert m.main(["x", "--owner", "acme", "--project", "8", "--apply"], run=run) == 0
    assert len(_seeds(run)) == 2
    assert all(c["status"] == "Ready" for c in run.state["cards"])


def test_dry_run_reports_the_seed_count_and_writes_nothing(capsys):
    m = _mod()
    run = _world2(OPTIONS, [_seedable(1), _seedable(2)])
    assert m.main(["x", "--owner", "acme", "--project", "8"], run=run) == 0
    assert _seeds(run) == []
    assert "2" in capsys.readouterr().out


def test_a_board_already_stuck_with_an_empty_ready_lane_is_REPAIRED_not_skipped():
    """The whole point: a board with Ready but nothing in it is not a no-op, it IS the bug."""
    m = _mod()
    opts = OPTIONS + [{"id": "o_ready", "name": "Ready", "color": "BLUE", "description": ""}]
    run = _world2(opts, [_seedable(1), _seedable(2)])
    assert m.main(["x", "--owner", "acme", "--project", "8", "--apply"], run=run) == 0
    assert _mutations(run) == []                       # lane already there — no field rewrite
    assert len(_seeds(run)) == 2                       # but the cards ARE seeded


def test_only_open_goal_labelled_backlog_cards_are_seeded():
    m = _mod()
    cards = [_seedable(1),                                        # yes
             _seedable(2, status="In Progress"),                  # wrong column
             _seedable(3, status="Done"),                         # wrong column
             _seedable(4, labels=("bug",)),                       # not a goal
             _seedable(5, status="Blocked")]                      # wrong column
    run = _world2(OPTIONS, cards)
    m.main(["x", "--owner", "acme", "--project", "8", "--apply"], run=run)
    moved = {c["content"]["number"] for c in run.state["cards"] if c["status"] == "Ready"}
    assert moved == {1}


def test_nothing_to_seed_is_reported_not_treated_as_failure():
    m = _mod()
    run = _world2(OPTIONS, [_seedable(1, status="Done")])
    assert m.main(["x", "--owner", "acme", "--project", "8", "--apply"], run=run) == 0
    assert _seeds(run) == []


# --- #1491: an honest summary, an equality post-condition, every card kind covered ----------------

def _world10(lane=False, clobber_pr=False):
    """10 cards: 7 issues with a Status (3 in Backlog with the goal label), 1 issue with none, 2 PRs."""
    cards = []
    for i, st in enumerate(["Backlog"] * 3 + ["In Progress", "In Progress", "Done", "Done"], 1):
        cards.append({"id": "PVTI_%d" % i, "kind": "Issue", "num": i, "status": st, "labels": ["sdlc:goal"]})
    cards.append({"id": "PVTI_8", "kind": "Issue", "num": 8, "status": None, "labels": []})
    cards.append({"id": "PVTI_9", "kind": "PullRequest", "num": 1, "status": "Done", "labels": []})
    cards.append({"id": "PVTI_10", "kind": "PullRequest", "num": 1, "status": "Done", "labels": []})  # same number, other repo
    opts = OPTIONS + ([{"id": "o_ready", "name": "Ready", "color": "BLUE", "description": ""}] if lane else [])
    state = {"options": opts, "cards": cards}
    calls = []

    def run(args):
        calls.append(list(args))
        if args[:3] == ["gh", "project", "item-edit"]:
            for c in cards:
                if c["id"] == args[args.index("--id") + 1]:
                    c["status"] = "Ready"
            return ""
        if args[:3] == ["gh", "project", "item-list"]:
            return json.dumps({"items": [{"id": c["id"], "content": {"number": c["num"]}} for c in cards
                                         if c["status"] == "Backlog" and "sdlc:goal" in c["labels"]]})
        q = next((a for a in args if a.startswith("query=")), "")
        if "updateProjectV2Field" in q:
            state["options"] = state["options"] + [{"id": "o_ready", "name": "Ready", "color": "BLUE", "description": ""}]
            if clobber_pr:
                cards[8]["status"] = "In Progress"
            return json.dumps({"data": {"updateProjectV2Field": {"projectV2Field": {"id": "F_status"}}}})
        if "items(" in q:
            nodes = [{"id": c["id"], "content": ({"__typename": "Issue", "number": c["num"]} if c["kind"] == "Issue"
                                                 else {"__typename": "PullRequest", "number": c["num"]}),
                      "fieldValueByName": ({"name": c["status"]} if c["status"] else None)} for c in cards]
            return json.dumps({"data": {"repositoryOwner": {"projectV2": {"items": {
                "nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}})
        return _field_payload(state["options"])

    run.calls, run.state = calls, state
    return run


def test_ten_card_board_summary_counts_add_up(capsys):
    m = _mod()
    run = _world10()
    assert m.main(["x", "--owner", "acme", "--project", "8", "--apply"], run=run) == 0
    out = capsys.readouterr().out
    assert "board: 10 card(s) (8 issues, 2 pull requests, 0 drafts)" in out
    assert "10 unchanged (1 had no Status), 0 changed" in out
    assert "3 moved Backlog -> 'Ready'" in out and "subset" in out
    assert "now: 3 Ready, 7 elsewhere" in out
    assert "kept their Status" not in out


def test_a_pull_request_card_whose_status_changes_fails_the_post_condition(capsys):
    m = _mod()
    run = _world10(clobber_pr=True)
    assert m.main(["x", "--owner", "acme", "--project", "8", "--apply"], run=run) == 1
    assert "PVTI_9" in capsys.readouterr().out
    assert not _seeds(run)


def test_an_existing_lane_prints_no_kept_their_status_claim(capsys):
    m = _mod()
    run = _world10(lane=True)
    assert m.main(["x", "--owner", "acme", "--project", "8", "--apply"], run=run) == 0
    out = capsys.readouterr().out
    assert "kept their Status" not in out and "unchanged" not in out
    assert "no option write" in out and "3 moved Backlog -> 'Ready'" in out
    assert not any("items(" in " ".join(c) for c in run.calls)


def test_snapshot_keys_by_item_id_so_equal_numbers_do_not_collapse():
    m = _mod()
    snap = m.card_statuses("acme", 8, "Status", _world10())
    assert len(snap) == 10
