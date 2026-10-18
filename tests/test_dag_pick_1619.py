"""#1619: the pick-time hold from the stored dependency map, and the shadow comparison that earns it.

Controls, each run red once on its own and restored (the test each one turns red):
* shadow returns the map's decision instead of the old one -> `test_shadow_returns_the_old_decision_*`
* mode `on` always calls the old path -> `test_on_with_a_usable_map_makes_no_mirror_and_no_live_read`
* the change-check clause dropped from `dag_pick._entry` -> `test_each_unusable_clause_*[no recent check]`
* the pull-request rule (use "is not open" for a pull request) -> `test_a_pull_request_blocker_*`
* the rotation threshold raised -> `test_the_log_rotates_*`
No test reaches the network: the board is the in-memory fake of `test_pick_dependency_gate`, and the
map is built with `dag_store.save`. Issue numbers here are fixture issues, not real ones.
"""
import importlib.util
import json
import pathlib
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


dp = _mod("dag_pick")
ds = dp.dag_store
doctor = _mod("doctor", ROOT / "skills" / "sigma-doctor" / "scripts")
NOW = time.time()
BLOCKER, A, B = 8, 9, 10                      # fixture issues: A and B wait on BLOCKER
SEED = [pg._blocked_by(A, BLOCKER), pg._blocked_by(B, BLOCKER), pg._issue(BLOCKER)]


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _node(number, state="open", kind="issue", life="goal"):
    return {"number": number, "kind": kind, "state": state, "life": life, "prio": None, "in_corpus": True}


def _store(base, nodes, edges, checked=NOW - 60, built=NOW - 60, partial=False):
    """The stored map, saved through the real writer. `edges` are (blocker, blocked[, source])."""
    h = {"repo": "owner/repo", "built_at": _iso(built), "refreshed_at": _iso(built),
         "last_ok_at": _iso(built), "last_error": None, "partial": partial}
    if checked is not None:
        h["checked_at"] = _iso(checked)
    ok, why = ds.save(base, {"header": h, "nodes": nodes, "edges": [
        {"blocker": e[0], "blocked": e[1], "source": e[2] if len(e) > 2 else "body"} for e in edges]})
    assert ok, why


def _trial_map(base, **kw):
    _store(base, [_node(BLOCKER), _node(A), _node(B)], [(BLOCKER, A), (BLOCKER, B)], **kw)


def _setup(tmp_path, mode, mirror=SEED, gh_mode=True):
    base = pg._base(str(tmp_path), mirror=mirror, gh_mode=gh_mode,
                    extra={"discovery": {"dag": {"mode": mode}}} if mode else None)
    lp = pg._loop()
    return base, lp, pg._cfg(lp, base)


def _log(base):
    p = pathlib.Path(base) / "state" / dp.LOG
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


# ------------------------------------------------------------------ 1. shadow returns the old decision

@pytest.mark.parametrize("case,mirror,edges,goal,want_hold,want_class", [
    ("agree-hold", SEED, [(BLOCKER, A), (BLOCKER, B)], A, "#%d (blocked by)" % BLOCKER, "agree-hold"),
    ("agree-free", SEED, [(BLOCKER, A), (BLOCKER, B)], BLOCKER, "", "agree-free"),
    ("map-only", [pg._issue(BLOCKER), pg._issue(A)], [(BLOCKER, A)], A, "", "map-only"),
    ("old-only", SEED, [], A, "#%d (blocked by)" % BLOCKER, "old-only"),
])
def test_shadow_returns_the_old_decision_in_all_four_cases(tmp_path, case, mirror, edges, goal,
                                                           want_hold, want_class):
    base, lp, cfg = _setup(tmp_path, "shadow", mirror=mirror)
    _store(base, [_node(BLOCKER), _node(A), _node(B)], edges)
    src = pg._Board([str(goal)], live=mirror)
    got = lp._map_dependency_hold(base, src, str(goal), cfg, {})
    assert got == want_hold, case
    recs = _log(base)
    assert [r["c"] for r in recs] == [want_class] and recs[0]["g"] == goal


