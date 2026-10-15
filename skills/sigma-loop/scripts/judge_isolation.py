"""Shared isolation helpers for headless `claude -p` calls (#1472).

A model call started from a project directory loads that project's instruction files, memory, skills,
settings and prompt-submit hooks. These helpers keep a call away from all of that: the refusal for a
working directory under an instruction file, a probe of which isolation flags the installed CLI lists
(from its `--help` text only, never a model call), and the child environment without a project pointer.
"""
import os
import pathlib
import subprocess

INSTRUCTION_FILES = ("CLAUDE.md", "CLAUDE.local.md", ".mcp.json", ".claude")

#: Isolation flags added only when the installed CLI's help text lists them. Never `--bare`: it skips
#: keychain and subscription sign-in.
ISOLATION_FLAGS = (("--setting-sources", "user"), ("--strict-mcp-config",), ("--no-session-persistence",))

_HELP_CACHE = {}


def refuse_ancestors(cwd):
    """-> a refusal text when any parent of `cwd` holds an instruction file, else None."""
    for parent in pathlib.Path(os.path.realpath(cwd)).parents:
        for name in INSTRUCTION_FILES:
            if (parent / name).exists():
                return "%s exists in a parent of the working directory (%s); the model may read it" % (name, parent)
    return None


def cli_help_text():
    """`claude --help` text, cached for the process; empty when the CLI is absent or the probe fails."""
    if "text" not in _HELP_CACHE:
        try:
            done = subprocess.run(["claude", "--help"], capture_output=True, text=True, timeout=20)
            _HELP_CACHE["text"] = (done.stdout or "") + (done.stderr or "")
        except Exception:                                   # noqa: BLE001 - absent CLI means no flags
            _HELP_CACHE["text"] = ""
    return _HELP_CACHE["text"]


def isolation_argv(help_text=None):
    """-> the argv tokens for every isolation flag the help text lists (none when it lists none)."""
    text = cli_help_text() if help_text is None else help_text
    out = []
    for spec in ISOLATION_FLAGS:
        if spec[0] in text:
            out.extend(spec)
    return out


def child_env(environ=None):
    """A copy of the environment without `CLAUDE_PROJECT_DIR`."""
    env = dict(os.environ if environ is None else environ)
    env.pop("CLAUDE_PROJECT_DIR", None)
    return env
