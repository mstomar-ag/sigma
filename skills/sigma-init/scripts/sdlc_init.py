#!/usr/bin/env python3
"""Deterministic, idempotent scaffolder for the per-project .sdlc/ layer.
Copies templates/**/<x>.tmpl -> <target>/.sdlc/<x>, skip-if-exists. Zero deps."""
import sys, json, pathlib, os, subprocess, tempfile

# script is at skills/sigma-init/scripts/sdlc_init.py; templates sit beside scripts/
TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "templates"
GITHUB_TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "github-templates"

# script is at skills/sigma-init/scripts/sdlc_init.py; the sibling sigma-setup skill sits beside it
SETUP_SCRIPT = pathlib.Path(__file__).resolve().parent.parent.parent / "sigma-setup" / "scripts" / "setup.py"


class RuntimeIgnoreWriteFailed(Exception):
    """Raised by _ignore_runtime_dirs() when the runtime dirs could not be git-ignored (#2626).
    Never swallowed anywhere in this module -- a caller that let this pass silently would leave
    `.sdlc/events/` and friends committable, the exact bug this fix exists to close. By the time
    this can raise, scaffold()'s template-writing loop has already finished and every template
    write is skip-if-exists, so a rerun after fixing the cause repeats no work."""


def _ignore_runtime_dirs(target_dir):
    """Shell out to the sibling sigma-setup skill's own CLI -- never a cross-skill Python import
    (skills do not import each other's Python; a skill needing a sibling shells out to that
    sibling's CLI, #2626). No timeout: a synchronous, local, no-network write the operator is
    already blocking on; a fixed constant here would be a limit copied from a laptop, not
    measured. Raises RuntimeIgnoreWriteFailed on any failure -- never prints and returns --
    because scaffold()'s two real callers (main() and wizard_actions.run_scaffold()) each need
    to notice this in their own idiom (a nonzero CLI exit vs. an {"ok": False} dict), and only an
    exception lets each one do that. setup.py itself is unmodified by this goal (Rev 8): any
    failure inside its ensure_ignore() -- e.g. a directory named .gitignore -- surfaces here as
    an ordinary nonzero CLI exit (Python's own default behavior on an uncaught exception), no
    extra code needed."""
    try:
        proc = subprocess.run(
            [sys.executable, str(SETUP_SCRIPT), "ignore", str(target_dir), "--scope", "tracked"],
            capture_output=True, text=True)
    except OSError as exc:          # defensive: sys.executable itself unusable
        raise RuntimeIgnoreWriteFailed(
            f"could not start the git-ignore write ({exc}) -- the .sdlc templates under "
            f"{target_dir} are already written and safe to keep; rerun this same /sigma-init "
            f"command once the underlying cause is fixed (already-written templates are "
            f"skipped, not overwritten)") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeIgnoreWriteFailed(
            f"git-ignore write failed (exit {proc.returncode}): {detail} -- the .sdlc templates "
            f"under {target_dir} are already written and safe to keep; rerun this same "
            f"/sigma-init command once the underlying cause is fixed (already-written templates "
            f"are skipped, not overwritten)")


def repair_stale_hook_path(target_dir):
    """#614: adoption NEVER writes `core.hooksPath` (owner decision D-1). Releases before #614 did --
    naming a directory nothing creates, so git ran no hooks -- and this undoes that one stale key on
    the next /sigma-init. setup.py owns the policy (`hooks-repair`); this relays its lines. Never
    raises and never refuses adoption: a repair that cannot run says so in one line with the manual
    command, and adoption carries on. Returns the lines to print (usually none)."""
    if not SETUP_SCRIPT.is_file():
        return []
    try:
        proc = subprocess.run([sys.executable, str(SETUP_SCRIPT), "hooks-repair", str(target_dir)],
                              capture_output=True, text=True)
    except OSError:
        proc = None
    if proc is not None and proc.returncode == 0:
        return [line for line in proc.stdout.splitlines() if line.strip()]
    return ["  [warn] git hooks: could not check for a stale core.hooksPath an earlier Sigma init "
            "set; if `git config --local --get core.hooksPath` prints that old value and no such "
            "directory exists, run `git config --local --unset core.hooksPath`"]


def scaffold(target_dir):
    target = pathlib.Path(target_dir)
    sdlc = target / ".sdlc"
    project_name = target.resolve().name
    created, skipped = [], []
    for tmpl in sorted(TEMPLATES.rglob("*.tmpl")):
        rel = tmpl.relative_to(TEMPLATES).with_name(tmpl.name[:-len(".tmpl")])  # strip literal .tmpl
        dest = sdlc / rel
        if dest.exists():
            skipped.append(str(rel))
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(tmpl.read_text(encoding="utf-8").replace("{{PROJECT_NAME}}", project_name), encoding="utf-8")
        created.append(str(rel))
    if "config.json" in created:
        # #228: the template cannot know a command, so it ships enforce OFF; record WHY here, naming
        # the detected candidate (if any) and the exact gesture that confirms it. Enforce turns ON
        # only through a confirmed command -- never ON with an empty one (every `done` refused).
        vd = _verify_detect()
        vd.write_verify(sdlc, None, vd.unconfirmed_why(vd.detect(target)))
    _ignore_runtime_dirs(target)
    return created, skipped


