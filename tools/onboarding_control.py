#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""The end-to-end onboarding control (#237): fresh repository -> /sigma-init -> one goal `done`,
in local-goals mode and in github mode, following the README Quickstart TEXT.

WHY. AGENTS.md: "Run the control, or the check is decoration." The Quickstart once shipped in a
state that could not reach `done` (verify.enforce on, verify.command empty) and nothing caught it,
because nothing ran the documented gesture end to end. This script runs it.

WHAT IT READS FROM THE README, AND WHY THAT MATTERS. The commands are PARSED from the README's
`## Quickstart` section and the verify gestures under "What /sigma-init will ask you" -- the init
script path (the Codex/Cursor script-form line), the `/sigma-init` flags (the Claude Code line),
the `verify_detect.py confirm .sdlc <n> <id>` gesture, and the `claude plugin ...` / `codex plugin
...` install lines. A README that drifts from the shipped scripts (a renamed script, a renamed
verb, a dropped line) therefore turns this control red; a README the control does not read could
drift silently. Since #277 every `python3` gesture under "What /sigma-init will ask you" and "If
/sigma-init says you lack access" is also EXECUTED (github mode, confirm variant, after the goal is
done), from the repository root, `<installed-sigma>` standing for the Sigma directory -- so a
gesture a user could not copy from their repository (a path relative to the plugin directory), a
renamed verb, or a placeholder nothing fills is red. The install lines (claude, codex, in-session `/plugin`) must match the plugin id
`.claude-plugin/marketplace.json` declares, and every init flag the Quickstart shows must be one
init_flow.py's parser accepts. Everything else comes from what /sigma-init PRINTS: the `[ask]`
lines (parsed in init_flow.ask_line's shape, `[ask] <id>: <prose> -> --flag VALUE|VALUE ; ...`,
and answered through ASK_POLICY, keyed by the flag NAME the line offers -- a renamed or new flag
is "unanswerable [ask]", red), the verify candidate number and id, and the `Next:` line's
`loop.py next` command. A README or printed command runs only in the pinned shape
`python3 <existing script under skills/ or tools/> <args without shell syntax>`, the script path
read from the repository root as a shell would (see _py_argv).

EVERY OTHER README GESTURE IS USAGE-CHECKED (#277 review). The two init subsections are executed;
every other `python3 <script> ...` line ANYWHERE in the README or a guide it links (README_DOCS;
fenced, inline, in a table or a
`$(...)`, the script path spelled any way: `<installed-sigma>/`, `"${VAR}/"`, `~/dir/`, absolute,
repository-relative or a bare name) cannot be -- most of them need a live board or a running loop.
A path that is not `<installed-sigma>/<shipped script>` is red at once (it exits 2 when copied
from the user's repository); every other one is
checked against the script's OWN usage instead: `<script> --help` runs (a pure print in every
shipped script; exit 0 required), and the gesture's verb, positional count and `--flags` must match
one of the usage alternatives it prints (see `usage_problems`). That is the decision, and its limit:
a gesture that PARSES but then does something other than its prose claims is not caught here --
that stays a prose review. Mode line `readme-usage`; red names the gesture and the usage it missed.

TWO VARIANTS per mode. `confirm`: a Makefile test target, confirmed by the README gesture
(enforce ON). `no-command`: a repository with nothing to confirm, the verify question left open,
so the default init SCAFFOLDS is what `record done` sees -- the only variant that can see the
original bug, because a confirm overwrites whatever default was scaffolded. In github mode the
no-command variant runs the REAL path, work ON (#312): PR, review gate, merge, done-means-merged,
exactly as the confirm variant does -- with enforce off and no command, `work.py merge` demands no
verify evidence (`state.verify_required`), so the approved merge must pass. Before #312 it parked
on "no fresh verify evidence", and an earlier version of this control hid that behind
`--local-only`. github mode passes `--repo` itself: no `[ask]` names it and the control's origin
is a local path.

WHAT IT DOES NOT DO (see docs/onboarding-control.md for the owner runbook of each):
  * no model session. `/sigma-loop` is a model turn; this control drives the SAME scripts the
    sigma-loop skill tells the agent to run (`loop.py start/next/agent-start`, `work.py`,
    `phase_report.py`, `loop.py verify`, `loop.py record`), with the "work" itself scripted. Token
    cost is therefore N/A: `phase_report.py end` runs and prints its honest `cost: unavailable`
    line, which is recorded as-is.
  * no real GitHub. github mode runs against the stateful fake `gh` from
    tests/test_public_bootstrap_control.py (extracted by `ast`, one source for both) and a local
    bare `origin`. Every gh call is recorded; a call the fake does not model fails the run.
  * no write outside its own temp directory, except the OPTIONAL `--install` step, which runs the
    README's `claude plugin` / `codex plugin` lines into an ISOLATED profile (HOME,
    CLAUDE_CONFIG_DIR, CODEX_HOME all pointed inside the temp dir) and records a hash of the real
    profile's plugin surface before and after, so "untouched" is measured, not assumed.

USAGE
    python3 tools/onboarding_control.py [--mode local|github|both] [--variant confirm|no-command|process|all]
                                        [--sigma DIR] [--readme FILE]
                                        [--install none|claude|codex|all] [--from-install]
                                        [--json FILE] [--workdir DIR] [--keep]

EXIT: 0 = every requested mode and variant reached `done` and every assertion held; 1 = a step or
an assertion failed (the JSON names the first failing step); 2 = bad arguments or a missing
precondition (`git` / `make` not found, the README unreadable) -- never reported as green.

Stdlib only. POSIX only (a `make` test target and a `#!python` fake gh); on Windows it exits 2.
"""
import ast
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
FAKE_GH_SOURCE = pathlib.Path("tests") / "test_public_bootstrap_control.py"
FAKE_REPO = "acme/onboarding-demo"
SCHEMA = "sigma.onboarding-control/1"
#: The one trivial goal the control files, and the file its "work" writes. The Makefile's test
#: target checks existing output; the goal acceptance command requires that output to exist.
#: Together they fail before the work and pass after it, while the earlier demo has a green baseline.
GOAL_TITLE = "Add hello.txt"
WORK_FILE = "hello.txt"
# The demo precedes the hello goal. Repository checks must be green before that future
# feature exists; its acceptance command below separately requires the feature's output.
MAKEFILE = "test:\n\ttest ! -e hello.txt || test -s hello.txt\n"
#: The work each known goal takes. Local mode with --demo queues the demo goal too (its own
#: frontmatter verify_command); a goal the control does not know is a red, never guessed at.
DEMO_FILE = "sigma-demo.md"
#: confirm: the Makefile target, confirmed through the README's own gesture (enforce ON).
#: no-command: nothing verify_detect can propose, the verify question left open -- so what the
#: SCAFFOLD writes is what `record done` sees. The confirm variant overwrites whatever default
#: init scaffolded, so only this variant sees a bad default (review of PR #306).
VARIANTS = ("confirm", "no-command", "process")
#: The README's one placeholder for the directory Sigma's scripts live in (#277): defined once in the
#: Quickstart, and every `python3` gesture a user copies starts with it.
INSTALLED_SIGMA = "<installed-sigma>"
#: The user guides the README links for detail. Their `python3` gestures are a user's to copy just
#: like the README's own, so the `readme-usage` mode checks them too (read from the Sigma tree).
README_DOCS = ("docs/getting-started.md", "docs/running-the-loop.md", "docs/backlog.md",
               "docs/worktrees-and-merging.md", "docs/planning-the-backlog.md",
               "docs/action-log-and-ledger.md", "docs/parallelism.md", "docs/knowledge-graph.md",
               "docs/companions.md", "docs/managed-settings.md", "docs/routines.md",
               "docs/configuration.md", "docs/features.md", "docs/architecture.md",
               "docs/platform-support.md")
#: The README subsections whose `python3` gestures the control EXECUTES from the repository root.
GESTURE_SECTIONS = ("### What `/sigma-init` will ask you", "### If `/sigma-init` says you lack access")
#: Exit codes a README gesture may return: `preflight.py check` exits 1 on a blocking failure (the
#: fake world has no real token), which is its documented report, not a broken gesture. Everything
#: else must exit 0; a usage error is 2. The exit code alone is not trusted: GESTURE_EFFECTS below
#: asserts what each gesture DID.
GESTURE_OK_RC = {("preflight.py", "check"): (0, 1)}


def _cfg(repo):
    return json.loads((repo / ".sdlc" / "config.json").read_text(encoding="utf-8"))


def _preflight_report(proc, repo):
    """A preflight REPORT, never a usage line: exactly one header, `OK - ...` or `N problem(s)`.
    rc 1 (blocking) needs the problems header; rc 0 takes either (a non-blocking problem, e.g. a
    non-github.com origin, is reported at exit 0)."""
    ok = re.search(r"(?m)^sigma: preflight OK - ", proc.stdout)
    bad = re.search(r"(?m)^sigma: preflight - \d+ problem\(s\)", proc.stdout)
    if bool(ok) == bool(bad):
        return False
    return proc.returncode == 0 or (proc.returncode == 1 and bool(bad))


#: (script, verb) -> predicate(proc, repo): the observable effect of each executed README gesture
#: (#277 review: "assert the effect, not just the code"). A gesture with no entry here is red.
GESTURE_EFFECTS = {
    ("verify_detect.py", "set"): lambda p, r: _cfg(r)["verify"].get("command") == "make test"
    and _cfg(r)["verify"].get("enforce") is True,
    ("verify_detect.py", "decline"): lambda p, r: _cfg(r)["verify"].get("enforce") is False,
    ("preflight.py", "check"): _preflight_report,
    ("preflight.py", "use-remote"): lambda p, r: _cfg(r)["work"].get("remote") == "origin",
    ("preflight.py", "local-only"): lambda p, r: _cfg(r)["work"].get("enabled") is False,
}


class Red(Exception):
    """A failed step: the control is red at `step`."""

    def __init__(self, step, detail):
        super().__init__(f"{step}: {detail}")
        self.step, self.detail = step, detail


# ------------------------------------------------------------------------------ README parsing

_FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.S)


def _section(text, heading):
    m = re.search(r"(?m)^## " + re.escape(heading) + r"\s*$", text)
    if not m:
        return None
    rest = text[m.end():]
    end = re.search(r"(?m)^## ", rest)
    return rest[:end.start()] if end else rest


def _lines(block):
    out = []
    for raw in block.splitlines():
        line = raw.split("  #", 1)[0].strip()          # a trailing `# comment` is prose
        if line and not line.startswith("#"):
            out.append(line)
    return out


def _plugin_id(sigma):
    """`<plugin>@<marketplace>` as `.claude-plugin/marketplace.json` declares it (Claude Code and
    Codex both read that file), so the README's install id is checked against what ships."""
    path = pathlib.Path(sigma) / ".claude-plugin" / "marketplace.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return f"{doc['plugins'][0]['name']}@{doc['name']}"
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise Red("readme", f"cannot read the plugin id from {path}: {exc}")


def readme_init_flags(quickstart):
    """Every `--flag` the Quickstart shows for init: on a fenced `/sigma-init ...` or
    `python3 .../init_flow.py ...` line, and in inline code in the prose of the `###` subsections
    about `/sigma-init` (e.g. the `[ask]` flag list). Sorted, de-duplicated."""
    flags = set()
    for block in _FENCE.findall(quickstart):
        for line in _lines(block):
            toks = line.split()
            if toks[0] in ("/sigma-init", "/sigma-setup") or \
                    (len(toks) > 1 and toks[1].endswith("init_flow.py")):
                flags.update(t for t in toks if re.fullmatch(r"--[a-z][a-z-]*", t))
    for sub in re.split(r"(?m)^### ", _FENCE.sub("", quickstart)):
        if "/sigma-init" in sub.split("\n", 1)[0]:
            flags.update(re.findall(r"`(--[a-z][a-z-]*)(?:[ =][^`]*)?`", sub))
    return sorted(flags)


def init_flow_flags(sigma):
    """The flags init_flow.py's own `parse()` accepts: its `_VALUE | _BOOL` sets, read by `ast`
    (never imported: importing it loads siblings and is not what a reader of the README runs)."""
    path = pathlib.Path(sigma) / "skills" / "sigma-init" / "scripts" / "init_flow.py"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as exc:
        raise Red("readme", f"cannot read init_flow.py's flags from {path}: {exc}")
    found = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") in ("_VALUE", "_BOOL")
                                                for t in node.targets):
            found |= set(ast.literal_eval(node.value))
    if not found:
        raise Red("readme", f"no _VALUE/_BOOL flag sets in {path}")
    return found


