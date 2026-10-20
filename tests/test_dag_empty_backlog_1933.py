"""#1933: an empty backlog is a valid state. A stored map with 0 nodes is usable; `dag show` and
`chain.py plan --longest` say "nothing to plan" and exit 0; the doctor map row is OK.

Fixture issue numbers are plain numbers; none refers to a real issue. Controls, each run red once on its
own: make `dag show` read 0 nodes as unusable; make `chain.py plan --longest` read 0 nodes as unusable;
make the doctor row fail on 0 nodes."""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
S = ROOT / "skills" / "sigma-loop" / "scripts"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))


def _mod(name, base=S):
    spec = importlib.util.spec_from_file_location(name, base / (name + ".py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


dc = _mod("dag_collect")
ds = dc.dag_store
dag_cli = _mod("dag")
ch = _mod("chain")
doctor = _mod("doctor", ROOT / "skills" / "sigma-doctor" / "scripts")

CONFIG = {"discovery": {"github": {"repo": "owner/repo", "assignee": "me-user"}, "dag": {"mode": "on"}}}


class _Empty:
    def __call__(self, args):
        return "[]"


def _empty_map(tmp_path):
    sdlc = tmp_path / ".sdlc"
    (sdlc / "state").mkdir(parents=True)
    (sdlc / "config.json").write_text(json.dumps(CONFIG))
    import time
    rep = dc.refresh(sdlc, CONFIG, _Empty(), time.time())
    assert rep["status"] == "saved", rep
    return sdlc


def test_show_says_nothing_to_plan_for_an_empty_map(tmp_path):
    """control: have show treat 0 nodes as not usable -> no 'nothing to plan'."""
    sdlc = _empty_map(tmp_path)
    out = dag_cli.show(sdlc)
    assert "nodes 0" in out and "nothing to plan" in out and "not usable" not in out


def test_chain_longest_says_nothing_to_plan_and_exits_zero(tmp_path, capsys):
    """control: have --longest raise unusable on 0 nodes -> exit 3 and 'map unusable'."""
    sdlc = _empty_map(tmp_path)
    rc = ch.main(["chain.py", "plan", str(sdlc), "--longest"])
    cap = capsys.readouterr()
    assert rc == 0 and "nothing to plan" in cap.out and "unusable" not in cap.err


def test_doctor_map_row_is_ok_for_an_empty_map(tmp_path):
    """control: make the row fail when nodes == 0 -> not ok."""
    sdlc = _empty_map(tmp_path)
    import time
    r = doctor._dag_map_row(CONFIG, str(sdlc), now=time.time())
    assert r is not None and r["ok"] and "0 nodes" in r["name"]
