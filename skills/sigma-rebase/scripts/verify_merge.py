#!/usr/bin/env python3
"""sigma-rebase, slice 3 (#2306, epic #2303, design #2288 §6-§7): the
verify-then-ask-to-merge tail that runs once #2304's rebase is clean, or #2305's conflict walker
has resolved every conflict.

WHAT THIS IS FOR. Slice 1 rebases (or explains and stops on a conflict); slice 2 resolves a
conflict interactively. Neither proves the result actually works, and neither ever lands anything.
This module is the last mile: run this repo's own proving command against the now-current tree,
report pass/fail honestly, and -- only once green, and only when a human explicitly says so --
land `feature/<name>` onto the integration branch.

REUSED, NEVER REINVENTED:
  * `rebase_brief.resolve_remote`/`resolve_base`/`resolve_branch`/`current_branch` -- the identical
    branch/base resolution slices 1 and 2 already use, so this tail agrees with them on "which
    branch" and "onto what" without a second definition of either.
  * `feature_rebase.rebase_stopped` -- the same structural (never exception-text) test slice 2 uses
    for resumability, reused here as a PRECONDITION: this tail refuses to run against a tree that
    still has a rebase stopped in it (conflict markers, a half-applied stash) rather than let
    `verify.command` report a false failure -- or worse, a false pass -- against a broken tree.
  * `unit_completion._repo_ref`/`_landing_pull_requests`/`_pick_landing`/`_pr_ref` -- the EXACT
    landing-PR discovery primitive #2289's own design already reused for the same purpose (BR-11):
    "does a landing PR already exist for this feature branch", `state=all` so a human's own
    CLOSED decision is never invisible and never silently reopened. Loaded the same way every
    sibling script in this skill loads its neighbours -- `_load()` by path, no package install.
  * `merge_observation.py feature-landing` plus `ledger.safe_append` -- this feature-level
    landing has a different subject than `work.py merge()` (`feature/<name>` -> the integration
    branch), but it obtains the canonical PR facts from GitHub and asks the loop skill's
    serialized receipt protocol to derive the same ownership/merge-SHA keys before writing
    `merged` and, when enabled, `merge_observed`.  A failed observation is intentionally pending;
    it never fabricates a local branch/number key or reclassifies a merge that GitHub accepted as
    failed.

DELIBERATELY NOT REUSED, AND WHY (design §7, BR-9/BR-10/PC-4):
  * `work.py merge()` -- record-bound (`_record(sdlc_dir, goal)`); there is no goal id here, and
    fabricating one would not just fail to apply, it is the exact anti-pattern this module's own
    verify step refuses for the identical reason (see `run_verify_command`'s docstring).
  * `unit_completion.py`'s own merge-adjacent paths -- `_draft`/`_landing`/`signal` never call
    `gh pr merge`, and that absence is pinned by that module's own test
    (`tests/test_unit_completion.py::test_the_default_opens_nothing_even_when_opening_would_have_
    SUCCEEDED`). This module calls `gh pr merge` directly, its own separate code path, never through
    that module -- the pin stays true.

THE MERGE METHOD IS `--merge`, NOT `--squash`, DELIBERATELY. `work.py`'s own goal-level default
(`work.merge_method`, "squash") answers a different question -- one goal's branch onto its unit.
Landing a WHOLE feature branch (itself built from many already-squashed goal merges) is the
question here, and the measured real-history precedent design BR-12/PC-5 verified (commit
`04d94f13`, "Merge pull request #2286 from .../feature/feature-priority") is a genuine two-parent
merge commit, not a squash -- squashing an entire unit's history into one commit on `main` would
throw away exactly the granularity that precedent kept. Not read from `work.merge_method`: that
key governs a different merge's method, and reusing it here would silently couple two unrelated
decisions the first time someone changed one to mean the other.

NEVER A DELETE-SHAPED CALL. `docs/branching-model.md` §13b's hard invariant (BR-26): no automatic
code path ever deletes a `feature/<name>` branch. The merge call below carries no
`-d`/`--delete-branch`.

CLI: `verify_merge.py land <sdlc_dir> [branch]` -- verify, then (only if green, and only on an
explicit yes) land."""
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys
import time

_HERE = pathlib.Path(__file__).resolve().parent
_LOOP_SCRIPTS = _HERE.parent.parent / "sigma-loop" / "scripts"
_MERGE_OBSERVATION_SCRIPT = _LOOP_SCRIPTS / "merge_observation.py"


