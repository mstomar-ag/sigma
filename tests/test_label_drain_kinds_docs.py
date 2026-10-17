"""`label_drain.kinds` is documented where the other label_drain keys are."""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_configuration_row_names_label_drain_kinds():
    text = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    row = [l for l in text.splitlines() if l.startswith("|") and "label_drain.default_priority" in l]
    assert len(row) == 1 and "label_drain.kinds" in row[0]


def test_label_drain_doc_lists_three_keys_including_kinds():
    text = (ROOT / "docs" / "label-drain.md").read_text(encoding="utf-8")
    para = text.split("## Arming defaults", 1)[1].split("\n## ", 1)[0]
    assert "three keys" in para and "two keys" not in para and "`kinds`" in para
