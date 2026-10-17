"""#1772 - the calibration runs found two Linux container gaps: no `ps` in a minimal image, and the host total.

No model call, no network. Control: make `tree_footprint` return 0 when `ps` is missing and the first test goes red.
"""
import importlib.util
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"


def _load():
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location("resource_verify_1772", SCRIPTS / "resource_verify.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SCRIPTS))


def _proc(root, pid, ppid):
    d = root / str(pid)
    d.mkdir()
    (d / "stat").write_text("%d (a b) S %d 1 1 0 -1 0\n" % (pid, ppid))


def test_proc_table_reads_pid_and_ppid_even_with_spaces_in_the_name(tmp_path):
    _proc(tmp_path, 10, 1)
    _proc(tmp_path, 11, 10)
    (tmp_path / "self").mkdir()
    assert sorted(_load().proc_table(str(tmp_path))) == [(10, 1), (11, 10)]


def test_tree_footprint_falls_back_to_proc_when_ps_is_missing(monkeypatch):
    rv = _load()

    def no_ps(*a, **k):
        raise FileNotFoundError("ps")

    monkeypatch.setattr(subprocess, "run", no_ps)
    monkeypatch.setattr(rv, "proc_table", lambda proc="/proc": [(2, 1), (3, 2), (4, 99)])
    assert rv.tree_footprint(1, lambda pid: (pid * 100, False)) == 500
