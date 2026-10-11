"""Record writers hand each stored record to the ledger pointer writer (story 988, pointer wiring)."""
import importlib.util
import os
import pathlib

import test_promote as tp
from test_decision_record import Src, _ids, _on

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _mod(name):
    spec = importlib.util.spec_from_file_location("pw_" + name, S / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def _cfg(pointer=True):
    cfg = _on({}, pointer={"enabled": pointer})
    cfg["ledger"] = {"enabled": True, "actor": "writer-a"}
    return cfg


def _pointers(sdlc):
    lp = _mod("ledger_pointer")
    return [t for e in _mod("ledger").read_all(str(sdlc)) for t in lp.parse(e)]


def _files(root):
    return sorted(os.path.join(d, f) for d, _n, fs in os.walk(root) for f in fs)


def test_real_promote_leaves_a_pointer_the_parser_reads(tmp_path):
    p = _mod("promote")
    run = tp._runner(views={"5": tp._view("sdlc:needs-confirmation")})
    p.promote(str(tmp_path), _cfg(), ["5"], run=run)
    ids = _ids(tmp_path)
    assert [t[0] for t in _pointers(tmp_path)] == ids and len(ids) == 1


def test_ack_leaves_a_pointer(tmp_path):
    _mod("handoff").acknowledge(str(tmp_path), _cfg(), 9, "accepted", why="ok", goal="g")
    ids = _ids(tmp_path)
    assert len(ids) == 1 and [t[0] for t in _pointers(tmp_path)] == ids


def test_sweep_path_through_write_all_leaves_a_pointer(tmp_path):
    d = _mod("decision_record")
    out = d.write_all(str(tmp_path), d.build("sweep", 5, "dependency", "unpark", "r", "autonomous"),
                      _cfg(), Src())
    assert _pointers(tmp_path) == [(out["id"], "dependency", "autonomous")]


def test_closed_pointer_writes_nothing(tmp_path):
    d = _mod("decision_record")
    d.write_all(str(tmp_path), d.build("promote", 5, "unknown", "c", "r", "human"), _cfg(False), Src())
    _mod("handoff").acknowledge(str(tmp_path), _cfg(False), 9, "accepted", why="ok", goal="g")
    assert _pointers(tmp_path) == []
    assert not any("pointer" in os.path.relpath(f, tmp_path) for f in _files(tmp_path))


def test_pointer_failure_does_not_change_the_gesture(tmp_path):
    d = _mod("decision_record")
    d.ledger_pointer.write_pointer = lambda *a, **k: 1 / 0
    src = Src()
    out = d.write_all(str(tmp_path), d.build("promote", 5, "unknown", "c", "r", "human"), _cfg(), src)
    assert out["id"] and out["error"] is None and out["commented"] and len(src.posted) == 1
