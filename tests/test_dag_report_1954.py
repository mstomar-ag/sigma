"""#1954: `dag.py report`, the read-only summary of the stored dependency map, and the /sigma-dag skill.

The controls, each run red once on its own and restored: the missing-map branch, the stale-map branch,
the mode-off branch, the unwritten-key branch, and the stuck-chain ordering (plain membership instead of
most-freeing-first). Fixture issue numbers are generic. No test reaches the network: subprocess is made
to abort, and the report is checked to write nothing."""
import importlib.util
import json
import pathlib
import subprocess
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
S = ROOT / "skills" / "sigma-loop" / "scripts"
DAG = S / "dag.py"
NOW = 1_800_000_000.0


def _mod(name, base=S):
    spec = importlib.util.spec_from_file_location(name, base / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


rep = _mod("dag_report")
ds = rep.dag_store


def _iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _node(number, life="goal", state="open"):
    return {"number": number, "kind": "issue", "state": state, "life": life, "prio": None, "in_corpus": True}


def _store(tmp_path, nodes, edges, mode="shadow", age=60, partial=False, history=None):
    sdlc = tmp_path / ".sdlc"
    (sdlc / "state").mkdir(parents=True)
    h = {"repo": "owner/repo", "built_at": _iso(NOW - age), "refreshed_at": _iso(NOW - age),
         "last_ok_at": _iso(NOW - age), "last_error": None, "partial": partial}
    ok, why = ds.save(sdlc, {"header": h, "nodes": nodes,
                             "edges": [{"blocker": a, "blocked": b, "source": "body"} for a, b in edges]})
    assert ok, why
    if mode:
        (sdlc / "config.json").write_text(json.dumps({"discovery": {"dag": {"mode": mode}}}))
    if history:
        d = sdlc / "state" / "dag-history"
        d.mkdir()
        (d / (time.strftime("%Y-%m", time.gmtime(NOW)) + ".jsonl")).write_text(
            "".join(json.dumps(r) + "\n" for r in history))
    return sdlc


# fixture map: 1 parked holds 2, 3 (3 holds 2 too); 10 unarmed holds 11; 20 and 21 wait on each other;
# 30 -> 31 is a plain chain; fixture issue 40 is done.
NODES = [_node(1, "parked"), _node(2), _node(3), _node(10, "needs-label"), _node(11),
         _node(20), _node(21), _node(30), _node(31), _node(40, state="closed")]
EDGES = [(1, 2), (1, 3), (10, 11), (20, 21), (21, 20), (30, 31), (40, 31)]
HISTORY = [{"t": _iso(NOW - 5), "ev": "chain-completed", "chain": 40, "n": 2},
           {"t": _iso(NOW - 5), "ev": "node-resolved", "number": 40}]


@pytest.fixture
def full(tmp_path):
    return _store(tmp_path, NODES, EDGES, history=HISTORY)


def test_full_report_matches_golden(full):
    got = rep.report(full, now=NOW)
    assert got[1] == 0
    text = got[0]
    for needle in ["# Dependency map report", "- mode: shadow", "- freshness: dag map: mode shadow", "- nodes: 10, edges: 7", "- usable: yes",
                   "## Stuck chains", "- #2, #3 wait on #1's decision; /sigma-unpark #1",
                   "- #11 wait on #10's decision; /sigma-promote #10", "- #20, #21 wait on each other",
                   "## Active chains", "## Cycles", "- #20, #21", "## Completed this month",
                   "1 chain completed (2 members)", "## Where the files live", "`.sdlc/state/dag.json`"]:
        assert needle in text, needle
    assert "ledger" not in text.lower()          # ledger is off in the fixture


def test_stuck_chains_most_freeing_first(full):
    text = rep.report(full, now=NOW)[0]
    stuck = text.split("## Stuck chains")[1].split("##")[0]
    assert stuck.index("#1's decision") < stuck.index("#10's decision")      # 2 waiting before 1


def test_stuck_ordering_is_not_membership(tmp_path):
    # the smaller root has the LOWER number; plain number order would put it first
    nodes = [_node(1, "parked"), _node(2), _node(5, "parked"), _node(6), _node(7), _node(8)]
    sdlc = _store(tmp_path, nodes, [(1, 2), (5, 6), (5, 7), (5, 8)])
    stuck = rep.report(sdlc, now=NOW)[0].split("## Stuck chains")[1].split("##")[0]
    assert stuck.index("#5's decision") < stuck.index("#1's decision")


def test_ledger_files_listed_when_on(tmp_path):
    sdlc = _store(tmp_path, NODES, EDGES)
    (sdlc / "config.json").write_text(json.dumps({"discovery": {"dag": {"mode": "shadow"}},
                                                  "ledger": {"enabled": True}}))
    assert "DAG.md" in rep.report(sdlc, now=NOW)[0]


def test_shadow_line_comes_from_the_doctor(tmp_path):
    sdlc = _store(tmp_path, NODES, EDGES)
    row = {"name": "dag shadow: 4 compared picks, 1 disagreements", "ok": True, "fix": ""}
    got = rep.report(sdlc, now=NOW, rows={"shadow": row})[0]
    assert "- shadow: dag shadow: 4 compared picks, 1 disagreements" in got


def test_goal_report_is_the_chain_markdown(full):
    dag = _mod("dag")
    text, code = rep.report(full, goal=2, now=NOW, chain_text=dag._chain_text)
    assert code == 0
    assert text.startswith("# Dependency chain for #2")
    assert "chain for #2 (shadow, map" in text
    assert "wave 0: #1" in text


def test_goal_unknown_gives_one_line(full):
    text, code = rep.report(full, goal=999, now=NOW, chain_text=_mod("dag")._chain_text)
    assert code == 2 and "\n" not in text.strip() and "999" in text


# ------------------------------------------------------- one-line fixes, each its own branch

def test_missing_map(tmp_path):
    sdlc = tmp_path / ".sdlc"
    (sdlc / "state").mkdir(parents=True)
    (sdlc / "config.json").write_text(json.dumps({"discovery": {"dag": {"mode": "shadow"}}}))
    text, code = rep.report(sdlc, now=NOW)
    assert text.count("\n") == 0 and "dag.py rebuild" in text and code == 0


def test_stale_map(tmp_path):
    sdlc = _store(tmp_path, NODES, EDGES, age=86400 * 5)
    text, _ = rep.report(sdlc, now=NOW)
    assert text.count("\n") == 0 and "dag.py refresh --force" in text and "stale" in text


def test_mode_off(tmp_path):
    sdlc = _store(tmp_path, NODES, EDGES, mode="off")
    text, _ = rep.report(sdlc, now=NOW)
    assert text.count("\n") == 0 and "discovery.dag.mode" in text and "off" in text


def test_unwritten_key(tmp_path):
    sdlc = _store(tmp_path, NODES, EDGES, mode=None, age=86400 * 5)
    text, _ = rep.report(sdlc, now=NOW)
    assert text.count("\n") == 0 and "discovery.dag.mode" in text
    assert "only once" in text and "automatic refresh" in text and "doctor" in text


def test_unwritten_key_and_no_map(tmp_path):
    (tmp_path / ".sdlc" / "state").mkdir(parents=True)
    text, _ = rep.report(tmp_path / ".sdlc", now=NOW)
    assert text.count("\n") == 0 and "not written" in text and "only once" in text and "rebuild" in text


def test_unwritten_key_with_a_usable_map_still_reports_with_a_note(tmp_path):
    sdlc = _store(tmp_path, NODES, EDGES, mode=None)
    text, _ = rep.report(sdlc, now=NOW)
    assert "## Stuck chains" in text and "not written" in text


def test_never_empty_without_a_state_dir(tmp_path):
    text, _ = rep.report(tmp_path / "nope", now=NOW)
    assert text.strip()


# ------------------------------------------------------- json, read-only, no network

def test_json_has_the_same_facts(full):
    text, code = rep.report(full, now=NOW, as_json=True)
    d = json.loads(text)
    assert code == 0 and d["schema"] == "dag-report/1"
    assert d["state"]["nodes"] == 10 and d["state"]["usable"] is True
    assert [s["root"] for s in d["stuck"]][:2] == [1, 10]
    assert d["cycles"] == [[20, 21]]
    assert d["completed"]["chains"] == 1


def test_json_for_a_fix_line_is_valid_json(tmp_path):
    sdlc = _store(tmp_path, NODES, EDGES, mode="off")
    d = json.loads(rep.report(sdlc, now=NOW, as_json=True)[0])
    assert d["fix"] and d["state"] is None


def _tree(root):
    return {p.relative_to(root).as_posix(): (p.stat().st_mtime_ns, p.stat().st_size)
            for p in pathlib.Path(root).rglob("*") if p.is_file()}


def test_writes_nothing_and_no_subprocess(full, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the report must not run a process")
    for name in ("run", "Popen", "check_output", "call", "check_call"):
        monkeypatch.setattr(subprocess, name, boom)
    before = _tree(full.parent)
    rep.report(full, now=NOW)
    rep.report(full, now=NOW, as_json=True)
    assert _tree(full.parent) == before


def test_cli_default_is_markdown_and_json_flag(full):
    run = lambda *a: subprocess.run([sys.executable, str(DAG), "report", "--sdlc", str(full), *a],  # noqa: E731
                                    capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    md = run()
    assert md.returncode == 0 and md.stdout.startswith("# ") or "dag:" in md.stdout
    js = run("--json")
    json.loads(js.stdout)
    assert run("--md").stdout == md.stdout


# ------------------------------------------------------- the skill

def test_skill_files_and_card():
    skill = (ROOT / "skills" / "sigma-dag" / "SKILL.md").read_text()
    assert "name: sigma-dag" in skill and "allowed-tools:" in skill
    assert "dag.py" in skill and "report" in skill and "--html" in skill
    assert (ROOT / "skills" / "sigma-dag" / "references" / "selection.md").is_file()
    card = json.loads((ROOT / "evals" / "skills" / "cards" / "sigma-dag.json").read_text())
    assert card["skill"] == "sigma-dag"
    assert len(skill.splitlines()) < 40


def test_docs_open_reading_the_map_with_the_skill():
    doc = (ROOT / "docs" / "dag.md").read_text()
    sec = doc.split("## Reading the map")[1].lstrip()
    assert sec.startswith("`/sigma-dag`") or "/sigma-dag" in sec.split("\n\n")[0]
    assert "/sigma-dag" in (ROOT / "docs" / "features.md").read_text()
