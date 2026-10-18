"""Doctor reports whether an agent file carries the chain block; read only (#1593)."""
import importlib.util
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("doctor_1593", ROOT / "skills/sigma-doctor/scripts/doctor.py")
doctor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(doctor)
BLOCK = re.search(r"```\n(.*?)```", (ROOT / "docs/chain.md").read_text(encoding="utf-8")
                  .split("## Solving a whole dependency chain", 1)[1], re.S).group(1)


def _repo(tmp_path, **files):
    base = tmp_path / ".sdlc"
    base.mkdir()
    for name, text in files.items():
        f = tmp_path / name.replace("__", "/")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    return base


def _row(base):
    rows = doctor._chain_block_rows(base)
    assert len(rows) == 1
    return rows[0]


def test_present_in_agents_md_is_ok(tmp_path):
    assert _row(_repo(tmp_path, **{"AGENTS.md": "# x\n\n" + BLOCK}))["ok"] is True


def test_present_only_in_claude_md_is_ok(tmp_path):
    assert _row(_repo(tmp_path, **{"CLAUDE.md": BLOCK}))["ok"] is True


def test_present_in_cursor_rule_is_ok(tmp_path):
    assert _row(_repo(tmp_path, **{".cursor__rules__sdlc.mdc": BLOCK}))["ok"] is True


def test_absent_is_not_ok_and_names_the_doc(tmp_path):
    row = _row(_repo(tmp_path, **{"AGENTS.md": "# nothing here\n"}))
    assert row["ok"] is False and "docs/chain.md" in row["fix"]


def test_no_agent_file_is_absent_row(tmp_path):
    row = _row(_repo(tmp_path))
    assert row["ok"] is False and "docs/chain.md" in row["fix"]


def test_heading_alone_is_not_enough(tmp_path):
    row = _row(_repo(tmp_path, **{"AGENTS.md": "## Solving a whole dependency chain\n"}))
    assert row["ok"] is False


def test_never_edits(tmp_path):
    base = _repo(tmp_path, **{"AGENTS.md": "# x\n"})
    _row(base)
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == "# x\n"


def test_uninitialised_dir_gives_no_row(tmp_path):
    assert doctor._chain_block_rows(tmp_path / ".sdlc") == []
