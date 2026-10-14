#!/usr/bin/env python3
"""OPEN a unit of work -- the half of the branching model that had no gesture (#1662, story #1427).

`features.py` reads a declaration. `feature_labels.py` acts on one at pick. `feature_stamp.py`
writes one onto an issue Sigma files itself. Every one of them presupposes that a unit ALREADY
EXISTS, and nothing in the kit creates one: `docs/branching-model.md` §14 documents opening a unit
as four commands a human types in a fixed order, from memory, spread across `git`, `gh`, `mkdir` and
an issue body. This module is that gesture.

IT ADDS THE FOLLOWING AND DELEGATES EVERYTHING ELSE. Intake, deduplication, planning, issue
creation, assignment and the run-now decision are `sigma-scope`'s, already shipped and already
tested; growing a second copy of them here would be the hardened-sibling divergence this repo's own
retrospectives keep flagging. What `sigma-scope` structurally cannot do is supply a UNIT -- §10 note
4 says so in as many words ("there is no calling goal to inherit from -- it is a human turning an
idea into a plan"). So:

    open_unit      the ordered gesture: branch, then label, then registry
    stamp_plan     the body marker, into every issue of a scope plan, BEFORE it is filed
    declare        the label, attached AFTER filing, and then VERIFIED through the real reader
    set_priority   THE answer to "prioritise this feature" -- one value recorded on the unit's OWN
                   registry entry, read by the comparator as a tie-break; never a write to a member
                   issue (#2266, epic #2260, slice 6)
    bump_priority  a DIFFERENT, still-legitimate gesture -- levelling a unit's members by hand,
                   applied to every open member's label, exactly (#2162). No longer what either
                   setup flow asks for by default; see `set_priority` above and #2266.
    priority_shorthand  `<name> <priority>`, two bare positional args, for a unit that already
                   exists -- calls `set_priority` UNCHANGED and returns its own report; refuses,
                   rather than guessing, when `name` is not yet a registered unit (#2331).

THE ORDER IS THE FEATURE, NOT AN IMPLEMENTATION DETAIL (§14). A body marker whose label does not yet
exist REFUSES the pick and sets the goal aside with `sdlc:needs-label` -- measured twice, and
measured on a repository with no registry at all, because `attach_at_pick` never looks at
`.sdlc/features/`. §14 states the order as a requirement rather than a suggestion for exactly that
reason. So `_STEPS` is a module constant, the executor is a loop over it, and A FAILING STEP STOPS
THE ONES AFTER IT. The last part is the one an implementation gets wrong by being helpful: running
all three and reporting the failures at the end leaves a label minted for a branch that does not
exist (and feature labels are never deleted, so there is no cleanup path), or a registry directory
created -- which arms the registry half for the WHOLE REPOSITORY, not just this unit -- on the
strength of a gesture that did not complete.

THIS IS THE ONE PLACE A `feature:*` LABEL MAY BE CREATED, AND IT SAYS SO RATHER THAN DOING IT
QUIETLY. The kit's standing rule is attach-never-create (§7): Sigma attaches an existing feature
label and never mints one, because a single typo in a body marker would mint junk that outlives the
unit. That rule is not weakened here and not touched anywhere else -- it is that `sigma-define` is the
HUMAN's own gesture, made once per unit, against a name this module has already validated, and a
model whose units can only be opened by hand is a model with half a life.

    IT DELIBERATELY DOES NOT ROUTE THE CREATE THROUGH `sources.GitHubSource`, AND THAT IS A SAFETY
    DECISION RATHER THAN A CONVENIENCE. `GitHubSource._run` refuses a `gh label create` carrying a
    `feature:*` argument at the single chokepoint every `gh` call in that class passes through -- and
    it RETURNS `""`, which is the SUCCESS shape for a create. Routing this step through that class
    would therefore report a created label for a label that does not exist, and every issue this flow
    then filed would be refused at its own pick: §14's measured bug, reached from the opposite
    direction. So the label step uses this module's own injected runner, and
    `tests/test_define.py::test_the_label_create_is_exactly_the_shape_the_kit_refuses_everywhere_else`
    feeds this module's argv to the kit's own refusal predicate, so the exception cannot quietly
    stop describing the same call.

NOT A SECOND READER AND NOT A SECOND RULE. Every derived string comes from the module that owns it:
`features._is_unit_name` for what a unit may be called, `features.BRANCH_PREFIX` for the branch,
`feature_labels.label_for` for the label, `feature_stamp.stamp_body` for the marker (which already
composes it from `features`' own key constants AND feeds it back through `features.parse_body`
before returning it), `feature_registry.registry_dir` for the directory. Nothing here re-derives any
of them, and `tests/test_define.py` asserts the name predicate by OBJECT IDENTITY -- a behaviour
table is satisfied by a faithful copy, and a faithful copy is exactly what drifts when the original
is corrected.

WHY THE REGISTRY STEP IS HERE AT ALL, given that #1576 gates it. See `SKILL.md`'s own section: the
four adoption-blocking defects #1564 tracked are fixed, and #1638's residual is gone too -- every
derived key folds as of #1672 and #1673. What remains true is the reason this step exists at all: a
second casing of one unit needs TWO SPELLINGS to arise, which is precisely
what a define-time single source prevents. `open_unit` refuses a label whose casing differs from the
one asked for, and `declare` reports a case drift rather than reading it as agreement, so the two
doors that drift could enter by are both watched.

Module shape follows `feature_stamp.py` and `compile_plan.py`: siblings loaded by file path, pure
functions plus one fail-open stderr note, an injected `(cwd, argv) -> stdout` runner, and a CLI that
prints one JSON line per verb (`brainstorm.py`/`dedup.py`/`scope.py`/`assign.py resolve`'s own
convention).
"""
import copy
import importlib.util
import json
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent


def _load_loop_script(name):
    """Cross-load a script from the sibling `sigma-loop` skill -- mirrors `compile_plan.py`'s own
    `_load_loop_script` byte-for-byte (the established, narrow, named exception to "don't reach
    across skill directories" the whole scope family already relies on)."""
    path = _HERE.parent.parent / "sigma-loop" / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


features = _load_loop_script("features")                # the one rule, and the one reader
feature_labels = _load_loop_script("feature_labels")    # `label_for`, and the never-create predicate
feature_stamp = _load_loop_script("feature_stamp")      # the marker composer, self-verifying
feature_registry = _load_loop_script("feature_registry")  # `registry_dir`
feature_sync = _load_loop_script("feature_sync")        # `amend` -- the unit's own priority write surface (#2266)
sources = _load_loop_script("sources")                  # `fetch_issues_rest`, the priority-label chokepoint
discovery = _load_loop_script("discovery")              # `priority_rank`, `PRIORITIES` -- #2162