def public_url(sigma=ROOT):
    """The public repository's git URL, from the launch definition (#524): the README's install lines
    carry it, and `--install` swaps in the local checkout so the run does not need the network."""
    try:
        repo = json.loads((pathlib.Path(sigma) / "docs" / "launch" / "definition.json").read_text())["public_repo"]
    except (OSError, ValueError, KeyError) as exc:
        raise Red("readme", f"cannot read public_repo from docs/launch/definition.json: {exc}")
    return "https://github.com/" + repo


def parse_quickstart(text, sigma=ROOT):
    """-> dict of the gestures the control runs, or raises Red("readme", ...) naming what is
    missing. Pure over README text (plus existence checks against `sigma`), so a drift control can
    feed it a doctored copy and run the very code the real run does."""
    qs = _section(text, "Quickstart")
    if qs is None:
        raise Red("readme", "no `## Quickstart` section")
    lines = [l for block in _FENCE.findall(qs) for l in _lines(block)]
    out = {"claude_install": [l for l in lines if l.startswith("claude plugin ")],
           "codex_install": [l for l in lines if l.startswith("codex plugin ")]}
    # The first-run pair lives in the `### Claude Code` subsection; a later `/sigma-init` (the
    # "Adopting into an existing repo" line) must not stand in for it if it drifts.
    m = re.search(r"(?ms)^### Claude Code\s*$(.*?)(?=^### |\Z)", qs)
    first = [l for block in _FENCE.findall(m.group(1) if m else "") for l in _lines(block)]
    init = [l for l in first if l.startswith("/sigma-init")]
    if not init:
        raise Red("readme", "the Quickstart's `### Claude Code` block has no `/sigma-init` line")
    out["init_flags"] = init[0].split()[1:]
    if not any(l.split()[0] == "/sigma-loop" for l in first):
        raise Red("readme", "the Quickstart's `### Claude Code` block has no `/sigma-loop` line")
    scripts = [re.search(r"(skills/sigma-init/scripts/[\w.-]+\.py)", l) for l in lines
               if "init_flow" in l or "sigma-init/scripts/" in l]
    scripts = [m.group(1) for m in scripts if m]
    if not scripts:
        raise Red("readme", "the Quickstart names no skills/sigma-init/scripts/*.py init script")
    out["init_script"] = scripts[0]
    out["session_install"] = [l for l in first if l.startswith("/plugin ")]
    plugin_id = _plugin_id(sigma)
    # Every install form the Quickstart shows, checked the same way: its marketplace-add line and
    # its install line, the id matching what .claude-plugin/marketplace.json actually declares.
    for key, prefix, verb in (("claude_install", "claude plugin", "install"),
                              ("session_install", "/plugin", "install"),
                              ("codex_install", "codex plugin", "add")):
        want = [f"{prefix} marketplace add {public_url(sigma)}", f"{prefix} {verb} {plugin_id}"]
        if [" ".join(l.split()) for l in out[key]] != want:
            raise Red("readme", f"the Quickstart's `{prefix}` lines are {out[key]}, not {want} "
                      "(the id must be what .claude-plugin/marketplace.json declares)")
    out["init_readme_flags"] = readme_init_flags(qs)
    accepted = init_flow_flags(sigma)
    unknown = sorted(set(out["init_readme_flags"]) - accepted)
    if unknown:
        raise Red("readme", f"the README shows init flag(s) {unknown}, which "
                  f"{out['init_script']} does not accept")
    asks = text[text.find("### What `/sigma-init` will ask you"):] if \
        "### What `/sigma-init` will ask you" in text else ""
    confirm = [l for block in _FENCE.findall(asks) for l in _lines(block)
               if "verify_detect.py" in l and "<n>" in l and "<id>" in l]
    if not confirm:
        raise Red("readme", "no `verify_detect.py ... <n> <id>` confirm gesture under "
                  "\"What /sigma-init will ask you\"")
    out["verify_confirm"] = confirm[0]
    for rel in [out["init_script"]] + [t for t in shlex.split(confirm[0]) if t.endswith(".py")]:
        if not (pathlib.Path(sigma) / rel.replace(INSTALLED_SIGMA + "/", "")).is_file():
            raise Red("readme", f"the README names {rel}, which {sigma} does not ship")
    out["gestures"] = readme_gestures(text)
    if not out["gestures"]:
        raise Red("readme", f"no `python3` gesture under {GESTURE_SECTIONS}")
    return out


def readme_gestures(text):
    """Every `python3 ...` gesture in the two init subsections (fenced lines and inline code, e.g.
    the access table's), in README order -- the commands the README tells a user to copy. Pure."""
    out = []
    for heading in GESTURE_SECTIONS:
        start = text.find(heading)
        if start < 0:
            raise Red("readme", f"no {heading!r} subsection")
        body = text[start + len(heading):]
        end = re.search(r"(?m)^##+ ", body)
        body = body[:end.start()] if end else body
        spans = _FENCE.findall(body) + re.findall(r"`([^`\n]+)`", _FENCE.sub("", body))
        for span in spans:
            for line in _lines(span):
                if re.match(r"python3?\s", line) and line not in out:
                    out.append(line)
    return out


# ------------------------------------------------------------------------------ plumbing

class Run:
    """One mode's run: an ordered step log with durations, and the assertions made."""

    def __init__(self, mode):
        self.mode, self.steps, self.assertions, self.failed = mode, [], [], None
        self.t0 = time.monotonic()

    def step(self, name, argv, cwd, env, ok_rc=(0,), stdin=None):
        t = time.monotonic()
        proc = subprocess.run([str(a) for a in argv], cwd=str(cwd), env=env, input=stdin,
                              capture_output=True, text=True)
        secs = round(time.monotonic() - t, 3)
        rec = {"step": name, "argv": [str(a) for a in argv], "rc": proc.returncode,
               "seconds": secs, "stdout_tail": proc.stdout[-600:], "stderr_tail": proc.stderr[-600:]}
        self.steps.append(rec)
        if ok_rc is not None and proc.returncode not in ok_rc:
            raise Red(name, f"exit {proc.returncode}: {(proc.stderr or proc.stdout).strip()[-400:]}")
        return proc

    def timed(self, name, fn):
        t = time.monotonic()
        try:
            return fn()
        finally:
            self.steps.append({"step": name, "seconds": round(time.monotonic() - t, 3)})

    def result(self, extra=None):
        out = {"mode": self.mode, "ok": self.failed is None and all(a["ok"] for a in self.assertions),
               "failed_step": self.failed, "seconds": round(time.monotonic() - self.t0, 3),
               "steps": self.steps, "assertions": self.assertions}
        if extra:
            out.update(extra)
        if out["ok"] is False and out["failed_step"] is None:
            out["failed_step"] = "assert:" + next(a["name"] for a in self.assertions if not a["ok"])
        return out


def _control_path(bin_dir=None):
    """The PATH every step of the control runs under (the one `red_green` also spawns `python3` from)."""
    py_dir = str(pathlib.Path(sys.executable).parent)
    return os.pathsep.join(([str(bin_dir)] if bin_dir else []) + [py_dir, "/usr/local/bin", "/usr/bin", "/bin"])


def _env(root, bin_dir, extra=None):
    env = {"PATH": _control_path(bin_dir),
           "HOME": str(root / "home"), "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_CONFIG_SYSTEM": os.devnull, "PYTHONDONTWRITEBYTECODE": "1",
           "GIT_AUTHOR_NAME": "Onboarding Control", "GIT_AUTHOR_EMAIL": "control@example.com",
           "GIT_COMMITTER_NAME": "Onboarding Control", "GIT_COMMITTER_EMAIL": "control@example.com",
           "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8",
           "SIGMA_RESOURCE_MODE": "off"}   # #1600: a loaded machine must not turn the control into a HOLD
    env.update(extra or {})
    (root / "home").mkdir(exist_ok=True)
    return env