def _verify_detect():
    """The sibling verify_detect.py, loaded by path: this module is itself loaded by path from
    wizard_actions.py, where the scripts dir is not on sys.path."""
    import importlib.util
    path = pathlib.Path(__file__).resolve().parent / "verify_detect.py"
    spec = importlib.util.spec_from_file_location("verify_detect", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_report(target_dir):
    """Lines /sigma-init prints about the verify command, on every host: the detected candidate and
    the exact gesture (Codex/Cursor have no interactive question, so this IS their prompt), or a
    loud warning when an existing config holds the permanent-refusal trap."""
    cfgp = pathlib.Path(target_dir) / ".sdlc" / "config.json"
    try:
        verify = json.loads(cfgp.read_text(encoding="utf-8")).get("verify") or {}
    except (OSError, ValueError, AttributeError):
        return []
    vd = _verify_detect()
    if verify.get("command"):
        return [f"sigma-init: verify command - `{vd.printable(verify['command'])}` "
                f"(enforce {'ON' if verify.get('enforce') else 'OFF'})."]
    enforce = verify.get("enforce")        # read generously, as loop.py's _enforce_enabled does
    if isinstance(enforce, str):
        enforce = enforce.strip().lower() not in ("", "false", "0", "no", "off")
    skipped = []
    # One coherent message: on the trap, proposal_lines leads with the WARNING and describes
    # confirm/decline against enforce ON -- it never also claims enforce is OFF.
    # The gestures name this .sdlc by absolute path (abspath: a symlinked .sdlc is left as named, and
    # verify_detect refuses it loudly), so a line pasted from any directory reaches it.
    sdlc = os.path.abspath(os.path.join(str(target_dir), ".sdlc"))
    return vd.proposal_lines(vd.detect(target_dir, skipped), skipped, trap=bool(enforce), sdlc=sdlc)


#: #229 test seams: the runner/which `main()`'s preflight uses (None = the real ones). Tests set them
#: on the module they loaded; nothing in production assigns them.
PREFLIGHT_RUNNER = None
PREFLIGHT_WHICH = None


def _preflight():
    """The sibling preflight.py (#229), loaded by path like verify_detect above."""
    import importlib.util
    path = pathlib.Path(__file__).resolve().parent / "preflight.py"
    spec = importlib.util.spec_from_file_location("preflight", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git_refusal(target_dir, runner=None, which=None):
    """#229: None when `target_dir` is inside a git work tree (a fresh `git init` with no commit yet
    passes here and is reported by `preflight_report`); else the lines /sigma-init
    prints (stderr) before REFUSING -- nothing is written into a directory the loop cannot use."""
    pf = _preflight()
    import shutil
    check = pf.check_git(str(pathlib.Path(target_dir).resolve()),
                         runner or PREFLIGHT_RUNNER or pf.real_runner,
                         which or PREFLIGHT_WHICH or shutil.which, pf.call_timeout())
    if check["ok"] is True or check.get("note") == "no-commit":
        return None           # a fresh `git init` is a normal start: reported after the scaffold
    return (["sigma-init: REFUSED - nothing written. The loop needs a git repository (a worktree and "
             "a branch per goal are cut from it)."]
            + pf.failure_lines(check) + ["  Then re-run /sigma-init."])


def preflight_report(target_dir, runner=None, which=None, checks=None):
    """#229: the preflight lines /sigma-init prints after scaffolding, on every host: each problem
    with one remediation line per host and what Sigma does meanwhile, plus the work.enabled DECISION
    when work is on but nothing can be pushed. Prints and flips nothing itself. `checks` (#236):
    results the caller already measured (init_flow.py reads them for its exit status), so the
    network checks are not run twice."""
    pf = _preflight()
    sdlc = os.path.abspath(os.path.join(str(target_dir), ".sdlc"))
    try:
        cfg = json.loads(pathlib.Path(sdlc, "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    if checks is None:
        checks = pf.preflight(str(pathlib.Path(target_dir).resolve()),
                              cfg if isinstance(cfg, dict) else {},
                              runner=runner or PREFLIGHT_RUNNER, which=which or PREFLIGHT_WHICH)
    lines = pf.report_lines(checks)
    if not pf.requirements(cfg if isinstance(cfg, dict) else {})["work"]:
        return lines
    by_id = {c["id"]: c for c in checks}
    remote, gh = by_id.get("remote") or {}, by_id.get("gh-installed") or {}
    if remote.get("ok") is False:
        lines += pf.decision_lines(sdlc, remote.get("remotes") or ())
    elif gh.get("ok") is False:
        lines += pf.decision_lines(sdlc, why="no-gh", commands_printed=bool(gh.get("commands")))
    elif (by_id.get("gh-auth") or {}).get("note") == "non-github":
        lines += pf.decision_lines(sdlc, why="non-github")
    return lines


_DEMO_GOAL = """---
id: 0000
title: "Demo - write a Sigma hello note"
lane: auto
done_when: "sigma-demo.md exists with a one-line note"
auto_ok: true
status: pending
verify_command: {python} -c "import pathlib,sys; p=pathlib.Path(sys.argv[1]); sys.exit(0 if p.is_file() and p.read_text().strip() else 1)" sigma-demo.md
---

A throwaway demo goal so you can watch the SDLC run end to end. Create
`sigma-demo.md` containing a single line noting that Sigma ran this goal
through Goal -> Research -> Plan -> Plan-Review -> Implement -> Review. Delete this
goal file once you've seen it work.

`verify_command` above is this goal's machine-checked done_when: `loop.py verify` runs it (it wins
over config `verify.command`), and `record done` needs it green whenever `verify.enforce` is on.
"""


def scaffold_demo(target_dir):
    """Queue a small, safe, runnable demo goal so `/sigma-loop` shows the SDLC immediately.
    Returns True if written, False if it already exists (never clobbered)."""
    dest = pathlib.Path(target_dir) / ".sdlc" / "goals" / "0000-demo.md"
    if dest.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    # #228: the interpreter name is resolved on THIS machine (python3 / python / py), so the demo's
    # verify_command runs on a Windows install that has no `python3` on PATH.
    dest.write_text(_DEMO_GOAL.replace("{python}", _verify_detect().python_command()), encoding="utf-8")
    return True


_NORTH_STAR = """# {{PROJECT_NAME}} - North Star

The product context that grounds every goal. Fill the tiers top-down and keep each short - this is
direction, not a spec. `/sigma-context` recalls this first; `sigma-plan-review` checks plans against it.

## Vision (why this exists, for whom)
<the change you want to make in the world, and who it's for>

## Strategy (what we're building now)
- Priorities: <the few things that matter this cycle>
- Non-goals: <what we are deliberately NOT doing - the alignment gate uses these>

## Design (how the product should feel)
<the experience + the principles a change must respect>

## Architecture (how it's built + the rules we develop by)
<the shape of the system - the stack itself lives in project.md. Then the **rules** that govern changes
as a NUMBERED, checkable list: plan-review enforces these (a plan that violates one is blocked). Unlike
the tiers above, this tier can be AI-drafted from the codebase and user-approved.>
1. <e.g. the UI layer holds no business logic>
2. <e.g. dependencies point inward; no sibling imports across modules>
"""


def scaffold_vision(target_dir):
    """Scaffold the opt-in vision-first north-star (.sdlc/context/north-star.md), skip-if-exists.
    Opt-in via --vision so plain /sigma-init stays drop-in. Returns True if written, False if present."""
    target = pathlib.Path(target_dir)
    dest = target / ".sdlc" / "context" / "north-star.md"
    if dest.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(_NORTH_STAR.replace("{{PROJECT_NAME}}", target.resolve().name), encoding="utf-8")
    return True


_CURSOR_RULE = """---
description: Goal-Based SDLC - standing discipline for every change (Sigma Loop)
globs:
alwaysApply: true
---

# Goal-Based SDLC (Sigma Loop)

Cursor has no UserPromptSubmit hook, so this always-applied rule is the standing policy. For any
non-trivial or implementation task, do NOT jump straight to coding - follow the phases and state which
one you are on:

1. **Goal** - restate the objective as one concrete, checkable goal.
2. **Research** - blast radius: affected files, existing patterns, constraints.
3. **Plan** - steps, files, tests, definition-of-done.
4. **Plan-Review** - adversarially review the plan BEFORE implementing. Never skip.
5. **Implement** - test-first (red -> green -> refactor); minimal code to pass.
6. **Review** - evidence before claims: run the checks, paste the output; a KPI + qualitative scan.
7. **Retrospective** - capture lessons.

**Intent-aware:** trivial / conversational / read-only requests may be answered directly - but say so
explicitly. The moment it turns into a code change, switch to the spine.

**Never run an irreversible or expensive action** (deploy, delete, overwrite, spend, migrate)
unattended - stop and ask.

**Nobody commits directly to a feature branch. All work reaches it through `sdlc/*` goal
branches.** Concretely: the `sdlc/<goal-id>` branch cut for a goal, then a pull request into
`feature/<name>`. Rebasing a shared branch rewrites published history, which normally forces every
holder to hard-reset; that risk disappears only because nobody is holding commits on it.

What Sigma does is MEASURE the branch before it force-pushes, and refuse to bring it forward
when it finds a commit no pull request accounts for. It checks only on a pick, and only when the
branch is BEHIND its base -- when it skips, it reports nothing at all, so silence is not evidence
the rule held. Sigma cannot stop a direct commit; only branch protection on the host can, and
protecting `feature/*` in the ordinary way blocks the force-push upkeep itself needs, which stops
upkeep entirely. Protect the integration branch; on `feature/*`, grant upkeep's actor a bypass.

To rebase, catch up or land a unit (a `feature/<name>` branch), use the `sigma-rebase` skill; do not run a raw rebase, force-push or merge by hand. Landing is never automatic: the skill asks for an explicit yes first.

**Executors (portable):** this host has no `superpowers` / `code-review` companion, so each phase runs
via Sigma's portable executor - the disciplines in `skills/sigma-*/SKILL.md` (`sigma-brainstorm` ->
Goal, `sigma-plan` -> Plan, `sigma-implement` -> Implement, `sigma-review` + `sigma-verify` -> Review).
Same discipline as the Claude companions, portable.

**Backlog + helpers (optional):** the loop, model-selection, status and KG helpers are plain, zero-dep
`python3` - run them from your Sigma checkout via Cursor's terminal, e.g.
`python3 <sigma>/skills/sigma-loop/scripts/loop.py next .sdlc` or
`python3 <sigma>/skills/sigma-model/scripts/predict.py "<goal>"`.
Status output is the sibling rule `output-contract.mdc`: every status block is constructed by
`python3 <sigma>/skills/sigma-loop/scripts/render.py`, never written by hand.
"""

#: The second always-apply Cursor rule (#2114): status output is CONSTRUCTED by render.py, never
#: written by hand. Pure ASCII on purpose -- it NAMES the contract's markers, separator and arrows
#: and points at `docs/output-contract.md` for them; the source file is read as ASCII by
#: `tests/test_sdlc_init.py`. This repo's own `.cursor/rules/output-contract.mdc` is this text byte
#: for byte (single source; pinned by the same tests), so `<sigma>` reads as `.` there.
_CURSOR_OUTPUT_RULE = """---
description: Status output is constructed by Sigma's render.py, never hand-written (blocks, triggers, refusal)
globs:
alwaysApply: true
---

# Output contract (Sigma Loop)

All status reporting in this repo follows `<sigma>/docs/output-contract.md`, and that document is
the SPECIFICATION of a renderer, not prose to imitate. A status block is CONSTRUCTED by
`<sigma>/skills/sigma-loop/scripts/render.py` from facts you pass it, and never written by hand.
(`<sigma>` is your Sigma checkout; inside the Sigma repository itself it is `.`.)

Three commands, run from Cursor's terminal:

- `python3 <sigma>/skills/sigma-loop/scripts/render.py status|event|decision` -- the facts are one
  JSON object on stdin, or inline as `--json '<facts>'`. `status` builds Block A (STATUS), `event`
  Block B (EVENT), `decision` Block C (DECISION). Relay its stdout exactly as printed.
- `python3 <sigma>/skills/sigma-log/scripts/log.py slots .sdlc` -- the live Block A: one two-line
  slot per active goal, read from the action log and built by render.py. Relay it verbatim; do not
  reword it, re-order it, or add a slot. When it says the log is off or unknown, relay that too.
- `python3 <sigma>/skills/sigma-loop/scripts/phase_report.py end .sdlc <goal> <phase> --pid "$PPID"` -- prints
  Block B at a phase boundary, carrying that phase's measured cost or an honest "unavailable" line.
  Read it; do not restate its numbers or imitate its shape.

A refusal is `render.py: REFUSED [<code>] at <path>: <detail>` on stderr, exit 2, and nothing on
stdout. It means the facts were malformed: fix the facts and run it again. Never patch the prose by
hand, and never emit a block the renderer did not build.

Emit only on a trigger, one block each: work dispatched or a slot refilled (A); a phase boundary
crossed (B); a background task or agent completing (B, then A if slot state changed); a merge
landing (B); a decision needed from the human (C); end of turn with nothing in flight (A). Starting
or finishing a tool call, reading files, thinking and every other intermediate step are not
triggers. If no trigger fires, emit nothing.

Dispatch labels follow the same identity: every subagent label, task title and branch name carries
the goal ref and the phase.

One limit, stated rather than discovered: the constructed line is unforgeable in shape, but it is
not guaranteed to reach the user unparaphrased -- anywhere a reply is read rather than the raw tool
output, it can still be paraphrased -- so relay the block exactly as printed and add nothing around it.

This governs status output only. Content the user explicitly asked for (a report, a walkthrough, an
explanation) is given in full.
"""


#: The always-apply Cursor rules `--cursor` writes, in write order. Each is skip-if-exists on its
#: own, so an adopter who already carries one still receives the other.
_CURSOR_RULES = (("sdlc.mdc", _CURSOR_RULE), ("output-contract.mdc", _CURSOR_OUTPUT_RULE))


def scaffold_cursor_rules(target_dir):
    """Write each rule in `_CURSOR_RULES` into <target>/.cursor/rules/, skip-if-exists PER FILE.
    Returns (created, skipped) as file names -- the same shape as `scaffold_github`."""
    rules_dir = pathlib.Path(target_dir) / ".cursor" / "rules"
    created, skipped = [], []
    for name, text in _CURSOR_RULES:
        dest = rules_dir / name
        if dest.exists():
            skipped.append(name)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        created.append(name)
    return created, skipped


def scaffold_cursor(target_dir):
    """Scaffold the Cursor host adapter: two always-apply rules under .cursor/rules/ -- `sdlc.mdc`
    carrying the SDLC discipline (Cursor's analog of the Claude UserPromptSubmit hook) and
    `output-contract.mdc` pointing status output at render.py instead of at prose to imitate
    (#2114). Opt-in via --cursor, each file skip-if-exists. Returns True if anything was written,
    False if both were already present."""
    created, _ = scaffold_cursor_rules(target_dir)
    return bool(created)


_CODEX_RULE = """<!-- sigma:codex:start -->
## Sigma Loop on Codex

For a non-trivial implementation task, use the installed Sigma skills and run Goal -> Research
-> Plan -> Plan-Review -> Implement -> Review -> Retrospective. Follow each skill's phase gates,
record the evidence, and keep the maker and checker in separate contexts. Read-only and trivial
requests may be answered directly. Use the project `.sdlc/config.json` for optional features.

Codex does not set `CLAUDE_SKILL_DIR`. Before running a Sigma skill command, take the absolute
directory containing that skill's `SKILL.md` from the installed skill path and substitute it for
`${CLAUDE_SKILL_DIR}` in that command. Do this for every independent shell call. An empty variable
would point at `/scripts/...`; never execute that unresolved path. Do not infer the plugin's versioned
cache directory from another machine.

Model tiers in the shared ledger are `haiku`, `sonnet`, `opus`, and `fable`. They are relative work
tiers, not Codex model IDs. Before every Codex subagent dispatch, run
`python3 "<absolute sigma-model skill directory>/scripts/predict.py" host-model codex "<tier-or-off>" .sdlc --effort "<effort>"`.
Pass its exact `model=` and the same selected `effort=` value to the subagent; never pass an Anthropic tier as a
model ID. If it refuses the mapping, do not dispatch: choose an approved current Codex model in the
affected `model_host_overrides.codex` entry, or update the plugin for a changed host catalog, then
rerun the resolver.

Nobody commits directly to a feature branch. All work reaches it through `sdlc/*` goal branches.
Use the `sdlc/<goal-id>` branch for a goal, then a pull request into `feature/<name>`. Sigma's
check runs only on a pick when that branch is behind its base; silence is not evidence it ran.
Only branch protection can prevent a direct commit. If protecting `feature/*`, grant upkeep's actor
a bypass for its force-push.
To rebase, catch up or land a unit (a `feature/<name>` branch), use the `sigma-rebase` skill; do not run a raw rebase, force-push or merge by hand.
<!-- sigma:codex:end -->
"""


def _legacy():
    """The shared previous-name helper (#239), loaded by path from the sibling sigma-loop skill --
    the same cross-skill idiom `doctor._load_loop_script` uses."""
    import importlib.util
    path = pathlib.Path(__file__).resolve().parent.parent.parent / "sigma-loop" / "scripts" / "legacy.py"
    spec = importlib.util.spec_from_file_location("legacy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _coexist():
    """The coexistence detector (#240), loaded by path like `_legacy` above."""
    import importlib.util
    path = pathlib.Path(__file__).resolve().parent.parent.parent / "sigma-loop" / "scripts" / "coexist.py"
    spec = importlib.util.spec_from_file_location("coexist", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def codex_block_update(existing, dest="AGENTS.md"):
    """-> the new AGENTS.md text with Sigma's Codex block in place, or None when it is already
    current. Pure: `scaffold_codex` writes the result, `migrate.py` previews and writes it too.

    WHICH PAIR IS OURS (#239). The Sigma pair when the file carries any Sigma Codex marker; the
    plugin's PREVIOUS-name pair only when it carries none -- that block is this adapter's own text
    from before the rename, so it is replaced in place like any owned block. With a Sigma block
    already present, a previous-name block beside it is left alone: it belongs to a teammate's old
    plugin, not to this one. Raises ValueError on a malformed or reversed pair (never guesses)."""
    start, end = "<!-- sigma:codex:start -->", "<!-- sigma:codex:end -->"
    if start not in existing and end not in existing:
        legacy = _legacy()
        start, end = legacy.retired_spelling(start), legacy.retired_spelling(end)
    starts, ends = existing.count(start), existing.count(end)
    if starts > 1 or ends > 1 or (ends and not starts):
        raise ValueError(f"malformed Sigma Codex managed block in {dest}")
    if starts and ends:
        first, last = existing.index(start), existing.index(end) + len(end)
        if first >= existing.index(end):
            raise ValueError(f"reversed Sigma Codex markers in {dest}")
        current = existing[first:last]
        if current == _CODEX_RULE.rstrip("\n"):
            return None
        # This is an owned block: refresh it when the installed plugin changes while preserving
        # every byte outside the markers. A project can keep its own rules before or after it.
        return existing[:first] + _CODEX_RULE.rstrip("\n") + existing[last:]
    if starts:
        # Recover a previous interrupted append. Only the managed partial tail is replaced.
        existing = existing[:existing.index(start)].rstrip("\n") + "\n"
        return existing + "\n" + _CODEX_RULE
    separator = "\n" if existing and not existing.endswith("\n") else ""
    if existing:
        separator += "\n"
    return existing + separator + _CODEX_RULE


def scaffold_codex(target_dir):
    """Add a managed Codex rule block without replacing a project's existing AGENTS.md."""
    dest = pathlib.Path(target_dir) / "AGENTS.md"
    if dest.is_symlink():
        raise ValueError(f"refusing to replace a symlinked AGENTS.md: {dest}")
    existing = dest.read_text(encoding="utf-8") if dest.exists() else ""
    updated = codex_block_update(existing, dest)
    if updated is None:
        return False
    data = updated.encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=".AGENTS.md.sigma-", dir=str(dest.parent))
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        if dest.exists():
            os.chmod(tmp, dest.stat().st_mode & 0o777)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return True


def scaffold_github(target_dir):
    """Materialize the GitHub PM scaffolding (issue templates, auto-add workflow, label rule, the
    critical-insight template) into <target>/.github/, skip-if-exists. Opt-in via the --github flag."""
    target = pathlib.Path(target_dir)
    project_name = target.resolve().name
    created, skipped = [], []
    for tmpl in sorted(GITHUB_TEMPLATES.rglob("*.tmpl")):
        rel = tmpl.relative_to(GITHUB_TEMPLATES).with_name(tmpl.name[:-len(".tmpl")])
        dest = target / ".github" / rel
        if dest.exists():
            skipped.append(str(rel))
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(tmpl.read_text(encoding="utf-8").replace("{{PROJECT_NAME}}", project_name), encoding="utf-8")
        created.append(str(rel))
    return created, skipped


def bootstrap_github_labels(target_dir):
    """#230: github mode creates the `sdlc:*` and `priority:P0`-`P3` labels NOW, before the first
    pick -- a repo without `sdlc:goal` reads as an empty backlog forever. Shells out to the sibling
    sigma-setup CLI (never a cross-skill import, #2626): `setup.py detect` for the origin's
    `owner/name`, then `setup.py labels --repo`, which prints one line per label (created /
    existed / FAILED: reason) and "ensured" only when every label was measured present. No GitHub
    remote: says so and writes nothing (`loop.py start` bootstraps again once the repo is set).
    Returns False -- init exits 1, before the remaining optional steps; every step is
    skip-if-exists, so rerunning once the token can write labels completes them -- when any label
    failed."""
    repo = _detect_repo(target_dir)
    if not repo:
        print("\nsigma-init: labels not created - no GitHub `origin` remote detected. Choose github "
              "mode with its repository (`init_flow.py --mode github --repo O/N`, #236); `loop.py "
              "start` creates them before the first pick.")
        return True
    print(f"\nsigma-init: labels on {repo}")
    proc = subprocess.run([sys.executable, str(SETUP_SCRIPT), "labels",
                           str(pathlib.Path(target_dir) / ".sdlc"), "--repo", repo],
                          capture_output=True, text=True)
    sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        print("sigma-init: stopping - required labels could not be created (see above). Fix the "
              "token's label-write permission and rerun; already-written files are kept.",
              file=sys.stderr)
        return False
    return True


BOARD_SETUP = pathlib.Path(__file__).resolve().parent / "board_setup.py"


def _detect_repo(target_dir):
    """`owner/name` of the GitHub origin via the sibling `setup.py detect` (local; no network)."""
    try:
        return subprocess.run([sys.executable, str(SETUP_SCRIPT), "detect", str(target_dir)],
                              capture_output=True, text=True).stdout.strip()
    except OSError:
        return ""


def board_offer(target_dir, github_flag):
    """#235: the lines that OFFER a GitHub Project board -- github mode only (`--github`, or
    `discovery.source: github`), never in local-goals mode, and never when a number is already
    pinned. It makes no gh call and changes nothing: the board is created only by the printed
    `board_setup.py create ... --yes`, which Claude Code runs after asking and Codex/Cursor users
    run themselves."""
    sdlc = os.path.abspath(os.path.join(str(target_dir), ".sdlc"))
    try:
        cfg = json.loads(pathlib.Path(sdlc, "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    disc = cfg.get("discovery") if isinstance(cfg, dict) and isinstance(cfg.get("discovery"), dict) else {}
    if not (github_flag or disc.get("source") == "github"):
        return []
    gh = disc.get("github") if isinstance(disc.get("github"), dict) else {}
    proj = gh.get("project") if isinstance(gh.get("project"), dict) else {}
    if proj.get("number"):
        return [f"sigma-init: board - project #{proj.get('number')} is pinned "
                f"(discovery.github.project.number); nothing to set up."]
    repo = str(gh.get("repo") or "").strip() or _detect_repo(target_dir)
    if not repo or "/" not in repo:
        return ["sigma-init: board - not offered: no GitHub `origin` remote detected (set "
                "discovery.github.repo, then run board_setup.py create <.sdlc>)."]
    vd = _verify_detect()
    cmd = f"{vd.python_command()} {vd._q(str(BOARD_SETUP))} create {vd._q(sdlc)}"
    owner, name = repo.split("/", 1)
    title = proj.get("title") or f"{name} \u2014 SDLC"
    return [
        "sigma-init: GitHub Project board - none is pinned (discovery.github.project.number is unset).",
        f"  OFFER: create '{title}' under {owner}, linked to {repo}, with the Status columns "
        "(project.columns) and a Priority field, and pin its number in config.json.",
        "  Nothing is created unless you say yes. It needs the gh `project` scope, and it refuses "
        "(printing a manual runbook) if that title already exists.",
        f"  Preview (read-only): {cmd}",
        f"  Claude Code: the agent asks you yes/no; on yes it runs: {cmd} --yes",
        f"  Codex / Cursor: to have the board, run it yourself: {cmd} --yes",
    ]


def _demo_board_hint(target):
    """What `/sigma-loop` does with a board, as config.json says NOW (#236: `/sigma-init` leaves
    `project.enabled` off until the user says `--board yes`, so "the loop creates the board" is not
    true by default any more)."""
    try:
        cfg = json.loads((pathlib.Path(target) / ".sdlc" / "config.json").read_text(encoding="utf-8"))
        proj = cfg["discovery"]["github"]["project"]
    except (OSError, ValueError, KeyError, TypeError):
        proj = {}
    if not isinstance(proj, dict) or not proj.get("enabled"):
        return ("`/sigma-loop` picks it (no board: mirroring is off; `/sigma-init --board yes` "
                "creates and pins one).")
    if proj.get("number"):
        return f"`/sigma-loop` moves its card on board #{proj['number']} Backlog -> ... -> Done."
    return ("`/sigma-loop` finds or creates the board on its first pick and moves the card "
            "Backlog -> ... -> Done.")


def scaffold_extras(target, flags):
    """The opt-in scaffolds (`--demo`, `--vision`, `--cursor`, `--codex`), each skip-if-exists and
    each reporting one line. Shared by `main()` and `init_flow.py` (#236), so the one entry point
    and this low-level scaffolder never diverge on what a flag writes."""
    if "--demo" in flags:
        if scaffold_demo(target):
            print("\nsigma-init: demo goal queued - `.sdlc/goals/0000-demo.md`. Run `/sigma-loop` to watch "
                  "the SDLC run it end to end (Goal -> Research -> ... -> Review).")
            if "--github" in flags:
                print("  github mode: file it as an issue - `gh issue create --label sdlc:goal "
                      "--title \"[Demo] Sigma\" --body \"<paste the demo goal body>\"` - then "
                      + _demo_board_hint(target))
        else:
            print("\nsigma-init: demo goal already present (kept).")
    if "--vision" in flags:
        if scaffold_vision(target):
            print("\nsigma-init: vision-first north-star queued - `.sdlc/context/north-star.md`. Run "
                  "`/sigma-vision` to fill the tiers (Vision -> Strategy -> Design -> Architecture); "
                  "`/sigma-context` then grounds every goal in it.")
        else:
            print("\nsigma-init: north-star already present (kept).")
    if "--cursor" in flags:
        rules_created, rules_skipped = scaffold_cursor_rules(target)
        # Cursor never has the superpowers/code-review companions - pin companions:off so the portable
        # executors are used without a pointless `claude plugin list` probe (fail-open if config's odd).
        try:
            cfgp = pathlib.Path(target) / ".sdlc" / "config.json"
            cfg = json.loads(cfgp.read_text(encoding="utf-8"))
            if cfg.get("companions") != "off":
                cfg["companions"] = "off"
                _verify_detect()._atomic_write_json(cfgp, cfg)       # #625: never a truncating write
        except Exception:
            pass
        if rules_created:
            print("\nsigma-init: Cursor adapter queued - "
                  + ", ".join("`.cursor/rules/%s`" % n for n in rules_created)
                  + " (always-applied rules; `sdlc.mdc` is the SDLC discipline, the hook analog, "
                  "and `output-contract.mdc` points status output at render.py). companions pinned "
                  "off (portable executors). The zero-dep python helpers run from your Sigma "
                  "checkout via Cursor's terminal.")
            for s in rules_skipped:
                print("  = .cursor/rules/%s (exists, kept)" % s)
        else:
            print("\nsigma-init: Cursor rules already present (kept).")
    if "--codex" in flags:
        if scaffold_codex(target):
            print("\nsigma-init: Codex adapter written to `AGENTS.md` (other rules preserved).")
        else:
            print("\nsigma-init: Codex rule already present (kept).")


USAGE = "usage: sdlc_init.py [target_dir] [--github] [--codex] [--cursor] [--vision] [--demo]"


def main(argv):
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    flags = {a for a in argv[1:] if a.startswith("--")}
    pos = [a for a in argv[1:] if not a.startswith("--")]
    target = pos[0] if pos else "."
    if not pathlib.Path(target).is_dir():
        print(f"sigma-init: target directory does not exist: {target}", file=sys.stderr)
        return 1
    refusal = git_refusal(target)                 # #229: before anything is written
    if refusal:
        for line in refusal:
            print(line, file=sys.stderr)
        return 2
    # #240/#314: the plugin under the previous name active here is a NOTICE (one line naming the
    # uninstall command), never a refusal; a repository it adopted is offered the migrate dry run
    # (never applied from here). An sigma-init copied without its sigma-loop sibling cannot check:
    # said aloud, then it proceeds.
    try:
        coexist = _coexist()
    except Exception as exc:                 # noqa: BLE001 - see above
        coexist = None
        print(f"sigma-init: warning: cannot check for a second plugin on this repository "
              f"({type(exc).__name__}); run `coexist.py check` once sigma-loop is installed",
              file=sys.stderr)
    if coexist is not None:
        coexist.gate(str(pathlib.Path(target) / ".sdlc"), "sigma-init")
        offer = coexist.takeover_line(str(pathlib.Path(target) / ".sdlc"))
        if offer:
            print(offer, file=sys.stderr)
    for line in repair_stale_hook_path(target):     # #614: never writes the key, only undoes it
        print(line)
    try:
        created, skipped = scaffold(target)
    except RuntimeIgnoreWriteFailed as exc:
        print(f"sigma-init: {exc}", file=sys.stderr)
        return 1
    if coexist is not None:
        coexist.write_owner(pathlib.Path(target) / ".sdlc")  # #240: Sigma owns this state dir
    root = pathlib.Path(target).resolve()
    print(f"sigma-init: {len(created)} created, {len(skipped)} skipped (target: {root})")
    for c in created:
        print(f"  + .sdlc/{c}")
    for s in skipped:
        print(f"  = .sdlc/{s} (exists, kept)")
    if created:
        print("\nTip: commit .sdlc/goals/, .sdlc/project.md and .sdlc/config.json. The machine-written "
              "dirs ('.sdlc/state/', '.sdlc/ledger/', '.sdlc/work/', '.sdlc/knowledge/') are now "
              "git-ignored automatically (`setup.py ignore`, the same mechanism /sigma-setup uses). To "
              "change the ignore scope later (tracked vs. local-only), move the lines from "
              ".gitignore to .git/info/exclude by hand -- /sigma-setup never relocates an existing "
              "rule.")
    if "--github" in flags:
        gcreated, gskipped = scaffold_github(target)
        print(f"\nsigma-init: GitHub PM scaffolding - {len(gcreated)} created, {len(gskipped)} skipped")
        for c in gcreated:
            print(f"  + .github/{c}")
        for s in gskipped:
            print(f"  = .github/{s} (exists, kept)")
        if not bootstrap_github_labels(target):
            return 1
        print("sigma-init: note - this low-level `--github` gesture does NOT switch the backlog to "
              "GitHub (discovery.source is unchanged). The one entry point does: "
              "`init_flow.py --mode github` (`/sigma-init`, #236).")
    scaffold_extras(target, flags)
    # #246 review 2: the verify report comes LAST, after every step above that writes files
    # (--github adds workflows, --codex AGENTS.md, --cursor .cursor/), so the candidates and ids it
    # prints are detected from the repository exactly as `confirm` will see it. The config's
    # `_why` is recomputed here for the same reason.
    # #229: git / remote / base / gh / scopes, after every flag above has settled the config.
    print()
    for line in preflight_report(target):
        print(line)
    offer = board_offer(target, "--github" in flags)      # #235: an offer only; nothing runs
    if offer:
        print()
        for line in offer:
            print(line)
    if "config.json" in created:
        vd = _verify_detect()
        vd.write_verify(pathlib.Path(target) / ".sdlc", None, vd.unconfirmed_why(vd.detect(target)))
    report = verify_report(target)
    if report:
        print()
        for line in report:
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