#: THE one rule for what a unit may be called, BOUND rather than re-derived. `feature_registry` does
#: exactly this (`is_unit_name = _load("features")._is_unit_name`), which is what makes this the
#: established gesture rather than a new reach into a private name. Identity is asserted in the
#: tests, because a behaviour table is satisfied by a copy and a copy is what drifts.
_is_unit_name = features._is_unit_name

#: THE three kinds a unit may be, and there are no others. A kind is METADATA -- it describes the
#: work, it does not steer the branch. See `branch_for`.
TYPES = ("feature", "bug", "refactor")

#: THE ORDER, AS DATA (§14). The executor is a loop over this tuple, so a reader checks the order in
#: one line and a test asserts it directly, rather than reconstructing it from three statements.
_STEPS = ("branch", "label", "registry")

# --- step outcomes -------------------------------------------------------------------------------
CREATED = "created"          # this run made it
EXISTS = "exists"            # it was already there; nothing was written
REFUSED = "refused"          # a human has to act -- not a transport failure
FAILED = "failed"            # the command did not succeed

# --- `declare` outcomes, per issue ---------------------------------------------------------------
DECLARED = "declared"        # both halves present, agreeing, same spelling
CASE_DRIFT = "case-drift"    # both halves agree, in two casings -- see #1638
NOT_DECLARED = "not-declared"  # the verdict is not `agree`
AMBIGUOUS = "ambiguous"      # this ONE issue contradicts itself
UNREADABLE = "unreadable"    # the issue could not be read back
NOT_SUPPORTED = "not-supported"  # the source has no label surface (LocalSource)

#: How many labels the existence check enumerates. See `_repo_labels` for why it enumerates at all,
#: and for what exceeding this bound costs (nothing: it falls through to a create that self-corrects).
_LABEL_PAGE = 500

#: The colour §14's own gesture uses, so a unit opened through this skill is indistinguishable on the
#: board from one opened by hand against the documented command.
_LABEL_COLOUR = "1d76db"

#: The three-method surface this module's `declare` duck-types on -- a strict subset of
#: `feature_labels.REQUIRED_SOURCE_METHODS`, because this path never refuses a pick and so needs
#: neither `mark_needs_label` nor the comment methods.
_REQUIRED_SOURCE_METHODS = ("attach_label", "fetch_body_labels")


def _note(message):
    """One stderr line, never an exception -- the shape every sibling on this epic holds, for the
    same reason: a diagnostic must never be the thing that breaks the gesture."""
    try:
        sys.stderr.write(message)
    except Exception:                     # noqa: BLE001 - a diagnostic must never break a gesture
        pass


def _flat(exc):
    return " ".join(str(exc).split())[:600] or exc.__class__.__name__


def _run_default(cwd, argv):
    """The default runner, matching `feature_sync._run`'s / `work._run`'s `(cwd, argv) -> stdout`
    contract exactly: raise on a non-zero exit, because every caller here reads an exception as "the
    command did not happen" and an empty string as "the answer is nothing". Collapsing those two is
    what turns an unreachable remote into "the branch does not exist yet"."""
    import subprocess                      # local: the injected runner is the real path in tests
    proc = subprocess.run([str(a) for a in argv], cwd=str(cwd), capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip() or
                           "%s exited %s" % (argv[0], proc.returncode))
    return (proc.stdout or "").strip()


# --------------------------------------------------------------------------- the derived strings


def branch_for(name):
    """`feature/<name>` -- FOR EVERY KIND, and that is the whole of §2/§5 on the subject.

    A shared bug and a refactor live under `feature/` exactly as a feature does. Giving a kind a
    branch prefix of its own would be intuitive and would break the model outright: base resolution matches on
    `features.BRANCH_PREFIX`, so a unit whose branch did not carry it would be invisible to the one
    mechanism the unit exists for. The kind survives as metadata, on the label's description."""
    return features.BRANCH_PREFIX + name


#: The label a unit projects to -- `feature_labels`' own function, bound rather than wrapped, so
#: there is one definition of a unit's label name and not two.
label_for = feature_labels.label_for


def validate(name, kind):
    """-> `(ok, why)`. The two questions asked before anything is written anywhere.

    `why` is prose for a human, never a code: the caller is a person who just typed a name."""
    if kind is None:
        return False, ("--type <%s> is required -- a unit is one of them, and nothing else"
                       % "|".join(TYPES))
    if name is None:
        return False, ("--name <name> is required -- a unit name is one git branch segment (no '/', "
                       "no '..', no leading '.', no trailing '.' or '.lock')")
    if kind not in TYPES:
        return False, ("%r is not a unit type -- a unit is one of %s, and nothing else; the type is "
                       "metadata about the work, never a branch prefix"
                       % (kind, ", ".join(TYPES)))
    if not (isinstance(name, str) and _is_unit_name(name)):
        return False, ("%r is not a unit name -- a unit name is one git branch segment (no '/', no "
                       "'..', no leading '.', no trailing '.' or '.lock'), because the same string "
                       "becomes a %s<name> branch, a %s<name> label and a file in the registry"
                       % (name, features.BRANCH_PREFIX, features.LABEL_PREFIX))
    return True, None


def _label_create_argv(name, kind):
    """The create invocation, in one place, so the test that proves this module is the kit's ONE
    authorized exception can feed the REAL argv to the kit's own refusal predicate rather than to a
    re-typed approximation of it."""
    return ["gh", "label", "create", label_for(name),
            "--description", "%s unit of work: %s" % (kind, name),
            "--color", _LABEL_COLOUR]


# --------------------------------------------------------------------------- opening a unit


class _Refused(Exception):
    """A step that a HUMAN has to resolve, as distinct from a command that failed. The two are
    reported differently because they are answered differently: a failure is retried, a refusal is
    corrected."""


