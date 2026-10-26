#!/usr/bin/env python3
"""Read-only scan of the agent host configuration for risky settings (#2032).

Usage: host_config_scan.py [PROJECT_ROOT]   (prints findings; exit 0)

Report only. It never blocks, never writes, makes no network call and spawns nothing. A config value
is never printed or stored: a finding carries the file (home shown as `~`), a rule id, a severity and
a fixed fix sentence, so a matched literal cannot leak through it. The rule set is provisional.

Rules (each is its own function, each independently removable):
  R1 an allow rule that grants a whole tool (`Bash(*)`, `Bash`)
  R2 a hook or MCP command that downloads and executes, or runs from a world-writable path
  R3 an MCP server started from an unpinned package
  R4 a literal credential in a config `env` block (Sigma's own commit-gate secret rules, imported)
  R5 sandbox and approval switched off (Codex both off, Claude Code `bypassPermissions`)

A file that exists but cannot be read or parsed is a `not_scanned` row, never silence. A host with no
config file has no row at all: there was nothing to scan, which is not the same as clean.
"""
import importlib.util
import json
import os
import pathlib
import re
import sys

try:
    import tomllib
except ImportError:                       # Python < 3.11
    tomllib = None

MAX_BYTES = 1_000_000
MAX_PLUGIN_HOOK_FILES = 50

RULES = {
    "R1": ("high", "allow-all",
           "narrow the allow rule to the commands the project needs, e.g. `Bash(git status:*)`"),
    "R2": ("high", "download-and-execute",
           "pin the command to a reviewed local script; do not pipe a download into a shell or run from /tmp"),
    "R3": ("medium", "unpinned-mcp-package",
           "pin the MCP package to an exact version, e.g. `package@1.2.3`, never `@latest` or no version"),
    "R4": ("high", "literal-credential",
           "move the credential out of the config file and reference it from the environment"),
    "R5": ("high", "protections-off",
           "turn the sandbox and approval prompts back on for this host"),
}

_ALLOW_ALL = re.compile(r"^[A-Za-z_][\w.\-]*(\(\s*(\*+|:\*)?\s*\))?$")
_DOWNLOAD_EXEC = re.compile(r"\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(ba|z|da|k)?sh\b")
_WORLD_WRITABLE = re.compile(r"(?:^|[\s\"'=])(?:/private)?(?:/var)?/tmp/")
_PINNED_NPM = re.compile(r"^(@[^/@]+/)?[^@/]+@\d+\.\d+\.\d+([-+][\w.\-]*)?$")
_PINNED_PY = re.compile(r"^[^=@]+==\d[\w.\-]*$")
_RUNNERS = {"npx", "bunx", "uvx", "pipx"}


def _scrub():
    here = pathlib.Path(__file__).resolve().parents[2] / "sigma-loop" / "scripts" / "scrub.py"
    spec = importlib.util.spec_from_file_location("_host_scan_scrub", here)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _show(path, home, root):
    """The path as shown: `~` for the home, project-relative inside the project."""
    for base, mark in ((root, ""), (home, "~/")):
        try:
            return mark + str(pathlib.Path(path).relative_to(base))
        except ValueError:
            continue
    return str(path)


def _read(path):
    """(text, None) or (None, reason). Opened read-only, size capped."""
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return None, "file larger than %d bytes" % MAX_BYTES
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read(), None
    except (OSError, UnicodeDecodeError) as exc:
        return None, "unreadable (%s)" % type(exc).__name__


def _commands(blocks):
    """(command string, args list, env dict) for each MCP server mapping in each block."""
    for server in (s for b in blocks if isinstance(b, dict) for s in b.values()):
        if isinstance(server, dict):
            args = server.get("args") if isinstance(server.get("args"), list) else []
            env = server.get("env") if isinstance(server.get("env"), dict) else {}
            yield str(server.get("command") or ""), [str(a) for a in args], env