def _load(name, directory=None):
    """Import a sibling script by path -- the kit's standard zero-install module loader
    (`rebase_brief._load`, `conflict_walk._load`, `feature_rebase._load`)."""
    directory = pathlib.Path(directory) if directory else _HERE
    spec = importlib.util.spec_from_file_location(name, directory / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


feature_rebase = _load("feature_rebase", _LOOP_SCRIPTS)
rebase_brief = _load("rebase_brief", _HERE)
state = _load("state", _LOOP_SCRIPTS)
ledger = _load("ledger", _LOOP_SCRIPTS)
shell_policy = _load("shell_policy", _LOOP_SCRIPTS)
unit_completion = _load("unit_completion", _LOOP_SCRIPTS)

# --------------------------------------------------------------------------- verify (§6)


def verify_command(config):
    """`verify.command` from `.sdlc/config.json` -- the SAME key `loop.py verify_goal` reads
    (design BR-21). No goal-frontmatter fallback: that half of `verify_goal`'s own source order is
    per-GOAL, and there is no goal here."""
    return (config.get("verify") or {}).get("command") or None


def run_verify_command(cmd, cwd):
    """Run `cmd` directly against `cwd` -- the CURRENT working tree, never a fabricated goal id.

    WHY NOT `loop.py verify_goal` ITSELF. `verify_goal` writes its evidence to
    `state.evidence_path(sdlc_dir, goal)` -- `.sdlc/state/verify/<goal-stem>.json` -- and that path
    is keyed by goal id for a real reason: `state.done_refusal` reads it back to decide whether a
    LATER `record done` for that exact goal may proceed. There is no goal here; inventing one
    (`"land"`, the branch name, anything) would write into that goal-keyed slot and could silently
    corrupt `done_refusal`'s bookkeeping for a real, unrelated goal that happens to share the
    fabricated id. So this mirrors `verify_goal`'s own "no work record -> verify the PROJECT ROOT"
    fallback (`loop.py:3996`) in the one way that matters here -- running the command against the
    real tree and being loud about what was verified -- without touching that goal-keyed store at
    all. Nothing is persisted; the report is IN THE TERMINAL, as the issue itself asks for.

    Mirrors `verify_goal`'s own subprocess shape exactly: `shell=True`, both streams captured, a
    5-line tail on failure. `subprocess.run` is looked up fresh on `subprocess` at call time (never
    bound as a default-argument value at import time) so a test's `monkeypatch.setattr(subprocess,
    "run", ...)` -- this suite's own `_no_live_gh` guard, `tests/conftest.py` -- is honoured exactly
    as it would be for any other caller. Never raises: a runner that cannot even start (a missing
    shell, an unreadable `cwd`) is reported as a failed verify, not a crash that leaves the human
    wondering whether anything ran at all."""
    start = time.perf_counter()
    if not shell_policy.repository_shell_commands_allowed(cwd):
        # #707 (TM-04): same gate `loop.py verify` applies to the same `verify.command`.
        return {"ok": False, "exit": None, "ms": 0, "tail": [],
                "why": shell_policy.refusal_message()}
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=cwd)
    except Exception as exc:                  # noqa: BLE001 - a verify that could not start is FAIL, not a crash
        return {"ok": False, "exit": None, "ms": int((time.perf_counter() - start) * 1000),
                "tail": [], "why": _flat(exc)}
    ms = int((time.perf_counter() - start) * 1000)
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-5:]
    return {"ok": proc.returncode == 0, "exit": proc.returncode, "ms": ms, "tail": tail, "why": None}


def format_verify_report(cmd, cwd, result):
    """Pass/fail, honestly, in the terminal (issue item 2) -- never silently swallowed. Names the
    tree it ran against, mirroring `verify_goal`'s own loud disclosure of what was verified."""
    if result["why"]:
        return "VERIFY did not even run `%s` in %s: %s" % (cmd, cwd, result["why"])
    verdict = "PASSED" if result["ok"] else "FAILED"
    lines = ["VERIFY %s -- `%s` in %s (exit %s, %dms)"
             % (verdict, cmd, cwd, result["exit"], result["ms"])]
    if not result["ok"] and result["tail"]:
        lines.append("last output:")
        lines.extend("  %s" % line for line in result["tail"])
    return "\n".join(lines)