class _Ctx(object):
    """Everything the three steps share, resolved once. A plain object rather than a dict so a typo
    in a step is an AttributeError at the step rather than a `None` three lines later."""

    def __init__(self, sdlc_dir, name, kind, config, run, cwd, remote, base):
        self.sdlc_dir = sdlc_dir
        self.name = name
        self.kind = kind
        self.config = config if isinstance(config, dict) else {}
        self.run = run or _run_default
        self.cwd = str(cwd or pathlib.Path(sdlc_dir).parent)
        safe = _load_loop_script("state").safe_ref               # #710: no option-shaped git args
        self._safe = safe
        self.remote = safe("--remote/work.remote", remote or self._configured("remote") or "origin")
        self.base = safe("--base", base)
        self.branch = branch_for(name)
        self.label = label_for(name)

    def _configured(self, key):
        work = self.config.get("work")
        got = work.get(key) if isinstance(work, dict) else None
        return self._safe("work." + key, got.strip()) if isinstance(got, str) and got.strip() else None

    def resolved_base(self):
        """Explicit, else `work.base`, else the branch we are on -- `sigma-loop/SKILL.md`'s own
        precedence for a goal's base, borrowed so a repository does not have to hold two different
        answers to "based on what?".

        `work.base` SHIPS EMPTY in `sigma-init`'s template, and an empty base is not a base: it means
        "the branch the loop was started on". Treating it as one would push `:refs/heads/feature/x`,
        which git reads as a DELETE of that ref."""
        if isinstance(self.base, str) and self.base.strip():
            return self.base.strip()
        configured = self._configured("base")
        if configured:
            return configured
        head = self.run(self.cwd, ["git", "rev-parse", "--abbrev-ref", "HEAD"]).strip()
        if not head or head == "HEAD":
            raise _Refused(
                "cannot tell what to base %s on: no --base was given, work.base is empty, and this "
                "checkout is not on a branch -- name the base explicitly" % self.branch)
        return head


def _step_branch(ctx):
    """REMOTE IS THE TRUTH (§8), so pushing the branch is what makes the unit exist -- not creating
    it locally, which no other checkout can see.

    An existing branch is never pushed over. Opening a unit twice has to be safe, and the unsafe
    shape here is specific and destructive: a second run pushing the base over a feature branch that
    has since accumulated merged work."""
    if ctx.run(ctx.cwd, ["git", "ls-remote", "--heads", ctx.remote, ctx.branch]).strip():
        return EXISTS, ctx.branch
    base = ctx.resolved_base()
    source = _push_source(ctx, base)
    ctx.run(ctx.cwd, ["git", "push", ctx.remote, "%s:refs/heads/%s" % (source, ctx.branch)])
    return CREATED, "%s -> %s" % (source, ctx.branch)


def _push_source(ctx, base):
    """The ref to push as the unit branch: the local `base` when it exists, else `<remote>/<base>`
    (#1397: a fresh clone has no local branch of the default name, and `git push` then fails with
    `src refspec ... does not match any`). When neither is found the bare base is returned so git
    reports the same error it always did."""
    for ref, source in (("refs/heads/" + base, base),
                        ("refs/remotes/%s/%s" % (ctx.remote, base), "%s/%s" % (ctx.remote, base))):
        try:
            ctx.run(ctx.cwd, ["git", "show-ref", "--verify", "--quiet", ref])
            return source
        except RuntimeError:
            continue
    return base


def _repo_labels(ctx):
    """Every label on the repository, ENUMERATED. Deliberately NOT `gh label list --search`.

    MEASURED, and the measurement is why this function exists. `--search` is a SEARCH INDEX, not a
    lookup, and it is eventually consistent: creating `feature:define-smoke-1662` on a live repo and
    searching for it immediately afterwards returned `["enhancement"]` -- an unrelated label, and not
    the one just created. Seconds later the same query returned it. A plain `gh label list` found it
    on the first call, every time.

    Both of that measurement's halves would have been bugs here, and each in the worse direction:

      - THE LAG. Reading a fresh miss as "absent" makes the create run against a label that already
        exists, which `gh` fails -- so the step FAILS and the whole gesture stops on a unit that was
        already correctly opened. `_already_exists` is the backstop for the residual race; this
        function is what makes the race rare rather than routine.
      - THE FUZZ. `--search` also matches DESCRIPTIONS and near names, so it answers with labels that
        merely resemble the query. Reading such a hit as "the label exists" would leave the real one
        uncreated, and every issue the flow then filed would be refused at its own pick -- the exact
        failure the step order exists to prevent, produced by the step meant to prevent it.

    THE BOUND, STATED RATHER THAN IMPLIED: `--limit` caps the enumeration, so a repository with more
    labels than `_LABEL_PAGE` can still miss one. That direction is safe -- it falls through to the
    create, and `_already_exists` turns the duplicate into `exists` -- which is precisely why the
    backstop is kept even though this call makes it almost unreachable."""
    try:
        got = ctx.run(ctx.cwd, ["gh", "label", "list", "--limit", str(_LABEL_PAGE),
                                "--json", "name", "-q", ".[].name"])
    except Exception:                      # noqa: BLE001 - #1398: `gh label list` is GraphQL; REST read
        got = ctx.run(ctx.cwd, ["gh", "api", "--paginate", "-q", ".[].name",
                                "repos/{owner}/{repo}/labels?per_page=100"])
    return [ln.strip() for ln in (got or "").splitlines() if ln.strip()]


def _already_exists(exc):
    """Is this create failure "the label is already there"? MEASURED against `gh` v2.x rather than
    guessed -- the message is:

        label with name "feature:x" already exists; use `--force` to update its color and description

    and `gh` says the SAME thing for a name differing only in case, which is the API confirming that
    GitHub label names are case-insensitively unique. Matching the phrase rather than the whole
    sentence: the `--force` half is advice and is the half most likely to be reworded."""
    return "already exists" in str(exc).lower()


