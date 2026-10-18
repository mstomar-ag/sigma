"""The chain agent-file block reaches new scaffold files only, once, and never edits an existing agent file (#1593)."""
import importlib.util
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "chain.md"


def _init():
    spec = importlib.util.spec_from_file_location("sdlc_init_1593b", ROOT / "skills/sigma-init/scripts/sdlc_init.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _block():
    after = DOC.read_text(encoding="utf-8").split("## Solving a whole dependency chain", 1)[1]
    return re.search(r"```\n(.*?)```", after, re.S).group(1)


def test_idempotent(tmp_path):
    init, block = _init(), _block()
    for _ in range(2):
        init.scaffold(tmp_path)
        init.scaffold_cursor_rules(tmp_path)
    assert (tmp_path / ".sdlc" / "project.md").read_text(encoding="utf-8").count(block) == 1
    assert (tmp_path / ".cursor" / "rules" / "sdlc.mdc").read_text(encoding="utf-8").count(block) == 1


def test_existing_file_untouched(tmp_path):
    init = _init()
    agents, claude = b"# mine\n", b"# also mine\n"
    (tmp_path / "AGENTS.md").write_bytes(agents)
    (tmp_path / "CLAUDE.md").write_bytes(claude)
    init.scaffold(tmp_path)
    init.scaffold_cursor_rules(tmp_path)
    assert (tmp_path / "AGENTS.md").read_bytes() == agents
    assert (tmp_path / "CLAUDE.md").read_bytes() == claude


def test_existing_project_md_untouched(tmp_path):
    init = _init()
    (tmp_path / ".sdlc").mkdir()
    (tmp_path / ".sdlc" / "project.md").write_bytes(b"# mine\n")
    init.scaffold(tmp_path)
    assert (tmp_path / ".sdlc" / "project.md").read_bytes() == b"# mine\n"