def _flat(exc):
    return " ".join(str(exc).split())


# --------------------------------------------------------------------------- landing PR (§7 steps 1-2)

EXISTS_READY = "exists-ready"      # an open, non-draft PR already covers this landing -- use it as-is
READIED = "readied"                # an open DRAFT PR was marked ready
CREATED = "created"                # no PR existed on this head -- one was opened
ALREADY_MERGED = "already-merged"  # the landing already happened; nothing to do
DECLINED = "declined"              # a human already CLOSED the landing PR without merging -- not reopened
REFUSED = "refused"                # could not safely proceed -- see "why"
DURABLE_RECEIPT = "durable-receipt"
_LANDING_WRITER = "verify_merge.ensure_landing_pr"

_PR_URL_RE = re.compile(r"/pull/(\d+)\s*$")


def _parse_created_pr_number(out):
    """The PR number `gh pr create`'s own stdout names -- just the URL on success (its own `--help`
    text: "Upon success, the URL of the created pull request will be printed."). Read from the
    LAST non-empty line so any incidental progress text ahead of it (a push notice, `--fill`'s own
    chatter) cannot be mistaken for the answer."""
    for line in reversed((out or "").strip().splitlines()):
        m = _PR_URL_RE.search(line.strip())
        if m:
            return int(m.group(1))
    return None


def _canonical_landing_facts(raw, branch):
    """Project GitHub's immutable PR facts into one feature-landing ownership receipt."""
    try:
        facts = {"canonical_repository_id": raw["base"]["repo"]["node_id"],
                 "owner_kind": "unit", "owner_id": str(branch), "goal": None,
                 "head_ref": raw["head"]["ref"], "base_ref": raw["base"]["ref"],
                 "pr_number": raw["number"], "pr_node_id": raw["node_id"],
                 "creating_writer": _LANDING_WRITER, "pr_created_at": raw["created_at"]}
    except (KeyError, TypeError) as exc:
        raise ValueError("canonical landing PR facts are incomplete") from exc
    if (facts["owner_kind"] != "unit" or facts["owner_id"] != str(branch)
            or facts["head_ref"] != str(branch) or not isinstance(facts["pr_number"], int)
            or facts["pr_number"] <= 0
            or not all(isinstance(facts[name], str) and facts[name] for name in
                       ("canonical_repository_id", "head_ref", "base_ref", "pr_node_id", "pr_created_at"))):
        raise ValueError("canonical landing PR facts are incomplete")
    return facts


def _receipt_request(request):
    """Call sigma-loop's narrow serialized feature-landing receipt protocol.

    Cross-skill imports would make this rebase tool depend on the loop skill's Python module
    layout.  The protocol pins the boundary to canonical JSON and lets merge_observation remain
    the sole owner of receipt bytes and semantic key derivation.
    """
    try:
        proc = subprocess.run(
            [sys.executable, str(_MERGE_OBSERVATION_SCRIPT), "feature-landing"],
            input=json.dumps(request, sort_keys=True), capture_output=True, text=True,
        )
    except Exception as exc:  # noqa: BLE001 - callers convert a failed best-effort observation to pending
        raise ValueError("feature landing receipt protocol did not start") from exc
    if proc.returncode:
        raise ValueError("feature landing receipt protocol failed: %s" % _flat(proc.stderr or proc.stdout))
    try:
        value = json.loads(proc.stdout)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("feature landing receipt protocol returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("feature landing receipt protocol returned an unsafe response")
    return value


def _read_canonical_pr(run, cwd, number):
    raw = json.loads(run(cwd, ["gh", "api", "repos/{owner}/{repo}/pulls/%s" % number]))
    if not isinstance(raw, dict) or raw.get("number") != int(number):
        raise ValueError("canonical landing PR response does not match its number")
    return raw


def _persist_landing_receipt(sdlc_dir, run, cwd, branch, number):
    """Write the immutable proof that this invocation created the feature landing PR."""
    if not sdlc_dir:
        raise ValueError("feature landing has no state directory for its ownership receipt")
    facts = _canonical_landing_facts(_read_canonical_pr(run, cwd, number), branch)
    response = _receipt_request({"action": "write-parent", "sdlc_dir": str(sdlc_dir), "facts": facts})
    if response.get("valid") is not True or not re.fullmatch(r"[0-9a-f]{64}", str(response.get("ownership_key"))):
        raise ValueError("feature landing receipt protocol refused the canonical parent")
    return facts


def _validated_landing_receipt(sdlc_dir, run, cwd, branch, number):
    """Return receipt facts only when the local durable proof matches GitHub's canonical PR."""
    if not sdlc_dir:
        return None
    try:
        facts = _canonical_landing_facts(_read_canonical_pr(run, cwd, number), branch)
        response = _receipt_request({"action": "check-parent", "sdlc_dir": str(sdlc_dir), "facts": facts})
        if response.get("valid") is not True or not re.fullmatch(r"[0-9a-f]{64}", str(response.get("ownership_key"))):
            return None
        return facts
    except Exception:  # noqa: BLE001 - a non-verifiable ownership claim is never attributed
        return None


def _try_write_delivery(path, delivery):
    """#6: this module's own docstring promises "a completed landing must not be reported as
    failed" -- a delivery-receipt write error must be reported, never raised, same as work.py's
    `_try_write_merge_delivery`. The receipt only makes a crash between the two sinks retryable;
    each sink's own stable key already collapses a duplicate write."""
    try:
        _write_delivery(path, delivery)
    except OSError as exc:
        print("feature landing delivery receipt pending: %s" % exc, file=sys.stderr)


def _write_delivery(path, value):
    path = pathlib.Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp-" + str(os.getpid()))
    data = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    with open(temporary, "wb") as handle:
        handle.write(data); handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)