def test_shadow_with_no_stored_map_logs_nothing_and_loads_no_map_code(tmp_path, monkeypatch):
    base, lp, cfg = _setup(tmp_path, None)         # the default mode, no map file
    loaded = []
    real = lp._load
    monkeypatch.setattr(lp, "_load", lambda n: (loaded.append(n), real(n))[1])
    src = pg._Board([str(A)])
    assert lp._map_dependency_hold(base, src, str(A), cfg, {}) == "#%d (blocked by)" % BLOCKER
    assert "dag_pick" not in loaded and _log(base) == []


# ------------------------------------------------------------------ 2. causes, numbers only, rotation

def _cause_map():
    return {"nodes": [_node(50, kind="pr"), _node(51), _node(52), _node(A)],
            "edges": [{"blocker": 51, "blocked": A, "source": "comment"}]}


@pytest.mark.parametrize("ref,cause", [(50, "pr"), (51, "comment-only"), (99, "cross-repo"), (52, "unknown")])
def test_each_cause_is_classified(ref, cause):
    kind, causes = dp.classify([ref], [], _cause_map(), A)
    assert (kind, causes) == ("old-only", [cause])


def test_a_record_is_numbers_and_codes_only(tmp_path):
    base, lp, cfg = _setup(tmp_path, "shadow")
    _trial_map(base)
    lp._map_dependency_hold(base, pg._Board([str(A)]), str(A), cfg, {})
    text = (pathlib.Path(base) / "state" / dp.LOG).read_text()
    rec = json.loads(text)
    assert sorted(rec) == ["c", "g", "k", "m", "o", "t"]
    assert all(isinstance(x, int) for x in rec["o"] + rec["m"] + [rec["g"], rec["t"]])
    assert "goal " not in text and "blocked by" not in text      # no title, no phrase
    meta = json.loads((pathlib.Path(base) / "state" / dp.META).read_text())
    assert list(meta) == ["since"]


def test_the_since_stamp_is_written_once(tmp_path):
    base, lp, cfg = _setup(tmp_path, "shadow")
    _trial_map(base)
    lp._map_dependency_hold(base, pg._Board([str(A)]), str(A), cfg, {})
    first = (pathlib.Path(base) / "state" / dp.META).read_text()
    dp.log_compare(base, cfg, B, "", {}, now=NOW + 5000)
    assert (pathlib.Path(base) / "state" / dp.META).read_text() == first


def test_the_log_rotates_at_one_megabyte(tmp_path):
    base, lp, cfg = _setup(tmp_path, "shadow")
    _trial_map(base)
    d = pathlib.Path(base) / "state"
    (d / dp.LOG).write_text("x" * dp.ROTATE_BYTES)
    lp._map_dependency_hold(base, pg._Board([str(A)]), str(A), cfg, {})
    assert (d / (dp.LOG + ".1")).stat().st_size == dp.ROTATE_BYTES
    assert (d / dp.LOG).stat().st_size < 500 and len(_log(base)) == 1
    # a log one byte under the threshold is appended to, not rotated
    (d / dp.LOG).write_text("y" * (dp.ROTATE_BYTES - 1))
    (d / (dp.LOG + ".1")).unlink()
    dp.log_compare(base, cfg, A, "", {})
    assert not (d / (dp.LOG + ".1")).exists()


def test_an_unwritable_state_directory_loses_the_record_silently(tmp_path):
    base, lp, cfg = _setup(tmp_path, "shadow")
    _trial_map(base)
    (pathlib.Path(base) / "state" / dp.LOG).mkdir()               # a directory where the log should be
    assert lp._map_dependency_hold(base, pg._Board([str(A)]), str(A), cfg, {}) == "#%d (blocked by)" % BLOCKER


# ------------------------------------------------------------------ 3. mode on

def _quiet(lp, monkeypatch):
    monkeypatch.setattr(lp, "_dag_check_pick", lambda *a, **k: None)   # the change check has its own tests
    lp._DAG_PICKED[0] = True