def _hook_commands(hooks):
    for entries in (hooks or {}).values() if isinstance(hooks, dict) else ():
        for entry in entries if isinstance(entries, list) else ():
            inner = entry.get("hooks") if isinstance(entry, dict) else None
            for hook in inner if isinstance(inner, list) else ():
                if isinstance(hook, dict) and isinstance(hook.get("command"), str):
                    yield hook["command"]


def _servers(doc):
    """Both server blocks, each checked on its own (a wrong-typed one never hides the other)."""
    return [doc[k] for k in ("mcpServers", "mcp_servers") if doc.get(k) is not None]


def _shape_ok(doc):
    """False when a block the rules read has the wrong type. Reads no value, only types."""
    for servers in _servers(doc):
        if not isinstance(servers, dict):
            return False
        for server in servers.values():
            if not isinstance(server, dict):
                return False
            if server.get("args") is not None and not isinstance(server.get("args"), list):
                return False
            if server.get("env") is not None and not isinstance(server.get("env"), dict):
                return False
    hooks = doc.get("hooks")
    if hooks is not None:
        if not isinstance(hooks, dict):
            return False
        for entries in hooks.values():
            if not isinstance(entries, list):
                return False
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("hooks", []), list):
                    return False
    perms = doc.get("permissions")
    if perms is not None and (not isinstance(perms, dict) or not isinstance(perms.get("allow", []), list)):
        return False
    return doc.get("env") is None or isinstance(doc.get("env"), dict)


def _r1(doc, _toml, _scrub_module):
    perms = doc.get("permissions")
    allow = perms.get("allow") if isinstance(perms, dict) else None
    allow = allow if isinstance(allow, list) else []
    return any(isinstance(a, str) and _ALLOW_ALL.match(a.strip()) for a in allow)


def _runs_bad(command):
    return bool(_DOWNLOAD_EXEC.search(command) or _WORLD_WRITABLE.search(command))


def _r2(doc, _toml, _scrub_module):
    for command in _hook_commands(doc.get("hooks")):
        if _runs_bad(command):
            return True
    for command, args, _env in _commands(_servers(doc)):
        if _runs_bad(" ".join([command] + args)):
            return True
    return False


def _unpinned(command, args):
    runner = os.path.basename(command)
    rest = list(args)
    if runner in ("pnpm", "yarn", "npm") and rest[:1] in (["dlx"], ["exec"]):
        rest = rest[1:]
    elif runner not in _RUNNERS:
        return False
    spec = next((a for a in rest if not a.startswith("-")), None)
    if spec is None:
        return False
    return not (_PINNED_NPM.match(spec) or _PINNED_PY.match(spec))


def _r3(doc, _toml, _scrub_module):
    return any(_unpinned(c, a) for c, a, _e in _commands(_servers(doc)))


def _env_blocks(doc):
    if isinstance(doc.get("env"), dict):
        yield doc["env"]
    for _c, _a, env in _commands(_servers(doc)):
        yield env


def _r4(doc, _toml, scrub_module):
    for env in _env_blocks(doc):
        for key, value in env.items():
            if not isinstance(value, str) or not value or value.lstrip().startswith("$"):
                continue
            if scrub_module.commit_secret_findings(json.dumps({str(key): value})) or \
                    scrub_module.commit_secret_findings(value):
                return True
    return False


def _r5(doc, toml, _scrub_module):
    if toml:
        return doc.get("approval_policy") == "never" and doc.get("sandbox_mode") == "danger-full-access"
    perms = doc.get("permissions")
    return isinstance(perms, dict) and perms.get("defaultMode") == "bypassPermissions"


_CHECKS = (("R1", _r1), ("R2", _r2), ("R3", _r3), ("R4", _r4), ("R5", _r5))


