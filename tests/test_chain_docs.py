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
    for name in ("chain.py", "chain_spend.py", "chain_parallel.py"):
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


def _block():
    text = DOC.read_text(encoding="utf-8")
    after = text.split(f"## {HEADING}", 1)[1]
    return re.search(r"```\n(.*?)```", after, re.S).group(1)


def _carries(copy, block):
    """True when `copy` holds `block` byte for byte (#1593)."""
    return block in copy


def _init_module():
    import importlib.util
    path = ROOT / "skills" / "sigma-init" / "scripts" / "sdlc_init.py"
    spec = importlib.util.spec_from_file_location("sdlc_init_1593", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_block_pinned(tmp_path):
    block = _block()
    assert block.startswith("## Solving a whole dependency chain\n")
    init = _init_module()
    assert _carries((ROOT / "AGENTS.md").read_text(encoding="utf-8"), block), "AGENTS.md"
    assert _carries((ROOT / "skills/sigma-init/templates/project.md.tmpl").read_text(encoding="utf-8"), block)
    assert _carries(init._CURSOR_RULE, block), "sdlc.mdc"
    # the scaffolded copy, not just the template
    init.scaffold(tmp_path)
    assert _carries((tmp_path / ".sdlc" / "project.md").read_text(encoding="utf-8"), block)


def test_comparison_fails_on_one_char_change():
    block = _block()
    copy = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    i = copy.index(block) + 10
    assert not _carries(copy[:i] + ("X" if copy[i] != "X" else "Y") + copy[i + 1:], block)


def test_parallel_and_lock_are_documented():
    """#1865: the width keys, the lock file, the stale rule and its consequence are in the doc."""
    text = DOC.read_text(encoding="utf-8")
    flat = " ".join(text.split())
    for key in ("`chain.parallel`", "`chain.max_active`", "parallel.goals.max_concurrent"):
        assert key in flat, key
    for phrase in (".sdlc/state/chain-active.json", "pid is dead AND its heartbeat is older than 900 seconds",
                   "further apart than 900 seconds can be reclaimed", "Manual recovery"):
        assert phrase in flat, phrase


def _flat():
    return " ".join(DOC.read_text(encoding="utf-8").split())


def test_lock_recovery_names_only_a_crashed_driver():
    """#1867: a stuck-ended chain is not described as holding the lock."""
    flat = _flat()
    assert "only a crashed or killed driver leaves an entry" in flat
    assert "ended stuck keeps it" not in flat


def test_parallel_row_states_the_width_one_fallback():
    """#1867: the budget shortfall falls back to width 1 (never refuses); below 2 is unset."""
    flat = _flat()
    assert "the run falls back to width 1" in flat
    assert "A value below 2 is read as unset" in flat
    assert "wider width is refused" not in flat