def ensure_landing_pr(run, cwd, config, branch, base, sdlc_dir=None):
    """Design §7 steps 1-2: find (or open) the pull request that lands `branch` onto `base`.

    NEVER REVERSES A HUMAN'S OWN DECISION (mirrors `unit_completion._draft`'s identical stance,
    BR-11): a landing PR a person already CLOSED without merging stays closed -- reopening it here
    would be this module overruling a choice that was never its to make. A landing PR already
    MERGED means the work already landed; there is nothing left to do, not an error.

    Returns `{"outcome": ..., "number": int|None, "why": str, "ownership": str|None}`. Never raises -- every `gh` call is
    guarded, and a guard's own failure is reported as REFUSED rather than propagated."""
    remote = rebase_brief.resolve_remote(config)
    repo_ref, owner_ref = unit_completion._repo_ref(config, run, cwd, remote)
    rows, error = unit_completion._landing_pull_requests(run, cwd, repo_ref, owner_ref, branch)
    if error is not None:
        return {"outcome": REFUSED, "number": None, "ownership": None,
                "why": "the pull requests on `%s` %s -- opening none rather than risking a "
                       "duplicate." % (branch, error)}
    open_pr, ended_pr = unit_completion._pick_landing(rows)
    if open_pr is not None:
        number = open_pr.get("number")
        owned = DURABLE_RECEIPT if _validated_landing_receipt(sdlc_dir, run, cwd, branch, number) else None
        if open_pr.get("draft") is True:
            try:
                run(cwd, ["gh", "pr", "ready", str(number)])
            except Exception as exc:          # noqa: BLE001 - a refused ready is reported, never a crash
                return {"outcome": REFUSED, "number": number, "ownership": owned,
                        "why": "%s is a draft and could not be marked ready (%s)."
                               % (unit_completion._pr_ref(open_pr), _flat(exc))}
            return {"outcome": READIED, "number": number, "ownership": owned,
                    "why": "%s was a draft -- marked ready." % unit_completion._pr_ref(open_pr)}
        return {"outcome": EXISTS_READY, "number": number, "ownership": owned,
                "why": "using the already-open %s." % unit_completion._pr_ref(open_pr)}
    if ended_pr is not None:
        merged = bool(ended_pr.get("merged_at"))
        if merged:
            number = ended_pr.get("number")
            owned = DURABLE_RECEIPT if _validated_landing_receipt(sdlc_dir, run, cwd, branch, number) else None
            return {"outcome": ALREADY_MERGED, "number": number, "ownership": owned,
                    "why": "%s already merged -- `%s` is already landed." %
                           (unit_completion._pr_ref(ended_pr), branch)}
        return {"outcome": DECLINED, "number": ended_pr.get("number"), "ownership": None,
                "why": "%s was already closed without merging -- that was somebody's decision and "
                       "reversing it is not this tool's to make." % unit_completion._pr_ref(ended_pr)}
    if not (isinstance(base, str) and base.strip()):
        return {"outcome": REFUSED, "number": None, "ownership": None,
                "why": "no integration branch resolved (work.base is empty) -- opening no pull "
                       "request against a guess."}
    try:
        out = run(cwd, ["gh", "pr", "create", "--base", base, "--head", branch, "--fill"])
    except Exception as exc:                  # noqa: BLE001 - a refused create is reported, never a crash
        return {"outcome": REFUSED, "number": None, "ownership": None,
                "why": "no landing pull request exists and one could not be opened (%s)." % _flat(exc)}
    number = _parse_created_pr_number(out)
    try:
        if sdlc_dir:
            _persist_landing_receipt(sdlc_dir, run, cwd, branch, number)
    except Exception as exc:  # a created PR without durable attribution must not be landed by us
        return {"outcome": REFUSED, "number": number, "ownership": None,
                "why": "opened a landing PR but could not persist its ownership receipt (%s)." % _flat(exc)}
    return {"outcome": CREATED, "number": number,
            "ownership": DURABLE_RECEIPT if sdlc_dir else None,
            "why": "opened a new landing pull request (`%s` -> `%s`)%s."
                   % (branch, base, " -- #%d" % number if number else "")}


