"""The sigma-chain skill and docs/chain.md stay true to chain.py (#1592)."""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "sigma-chain" / "SKILL.md"
DOC = ROOT / "docs" / "chain.md"
SCRIPTS = ROOT / "skills" / "sigma-loop" / "scripts"
RESERVED = {"auto_longest"}          # documented, read by nothing yet
HEADING = "Solving a whole dependency chain"
ARGS = ("--longest", "--dry-run", "--cap-usd", "--parallel", "--no-grow", "--defaults",
        "status", "resume", "stop")


def _code_keys():
    keys = set()
    for name in ("chain.py", "chain_spend.py"):
        text = (SCRIPTS / name).read_text(encoding="utf-8")
        keys |= set(re.findall(r'\b(?:block|chain_cfg|cfg\[?"?chain"?\]?)\.get\("(\w+)"', text))
        keys |= set(re.findall(r'_num\(block, "(\w+)"', text))
        keys |= set(re.findall(r'\.get\("chain"\) or \{\}\)\.get\("(\w+)"', text))
    return keys


def _doc_keys():
    return set(re.findall(r"`chain\.(\w+)`", DOC.read_text(encoding="utf-8")))


def test_keys_match():
    code, doc = _code_keys(), _doc_keys() - RESERVED
    assert code, "the scan found no chain.* keys: the pattern is stale"
    assert code == doc, f"unread documented: {sorted(doc - code)}; undocumented: {sorted(code - doc)}"
    assert RESERVED <= _doc_keys()


def test_host_agnostic():
    for path in (SKILL, DOC):
        text = path.read_text(encoding="utf-8")
        for word in ("PreToolUse", "PostToolUse", ".mdc", "hook", "Hook"):
            assert word not in text, f"{path.name} names {word}"
        tools = set(re.findall(r"\b(?:AskUserQuestion|TodoWrite|ExitPlanMode|WebFetch|Task)\b", text))
        assert tools <= {"AskUserQuestion"}, tools


def test_frontmatter_and_arguments():
    text = SKILL.read_text(encoding="utf-8")
    m = re.match(r"---\nname: sigma-chain\ndescription: (.+)\nallowed-tools: Bash\(python3 \*\)\n---\n", text)
    assert m, "frontmatter shape"
    assert len(m.group(1)) <= 170
    for a in ARGS:
        assert a in text, f"argument {a} not named"
    assert "<#N" in text or "<N>" in text
    assert "## Codex path resolution" in text
    assert "chain.py" in text


def test_agent_block_is_verbatim():
    text = DOC.read_text(encoding="utf-8")
    assert f"## {HEADING}" in text
    after = text.split(f"## {HEADING}", 1)[1]
    block = re.search(r"```\n(.*?)```", after, re.S)
    assert block, "agent-file block missing"
    body = block.group(1)
    assert "sigma-chain" in body and "chain.py" in body
    assert "one by one" in body
    assert body.startswith("## Solving a whole dependency chain\n")