def _git(args, cwd, env):
    proc = subprocess.run(["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True)
    if proc.returncode:
        raise Red("setup", f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc


#: Characters a README/printed argument may not carry: each one only means something to a shell
#: (substitution, chaining, pipes, redirection, globbing, quoting escapes) or is not visible text.
_ARG_UNSAFE = re.compile(r"[`$;&|<>(){}*?!\\\x00-\x1f\x7f]")


def _py_argv(line, sigma, subs, step="readme gesture", *, cwd=None):
    """A README/printed `python3 <script> ...` line -> argv, or Red(step) naming what was refused.

    PINNED SHAPE, because the text comes from a document and a program's output, not from code:
    the first token must be `python3` / `python` / `py` (what `verify_detect.python_command()`
    prints) and becomes THIS interpreter (so a 3.10 CI leg tests 3.10); the second must be a
    `.py` script that EXISTS under the Sigma directory's `skills/` or `tools/`. The script path is
    read the way a shell reads it from `cwd` -- the user's repository root, where the README says
    to run every gesture (#277): `<installed-sigma>` becomes the Sigma directory, and any other
    relative path resolves against `cwd`, so a README path relative to the plugin directory (which
    a user could not copy from their repository) is refused as "not a script under ...". Every
    remaining argument, after each `<placeholder>` in `subs` is replaced, must be free of shell
    syntax. Anything else is refused before anything runs. Nothing goes through a shell."""
    try:
        toks = shlex.split(line)
    except ValueError as exc:
        raise Red(step, f"refused {line!r}: {exc}")
    if len(toks) < 2 or not re.fullmatch(r"python3?|py", toks[0]):
        raise Red(step, f"refused {line!r}: not `python3 <sigma script> ...`")
    sigma = pathlib.Path(sigma).resolve()
    # Legacy internal callers parse generated `<sigma>/...` commands rather than README text.
    # Preserve that safe, plugin-root-relative behavior while README callers pass their repository
    # root explicitly and therefore remain subject to the documented-gesture check.
    cwd = sigma if cwd is None else pathlib.Path(cwd).resolve()
    script = pathlib.Path(toks[1].replace(INSTALLED_SIGMA, str(sigma)))
    script = (script if script.is_absolute() else cwd / script).resolve()
    inside = any(sigma / d in script.parents for d in ("skills", "tools"))
    if script.suffix != ".py" or not inside or not script.is_file():
        raise Red(step, f"refused {line!r}: {toks[1]} is not a script under {sigma}/skills or "
                  f"/tools, read from the repository root {cwd}")
    out = [sys.executable, str(script)]
    for t in toks[2:]:
        for k, v in subs.items():
            t = t.replace(k, v)
        if _ARG_UNSAFE.search(t):
            raise Red(step, f"refused {line!r}: argument {t!r} carries shell syntax")
        out.append(t)
    return out


def _frontmatter(path):
    text = pathlib.Path(path).read_text(encoding="utf-8")
    head = text[3:].split("\n---", 1)[0] if text.startswith("---") else ""
    return dict(re.findall(r"(?m)^(\w+):\s*\"?(.*?)\"?\s*$", head))


_ASK = re.compile(r"(?m)^\s*\[ask\] ([\w-]+): (.*)$")
_ALT = re.compile(r"(--[a-z][a-z-]*)(?: (\S+))?")


def parse_asks(output):
    """Every `[ask]` line init printed -> {question id: [(flag, value spec or None), ...]}.

    The shape is init_flow.ask_line's: `[ask] <id>: <prose> -> <alt> ; <alt> ...`, each alt a bare
    `--flag` or `--flag VALUE|VALUE` / `--flag PLACEHOLDER`. A line without that machine part is a
    red ("unanswerable [ask]"), never a guess read out of the prose."""
    asks = {}
    for qid, rest in _ASK.findall(output):
        machine = rest.rsplit(" -> ", 1)[1] if " -> " in rest else ""
        alts = [_ALT.fullmatch(a.strip()) for a in machine.split(" ; ")] if machine else [None]
        if not all(alts):
            raise Red("init", f"unanswerable [ask] {qid}: no machine-readable "
                      f"`-> --flag VALUE|VALUE` part in {rest!r}")
        asks[qid] = [(m.group(1), m.group(2)) for m in alts]
    return asks


#: The answer a READER of the README gives, by question id: (flag NAME, value). The flag must be one
#: the `[ask]` line offers -- a question whose line does not offer it (a renamed flag), or a question
#: id not in this table (a new question), has no policy and the control is red. VERIFY_BY_README:
#: answered after init by the README's own confirm gesture (or, in the no-command variant, left
#: open, as a user with no test command to confirm does) -- never by an init flag.
VERIFY_BY_README = "<the README's verify_detect.py confirm gesture>"
ASK_POLICY = {
    "mode": ("--mode", lambda mode: "github" if mode == "github" else "local-goals"),
    "work": ("--local-only", None),
    "board": ("--board", "no"),
    "ledger": ("--ledger", "no"),
    "verify": ("--verify", VERIFY_BY_README),
}


def answer_asks(asks, mode):
    """{qid: alts} -> the init flags that answer them (verify excluded: see VERIFY_BY_README)."""
    flags = []
    for qid, alts in asks.items():
        if qid not in ASK_POLICY:
            raise Red("init", f"unanswerable [ask] {qid}: the control has no policy for this "
                      f"question (it offers {[a for a, _ in alts]})")
        flag, value = ASK_POLICY[qid]
        spec = dict(alts)
        if flag not in spec:
            raise Red("init", f"unanswerable [ask] {qid}: it offers {sorted(spec)}, not the "
                      f"{flag} a reader of the README answers with")
        if value == VERIFY_BY_README:
            continue
        value = value(mode) if callable(value) else value
        if value is None:
            if spec[flag] is not None:
                raise Red("init", f"unanswerable [ask] {qid}: {flag} now takes a value ({spec[flag]})")
            flags.append(flag)
            continue
        choices = spec[flag]
        if choices is None or (choices.lower() == choices and value not in choices.split("|")):
            raise Red("init", f"unanswerable [ask] {qid}: {flag} offers {choices!r}, "
                      f"not {value!r}")
        flags += [flag, value]
    return flags


def _candidate(init_out):
    """The first verify candidate init printed: (n, id). The printed confirm gesture carries both."""
    m = re.search(r"verify_detect\.py\S*\s+confirm\s+\S+\s+(\d+)\s+([0-9a-f]{12})", init_out)
    if not m:
        raise Red("verify confirm", "init printed no `verify_detect.py confirm <sdlc> <n> <id>` line")
    return m.group(1), m.group(2)


def _loop_next_line(init_out):
    m = re.search(r"Next: .*\(Codex/Cursor: (.+?)\)\.?\s*$", init_out, re.M)
    if not m:
        raise Red("init", "init printed no `Next: ... (Codex/Cursor: <loop.py next ...>)` line")
    return m.group(1)


# ------------------------------------------------------------------------------ the flow

def _init_and_verify(run, qs, sigma, repo, env, mode, variant):
    """README flags, then the [ask] answers, then (confirm variant) the README's confirm gesture.
    -> (init's last stdout, config.json's `verify` as init left it, before any confirm)."""
    init = [sys.executable, pathlib.Path(sigma) / qs["init_script"], "."] + qs["init_flags"]
    out = run.step("init (README flags)", init, repo, env, ok_rc=(0, 1))
    answered, flags = set(), []
    # Each run can open questions the last one could not reach (github mode's board/ledger appear
    # once the mode is answered): answer every open one with the flag its [ask] line offers,
    # re-run, repeat. verify stays open here by design (VERIFY_BY_README).
    for _ in range(4):
        asks = parse_asks(out.stdout)
        answer_asks(asks, mode)                        # every open question must have a policy
        asks = {q: a for q, a in asks.items() if q != "verify"}
        new = {q: a for q, a in asks.items() if q not in answered}
        if not asks and out.returncode == 0:
            break
        if not new:
            raise Red("init", f"questions still open after answering them: {sorted(asks)} "
                      f"(exit {out.returncode})")
        answered.update(new)
        flags += answer_asks(new, mode)
        if mode == "github" and "mode" in new:
            # No [ask] names --repo: the control's origin is a local path (or absent), not a GitHub
            # owner/name, so github mode needs the repository given -- as the SKILL table says.
            flags += ["--repo", FAKE_REPO]
        out = run.step("init (answers: " + ",".join(new) + ")", init + flags, repo, env, ok_rc=(0, 1))
    else:
        raise Red("init", "init still asking after four rounds of answers")
    if out.returncode != 0:
        raise Red("init", f"exit {out.returncode}: {out.stdout.strip()[-300:]}")
    cfg = json.loads((repo / ".sdlc" / "config.json").read_text(encoding="utf-8"))
    scaffolded = {k: cfg["verify"].get(k) for k in ("command", "enforce")}
    if variant == "no-command":
        run.assertions.append(_check("no verify candidate was detected (the variant's premise)",
                                     not re.search(r"verify_detect\.py\S*\s+confirm\s", out.stdout),
                                     out.stdout[-300:]))
        return out.stdout, scaffolded
    n, ident = _candidate(out.stdout)
    argv = _py_argv(qs["verify_confirm"], sigma, {"<n>": n, "<id>": ident},
                    step="verify confirm (README gesture)", cwd=repo)
    run.step("verify confirm (README gesture)", argv, repo, env)
    cfg = json.loads((repo / ".sdlc" / "config.json").read_text(encoding="utf-8"))
    run.assertions.append(_check("verify confirmed: enforce ON with a command",
                                 bool(cfg["verify"].get("enforce")) and bool(cfg["verify"].get("command")),
                                 cfg["verify"]))
    return out.stdout, scaffolded


#: What the README's own placeholders stand for when the control runs a gesture (#277). `<n>`/`<id>`
#: belong to the confirm gesture, which `verify confirm (README gesture)` runs with init's printed
#: candidate; the file holds the Makefile target that variant confirms.
GESTURE_FILE = "verify-command.txt"
GESTURE_SUBS = {"<file>": GESTURE_FILE, "<remote>": "origin"}


def run_readme_gestures(run, qs, sigma, repo, env):
    """Every README `python3` gesture from the two init subsections, run as the README says: from
    the repository root, `<installed-sigma>` = the Sigma directory. -> [(gesture, rc)]. A gesture
    whose script path is not copyable from the repository root is refused by `_py_argv` (red at
    this step); one exiting outside GESTURE_OK_RC is red; a placeholder the control cannot fill is
    red. Runs last in github mode (confirm variant): `decline` and `local-only` change config."""
    (repo / GESTURE_FILE).write_text("make test\n", encoding="utf-8")
    ran = []
    for line in qs["gestures"]:
        if line == qs["verify_confirm"]:
            continue                                   # already run, with init's printed <n> <id>
        filled = line
        for k, v in GESTURE_SUBS.items():
            filled = filled.replace(k, v)
        left = [t for t in re.findall(r"<[\w -]+>", filled) if t != INSTALLED_SIGMA]
        if left:
            raise Red("README gesture", f"{line!r}: the control cannot fill {left}")
        argv = _py_argv(filled, sigma, {}, step="README gesture", cwd=repo)
        toks = shlex.split(filled)
        ok = GESTURE_OK_RC.get((pathlib.Path(toks[1]).name, toks[2] if len(toks) > 2 else ""), (0,))
        proc = run.step("README gesture: " + line, argv, repo, env, ok_rc=ok)
        key = (pathlib.Path(toks[1]).name, toks[2] if len(toks) > 2 else "")
        effect = GESTURE_EFFECTS.get(key)
        if effect is None or not effect(proc, repo):
            raise Red("README gesture", f"{line!r}: exit {proc.returncode} but "
                      + ("no effect is defined for it (GESTURE_EFFECTS)" if effect is None
                         else "its effect is not observable: " + proc.stdout.strip()[-300:]))
        ran.append((line, proc.returncode))
    return ran


#: A `python3 <script>.py <args>` gesture anywhere in the README, in EVERY spelling of the script
#: path (#277 review): `<installed-sigma>/...`, quoted or not, `${VAR}/...` / `$VAR/...`,
#: `~/<dir>/...`, an absolute path, a repository-relative `skills/...`, or a bare `watch_daemon.py`.
#: Group 1 is the quote, 2 the script path as written, 3 the args (to the end of the code span / line,
#: a `)` closing a `$(...)`, a table `|`, a `#` comment, or a shell `;` / `&`).
_README_GESTURE = re.compile(r"python3?\s+([\"']?)([^\s\"'`|()]+?\.py)\1(?![\w.])([^`\n)|#;&]*)")
_SHIPPED_REL = re.compile(r"(?:^|/)((?:skills|hooks|evals|tools)/[\w./-]+\.py)$")
#: `[...]` may nest one level (`[--apply [--replace-old-plugin]]`): one token, never a `[...` group
#: followed by a stray required `]`.
_USAGE_TOKEN = re.compile(r"\[(?:[^\[\]]|\[[^\]]*\])*\]|\([^)]*\)|<[^>]*>|\S+")


def _shipped_rel(script, sigma):
    """The shipped path (under the Sigma dir) a gesture's script path names, or None. A bare name
    resolves only when exactly one shipped script (outside tests/ and .sdlc/) carries it."""
    m = _SHIPPED_REL.search(script)
    if m:
        return m.group(1)
    if "/" in script or sigma is None:
        return None
    hits = [q for q in pathlib.Path(sigma).rglob(script)
            if not {"tests", ".sdlc", ".git"} & set(q.relative_to(sigma).parts)]
    return hits[0].relative_to(sigma).as_posix() if len(hits) == 1 else None


def readme_script_gestures(text, sigma=None):
    """[(script path under the Sigma dir, [args], script path as written)] for every
    `python3 <script>.py` gesture anywhere in the README, in any spelling (see `_README_GESTURE`),
    deduplicated, in README order. The first item is the path as written when it names no shipped
    script. Pure except for the bare-name lookup under `sigma`."""
    out = []
    for m in _README_GESTURE.finditer(text):
        try:
            args = shlex.split(m.group(3), comments=True)
        except ValueError:
            args = m.group(3).split()
        written = m.group(2)
        item = (_shipped_rel(written, sigma) or written, args, written)
        if item not in out:
            out.append(item)
    return out


def _split_top(body):
    """`a | b (c | d) | e` -> ['a', 'b (c | d)', 'e']: split on ` | ` outside brackets/parens."""
    parts, depth, cur, i = [], 0, "", 0
    while i < len(body):
        ch = body[i]
        depth += ch in "[(<"
        depth -= ch in "])>"
        if depth == 0 and body.startswith(" | ", i):
            parts.append(cur)
            cur, i = "", i + 3
            continue
        cur += ch
        i += 1
    return parts + [cur]


def usage_alternatives(help_text):
    """A script's `--help` text -> [{"required": [set of literals | None], "extras": n}]: one per
    usage alternative. `<x>` is a required placeholder (None), `a|b` a literal set, `[x]` one
    optional positional, `[--flag]` none, `(...)` / `...` / `[options]` any number (lenient: a
    grouped alternative is not modelled), `--flag VALUE` a flag and its value. Pure."""
    alts = []
    for line in help_text.splitlines():
        m = re.match(r"\s*(?:usage:\s*)?[\w-]+\.py\b(.*)$", line)
        if not m:
            continue
        for alt in _split_top(m.group(1)):
            alt = re.sub(r"^\s*[\w-]+\.py\b", "", alt)
            toks, req, extras, i = _USAGE_TOKEN.findall(alt), [], 0, 0
            while i < len(toks):
                t = toks[i]
                if t[0] == "(" or "..." in t or t == "[options]":
                    extras = float("inf")                         # open-ended: lenient on purpose
                elif t[0] == "[":
                    extras += not t[1:].lstrip().startswith("-")  # `[x]` one optional slot, `[--f]` none
                elif t.startswith("-") and len(t) > 1:
                    if i + 1 < len(toks) and toks[i + 1][0] not in "-[(":
                        i += 1                                    # the flag's value
                elif t.startswith("<"):
                    req.append(None)
                else:
                    req.append(set(t.split("|")))
                i += 1
            alts.append({"required": req, "extras": extras})
    return alts


def usage_problems(args, help_text):
    """Why `args` fits none of the usage alternatives in `help_text`, or [] when one fits. A
    `--flag` the help never names is a problem; a flag the help shows with a value takes the next
    argument; the rest are positionals, matched verb-first (a first positional that is a verb of
    some alternative is only matched against the alternatives starting with that verb). Pure."""
    problems, pos, i = [], [], 0
    while i < len(args):
        a = args[i]
        if a.startswith("-") and len(a) > 1:
            flag = a.split("=", 1)[0]
            if flag not in help_text:
                problems.append(f"flag {flag} is not in the script's usage")
            elif "=" not in a and re.search(re.escape(flag) + r"[ =](?![-\[\]|)])\S", help_text):
                i += 1
        else:
            pos.append(a)
        i += 1
    alts = usage_alternatives(help_text)
    verbs = set().union(*[a["required"][0] for a in alts if a["required"] and a["required"][0]])
    if pos and pos[0] in verbs:
        alts = [a for a in alts if a["required"] and a["required"][0] and pos[0] in a["required"][0]]

    def fits(alt):
        req = alt["required"]
        if not len(req) <= len(pos) <= len(req) + alt["extras"]:
            return False
        return all(r is None or p in r for p, r in zip(pos, req))
    if not any(fits(a) for a in alts):
        problems.append(f"positionals {pos} fit no usage alternative")
    return problems


def usage_corpus(readme_text, sigma):
    """The README text followed by every guide in README_DOCS that `sigma` ships. Pure but for reads."""
    docs = [pathlib.Path(sigma) / rel for rel in README_DOCS]
    return "\n".join([readme_text] + [p.read_text(encoding="utf-8") for p in docs if p.is_file()])


def check_readme_usage(text, sigma, cwd):
    """The `readme-usage` mode (#277 review): every README script gesture against its script's own
    `--help` usage. -> a mode result like run_local's. `--help` runs from `cwd` (a scratch dir)."""
    t0, checked, bad, helps = time.monotonic(), [], [], {}
    gestures = readme_script_gestures(text, sigma)
    for rel, args, written in gestures:
        script = pathlib.Path(sigma) / rel
        line = f"python3 {written} {' '.join(args)}".strip()
        if not script.is_file():
            bad.append({"gesture": line, "problems": [f"{rel} is not shipped"]})
            continue
        if not written.startswith(INSTALLED_SIGMA + "/"):
            bad.append({"gesture": line, "problems": [
                f"not copyable from the user's repository: `{written}` is not "
                f"`{INSTALLED_SIGMA}/{rel}`"]})
            continue
        if rel not in helps:
            proc = subprocess.run([sys.executable, str(script), "--help"], cwd=str(cwd),
                                  capture_output=True, text=True, timeout=60)
            helps[rel] = proc.stdout if proc.returncode == 0 and "usage" in proc.stdout else None
        if helps[rel] is None:
            bad.append({"gesture": line, "problems": ["`--help` did not exit 0 with a usage"]})
            continue
        problems = usage_problems(args, helps[rel])
        checked.append({"gesture": line, "problems": problems})
        if problems:
            bad.append({"gesture": line, "problems": problems, "usage": helps[rel].strip()[:600]})
    ok = bool(gestures) and not bad
    return {"mode": "readme-usage", "ok": ok, "failed_step": None if ok else "README usage",
            "detail": bad or (None if gestures else "no README script gesture found"),
            "checked": checked, "seconds": round(time.monotonic() - t0, 3)}


def _check(name, ok, detail=None):
    return {"name": name, "ok": bool(ok), "detail": detail}


def check_local(obs):
    """Assertions over one local goal's observations. Pure, so each can be broken once in a test."""
    ev = obs.get("evidence") or {}
    return [
        _check("record done exited 0", obs.get("record_rc") == 0, obs.get("record_rc")),
        _check("goal frontmatter status is done", obs.get("status") == "done", obs.get("status")),
        _check("verify evidence exists and passed",
               ev.get("verify_state") == "pass" and ev.get("exit") == 0, ev.get("verify_state")),
        _check("verify evidence ran a non-empty command", bool(ev.get("command")), ev.get("command")),
        _check("the loop made no gh call in local-goals mode", obs.get("gh_calls") == [],
               obs.get("gh_calls")),
    ]


def check_no_command(obs):
    """The local no-command variant's own goal (#228's trap, at the default init scaffolds): with
    no confirmable candidate and the verify question left open, init must leave enforce OFF, so
    `loop.py verify` says NO-COMMAND (exit 3) and `record done` still succeeds. The shipped bug
    (enforce ON + command "" persisted by the scaffold) is exit 4 here. Pure, like check_local."""
    cfg = obs.get("scaffolded_verify") or {}
    out = [
        _check("init left verify.enforce OFF with no command confirmed",
               not cfg.get("enforce") and not cfg.get("command"), cfg),
        _check("loop verify said NO-COMMAND (exit 3)", obs.get("verify_rc") == 3, obs.get("verify_rc")),
        _check("record done exited 0 (nothing to enforce)", obs.get("record_rc") == 0,
               obs.get("record_rc")),
    ]
    if "status" in obs:
        out.append(_check("goal frontmatter status is done", obs.get("status") == "done",
                          obs.get("status")))
    if "gh_calls" in obs:
        out.append(_check("the loop made no gh call in local-goals mode", obs.get("gh_calls") == [],
                          obs.get("gh_calls")))
    return out


def check_github(obs):
    """#232 done-means-merged, as the fake records it. Pure, like check_local."""
    return [
        _check("the review gate ran (a sigma:block parked the merge)",
               str(obs.get("blocked_merge", "")).startswith("PARK:")
               and "sigma:block" in str(obs.get("blocked_merge")), obs.get("blocked_merge")),
        _check("merge passed the review gate once approved",
               "review gate passed" in str(obs.get("merge", "")), obs.get("merge")),
        _check("record done REFUSED while the PR is open", obs.get("early_done_rc") == 4,
               obs.get("early_done_rc")),
        _check("issue still open before the PR merged", obs.get("issue_before_merge") == "open",
               obs.get("issue_before_merge")),
        _check("PR open before the human merge", obs.get("pr_before_merge") == "OPEN",
               obs.get("pr_before_merge")),
        _check("reconcile recorded done only after the merge",
               re.fullmatch(r"\d+ done \(PR #\d+ merged\)", str(obs.get("reconcile", ""))) is not None,
               obs.get("reconcile")),
        _check("issue closed after done", obs.get("issue_final") == "closed", obs.get("issue_final")),
        _check("PR merged", obs.get("pr_final") == "MERGED", obs.get("pr_final")),
        _check("the change is on origin/main", obs.get("remote_file") == "hi\n", obs.get("remote_file")),
        _check("verify evidence passed in the goal worktree",
               (obs.get("evidence") or {}).get("verify_state") == "pass",
               (obs.get("evidence") or {}).get("verify_state")),
        _check("every gh call was one the fake models", obs.get("unhandled") == "",
               obs.get("unhandled")),
    ]


def check_gestures(obs, qs):
    """#277: every README gesture of the two init subsections ran from the repository root."""
    want = [g for g in qs["gestures"] if g != qs["verify_confirm"]]
    ran = [g for g, _rc in obs.get("readme_gestures") or []]
    return _check("every README init gesture ran from the repository root", want and ran == want,
                  {"want": want, "ran": ran})


def check_github_no_command(obs):
    """#312: the github no-command variant runs the confirm variant's whole real path (work ON), so
    every check_github assertion applies except the one about evidence, which is replaced by its
    honest opposite: nothing to run, so no evidence -- and the merge passed anyway. The #312 bug is
    `merge passed the review gate once approved` red (the approved merge parked on "no fresh verify
    evidence"). Pure, like check_local."""
    cfg = obs.get("scaffolded_verify") or {}
    return [
        _check("init left verify.enforce OFF with no command confirmed",
               not cfg.get("enforce") and not cfg.get("command"), cfg),
        _check("loop verify said NO-COMMAND (exit 3)", obs.get("verify_rc") == 3, obs.get("verify_rc")),
        _check("no verify evidence was written (nothing to run)", not obs.get("evidence"),
               obs.get("evidence")),
    ] + [a for a in check_github(obs) if a["name"] != "verify evidence passed in the goal worktree"]


def _stub_gh(bin_dir, log):
    """Local mode: a `gh` that logs and fails -- so any gh call local-goals mode makes is seen."""
    path = bin_dir / "gh"
    path.write_text(f"#!{sys.executable}\nimport json,sys\n"
                    f"open({str(log)!r},'a').write(json.dumps(sys.argv[1:])+'\\n')\nsys.exit(1)\n",
                    encoding="utf-8")
    path.chmod(0o755)


def _gh_calls(log):
    if not log.is_file():
        return []
    return [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]


def _summarise_gh(calls):
    """`gh <verb> <sub>` (or `gh api <endpoint>`, numbers folded to N) -> call count."""
    counts = {}
    for argv in calls:
        if argv[:1] == ["api"]:
            rest, i = [], 1
            while i < len(argv):
                if argv[i] in ("-X", "--method", "-f", "-F", "--jq", "-H", "--input"):
                    i += 2
                    continue
                if not argv[i].startswith("-"):
                    rest.append(argv[i])
                i += 1
            key = "api " + re.sub(r"\d+", "N", (rest or [""])[0].split("?")[0])
        else:
            key = " ".join(argv[:2])
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _local_goal_work(goal_path, repo):
    fm = _frontmatter(goal_path)
    if DEMO_FILE in fm.get("done_when", "") or DEMO_FILE in fm.get("verify_command", ""):
        (repo / DEMO_FILE).write_text("Sigma ran this goal.\n", encoding="utf-8")
        return DEMO_FILE
    if fm.get("title") == GOAL_TITLE:
        (repo / WORK_FILE).write_text("hi\n", encoding="utf-8")
        return WORK_FILE
    raise Red("work", f"the loop picked a goal the control does not know: {goal_path}")


def _fresh_files(repo, variant):
    """The fresh repository's one commit: a Makefile test target (confirm variant), or only a
    README (no-command variant: nothing verify_detect can propose)."""
    if variant == "no-command":
        (repo / "README.txt").write_text("A repository with no test command.\n", encoding="utf-8")
    else:
        (repo / "Makefile").write_text(MAKEFILE, encoding="utf-8")


def run_local(sigma, readme_text, root, qs=None, variant="confirm"):
    run = Run("local-goals" + ("" if variant == "confirm" else "/" + variant))
    goals = []
    base = root / ("local" if variant == "confirm" else "local-" + variant)
    try:
        qs = qs or run.timed("readme parse", lambda: parse_quickstart(readme_text, sigma))
        repo, bin_dir = base / "repo", base / "bin"
        repo.mkdir(parents=True)
        bin_dir.mkdir()
        gh_log = base / "gh_calls.jsonl"
        _stub_gh(bin_dir, gh_log)
        env = _env(base, bin_dir)
        _git(["init", "-q", "-b", "main"], repo, env)
        _fresh_files(repo, variant)
        _git(["add", "-A"], repo, env)
        _git(["commit", "-qm", "fresh repository"], repo, env)
        init_out, scaffolded = _init_and_verify(run, qs, sigma, repo, env, "local", variant)
        if variant == "no-command":
            # The fixture still exercises the packaged demo's frontmatter
            # command before it reaches the no-command goal that tests the
            # scaffold default.  Model the operator's separate, explicit
            # trusted-project choice for that known demo; config remains
            # commandless, so the target control is unchanged.
            run.step("operator trusts packaged demo verification",
                     ["git", "-C", str(repo), "config", "--local",
                      "sigma.allowRepositoryShellCommands", "true"], repo, env)
        (repo / ".sdlc" / "goals" / "0002-onboarding-hello.md").write_text(
            f'---\nid: "0002"\ntitle: {GOAL_TITLE}\nstatus: pending\n---\n\n'
            f"Create {WORK_FILE} with one line. Filed by the onboarding control.\n", encoding="utf-8")
        run.steps.append({"step": "file goal (.sdlc/goals/0002-onboarding-hello.md)", "seconds": 0})
        init_gh = len(_gh_calls(gh_log))       # init's own preflight may ask gh; the loop may not
        pid = str(os.getpid())
        loop = pathlib.Path(sigma) / "skills" / "sigma-loop" / "scripts"
        run.step("loop start", [sys.executable, loop / "loop.py", "start", ".sdlc", "--session-pid", pid],
                 repo, env)
        nxt = _py_argv(_loop_next_line(init_out), sigma, {}, step="loop next", cwd=repo) + \
            ["--session-pid", pid]
        for _ in range(5):
            goal = run.step("loop next", nxt, repo, env).stdout.strip()
            if not goal or goal == "DONE":
                break
            if not (repo / goal).is_file():
                raise Red("loop next", f"`next` printed {goal!r}, not a goal file")
            obs = _drive_local_goal(run, loop, repo, env, goal, pid)
            goal = obs["goal"]
            obs["gh_calls"] = _gh_calls(gh_log)[init_gh:]
            obs["scaffolded_verify"] = scaffolded
            goals.append(obs)
            check = check_no_command if variant == "no-command" and obs["work"] == WORK_FILE \
                else check_local
            for a in check(obs):
                a["name"] = f"{goal}: {a['name']}"
                run.assertions.append(a)
            if obs["record_rc"] != 0:
                raise Red("record done", f"{goal}: {obs['record_err'].strip()[-300:]}")
        run.assertions.append(_check("the control's own goal reached done",
                                     any(g["work"] == WORK_FILE and g["status"] == "done" for g in goals),
                                     [(g["goal"], g["status"]) for g in goals]))
    except Red as red:
        run.failed = red.step
        run.steps.append({"step": "RED", "at": red.step, "detail": red.detail})
    return run.result({"variant": variant, "goals": goals,
                       "gh_calls": _summarise_gh(_gh_calls(base / "gh_calls.jsonl"))})


def _scripted_phases(run, loop, repo, env, goal, pid, part):
    """#684: the phases the loop's one-subagent-per-phase rule produces, scripted like the work itself.
    `part` "before" runs research, plan and plan-review (with its recorded verdict); "after" runs review
    (with its verdict) and retro. `record done` is REFUSED without them, so a control that skipped them
    would only be exercising the refusal."""
    import hashlib
    py = sys.executable
    stem = pathlib.Path(goal).stem

    def phase(name, agent):
        run.step(f"phase_report start {name}", [py, loop / "phase_report.py", "start", ".sdlc", goal, name,
                                                "--model", "haiku", "--pid", pid], repo, env)
        run.step(f"phase_report end {name}", [py, loop / "phase_report.py", "end", ".sdlc", goal, name,
                                              "--agent-id", agent, "--pid", pid], repo, env)
    if part == "before":
        (repo / ".sdlc" / "research").mkdir(parents=True, exist_ok=True)
        (repo / ".sdlc" / "plans").mkdir(parents=True, exist_ok=True)
        (repo / ".sdlc" / "research" / f"{stem}.md").write_text("# Research\nscripted\n", encoding="utf-8")
        phase("research", "onb-research")
        plan = repo / ".sdlc" / "plans" / f"{stem}.md"
        plan.write_text("# Plan\nscripted\n", encoding="utf-8")
        phase("plan", "onb-plan")
        phase("plan_review", "onb-plan-reviewer")
        sha = hashlib.sha256(plan.read_bytes()).hexdigest()
        run.step("record plan-review", [py, loop / "work.py", "record-plan-review", ".sdlc", goal, "--verdict",
                                        "SOUND", "--plan-sha256", sha, "--agent-id", "onb-plan-reviewer"],
                 repo, env)
        wt = repo / ".sdlc" / "work" / goal / ".sdlc"          # work.py pr wants both on the branch
        if wt.parent.is_dir():
            for sub in ("research", "plans"):
                (wt / sub).mkdir(parents=True, exist_ok=True)
                shutil.copy2(repo / ".sdlc" / sub / f"{stem}.md", wt / sub / f"{stem}.md")
    else:
        phase("review", "onb-reviewer")
        run.step("record review", [py, loop / "work.py", "record-review", ".sdlc", goal, "--verdict",
                                   "APPROVE", "--agent-id", "onb-reviewer"], repo, env)
        phase("retro", "onb-retro")


def _drive_local_goal(run, loop, repo, env, goal, pid):
    """The sigma-loop skill's per-goal gestures with work.enabled off (--local-only). `goal` is a
    goal file (local-goals mode)."""
    is_file = (repo / goal).is_file()
    py = sys.executable
    name = pathlib.Path(goal).name
    run.step(f"agent-start {name}", [py, loop / "loop.py", "agent-start", ".sdlc", goal, "--pid", pid],
             repo, env)
    draft = repo / ".sdlc" / "onboarding-acceptance.md"
    draft.write_text("## Done when\n- [ ] The goal's declared output is created.\n"
                     "- [ ] Verification succeeds when configured.\n"
                     "- [ ] The goal is recorded done through the loop.\n", encoding="utf-8")
    capture = [py, loop / "acceptance.py", "record", ".sdlc", goal, "--draft", draft]
    cfg = json.loads((repo / ".sdlc" / "config.json").read_text(encoding="utf-8"))
    if is_file and _frontmatter(repo / goal).get("title") == GOAL_TITLE and (cfg.get("verify") or {}).get("command"):
        capture += ["--verify-command", "test -s " + WORK_FILE]
    run.step(f"record acceptance {name}", capture, repo, env)
    _scripted_phases(run, loop, repo, env, goal, pid, "before")
    run.step(f"phase_report start {name}", [py, loop / "phase_report.py", "start", ".sdlc", goal,
                                            "implement", "--model", "haiku", "--pid", pid], repo, env)
    if is_file:
        work = _local_goal_work(repo / goal, repo)
    else:
        (repo / WORK_FILE).write_text("hi\n", encoding="utf-8")
        work = WORK_FILE
    end = run.step(f"phase_report end {name}", [py, loop / "phase_report.py", "end", ".sdlc", goal,
                                                "implement", "--pid", pid], repo, env)
    _scripted_phases(run, loop, repo, env, goal, pid, "after")
    # loop.py verify's exit is recorded, not fatal: `record done` is the gate that decides, and the
    # control asserts at the gate -- so the empty-command trap reads red AT `record done`.
    ver = run.step(f"loop verify {name}", [py, loop / "loop.py", "verify", ".sdlc", goal], repo, env,
                   ok_rc=None)
    rec = run.step(f"record done {name}", [py, loop / "loop.py", "record", ".sdlc", goal, "done"],
                   repo, env, ok_rc=None)
    ev_path = repo / ".sdlc" / "state" / "verify" / (pathlib.Path(goal).stem + ".json")
    evidence = json.loads(ev_path.read_text(encoding="utf-8")) if ev_path.is_file() else None
    return {"goal": pathlib.Path(goal).name, "work": work, "verify_rc": ver.returncode, "record_rc": rec.returncode,
            "record_err": rec.stderr + rec.stdout, "status": _frontmatter(repo / goal).get("status") if is_file else None,
            "evidence": {k: (evidence or {}).get(k) for k in ("command", "exit", "verify_state")}
            if evidence else None,
            "cost_line": next((l for l in end.stdout.splitlines() if "cost" in l), "")}


# ------------------------------------------------------------------------------ github mode

def _fake_gh_body(sigma):
    """The fake gh from tests/test_public_bootstrap_control.py, extracted by `ast` (never
    imported -- that module needs pytest), so the control and that test share one fake."""
    src = pathlib.Path(sigma) / FAKE_GH_SOURCE
    if not src.is_file():
        src = ROOT / FAKE_GH_SOURCE
    tree = ast.parse(src.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "_FAKE_GH_BODY" for t in node.targets):
            return ast.literal_eval(node.value)
    raise Red("setup", f"no _FAKE_GH_BODY in {src}")


def run_github(sigma, readme_text, root, qs=None, variant="confirm"):
    run = Run("github" + ("" if variant == "confirm" else "/" + variant))
    obs = {}
    base = root / ("github" if variant == "confirm" else "github-" + variant)
    state_path, log_path, unhandled = base / "gh_state.json", base / "gh_log.jsonl", base / "gh_unhandled.jsonl"
    try:
        qs = qs or run.timed("readme parse", lambda: parse_quickstart(readme_text, sigma))
        bin_dir, remote, repo = base / "bin", base / "remote.git", base / "repo"
        bin_dir.mkdir(parents=True)
        gh = bin_dir / "gh"
        gh.write_text(f"#!{sys.executable}\n" + _fake_gh_body(sigma), encoding="utf-8")
        gh.chmod(0o755)
        log_path.write_text("", encoding="utf-8")
        unhandled.write_text("", encoding="utf-8")
        state_path.write_text(json.dumps({
            "repo": FAKE_REPO, "remote_git_dir": str(remote), "labels": {}, "label_seq": 0,
            "issues": {}, "issue_seq": 1, "prs": {}, "pr_seq": 100, "projects": [],
            "login": "onboarding-bot", "default_branch": "main", "allow_auto_merge": True,
            "viewer_permission": "ADMIN", "refuse_label_create": False}), encoding="utf-8")
        env = _env(base, bin_dir, {"FAKE_GH_STATE": str(state_path), "FAKE_GH_LOG": str(log_path),
                                   "FAKE_GH_UNHANDLED": str(unhandled)})
        _git(["init", "-q", "--bare", "-b", "main", str(remote)], base, env)
        _git(["clone", "-q", str(remote), str(repo)], base, env)
        _fresh_files(repo, variant)
        _git(["add", "-A"], repo, env)
        _git(["commit", "-qm", "fresh repository"], repo, env)
        _git(["push", "-q", "-u", "origin", "main"], repo, env)
        # #312: both variants run work ON -- the real github path. (The no-command variant used to
        # add --local-only, because `work.py merge` demanded verify evidence even with enforce off
        # and no command to produce it; that was the bug, not the premise.)
        init_out, obs["scaffolded_verify"] = _init_and_verify(run, qs, sigma, repo, env, "github",
                                                              variant)

        def gh_(args, name):
            return run.step(name, [gh] + args, repo, env)

        def state():
            return json.loads(state_path.read_text(encoding="utf-8"))

        gh_(["issue", "create", "--repo", FAKE_REPO, "--label", "sdlc:goal", "--assignee",
             "@me", "--title", GOAL_TITLE, "--body", f"Create {WORK_FILE} with one line.\n\n"
             f"## Done when\n- [ ] {WORK_FILE} contains hi.\n"
             "- [ ] Verification passes when configured.\n"
             "- [ ] The goal can be recorded done after its PR merges.\n"],
            "file goal (gh issue create, labelled sdlc:goal, assigned @me)")
        pid = str(os.getpid())
        loop = pathlib.Path(sigma) / "skills" / "sigma-loop" / "scripts"
        py = sys.executable
        run.step("loop start", [py, loop / "loop.py", "start", ".sdlc", "--session-pid", pid], repo, env)
        nxt = _py_argv(_loop_next_line(init_out), sigma, {}, step="loop next", cwd=repo) + \
            ["--session-pid", pid]
        goal = run.step("loop next", nxt, repo, env).stdout.strip()
        if goal != "1":
            raise Red("loop next", f"expected issue 1, got {goal!r}")
        run.step("agent-start", [py, loop / "loop.py", "agent-start", ".sdlc", goal, "--pid", pid], repo, env)
        run.step("work start", [py, loop / "work.py", "start", ".sdlc", goal, "--session-pid", pid], repo, env)
        capture = [py, loop / "acceptance.py", "record", ".sdlc", goal]
        cfg = json.loads((repo / ".sdlc" / "config.json").read_text(encoding="utf-8"))
        if (cfg.get("verify") or {}).get("command"):
            capture += ["--verify-command", "test -s " + WORK_FILE]
        run.step("record acceptance", capture, repo, env)
        acceptance = repo / ".sdlc" / "acceptance" / (goal + ".md")
        target = repo / ".sdlc" / "work" / goal / ".sdlc" / "acceptance" / acceptance.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(acceptance, target)
        _scripted_phases(run, loop, repo, env, goal, pid, "before")
        run.step("phase_report start", [py, loop / "phase_report.py", "start", ".sdlc", goal, "implement",
                                        "--model", "haiku", "--pid", pid], repo, env)
        (repo / ".sdlc" / "work" / goal / WORK_FILE).write_text("hi\n", encoding="utf-8")
        end = run.step("phase_report end", [py, loop / "phase_report.py", "end", ".sdlc", goal,
                                            "implement", "--pid", pid], repo, env)
        obs["cost_line"] = next((l for l in end.stdout.splitlines() if "cost" in l), "")
        _scripted_phases(run, loop, repo, env, goal, pid, "after")
        obs["verify_rc"] = run.step("loop verify", [py, loop / "loop.py", "verify", ".sdlc", goal],
                                    repo, env, ok_rc=None).returncode
        ev = repo / ".sdlc" / "state" / "verify" / f"{goal}.json"
        obs["evidence"] = json.loads(ev.read_text(encoding="utf-8")) if ev.is_file() else None
        run.step("work commit", [py, loop / "work.py", "commit", ".sdlc", goal, "--message",
                                 f"sdlc: {GOAL_TITLE.lower()}"], repo, env)
        run.step("work pr", [py, loop / "work.py", "pr", ".sdlc", goal, "--no-tests",
                             "Bootstrap fixture writes text only; shell verification is retained."], repo, env)
        pr = next(iter(state()["prs"]), None)
        if pr is None:
            raise Red("work pr", "no PR was opened")
        gh_(["pr", "comment", pr, "--repo", FAKE_REPO, "--body", "sigma:block no test for this"],
            "reviewer comments sigma:block")
        obs["blocked_merge"] = run.step("work merge (blocked)", [py, loop / "work.py", "merge", ".sdlc",
                                                                 goal], repo, env).stdout.strip()
        gh_(["pr", "comment", pr, "--repo", FAKE_REPO, "--body", "sigma:approve"],
            "reviewer comments sigma:approve")
        obs["merge"] = run.step("work merge", [py, loop / "work.py", "merge", ".sdlc", goal],
                                repo, env).stdout.strip()
        obs["early_done_rc"] = run.step("record done (PR still open)", [py, loop / "loop.py", "record",
                                        ".sdlc", goal, "done"], repo, env, ok_rc=None).returncode
        run.step("record review", [py, loop / "loop.py", "record", ".sdlc", goal, "review"], repo, env)
        obs["issue_before_merge"] = state()["issues"][goal]["state"]
        obs["pr_before_merge"] = state()["prs"][pr]["state"]
        gh_(["pr", "merge", pr, "--repo", FAKE_REPO, "--squash"], "human merges the PR")
        obs["reconcile"] = run.step("loop reconcile-merges", [py, loop / "loop.py", "reconcile-merges",
                                                              ".sdlc"], repo, env).stdout.strip()
        obs["issue_final"] = state()["issues"][goal]["state"]
        obs["pr_final"] = state()["prs"][pr]["state"]
        shown = subprocess.run(["git", "--git-dir", str(remote), "show", f"main:{WORK_FILE}"],
                               capture_output=True, text=True)
        obs["remote_file"] = shown.stdout if shown.returncode == 0 else None
        if variant == "confirm":
            obs["readme_gestures"] = run_readme_gestures(run, qs, sigma, repo, env)
    except Red as red:
        run.failed = red.step
        run.steps.append({"step": "RED", "at": red.step, "detail": red.detail})
    obs["unhandled"] = unhandled.read_text(encoding="utf-8") if unhandled.is_file() else ""
    if run.failed is None:
        run.assertions += (check_github_no_command if variant == "no-command" else check_github)(obs)
        if variant == "confirm":
            run.assertions.append(check_gestures(obs, qs))
    calls = _gh_calls(log_path)
    ev = obs.get("evidence") or {}
    obs["evidence"] = {k: ev.get(k) for k in ("command", "exit", "verify_state")} if ev else None
    return run.result({"variant": variant, "observations": obs, "gh_calls": _summarise_gh(calls),
                       "gh_call_count": len(calls)})


# ------------------------------------------------------------------------------ github/process (#878)

#: A fixed, non-secret test session id: `phase_report.py` looks agent transcripts up under it, and
#: `reviewer.py resolve` only selects the `subagent` route when a Claude session id is present.
PROCESS_SESSION = "00000000-0000-4000-8000-0000000000a1"
PROCESS_AGENTS = ("onb-research", "onb-plan", "onb-plan-reviewer", "onb-implement", "onb-reviewer",
                  "onb-retro")
PROCESS_MODEL = "claude-haiku-4-5-20251001"
PROCESS_TEST = "tests/test_hello.py"
#: The scratch test the control writes into the goal worktree. It asserts on EXISTENCE (an assertion
#: failure, never an error) so the red `loop.py verify` records a witness row, and the SAME bytes go
#: green when `hello.txt` is written: the red comes from the code under test, never from editing the test.
PROCESS_TEST_SRC = ('import pathlib\n\nROOT = pathlib.Path(__file__).resolve().parents[1]\n\n\n'
                    'def test_hello_written():\n    target = ROOT / "hello.txt"\n'
                    '    assert target.is_file() and target.read_text() == "hi\\n"\n')
PROCESS_PLAN = ("# Plan\nscripted\n\n## Tests\n- `tests/test_hello.py::test_hello_written`\n")
_PROPERTY_LINE = re.compile(r"(?m)^(PASS|FAIL|NOT EVALUABLE)\s+(\S+) \[\w+(, hard)?\]")


def _pytest_env():
    """The environment `python3 -m pytest` runs under inside the variant: the control's PATH plus the
    invoking user's site (`PYTHONUSERBASE`), because the variant's fake HOME would otherwise hide a
    pytest installed per-user. Nothing else of the real profile is passed."""
    import site
    return {"PATH": _control_path(), "PYTHONUSERBASE": site.getuserbase()}


def _pytest_precondition(env=None):
    """"" when `python3 -m pytest` runs under the variant's own environment, else the reason. SAFETY:
    the test-first proof needs it, and a variant that quietly skipped the proof would be a weaker
    control wearing the same name."""
    try:
        proc = subprocess.run(["python3", "-m", "pytest", "--version"], capture_output=True, text=True,
                              env=env or _pytest_env(), timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"python3 could not run ({exc.__class__.__name__})"
    return "" if proc.returncode == 0 else "python3 -m pytest is unavailable"


def _write_transcripts(base):
    """One fixture transcript per agent id the variant passes to `--agent-id`, under the control's own
    fake profile (never the real one), shaped as Claude Code writes them; timestamped at write time."""
    from datetime import datetime, timezone
    for agent in PROCESS_AGENTS:
        path = (base / "home" / ".claude" / "projects" / "onboarding" / PROCESS_SESSION / "subagents" /
                f"agent-{agent}.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = []
        for i in range(3):
            rows.append({"type": "assistant",
                         "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                         "message": {"id": f"msg_{agent}_{i}", "role": "assistant", "model": PROCESS_MODEL,
                                     "usage": {"input_tokens": 40000, "output_tokens": 8000,
                                               "cache_read_input_tokens": 90000,
                                               "cache_creation": {"ephemeral_5m_input_tokens": 20000,
                                                                  "ephemeral_1h_input_tokens": 0}}}})
        path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def check_github_process(obs):
    """github/process: the control's own pre-record facts first, then ONE assertion per property line
    `check.py` printed, named EXACTLY the property id and in its print order, so the first failing
    assertion is the first property the evaluator failed. A soft NOT EVALUABLE is not red (check.py's
    own exit rule). Pure, so each assertion can be broken once in a test."""
    probe = str(obs.get("probe_text", ""))
    out = [
        _check("negative probe printed the plan-review refusal",
               "gates.plan_review is on" in probe and "has no recorded review" in probe, probe[-300:]),
        _check("negative probe left the fake store without a PR", obs.get("probe_prs") == {},
               obs.get("probe_prs")),
        _check("PR opened only after plan-review was recorded", bool(obs.get("pr_after_record")),
               obs.get("pr_after_record")),
        _check("review chain posted sigma:approve through post-review",
               "posted sigma:approve" in str(obs.get("post_review_text", "")), obs.get("post_review_text")),
        _check("every gh call was one the fake models", obs.get("unhandled") == "", obs.get("unhandled")),
        _check("record build succeeded", obs.get("build_rc") == 0, obs.get("build_text")),
    ]
    for word, ident, hard in _PROPERTY_LINE.findall(str(obs.get("check_text", ""))):
        out.append(_check(ident, word == "PASS" or (word == "NOT EVALUABLE" and not hard), word))
    if len(out) == 6:
        out.append(_check("check.py printed its property lines", False, str(obs.get("check_text", ""))[-300:]))
    return out


def _edit_config(repo, edits):
    path = repo / ".sdlc" / "config.json"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    for dotted, value in edits.items():
        node = cfg
        *head, leaf = dotted.split(".")
        for part in head:
            node = node.setdefault(part, {})
        node[leaf] = value
    path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")


def run_github_process(sigma, readme_text, root, qs=None):
    """The github/process variant (#878): the confirm world, driven through Sigma's OWN writers end to
    end -- phase boundaries, plan-review, a negative probe, the test-first leg, the review chain with
    `post-review`, merge, done -- then a run record built by `evals/regression/record.py` and judged by
    `evals/regression/check.py`. RED names the first property that failed. The runner is a deliberate
    copy of `run_github`'s world setup rather than a shared helper: moving counts out of `run_github`
    would force re-proving the four variants that already hold."""
    import hashlib
    run = Run("github/process")
    obs = {}
    # Resolved: on macOS a temp dir is spelled through the /var -> /private/var symlink, and pytest then
    # reports the planned node as `::test_hello_written` (no file part), which red_green rejects.
    base = root.resolve() / "github-process"
    state_path, log_path, unhandled = base / "gh_state.json", base / "gh_log.jsonl", base / "gh_unhandled.jsonl"
    try:
        qs = qs or run.timed("readme parse", lambda: parse_quickstart(readme_text, sigma))
        bin_dir, remote, repo = base / "bin", base / "remote.git", base / "repo"
        bin_dir.mkdir(parents=True)
        gh = bin_dir / "gh"
        gh.write_text(f"#!{sys.executable}\n" + _fake_gh_body(sigma), encoding="utf-8")
        gh.chmod(0o755)
        log_path.write_text("", encoding="utf-8")
        unhandled.write_text("", encoding="utf-8")
        state_path.write_text(json.dumps({
            "repo": FAKE_REPO, "remote_git_dir": str(remote), "labels": {}, "label_seq": 0,
            "issues": {}, "issue_seq": 1, "prs": {}, "pr_seq": 100, "projects": [],
            "login": "onboarding-bot", "default_branch": "main", "allow_auto_merge": True,
            "viewer_permission": "ADMIN", "refuse_label_create": False}), encoding="utf-8")
        env = _env(base, bin_dir, {"FAKE_GH_STATE": str(state_path), "FAKE_GH_LOG": str(log_path),
                                   "FAKE_GH_UNHANDLED": str(unhandled),
                                   "CLAUDE_CODE_SESSION_ID": PROCESS_SESSION,
                                   "PYTHONUSERBASE": _pytest_env()["PYTHONUSERBASE"]})
        _git(["init", "-q", "--bare", "-b", "main", str(remote)], base, env)
        _git(["clone", "-q", str(remote), str(repo)], base, env)
        _fresh_files(repo, "process")
        _git(["add", "-A"], repo, env)
        _git(["commit", "-qm", "fresh repository"], repo, env)
        _git(["push", "-q", "-u", "origin", "main"], repo, env)
        init_out, obs["scaffolded_verify"] = _init_and_verify(run, qs, sigma, repo, env, "github", "process")
        _edit_config(repo, {"journal.enabled": True, "gates.plan_review.enabled": True,
                            "action_log.enabled": True, "work.require_review": "approval",
                            "work.auto_merge": "always"})
        _write_transcripts(base)
        py = sys.executable
        loop = pathlib.Path(sigma) / "skills" / "sigma-loop" / "scripts"

        def state():
            return json.loads(state_path.read_text(encoding="utf-8"))

        def script(name, *args, step=None, ok_rc=(0,)):
            return run.step(step or f"{name} {args[0]}", [py, loop / f"{name}.py", *args], repo, env,
                            ok_rc=ok_rc)

        def phase(name, agent=None, goal="1"):
            script("phase_report", "start", ".sdlc", goal, name, "--model", "haiku", "--pid", pid,
                   step=f"phase_report start {name}")
            return lambda: script("phase_report", "end", ".sdlc", goal, name, "--pid", pid,
                                  *(["--agent-id", agent] if agent else []), step=f"phase_report end {name}")

        run.step("file goal (gh issue create, labelled sdlc:goal, assigned @me)",
                 [gh, "issue", "create", "--repo", FAKE_REPO, "--label", "sdlc:goal", "--assignee", "@me",
                  "--title", GOAL_TITLE, "--body", f"Create {WORK_FILE} with one line.\n\n"
                  f"## Done when\n- [ ] {WORK_FILE} contains hi.\n"
                  "- [ ] Verification passes when configured.\n"
                  "- [ ] The goal can be recorded done after its PR merges.\n"], repo, env)
        pid = str(os.getpid())
        script("loop", "start", ".sdlc", "--session-pid", pid, step="loop start")
        nxt = _py_argv(_loop_next_line(init_out), sigma, {}, step="loop next", cwd=repo) + \
            ["--session-pid", pid]
        goal = run.step("loop next", nxt, repo, env).stdout.strip()
        if goal != "1":
            raise Red("loop next", f"expected issue 1, got {goal!r}")
        script("loop", "agent-start", ".sdlc", goal, "--pid", pid, step="agent-start")
        script("work", "start", ".sdlc", goal, "--session-pid", pid, step="work start")
        capture = ["record", ".sdlc", goal]
        if (_cfg(repo).get("verify") or {}).get("command"):
            capture += ["--verify-command", "test -s " + WORK_FILE]
        script("acceptance", *capture, step="record acceptance")
        wt = repo / ".sdlc" / "work" / goal
        acceptance = repo / ".sdlc" / "acceptance" / (goal + ".md")
        (wt / ".sdlc" / "acceptance").mkdir(parents=True, exist_ok=True)
        shutil.copy2(acceptance, wt / ".sdlc" / "acceptance" / acceptance.name)
        # research, plan, plan-review (the verdict is NOT recorded yet: the probe needs it absent)
        (repo / ".sdlc" / "research").mkdir(parents=True, exist_ok=True)
        (repo / ".sdlc" / "plans").mkdir(parents=True, exist_ok=True)
        (repo / ".sdlc" / "research" / f"{goal}.md").write_text("# Research\nscripted\n", encoding="utf-8")
        phase("research", "onb-research")()
        plan = repo / ".sdlc" / "plans" / f"{goal}.md"
        plan.write_text(PROCESS_PLAN, encoding="utf-8")
        phase("plan", "onb-plan")()
        phase("plan_review", "onb-plan-reviewer")()
        for sub in ("research", "plans"):
            (wt / ".sdlc" / sub).mkdir(parents=True, exist_ok=True)
            shutil.copy2(repo / ".sdlc" / sub / f"{goal}.md", wt / ".sdlc" / sub / f"{goal}.md")
        # negative probe: a branch carrying its plan and research but NO recorded plan-review must be
        # refused, naming the gate (the earlier pr() guards need a clean tree and the plan committed)
        script("work", "commit", ".sdlc", goal, "--message", "sdlc: plan and research",
               step="work commit (plan and research)")
        probe = script("work", "pr", ".sdlc", goal, step="work pr (negative probe)", ok_rc=None)
        obs["probe_text"] = probe.stdout + probe.stderr
        obs["probe_prs"] = state()["prs"]
        sha = hashlib.sha256(plan.read_bytes()).hexdigest()
        script("work", "record-plan-review", ".sdlc", goal, "--verdict", "SOUND", "--plan-sha256", sha,
               "--agent-id", "onb-plan-reviewer", step="record plan-review")
        # test-first leg: the test exists, hello.txt does not -> RED; then the work -> GREEN
        (wt / "tests").mkdir(exist_ok=True)
        (wt / PROCESS_TEST).write_text(PROCESS_TEST_SRC, encoding="utf-8")
        implement_end = phase("implement", "onb-implement")
        red = script("loop", "verify", ".sdlc", goal, step="loop verify (red)", ok_rc=None)
        obs["verify_red_rc"] = red.returncode
        (wt / WORK_FILE).write_text("hi\n", encoding="utf-8")
        end = implement_end()
        obs["cost_line"] = next((l for l in end.stdout.splitlines() if "cost" in l), "")
        obs["verify_rc"] = script("loop", "verify", ".sdlc", goal, step="loop verify (green)",
                                  ok_rc=None).returncode
        ev = repo / ".sdlc" / "state" / "verify" / f"{goal}.json"
        obs["evidence"] = json.loads(ev.read_text(encoding="utf-8")) if ev.is_file() else None
        script("work", "commit", ".sdlc", goal, "--message", f"sdlc: {GOAL_TITLE.lower()}",
               step="work commit")
        script("work", "pr", ".sdlc", goal, step="work pr")
        prs = state()["prs"]
        pr = next(iter(prs), None)
        if pr is None:
            raise Red("work pr", "no PR was opened")
        obs["pr_after_record"] = len(prs) == 1
        # the review chain: generation -> resolver -> reviewer -> bound evidence -> post-review
        sdlc = repo / ".sdlc"
        manifest = sdlc / "state" / "review-manifests" / f"{goal}.json"
        script("review_context", "brief", ".sdlc", goal, "--for", "pr-review", "--artifact", f"PR#{pr}",
               "--output", str(manifest), step="review brief")
        resolution = sdlc / "state" / "review-resolution.json"
        resolution.write_text(script("reviewer", "resolve", ".sdlc", step="reviewer resolve").stdout,
                              encoding="utf-8")
        paths = script("work", "review-paths", ".sdlc", goal, "--manifest", str(manifest), "--format", "sh",
                       step="review paths").stdout
        vars_ = dict(tok.split("=", 1) for tok in shlex.split(paths) if "=" in tok)
        phase("review", "onb-reviewer")()
        script("work", "record-subagent-review", ".sdlc", goal, "--manifest", str(manifest), "--resolution",
               str(resolution), "--verdict", "approve", "--reason", "scripted review of the fixture diff",
               "--agent-id", "onb-reviewer", step="record subagent review")
        script("work", "record-review", ".sdlc", goal, "--verdict", "APPROVE", "--agent-id", "onb-reviewer",
               step="record review")
        evidence = json.loads(script("work", "review-evidence", ".sdlc", goal, "--manifest", str(manifest),
                                     "--review-result", vars_["REVIEW_RESULT"],
                                     step="review evidence").stdout)["path"]
        obs["post_review_text"] = script("work", "post-review", ".sdlc", goal, "--verdict", "approve",
                                         "--evidence", evidence, step="post-review").stdout.strip()
        phase("retro", "onb-retro")()                  # the merge gate demands all seven phase records
        merge = script("work", "merge", ".sdlc", goal, step="work merge", ok_rc=None)
        obs["merge_text"], obs["merge_rc"] = merge.stdout.strip(), merge.returncode
        rec = script("loop", "reconcile-merges", ".sdlc", step="loop reconcile-merges", ok_rc=None)
        obs["reconcile_text"] = rec.stdout.strip()
        # the record and the evaluator, on the builder's own run-dir layout (<base> holds repo/)
        record_py = pathlib.Path(sigma) / "evals" / "regression" / "record.py"
        check_py = pathlib.Path(sigma) / "evals" / "regression" / "check.py"
        build = run.step("record build", [py, record_py, "build", base, "--goal", goal], repo, env, ok_rc=None)
        obs["build_rc"], obs["build_text"] = build.returncode, (build.stdout + build.stderr)[-400:]
        if build.returncode != 0:
            raise Red("record build", obs["build_text"])
        verdict = run.step("check", [py, check_py, base / "record.json",
                                     pathlib.Path(sigma) / "evals" / "regression" / "properties.json"],
                           repo, env, ok_rc=None)
        obs["check_text"], obs["check_rc"] = verdict.stdout, verdict.returncode
    except Red as red:
        run.failed = red.step
        run.steps.append({"step": "RED", "at": red.step, "detail": red.detail})
    obs["unhandled"] = unhandled.read_text(encoding="utf-8") if unhandled.is_file() else ""
    if run.failed is None:
        run.assertions += check_github_process(obs)
    calls = _gh_calls(log_path)
    ev = obs.get("evidence") or {}
    obs["evidence"] = {k: ev.get(k) for k in ("command", "exit", "verify_state")} if ev else None
    return run.result({"variant": "process", "observations": obs, "gh_calls": _summarise_gh(calls),
                       "gh_call_count": len(calls)})


# ------------------------------------------------------------------------------ host install

def _surface_hash(home):
    """A hash of the real profiles' plugin surface: every file's relative path, size and mtime
    under ~/.claude/plugins, ~/.claude/settings.json and ~/.codex/{config.toml,plugins}. Session
    logs elsewhere under ~/.claude churn constantly and are not what a plugin install writes."""
    h = hashlib.sha256()
    for rel in (".claude/plugins", ".claude/settings.json", ".codex/config.toml", ".codex/plugins"):
        p = pathlib.Path(home) / rel
        files = [p] if p.is_file() else sorted(x for x in p.rglob("*") if x.is_file()) if p.is_dir() else []
        for f in files:
            try:
                st = f.stat()
            except OSError:
                continue
            h.update(f"{f.relative_to(home)}|{st.st_size}|{st.st_mtime_ns}\n".encode())
    return h.hexdigest()[:16]


def _codex_bin():
    found = shutil.which("codex")
    bundled = pathlib.Path("/Applications/ChatGPT.app/Contents/Resources/codex")
    return found or (str(bundled) if bundled.is_file() else None)


def host_install(qs, sigma, root, which):
    """The README's CLI install lines, run into an isolated profile. Read-only detection first:
    a host CLI that is absent is `skipped`, never green."""
    real_home = os.path.expanduser("~")
    before = _surface_hash(real_home)
    out = {"real_profile_surface_hash_before": before}
    iso = root / "iso"
    for host, lines, binary in (("claude", qs["claude_install"], shutil.which("claude")),
                                ("codex", qs["codex_install"], _codex_bin())):
        if which not in (host, "all"):
            continue
        if not binary:
            out[host] = {"status": "skipped", "why": f"no `{host}` CLI on this machine"}
            continue
        home, cfg = iso / host / "home", iso / host / "cfg"
        home.mkdir(parents=True)
        cfg.mkdir(parents=True)
        env = dict(os.environ, HOME=str(home), CLAUDE_CONFIG_DIR=str(cfg), CODEX_HOME=str(cfg))
        rec = {"binary": binary, "steps": []}
        for line in lines:
            argv = [binary] + [t.replace(public_url(sigma), str(sigma)) for t in shlex.split(line)[1:]]
            t = time.monotonic()
            proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=300)
            rec["steps"].append({"line": line, "rc": proc.returncode,
                                 "seconds": round(time.monotonic() - t, 3),
                                 "tail": (proc.stdout + proc.stderr).strip()[-300:]})
        # <plugin root>/skills/sigma-init/scripts/init_flow.py, in the host's plugin cache
        installed = sorted(str(p.parents[3]) for p in cfg.rglob("init_flow.py")
                           if "cache" in p.parts)
        rec["installed_path"] = installed[0] if installed else None
        rec["status"] = "ok" if installed and all(s["rc"] == 0 for s in rec["steps"]) else "red"
        out[host] = rec
    out["real_profile_surface_hash_after"] = _surface_hash(real_home)
    out["real_profile_untouched"] = out["real_profile_surface_hash_after"] == before
    return out


# ------------------------------------------------------------------------------ main

def main(argv):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--mode", choices=("local", "github", "both"), default="both")
    ap.add_argument("--variant", choices=VARIANTS + ("all",), default="all",
                    help="confirm: a Makefile test target, confirmed by the README gesture; "
                         "no-command: nothing to confirm, the question left open; "
                         "process: github only, drives Sigma's own writers and checks a run record "
                         "(default: all)")
    ap.add_argument("--sigma", default=str(ROOT), help="the Sigma checkout to run (default: this one)")
    ap.add_argument("--readme", help="the README to follow (default: <sigma>/README.md)")
    ap.add_argument("--install", choices=("none", "claude", "codex", "all"), default="none")
    ap.add_argument("--from-install", action="store_true",
                    help="run the scripts from the isolated Claude Code install, not --sigma")
    ap.add_argument("--json", help="write the result JSON here (default: stdout only)")
    ap.add_argument("--workdir", help="parent for the temp directory")
    ap.add_argument("--keep", action="store_true", help="keep the temp directory")
    args = ap.parse_args(argv[1:])
    if os.name == "nt":
        print("onboarding_control: POSIX only (a make test target, a #! fake gh)", file=sys.stderr)
        return 2
    for tool in ("git", "make"):
        if not shutil.which(tool):
            print(f"onboarding_control: precondition missing: `{tool}` not on PATH", file=sys.stderr)
            return 2
    wants_process = args.variant in ("all", "process") and args.mode in ("github", "both")
    if args.variant == "process" and args.mode == "local":
        print("onboarding_control: --variant process is github-only; use --mode github or --mode both",
              file=sys.stderr)
        return 2
    if wants_process:
        missing = _pytest_precondition()
        if missing:
            print(f"onboarding_control: precondition missing: {missing} (the github/process variant "
                  "needs it for the test-first proof); install pytest or run --variant confirm",
                  file=sys.stderr)
            return 2
    sigma = pathlib.Path(args.sigma).resolve()
    readme = pathlib.Path(args.readme or sigma / "README.md")
    try:
        text = readme.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"onboarding_control: precondition missing: cannot read the README to follow "
              f"({readme}): {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 2
    root = pathlib.Path(tempfile.mkdtemp(prefix="sigma-onboarding-", dir=args.workdir))
    result = {"schema": SCHEMA, "sigma": str(sigma), "readme": str(readme),
              "python": sys.version.split()[0], "platform": sys.platform,
              "tokens": "N/A: script-level control, no model session ran",
              "workdir": str(root), "modes": {}}
    t0 = time.monotonic()
    try:
        try:
            qs = parse_quickstart(text, sigma)
            result["readme_gestures"] = qs
        except Red as red:
            result["modes"]["readme"] = {"ok": False, "failed_step": red.step, "detail": red.detail}
            qs = None
        run_from = sigma
        if qs and args.install != "none":
            result["host_install"] = host_install(qs, sigma, root, args.install)
            inst = (result["host_install"].get("claude") or {}).get("installed_path")
            if args.from_install:
                if not inst:
                    result["modes"]["install"] = {"ok": False, "failed_step": "install",
                                                  "detail": "--from-install but no Claude Code install"}
                    qs = None
                else:
                    run_from = pathlib.Path(inst)
        result["scripts_from"] = str(run_from)
        result["modes"]["readme-usage"] = check_readme_usage(usage_corpus(text, sigma), run_from, root)
        if qs:
            for variant in VARIANTS if args.variant == "all" else (args.variant,):
                tag = "" if variant == "confirm" else "/" + variant
                if variant != "process" and args.mode in ("local", "both"):
                    result["modes"]["local" + tag] = run_local(run_from, text, root, qs, variant)
                if variant == "process" and args.mode in ("github", "both"):
                    result["modes"]["github" + tag] = run_github_process(run_from, text, root, qs)
                elif variant != "process" and args.mode in ("github", "both"):
                    result["modes"]["github" + tag] = run_github(run_from, text, root, qs, variant)
    finally:
        result["seconds"] = round(time.monotonic() - t0, 3)
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)
    hi = result.get("host_install") or {}
    result["ok"] = bool(result["modes"]) and all(m.get("ok") for m in result["modes"].values()) and \
        all((hi.get(h) or {}).get("status", "ok") in ("ok", "skipped") for h in ("claude", "codex")) and \
        hi.get("real_profile_untouched", True)
    blob = json.dumps(result, indent=2)
    if args.json:
        pathlib.Path(args.json).write_text(blob + "\n", encoding="utf-8")
    for name, m in result["modes"].items():
        print(f"onboarding-control: {name}: {'GREEN' if m.get('ok') else 'RED at ' + str(m.get('failed_step'))}"
              f" ({m.get('seconds', 0)}s)")
    for h in ("claude", "codex"):
        if h in hi:
            print(f"onboarding-control: install {h}: {hi[h].get('status')}")
    if hi:
        print(f"onboarding-control: real profile untouched: {hi.get('real_profile_untouched')}")
    if not args.json:
        print(blob)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