# --------------------------------------------------------------------------- the merge itself (§7 step 3-4)

MERGE_METHOD = "merge"    # see module docstring -- the measured precedent, never squash


def merge_pr(run, cwd, number):
    """`gh pr merge <number> --merge` -- a plain, standalone call (design §7 step 3), never routed
    through `work.py merge()` or `unit_completion.py`'s merge-adjacent paths (module docstring).
    Carries no delete flag -- §13b's no-autonomous-delete invariant (BR-26)."""
    try:
        run(cwd, ["gh", "pr", "merge", str(number), "--%s" % MERGE_METHOD])
    except Exception as exc:                  # noqa: BLE001 - a refused merge is reported, never a crash
        return {"ok": False, "why": "PR #%s was NOT merged: %s" % (number, _flat(exc))}
    return {"ok": True, "why": "PR #%s merged (%s)." % (number, MERGE_METHOD)}


def record_merge(sdlc_dir, config, branch, number, why, run=None, cwd=None, ownership=None):
    """Observe the landing through GitHub's canonical PR facts before recording either stream.

    A feature landing has no goal work record, but it must not bypass the ownership/key scheme
    used by goal landings.  The REST response supplies the immutable repository, PR, head and
    merge facts; ``merge_observation`` derives both semantic keys.  If those facts cannot be
    read, the already-completed merge remains successful and this best-effort observation is left
    pending rather than inventing a key from local branch text.  An existing PR on the branch might
    be a human's, so this writer requires the canonical local parent receipt emitted only when
    ``ensure_landing_pr`` created the PR.  Every later process validates that receipt against
    GitHub before it emits either sink.
    """
    wants_entries = ledger.enabled(config)
    wants_journal = ledger.journal_on(sdlc_dir, config)
    if not wants_entries and not wants_journal:
        return None
    if run is None or cwd is None:
        return None
    try:
        raw = _read_canonical_pr(run, cwd, number)
        merge_sha, merged_at = raw.get("merge_commit_sha"), raw.get("merged_at")
        if (raw.get("number") != int(number) or not merged_at
                or not isinstance(merge_sha, str) or not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", merge_sha)):
            raise ValueError("canonical PR merge facts are incomplete")
        facts = _canonical_landing_facts(raw, branch)
        response = _receipt_request({"action": "observation-keys", "sdlc_dir": str(sdlc_dir),
                                     "facts": facts, "merge_sha": merge_sha})
        if response.get("valid") is not True:
            return None
        ownership = response.get("ownership_key")
        entry_key, observation_key = response.get("entry_key"), response.get("observation_key")
        if not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
                   for value in (ownership, entry_key, observation_key)):
            return None
    except Exception:  # noqa: BLE001 - a completed landing must not be reported as failed
        return None
    path = pathlib.Path(sdlc_dir) / "state" / "feature-merge-deliveries" / (entry_key + ".json")
    try:
        delivery = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        delivery = {}
    if delivery.get("entry_key") not in (None, entry_key) or delivery.get("observation_key") not in (None, observation_key):
        return None
    delivery.update({"branch": str(branch), "pr": int(number), "entry_key": entry_key,
                     "observation_key": observation_key, "entry_delivered": bool(delivery.get("entry_delivered")) or not wants_entries,
                     "journal_delivered": bool(delivery.get("journal_delivered")) or not wants_journal})
    _try_write_delivery(path, delivery)
    if not delivery["entry_delivered"]:
        if ledger.safe_append(sdlc_dir, "merged", branch, config=config, pr=str(number), why=why,
                              merged_entry_key=entry_key) is not None:
            delivery["entry_delivered"] = True; _try_write_delivery(path, delivery)
    if not delivery["journal_delivered"]:
        if ledger.safe_append(sdlc_dir, "merge_observed", branch, config=config, stream=ledger.EVENTS,
                              observation_key=observation_key, subject_kind="branch", subject=str(branch),
                              pr=int(number), merge_sha=merge_sha) is not None:
            delivery["journal_delivered"] = True; _try_write_delivery(path, delivery)
    return {"entry_key": entry_key, "observation_key": observation_key}


