"""#1614: root-cause lines for stuck chains (`dag_stuck.py`) in the pick, status and the doctor row.

The controls, each run red once on its own and restored: the ordering (sort by number only), the
"every held goal" clause of the pick (print the new text whenever any goal is held), the
"leaf needs a person" clause (treat every chain as stuck), the five-number cap, the mode-off clause
(read the map regardless of mode). No test reaches the network: `gh` is replaced by a function that
aborts, and the pick's source is an in-memory fake."""
import importlib.util
import json
import pathlib
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_pick_dependency_gate as pg  # noqa: E402  (its board fake and mirror fixtures)

ROOT = pathlib.Path(__file__).resolve().parent.parent
S = ROOT / "skills" / "sigma-loop" / "scripts"


def _mod(name, base=S):
    spec = importlib.util.spec_from_file_location(name, base / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


st = _mod("dag_stuck")
ds = st.dag_chain.dag_store
status = _mod("status", ROOT / "skills" / "sigma-status" / "scripts")
doctor = _mod("doctor", ROOT / "skills" / "sigma-doctor" / "scripts")

NOW = time.time()


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _node(number, life="goal", state="open", kind="issue"):
    return {"number": number, "kind": kind, "state": state, "life": life, "prio": None, "in_corpus": True}


def _store(sdlc, nodes, edges, mode=None):
    sdlc = pathlib.Path(sdlc)
    (sdlc / "state").mkdir(parents=True, exist_ok=True)
    h = {"repo": "owner/repo", "built_at": _iso(NOW - 60), "refreshed_at": _iso(NOW - 60),
         "last_ok_at": _iso(NOW - 60), "last_error": None, "partial": False}
    ok, why = ds.save(sdlc, {"header": h, "nodes": nodes,
                             "edges": [{"blocker": a, "blocked": b, "source": "body"} for a, b in edges]})
    assert ok, why
    if mode:
        (sdlc / "config.json").write_text(json.dumps({"discovery": {"dag": {"mode": mode}}}))
    return sdlc


def _lines(tmp_path, nodes, edges, **kw):
    return st.lines(_store(tmp_path / ".sdlc", nodes, edges, **kw), now=NOW)


# ---------------------------------------------------------------- golden lines

def test_parked_root_line(tmp_path):
    got = _lines(tmp_path, [_node(1, "parked"), _node(2), _node(3)], [(1, 2), (2, 3)])
    assert got == ["#2, #3 wait on #1's decision; /sigma-unpark #1"]


def test_unarmed_root_line(tmp_path):
    got = _lines(tmp_path, [_node(1, "needs-label"), _node(2)], [(1, 2)])
    assert got == ["#2 wait on #1's decision; /sigma-promote #1"]


def test_pull_request_root_line(tmp_path):
    got = _lines(tmp_path, [_node(1, "none", kind="pr"), _node(2)], [(1, 2)])
    assert got == ["#2 wait on #1's decision; PR #1 is open"]


def test_cycle_line(tmp_path):
    got = _lines(tmp_path, [_node(1), _node(2)], [(1, 2), (2, 1)])
    assert got == ["#1, #2 wait on each other"]


def test_more_than_five_goals_are_cut(tmp_path):
    """control: drop the cap -> all eight numbers print and this goes red."""
    nodes = [_node(1, "parked")] + [_node(n) for n in range(2, 10)]
    got = _lines(tmp_path, nodes, [(1, n) for n in range(2, 10)])
    assert got == ["#2, #3, #4, #5, #6 and 3 more wait on #1's decision; /sigma-unpark #1"]


# ---------------------------------------------------------------- what is stuck

def test_a_chain_whose_leaves_are_all_workable_is_not_stuck(tmp_path):
    """control: report every chain as stuck -> this goes red."""
    assert _lines(tmp_path, [_node(1), _node(2), _node(3)], [(1, 2), (2, 3)]) == []


def test_one_workable_and_one_parked_leaf_is_stuck_on_the_parked_one_only(tmp_path):
    nodes = [_node(1), _node(2, "parked"), _node(3)]
    got = _lines(tmp_path, nodes, [(1, 3), (2, 3)])
    assert got == ["#3 wait on #2's decision; /sigma-unpark #2"]


def test_most_freeing_first_ties_by_number(tmp_path):
    """control: sort by number only -> the two-goal root (9) no longer comes before 1 and 2."""
    nodes = [_node(1, "parked"), _node(2, "parked"), _node(9, "parked")] + [_node(n) for n in range(20, 25)]
    edges = [(1, 20), (2, 21), (9, 22), (9, 23), (9, 24)]
    got = _lines(tmp_path, nodes, edges)
    assert [g.split("'s")[0].split("#")[-1] for g in got] == ["9", "1", "2"]


# ---------------------------------------------------------------- the pick

def _pick(tmp_path, monkeypatch, root_life, mode="on", corrupt=False, extra_cfg=None):
    lp = pg._loop()
    monkeypatch.setattr(lp, "_dag_first_pick", lambda *a, **k: None)
    monkeypatch.setattr(lp, "_dag_check_pick", lambda *a, **k: None)
    base = pg._base(str(tmp_path), extra=extra_cfg)
    nodes = [_node(1594, root_life), _node(1595), _node(1596)]
    _store(base, nodes, [(1594, 1595), (1594, 1596)], mode=None)
    if mode != "on":
        cfg = json.loads((pathlib.Path(base) / "config.json").read_text())
        cfg.setdefault("discovery", {})["dag"] = {"mode": mode}
        (pathlib.Path(base) / "config.json").write_text(json.dumps(cfg))
    if corrupt:
        (pathlib.Path(base) / "state" / "dag.json").write_text("{not json")
    src = pg._Board(["1595", "1596"])
    kind = lp._next(base, src, lp.state.load_config(base))
    return kind


def test_all_held_goals_stuck_prints_root_cause_lines(tmp_path, monkeypatch, capsys):
    assert _pick(tmp_path, monkeypatch, "parked") == ("DONE", None)
    err = capsys.readouterr().err
    assert "#1595, #1596 wait on #1594's decision; /sigma-unpark #1594" in err
    assert "pickable by themselves" not in err


def test_a_plain_open_blocker_keeps_the_old_text(tmp_path, monkeypatch, capsys):
    """control: always print the new text -> this goes red."""
    _pick(tmp_path, monkeypatch, "goal")
    err = capsys.readouterr().err
    assert "They become pickable by themselves when it closes" in err and "wait on" not in err


def test_one_unstuck_held_goal_keeps_the_old_text(tmp_path, monkeypatch, capsys):
    """The 'every held goal' clause alone: 1595 is stuck behind the parked 1594, 1596 only behind a
    workable 1597. control: print the new text when ANY held goal is stuck -> red."""
    lp = pg._loop()
    monkeypatch.setattr(lp, "_dag_first_pick", lambda *a, **k: None)
    monkeypatch.setattr(lp, "_dag_check_pick", lambda *a, **k: None)
    live = pg.TRIAL + [pg._blocked_by(1598, 1597), pg._issue(1597)]
    base = pg._base(str(tmp_path), mirror=live)
    _store(base, [_node(1594, "parked"), _node(1595), _node(1597), _node(1598)], [(1594, 1595), (1597, 1598)])
    src = pg._Board(["1595", "1598"], live=live)
    lp._next(base, src, lp.state.load_config(base))
    err = capsys.readouterr().err
    assert "pickable by themselves" in err and "wait on" not in err


def test_map_unusable_or_mode_off_is_byte_identical_to_no_map(tmp_path, monkeypatch, capsys):
    """The existing pick-gate fixtures, three ways: no map at all (today), a corrupt map, mode off with a
    perfectly good map. All three must print the same bytes. control: read the map whatever the mode
    -> the mode-off run prints the new lines and this goes red."""
    outs = []
    for i, kw in enumerate(({"mode": "on", "nomap": True}, {"corrupt": True}, {"mode": "off"})):
        d = tmp_path / str(i)
        d.mkdir()
        if kw.get("nomap"):
            lp = pg._loop()
            base = pg._base(str(d))
            lp._next(base, pg._Board(["1595", "1596"]), lp.state.load_config(base))
        else:
            _pick(d, monkeypatch, "parked", **kw)
        outs.append(capsys.readouterr().err.replace(str(d), "<d>"))
    assert outs[0] == outs[1] == outs[2]
    assert "They become pickable by themselves when it closes" in outs[0]


# ---------------------------------------------------------------- status and doctor

def _abort(*a, **k):
    raise AssertionError("a gh call was made")


def test_status_shows_stuck_list_with_gh_aborting(tmp_path, monkeypatch, capsys):
    sdlc = _store(tmp_path / ".sdlc", [_node(1, "parked")] + [_node(n) for n in range(2, 10)],
                  [(1, n) for n in range(2, 10)])
    monkeypatch.setattr(subprocess, "run", _abort)
    monkeypatch.setattr(subprocess, "Popen", _abort)
    seg = status._stuck_lines(str(sdlc))
    assert seg[0] == "stuck chains: 1"
    assert seg[1] == "  #2, #3, #4, #5, #6 and 3 more wait on #1's decision; /sigma-unpark #1"


def test_status_lists_top_five_and_the_count(tmp_path):
    nodes = [_node(n, "parked") for n in range(1, 8)] + [_node(n) for n in range(11, 18)]
    sdlc = _store(tmp_path / ".sdlc", nodes, [(n, n + 10) for n in range(1, 8)])
    seg = status._stuck_lines(str(sdlc))
    assert seg[0] == "stuck chains: 7" and len(seg) == 6          # the count, then the top five
    assert seg[1].startswith("  #11 wait on #1's") and seg[5].startswith("  #15 wait on #5's")


def test_status_main_adds_nothing_when_mode_off_or_no_map(tmp_path, capsys, monkeypatch):
    base = tmp_path / ".sdlc"
    (base / "state").mkdir(parents=True)
    (base / "goals").mkdir()
    status.main(["status.py", str(base)])
    plain = capsys.readouterr().out
    _store(base, [_node(1, "parked"), _node(2)], [(1, 2)], mode="off")
    status.main(["status.py", str(base)])
    assert capsys.readouterr().out == plain
    (base / "config.json").write_text("{}")
    status.main(["status.py", str(base)])
    assert "stuck chains: 1" in capsys.readouterr().out


def test_doctor_row_counts_stuck_roots_and_mode_off_adds_nothing(tmp_path):
    sdlc = _store(tmp_path / ".sdlc", [_node(1, "parked"), _node(2), _node(3, "needs-label"), _node(4)],
                  [(1, 2), (3, 4)])
    cfg = {"discovery": {"dag": {"mode": "on"}}}
    row = doctor._dag_map_row(cfg, str(sdlc))
    assert row["ok"] and row["name"].endswith(", 2 stuck roots")
    calm = _store(tmp_path / "c" / ".sdlc", [_node(1), _node(2)], [(1, 2)])
    assert "stuck" not in doctor._dag_map_row(cfg, str(calm))["name"]
    assert doctor._dag_map_row({"discovery": {"dag": {"mode": "off"}}}, str(sdlc)) is None
