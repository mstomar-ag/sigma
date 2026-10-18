"""docs/dag.md stays true to the code: linked, every key named, every verb real, every path exists."""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
PAGE = ROOT / "docs" / "dag.md"
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
# Files the map writes at run time: they exist in a user's checkout, not in this repository.
RUNTIME = {".sdlc/state/", ".sdlc/state/dag.json", ".sdlc/state/dag-shadow.jsonl", ".sdlc/state/dag-history/",
           ".sdlc/state/dag-history/YYYY-MM.jsonl", "dag/", "DAG.md", ".sdlc/state/dag-shadow.meta.json",
           "dag/YYYY-MM/<writer>.jsonl"}


def _page():
    assert PAGE.is_file(), "docs/dag.md is missing"
    return PAGE.read_text(encoding="utf-8")


def _verbs(script):
    return set(re.findall(r'add_parser\(\s*"([a-z-]+)"', (SCRIPTS / script).read_text(encoding="utf-8")))


def test_readme_links_the_page():
    assert "docs/dag.md" in (ROOT / "README.md").read_text(encoding="utf-8")


def test_every_dag_key_in_the_code_is_in_the_page():
    keys = set()
    for p in (ROOT / "skills").rglob("*.py"):
        keys |= set(re.findall(r"discovery\.dag\.([a-z_]+)", p.read_text(encoding="utf-8")))
    assert keys, "found no discovery.dag.* key in the code"
    text = _page()
    missing = sorted(k for k in keys if f"discovery.dag.{k}" not in text)
    assert not missing, f"keys missing from docs/dag.md: {missing}"


def test_every_verb_named_is_a_real_subcommand():
    text = _page()
    for script in ("dag.py", "chain.py"):
        named = set(re.findall(re.escape(script) + r" ([a-z-]+)", text))
        bad = sorted(v for v in named if v not in _verbs(script))
        assert not bad, f"{script} verbs that do not exist: {bad}"
    assert re.search(r"dag\.py (show|chain|stuck|chains)", text)


def test_every_backticked_path_exists():
    bad = []
    for tok in re.findall(r"`([^`\s]+)`", _page()):
        if not (re.search(r"\.(py|md|json|jsonl)$", tok) or "/" in tok):
            continue
        if re.fullmatch(r"[a-z-]+/\d+", tok) or tok.startswith("/sigma-"):
            continue                            # a schema name or a slash command, not a path
        if tok in RUNTIME or tok.startswith("-") or "<" in tok or "://" in tok:
            continue
        if not ((ROOT / tok).exists() or (SCRIPTS / tok).exists() or (ROOT / "docs" / tok).exists()):
            bad.append(tok)
    assert not bad, f"paths that do not exist: {bad}"


def test_no_issue_numbers():
    assert not re.search(r"#\d", _page())