def _step_label(ctx):
    """THE ONE PLACE THE KIT CREATES A `feature:*` LABEL. See the module docstring for why it is
    allowed here and why it does not route through `sources.GitHubSource`.

    GITHUB LABEL NAMES ARE CASE-INSENSITIVELY UNIQUE, so a repo carrying `feature:Voice` cannot also
    carry `feature:voice`. That is REFUSED rather than retried, naming the spelling already there,
    because two casings of one unit is #1638's remaining exposure and this is one of the two doors it
    could enter by (`declare` watches the other)."""
    for existing in _repo_labels(ctx):
        if existing == ctx.label:
            return EXISTS, existing
        if existing.lower() == ctx.label.lower():
            raise _Refused(
                # #1673: THE COUNT CAME OUT OF THIS SENTENCE RATHER THAN BEING CORRECTED AGAIN.
                # It said "three derived keys", went stale the moment two of them were folded, and
                # survived two sweeps of this change because it is a runtime string and nothing
                # asserted on it. A number in user-facing text that no test pins is a fourth stale
                # count waiting to happen; what a person needs here is the consequence, not the
                # size of the set. `tests/test_feature_registry.py` holds the real inventory, and
                # `test_a_DIFFERENT_CASING_of_our_label_is_refused_rather_than_created` now pins
                # this wording so it cannot silently drift back into a claim.
                "this repository already carries %r, which is the same label as %r -- GitHub label "
                "names are case-insensitively unique, so this repository cannot carry both "
                "spellings and one unit would answer to two names (#1638). Open the unit under the "
                "existing spelling, "
                "or rename that label first." % (existing, ctx.label))
    try:
        ctx.run(ctx.cwd, _label_create_argv(ctx.name, ctx.kind))
    except Exception as exc:              # noqa: BLE001 - one failure here is not a failure
        if not _already_exists(exc):
            raise
        # The enumeration missed it and the create found it. Both are the same answer -- the label is
        # there -- and reporting the gesture as FAILED for having learned it a call later would stop
        # a unit that is correctly open. Nothing was written either way.
        return EXISTS, ctx.label
    return CREATED, ctx.label


def _step_registry(ctx):
    """`.sdlc/features/`, once per REPOSITORY rather than once per unit (§14 step 3).

    IT IS LAST, and that is not tidiness. Its existence IS the repository's adoption of the
    registry-backed half -- base resolution, the registry sync, the scope gate, the ownership gate,
    rebase upkeep, unit completion and the cross-repo access check each open with the same
    `registry_dir(...).is_dir()` test and return `not-adopted` before spending anything. Creating it
    on the way out of a gesture that did not complete would arm all seven for the whole repository on
    the strength of a unit that does not exist."""
    path = feature_registry.registry_dir(ctx.sdlc_dir)
    if path.is_dir():
        return EXISTS, str(path)
    path.mkdir(parents=True, exist_ok=True)
    return CREATED, str(path)


def _commit_record(ctx):
    """#1312: make the unit visible to other machines. A unit with no record gets one written (the
    same `write_unit` shape the sync uses: no person or login field), then that one path is committed
    on the current branch (never a push), except on a unit branch, where it is left uncommitted and the
    command is printed (#1325). Never raises and never changes the report: when the write
    or commit cannot be made it prints one line naming the exact command."""
    path = None
    try:
        features_dir = feature_registry.registry_dir(ctx.sdlc_dir)
        path = feature_registry.unit_path(features_dir, ctx.name)
        if not path.is_file():
            report = feature_sync.amend(ctx.sdlc_dir, ctx.name, lambda entry: entry.update(title=ctx.name))
            if report.get("refused") or not path.is_file():
                raise RuntimeError(report.get("refused") or "the record was not written")
        if not ctx.run(ctx.cwd, ["git", "status", "--porcelain", "--", str(path)]).strip():
            return                                  # already committed and unchanged
        branch = ctx.run(ctx.cwd, ["git", "rev-parse", "--abbrev-ref", "HEAD"]).strip()
        if not branch or branch.startswith(features.BRANCH_PREFIX):
            # #1325: nobody commits directly to a unit branch (an unreadable branch fails closed).
            raise RuntimeError("a unit branch is checked out" if branch else "the current branch is unreadable")
        ctx.run(ctx.cwd, ["git", "add", "--", str(path)])
        ctx.run(ctx.cwd, ["git", "commit", "-m", "sdlc: record unit %s" % ctx.name, "--", str(path)])
    except Exception as exc:                        # noqa: BLE001 - a diagnostic never breaks the gesture
        _note("sigma: define: unit record not committed (%s); run: git add -- %s && git commit "
              "-m 'sdlc: record unit %s' -- %s\n" % (_flat(exc), path, ctx.name, path))


#: D7: the recovery differs per step, so each failure names its own first check (the raw detail stays).
_STEP_HINTS = {
    "branch": " -- check that `git remote -v` lists the remote and that you can push to it "
              "(/sigma-doctor runs the same checks)",
    "label": " -- needs `gh` installed and logged in with repo scope: run `gh auth status` "
             "(or /sigma-doctor)",
    "registry": " -- check that the .sdlc directory is writable",
}

_RUNNERS = {"branch": _step_branch, "label": _step_label, "registry": _step_registry}


def open_unit(sdlc_dir, name, kind, *, config=None, run=None, cwd=None, remote=None, base=None):
    """Open a unit: branch, then label, then registry, in that order and no other. Never raises.

    -> a report whose `steps` list is as long as the gesture actually got. `ok` is True only when all
    three steps are behind us; a stopped run's report NAMES the step that stopped it and why, because
    the recovery differs per step (push access, label permissions, a wrong `.sdlc` path) and a single
    "it failed" would send a human looking in the wrong place."""
    report = {"ok": False, "unit": name, "kind": kind, "branch": None, "label": None,
              "registry": str(feature_registry.registry_dir(sdlc_dir)), "steps": [], "why": None}
    ok, why = validate(name, kind)
    if not ok:
        report["why"] = why
        return report                     # NOTHING has run: not one command, not one directory
    report["branch"], report["label"] = branch_for(name), label_for(name)
    ctx = _Ctx(sdlc_dir, name, kind, config, run, cwd, remote, base)
    for step in _STEPS:
        try:
            outcome, detail = _RUNNERS[step](ctx)
        except _Refused as exc:
            report["steps"].append({"step": step, "outcome": REFUSED, "detail": _flat(exc)})
            report["why"] = _flat(exc)
            return report                 # a refusal stops the gesture: see the module docstring
        except Exception as exc:          # noqa: BLE001 - every failure is a report, never a raise
            report["steps"].append({"step": step, "outcome": FAILED, "detail": _flat(exc)})
            hint = _STEP_HINTS.get(step, "")
            if "rate limit" in str(exc).lower():     # #1398: a quota refusal is not a login problem
                hint = " -- GitHub rate limit; wait for it to reset and re-run"
            report["why"] = "%s: %s%s" % (step, _flat(exc), hint)
            return report
        report["steps"].append({"step": step, "outcome": outcome, "detail": detail})
    report["ok"] = True
    _commit_record(ctx)
    return report


# --------------------------------------------------------------------------- the body half