def test_on_with_a_usable_map_makes_no_mirror_and_no_live_read(tmp_path, monkeypatch):
    base, lp, cfg = _setup(tmp_path, "on", mirror=None)             # no mirror file on disk at all
    _trial_map(base)
    _quiet(lp, monkeypatch)
    loaded = []
    real = lp._load
    monkeypatch.setattr(lp, "_load", lambda n: (loaded.append(n), real(n))[1])
    src = pg._Board([str(BLOCKER)])
    assert lp._next(base, src, cfg) == ("goal", str(BLOCKER))
    assert src.calls == [] and src.live_reads() == [] and src.mirror_fetches() == []
    assert "mirror" not in loaded
    assert not (pathlib.Path(base) / "state" / "board-mirror.ndjson").exists()


def test_on_holds_a_goal_from_the_map_and_reaches_the_free_one(tmp_path, monkeypatch, capsys):
    base, lp, cfg = _setup(tmp_path, "on", mirror=None)
    _trial_map(base)
    _quiet(lp, monkeypatch)
    src = pg._Board([str(A), str(B), str(BLOCKER)])
    assert lp._next(base, src, cfg) == ("goal", str(BLOCKER))
    assert src.marked == [str(BLOCKER)] and src.calls == []
    assert "not claiming" in capsys.readouterr().err


def _break(base, how):
    d = pathlib.Path(base) / "state"
    nodes, edges = [_node(BLOCKER), _node(A), _node(B)], [(BLOCKER, A), (BLOCKER, B)]
    if how == "stale":
        _store(base, nodes, edges, built=NOW - 4 * 3600, checked=NOW - 3 * 3600)
    elif how == "partial":
        _store(base, nodes, edges, partial=True)
    elif how == "no recent check":
        _store(base, nodes, edges, checked=NOW - 3600)
    elif how == "no check at all":
        _store(base, nodes, edges, checked=None)
    elif how == "goal not in map":
        _store(base, [_node(BLOCKER), _node(B)], [(BLOCKER, B)])
    elif how == "unknown blocker":
        _store(base, [_node(BLOCKER, state="unknown"), _node(A), _node(B)], edges)
    elif how == "schema":
        _trial_map(base)
        doc = json.loads((d / "dag.json").read_text())
        doc["schema"] = "dag/0"
        (d / "dag.json").write_text(json.dumps(doc))
    elif how == "missing":
        pass


@pytest.mark.parametrize("how,reason", [
    ("stale", "stale"), ("partial", "partial"), ("schema", "schema"), ("missing", "missing"),
    ("no recent check", "no change check"), ("no check at all", "no change check"),
    ("goal not in map", "goal not in map"), ("unknown blocker", "unknown"),
])
def test_each_unusable_clause_takes_the_old_path_with_one_stderr_line(tmp_path, capsys, how, reason):
    base, lp, cfg = _setup(tmp_path, "on")
    _break(base, how)
    src = pg._Board([str(A), str(B)], live=SEED)
    cache = {}
    want = "#%d (blocked by)" % BLOCKER
    assert lp._map_dependency_hold(base, src, str(A), cfg, cache) == want      # the mirror's answer
    assert lp._map_dependency_hold(base, src, str(B), cfg, cache) == want
    lines = [x for x in capsys.readouterr().err.splitlines() if "dependency map not used" in x]
    assert len(lines) == 1 and reason in lines[0] and lines[0].endswith("using the board mirror")


# ------------------------------------------------------------------ 4. pull requests

@pytest.mark.parametrize("state,held", [("open", True), ("merged", False), ("closed-unmerged", True)])
def test_a_pull_request_blocker_holds_until_it_merges(tmp_path, state, held):
    """The graph's own rule (`Graph.resolved`): a merged pull request resolves, an open one holds, and
    a closed-unmerged one never landed so it still holds (it shows as a stuck root, not a release)."""
    base, lp, cfg = _setup(tmp_path, "on", mirror=None)
    _store(base, [_node(A), _node(60, state=state, kind="pr", life="none")], [(60, A)])
    got = lp._map_dependency_hold(base, pg._Board([str(A)]), str(A), cfg, {})
    assert got == ("#60 (blocked by)" if held else "")


