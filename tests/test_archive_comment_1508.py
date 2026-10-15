"""#1508: the archive_after_days note must not tell a reader to run a verb that does not exist."""
import importlib
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TMPL = ROOT / "skills" / "sigma-init" / "templates" / "config.json.tmpl"
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"


def _note():
    text = json.loads(TMPL.read_text(encoding="utf-8"))["_rulebook"]
    m = re.search(r"`archive_after_days`:(.*?); `max_files_warn`", text)
    assert m, "archive_after_days note missing"
    return m.group(1)


def test_note_names_no_archive_verb_or_command():
    note = _note().lower()
    assert "archive verb" not in note and "archive command" not in note


def test_note_says_what_actually_happens():
    note = _note()
    assert "library function" in note and "nothing" in note


def test_decision_store_cli_has_no_archive_verb():
    src = (SCRIPTS / "decision_store.py").read_text(encoding="utf-8")
    assert "def archive(" in src
    assert not re.search(r"""["']archive["']\s*:|argv\[\d\]\s*==\s*["']archive["']""", src)


def test_ceiling_warning_names_no_nonexistent_verb():
    sys.path.insert(0, str(SCRIPTS))
    try:
        ds = importlib.import_module("decision_store")
    finally:
        sys.path.remove(str(SCRIPTS))
    src = pathlib.Path(ds.__file__).read_text(encoding="utf-8")
    assert "run the archive verb" not in src