def _stampable(plan):
    """Every issue in a scope plan that carries a body -- the epic included.

    THE EPIC IS STAMPED TOO, deliberately. It is an issue on the board like any other, it is the one
    a human opens first, and a parent declaring no unit while all its children declare one is the
    shape that makes a reader doubt the children."""
    epic = plan.get("epic")
    if isinstance(epic, dict):
        yield "epic", epic
    issues = plan.get("issues")
    if isinstance(issues, list):
        for index, item in enumerate(issues):
            if isinstance(item, dict):
                yield str(item.get("key") or "issue %d" % index), item


def stamp_plan(plan, unit):
    """-> `(plan, warnings)`. Put `unit`'s BARE two-line marker into every body in a scope plan,
    BEFORE `compile_plan` files any of them. Never raises; never mutates the caller's plan.

    THE MARKER IS COMPOSED AND VERIFIED BY `feature_stamp.stamp_body`, NOT HERE. That function
    already builds the two lines from `features`' own key constants, feeds the candidate back through
    the REAL `features.parse_body`, falls back to the top of the body when the natural placement does
    not read back (an unterminated fence in a caller's own text blanks everything after it), and
    returns the body unstamped WITH A WARNING when no placement reads back at all. Re-implementing
    any of that here would be a second writer against one reader -- the exact drift the constants
    exist to prevent.

    THE COPY IS NOT DEFENSIVE TIDINESS. `sigma-scope` writes the plan to
    `.sdlc/plans/scope/<slug>.plan.json` and hands the SAME file to `assign.py execute` afterwards,
    so a plan mutated in place would put a machine-written marker into an artifact a later step
    re-reads as the human's own text."""
    out = copy.deepcopy(plan) if isinstance(plan, dict) else {}
    warnings = []
    for label, item in _stampable(out):
        stamp = feature_stamp.stamp_body(item.get("body"), unit)
        item["body"] = stamp.body
        warnings += ["%s: %s" % (label, w) for w in stamp.warnings]
    return out, warnings


# --------------------------------------------------------------------------- the label half


def declare(source, unit, issues):
    """Attach `feature:<unit>` to each freshly-filed issue, then VERIFY through the real reader.

    THE LABEL IS ATTACHED AFTER CREATION, NEVER HANDED TO `gh issue create --label`. §10's asymmetry,
    inherited rather than re-argued: an absent label makes `gh issue create` fail the WHOLE create
    (label resolution precedes the create mutation), so the atomic-looking route loses the entire
    issue in exactly the case that has to survive. Attaching afterwards degrades to `body_only`, and
    `feature_labels.attach_at_pick` closes that the first time the issue is picked.

    THE NEVER-CREATE RULE HOLDS HERE BY CONSTRUCTION AND NOT BY PROMISE. `attach_label` resolves
    every name to a node id first and RAISES rather than minting one. This module's create lives at
    `open_unit`, before any issue exists, against a name it validated itself -- by the time we are
    here the bodies exist and may have been edited, and minting from edited text is precisely the
    typo-outlives-the-unit failure §7 refuses.

    VERIFICATION IS THE POINT OF THE FUNCTION, not a postscript. §4a is silent by design: a marker
    that lands somewhere `features.py` rule 5 refuses to read produces no error anywhere. So each
    issue is read BACK through `features.read` and its verdict recorded -- and `AmbiguousUnit` is
    caught PER ISSUE, which is the obligation that function's own docstring puts on every caller that
    sweeps: one hand-edited issue must cost one issue, never the whole flow.

    A CASE DRIFT IS REPORTED RATHER THAN READ AS AGREEMENT. `features.read` compares the two halves
    case-insensitively and returns the BODY's spelling, so a body saying `Voice` under a
    `feature:voice` label is a legitimate `agree`. It is still two casings of one unit -- one thing
    a person reads under two names, and the state every derived key had to be taught to fold away
    (#1638, closed by #1672 and #1673) -- so this, the moment the unit's issues are created, is
    where it is named rather than left to be absorbed downstream."""
    label = label_for(unit)
    report = {"unit": unit, "label": label, "issues": [], "ok": False,
              "outcome": None, "why": None}
    missing = [m for m in _REQUIRED_SOURCE_METHODS if not callable(getattr(source, m, None))]
    if missing:
        # `LocalSource` deliberately defines none of these -- there is no label index over goal
        # FILES -- so the label half degrades to a stated no-op, the same posture
        # `feature_labels.attach_at_pick` takes for the same reason. The BODY marker still landed.
        report["outcome"] = NOT_SUPPORTED
        report["why"] = ("this backlog source has no label surface (missing %s), so the unit's label "
                         "half cannot be written; the body marker in each issue still declares the "
                         "unit. Nothing to do for a source without a label surface (the exit stays 1 so "
                         "callers see the label half was skipped)" % ", ".join(missing))
        return report
    for issue in issues:
        report["issues"].append(_declare_one(source, unit, label, issue))
    report["ok"] = bool(report["issues"]) and all(r["outcome"] == DECLARED
                                                  for r in report["issues"])
    report["outcome"] = DECLARED if report["ok"] else None
    return report


def _row_why(exc, generic_fix):
    """A declare row's reason. A failure gh itself reported and the shared table can classify (bad
    token, 404, 403, network) carries ITS fix, alone; any other failure gets `generic_fix`."""
    classified = sources._fx(exc)
    if classified != str(exc):
        return _flat(classified)
    return _flat(exc) + " -- fix: " + generic_fix


def _declare_one(source, unit, label, issue):
    row = {"issue": issue, "attached": False, "state": None, "unit": None,
           "outcome": None, "why": None}
    try:
        row["attached"] = bool(source.attach_label(issue, label))
    except Exception as exc:              # noqa: BLE001 - one issue's failure is one issue's
        row["outcome"], row["why"] = FAILED, _row_why(exc, (
            "if the label does not exist, create it by hand (`gh label create %s`; Sigma never creates "
            "the first label of a unit), then re-run define.py declare for this issue" % label))
        return row
    try:
        verdict = features.read(source.fetch_body_labels(issue))
    except features.AmbiguousUnit as exc:
        row["outcome"], row["why"] = AMBIGUOUS, _flat(exc) + (
            " -- fix: edit the issue so its body marker and label name one unit, then re-run "
            "define.py declare")
        return row
    except Exception as exc:              # noqa: BLE001 - a read we could not make is evidence of nothing, and must not stop the rest
        row["outcome"], row["why"] = UNREADABLE, _row_why(exc, (
            "re-run define.py declare for this issue once gh can read it (`gh auth status`)"))
        return row
    row["state"], row["unit"] = verdict.state, verdict.unit
    if verdict.state != features.AGREE:
        row["outcome"] = NOT_DECLARED
        row["why"] = ("the issue does not declare the unit on both halves (state %s) -- fix: put "
                      "`Feature: %s` on its own line in the issue body and make sure it carries the "
                      "label %s, then re-run define.py declare" % (verdict.state, unit, label))
    elif verdict.unit != unit:
        row["outcome"] = CASE_DRIFT
        row["why"] = ("the body declares %r where the unit was opened as %r -- one unit, two "
                      "casings; correct the body marker (#1638)" % (verdict.unit, unit))
    else:
        row["outcome"] = DECLARED
    return row


