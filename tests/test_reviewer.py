"""Tests for skills/sigma-loop/scripts/reviewer.py (issue #1983).

Independence is a property of how a review is RUN, not a claim the reviewer can make about itself.
These assert the resolution ITSELF -- which mechanism a given host/config lands on -- because that is
the only part a test can hold; the prose that consumes it is asserted in test_packaging_slice4.py.
"""
import importlib.util
import json
import os
import pathlib
import shlex
import signal
import subprocess
import sys
import tempfile
import time

S = pathlib.Path(__file__).resolve().parent.parent / "skills" / "sigma-loop" / "scripts"


def _r():
    spec = importlib.util.spec_from_file_location("reviewer", S / "reviewer.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def _sdlc(d, review):
    base = pathlib.Path(d) / ".sdlc"; base.mkdir(parents=True, exist_ok=True)
    (base / "config.json").write_text(json.dumps({"review": review}), encoding="utf-8")
    # #707: review.command needs the operator's Git-local opt-in; these fixtures are the operator's own.
    subprocess.run(["git", "init", "-q", str(base.parent)], check=True)
    subprocess.run(["git", "-C", str(base.parent), "config", "--local",
                    "sigma.allowRepositoryShellCommands", "true"], check=True)
    return str(base)


def test_explicit_host_beats_the_environment():
    """review.host is the operator's override; a Claude env must not win over it."""
    with tempfile.TemporaryDirectory() as d:
        base = _sdlc(d, {"host": "inline"})
        got = _r().resolve(base, env={"CLAUDECODE": "1"}, which=lambda b: "/usr/bin/" + b)
        assert got["mechanism"] == "inline"


def test_codex_only_review_override_preserves_claude_selection():
    with tempfile.TemporaryDirectory() as d:
        base = _sdlc(d, {"host": "claude", "host_overrides": {"codex": "codex"}})
        codex = _r().resolve(base, env={"CODEX_THREAD_ID": "task"},
                             which=lambda b: "/usr/bin/" + b)
        claude = _r().resolve(base, env={"CLAUDECODE": "1"},
                              which=lambda b: "/usr/bin/" + b)
        assert codex["host"] == "codex" and codex["mechanism"] == "process"
        assert claude["host"] == "claude" and claude["mechanism"] == "subagent"


def test_claude_env_resolves_a_subagent():
    with tempfile.TemporaryDirectory() as d:
        base = _sdlc(d, {"host": "auto"})
        got = _r().resolve(base, env={"CLAUDECODE": "1"}, which=lambda b: None)
        assert got["mechanism"] == "subagent" and got["host"] == "claude"
        assert got["verified"] is True


def test_cursor_resolves_a_process_with_its_real_flags():
    with tempfile.TemporaryDirectory() as d:
        base = _sdlc(d, {"host": "cursor"})
        got = _r().resolve(base, env={}, which=lambda b: "/usr/bin/" + b)
        assert got["mechanism"] == "process"
        assert got["command"][:2] == ["cursor-agent", "-p"]


def test_codex_is_marked_unverified():
    """AGENTS.md RELIABILITY: the codex branch was never executed, and must say so."""
    with tempfile.TemporaryDirectory() as d:
        base = _sdlc(d, {"host": "codex"})
        got = _r().resolve(base, env={}, which=lambda b: "/usr/bin/" + b)
        assert got["mechanism"] == "process" and got["verified"] is False


def test_codex_session_auto_resolves_a_read_only_fresh_process():
    with tempfile.TemporaryDirectory() as d:
        base = _sdlc(d, {"host": "auto", "independent": True})
        got = _r().resolve(base, env={"CODEX_SESSION_ID": "real-session"},
                           which=lambda b: "/usr/bin/" + b)
        assert got["mechanism"] == "process" and got["host"] == "codex"
        assert got["command"] == ["codex", "exec", "--sandbox", "read-only",
                                  "--ephemeral", "-"]


def test_claude_marker_still_wins_when_both_host_markers_are_present():
    with tempfile.TemporaryDirectory() as d:
        base = _sdlc(d, {"host": "auto"})
        got = _r().resolve(base, env={"CLAUDECODE": "1", "CODEX_SESSION_ID": "nested"},
                           which=lambda b: "/usr/bin/" + b)
        assert got["mechanism"] == "subagent" and got["host"] == "claude"


def test_named_host_whose_cli_is_absent_degrades_loudly():
    """The failure this whole goal exists to prevent: never a SILENT self-review."""
    with tempfile.TemporaryDirectory() as d:
        base = _sdlc(d, {"host": "cursor"})
        got = _r().resolve(base, env={}, which=lambda b: None)
        assert got["mechanism"] == "inline"
        assert "cursor-agent" in got["reason"]


def test_review_command_wins_under_auto():
    with tempfile.TemporaryDirectory() as d:
        base = _sdlc(d, {"host": "auto", "command": "my-agent --print"})
        got = _r().resolve(base, env={"CLAUDECODE": "1"}, which=lambda b: None)
        assert got["mechanism"] == "command" and got["command"] == "my-agent --print"


def test_host_command_with_empty_command_is_named_not_guessed():
    with tempfile.TemporaryDirectory() as d:
        base = _sdlc(d, {"host": "command", "command": "   "})
        got = _r().resolve(base, env={}, which=lambda b: None)
        assert got["mechanism"] == "inline" and "review.command" in got["reason"]


def test_unknown_host_value_is_inline_with_a_reason():
    with tempfile.TemporaryDirectory() as d:
        base = _sdlc(d, {"host": "banana"})
        got = _r().resolve(base, env={}, which=lambda b: None)
        assert got["mechanism"] == "inline" and "banana" in got["reason"]


def test_missing_config_never_raises():
    with tempfile.TemporaryDirectory() as d:
        got = _r().resolve(str(pathlib.Path(d) / "nope"), env={}, which=lambda b: None)
        assert got["mechanism"] == "inline" and got["reason"]


def test_cli_prints_one_json_object_and_exits_zero():
    with tempfile.TemporaryDirectory() as d:
        base = _sdlc(d, {"host": "inline"})
        p = subprocess.run([sys.executable, str(S / "reviewer.py"), "resolve", base],
                           capture_output=True, text=True)
        assert p.returncode == 0
        assert json.loads(p.stdout)["mechanism"] == "inline"


# --- the dispatch-site half of breaker.py's guarantee (#1936 -> #1983) ---------------------------

def test_check_passes_a_clean_brief(tmp_path):
    b = tmp_path / "brief.md"
    b.write_text("Review the goal's acceptance criteria.\n", encoding="utf-8")
    p = subprocess.run([sys.executable, str(S / "reviewer.py"), "check", str(b)],
                       capture_output=True, text=True)
    assert p.returncode == 0


def test_a_diff_in_a_review_brief_is_the_artifact_not_a_leak(tmp_path):
    """A breaker must never see the implementation; a REVIEWER must -- the diff is what it reviews.
    breaker's _LEAK also matches any `-` bullet and any prose with `def `/`return `, so applying it
    to a markdown review brief flags it on formatting alone. Measured: the real #1983 brief was
    flagged on its first run, which is the false positive breaker's own comment warns gets a guard
    switched off."""
    b = tmp_path / "brief.md"
    b.write_text("Review this:\ndiff --git a/x.py b/x.py\n+def f(): pass\n", encoding="utf-8")
    p = subprocess.run([sys.executable, str(S / "reviewer.py"), "check", str(b)],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr


def test_a_real_review_brief_passes_the_check(tmp_path):
    """The regression that motivated the fix: an ordinary markdown brief -- `-` bullets, `##`
    headers, prose containing the word `return` -- must not be flagged."""
    b = tmp_path / "brief.md"
    b.write_text("## What you are reviewing\n- the branch diff\n- every caller\n\n"
                 "Trace what the callers return before judging.\n", encoding="utf-8")
    p = subprocess.run([sys.executable, str(S / "reviewer.py"), "check", str(b)],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr


def test_check_fails_closed_on_a_shared_scratch_path(tmp_path):
    b = tmp_path / "brief.md"
    b.write_text("notes are in /tmp/shared/notes.md\n", encoding="utf-8")
    p = subprocess.run([sys.executable, str(S / "reviewer.py"), "check", str(b),
                        "--scratch", "/tmp/shared/notes.md"], capture_output=True, text=True)
    assert p.returncode == 1
    assert "scratch" in (p.stdout + p.stderr)


def test_check_does_not_reimplement_breaker():
    """Reuse the sibling, do not clone its regexes: a tightening in breaker must be inherited.

    Asserts the LOAD, not the name: this module's own docstring cites `no_channel()`, so a substring
    check for it passes with no implementation at all -- which it did, before this was tightened."""
    src = (S / "reviewer.py").read_text(encoding="utf-8")
    assert '_load("breaker")' in src, "reviewer.py does not actually load breaker"
    assert "diff --git" not in src, "reviewer.py clones breaker's leak patterns instead of calling it"


def test_check_on_an_unreadable_brief_fails_closed():
    """A guard that cannot read its input has not checked anything, so it must not report ok."""
    p = subprocess.run([sys.executable, str(S / "reviewer.py"), "check", "/nope/missing.md"],
                       capture_output=True, text=True)
    assert p.returncode == 1


# --- the documented process/command review gesture ----------------------------------------------

def _review_run(base, brief, scratch, *, env=None):
    return subprocess.run([sys.executable, str(S / "reviewer.py"), "run", base, str(brief),
                           "--scratch", str(scratch)], capture_output=True, text=True,
                          env=env, timeout=10)


def test_run_gesture_passes_the_existing_brief_to_an_explicit_command(tmp_path):
    """The actual skill gesture must invoke the reviewer with the brief on stdin."""
    script = tmp_path / "review command.py"
    script.write_text("import sys\nprint('VERDICT: ' + sys.stdin.read().strip())\n")
    base = _sdlc(tmp_path, {"host": "command", "command": f"{shlex.quote(sys.executable)} "
                                                     f"{shlex.quote(str(script))}",
                             "timeout_seconds": 3})
    brief = tmp_path / "brief.md"
    brief.write_text("Review the concrete plan.\n")

    got = _review_run(base, brief, tmp_path / "scratch")
    assert got.returncode == 0, got.stderr
    assert got.stdout.strip() == "VERDICT: Review the concrete plan."


def test_run_gesture_executes_a_resolved_process_without_shell_expansion(tmp_path):
    cli_dir = tmp_path / "bin"
    cli_dir.mkdir()
    cli = cli_dir / "cursor-agent"
    cli.write_text("#!/usr/bin/env python3\n"
                   "import sys\n"
                   "assert sys.argv[1:] == ['-p', '--output-format', 'text']\n"
                   "print('VERDICT: ' + sys.stdin.read().strip())\n")
    cli.chmod(0o755)
    base = _sdlc(tmp_path, {"host": "cursor", "timeout_seconds": 3})
    brief = tmp_path / "brief.md"
    brief.write_text("Review the concrete code.\n")
    env = dict(os.environ, PATH=f"{cli_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    got = _review_run(base, brief, tmp_path / "scratch", env=env)
    assert got.returncode == 0, got.stderr
    assert got.stdout.strip() == "VERDICT: Review the concrete code."


def test_run_gesture_rejects_nonzero_or_empty_reviewer_output(tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text("Review this.\n")
    for program, reason in (("import sys; sys.exit(7)", "exit 7"),
                            ("import sys; sys.stdin.read()", "empty output")):
        base = _sdlc(tmp_path, {"host": "command",
                                "command": f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}",
                                "timeout_seconds": 3})
        got = _review_run(base, brief, tmp_path / "scratch")
        assert got.returncode != 0
        assert not got.stdout.strip()
        assert reason in got.stderr


def test_run_gesture_checks_shared_scratch_before_spawning(tmp_path):
    marker = tmp_path / "spawned"
    program = f"from pathlib import Path; Path({str(marker)!r}).write_text('spawned')"
    base = _sdlc(tmp_path, {"host": "command",
                            "command": f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}"})
    scratch = tmp_path / "scratch"
    brief = tmp_path / "brief.md"
    brief.write_text(f"Use {scratch} as your notes.\n")

    got = _review_run(base, brief, scratch)
    assert got.returncode != 0 and "LEAK:" in got.stderr
    assert not marker.exists()


def test_run_gesture_refuses_claude_subagent_route(tmp_path):
    base = _sdlc(tmp_path, {"host": "auto"})
    brief = tmp_path / "brief.md"
    brief.write_text("Review this.\n")
    env = dict(os.environ, CLAUDECODE="1")
    env.pop("CODEX_SESSION_ID", None)
    env.pop("CODEX_THREAD_ID", None)

    got = _review_run(base, brief, tmp_path / "scratch", env=env)
    assert got.returncode != 0 and "subagent" in got.stderr


def test_run_gesture_timeout_stops_the_reviewer_and_its_child(tmp_path):
    """The documented run gesture must not leave a child consuming quota after timeout."""
    heartbeat = tmp_path / "heartbeat"
    child_pid = tmp_path / "child.pid"
    child = tmp_path / "child.py"
    child.write_text("import pathlib,time\n"
                     f"p=pathlib.Path({str(heartbeat)!r})\n"
                     "while True:\n p.write_text(str(time.time_ns()))\n time.sleep(0.05)\n")
    parent = tmp_path / "review.py"
    parent.write_text("import pathlib,subprocess,sys,time\n"
                      f"p=subprocess.Popen([sys.executable,{str(child)!r}], "
                      "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
                      f"pathlib.Path({str(child_pid)!r}).write_text(str(p.pid))\n"
                      f"while not pathlib.Path({str(heartbeat)!r}).exists(): time.sleep(0.01)\n"
                      "time.sleep(30)\n")
    base = _sdlc(tmp_path, {"host": "command",
                            "command": f"{shlex.quote(sys.executable)} {shlex.quote(str(parent))}",
                            "timeout_seconds": 1})
    brief = tmp_path / "brief.md"
    brief.write_text("Review this.\n")
    try:
        start = time.monotonic()
        got = _review_run(base, brief, tmp_path / "scratch")
        assert got.returncode == 124 and "timed out" in got.stderr
        assert time.monotonic() - start < 1 + 10   # timeout_seconds=1 plus a 10s load margin; the child sleeps 30s
        assert heartbeat.exists(), "control never started a child"
        before = heartbeat.read_text()
        time.sleep(0.3)
        assert heartbeat.read_text() == before, "timed-out review child kept running"
    finally:
        if child_pid.exists():
            try:
                os.kill(int(child_pid.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_review_skills_document_the_executable_run_gesture():
    root = pathlib.Path(__file__).resolve().parent.parent
    for skill in ("sigma-review", "sigma-plan-review", "sigma-goal-review"):
        text = (root / "skills" / skill / "SKILL.md").read_text()
        assert 'reviewer.py" run .sdlc ' in text, skill
        assert '--scratch "$(pwd)/.sdlc/work/' in text, skill


# --- the adopter-facing contract ----------------------------------------------------------------

def test_template_ships_every_key_the_resolver_reads():
    """A resolver key absent from the template is a key no adopter ever discovers."""
    root = pathlib.Path(__file__).resolve().parent.parent
    # The template is plain JSON -- verified: it parses as-is, with no {{placeholders}} and no //
    # comments. Do not add comment-stripping for a format this file does not use.
    tmpl = json.loads((root / "skills" / "sigma-init" / "templates" / "config.json.tmpl")
                      .read_text(encoding="utf-8"))
    review = tmpl["review"]
    for key in ("independent", "host", "command", "timeout_seconds"):
        assert key in review, "config template's review block is missing %s" % key
    assert review["host"] == "auto"


def test_template_documents_every_legal_host_value():
    root = pathlib.Path(__file__).resolve().parent.parent
    doc = (root / "skills" / "sigma-init" / "templates"
           / "config.json.tmpl").read_text(encoding="utf-8")
    for host in _r().HOSTS:
        assert host in doc, "_review_host does not document the %s value" % host


def test_doctor_remedy_string_names_keys_that_actually_exist():
    """doctor.py advertised '"context": "project"' -- a key that is not in the template at all.
    A remedy line telling an operator to set a key nothing reads is worse than none."""
    root = pathlib.Path(__file__).resolve().parent.parent
    tmpl = json.loads((root / "skills" / "sigma-init" / "templates" / "config.json.tmpl")
                      .read_text(encoding="utf-8"))
    doctor = (root / "skills" / "sigma-doctor" / "scripts" / "doctor.py").read_text(encoding="utf-8")
    assert '"context": "project"' not in doctor, "doctor still advertises a nonexistent review key"
    for key in tmpl["review"]:
        assert key in doctor, "doctor's remedy line never mentions review.%s" % key


# --- P6 review findings: the fail-open contract was prose, not behaviour ---------------------------

def test_malformed_config_shapes_resolve_inline_and_never_raise():
    """`resolve`'s docstring says "Never raises" and the module docstring promises every failure
    resolves inline with a reason. _config() guarded only the read+parse; every .get/.strip after it
    was unguarded. A list-valued `command` is not contrived -- the resolver's OWN `process` output
    carries command as a list, so an operator copying one into the other crashed the gate."""
    bad = [
        {"review": {"host": "command", "command": ["cursor-agent", "-p"]}},
        {"review": {"host": 7}},
        {"review": "not-a-block"},
        ["not", "an", "object"],
        "a bare string",
    ]
    for cfg in bad:
        with tempfile.TemporaryDirectory() as d:
            base = pathlib.Path(d) / ".sdlc"; base.mkdir()
            (base / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
            got = _r().resolve(str(base), env={}, which=lambda b: None)
            assert got["mechanism"] in _r().MECHANISMS, (cfg, got)
            assert got["reason"], cfg


def test_unrun_branches_are_not_marked_verified():
    """`verified` means "RUN end to end BY THIS PROJECT" -- this module's own words. Neither the
    cursor process nor an operator command has had a review run through it, so neither may claim it.
    Marking codex unverified and then asserting its two unrun neighbours was the same defect."""
    with tempfile.TemporaryDirectory() as d:
        base = _sdlc(d, {"host": "cursor"})
        assert _r().resolve(base, env={}, which=lambda b: "/usr/bin/" + b)["verified"] is False
        base = _sdlc(d, {"host": "auto", "command": "my-agent -p"})
        assert _r().resolve(base, env={}, which=lambda b: None)["verified"] is False


def test_independent_false_is_honoured_by_the_resolver():
    """doctor reports "a fresh reviewer is not spawned" for this config while the resolver said
    `subagent`, so the loop path and the direct-invocation path disagreed about the same flag."""
    with tempfile.TemporaryDirectory() as d:
        base = _sdlc(d, {"independent": False, "host": "auto"})
        got = _r().resolve(base, env={"CLAUDECODE": "1"}, which=lambda b: None)
        assert got["mechanism"] == "inline"
        assert "independent" in got["reason"]


def test_explicit_claude_host_without_a_marker_degrades_like_every_other_host():
    """cursor/codex degrade loudly when their CLI is absent; claude had no check at all, so a host
    that cannot spawn subagents was handed `subagent` and would self-review under a
    "dispatched subagent" stamp -- the exact failure this goal exists to prevent."""
    with tempfile.TemporaryDirectory() as d:
        base = _sdlc(d, {"host": "claude"})
        got = _r().resolve(base, env={}, which=lambda b: None)
        assert got["mechanism"] == "inline"
        # assert the REASON, not just the mechanism: without the claude branch this falls through to
        # the generic "no host marker" return, which is also inline -- so a mechanism-only assertion
        # passes against the bug. Caught by running the control.
        assert got["host"] == "claude", got
        assert "CLAUDECODE" in got["reason"], got


def test_resolver_carries_the_timeout_the_prose_tells_the_agent_to_apply():
    """Both skills say to run the resolved command "bounded by review.timeout_seconds", but the
    JSON had no such key -- the agent would have to open config.json itself, which no prose says."""
    with tempfile.TemporaryDirectory() as d:
        base = _sdlc(d, {"host": "cursor", "timeout_seconds": 120})
        assert _r().resolve(base, env={}, which=lambda b: "/x/" + b)["timeout_seconds"] == 120
        base = _sdlc(d, {"host": "cursor"})
        assert _r().resolve(base, env={}, which=lambda b: "/x/" + b)["timeout_seconds"] == 900
