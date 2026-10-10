"""The core reaches the network only through five reviewed (file, module) pairs (S1-G1; strict
since #2584).

WHAT IS CHECKED, over the public surface's Python outside tests/ (`_scanned()`, from
`tests/public_surface.py`, the surface all three guards share):
  - NETWORK MODULES (`_NETWORK_MODULES`), imported at ANY nesting level (`ast.walk`: inside a
    function, under `if TYPE_CHECKING:`). `urllib.request` is network; `urllib.parse` is not.
  - NETWORK TOOLS (`_NETWORK_TOOLS`) started as a child: a call to `subprocess.run/call/check_call/
    check_output/Popen/getoutput/getstatusoutput`, to a name imported from `subprocess`, or to
    `os.system`/`os.popen`, whose FIRST argument names one -- a list or tuple by the basename of its
    element 0, a string by the basename of any `shlex` token. Found as `subprocess:<tool>`. (Until
    #2584 only a string argument of `subprocess.<call>` was read, and as a substring: a list argv
    was invisible and `rsync` read as `nc`.)
  - An UNPARSEABLE file is a finding (`cannot parse`), never silently clean.

ALLOWLIST (`_ALLOWLIST`): exact `(file, module)` pairs, each with its reason -- a listed file can
no longer import an unlisted module. The reasons are reviewed claims, so they say what the code
does, including where a docstring promises more than it enforces:
  - agent_watch.py, smtplib: SMTP to the server the user configured.
  - agent_watch.py, socket: `socket.gethostname()` for the default From address; it opens no
    connection (`platform.node()` would remove this pair -- a named follow-up).
  - channel_notify.py, urllib.request: POSTs only to an http(s) loopback webhook by default;
    remote delivery needs the exact allow_remote_webhook boolean opt-in.
  - slack_client.py, urllib.request: the Slack Web API, with the user's token.
  - slack_commands_listen.py, slack_sdk: Slack Socket Mode, with the user's app.

STRICT. There is no baseline file: the found pairs must equal the allowlist exactly -- a new pair
fails, and a listed pair that is gone fails as stale. No command writes an entry (#2584 owner
ruling 3); every failure message ends with `Recheck: python3 tests/test_no_network_in_core.py`.
Run as a script, the file takes NO arguments (any argument exits 2, writing nothing), runs every
zero-argument test here, printing `ran: <name>` before each, and ends with
`test_no_network_in_core: OK|FAIL (<k> finding(s) over <n> scanned file(s))`.

NOT COVERED: modules outside `_NETWORK_MODULES` -- the list is the clear-cut network modules, not
an exhaustive census (`asyncio`'s streams and `multiprocessing.connection` can open sockets and are
not listed); dynamic imports (`importlib`, `__import__`); binaries other than `_NETWORK_TOOLS`; a
network tool behind a wrapper -- `["sh", "-c", "curl ..."]`, `"sh -c 'curl ...'"` or
`["/usr/bin/env", "curl", ...]` -- because only element 0 of a list, or a whole `shlex` token of a
string, is read; and tests/ (three test files import `socket` for local fixtures).
"""
import ast
import inspect
import pathlib
import shlex
import subprocess
import sys

import public_surface

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Network modules. urllib keeps its submodule: only `urllib.request` is network.
_NETWORK_MODULES = frozenset({
    "socket", "ssl", "http", "urllib.request", "urllib3", "requests", "httpx", "aiohttp",
    "websocket", "websockets", "slack_sdk", "smtplib", "ftplib", "telnetlib", "xmlrpc",
    "imaplib", "poplib", "nntplib", "socketserver",
    "anthropic",
})

#: Network binaries a child process might run.
_NETWORK_TOOLS = frozenset({"curl", "wget", "nc", "ncat", "socat"})

_SUBPROCESS_CALLS = frozenset({"run", "call", "check_call", "check_output", "Popen", "getoutput",
                               "getstatusoutput"})
_OS_CALLS = frozenset({"system", "popen"})

#: The only network the core may use: exact (file, module) pairs, each a reviewed claim.
_ALLOWLIST = {
    ("skills/sigma-loop/scripts/agent_watch.py", "smtplib"):
        "SMTP to the server the user configured",
    ("skills/sigma-loop/scripts/agent_watch.py", "socket"):
        "socket.gethostname() for the default From address; opens no connection "
        "(platform.node() would remove this pair: a follow-up)",
    ("skills/sigma-loop/scripts/channel_notify.py", "urllib.request"):
        "POSTs only to an http(s) loopback webhook by default; remote delivery needs the exact "
        "allow_remote_webhook boolean opt-in",
    ("skills/sigma-loop/scripts/slack_client.py", "urllib.request"):
        "the Slack Web API, with the user's token",
    ("skills/sigma-loop/scripts/slack_commands_listen.py", "slack_sdk"):
        "Slack Socket Mode, with the user's app",
}