# --------------------------------------------------------------------------- the priority half


def set_priority(sdlc_dir, unit, priority):
    """Record `unit`'s OWN priority on its registry entry -- #2266 (epic #2260, slice 6 of
    design #2253). THIS is the answer to "prioritise this feature": one value, on the
    unit, that the comparator (`_pick_key`'s `feature_rank` term, #2262/#2264) reads as a
    tie-break among issues that are already eligible. It never touches a member issue's own
    `priority:` label, so it never erases the per-issue tiers a human set deliberately -- the
    defect `bump_priority` below has by construction (§5 of the design, "Superseding
    `bump-priority`": the shipped first cut is actively harmful once priority is data on the unit,
    because it erases the per-issue tiers the comparator reads).

    THE WRITE GOES THROUGH `feature_sync.amend` AND NOTHING ELSE -- #2261's own write surface,
    already proven (`tests/test_feature_sync.py::test_amend_is_the_write_surface_for_a_units_priority`):
    the read-modify-write happens under the unit's own lock, and the WHOLE entry goes back, so a
    priority recorded here survives a concurrent pick recording a goal on the same unit rather than
    erasing its repos, goals or tracking issue the way a one-field write would (`write_unit`
    replaces, never merges). No second write surface is added for it, deliberately, matching #2261's
    own module docstring on the same point.

    NEVER RAISES: a bad priority is refused before the registry is ever touched, exactly as
    `bump_priority` refuses one before any read or write -- and a name `amend` cannot lock (a
    different `InvalidUnitName` class than this module's own copy, per `_load_loop_script`'s
    fresh-module-per-call contract; see `feature_sync.py`'s own note on why every catch here is
    `ValueError`, never the named subclass) is reported the same way, never thrown.

    `ok` is true once `written` is -- the shard on disk says what we asked for, verified by a read
    of it (`amend`'s own documented meaning of `written`). It does not require `landed` (which also
    demands the lock was actually held): that stricter guarantee matters to a concurrent pick's own
    safety net, not to a single human-driven CLI call reporting whether its answer was recorded."""
    report = {"unit": unit, "priority": priority, "canon": None, "ok": False,
              "changed": None, "written": None, "landed": None, "why": None}
    rank = discovery.priority_rank(priority)
    if rank >= len(discovery.PRIORITIES):
        report["why"] = ("%r is not a P0-P4 priority tier -- discovery.priority_rank could not "
                         "resolve it to one" % (priority,))
        return report
    canon = discovery.PRIORITIES[rank]
    report["canon"] = canon
    try:
        amended = feature_sync.amend(sdlc_dir, unit,
                                     lambda entry, _c=canon: entry.__setitem__("priority", _c))
    except ValueError as exc:      # noqa: BLE001 - `InvalidUnitName`; see the docstring above
        report["why"] = _flat(exc)
        return report
    if amended.get("refused"):
        report["why"] = amended["refused"]
        return report
    report["changed"] = amended["changed"]
    report["written"] = amended["written"]
    report["landed"] = amended["landed"]
    report["ok"] = bool(amended["written"])
    return report


# --------------------------------------------------------------------------- the `<name> <priority>` shorthand


#: Every verb `main` already recognises as `argv[1]`. The shorthand's CLI form (below) is reached
#: only when `argv[1]` matches none of these, so an existing invocation -- any real verb, any
#: argument count -- is never reinterpreted, and the shorthand can never shadow one of these five.
_VERBS = ("open", "stamp", "declare", "set-priority", "bump-priority")


def _registered_unit(sdlc_dir, name):
    """-> the registry's OWN spelling of `name`, if `.sdlc/features/` already carries an entry for
    it (open or closed), else `None`.

    Matched case-insensitively against `feature_registry.read`'s MERGED view (`index.json` folded
    with every unit's own shard) -- the same fold `feature_registry.resolve_open_unit` applies, and
    for the identical reason: a human typing the shorthand must not be refused over a spelling that
    differs only in case from the one already on record. UNLIKE `resolve_open_unit` this does not
    filter on `open` -- the question here is "does the registry know this unit at all", never "may
    new work be attached to it", and recording a tie-break priority is harmless on a closed unit.

    NEVER RAISES: an illegal name cannot be a registered one (`_is_unit_name`, the same guard
    `unit_path` itself applies), and a missing or corrupt registry degrades to "no units known"
    exactly as `feature_registry.read` already does -- so a bad registry answers `None` here too,
    rather than taking the shorthand down."""
    if not (isinstance(name, str) and _is_unit_name(name)):
        return None
    lowered = name.lower()
    for key in feature_registry.read(feature_registry.registry_dir(sdlc_dir)):
        if key.lower() == lowered:
            return key
    return None


def priority_shorthand(sdlc_dir, name, priority):
    """The convenience wrapper the issue asked for: `<name> <priority>`, two positional args, no
    `--unit`/`--priority` flags -- for a unit that ALREADY EXISTS (#2331).

    IF `name` IS A KNOWN UNIT, THIS CALLS `set_priority` UNCHANGED AND RETURNS ITS OWN REPORT,
    BYTE-IDENTICAL. No second write path, no re-validation of the priority tier -- that stays
    `set_priority`'s own job, through `discovery.priority_rank` -- and no field of its report is
    added, removed or renamed here.

    IF `name` IS NOT YET A UNIT, THIS REFUSES RATHER THAN GUESSING. Silently attempting to open one
    from two bare positional args would be exactly the "helpful" shortcut this repo's own idiom
    refuses (see the module docstring on `_STEPS`) -- a shorthand cannot know a TYPE
    (`feature`/`bug`/`refactor`), cannot pick a BASE, and has no business minting a `feature:*`
    label or a branch on the strength of a typo. The report keeps `set_priority`'s own shape
    (`unit`/`priority`/`canon`/`ok`/`changed`/`written`/`landed`/`why`) so a caller reading one
    field off either outcome never has to branch on which path it took -- only `why` differs, and
    it names the real fix: the full `open`/`declare` flow."""
    existing = _registered_unit(sdlc_dir, name)
    if existing is None:
        return {"unit": name, "priority": priority, "canon": None, "ok": False, "changed": None,
                "written": None, "landed": None,
                "why": ("%r is not a unit this repository's registry already knows -- the `<name> "
                       "<priority>` shorthand only sets a tie-break priority on a unit that ALREADY "
                       "EXISTS. To open a new one, run the full `/sigma-define` flow (or `define.py "
                       "open` followed by `declare`) instead." % (name,))}
    return set_priority(sdlc_dir, existing, priority)