# --------------------------------------------------------------------------- the whole tail (§6-§7)

NO_COMMAND = "no-command"
VERIFY_FAILED = "verify-failed"
DECLINED_MERGE = "declined-merge"
MERGED = "merged"
MERGE_FAILED = "merge-failed"


def verify_and_offer_merge(run, cwd, sdlc_dir, config, branch, base, decide):
    """The whole verify-then-ask-to-merge tail, as one testable, driveable function -- mirroring
    `conflict_walk.walk_conflicts(run, cwd, brief, decide)`'s own shape (issue #2306's own framing):
    `decide` is the injected seam, a `() -> bool` callable ("merge now?"), never a hardcoded
    `input()` inside this function. `main()`'s own `_interactive_decide` builds a real one from
    `input()`; a test supplies a canned one instead. Neither this function nor anything it calls
    ever prompts directly.

    Runs `verify.command` first and ALWAYS reports it (issue item 2); only a green result reaches
    `decide()` at all (issue item 3: "always asked, never automatic", and only once green). On
    "no", stops -- everything is left exactly as it is (rebased/resolved, not merged), matching the
    issue's own words. Never raises.

    Returns `{"stage": ..., "outcome": ..., "verify": {...}, "landing": {...}?, "merge": {...}?}`."""
    cmd = verify_command(config)
    if not cmd:
        return {"stage": "verify", "outcome": NO_COMMAND, "verify": None}
    result = run_verify_command(cmd, cwd)
    if not result["ok"]:
        return {"stage": "verify", "outcome": VERIFY_FAILED, "verify": result}
    if not decide():
        return {"stage": "verify", "outcome": DECLINED_MERGE, "verify": result}
    landing = ensure_landing_pr(run, cwd, config, branch, base, sdlc_dir=sdlc_dir)
    if landing["outcome"] == ALREADY_MERGED:
        # Unconditional (round 4, B2): matches the base branch's own unconditional write, and no
        # longer depends on whether THIS invocation happened to be the one that created the PR.
        record_merge(sdlc_dir, config, branch, landing["number"], landing["why"], run=run, cwd=cwd,
                     ownership=landing.get("ownership"))
        return {"stage": "land", "outcome": landing["outcome"], "verify": result, "landing": landing}
    if landing["outcome"] in (REFUSED, DECLINED):
        return {"stage": "land", "outcome": landing["outcome"], "verify": result, "landing": landing}
    merged = merge_pr(run, cwd, landing["number"])
    if merged["ok"]:
        record_merge(sdlc_dir, config, branch, landing["number"], merged["why"], run=run, cwd=cwd,
                     ownership=landing.get("ownership"))
    return {"stage": "merge", "outcome": MERGED if merged["ok"] else MERGE_FAILED,
            "verify": result, "landing": landing, "merge": merged}


# --------------------------------------------------------------------------- a real interactive CLI


def _interactive_decide():
    """The real CLI's own decision provider -- `input()`, entirely outside
    `verify_and_offer_merge` (see that function's own docstring). The one place this module blocks
    on a human, matching `conflict_walk._interactive_decide`'s identical posture."""
    while True:
        choice = input("Verify passed. Merge now? [y/N]: ").strip().lower()
        if choice in ("y", "yes"):
            return True
        if choice in ("", "n", "no"):
            return False
        print("Please answer y or n.")


USAGE = "usage: verify_merge.py land <sdlc_dir> [branch]"

UNIT_BRANCH_PREFIX = "feature/"