def test_a_comment_edge_never_holds(tmp_path):
    base, lp, cfg = _setup(tmp_path, "on", mirror=None)
    _store(base, [_node(A), _node(BLOCKER)], [(BLOCKER, A, "comment")])
    assert lp._map_dependency_hold(base, pg._Board([str(A)]), str(A), cfg, {}) == ""


# ------------------------------------------------------------------ 5. off

def test_off_loads_no_map_code_and_equals_the_old_function(tmp_path, monkeypatch):
    base, lp, cfg = _setup(tmp_path, "shadow")
    _trial_map(base)                                    # a map exists, then the operator turns it off
    c = json.loads((pathlib.Path(base) / "config.json").read_text())
    c["discovery"]["dag"]["mode"] = "off"
    (pathlib.Path(base) / "config.json").write_text(json.dumps(c))
    cfg = pg._cfg(lp, base)
    loaded = []
    real = lp._load
    monkeypatch.setattr(lp, "_load", lambda n: (loaded.append(n), real(n))[1])
    src = pg._Board([str(A)], live=SEED)
    got = lp._map_dependency_hold(base, src, str(A), cfg, {})
    assert got == lp._pick_dependency_hold(base, src, str(A), cfg, {}) == "#%d (blocked by)" % BLOCKER
    assert "dag_pick" not in loaded and _log(base) == []


def test_a_local_goals_source_never_reaches_the_map(tmp_path, monkeypatch):
    base, lp, cfg = _setup(tmp_path, "on", gh_mode=False)
    loaded = []
    real = lp._load
    monkeypatch.setattr(lp, "_load", lambda n: (loaded.append(n), real(n))[1])
    assert lp._map_dependency_hold(base, pg._Board([str(A)]), str(A), cfg, {}) == ""
    assert "dag_pick" not in loaded


# ------------------------------------------------------------------ 6. the doctor row

def _stats_dir(tmp_path, picks=200, days=8, unknown=0, disagree=3):
    sdlc = tmp_path / ".sdlc"
    d = sdlc / "state"
    d.mkdir(parents=True)
    recs = []
    for i in range(picks):
        recs.append({"t": int(NOW), "g": A, "c": "agree-free", "k": [], "o": [], "m": []})
    for i in range(disagree):
        cause = "unknown" if i < unknown else "pr"
        recs[i] = {"t": int(NOW), "g": A, "c": "map-only", "k": [cause], "o": [], "m": [60]}
    half = picks // 2                                               # spread over both files
    (d / (dp.LOG + ".1")).write_text("".join(json.dumps(r) + "\n" for r in recs[:half]))
    (d / dp.LOG).write_text("".join(json.dumps(r) + "\n" for r in recs[half:]))
    (d / dp.META).write_text(json.dumps({"since": int(NOW - days * 86400 - 5)}))
    return str(sdlc)


CFG = {"discovery": {"dag": {"mode": "shadow"}}}


def test_doctor_row_counts_causes_days_and_the_flip_criterion(tmp_path):
    row = doctor._dag_shadow_row(CFG, _stats_dir(tmp_path, unknown=0, disagree=3), now=NOW)
    assert row["ok"] and "200 compared picks, 3 disagreements" in row["name"]
    assert "pr 3, comment-only 0, cross-repo 0, unknown 0" in row["name"]
    assert "8 days in shadow" in row["name"] and "flip criterion met" in row["name"]


@pytest.mark.parametrize("kw", [dict(days=6), dict(picks=199), dict(unknown=1)])
def test_each_flip_clause_alone_makes_it_not_met(tmp_path, kw):
    row = doctor._dag_shadow_row(CFG, _stats_dir(tmp_path, **kw), now=NOW)
    assert row["ok"] and "flip criterion not met" in row["name"]


def test_doctor_row_is_absent_when_off_or_without_a_log(tmp_path):
    sdlc = _stats_dir(tmp_path)
    assert doctor._dag_shadow_row({"discovery": {"dag": {"mode": "off"}}}, sdlc, now=NOW) is None
    assert doctor._dag_shadow_row(CFG, str(tmp_path / "nowhere"), now=NOW) is None