# --------------------------------------------------------------------------- levelling a unit's members by hand


#: How many of a feature's own OPEN, `feature:<name>`-labelled members a single bump enumerates.
#: Mirrors `_LABEL_PAGE`'s own posture above: bounded rather than unlimited, and the bound is safe
#: in the direction that matters -- a feature carrying more members than this bumps the first
#: `_MEMBER_FETCH_CAP` (`fetch_issues_rest`'s own default oldest-first order), never fewer than
#: exist and never a crash.
_MEMBER_FETCH_CAP = 500

# --- `bump_priority` outcomes, per issue ---------------------------------------------------------
BUMPED = "bumped"                                    # the member's rank was less urgent than the target
ALREADY_AT_OR_ABOVE = "already-at-or-above"          # the member was already at least this urgent
EXCLUDED_NEEDS_CONFIRMATION = "excluded-needs-confirmation"  # not yet a confirmed member (#233)
WRITE_FAILED = "write-failed"                        # the label write itself raised

#: The surface `bump_priority` duck-types `source` on -- exactly what `sources.GitHubSource`
#: exposes for reading + writing a priority label, and exactly what `LocalSource` (frontmatter-only
#: priority, no priority LABEL at all) does not have. Checked up front so a local backlog degrades
#: to one clear report row instead of failing halfway through a partial sweep.
_REQUIRED_PRIORITY_METHODS = ("_write_priority_label", "_run")


def _priority_label_names(labels):
    """Every label NAME off a raw label list, in EITHER shape a caller might hand in -- REST/board
    `{"name": ...}` objects (what `fetch_issues_rest` returns) or bare strings -- mirroring
    `GitHubSource._write_priority_label`'s own extraction (sources.py) exactly, so this reads one
    issue's labels the same way the chokepoint it feeds does."""
    return {(l.get("name") if isinstance(l, dict) else str(l or "")) for l in (labels or [])}


def bump_priority(source, unit, priority, *, cap=_MEMBER_FETCH_CAP):
    """Set every OPEN `feature:<unit>` member issue's priority label to EXACTLY `priority` --
    #2162, slice 1 of epic #2161. Never raises: a bad priority, an unreadable backlog, or one
    issue's failed write are all reported as data, never thrown.

    NOT THE ANSWER TO "PRIORITISE THIS FEATURE" ANY MORE (#2266, epic #2260, slice 6). Neither
    setup flow calls this by default -- `set_priority` above is what `sigma-define`'s `declare` step
    and `sigma-goal-review`'s feature-ification step call now, because this routine's whole effect is
    to overwrite the individual priorities a tie-break needs to read. The verb stays, idempotent and
    human-invoked, for the different and still-legitimate job stated below: genuinely LEVELLING a
    unit's members by hand, on purpose, when that is actually wanted.

    UNCONDITIONAL, EXACT-MATCH, ALWAYS -- and DELIBERATELY NOT `GitHubSource._promote_blockers`'s
    own "highest wins, never downgrades" `min()` rule. Every remaining member's priority label is
    set to `priority` regardless of what it already carried, including downward, because a human
    invoked THIS routine directly, by name, to make the feature's members agree -- never as a side
    effect of setting the feature's own priority. `_promote_blockers`'s job starts only where this
    one stops: see the note below.

    NO BLOCKER-WALKING LOGIC OF ANY KIND. `GitHubSource._promote_blockers` (sources.py) already
    walks `blocked_by` edges transitively and cycle-safely on every `_sync_backlog` run once opted
    in (`discovery.blocker_promotion.mode`, already `"smart"` on this repo), and this routine's own
    member-priority writes are exactly the upstream change that mechanism watches for on its next
    run. Building a second blocker-walk here would be the "second, parallel write path"
    `_write_priority_label`'s own docstring forbids -- so this function enumerates and writes ONLY
    the feature's direct, open, labelled members: nothing a member itself blocks or is blocked by.

    THE WRITE GOES THROUGH `source._write_priority_label` -- sources.py's OWN docstring names it
    "the one place an issue's priority label is ever written" -- so there is exactly one `gh`-call
    path into a priority label whether `_promote_blockers` or this routine triggered it.

    `source` is duck-typed on `_REQUIRED_PRIORITY_METHODS` plus `repo`/`proposed_label`/
    `priority_prefix`/`priority_aliases` -- the real surface `sources.GitHubSource` already
    exposes. A `LocalSource` (frontmatter-only priority, no priority LABEL at all) has none of
    these and is refused up front, with a stated reason, rather than half-applied."""
    label = label_for(unit)
    report = {"unit": unit, "priority": priority, "canon": None, "label": label,
              "ok": False, "outcome": None, "issues": [], "why": None}
    rank = discovery.priority_rank(priority)
    if rank >= len(discovery.PRIORITIES):
        report["why"] = ("%r is not a P0-P4 priority tier -- discovery.priority_rank could not "
                         "resolve it to one" % (priority,))
        return report
    canon = discovery.PRIORITIES[rank]
    report["canon"] = canon
    missing = [m for m in _REQUIRED_PRIORITY_METHODS if not callable(getattr(source, m, None))]
    if missing or not hasattr(source, "repo"):
        report["outcome"] = NOT_SUPPORTED
        report["why"] = ("this backlog source has no priority-label surface (missing %s), so "
                         "feature-level priority cannot be applied. Use `define.py set-priority "
                         "<sdlc_dir> --unit NAME --priority P1` instead: it records the tie-break on "
                         "the unit and works without GitHub" % ", ".join(missing or ["repo"]))
        return report
    try:
        issues = sources.fetch_issues_rest(source._run, source.repo, [label], cap)
    except Exception as exc:                      # noqa: BLE001 - a read failure is one report, not a raise
        report["why"] = "could not enumerate %s's members: %s" % (label, _flat(exc))
        return report
    proposed = getattr(source, "proposed_label", None) or "sdlc:needs-confirmation"
    prefix = getattr(source, "priority_prefix", None) or "priority:"
    aliases = getattr(source, "priority_aliases", None)
    for issue in issues:
        report["issues"].append(_bump_one(source, issue, canon, rank, proposed, prefix, aliases))
    report["ok"] = all(row["outcome"] != WRITE_FAILED for row in report["issues"])
    return report