def _targets(root, home, claude_dir, codex_dir):
    """`(targets, dropped)`: (path, kind) for each config that may exist, and how many plugin hook
    files lay beyond the cap. `kind` is json, toml or presence."""
    out = [(root / ".claude" / "settings.json", "json"), (root / ".claude" / "settings.local.json", "json"),
           (claude_dir / "settings.json", "json"), (root / ".mcp.json", "json"),
           (codex_dir / "config.toml", "toml"), (root / ".cursor" / "mcp.json", "json"),
           (root / "AGENTS.md", "presence"), (root / ".cursor" / "rules", "presence")]
    plugins = sorted((claude_dir / "plugins" / "cache").glob("*/*/*/hooks/hooks.json"))
    out += [(p, "json") for p in plugins[:MAX_PLUGIN_HOOK_FILES]]
    return out, max(0, len(plugins) - MAX_PLUGIN_HOOK_FILES)


def scan(project_root, home=None):
    """`{"findings": [...], "not_scanned": [...], "scanned": [...]}`.

    `home` injects the home folder (tests pass a temp one). Without it the Claude folder is
    `$CLAUDE_CONFIG_DIR` else `~/.claude` and the Codex folder `$CODEX_HOME` else `~/.codex`."""
    root = pathlib.Path(project_root).resolve()
    if home is not None:
        home = pathlib.Path(home)
        claude_dir, codex_dir = home / ".claude", home / ".codex"
    else:
        home = pathlib.Path.home()
        claude_dir = pathlib.Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude")
        codex_dir = pathlib.Path(os.environ.get("CODEX_HOME") or home / ".codex")
    result = {"findings": [], "not_scanned": [], "scanned": []}
    scrub_module = scrub_err = None
    try:
        scrub_module = _scrub()
    except Exception as exc:                      # noqa: BLE001 - report, never raise
        scrub_err = "secret rules unavailable (%s)" % type(exc).__name__
    targets, dropped = _targets(root, home, claude_dir, codex_dir)
    if dropped:
        result["not_scanned"].append({"file": _show(claude_dir / "plugins" / "cache", home, root),
                                      "reason": "%d plugin hook files beyond the %d-file cap"
                                      % (dropped, MAX_PLUGIN_HOOK_FILES)})
    for path, kind in targets:
        if not (path.is_file() or (kind == "presence" and path.exists())):
            continue
        shown = _show(path, home, root)
        if kind == "presence":
            result["scanned"].append(shown)
            continue
        text, reason = _read(path)
        doc = None
        if reason is None and kind == "toml":
            if tomllib is None:
                reason = "toml reader unavailable"
            else:
                try:
                    doc = tomllib.loads(text)
                except tomllib.TOMLDecodeError:
                    reason = "not valid TOML"
        elif reason is None:
            try:
                doc = json.loads(text)
            except ValueError:
                reason = "not valid JSON"
            if reason is None and not isinstance(doc, dict):
                reason = "not a JSON object"
        if reason is not None:
            result["not_scanned"].append({"file": shown, "reason": reason})
            continue
        result["scanned"].append(shown)
        found, bad = [], False
        try:
            bad = not _shape_ok(doc)
            for rule, check in _CHECKS:
                if rule == "R4" and scrub_module is None:
                    result["not_scanned"].append({"file": shown, "reason": scrub_err})
                    continue
                if check(doc, kind == "toml", scrub_module):
                    severity, _name, fix = RULES[rule]
                    found.append({"file": shown, "rule": rule, "severity": severity, "fix": fix})
        except Exception:                          # noqa: BLE001 - never raise, never echo
            bad = True
        result["findings"].extend(found)
        if bad:
            result["not_scanned"].append({"file": shown, "reason": "unexpected structure"})
    return result


def lines(result):
    """Printable lines, value-free by construction (they read only file, rule, severity, fix)."""
    out = ["host config %s %s: %s -> %s" % (f["rule"], f["severity"], f["file"], f["fix"])
           for f in result["findings"]]
    out += ["host config not scanned: %s: %s" % (n["file"], n["reason"]) for n in result["not_scanned"]]
    return out


def main(argv):
    if argv[1:2] in (["-h"], ["--help"]):
        print(__doc__)
        return 0
    for line in lines(scan(argv[1] if len(argv) > 1 else ".")):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