_RECHECK = "Recheck: python3 tests/test_no_network_in_core.py"

_SLACK_CLIENT = "skills/sigma-loop/scripts/slack_client.py"

#: The one file #2575 (S1-G8) took a remote row-count call out of; the call now lives on the private
#: side, and `test_the_core_doctor_holds_no_network_entry` asserts its absence positively.
_CORE_DOCTOR = "skills/sigma-doctor/scripts/doctor.py"


def _imported_modules(tree):
    """Every imported module, reduced to its root -- except `urllib.<sub>`, kept whole."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.name if alias.name.startswith("urllib.")
                        else alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == "urllib":
                out |= {"urllib.%s" % a.name for a in node.names}
            elif node.module.startswith("urllib."):
                out.add(node.module)
            else:
                out.add(node.module.split(".", 1)[0])
    return out


def _tool_of(arg):
    """The network tool `arg` (a call's first argument) starts, or None."""
    if isinstance(arg, (ast.List, ast.Tuple)) and arg.elts:
        first = arg.elts[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            base = first.value.rsplit("/", 1)[-1]
            return base if base in _NETWORK_TOOLS else None
    elif isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        try:
            tokens = shlex.split(arg.value)
        except ValueError:
            tokens = arg.value.split()
        for token in tokens:
            base = token.rsplit("/", 1)[-1]
            if base in _NETWORK_TOOLS:
                return base
    return None


def _network_tool_calls(tree):
    """`{"subprocess:<tool>"}` for every child-process call whose first argument names a tool."""
    from_subprocess, from_os = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module in ("subprocess", "os"):
            for a in node.names:
                calls = _SUBPROCESS_CALLS if node.module == "subprocess" else _OS_CALLS
                if a.name in calls:
                    (from_subprocess if node.module == "subprocess" else from_os).add(a.asname or a.name)
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        starts = (
            (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
             and ((f.value.id == "subprocess" and f.attr in _SUBPROCESS_CALLS)
                  or (f.value.id == "os" and f.attr in _OS_CALLS)))
            or (isinstance(f, ast.Name) and f.id in (from_subprocess | from_os)))
        if not starts:
            continue
        first = node.args[0] if node.args else next(
            (k.value for k in node.keywords if k.arg in ("args", "cmd", "command")), None)
        tool = _tool_of(first) if first is not None else None
        if tool:
            out.add("subprocess:%s" % tool)
    return out


def _check(rel, source):
    """The network modules and `subprocess:<tool>` keys `source` uses; `{"cannot parse"}` when it
    does not parse."""
    try:
        tree = ast.parse(source, filename=rel)
    except (SyntaxError, ValueError):
        return {"cannot parse"}
    return (_imported_modules(tree) & _NETWORK_MODULES) | _network_tool_calls(tree)


def _scanned():
    """The public surface's Python outside tests/ (`public_surface.public_files`): posix relpaths,
    sorted, any-case `.py`."""
    return [rel for rel in public_surface.public_files(ROOT)
            if rel.lower().endswith(".py") and not rel.startswith("tests/")]


def _scan():
    """`{(file, module)}` over `_scanned()`."""
    return {(rel, module) for rel in _scanned()
            for module in _check(rel, (ROOT / rel).read_text(encoding="utf-8-sig", errors="replace"))}


def _assert_exact(found):
    new = sorted(found - set(_ALLOWLIST))
    stale = sorted(set(_ALLOWLIST) - found)
    assert not new and not stale, (
        "the core's network use is not exactly the allowlist:\n"
        + "".join("  NEW %s: %s\n" % k for k in new)
        + ("  -> the core opens no connection the user did not configure; there is no baseline "
           "to add it to\n" if new else "")
        + "".join("  STALE %s: %s -- remove the entry; the list only shrinks\n" % k for k in stale)
        + _RECHECK)


def _baselines_state():
    d = ROOT / "tests" / "boundary_baselines"
    return (d.exists(), sorted(p.name for p in d.iterdir()) if d.exists() else [])


def test_the_allowlist_is_exact_pairs_with_reasons():
    assert len(_ALLOWLIST) == 5
    for (rel, module), reason in _ALLOWLIST.items():
        assert (ROOT / rel).is_file(), rel
        assert module in _NETWORK_MODULES or module.startswith("subprocess:"), module
        assert reason.strip(), (rel, module)


def test_a_listed_file_cannot_add_an_unlisted_module():
    assert "socket" in _check(_SLACK_CLIENT, "import socket\n")
    assert _check(_SLACK_CLIENT, "import socket\n") - {m for f, m in _ALLOWLIST if f == _SLACK_CLIENT}


_FORMS = {
    "list": 'import subprocess\nsubprocess.run(["curl", "https://x"])\n',
    "tuple": 'import subprocess\nsubprocess.Popen(("wget", "x"))\n',
    "from-import": 'from subprocess import run\nrun(["/usr/bin/nc", "x", "1"])\n',
    "shell-string": 'import subprocess\nsubprocess.run("socat a b", shell=True)\n',
    "os.system": 'import os\nos.system("curl http://x")\n',
    "os.popen": 'import os\nos.popen("/usr/bin/wget x")\n',
}


def test_network_tools_are_found_in_every_argv_form():
    """Every argv form, in one zero-argument test so the script gesture runs it too."""
    missed = {form: sorted(_check("x.py", _FORMS[form])) for form in sorted(_FORMS)
              if not any(m.startswith("subprocess:") for m in _check("x.py", _FORMS[form]))}
    assert not missed, "argv form(s) whose network tool was not found: %s" % missed


def test_a_tool_name_inside_another_word_is_not_found():
    assert _check("x.py", 'import subprocess\nsubprocess.run("rsync a b", shell=True)\n') == set()
    assert _check("x.py", 'import subprocess\nsubprocess.run(["curlx", "a"])\n') == set()
    assert _check("x.py", 'import subprocess\nsubprocess.run(["git", "nc"])\n') == set()


def test_an_unparseable_file_is_a_finding():
    assert _check("x.py", "print 'not python 3'\n") == {"cannot parse"}


def test_the_guard_takes_no_arguments_and_writes_nothing():
    before = _baselines_state()
    proc = subprocess.run([sys.executable, str(ROOT / "tests" / "test_no_network_in_core.py"),
                           "--write-baseline"], capture_output=True, text=True, cwd=str(ROOT))
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert "this guard takes no arguments; there is no baseline to write (#2584)" in proc.stderr
    assert _baselines_state() == before


def test_the_real_core_is_scanned():
    scanned = _scanned()
    assert "skills/sigma-loop/scripts/loop.py" in scanned and _SLACK_CLIENT in scanned
    print("network guard scans %d file(s)" % len(scanned))


def test_the_core_doctor_holds_no_network_entry():
    """#2575 DoD-3, asserted POSITIVELY, in both places it can be true: no allowlist key names the
    core doctor, and an AST walk of the live file finds no network use.

    PARSE FIRST, AND FAIL ON A SYNTAX ERROR: an unparseable doctor must not read as "imports no
    network module" (measured while breaking this control on purpose, #2575)."""
    assert not [k for k in _ALLOWLIST if k[0] == _CORE_DOCTOR]
    source = (ROOT / _CORE_DOCTOR).read_text(encoding="utf-8")
    ast.parse(source, filename=_CORE_DOCTOR)
    live = _check(_CORE_DOCTOR, source)
    assert not live, ("the core doctor uses the network: %s -- `urllib.parse` is fine, only "
                      "`urllib.request` is in _NETWORK_MODULES" % sorted(live))


def test_no_network_in_core():
    _assert_exact(_scan())


def _zero_argument_tests():
    """Every `test_*` here that takes no fixture, in definition order."""
    return [(name, fn) for name, fn in list(globals().items())
            if name.startswith("test_") and inspect.isfunction(fn)
            and not inspect.signature(fn).parameters]


def _tests_the_script_cannot_run():
    """`test_*` functions that take a parameter: the script gesture cannot run them, so a guard
    whose documented invocation silently skipped one could not fail on it (#2584 review round 5)."""
    return [name for name, fn in list(globals().items())
            if name.startswith("test_") and inspect.isfunction(fn)
            and inspect.signature(fn).parameters]


# The script gesture sits at the END of the file on purpose (#2580).
if __name__ == "__main__":
    if sys.argv[1:]:
        print("this guard takes no arguments; there is no baseline to write (#2584)", file=sys.stderr)
        sys.exit(2)
    failed = []
    for name, fn in _zero_argument_tests():
        print("ran: %s" % name)
        try:
            fn()
        except Exception as e:              # noqa: BLE001 - every failure is reported, then rc 1
            failed.append(name)
            print("FAILED %s: %s" % (name, e), file=sys.stderr)
    skipped = _tests_the_script_cannot_run()
    if skipped:
        failed.append("script coverage")
        print("FAILED script coverage: test(s) this script cannot run (they take a parameter): %s"
              % ", ".join(skipped), file=sys.stderr)
    found = _scan()
    findings = (found - set(_ALLOWLIST)) | (set(_ALLOWLIST) - found)
    print("test_no_network_in_core: %s (%d finding(s) over %d scanned file(s))"
          % ("FAIL" if failed else "OK", len(findings), len(_scanned())))
    sys.exit(1 if failed else 0)