def engine_exit_code(outcome):
    """The exit code for a landing-engine outcome (upkeep gate open only): landed or already landed 0, a refusal 1, an
    unconfirmed landing (the pending record stays; the next run settles it) 3."""
    outcome = str(outcome)
    if outcome in ("merged", "merged-with-warning", "already-landed", "rehearsal", "armed"):
        return 0
    return 3 if outcome.startswith("unconfirmed") else 1


def _engine_route(config, sdlc_dir, branch, decide):
    """-> an exit code when the upkeep gate is open and `branch` is a unit branch (the engine ran), else None and the
    closed path below runs unchanged. The gate is read through `feature_upkeep` only; nothing is loaded while closed."""
    try:
        if not _load("feature_upkeep", _LOOP_SCRIPTS).enabled(config):
            return None
    except Exception:  # noqa: BLE001 - a gate that cannot answer is closed
        return None
    if not branch.startswith(UNIT_BRANCH_PREFIX):
        return None
    unit = branch[len(UNIT_BRANCH_PREFIX):]
    if not decide():
        print("\nnot merging -- the tree is left exactly as it is.")
        return 0
    engine = _load("feature_land", _LOOP_SCRIPTS)
    out = engine.land(config, sdlc_dir, unit, argv=["--user-requested", unit], environ=os.environ, merge=True)
    if out.get("closed"):
        return None
    print(out["outcome"] + (" (%s)" % out["detail"] if out.get("detail") else ""))
    return engine_exit_code(out["outcome"])


def main(argv):
    """`verify_merge.py land <sdlc_dir> [branch]` -- run `verify.command` against the current
    working tree and report it; only once green, ask whether to merge; on yes, land `branch` onto
    `work.base` (design §6-§7)."""
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    if len(argv) < 3 or argv[1] != "land":
        print(USAGE, file=sys.stderr)
        return 2
    sdlc_dir = argv[2]
    explicit = argv[3] if len(argv) >= 4 else None
    config = state.load_config(sdlc_dir)
    cwd = str(pathlib.Path(sdlc_dir).parent)
    run = feature_rebase._run
    branch = rebase_brief.resolve_branch(run, cwd, explicit)
    base = rebase_brief.resolve_base(config)
    if not base or base == branch:
        print("no integration branch to compare against (work.base is %r)" % base, file=sys.stderr)
        return 1
    if rebase_brief.current_branch(run, cwd) != branch:
        print("`%s` is not the currently checked-out branch -- verify needs the real working "
              "tree, not attempted." % branch, file=sys.stderr)
        return 1
    if feature_rebase.rebase_stopped(run, cwd):
        print("a rebase is still stopped in this tree -- resolve it first (see `conflict_walk.py "
              "walk`) before verifying.", file=sys.stderr)
        return 1
    routed = _engine_route(config, sdlc_dir, branch, _interactive_decide)
    if routed is not None:
        return routed
    report = verify_and_offer_merge(run, cwd, sdlc_dir, config, branch, base, _interactive_decide)
    verify_result = report.get("verify")
    if report["outcome"] == NO_COMMAND:
        print("no verify.command configured in .sdlc/config.json -- nothing to run.", file=sys.stderr)
        return 3
    cmd = verify_command(config)
    print(format_verify_report(cmd, cwd, verify_result))
    if report["outcome"] == VERIFY_FAILED:
        return 1
    if report["outcome"] == DECLINED_MERGE:
        print("\nnot merging -- the tree is left exactly as it is.")
        return 0
    landing = report["landing"]
    print("\n%s" % landing["why"])
    if report["outcome"] == REFUSED:
        return 1
    if report["outcome"] == ALREADY_MERGED:
        return 0
    if report["outcome"] == DECLINED:
        return 0
    merge = report["merge"]
    print(merge["why"])
    return 0 if report["outcome"] == MERGED else 1


def _state_guard(argv):
    """#708: refuse a committed symlink under .sdlc/state or .sdlc/journey before any write."""
    import importlib.util as _u
    import pathlib as _p
    spec = _u.spec_from_file_location("_guard_state", _p.Path(__file__).resolve().parent.parent.parent / "sigma-loop" / "scripts" / "state.py")
    mod = _u.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.guard_argv(argv, _p.Path(__file__).name)


if __name__ == "__main__":
    raise SystemExit(_state_guard(sys.argv) or main(sys.argv))
