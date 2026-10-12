#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""The ONE entry point a new user runs after installing Sigma (#236): `/sigma-init` (and its alias
`/sigma-setup`) run this, on every host.

WHY. Before #236 there were three doors: `sdlc_init.py` wrote files and asked nothing, `/sigma-setup`
adopted GitHub by model judgment around `setup.py` (and set github mode with no repository), and the
wizard was a Claude-Code-only session hook. `sdlc_init.py --github` created labels on GitHub but left
`discovery.source: local-goals`, so the loop never read the issues it had just made pickable -- and
the first thing it picked on every fresh repository was the scaffolded "Example goal".

WHAT. One flow, in this order, integrating the sibling goals rather than re-implementing them:
  1. preflight (#229, `preflight.py`)          git / remote / base / gh / auth / scopes
  2. mode                                      local-goals or github (github by default when `origin`
                                               is a GitHub owner/repo)
  3. verify command (#228, `verify_detect.py`) confirm by number+id, your own command, or decline
  4. github mode only: labels (#230, `setup.py labels`), assignee `@me`, the board OFFER (#235,
     `board_setup.py create`), and the ledger yes/no
  5. a one-screen summary and the next command

QUESTIONS ARE FLAGS, SO EVERY HOST RUNS THE SAME FLOW. Claude Code asks the user and re-runs with the
answers; Codex and Cursor relay the printed `[ask]` lines, each carrying the exact flag (the shape,
`[ask] <id>: <prose> -> --flag VALUE|VALUE ; --flag ...`, is `ask_line`'s).

CONFIG.JSON IS THE ONE SOURCE OF TRUTH FOR CURRENT STATE (review of PR #286). `.sdlc/state/init.json`
(runtime, git-ignored) records only THAT a question was answered -- so a re-run does not ask it again
-- and never re-applies a value over config.json: a user who later runs `preflight.py local-only` or
edits config.json by hand keeps that choice on every re-run, and the flow says so ("kept ... config.json
wins"). A consequential change -- work off/on, the ledger on, a board created, the source switched --
happens only from a flag on the CURRENT run (or `--yes`, below, for a question nothing has answered).
A remembered repository is never used: `--repo`, else config.json's, else the current `origin`.

`--yes` ANSWERS ONLY WHAT IS SAFE TO DEFAULT AND STILL OPEN: the mode (the detected one) and the ledger
(no), and only where neither a flag nor config.json already answers it -- on a repository configured
before this flow existed it changes nothing and says "kept". A key is open when config.json does not
carry it, or still carries the template value this flow scaffolded AND config.json is byte-for-byte,
mtime-for-mtime what this flow last wrote (the fingerprint in init.json -- see `load_memory`). `--yes`
never answers the board (creates external state -- #235 "never unasked"), the verify command (only the
user knows what proves their repo -- #228), or a `work.enabled` flip (#229 "nothing flips it silently").

EXIT: 0 = every attempted step passed (open `[ask]` questions allowed); 1 = a step FAILED, or preflight
found a blocking problem -- the last line is `Resume: <the exact command>`; 2 = refused before anything
was written (not a git repository, another plugin active, github mode with no repository, bad flags).

Stdlib only. Siblings are loaded by path (the `sdlc_init._preflight` idiom); the sigma-setup and
board CLIs are shelled out to, as `sdlc_init.py` already does.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys

_HERE = pathlib.Path(__file__).resolve().parent
SETUP_SCRIPT = _HERE.parent.parent / "sigma-setup" / "scripts" / "setup.py"
BOARD_SETUP = _HERE / "board_setup.py"
BOARD_LAYOUT = _HERE / "board_layout.py"
LOOP_SCRIPT = _HERE.parent.parent / "sigma-loop" / "scripts" / "loop.py"
SYNC_SCRIPT = _HERE.parent.parent / "sigma-loop" / "scripts" / "sync.py"
ANSWERS = pathlib.Path("state") / "init.json"
MODES = ("local-goals", "github")
#: The questions whose answer lives in config.json, and the values each may take (a remembered value
#: outside these is ignored -- init.json is runtime state, never trusted as config).
ANSWER_VALUES = {"mode": MODES, "work": ("on", "off"), "ledger": ("yes", "no"), "board": ("yes", "no")}
CONFIG_KEYS = ("mode", "work", "ledger")
#: GitHub's owner (login: alphanumerics and single hyphens, <= 39) / repository name shape.
_REPO_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")
#: Characters no single quoting survives in BOTH cmd.exe and PowerShell -- the same set, and the same
#: refusal, as `board_setup._WIN_UNSAFE`: `"` ends the quote, `%` (cmd) and `$` / backtick
#: (PowerShell) expand inside double quotes, `!` does under cmd's delayed expansion.
_WIN_UNSAFE = frozenset('"%$`!')

USAGE = """usage: init_flow.py [target_dir] [options]
  --mode local-goals|github      the backlog: goal files in .sdlc/goals/, or GitHub issues
  --repo OWNER/NAME              github mode's repository (default: config.json's, else `origin`)
  --local-only                   work.enabled off: the loop edits this checkout (no worktree/PR)
  --work on                      work.enabled on: a worktree + branch + PR per goal
  --verify N:ID                  confirm detected candidate N (the id printed beside it)
  --verify-command-file FILE     your own verify command, one line in FILE
  --no-verify                    decline: verify.enforce stays off, the reason is recorded
  --board yes|no                 github mode: create + pin a Projects board (yes), or decline
  --ledger yes|no                github mode: the team ledger on or off
  --yes                          accept the safe defaults (mode = detected, ledger = no) for
                                 questions config.json does not already answer; never answers
                                 --board, --verify or a work flip, never changes a setting
  --demo --vision --codex --cursor   the opt-in scaffolds (see sdlc_init.py)
  --github-templates             also copy the .github/ issue templates + workflows
  --github                       shorthand: --mode github --github-templates (the pre-#236 flag)
  --ignore-scope tracked|local   where the runtime dirs are git-ignored (default tracked)
/sigma-init runs this; /sigma-setup is an alias (`setup.py init ...`)."""

_VALUE = {"--mode", "--repo", "--work", "--verify", "--verify-command-file", "--board", "--ledger",
          "--ignore-scope"}
_BOOL = {"--local-only", "--no-verify", "--yes", "--demo", "--vision", "--codex", "--cursor",
         "--github-templates", "--github"}

#: The start of board_setup.py's success-path note that mirroring is off (`create`); dropped from
#: this flow's output when `--board yes` turns mirroring on right after it.
BOARD_NOT_ENABLED_NOTE = "note: discovery.github.project.enabled is not true"

_KEY = "sigma.allowRepositoryShellCommands"

#: Test seam: `board_step`'s runner, `(argv) -> (rc, output)`. None = a real subprocess.
BOARD_RUNNER = None


def _load(name, path=None):
    spec = importlib.util.spec_from_file_location("init_flow_" + name, str(path or _HERE / f"{name}.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_si = _load("sdlc_init")
_pf = _si._preflight()
_vd = _si._verify_detect()
_sp = _load("shell_policy", _HERE.parent.parent / "sigma-loop" / "scripts" / "shell_policy.py")


def parse(argv):
    """-> (opts dict, target, error). Unknown or malformed flags are an error, never ignored."""
    opts, pos, i = {}, [], 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            opts["help"] = True
        elif a in _BOOL:
            opts[a[2:]] = True
        elif a in _VALUE:
            if i + 1 >= len(argv):
                return opts, None, f"{a} needs a value"
            opts[a[2:]] = argv[i + 1]
            i += 1
        elif a.startswith("--"):
            return opts, None, f"unknown option {a}"
        else:
            pos.append(a)
        i += 1
    if opts.pop("github", False):
        # the pre-#236 `/sigma-init --github` meant "GitHub": now it also SAYS github mode
        if opts.get("mode", "github") != "github":
            return opts, None, "--github contradicts --mode " + opts["mode"]
        opts["mode"], opts["github-templates"] = "github", True
    for key, allowed in (("mode", MODES), ("board", ("yes", "no")), ("ledger", ("yes", "no")),
                         ("work", ("on",)), ("ignore-scope", ("tracked", "local"))):
        if key in opts and opts[key] not in allowed:
            return opts, None, f"--{key} takes one of: {' | '.join(allowed)}"
    if "repo" in opts and (not _REPO_RE.fullmatch(opts["repo"])
                           or opts["repo"].split("/")[1] in (".", "..")
                           or opts["repo"].lower().endswith(".git")):
        return opts, None, ("--repo takes OWNER/NAME (e.g. acme/app), not a URL, a path, or a name "
                            "ending .git")
    if "verify" in opts and ":" not in opts["verify"]:
        return opts, None, "--verify takes N:ID, exactly as printed beside a candidate"
    if sum(k in opts for k in ("verify", "verify-command-file", "no-verify")) > 1:
        return opts, None, "choose one of --verify, --verify-command-file, --no-verify"
    if opts.get("local-only") and opts.get("work"):
        return opts, None, "choose one of --local-only, --work on"
    if len(pos) > 1:
        return opts, None, "one target directory at most"
    return opts, (pos[0] if pos else "."), None


def detect_github_repo(target):
    """`owner/name` when `origin` is a GitHub-shaped remote (never a gitlab/bitbucket host), else "".
    Local: one `git remote get-url`; no network."""
    rc, url = _pf.real_runner(["git", "remote", "get-url", "origin"], str(target), _pf.call_timeout())
    if rc != 0:
        return ""
    host, owner, name = _pf.parse_remote_url(url.strip())
    if not host or _pf.is_non_github(host):
        return ""
    return f"{owner}/{name}"


def config_answers(cfg):
    """The questions config.json ALREADY answers, in answer vocabulary: {"mode": source, "work":
    on|off, "ledger": yes|no}. A key it does not carry -- or carries as null -- is absent (unset)."""
    out = {}
    disc = cfg.get("discovery") if isinstance(cfg.get("discovery"), dict) else {}
    if disc.get("source") in MODES:
        out["mode"] = disc["source"]
    work = cfg.get("work") if isinstance(cfg.get("work"), dict) else {}
    if isinstance(work.get("enabled"), bool):
        out["work"] = "on" if work["enabled"] else "off"
    ledger = cfg.get("ledger") if isinstance(cfg.get("ledger"), dict) else {}
    if isinstance(ledger.get("enabled"), bool):
        out["ledger"] = "yes" if ledger["enabled"] else "no"
    return out


def config_fingerprint(path):
    """{"sha256": <hex of config.json's exact bytes>, "mtime_ns": <its mtime>}, or None when it
    cannot be read. The mtime is part of it so that a write leaving the very same bytes (a revert,
    `setup.py configure` re-saving an identical file) still counts as a write: a re-save is a
    decision, and nothing here may read it as "untouched"."""
    try:
        path = pathlib.Path(path)
        data, st = path.read_bytes(), path.stat()
    except OSError:
        return None
    return {"sha256": hashlib.sha256(data).hexdigest(), "mtime_ns": st.st_mtime_ns}


def load_memory(sdlc):
    """-> (answered, scaffolded) from init.json: which questions were answered (and what was said,
    for the "config.json wins" note only), and the template values this flow scaffolded for keys no
    one has answered yet. Values outside `ANSWER_VALUES` are dropped; a remembered repo is ignored.
    Reads the pre-review flat format ({"mode": ..., "board": ...}) as `answered`.

    Review of PR #286, block #2: a value equal to the template's is NOT evidence nobody chose it
    (`setup.py configure --source local-goals`, a hand edit, `preflight.py local-only`, board_setup
    pinning -- all can leave exactly the template value). So `scaffolded` is returned ONLY while
    config.json still fingerprints to exactly what init.json recorded when this flow last wrote it;
    ANY other write to config.json, by any tool or hand, makes every key it carries explicit.
    No init.json, an unreadable/invalid one, one with no fingerprint, or no config.json: nothing is
    open (never guessed). A key config.json does not carry (absent or null) is still unanswered."""
    raw = _read_json(pathlib.Path(sdlc) / ANSWERS)
    if isinstance(raw.get("answered"), dict) or isinstance(raw.get("scaffolded"), dict):
        answered, scaffolded = raw.get("answered") or {}, raw.get("scaffolded") or {}
    else:
        answered, scaffolded = raw, {}
    recorded = raw.get("config")
    if not isinstance(recorded, dict) or recorded != config_fingerprint(pathlib.Path(sdlc) / "config.json"):
        scaffolded = {}

    def valid(d):
        if not isinstance(d, dict):
            return {}
        return {k: v for k, v in d.items() if k in ANSWER_VALUES and v in ANSWER_VALUES[k]}
    return valid(answered), valid(scaffolded)


def resolve_answers(opts, answered, scaffolded, current, detected_mode):
    """-> {key: (value, how)} for mode / work / ledger, and board. `how`:
      "flag"    a flag on THIS run -- the only thing that changes an existing setting
      "default" `--yes` took the safe default for a question nothing had answered
      "kept"    config.json already answers it (a value is never re-applied over it)
      "open"    unanswered -- asked
    `current` is `config_answers(config.json)` before this run."""
    yes = bool(opts.get("yes"))
    flags = {"mode": opts.get("mode"), "ledger": opts.get("ledger"),
             "work": "off" if opts.get("local-only") else opts.get("work")}
    defaults = {"mode": detected_mode, "ledger": "no"}
    out = {}
    for key in CONFIG_KEYS:
        cur = current.get(key)
        if flags[key]:
            out[key] = (flags[key], "flag")
        elif key in answered or (cur is not None and scaffolded.get(key) != cur):
            out[key] = (cur, "kept")
        elif yes and key in defaults:
            out[key] = (defaults[key], "default")
        else:
            out[key] = (cur, "open")
    if opts.get("board"):
        out["board"] = (opts["board"], "flag")
    else:
        out["board"] = (answered.get("board"), "kept" if "board" in answered else "open")
    return out


def _read_json(path):
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


class ConfigUnreadable(Exception):
    """config.json exists but is not a JSON object: writing a fresh one would destroy the user's keys."""


def read_config_strict(path):
    """-> the config dict; `{}` ONLY when the file is absent. Raises ConfigUnreadable (with the lever)
    for a present file that cannot be read, parsed, or is not a JSON object (#625), so no writer of
    config.json can replace a corrupt file with a fresh one."""
    path = pathlib.Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:                 # UnicodeDecodeError is a ValueError
        problem = "cannot be read (%s)" % exc.__class__.__name__
    else:
        try:
            data = json.loads(text)
        except ValueError as exc:
            problem = "is not valid JSON (%s; truncated or hand-edited?)" % exc
        else:
            if isinstance(data, dict):
                return data
            problem = "is JSON but not an object (top level is %s)" % type(data).__name__
    raise ConfigUnreadable(
        f"{path} {problem}. Nothing was written and the file is untouched. Fix it: repair the JSON "
        f"by hand (your keys are still in the file), or back it up (`cp {path.name} {path.name}.bak`) "
        f"and delete it on purpose so /sigma-init writes a fresh one.")


def _write_json(path, data):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _vd._atomic_write_json(path, data)


def _child(d, key):
    if not isinstance(d.get(key), dict):
        d[key] = {}
    return d[key]


def _project(cfg):
    disc = cfg.get("discovery") if isinstance(cfg.get("discovery"), dict) else {}
    gh = disc.get("github") if isinstance(disc.get("github"), dict) else {}
    return gh.get("project") if isinstance(gh.get("project"), dict) else {}


def apply_config(sdlc, res, repo, repo_flag=False):
    """Write into config.json ONLY what a flag (or `--yes`, for an open question) decided on THIS
    run; -> the lines saying what the mode is. A key config.json already answers ("kept") is never
    rewritten, and a value already equal is not rewritten either (idempotent)."""
    path = pathlib.Path(sdlc) / "config.json"
    cfg = read_config_strict(path)                      # #625: raises on a corrupt/non-object file
    before = json.dumps(cfg, sort_keys=True)
    lines = []
    disc = _child(cfg, "discovery")
    was_github = disc.get("source") == "github"
    mode, how = res["mode"]
    if how in ("flag", "default"):
        disc["source"] = mode
    kept = " (kept from config.json; --mode %s changes it)"
    if disc.get("source") == "github":
        gh = _child(disc, "github")
        if repo and (repo_flag or not str(gh.get("repo") or "").strip()):
            gh["repo"] = repo                            # a flag, or filling an empty key -- never a swap
        for key, value in (("goal_label", "sdlc:goal"), ("in_progress_label", "sdlc:in-progress"),
                           ("parked_label", "sdlc:parked")):
            gh.setdefault(key, value)
        if gh.get("assignee") is None:                 # never clobber a real choice (setup.py #2255)
            gh["assignee"] = "@me"
        # The loop FINDS OR CREATES a board on its first github-mode pick while `project.enabled`
        # is on (the template ships it on). So when THIS run switches the source to github, an
        # unpinned board is switched off, with the reason -- otherwise "no board without a yes"
        # would hold for init and then break at the first `loop.py next`. `--board yes` turns it on
        # only after board_setup.py pinned a board (`board_step`). A pinned board is the user's.
        proj = _child(gh, "project")
        board, board_how = res["board"]
        if board_how == "flag" and board == "no":
            proj["enabled"] = False
            proj["_enabled_why"] = ("off: declined at /sigma-init (--board no); /sigma-init --board yes "
                                    "creates and pins a board and turns this on")
        elif not was_github and not proj.get("number"):
            proj["enabled"] = False
            proj["_enabled_why"] = ("off until you say yes: /sigma-init --board yes creates and pins "
                                    "a board (the loop would otherwise create one on its first pick)")
        lines.append(f"  [ok] mode: github - issues labelled sdlc:goal on {gh.get('repo')}, "
                     f"assignee {gh['assignee']}" + (kept % "local-goals" if how == "kept" else ""))
    elif disc.get("source") == "local-goals" and how != "open":
        lines.append("  [ok] mode: local-goals - goal files in .sdlc/goals/"
                     + (kept % "github" if how == "kept" else ""))
    work = _child(cfg, "work")
    value, how = res["work"]
    if how == "flag" and value == "off":
        work["enabled"] = False
        work["_enabled_why"] = ("set false at /sigma-init (--local-only): the loop edits this "
                                "checkout directly -- no worktree, branch, push or PR.")
        lines.append("  [ok] work: local-only (the loop edits this checkout; no worktree, branch or PR)")
    elif how == "flag" and value == "on":
        work["enabled"] = True
        work["_enabled_why"] = "set true at /sigma-init (--work on): a worktree + branch + PR per goal."
        lines.append("  [ok] work: a worktree + branch + PR per goal")
    value, how = res["ledger"]
    if how in ("flag", "default"):
        _child(cfg, "ledger")["enabled"] = value == "yes"
    if json.dumps(cfg, sort_keys=True) != before:
        _write_json(path, cfg)
    return lines


def _set_project(sdlc, **values):
    """Set (value) or remove (None) keys of discovery.github.project; never writes a config.json it
    could not read (a `{}` from an unreadable file would otherwise replace the whole config)."""
    path = pathlib.Path(sdlc) / "config.json"
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(cfg, dict):
        return
    proj = _child(_child(_child(cfg, "discovery"), "github"), "project")
    before = json.dumps(proj, sort_keys=True)
    for key, value in values.items():
        if value is None:
            proj.pop(key, None)
        else:
            proj[key] = value
    if json.dumps(proj, sort_keys=True) != before:
        _write_json(path, cfg)


def board_step(target, sdlc, answer, repo, how="flag"):
    """#235, github mode only. -> (lines, ok). Only a `--board yes` FLAG on this run runs
    `board_setup.py create <sdlc> --yes` -- the one path that creates (or, for a pinned board,
    verifies: it refuses a pinned number the owner does not have) a board -- and only its success
    turns `project.enabled` on. Nothing here writes `enabled` before or on a failure (and
    board_setup.py never writes it -- only number/owner/columns), so a failure leaves it exactly as
    it was, with nothing to restore. A remembered answer never runs anything: it only stops the
    OFFER being printed again."""
    proj = _project(_read_json(pathlib.Path(sdlc) / "config.json"))
    number = proj.get("number")
    cmd = [_vd.python_command(), str(BOARD_SETUP), "create", os.path.abspath(str(sdlc))]
    if how == "flag" and answer == "yes":
        prev_enabled = proj.get("enabled")
        rc, out = (BOARD_RUNNER or _run_board)(cmd + ["--yes"])
        lines = ["  " + l for l in (out or "").splitlines()]
        pinned = _project(_read_json(pathlib.Path(sdlc) / "config.json")).get("number")
        if rc == 0 and pinned:
            _set_project(sdlc, enabled=True, _enabled_why=None)
            # board_setup's own "enabled is not true" note is true when it prints and false one line
            # later, when this turns it on: not shown in this flow.
            lines = [l for l in lines if BOARD_NOT_ENABLED_NOTE not in l]
            return (lines + [f"  [ok] board: project #{pinned} pinned and reachable - mirroring on"]
                    + layout_lines(sdlc)), True
        why = (f"board_setup.py exited {rc} (its resume command is above)" if rc
               else "board_setup.py pinned no board")
        return lines + [f"  [FAIL] board: {why}; project.enabled left as it was ({prev_enabled})"], False
    if how == "flag" and answer == "no":
        also = f"; project #{number} stays pinned" if number else ""
        return [f"  [ok] board: declined - mirroring off{also} (pin one later: --board yes)"], True
    if number:
        state = "on" if proj.get("enabled") else "off (--board yes turns it on)"
        return [f"  [ok] board: project #{number} is pinned, mirroring {state}"], True
    if proj.get("enabled"):
        return ["  [ok] board: on, none pinned - the loop finds or creates one on its first pick "
                "(kept from config.json; --board no turns it off)"], True
    if how == "kept":
        said = "declined - off" if answer == "no" else "off"
        return [f"  [ok] board: {said} (kept from config.json; --board yes creates and pins one)"], True
    offer = _si.board_offer(target, True)
    if not offer:
        offer = [f"  OFFER: create a GitHub Project board for {repo} and pin it. Preview: "
                 + " ".join(_vd._q(c) for c in cmd)]
    template = [_vd.python_command(), str(BOARD_SETUP), "create", os.path.abspath(str(sdlc)),
                "--template", "OWNER/N", "--yes"]
    return (["  " + l for l in offer]
            + [ask_line("board", "re-run with --board yes to create it now, or --board no to decline",
                        ["--board yes|no"]),
               "  (or copy a template board that already has the canonical fields and views; which "
               "views a copy keeps is not yet measured, see docs/board.md: "
               + " ".join(_vd._q(c) for c in template) + ")"]), True


def layout_lines(sdlc):
    """#234: the canonical fields and six views are a separate, explicit step. PRINTED, never run:
    this flow's one board mutation is `create` on `--board yes`, and that stays true."""
    base = [_vd.python_command(), str(BOARD_LAYOUT)]
    cmd = lambda verb: " ".join(_vd._q(c) for c in base + [verb, os.path.abspath(str(sdlc))])  # noqa: E731
    return ["  next (optional): give the board the canonical fields and six views (docs/board.md). "
            "Each is a dry run until you add --yes:",
            "    " + cmd("fields"), "    " + cmd("views"),
            "  then check it (read-only): " + cmd("verify")]


def ask_line(qid, prose, answers):
    """One open question, in the ONE machine-readable shape every host relays (#237):

        [ask] <id>: <prose> -> <answer> ; <answer> ...

    Each `<answer>` is an alternative: a bare `--flag`, or `--flag VALUE|VALUE` (a closed set), or
    `--flag PLACEHOLDER` (upper case: the user supplies it, e.g. `N:ID`, `FILE`). Everything after
    the LAST ` -> ` is the machine part; the prose before it is for people and may change freely.
    tools/onboarding_control.py parses this shape and answers only flags it has a policy for, so a
    flag renamed here without the control knowing turns the control red, not silently green."""
    return f"  [ask] {qid}: {prose} -> {' ; '.join(answers)}"


def _run_board(argv):
    proc = subprocess.run(argv, capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


def _capture(fn, *args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = fn(*args)
    return rc, buf.getvalue()


def _verify_trust_lines(sdlc, command):
    """#615. A committed `verify.command` only runs once THIS checkout has the git-local
    `sigma.allowRepositoryShellCommands` (#422: not committed, not cloned), so a migrated repo or a teammate's
    fresh clone must not read `[ok]`. -> [] when trusted, else the `[trust]` lines. Not an `[ask]`: no init flag
    answers it, and trust is the operator's own act after inspecting the command, so the line says to run the
    gesture, not to re-run init. The key is read where `loop.py verify` and `verify_detect` read it: the
    directory holding `.sdlc`. The repository-controlled command is printed alone, as JSON, never inside the gesture."""
    repo = pathlib.Path(os.path.abspath(sdlc)).parent
    if _sp.repository_shell_commands_allowed(repo):
        return []
    head = ("  [trust] verify: a verify command is configured but this checkout has not granted it trust, so "
            "`loop.py verify` will refuse it (git-local, not committed or cloned; every checkout grants it once).")
    shown = "  " + json.dumps(_vd.printable(command))
    try:
        inside = subprocess.run(["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
                                capture_output=True, text=True, timeout=5).stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError):
        inside = False
    if not inside:
        return [head, shown, "  It cannot be trusted: this is not a Git worktree. Run init inside the repository."]
    if any(_vd._unsafe_char(ch) for ch in str(repo)):
        return [head, shown, "  No gesture is printed: the project path holds a control character. "
                "Move the checkout, then re-run init."]
    return [head, shown,
            "  Inspect that command. If you trust this project, run this once yourself (an agent must not):",
            f"  git -C {_vd._q(repo)} config --local {_KEY} true"]


def verify_step(target, sdlc, opts):
    """#228. -> (lines, ok, asked)."""
    vd_argv = None
    if opts.get("verify"):
        n, _, ident = opts["verify"].partition(":")
        vd_argv = ["verify_detect.py", "confirm", sdlc, n, ident]
    elif opts.get("verify-command-file"):
        vd_argv = ["verify_detect.py", "set", sdlc, "--command-file", opts["verify-command-file"]]
    elif opts.get("no-verify"):
        vd_argv = ["verify_detect.py", "decline", sdlc]
    if vd_argv:
        rc, out = _capture(_vd.main, vd_argv)
        lines = ["  " + l for l in out.splitlines()]
        if rc != 0:
            return lines + ["  [FAIL] verify: nothing stored (see above)"], False, False
        return lines + ["  [ok] verify: settled"], True, False
    verify = _read_json(pathlib.Path(sdlc) / "config.json").get("verify") or {}
    if verify.get("command"):
        trust = _verify_trust_lines(sdlc, verify["command"])
        if trust:
            return trust, True, False
        return [f"  [ok] verify: `{_vd.printable(verify['command'])}` "
                f"(enforce {'ON' if verify.get('enforce') else 'OFF'})"], True, False
    if str(verify.get("_why") or "").startswith("enforce OFF: the user declined"):
        return ["  [ok] verify: declined earlier - enforce OFF (the reason is in verify._why)"], True, False
    lines = ["  " + l for l in _si.verify_report(target)]
    return lines + [ask_line("verify", "re-run with --verify N:ID (a candidate above), "
                             "--verify-command-file FILE (your own), or --no-verify",
                             ["--verify N:ID", "--verify-command-file FILE", "--no-verify"])], True, True


def labels_step(sdlc):
    proc = subprocess.run([sys.executable, str(SETUP_SCRIPT), "labels", sdlc],
                          capture_output=True, text=True)
    lines = ["  " + l.strip() for l in (proc.stdout + proc.stderr).splitlines() if l.strip()]
    if proc.returncode != 0:
        return lines + ["  [FAIL] labels: a required label could not be created (see above)"], False
    return lines + ["  [ok] labels: every sdlc:* and priority:P0-P3 label exists"], True


def _has_pending_goal(sdlc):
    for path in sorted((pathlib.Path(sdlc) / "goals").glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if "\nstatus: pending" in text.split("\n---", 1)[0] + "\n":
            return True
    return False


def _windows():
    return os.name == "nt"


def resume_command(target, argv):
    """The exact gesture to re-run, or None when it cannot be printed safely: on Windows a value
    carrying a character cmd or PowerShell would expand or end the quote on is refused (as
    `board_setup.resume_command` does) rather than printed as a line that runs something else.
    `--verify-command-file` is made absolute: the resume line must work from any directory."""
    rest, i, dropped_target = [], 0, False
    while i < len(argv):
        a = argv[i]
        if a in _VALUE and i + 1 < len(argv):
            value = argv[i + 1]
            if a == "--verify-command-file":
                value = os.path.abspath(value)
            rest += [a, value]
            i += 2
            continue
        if not a.startswith("-") and not dropped_target:
            dropped_target = True                        # the positional target: printed absolute
        else:
            rest.append(a)
        i += 1
    values = [str(pathlib.Path(__file__).resolve()), os.path.abspath(str(target))] + rest
    if _windows():
        if any(ch in _WIN_UNSAFE or _vd._unsafe_char(ch) for v in values for ch in v):
            return None
        quote = lambda v: v if v.startswith("--") else '"%s"' % v      # noqa: E731
    else:
        quote = shlex.quote
    return " ".join([_vd.python_command()] + [quote(v) for v in values])


def _say_resume(target, argv):
    line = resume_command(target, argv)
    print("Resume: " + (line if line is not None else
                        "(no command printed: a value contains a character cmd/PowerShell would "
                        "expand or end the quote on -- one of \" % $ ` !). Re-run the command you ran."))


def _records_open(cfg):
    """True only when decision_rubric.records.enabled is the JSON boolean true (the optional import line)."""
    node = cfg
    for key in ("decision_rubric", "records", "enabled"):
        node = node.get(key) if isinstance(node, dict) else None
    return node is True


def main(argv):
    args = argv[1:]
    opts, target, err = parse(args)
    if opts.get("help"):
        print(USAGE)
        return 0
    if err:
        print(f"sigma-init: {err}\n{USAGE}", file=sys.stderr)
        return 2
    if not pathlib.Path(target).is_dir():
        print(f"sigma-init: target directory does not exist: {target}", file=sys.stderr)
        return 2
    refusal = _si.git_refusal(target)                    # #229: before anything is written
    if refusal:
        print("\n".join(refusal), file=sys.stderr)
        return 2
    sdlc = os.path.abspath(os.path.join(str(target), ".sdlc"))
    try:
        coexist = _si._coexist()
    except Exception as exc:                             # noqa: BLE001 - same posture as sdlc_init
        coexist = None
        print(f"sigma-init: warning: cannot check for a second plugin ({type(exc).__name__})",
              file=sys.stderr)
    if coexist is not None:                              # #314: a notice, never a refusal
        coexist.gate(sdlc, "sigma-init")
        offer = coexist.takeover_line(sdlc)              # the dry-run command; never --apply
        if offer:
            print(offer, file=sys.stderr)

    answered, scaffolded = load_memory(sdlc)
    cfg_path = pathlib.Path(sdlc) / "config.json"
    try:
        cfg0 = read_config_strict(cfg_path)             # #625: refuse BEFORE any write of this run
    except ConfigUnreadable as exc:
        print(f"sigma-init: REFUSED - {exc}", file=sys.stderr)
        return 2
    detected = detect_github_repo(target)
    res = resolve_answers(opts, answered, scaffolded, config_answers(cfg0),
                          "github" if detected else "local-goals")
    cfg_repo = str(((cfg0.get("discovery") or {}).get("github") or {}).get("repo") or "").strip() \
        if isinstance((cfg0.get("discovery") or {}).get("github"), dict) else ""
    repo = opts.get("repo") or cfg_repo or detected
    if res["mode"][0] == "github" and not repo:
        print("sigma-init: REFUSED - github mode needs a repository, and `origin` is not a GitHub "
              "remote. Re-run with --repo OWNER/NAME (or add the GitHub remote first). Nothing "
              "was written.", file=sys.stderr)
        return 2

    # stale hook path + scaffold (skip-if-exists) -----------------------------------------------
    # #614: adoption NEVER writes `core.hooksPath` (owner decision D-1) -- an adopter's own hooks,
    # local or global, keep running. This only undoes the key releases before #614 wrote, and says
    # so in one line; it never refuses adoption.
    for line in _si.repair_stale_hook_path(target):
        print(line)
    if coexist is not None:
        # BEFORE the scaffold: a `.sdlc/` whose scaffold is interrupted is still recognisably
        # Sigma's, so the session wizard can say "re-run /sigma-init" there (and only there).
        coexist.write_owner(sdlc)
    if opts.get("ignore-scope") == "local":
        subprocess.run([sys.executable, str(SETUP_SCRIPT), "ignore", str(target), "--scope", "local"],
                       capture_output=True, text=True)
    try:
        created, skipped = _si.scaffold(target)
    except _si.RuntimeIgnoreWriteFailed as exc:
        print(f"sigma-init: [FAIL] scaffold: {exc}")
        _say_resume(target, args)
        return 1
    print(f"sigma-init: scaffold - {len(created)} created, {len(skipped)} kept "
          f"(target: {pathlib.Path(target).resolve()})")
    for c in created:
        print(f"  + .sdlc/{c}")
    if "config.json" in created:                         # #228: `_why` from the repo as it now is
        _vd.write_verify(sdlc, None, _vd.unconfirmed_why(_vd.detect(target)))
        # the template's values: still OPEN questions until someone answers or changes them
        fresh = config_answers(_read_json(cfg_path))
        scaffolded = {k: v for k, v in fresh.items() if res[k][1] == "open"}

    mode_lines = apply_config(sdlc, res, repo, repo_flag=bool(opts.get("repo")))
    cfg = _read_json(cfg_path)
    source = (cfg.get("discovery") or {}).get("source")
    extras = {"--" + k for k in ("demo", "vision", "codex", "cursor") if opts.get(k)}
    if source == "github":
        extras.add("--github")                           # only for the demo's gh-issue hint
    _si.scaffold_extras(target, extras)
    if opts.get("github-templates"):
        gcreated, _ = _si.scaffold_github(target)
        print(f"\nsigma-init: .github/ scaffolding - {len(gcreated)} created")

    # init.json: which questions are answered -- never a value re-applied over config.json
    now_answered = dict(answered)
    for key in CONFIG_KEYS:
        if res[key][1] in ("flag", "default"):
            now_answered[key] = res[key][0]
    kept_notes = []
    for key, flag in (("mode", "--mode %s"), ("work", "--work on / --local-only"),
                      ("ledger", "--ledger %s")):
        cur, how = res[key]
        said = answered.get(key)
        if how == "kept" and said is not None and cur is not None and said != cur:
            kept_notes.append(f"  [kept] {key}: config.json says {cur}; you answered {said} at an "
                              f"earlier /sigma-init - config.json wins (to change it: "
                              f"{flag % said if '%s' in flag else flag})")

    failed, asked = [], []
    trust_open = False

    # 1 preflight --------------------------------------------------------------------------------
    print("\nsigma-init: 1/5 preflight")
    checks = _pf.preflight(str(pathlib.Path(target).resolve()), cfg,
                           runner=_si.PREFLIGHT_RUNNER, which=_si.PREFLIGHT_WHICH)
    for line in _si.preflight_report(target, checks=checks):
        print("  " + line)
    work_on = bool((cfg.get("work") or {}).get("enabled"))
    remote = next((c for c in checks if c["id"] == "remote"), {})
    if work_on and remote.get("ok") is False and res["work"][1] != "flag" and "work" not in answered:
        asked.append("work")
        print(ask_line("work", "fix the remote above, or re-run with --local-only", ["--local-only"]))
    # A missing remote while that very choice is still open is a question (the DECISION above),
    # not a failure; every other blocking check fails the run.
    if [c for c in _pf.blocking(checks) if not (c["id"] == "remote" and "work" in asked)]:
        failed.append("preflight")

    # 2 mode -------------------------------------------------------------------------------------
    print("\nsigma-init: 2/5 mode")
    if res["mode"][1] == "open":
        asked.append("mode")
        default = "github" if detected else "local-goals"
        why = f"origin is {detected}" if detected else "origin is not a GitHub repository"
        print(ask_line("mode", f"local-goals (goal files) or github (issues)? default: {default} ({why}).",
                       ["--mode local-goals|github"]))
        print("        Re-run with --mode github or --mode local-goals (--yes takes the default).")
    for line in mode_lines + kept_notes:
        print(line)
    if source == "github" and detected and cfg_repo and detected != cfg_repo and not opts.get("repo"):
        print(f"  [note] origin is {detected}; discovery.github.repo stays {cfg_repo} "
              f"(re-run with --repo {detected} to switch)")

    # 3 verify -----------------------------------------------------------------------------------
    print("\nsigma-init: 3/5 verify")
    lines, ok, ask = verify_step(target, sdlc, opts)
    print("\n".join(lines))
    if any(l.startswith("  [trust] verify") for l in lines):
        trust_open = True
    if not ok:
        failed.append("verify")
    if ask:
        asked.append("verify")

    # 4 github -----------------------------------------------------------------------------------
    print("\nsigma-init: 4/5 github")
    if source != "github":
        print("  [skip] " + ("mode not chosen yet" if res["mode"][1] == "open"
                             else "local-goals mode: no labels, no board, no ledger question"))
    else:
        lines, ok = labels_step(sdlc)
        print("\n".join(lines))
        if not ok:
            failed.append("labels")
        cfg = _read_json(cfg_path)
        print(f"  [ok] assignee: {((cfg.get('discovery') or {}).get('github') or {}).get('assignee')}"
              " (the loop picks issues assigned to you)")
        board, board_how = res["board"]
        lines, ok = board_step(target, sdlc, board, repo, board_how)
        print("\n".join(lines))
        if not ok:
            failed.append("board")
        elif board_how == "flag":
            now_answered["board"] = board
        elif any("[ask] board" in l for l in lines):
            asked.append("board")
        value, how = res["ledger"]
        suffix = " (kept from config.json; --ledger yes|no changes it)" if how == "kept" else ""
        if value == "yes":
            print("  [ok] ledger: on. Bootstrap its ops branch when ready (it pushes a branch): "
                  + " ".join([_vd.python_command(), _vd._q(str(SYNC_SCRIPT)), "bootstrap", _vd._q(sdlc)])
                  + suffix)
        elif value == "no" or how == "kept":
            print("  [ok] ledger: off" + suffix)
        else:
            asked.append("ledger")
            print(ask_line("ledger", "the team ledger (claims + hand-offs on an ops branch)? "
                           "re-run with --ledger yes or --ledger no (--yes: no)", ["--ledger yes|no"]))

    # LAST write to config.json of this run is above: record its fingerprint, so a later run knows
    # the template values are still open only if nothing else has written config.json since.
    memory = {"answered": now_answered,
              "scaffolded": {k: v for k, v in scaffolded.items() if k not in now_answered},
              "config": config_fingerprint(cfg_path)}
    if memory != _read_json(pathlib.Path(sdlc) / ANSWERS):
        _write_json(pathlib.Path(sdlc) / ANSWERS, memory)

    # 5 summary ----------------------------------------------------------------------------------
    cfg = _read_json(cfg_path)
    verify = cfg.get("verify") or {}
    print("\nsigma-init: 5/5 summary")
    print(f"  mode:   {(cfg.get('discovery') or {}).get('source')}"
          + (f" ({((cfg.get('discovery') or {}).get('github') or {}).get('repo')})"
             if (cfg.get("discovery") or {}).get("source") == "github" else ""))
    print(f"  work:   {'PR per goal' if (cfg.get('work') or {}).get('enabled') else 'local-only (this checkout)'}")
    print(f"  verify: {('`' + _vd.printable(verify['command']) + '`') if verify.get('command') else 'none (enforce OFF)'}")
    if failed:
        print(f"  result: FAILED at {', '.join(failed)} - everything written so far is kept")
        _say_resume(target, args)
        return 1
    if asked:
        print(f"  open:   {', '.join(asked)} - answer the [ask] lines above (same command + the flag)")
    if trust_open:
        print("  open:   verify trust - run the [trust] gesture above yourself, once per checkout; "
              "do not re-run init for it")
    if _records_open(cfg):
        print("Optional (never run by init): import past decisions as records; it reads only local files: "
              + " ".join([_vd.python_command(), _vd._q(str(LOOP_SCRIPT.with_name("decision_import.py"))),
                          "plan", _vd._q(sdlc)]))
    loop = " ".join([_vd.python_command(), _vd._q(str(LOOP_SCRIPT)), "next", _vd._q(sdlc)])
    if (cfg.get("discovery") or {}).get("source") == "github":
        print("Next: label an issue `sdlc:goal` (assigned to you), then /sigma-loop "
              f"(Codex/Cursor: {loop}).")
    elif _has_pending_goal(sdlc):
        print(f"Next: /sigma-loop (Codex/Cursor: {loop}).")
    else:
        print("Next: re-run with --demo to queue a demo goal first (or add .sdlc/goals/NNNN-*.md with "
              f"status: pending), then /sigma-loop (Codex/Cursor: {loop}).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
