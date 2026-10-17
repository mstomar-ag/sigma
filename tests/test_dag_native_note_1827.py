"""#1827: the native-relations note names the map store's real edge-source list."""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
NOTE = ROOT / "docs" / "dag-native-relations.md"


def _sources():
    p = ROOT / "skills" / "sigma-loop" / "scripts" / "dag_store.py"
    spec = importlib.util.spec_from_file_location("dag_store_1827", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.EDGE_SOURCES


def test_note_lists_every_real_edge_source_and_the_other_coercion():
    """control: drop `timeline` from the note, or restore the old 'next to `body` and `ledger`'
    sentence, and this goes red."""
    text = NOTE.read_text()
    for src in _sources():
        assert "`%s`" % src in text
    assert "stored as `other`" in text
    assert "`ledger`" not in text
