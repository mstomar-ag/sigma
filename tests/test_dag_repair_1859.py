"""#1859: the map-backed blocker repair reaches every legacy-labelled corpus blocker, not only ten.

Controls, each run red once on its own and restored (the test each one turns red):
* the stalest-first ordering dropped (newest first again) -> `test_second_pass_reads_refs_the_first_did_not`
* the inside reserve dropped (outside fills the cap) -> `test_inside_blockers_keep_reads_beside_many_outside_ones`
* the stamp write dropped -> `test_second_pass_reads_refs_the_first_did_not` and the golden
* the map flag dropped from the result -> `test_preview_row_names_the_missing_notes_on_the_map_path_only`
No test reaches the network; every issue number is a fixture issue.
"""
import copy
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_blocker_rank_repair as br  # noqa: E402
import test_dag_repair_1626 as t  # noqa: E402

GOAL = "sdlc:goal"
LEGACY = "sdlc:needs-confirmation"
NOTE = "other-repository notes not counted on the map path"


def _legacy_board(n, outside=0):
    """Issue 500 (P0) is blocked by `n` legacy-labelled corpus issues (and `outside` open outside ones)."""
    body = " ".join("blocked by #%d" % i for i in range(1, n + 1))
    body += " " + " ".join("blocked by #%d" % i for i in range(200, 200 + outside))
    issues = [br._issue(500, [GOAL, "priority:P0"], body=body)]
    issues += [br._issue(i, [GOAL, "priority:P0", LEGACY]) for i in range(1, n + 1)]
    issues += [br._issue(i, ["priority:P0"]) for i in range(200, 200 + outside)]
    nodes = [{"number": i, "kind": "issue", "state": "open", "life": "none", "prio": "P0", "in_corpus": False}
             for i in range(200, 200 + outside)]
    return issues, nodes


def _passes(tmp_path, issues, nodes, mode, count):
    """Run `count` apply passes against a board where a stripped label is really gone; -> list of stripped sets."""
    d, cfg = t._setup(tmp_path, issues, mode=mode, save=(mode == "on"), extra_nodes=nodes)
    board = copy.deepcopy(issues)
    seen = []
    for _ in range(count):
        res, _run = t._run(board, d, cfg)
        seen.append(set(res["stripped"]))
        for it in board:
            if str(it["number"]) in res["stripped"]:
                it["labels"] = [l for l in it["labels"] if l["name"] != LEGACY]
    return seen, d


def test_golden_25_legacy_blockers_are_all_stripped_in_three_map_passes_as_on_the_old_path(tmp_path):
    issues, nodes = _legacy_board(25)
    old, _ = _passes(tmp_path / "old", issues, nodes, "off", 3)
    new, d = _passes(tmp_path / "new", issues, nodes, "on", 3)
    everyone = {str(i) for i in range(1, 26)}
    assert set().union(*old) == everyone                       # the golden is not vacuous
    assert set().union(*new) == set().union(*old)
    assert [len(s) for s in new] == [10, 10, 5]
    assert (d / "state" / "blocker-inside-read.json").exists()


def test_second_pass_reads_refs_the_first_did_not(tmp_path):
    issues, nodes = _legacy_board(25)
    new, _ = _passes(tmp_path, issues, nodes, "on", 2)
    assert new[0] and new[1] and not (new[0] & new[1])


def test_inside_blockers_keep_reads_beside_many_outside_ones(tmp_path):
    issues, nodes = _legacy_board(12, outside=15)
    d, cfg = t._setup(tmp_path, issues, extra_nodes=nodes)
    res, _run = t._run(issues, d, cfg)
    assert len(res["stripped"]) == 5                           # half the cap is held for the corpus
    assert res.get("unread") == 10


def test_parked_goal_labelled_blocker_is_not_raised_or_stripped_on_the_map_path(tmp_path):
    issues = [br._issue(9, [GOAL, "priority:P0"], body="blocked by #7"),
              br._issue(7, [GOAL, "priority:P3", LEGACY])]
    parked = {"number": 7, "kind": "issue", "state": "open", "life": "parked", "prio": "P3", "in_corpus": True}
    d, cfg = t._setup(tmp_path, issues, extra_nodes=[parked])
    mp = br._mod("sources").GitHubSource(cfg, run=br._runner(issues), sdlc_dir=str(d))._repair_map_input()
    assert "7" not in mp["corpus"]
    res, run = t._run(issues, d, cfg)
    assert res["raised"] == [] and res["stripped"] == [] and br._writes(run) == []


def test_preview_row_names_the_missing_notes_on_the_map_path_only(tmp_path, capsys):
    rec = br._mod("reconcile")
    issues = [br._issue(9, [GOAL, "priority:P0"], body="blocked by #7"), br._issue(7, [GOAL, "priority:P3"])]
    d, cfg = t._setup(tmp_path / "a", issues)
    assert rec.blockers_cmd(str(d), cfg, run=br._runner(copy.deepcopy(issues))) == 0
    on = capsys.readouterr().out
    d2, cfg2 = t._setup(tmp_path / "b", issues, mode="off", save=False)
    assert rec.blockers_cmd(str(d2), cfg2, run=br._runner(copy.deepcopy(issues))) == 0
    off = capsys.readouterr().out
    assert NOTE in on and NOTE not in off
    assert on.replace(" (" + NOTE + ")", "") == off


def test_a_dry_pass_and_a_preview_stamp_nothing(tmp_path):
    issues, nodes = _legacy_board(12)
    d, cfg = t._setup(tmp_path, issues, extra_nodes=nodes)
    src = br._mod("sources").GitHubSource(cfg, run=br._runner(copy.deepcopy(issues)), sdlc_dir=str(d))
    src.repair_blocker_ranks(apply=False)
    src.repair_blocker_ranks(preview=True)
    assert not (d / "state" / "blocker-inside-read.json").exists()


def test_the_stamp_store_is_bounded_and_keeps_the_newest(tmp_path):
    issues, nodes = _legacy_board(2)
    d, cfg = t._setup(tmp_path, issues, extra_nodes=nodes)
    src = br._mod("sources").GitHubSource(cfg, run=br._runner(issues), sdlc_dir=str(d))
    src._remember_inside_read({}, [str(i) for i in range(1, 700)])
    src._remember_inside_read(src._inside_read_known(), ["9999"])
    data = json.loads((d / "state" / "blocker-inside-read.json").read_text(encoding="utf-8"))
    assert len(data) == src._INSIDE_READ_MAX and "9999" in data
