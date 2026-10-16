"""#1510: doctor names a leftover watch.stop only when no watcher is running; docs pin the step."""
import importlib.util
import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
D = ROOT / "skills" / "sigma-doctor" / "scripts" / "doctor.py"


def _doctor():
    spec = importlib.util.spec_from_file_location("doctor_1510", D)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _base(tmp_path, stop=True, pid=None):
    st = tmp_path / "state"
    st.mkdir()
    if stop:
        (st / "watch.stop").write_text("")
    if pid is not None:
        (st / "watch.pid").write_text(str(pid))
    return tmp_path


def test_stop_file_without_watcher_gives_row(tmp_path):
    rows = _doctor()._stale_watch_stop_rows(_base(tmp_path), {})
    assert len(rows) == 1
    text = repr(rows[0])
    assert "watch.stop" in text and "rm " in text


def test_no_stop_file_no_row(tmp_path):
    assert _doctor()._stale_watch_stop_rows(_base(tmp_path, stop=False), {}) == []


def test_stop_file_with_live_watcher_no_row(tmp_path):
    d = _doctor()
    base = _base(tmp_path, pid=os.getpid())
    class _Sync:
        @staticmethod
        def watcher_liveness(*a, **k):
            return ("live", 1)

    d._load_loop_script = lambda name: _Sync
    assert d._stale_watch_stop_rows(base, {}) == []


def test_docs_name_the_stop_file():
    for page in ("decision-rulebook.md", "upkeep.md"):
        text = (ROOT / "docs" / page).read_text(encoding="utf-8")
        assert "watch.stop" in text
    rb = (ROOT / "docs" / "decision-rulebook.md").read_text(encoding="utf-8")
    assert "leftover" in rb and "before starting the watcher" in rb.lower()
    up = (ROOT / "docs" / "upkeep.md").read_text(encoding="utf-8")
    assert "leftover `.sdlc/state/watch.stop`" in up