def _bump_one(source, issue, canon, rank, proposed_label, prefix, aliases):
    """One member issue's outcome. `before` (a `P<n>` string, or None when the issue carried no
    priority label at all) is reported alongside `outcome` even though the write is unconditional
    either way -- so "already-at-or-above" reads as evidence, not just a claim."""
    n = issue.get("number")
    row = {"issue": n, "outcome": None, "before": None, "why": None}
    names = _priority_label_names(issue.get("labels"))
    if proposed_label in names:
        row["outcome"] = EXCLUDED_NEEDS_CONFIRMATION
        return row
    current = [discovery.priority_rank(name, aliases) for name in names if name.startswith(prefix)]
    current_rank = min(current) if current else discovery.UNPRIORITISED
    if current_rank < len(discovery.PRIORITIES):
        row["before"] = discovery.PRIORITIES[current_rank]
    try:
        source._write_priority_label(n, canon, issue.get("labels"))
    except Exception as exc:                      # noqa: BLE001 - one issue's failure is one issue's
        row["outcome"], row["why"] = WRITE_FAILED, _flat(exc)
        return row
    row["outcome"] = ALREADY_AT_OR_ABOVE if current_rank <= rank else BUMPED
    return row


# --------------------------------------------------------------------------- CLI


def _flag(argv, name, default=None):
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) \
        else default


USAGE = ("usage: define.py open <sdlc_dir> --name <name> --type <feature|bug|refactor> "
         "[--base <ref>] [--remote <remote>] [--config <path>]\n"
         "       define.py stamp <plan.json> --unit <name> [--out <path>]\n"
         "       define.py declare <sdlc_dir> --unit <name> --issues <n,n,n>\n"
         "       define.py set-priority <sdlc_dir> --unit <name> --priority <P0..P4>\n"
         "       define.py bump-priority <sdlc_dir> --unit <name> --priority <P0..P4>\n"
         "       define.py <sdlc_dir> <name> <P0..P4>   (shorthand: set-priority, EXISTING unit "
         "only -- refuses rather than opening one)")


def main(argv):
    """One JSON line per verb -- `brainstorm.py`/`dedup.py`/`scope.py`/`assign.py resolve`'s own
    convention, so `SKILL.md` can read a field out of any of them the same way."""
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    if len(argv) >= 3 and argv[1] == "open":
        config = None
        config_path = _flag(argv, "--config")
        if config_path:
            config = json.loads(pathlib.Path(config_path).read_text(encoding="utf-8"))
        elif pathlib.Path(argv[2], "config.json").is_file():
            try:
                config = json.loads(pathlib.Path(argv[2], "config.json")
                                    .read_text(encoding="utf-8"))
            except Exception as exc:      # noqa: BLE001 - an unreadable config is not a base
                _note("sigma: define: could not read %s/config.json (%s)\n" % (argv[2], exc))
        report = open_unit(argv[2], _flag(argv, "--name"), _flag(argv, "--type"),
                           config=config, remote=_flag(argv, "--remote"),
                           base=_flag(argv, "--base"))
        print(json.dumps(report))
        return 0 if report["ok"] else 1
    if len(argv) >= 3 and argv[1] == "stamp":
        path = pathlib.Path(argv[2])
        plan, warnings = stamp_plan(json.loads(path.read_text(encoding="utf-8")),
                                    _flag(argv, "--unit"))
        out = pathlib.Path(_flag(argv, "--out", str(path)))
        out.write_text(json.dumps(plan, indent=2), encoding="utf-8")
        print(json.dumps({"ok": not warnings, "plan": str(out), "unit": _flag(argv, "--unit"),
                          "warnings": warnings}))
        return 0 if not warnings else 1
    if len(argv) >= 3 and argv[1] == "declare":
        # `sources` is already loaded at module level (see the top of the file) -- no need to
        # reload it here. (It USED to be reloaded here, before `bump_priority` needed the same
        # module: a bare `sources = _load_loop_script(...)` anywhere in this function makes
        # `sources` a LOCAL for the WHOLE function body, which raised `UnboundLocalError` the
        # moment the `bump-priority` branch below referenced it on an argv that never took this
        # branch.)
        ledger = _load_loop_script("ledger")
        config = ledger._config(argv[2])
        issues = [n.strip() for n in (_flag(argv, "--issues") or "").split(",") if n.strip()]
        report = declare(sources.get_source(argv[2], config), _flag(argv, "--unit"), issues)
        print(json.dumps(report))
        return 0 if report["ok"] else 1
    if len(argv) >= 3 and argv[1] == "set-priority":
        # No `source`/`ledger` config needed at all -- the write is local, through
        # `feature_sync.amend` against `sdlc_dir` directly, never a `gh` call (#2266).
        report = set_priority(argv[2], _flag(argv, "--unit"), _flag(argv, "--priority"))
        print(json.dumps(report))
        return 0 if report["ok"] else 1
    if len(argv) >= 3 and argv[1] == "bump-priority":
        ledger = _load_loop_script("ledger")
        config = ledger._config(argv[2])
        report = bump_priority(sources.get_source(argv[2], config), _flag(argv, "--unit"),
                               _flag(argv, "--priority"))
        print(json.dumps(report))
        return 0 if report["ok"] else 1
    if len(argv) == 4 and argv[1] not in _VERBS:
        # The shorthand (#2331): `<sdlc_dir> <name> <priority>`, no verb at all -- reached only
        # when `argv[1]` is none of the five real verbs above (each of which already returned),
        # so this can never reinterpret a genuine `open`/`stamp`/`declare`/`set-priority`/
        # `bump-priority` call, however many or few arguments it was given.
        report = priority_shorthand(argv[1], argv[2], argv[3])
        print(json.dumps(report))
        return 0 if report["ok"] else 1
    print(__doc__.strip().split("\n")[0])
    print(USAGE)
    return 2


if __name__ == "__main__":
    # The script-time floor: this run's wall time, recorded to the session resolved at exit.
    raise SystemExit(_load_loop_script("timing_store").timed_main(main, sys.argv, "define"))
