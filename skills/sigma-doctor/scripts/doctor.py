#!/usr/bin/env python3
"""sigma-doctor: a setup check-up. Audit only what THIS project's config makes relevant — github board
-> gh auth + project scope; KG enabled -> the builder; vision-first -> the north-star; always -> the
.sdlc layer — and report each check with the exact one-line fix. The command runner is injectable so
the logic is hermetically testable. Zero-dep."""
import sys, json, os, pathlib, re, shutil, subprocess, importlib.util, importlib.metadata

try:                    # portable output: force UTF-8 so the plugin's own non-ASCII (arrows, em-dashes)
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")   # doesn't garble to '?' or
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")   # crash on a non-UTF-8 console
except Exception:       # (the Windows cp1252 default); a stream without reconfigure is left as-is
    pass

_HERE = pathlib.Path(__file__).resolve().parent

# graphify emits this warning while still exiting successfully, so a presence-only probe would
# report a healthy builder even though every extraction uses the stale bundled skill. Keep the
# pattern intentionally narrow: other successful version diagnostics are not Sigma's to classify.
_GRAPHIFY_SKILL_PACKAGE_MISMATCH = re.compile(
    r"skill is from graphify\s+([0-9][0-9A-Za-z._+-]*),\s*package is\s+([0-9][0-9A-Za-z._+-]*)",
    re.IGNORECASE)


class _RawFailure(str):
    """An empty string for every existing caller of `run(...)` — `bool()`, `in`, `json.loads()` all
    treat this exactly like the old bare `""` already did, so every OTHER check in `check()` keeps
    reading a failed call as falsy, unchanged. It ALSO carries the real combined stdout+stderr on
    `.raw`, for the one caller that needs it (#78): the "gh auth" check below, which has to tell a
    Claude Code Remote session's gh proxy block apart from a genuinely missing/bad token to print a
    remediation that is actually correct — see `gh_session.py`'s module docstring for the two
    confirmed proxy shapes. `getattr(x, "raw", "")` reads `""` for every fake `run` in
    test_doctor.py too (none of them know this attribute exists), so this is additive only: no
    existing check's pass/fail signal changes, hermetic or real."""
    def __new__(cls, raw):
        obj = super().__new__(cls, "")
        obj.raw = raw
        return obj


def _failure_text(result):
    """THE runner-failure contract, read in one place: the text a FAILED `run(...)` carried (gh's
    stderr + stdout on `_RawFailure.raw`), or "" for a success or a runner that does not carry it
    (every plain-string fake). A check that must tell "GitHub answered no" from "the call itself
    failed" (`_pinned_board_state`, the preflight adapter) reads it through this, so the producer
    (`_real_run`) and those consumers cannot drift apart silently -- pinned by
    tests/test_doctor.py::test_runner_failure_contract_reaches_the_pinned_board_row_235."""
    return str(getattr(result, "raw", "") or "")


def _real_run(args):
    # `/sigma-doctor` reaches GitHub only because this repository opted into github discovery,
    # but an unavailable API must not turn that diagnostic into an unbounded session start.
    # Reuse init's derived network budget so the two entry points cannot disagree. Local
    # diagnostics intentionally retain their existing uncapped behavior.
    timeout = None
    if args and args[0] == "gh":
        try:
            timeout = _load_init_script("preflight").network_timeout()
        except Exception:  # the doctor still needs a finite bound if its sibling cannot load
            timeout = 15.0
    import subprocess
    try:
        kwargs = {"capture_output": True, "text": True}
        if timeout is not None:
            kwargs["timeout"] = timeout
        p = subprocess.run(args, **kwargs)
        if p.returncode == 0:
            return p.stdout + p.stderr
        return _RawFailure(p.stdout + p.stderr)
    except subprocess.TimeoutExpired:
        command = " ".join(str(part) for part in args[:2])
        return _RawFailure(f"{command}: timed out after {timeout:g}s")
    except Exception as exc:
        return _RawFailure(str(exc))


def _bounded_run(args, timeout=None):
    """`_real_run`'s return contract (the output text, or a `_RawFailure` carrying it), with a time limit on EVERY
    command, local or not. `_real_run` leaves local probes uncapped on purpose (a slow local diagnostic must never be
    reclassified as a missing tool), so a caller that cannot afford to hang on a wedged binary calls this instead: the
    upkeep readiness rows of a later slice. Built on init's runner, which stops the whole process tree on an overrun
    and returns 124; the limit is the fleet's per-call bound unless the caller passes one. Never raises."""
    try:
        preflight = _load_init_script("preflight")
        limit = preflight.call_timeout() if timeout is None else timeout
        code, text = preflight.real_runner([str(a) for a in args], timeout=limit)
    except Exception as exc:
        return _RawFailure("%s: the bounded probe could not run (%s)" % (args[0] if args else "", exc))
    return text if code == 0 else _RawFailure(text)


def _cfg(sdlc_dir):
    try:
        data = json.loads((pathlib.Path(sdlc_dir) / "config.json").read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}   # a non-dict config.json reads as empty, never crashes


def _block(cfg, name):
    """A config BLOCK read that can't crash on a shape typo. `cfg.get(name)` alone is not enough: a
    truthy NON-dict value (e.g. `{"verify": "pytest"}`) sails past `or {}` unchanged, and every
    `.get()` a caller then does on it raises `AttributeError` — in the one tool an adopter runs
    *because* their config is wrong. Every block reader in this file goes through this (including
    nested ones — pass the parent's OWN `_block()` result back in), so a malformed block anywhere
    degrades to reading as off/absent instead of aborting the whole check/features/dashboard run.
    Guards `cfg` itself too (not just the extracted value) — a caller holding a non-dict `cfg` (e.g.
    a board-helper's `gh_cfg` reached some other way) gets `{}` instead of an AttributeError on the
    `.get(name)` call, so this composes safely with itself at any nesting depth."""
    if not isinstance(cfg, dict):
        return {}
    value = cfg.get(name)
    return value if isinstance(value, dict) else {}


#: One year. The ceiling both gates clamp `plan_freshness_hours` to before it reaches their
#: `$(( hours * 60 ))`, which is 64-bit and wraps above (2^63-1)/60 ~= 1.537e17. Any window past a
#: year already means "effectively never expires", so clamping DOWN preserves that intent, while
#: falling back to the 24h default would make a deliberately-large working config 4000x stricter.
#: Must stay identical to the literal in both hook scripts — the shape tripwire asserts it there.
_MAX_WINDOW_HOURS = 8760


def _effective_window(gate):
    """The freshness window a gate ACTUALLY enforces, which is not always the one in config.

    This dashboard's whole job is to tell an adopter what is switched on, so rendering
    `plan_freshness_hours` raw was worse than useless: `"24h"` printed as `ON (24hh window)` and
    `-5` as `ON (-5h window)`, advertising settings both gates reject. Same crying-wolf failure the
    hygiene checks are built to avoid, one surface over.

    Mirrors `plan_gate.sh` / `completion_gate.sh`'s rule exactly, and it is THREE steps, not one — a
    single `int()` would still report `-5`:
      1. an inner `try` around `int(value or 24)`, so an unparseable freshness cannot corrupt the
         gate's own mode line (that separation is why the gates read it independently at all);
      2. a clamp to `_MAX_WINDOW_HOURS`, because the shell's `$(( hours * 60 ))` is 64-bit and wraps
         above (2^63-1)/60 — the digits guard below bounds the value's SHAPE, never its MAGNITUDE
         (#602);
      3. a digits-only guard on the result, which is what actually rejects a negative before it can
         reach the shell's `$(( ))`.
    `or 24` in step 1 is load-bearing in its own right — but only for the INT `0` and a null, which
    are falsy and so hand the gates the default. The STRING `"0"` is truthy, survives the digits
    guard, and really does configure a zero-hour window that both gates enforce; this function
    reports it, and a matrix row pins that agreement rather than papering over it.
    Kept in lockstep by test — the value is read from the same key by three files, and only this one
    has to answer for what the other two decided."""
    try:
        hours = int(gate.get("plan_freshness_hours") or 24)
    except Exception:                        # noqa: BLE001 - any unparseable value means "default"
        hours = 24
    hours = min(hours, _MAX_WINDOW_HOURS)
    return hours if str(hours).isdigit() else 24


def _verify_trusted(repo):
    """#615: has THIS checkout granted the git-local `sigma.allowRepositoryShellCommands` that lets
    `loop.py verify` run a repository-configured command? Duplicated from sigma-loop's `shell_policy`
    (this script stays standalone, like `_enforce_enabled`; a parity test pins the two together).
    Fail closed: any read failure is "not trusted"."""
    try:
        r = subprocess.run(["git", "-C", str(repo), "config", "--local", "--type=bool",
                            "sigma.allowRepositoryShellCommands"],
                           capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and r.stdout.strip().lower() == "true"


def _in_git_worktree(repo):
    try:
        r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
                           capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and r.stdout.strip() == "true"


def _chk(name, ok, fix):
    return {"name": name, "ok": bool(ok), "fix": "" if ok else fix}


def _enforce_enabled(verify):
    """`verify.enforce` as a bool, read generously — intentionally duplicated from loop.py's own
    `_enforce_enabled` (F17/#342) rather than imported: doctor.py is a standalone diagnostic script
    with no cross-skill import (every other helper here is self-contained too), and this check has
    to keep working even if something ELSE in the loop is broken. A strict `is True` let `enforce: 1`
    or `enforce: "true"` (easy JSON typos, both plainly meant as true) read as off HERE too — doctor
    would then correctly stay silent on a gate that isn't really enforcing... except once loop.py
    reads the SAME value generously (as it now does), the two sides go from "consistently wrong
    together" to doctor actively lying that the gate is off while it is genuinely on and refusing
    every `done`. Keep this in lockstep with loop.py's copy — a parity test in test_doctor.py pins
    them to the same truth table so a future edit to one alone fails loudly, not silently."""
    value = verify.get("enforce")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "0", "no", "off")
    return bool(value)


def _gate_enabled(gate):
    """`gates.<name>.enabled` (hard_plan_gate / stop_gate) read generously — the SAME F17/#342
    direction as `_enforce_enabled` above (#416): both are hard DENY/block gates (the two hook
    scripts `hooks/plan_gate.sh` / `hooks/completion_gate.sh` refuse an edit or a Stop with them
    on), so a strict `is True` read has the identical unsafe failure direction as `verify.enforce`
    did — `enabled: 1` or `enabled: "true"` (easy JSON typos, both plainly meant as true) would
    silently leave a safety gate OFF while this dashboard kept reporting it that way, with nothing
    to say the two hooks (fixed to read generously too, same issue) now disagree with it. A
    separate helper rather than reusing `_enforce_enabled` under a misleading name: the two read
    different keys off different-shaped blocks (`verify.enforce` vs a gate block's own `enabled`)
    even though the LOGIC is identical on purpose — `test_gate_enabled_reads_the_full_generous_truth_table`
    (test_doctor.py) proves the two answer the same truth table on the same inputs, the same
    lockstep discipline `_enforce_enabled`'s own cross-module parity test already established.

    TOTAL SINCE #2116 — a NON-DICT `gate` is read as the `enabled` it plainly means, not as an
    `AttributeError` and not as off. `ledger.LOCKABLE_KEYS` names the BLOCK path
    `gates.hard_plan_gate`, so `{"gates": {"hard_plan_gate": true}}` is a legal
    Org policy, and `work.py`'s new host-agnostic enforcement point reads it as ON. This row calling
    the same config `off` would put doctor back in exactly the position the paragraph above warns
    against — "actively lying that the gate is off while it is genuinely on and refusing" — with the
    BLOCK, rather than the value, as the shape that diverges. The caller must stop coercing too:
    `_block` flattens a non-dict to `{}` before this function can see it, so the raw value has to be
    read at the derivation (see `features()`), and the test for this asserts through `features()`
    for that reason — a test written against this helper alone passes while the row still lies."""
    if not isinstance(gate, dict):
        gate = {"enabled": gate}
    value = gate.get("enabled")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ("", "false", "0", "no", "off")
    return bool(value)


#: `managed_settings.MANAGED_SETTINGS_FILENAME`, a deliberate second copy — doctor.py is a
#: standalone diagnostic with no cross-skill import by design (see `_enforce_enabled`), and this
#: file already carries several such copies for that reason. Pinned to the original by
#: `test_doctor.py::test_the_managed_settings_filename_matches_the_loops_own`.
_MANAGED_SETTINGS_FILE = "managed-settings.json"

#: `managed_settings.CONFIG_KEY` (#2580, D1), a second copy for the same reason as
#: `_MANAGED_SETTINGS_FILE`, pinned to the loop's own by
#: `test_doctor.py::test_managed_settings_adoption_matches_the_loops_own`. The only adoption block
#: read: #2706 dropped the one-release read of the old block name, and its notice with it.
_MANAGED_SETTINGS_KEY = "managed_settings"

#: `features.BRANCH_PREFIX`, the SAME deliberate-second-copy idiom as `_MANAGED_SETTINGS_FILE`
#: above (doctor.py has no cross-skill import by design — see `_enforce_enabled`). Used only by
#: `_open_unit_branch` below to recognise a `feature/<name>` unit branch by name before ever asking
#: the registry about it. Pinned to the original by
#: `test_doctor.py::test_the_unit_branch_prefix_matches_the_loops_own`.
_UNIT_BRANCH_PREFIX = "feature/"


def _declared_managed_settings_id(cfg):
    """`managed_settings.declared_project_id`, copied (see `_MANAGED_SETTINGS_KEY`): the
    `_MANAGED_SETTINGS_KEY` block's stripped `project_id`, or None."""
    cfg = cfg if isinstance(cfg, dict) else {}
    section = cfg.get(_MANAGED_SETTINGS_KEY)
    if not isinstance(section, dict):
        return None
    declared = section.get("project_id")
    return declared.strip() if isinstance(declared, str) and declared.strip() else None


def _managed_enrolled(base):
    """Was a valid policy file ever read here (#423)? Cross-loads the loop's own
    `managed_settings.is_enrolled` rather than copying the marker layout, so doctor and the gates
    cannot disagree about where enrolment lives. Fails open to False (the pre-#423 reading)."""
    try:
        return bool(_load_loop_script("managed_settings").is_enrolled(base))
    except Exception:                      # noqa: BLE001 - a diagnostic never raises
        return False


def _managed_settings_adopted(base, cfg):
    """Has this checkout opted into org policy? `managed_settings.is_adopted`'s two free signals,
    copied: a declared project id (`_MANAGED_SETTINGS_KEY`), or the
    managed-settings file sitting beside the config, or (#423) an enrolment marker left by a file
    that has since been deleted. Dict reads and a few `stat`s — no subprocess, and
    False for every install in the wild. The gate itself has read nothing else since S1-G5."""
    if _declared_managed_settings_id(cfg):
        return True
    try:
        if (pathlib.Path(base) / _MANAGED_SETTINGS_FILE).exists():
            return True
    except OSError:
        pass
    return _managed_enrolled(base)


def _managed_settings_state(base, cfg):
    """State of the managed-settings.json file for org policy.

    Returns one of:
    - "absent" if the file does not exist and was never enrolled
    - "enrolled, policy file MISSING ..." if it does not exist but once did (#423)
    - "unreadable (error description)" if the file exists but cannot be read
    - "active with N locked keys" if the file is valid and status is "ok"
    - "revoked (member denied)" if status is "access-revoked"
    - "unreadable (server verification failed)" if status is "locked-key-unverifiable"
    - "unreadable (unknown status)" if status is unrecognized

    One-line string, ≤ 100 characters."""
    file_path = pathlib.Path(base) / _MANAGED_SETTINGS_FILE
    if not file_path.exists():
        if _managed_enrolled(base):
            return "enrolled, policy file MISSING (restore it or run managed_settings.py unenroll)"
        return "absent"

    try:
        content = file_path.read_text(encoding="utf-8")
        import json
        data = json.loads(content)
    except (OSError, UnicodeDecodeError) as e:
        return "unreadable (I/O error: %s)" % type(e).__name__
    except json.JSONDecodeError:
        return "unreadable (invalid JSON)"

    if not isinstance(data, dict):
        return "unreadable (invalid JSON)"

    # Validate version
    if data.get("version") != 1:
        return "unreadable (unknown version)"

    # Extract and check status
    status = data.get("status")
    if status == "ok":
        # Count locked keys
        locked = data.get("locked")
        if isinstance(locked, dict):
            count = len([k for k, v in locked.items() if v is not None])
            return "active with %d locked keys" % count
        return "active with 0 locked keys"
    elif status == "access-revoked":
        return "revoked (member denied)"
    elif status == "locked-key-unverifiable":
        return "unreadable (server verification failed)"
    else:
        return "unreadable (unknown status)"


#: The hard-plan-gate row's hint on an adopted checkout (#2580): it names the file the gate reads,
#: which exists in a core-only install -- never a command a core-only install does not have.
_ORG_LOCK_HINT = ("an org policy lock may differ — check `locked` in `.sdlc/%s`"
                  % _MANAGED_SETTINGS_FILE)


def _hard_plan_gate_state(base, cfg, gate, gate_window, wk):
    """The `hard plan-gate` row's state string (#2116). Four outcomes, and the two new ones both
    exist to stop this row asserting something it cannot see.

    Until #2116 the key had ONE outcome-changing reader, `hooks/plan_gate.sh` — a Claude Code
    `PreToolUse` hook — so an Org that locked it got enforcement on one host and nothing on Cursor
    or Codex. `work.py`'s `pr()` is now the host-agnostic point. That makes two states this row has
    to tell apart rather than flatten:

      * **ON but `work.enabled` off.** `work.py main()` refuses every verb in that configuration, so
        `pr()` never runs and the hook is again the only enforcement — exactly the shape
        `sdlc_init.py --cursor` pins. Reporting a plain "ON" there would present a lock as enforced
        where it is not, which is the "silent half-guarantee" AGENTS.md rejects.
      * **off in local config, on an ADOPTED checkout.** `_cfg` reads `<sdlc_dir>/config.json` and
        nothing else — doctor resolves no org policy itself — so on an adopted checkout an Org lock
        may say ON while this file says off. Printing "off" there is not silence, it is a fabricated
        reading. So the row says what it actually read, and names the one file that holds the org
        lock (`locked` in `.sdlc/managed-settings.json`), which is true in a core-only install too.
        A downstream config cache was rejected as the source: it is up to one poll interval stale,
        and a second stale source of truth is worse than pointing at the file the gate reads. An
        UNADOPTED checkout — every install in the wild — keeps the old string byte for byte."""
    if _gate_enabled(gate):
        window = _effective_window(gate_window)
        if _work_enabled(wk):
            row = (f"ON ({window}h window) — Claude Code hook denies the edit; `work.py pr` "
                   "refuses the push on every host")
            # The mirror of the off-direction caveat below, and it needs saying for the same
            # reason: this file reads LOCAL config only, so an Org lock of `false` over a local
            # `true` would leave this row asserting enforcement that `pr()` has already resolved
            # away. An UNSET Org key correctly falls back to local, so this is the narrow
            # explicitly-locked-off case, not a general doubt.
            return row + (" (local config; %s)" % _ORG_LOCK_HINT
                          if _managed_settings_adopted(base, cfg) else "")
        return (f"ON ({window}h window) in config but NOT ENFORCED here — work.enabled is off, so "
                "`work.py pr` never runs; the Claude Code hook is the only enforcement and "
                "Cursor/Codex get none")
    if _managed_settings_adopted(base, cfg):
        return "off in local config — this checkout is adopted, so %s" % _ORG_LOCK_HINT
    return "off (prompt-gate reminder only)"


def _work_enabled(wk):
    """`work.enabled` as `work.py::enabled()` itself reads it — a plain `bool()`, NOT the generous
    truthiness `_gate_enabled` uses.

    The two differ, and copying the wrong one would be a silent lie in the opposite direction: on
    `{"work": {"enabled": "false"}}` a generous read says off, `work.enabled()` says ON, and every
    work verb runs. This row is about whether `work.py pr` actually executes, so it must answer the
    question the way the code that executes it does. Where the gate flags' generosity is the safe
    direction (#416), here matching the real reader is."""
    return bool(wk.get("enabled"))


def _board_dup_risk(gh_cfg, run):
    """Board mirroring on, but NO `project.number` pinned, and the owner already has board(s): sigma
    resolves the board by TITLE, and if none matches its auto-title it CREATES a new one on first use -
    silently duplicating a board the loop then manages instead of the adopter's. Returns a one-line fix
    when that risk is present, else None. NOT gated on `number` (it fires precisely when number is
    unset); read-only; a can't-read returns None (no false alarm). Catches #9 at setup."""
    proj = _block(gh_cfg, "project")
    if proj.get("number"):                     # a pinned number resolves directly - no create path
        return None
    repo = gh_cfg.get("repo") or ""
    owner = proj.get("owner") or (repo.split("/")[0] if "/" in repo else "@me")   # mirror sources._proj_owner
    raw = run(["gh", "project", "list", "--owner", owner, "--format", "json", "--limit", "100"])
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    boards = (data.get("projects") if isinstance(data, dict) else data) or []
    if not boards:                             # nothing to duplicate - a fresh create is safe
        return None
    name = repo.split("/")[-1] if "/" in repo else (repo or "project")
    target = proj.get("title") or f"{name} — SDLC"   # mirror sources._proj_title (em-dash: it byte-matches)
    if any(b.get("title") == target for b in boards):
        return None                            # sigma's title already resolves - reused, not duplicated
    titles = ", ".join(b.get("title", "") for b in boards[:4] if b.get("title"))
    return (f"board mirroring is on with NO project.number, and {owner} already has board(s) "
            f"({titles}) - sigma resolves by title and will CREATE a new board if none matches "
            "its auto-title, silently duplicating one. Set discovery.github.project.number, or "
            "create and pin one: python3 <sigma>/skills/sigma-init/scripts/board_setup.py create "
            ".sdlc (read-only preview; add --yes to create).")


#: What gh prints when GitHub ANSWERED and the board (or its owner) is not there. Measured:
#: `gh project view 99999 --owner <org>` -> "GraphQL: Could not resolve to a ProjectV2 with the
#: number 99999."; an owner login that does not exist -> "unknown owner type".
_BOARD_GONE = ("could not resolve to a", "unknown owner type")


def _pinned_board_unreachable(gh_cfg, run):
    """The fix line when `_pinned_board_state` says "gone", else None."""
    state, fix = _pinned_board_state(gh_cfg, run)
    return fix if state == "gone" else None


def _pinned_board_state(gh_cfg, run):
    """(state, fix): "ok", "gone" (with the fix), "unverifiable", or "unpinned".

    #235: `project.number` is pinned but GitHub says that board does not exist under its owner --
    deleted, transferred, or a wrong `project.owner`. The loop then turns board mirroring OFF on
    every run (`_warn_board_unresolved`), so the pin is only worth what this row confirms. Returns
    a one-line fix, else None.

    None -- no alarm, and the caller emits NO row (never a pass) -- whenever the read itself failed:
    offline, rate-limited, gh missing or killed, or a token without the `project` scope (the
    "gh project scope" row already names that). A failed read says nothing about the board, and a
    FAIL there would send an operator to "fix" a pin that is fine. Only a read GitHub ANSWERED
    (`_BOARD_GONE`) is an alarm. None too when no number is pinned: that case is
    `_board_dup_risk`'s ("unpinned"). One GraphQL-backed read (`gh project view`), the same verb the loop's own
    resolution falls back to for a pin outside the first 100 boards; the caller gates it out of
    `cheap_only`."""
    proj = _block(gh_cfg, "project")
    number = proj.get("number")
    if number in (None, ""):
        return "unpinned", None
    repo = gh_cfg.get("repo") or ""
    owner = proj.get("owner") or (repo.split("/")[0] if "/" in repo else "@me")
    try:
        want = int(number)
    except (TypeError, ValueError):
        return "gone", f"project.number {number!r} is not a number - set it to the board's number."
    raw = run(["gh", "project", "view", str(want), "--owner", owner, "--format", "json"])
    if raw:
        try:
            got = json.loads(raw)
        except Exception:
            got = None
        # an answer that is not this board is odd, but it is not GitHub saying the board is gone
        return ("ok" if isinstance(got, dict) and got.get("number") == want
                else "unverifiable"), None
    said = _failure_text(raw).lower()
    if not any(tell in said for tell in _BOARD_GONE):
        return "unverifiable", None  # the read failed -- says nothing about the board
    return "gone", (f"the pinned board #{want} does not exist under {owner} (GitHub: could not "
                    "resolve it -- deleted, moved, or a wrong project.owner) - the loop mirrors "
                    "nothing while it is unreachable. Fix discovery.github.project.number/owner, "
                    "or create and pin a new one: python3 "
                    "<sigma>/skills/sigma-init/scripts/board_setup.py create .sdlc.")


def _self_merge_risk(gh_cfg, wk, run):
    """The specific hazardous combination issue #821 exists to warn about: `auto_merge: "always"` +
    `require_review: "approval"` + no branch protection on the base. `require_review: "approval"`
    LOOKS like a real gate, but its self-authorship fallback (`sigma:approve` as a comment,
    since GitHub structurally forbids approving your own PR) is satisfiable by ANY comment on the
    PR -- so with no branch protection standing behind it, nothing independent ever has to look at
    a diff before it reaches the base. Live incident that motivated this check: another deployment
    of this plugin self-reviewed and self-merged a PR (zero GitHub-native reviews, the only signal
    a self-posted `sigma:approve`) straight onto an unprotected `main`.

    Returns a one-line fix when the combination is present, else None. `require_review: "changes"`
    is NOT flagged -- it never requires a comment to merge at all (only the ABSENCE of a block), a
    materially weaker claim to begin with, so there is no false-approval illusion to warn about.

    Read-only, and deliberately fail-open in a specific way: the branch-protection endpoint returns
    an EMPTY result both when a branch is genuinely unprotected (a real 404) and when the API call
    could not be reached at all (auth, network, wrong repo) -- and for THIS check, unlike
    `_board_dup_risk`'s, empty IS the signal being tested for, so blindly trusting an empty result
    would report reachability failures as a security finding. A cheap canary call (`repos/<repo>`)
    is read FIRST: only once it proves the repo/auth path itself works does an empty protection
    read count as a genuine 404, so a can't-tell still returns None -- no false alarm, matching the
    file's own convention, adapted for a check whose signal is emptiness rather than content."""
    if wk.get("enabled") is not True:
        return None
    auto = wk.get("auto_merge")
    auto = "always" if auto is True else (str(auto).strip().lower() if auto else "off")
    if auto != "always":
        return None
    review = wk.get("require_review")
    review = "approval" if review is True else (str(review).strip().lower() if review else "off")
    if review != "approval":
        return None
    repo = gh_cfg.get("repo") or ""
    if not repo:
        return None
    base = wk.get("base") or "main"           # doctor has no live checkout to resolve an unset base from
    canary = run(["gh", "api", f"repos/{repo}", "--jq", ".id"])
    if not canary:
        return None                            # can't even reach the repo -- no verdict, not a false alarm
    protection = run(["gh", "api", f"repos/{repo}/branches/{base}/protection", "--jq", ".url"])
    if protection:
        return None                            # genuinely protected -- something IS enforced independently
    return (f"work.auto_merge is \"always\" and require_review is \"approval\", but that mode's "
            f"self-authorship fallback (a `sigma:approve` COMMENT, not a real review -- GitHub "
            f"forbids approving your own PR) is satisfiable by any OWNER, MEMBER or COLLABORATOR commenter (the PR author included), and {base!r} has NO "
            "branch protection. Nothing independent stands between an unattended merge and this "
            "branch. Add branch protection requiring an approval from a second identity, or accept "
            "the risk knowingly.")


def _open_unit_branch(sdlc_dir, branch):
    """#2452 follow-up (independent review on PR #2461, blocking finding): is `branch` itself the
    registered, OPEN base of a `feature/<name>` unit (`.sdlc/features/`)? -> True if so.

    `_stray_commits_after_merge` below cannot treat "this branch's own most recent PR merged or
    closed" as proof of abandonment for a `feature/<name>` unit branch: `docs/branching-model.md`
    §13/§13b make that branch a long-lived integration branch, NEVER auto-deleted (§13b: "a hard
    invariant"), that legitimately keeps receiving further `sdlc/<goal>` PRs after its own
    historical completion PR into the default branch has already merged ("a unit whose branch
    merged away but whose goals are recorded is left open", §13). Reproduced live against this very
    repo: `feature/dangling-completion`'s own completion PR #2379 merged with
    `headRefOid=278bb7647c896d5c3aaea970b3064383beec248a`, and that branch sits 6 ordinary,
    independently-reviewed commits ahead of that head today (`18045132`, `7ad7358c`, `5b892c95`,
    among others) -- none of them stray, all of them normal unit work continuing after completion.

    GATED ON OPEN, NOT MERELY REGISTERED. In the ORDINARY close path (§8f's rule 5: no branch and no
    recorded goals) a CLOSED unit's branch is exactly the abandoned-checkout shape this check exists
    to catch, and §13b makes the branch persist forever -- so "closed" is the best signal available
    that no further work is expected on it. `feature_registry.resolve_open_unit` already answers
    `None` for a closed unit BY DESIGN (see its own docstring: "a closed unit answers None,
    indistinguishable from unknown -- deliberately") -- exactly the behaviour wanted here: a closed
    unit's branch falls through to the ordinary stray-commit verdict below, unexempted, same as any
    other one-off branch.

    STATED PLAINLY, NOT SWEPT UNDER THE ORDINARY CASE: this is not the ONLY way a unit ends up
    closed. `feature_sync.PICKED_WHILE_CLOSED` (`skills/sigma-loop/scripts/feature_sync.py`) is a
    named divergence for the exact opposite shape -- "somebody closed this unit and somebody else
    picked a goal onto it," which the sync pass deliberately does not settle, only reports. In that
    narrow, already-anomalous window a closed unit's branch CAN legitimately keep receiving one more
    goal PR, and this function -- gated purely on `open` -- will not exempt it there, so
    `_stray_commits_after_merge` still flags it.

    THAT REPORT IS NOT A LEDGER ENTRY, AND THIS MUST NOT BE OVERSTATED THE WAY AN EARLIER DRAFT OF
    THIS DOCSTRING DID: `feature_sync.LEDGERED = (BRANCH_MISSING, CLOSED, LOST, SCOPE_EXPANSION)`
    deliberately excludes `PICKED_WHILE_CLOSED` -- `_surface`'s own docstring says why ("an
    observation that merely describes the ordinary world... belongs on the console of whoever is
    running the pick, where it costs nobody's attention twice"), so the only trace it leaves is one
    `_note()` stderr line at pick time, never a durable, owner-addressed `_tell()` record. This IS
    still an accepted, conservative gap, not an oversight -- but the reason is narrower than "already
    surfaced elsewhere": it is that closing the gate's OPEN-only design would need this function to
    read recorded-goal history, not just `open`, which is materially more than a false-positive fix
    should take on. A `/sigma-doctor` false alarm here is, at worst, a second, more durable echo of a
    state that ALREADY went unrecorded once. Revisit only if this combination is measured causing
    real doctor noise.

    REUSES THE EXISTING READER, `feature_registry.resolve_open_unit`, rather than re-deriving unit
    detection -- loaded via `_load_loop_script`, the same cross-skill-reuse idiom this file already
    applies to `gh_session`/`sources`/`backlog_check` (see that function's own docstring), for the
    identical reason: a doctor-local reimplementation of "is this a registered unit" could silently
    drift from what the loop itself already treats as one.

    A REPO WITH NO `.sdlc/features/` AT ALL -- MOST ADOPTERS -- IS COMPLETELY UNAFFECTED. `branch`
    not shaped `feature/<name>` returns False before any file is even touched, and
    `resolve_open_unit` itself degrades a missing or corrupt registry to "no units known" (see its
    own docstring), so a `feature/<name>`-shaped branch in an unadopted repo also returns False --
    the exact pre-#2452-follow-up behaviour, unchanged either way.

    NEVER RAISES, matching every early return in `_stray_commits_after_merge` below: a missing
    `sdlc_dir`, a branch not shaped like a unit branch, an illegal unit name, or a load failure of
    `feature_registry` itself all degrade to False -- fail TOWARD the pre-fix behaviour (the
    ordinary stray-commit verdict still runs), never toward a false exemption."""
    if not sdlc_dir or not branch or not branch.startswith(_UNIT_BRANCH_PREFIX):
        return False
    name = branch[len(_UNIT_BRANCH_PREFIX):]
    if not name:
        return False
    try:
        fr = _load_loop_script("feature_registry")
        return fr.resolve_open_unit(sdlc_dir, name) is not None
    except Exception:                            # noqa: BLE001 - fail toward the old verdict, not a false alarm
        return False


def _landing_pr_unverifiable(run, repo, unit):
    """Detail string naming unit `unit`'s own open landing PR (`feature/<unit>` -> base) as
    UNVERIFIABLE, or None (#2593). GitHub cannot build a merge ref for a conflicted PR, so no
    `pull_request` workflow ever runs and `statusCheckRollup` comes back empty -- the PR shows NO
    checks, visually identical to "CI not configured yet". Distinct from "checks pending"
    (non-empty rollup, still running), "checks passed" (non-empty rollup, all green), AND an
    ordinary MERGEABLE branch that simply has no CI wired up at all (also an empty rollup, but not
    this issue's shape -- `mergeable` is what tells the two empty-rollup cases apart).

    ONE `gh pr list` call, matching this file's own single-argument, never-raising `run`
    convention throughout -- NOT `unit_completion._landing_pull_requests` (a different, two-
    argument, RAISING convention this file does not share, see `_gh_runner`'s own docstring for
    why the two must never be handed to each other directly). `--repo repo` is required: every
    real precedent for this exact `gh pr list --head ... --json mergeable,...,statusCheckRollup`
    shape in this codebase passes it explicitly (`work.py`'s `SIBLING_PR_FIELDS` sibling-gate
    call, and `_stray_commits_after_merge` below, this function's own integration precedent) --
    `_real_run` sets no `cwd=`, so a bare `--head` with no `--repo` would depend entirely on
    `gh`'s own remote-inference from whatever directory the doctor.py PROCESS happens to run in,
    unverified by anything in `check()`."""
    branch = "feature/" + unit
    raw = run(["gh", "pr", "list", "--repo", repo, "--head", branch, "--state", "open",
              "--limit", "1", "--json", "number,mergeable,mergeStateStatus,statusCheckRollup"])
    if not raw:
        return None                              # can't tell (or none open) -- no false alarm
    try:
        rows = json.loads(raw)
    except Exception:
        return None
    if not isinstance(rows, list) or not rows:
        return None
    pr = rows[0]
    if not isinstance(pr, dict) or pr.get("mergeable") != "CONFLICTING":
        return None
    if pr.get("statusCheckRollup"):
        return None                              # conflicted but checks DID run -- a rarer, different shape
    return (f"unit {unit!r}'s landing PR #{pr.get('number')} is UNVERIFIABLE, not merely "
            f"unchecked — GitHub could not build a merge ref for it (conflicted with its "
            f"base), so no CI workflow could ever run. It reads on GitHub as \"no checks\", "
            f"visually identical to \"CI not configured\" -- resolve the conflict before "
            f"trusting the absence of failures as health.")


def _landing_pr_unverifiable_units(sdlc_dir, gh_disc, run):
    """One aggregated detail string naming every open unit whose landing PR is UNVERIFIABLE, or
    None (#2593) -- mirrors `_stray_commits_after_merge`'s own single-`_chk`-row shape exactly,
    never one row per unit. `feature_registry` cross-loaded via `_load_loop_script`, this file's
    own established convention, inside the same fail-gracefully wrapper every other cross-load
    call site already uses."""
    repo = gh_disc.get("repo") or ""
    if not repo:
        return None
    try:
        feature_registry = _load_loop_script("feature_registry")
        registry = feature_registry.read(feature_registry.registry_dir(sdlc_dir))
    except Exception:                            # noqa: BLE001 - a load/read failure answers nothing
        return None
    found = []
    for unit, entry in (registry or {}).items():
        if not isinstance(entry, dict) or not entry.get("open"):
            continue
        detail = _landing_pr_unverifiable(run, repo, unit)
        if detail:
            found.append(detail)
    return "; ".join(found) if found else None


def _stray_commits_after_merge(gh_cfg, repo_root, run, sdlc_dir=None):
    """#2452: a merged (or closed) PR's branch that the main checkout never left, still quietly
    collecting commits nobody notices landed nowhere. Live incident: a squash-merged PR (#3604, on
    a downstream deployment of this plugin) had its checkout stay on that branch, and 5 unrelated commits
    landed on it over the next 2 days before a human noticed by accident (recovered via PR #3712) --
    nothing had detected the branch was dead before that. The issue's own "Suggested check" section
    asks only that this be flagged, with a fix hint -- see `gh issue view 2452` -- never that doctor
    switch or cut a branch itself; that is why this function only ever returns a string and never
    runs a mutating `git`/`gh` command.

    Anchored on `headRefOid`, never the merge commit. This repo's own `.sdlc/config.json` sets
    `work.merge_method: "squash"`, and a squash merge creates a brand-new commit on the base with NO
    ancestor/descendant relationship to the feature branch's own commits -- `git rev-list --count
    <mergeCommitSha>..HEAD` would count the branch's own pre-merge history as "stray," a false alarm
    on every squash-merged branch in this repo. `headRefOid` is GitHub's own record of the PR head's
    tip at merge/close time: it is a SHA, so it survives the squash AND survives the remote branch
    being deleted, and `git rev-list --count <headRefOid>..HEAD` is the correct anchor for squash,
    merge-commit, and rebase alike.

    The default branch is read LIVE (`gh api repos/<repo> --jq .default_branch`), never from
    `work.base`: unlike most checks in this file, this one has a real checkout to ask, and
    `work.base` can legitimately diverge from the repo's true default under this repo's own
    feature-branch unit model (goals here base off `feature/<name>`, not off the actual default
    branch) -- but the MAIN checkout should always rest on the true default between runs, so the
    live question is the more correct one. The same call doubles as the reachability canary, so an
    unreachable repo/auth path can never be misread as "clean."

    Every early return below is fail-open, matching this file's own "no false alarm" convention -- a
    can't-tell must never render as a risk:
      - no `discovery.github.repo` configured -- nothing to query;
      - `git branch --show-current` empty -- covers BOTH a genuinely detached HEAD (git exits 0 and
        prints nothing, verified live) and git itself being unreachable (`_RawFailure` is also
        falsy); both collapse to "no branch to look a PR up by," and neither is a false alarm;
      - the default-branch canary empty -- can't reach the repo/auth path at all;
      - current branch IS the default -- the healthy resting state; no PR lookup even attempted;
      - current branch is the registered, OPEN base of a `feature/<name>` unit (`#2452` follow-up
        after PR #2461's own independent review reproduced a false positive live against this
        repo's `feature/dangling-completion` -- see `_open_unit_branch`) -- that branch is a
        long-lived integration branch expected to keep collecting merged goal PRs after its own
        historical completion PR into the default branch, so "stray" does not apply to it at all;
      - no PR found for this branch (exit 1, "no pull requests found for branch ...", live-verified)
        -- not every branch has one;
      - PR `state` is anything other than MERGED/CLOSED (OPEN, live-verified, or malformed) -- still
        in progress, nothing stray is even possible yet;
      - `headRefOid` missing from the PR's own JSON -- defensive: no anchor to compute a count from;
      - `git rev-list --count` itself fails (shallow clone, rewritten history, an unreachable SHA,
        live-verified as exit 128) -- can't compute a count, which is not the same as zero;
      - the resulting `stray_count` is 0 -- `headRefOid == HEAD`, the correct state right after a
        merge/close with nothing yet committed on top.

    Read-only, unconditionally: never runs `git checkout`/`switch`/`branch -D`/`reset`, and never
    writes back to GitHub. It returns a one-line fix string for a human (or a future, separately
    scoped issue) to act on -- detection only, exactly what this issue asked for.

    Independent of `work.enabled`/`work.auto_merge` (unlike `_self_merge_risk` above): a human
    working the main checkout by hand strands a branch exactly as easily as the loop can, so this
    reads no `work` config at all.

    `sdlc_dir` (added by the #2452 follow-up above; default `None`) is the ONLY new input the fix
    needed -- it is never used to read `work` or anything else, only handed straight to
    `_open_unit_branch` so it can ask the feature registry (`.sdlc/features/`, under `sdlc_dir`)
    whether the current branch is itself a live unit base. Omitted (or a repo with no registered
    units at all), the check behaves exactly as it did before the follow-up -- see
    `_open_unit_branch`'s own docstring for why that degrades safely rather than by coincidence."""
    repo = gh_cfg.get("repo") or ""
    if not repo:
        return None
    branch = (run(["git", "-C", str(repo_root), "branch", "--show-current"]) or "").strip()
    if not branch:
        return None
    raw_default = run(["gh", "api", f"repos/{repo}", "--jq", ".default_branch"])
    default_branch = (raw_default or "").strip()
    if not default_branch:
        return None                            # can't-tell must never read as a false alarm
    if branch == default_branch:
        return None                            # the healthy resting state
    if _open_unit_branch(sdlc_dir, branch):
        return None                            # a live unit's own integration branch -- see `_open_unit_branch`
    raw_pr = run(["gh", "pr", "view", branch, "--repo", repo, "--json",
                  "state,mergedAt,closedAt,headRefOid,headRefName,number"])
    if not raw_pr:
        return None                            # no PR exists for this branch
    try:
        data = json.loads(raw_pr)
    except Exception:
        return None
    state = str(data.get("state") or "").upper()
    if state not in ("MERGED", "CLOSED"):
        return None                            # OPEN, or an unexpected shape -- either way, no verdict
    head_ref_oid = data.get("headRefOid") or ""
    if not head_ref_oid:
        return None                            # no anchor to compute a stray count from
    raw_count = run(["git", "-C", str(repo_root), "rev-list", "--count", f"{head_ref_oid}..HEAD"])
    if not raw_count:
        return None                            # headRefOid unreachable locally -- can't compute, not zero
    try:
        stray_count = int(raw_count.strip())
    except Exception:
        return None
    if stray_count <= 0:
        return None                            # headRefOid == HEAD -- nothing committed since
    number = data.get("number")
    at_ts = data.get("mergedAt") or data.get("closedAt") or "an unknown time"
    if state == "MERGED":
        return (f"checked-out branch {branch!r} belongs to PR #{number}, already merged (at {at_ts}), but "
                f"this branch carries {stray_count} commit(s) made after that PR's own head -- that work "
                f"already landed via the PR. Switch to {default_branch!r}, or cut a throwaway branch off "
                f"origin/{default_branch} for anything further here.")
    return (f"checked-out branch {branch!r} belongs to PR #{number}, closed without merging (at {at_ts}), "
            f"and this branch carries {stray_count} commit(s) made after that PR's own head -- none of "
            f"that work has landed anywhere. Switch to {default_branch!r}, or cut a throwaway branch off "
            f"origin/{default_branch} and open a new PR for anything here worth keeping.")


#: GitHub's own name for the built-in workflow that moves a closed item to Done. Matched exactly,
#: because that is what the Projects UI calls it and what the API returns.
_ITEM_CLOSED_WORKFLOW = "Item closed"


def _item_closed_workflow_off(gh_cfg, run):
    """The board's built-in "Item closed" workflow is DISABLED — return a one-line fix, else None.

    That workflow is what sets Status to Done when an item's issue closes. With it off, the only
    thing that ever moves a card to Done is the loop's own `complete()` path, so every close the
    loop did not perform — a human closing an issue, a PR auto-close on an issue the loop never
    claimed, a bulk close — strands its card wherever it happened to be. Measured on this repo's own
    board: 92 cards were stranded across Backlog / Blocked / QC / In Progress while their issues
    were closed.

    Detection only, deliberately: sigma CANNOT fix this. The GraphQL schema exposes exactly one
    workflow mutation, `deleteProjectV2Workflow` — there is no enable/update — and `gh project` has
    no `workflow` subcommand. So the remedy is a human clicking a toggle, and the value of this
    check is naming it at setup instead of leaving it to be discovered as inexplicable drift.

    Read-only and fail-open on everything (no `project` scope, no pinned number, an API blip,
    malformed JSON), matching `_board_dup_risk` / `_unmapped_board_fields` above: a doctor check
    that can crash `/sigma-doctor` is worse than no check at all."""
    proj = _block(gh_cfg, "project")
    number = proj.get("number")
    if not number:
        return None                       # nothing to query against; the dup-risk check covers this case
    repo = gh_cfg.get("repo") or ""
    owner = proj.get("owner") or (repo.split("/")[0] if "/" in repo else "@me")
    # `@me` is not a login GraphQL can resolve — it needs the `viewer` root. A real login may belong
    # to either a User or an Organization, so `repositoryOwner` with both inline fragments covers
    # both without the caller having to know which.
    inner = ("projectV2(number: %d) { workflows(first: 30) { nodes { name enabled } } }" % int(number))
    if str(owner).startswith("@"):
        query = "query { viewer { %s } }" % inner
        roots = ("viewer",)
    else:
        query = ('query { repositoryOwner(login: "%s") { ... on User { %s } ... on Organization { %s } } }'
                 % (owner, inner, inner))
        roots = ("repositoryOwner",)
    try:
        raw = run(["gh", "api", "graphql", "-f", "query=" + query])
        data = (json.loads(raw or "{}") or {}).get("data") or {}
        node = next((data.get(r) for r in roots if data.get(r)), None)
        nodes = (((node or {}).get("projectV2") or {}).get("workflows") or {}).get("nodes")
        if not nodes:
            return None                   # unreadable or no workflows -> no false alarm
        wf = next((w for w in nodes if (w or {}).get("name") == _ITEM_CLOSED_WORKFLOW), None)
        if wf is None or wf.get("enabled"):
            return None
    except Exception:                     # noqa: BLE001 - fail-open, see docstring
        return None
    return (f"the board's built-in \"{_ITEM_CLOSED_WORKFLOW}\" workflow is OFF, so a card only ever "
            "reaches Done when the loop itself closes the issue - anything you close by hand, or "
            "that a merged PR auto-closes, is stranded in whatever column it was in. sigma "
            "cannot switch this on (the API has no enable mutation): open the board's Workflows "
            f"page, pick \"{_ITEM_CLOSED_WORKFLOW}\", set Status: Done, and Enable.")


def _open_issue_done_card(gh_cfg, run):
    """The sibling of `_item_closed_workflow_off` above, covering the OPPOSITE direction: an open
    issue whose board card already reads Status = Done. GitHub's built-in project workflows move a
    card TO Done in exactly one direction — closing an issue fires "Item closed" and moves its
    card — and there is no "Item reopened" workflow that fires the other way. Reopen the issue and
    its card just sits at Done for the rest of its life, until a human resets it by hand.

    That is a silent hazard, not just a cosmetic one: nothing in the kit measures the opposite-of-
    open set, so an accidental close IMPROVES every check/metric that only counts open issues —
    the stranded card disappears from view along with the mistake. (#1206: this same blind spot let
    `_sync_backlog`'s own Priority mirror keep writing the board's Priority field on a card in this
    exact state, unconditionally, forever — fixed separately in sources.py's
    `_write_priority_field`, which now skips exactly this case. This check is the detection half of
    that fix: naming the card for a human, the same job `_item_closed_workflow_off` already does for
    the mirror-image direction.)

    Two `gh` calls: the open goal-labelled issues (cheap, no `project` scope needed — degrades to
    "can't tell" the same as every other check here if that alone fails), then the board's own
    items (needs `project` scope) to read each one's current Status. Returns a one-line fix naming
    the stranded issue(s), or None when none are stranded OR the board can't be read at all (no
    project number, no repo, no `project` scope, an API error, malformed JSON on either call) —
    fail-open, matching every other board check in this file: a can't-tell must never read as a
    finding, and (unlike `_item_closed_workflow_off`, which can't fix its own finding at all) this
    one's remedy is cheap enough that a false alarm would be actively misleading, not just noise."""
    proj = _block(gh_cfg, "project")
    number = proj.get("number")
    repo = gh_cfg.get("repo") or ""
    if not number or not repo:
        return None                       # nothing to query against; matches the sibling's own gate
    owner = proj.get("owner") or (repo.split("/")[0] if "/" in repo else "@me")
    cols = proj.get("columns")
    done_name = (cols.get("done") if isinstance(cols, dict) else None) or "Done"
    goal_label = gh_cfg.get("goal_label", "sdlc:goal")
    issues = _list(run, gh_cfg, ["number"], [goal_label], repo=repo)
    if not issues:
        return None
    open_numbers = {i.get("number") for i in issues if isinstance(i, dict) and i.get("number") is not None}
    if not open_numbers:
        return None
    raw_items = run(["gh", "project", "item-list", str(number), "--owner", owner,
                     "--format", "json", "--limit", "200"])
    if not raw_items:
        return None
    try:
        data = json.loads(raw_items)
    except Exception:
        return None
    items = (data.get("items") if isinstance(data, dict) else data) or []
    status_by_number = {}
    for it in items:
        n = (it.get("content") or {}).get("number")
        if n is not None:
            status_by_number[n] = it.get("status")
    stranded = sorted(n for n in open_numbers if status_by_number.get(n) == done_name)
    if not stranded:
        return None
    names = ", ".join("#%d" % n for n in stranded[:10])
    if len(stranded) > 10:
        names += f" (+{len(stranded) - 10} more)"
    return (f"open issue(s) with a board card stuck at {done_name!r}: {names} — GitHub has no "
            "\"Item reopened\" workflow (only \"Item closed\" exists), so reopening an issue never "
            f"moves its card back; it stays at {done_name!r} until a human resets it, and every "
            "board-derived metric silently under-counts it. Reset each card's Status on the board.")


_UNREAD = object()   # "no field list passed": the function reads it itself


def _board_field_list(gh_cfg, run):
    """The pinned board's fields from ONE read-only `gh project field-list`, or None when it cannot
    be read (nothing pinned, no scope, an API error, bad JSON). #280: shared by
    `_unmapped_board_fields` and `_board_columns_unmatched`, so `check` pays one call for both."""
    proj = _block(gh_cfg, "project")
    repo = gh_cfg.get("repo") or ""
    owner = proj.get("owner") or (repo.split("/")[0] if "/" in repo else "")
    number = proj.get("number")
    if not owner or not number:
        return None
    try:
        raw = run(["gh", "project", "field-list", str(number), "--owner", owner, "--format", "json",
                   "--limit", "100"])
        if not raw:
            return None
        data = json.loads(raw)
    except Exception:
        return None
    return [f for f in ((data.get("fields") if isinstance(data, dict) else data) or [])
            if isinstance(f, dict)]


def _unmapped_board_fields(gh_cfg, run, fields=_UNREAD):
    """Single-select board fields — beyond the Status field sigma drives, and beyond what
    project.custom_fields already maps — that an issue the loop CREATES (a hand-off) would be left
    blank on while every human-made issue carries them. Returns the unmapped names, [] when every
    field is covered, or None when the board can't be read (no number yet, no `project` scope, an API
    error) — so a can't-tell never reports a false all-clear. The one silent-data-loss trap doctor
    can catch before it fires."""
    proj = _block(gh_cfg, "project")
    if fields is _UNREAD:
        fields = _board_field_list(gh_cfg, run)
    if fields is None:
        return None
    status_field = proj.get("status_field") or "Status"
    cf = proj.get("custom_fields")                    # a malformed (non-dict) value must not crash the run
    mapped = set(cf.keys()) if isinstance(cf, dict) else set()
    return [f.get("name") for f in fields
            if f.get("options")                       # single-select fields are the ones that carry options
            and f.get("name") != status_field
            and f.get("name") not in mapped]


def _board_columns_unmatched(gh_cfg, run, fields=_UNREAD):
    """#280: the loop's columns (`project.columns`, defaults `sources.BOARD_COLUMNS`) that match NO
    option of the pinned board's Status field -- a card move to one of them writes nothing (the
    loop warns once per run). Matching is the loop's own resolver, `GitHubSource._match_option`
    (exact, else the ONE option equal ignoring case and whitespace; duplicate lane names count
    once, as in the loop's option dict), so doctor and the loop cannot disagree. Returns
    ["<key> '<name>'", ...] (an ambiguous column carries its variants), [] when every column
    matches, or None when the board cannot be read -- a can't-tell is never a false all-clear.

    Not counted: `ready` (a board without an exact `Ready` is the designed label queue,
    `_ready_lane`), and `parked` while `blocked` matches (the loop parks into Blocked then).
    READ-ONLY: `fields` is the caller's one `_board_field_list` read, shared with
    `_unmapped_board_fields`; the caller keeps it out of `cheap_only`."""
    proj = _block(gh_cfg, "project")
    if fields is _UNREAD:
        fields = _board_field_list(gh_cfg, run)
    if fields is None:
        return None
    try:
        src = _load_loop_script("sources")
    except Exception:
        return None
    status_field = proj.get("status_field") or "Status"
    fld = next((f for f in fields if f.get("name") == status_field), None)
    names = [o.get("name") for o in ((fld or {}).get("options") or []) if isinstance(o, dict)]
    cfg_cols = proj.get("columns") if isinstance(proj.get("columns"), dict) else {}
    missing = {}
    for k, d in src.BOARD_COLUMNS:
        n = cfg_cols.get(k, d)
        hit, variants = src.GitHubSource._match_option(names, n)
        if hit is None:
            missing[k] = f"{k} {n!r}" + (" (ambiguous: %s)" % ", ".join(repr(v) for v in variants)
                                         if variants else "")
    missing.pop("ready", None)
    if "blocked" not in missing:
        missing.pop("parked", None)
    return list(missing.values())


_DEFAULT_DOCTOR_MAX_ISSUES = 10            # backlog_check.doctor_scan.max_issues (R6, see docstring below)
_DEFAULT_DOCTOR_MAX_COMMENTS = 20          # backlog_check.doctor_scan.max_comments (= sources.DEFAULT_COMMENT_LIMIT)


def _int_cfg(block, key, default):
    """doctor.py has no shared numeric-coercion helper today (backlog_check.py's own `_num` is
    private to that module, and doctor.py's existing convention -- see the dup/park threshold check
    further down in check() -- is a local try/except per call site, not a shared utility). A garbage
    value (a hand-edited `max_issues: "all"`) falls back to the default rather than crashing the
    whole doctor run, matching that existing convention exactly."""
    try:
        return int(block.get(key, default))
    except (TypeError, ValueError):
        return default


def _load_loop_script(name):
    """Cross-load a script from the sibling sigma-loop skill (skills/sigma-doctor/scripts/doctor.py ->
    skills/sigma-loop/scripts/<name>.py, mirroring backlog_check.py's own `_load_velocity()` cross-skill
    idiom -- two `.parent`s up from this file's own scripts/ dir, then back down into sigma-loop/scripts).

    A DELIBERATE, narrow exception to this file's usual no-cross-skill-import convention (see
    `_enforce_enabled`'s docstring above for that convention stated in full): `_dependency_marker_scan`
    below reuses `sources.fetch_comments` (the shared, already-tested comment-read primitive) and
    `backlog_check._BLOCK_RE` (the exact pattern `_explicit_blockers()` itself gates auto-skip on)
    VERBATIM, rather than a doctor-local reimplementation of either. A doctor-local copy of `_BLOCK_RE`
    would itself be the hardened-sibling-divergence bug class this plugin already tracks (scrub.py's
    docstring flags an existing, accepted instance) -- flagging something `precheck()` would never
    have honored, or missing something it would, as the two patterns drifted apart over time.

    Never raises -- the caller (`_dependency_marker_scan`) treats a load failure exactly like any
    other hard failure (no gh, network down): "could not run this check", reported as silent/ok
    rather than a false alarm."""
    path = _HERE.parent.parent / "sigma-loop" / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _graphql_capability_rows(base, cfg, env=None):
    """#801 slice 1: ONE advisory row when GitHub GraphQL looks unavailable (Claude Code cloud proxy).

    DETECTION AND REPORTING ONLY. `ok=True` and hand-built (not `_chk`, which drops `fix` text when
    ok): in a cloud session this is a permanent condition the user cannot "fix", and `main()` counts
    `not ok` as MISSING. Appears only on a positive signal (env/override), so ordinary runs are
    unchanged. `probe=False` always: doctor is a report, never a network caller here. Fail-open: a
    load or runtime failure yields no row rather than a crash. Reads `os.environ` unless `env` given."""
    try:
        res = _load_loop_script("gh_api").graphql_available(
            env=os.environ if env is None else env, probe=False)
    except Exception:                        # noqa: BLE001 - a check that cannot run says nothing
        return []
    if res.get("available"):
        return []
    return [{"name": "GitHub GraphQL unavailable (cloud proxy)", "ok": True,
             "fix": ("source=%s (%s). Features turned off or degraded while GraphQL is blocked: "
                     "board/Projects mirroring, gh pr merge --auto, timelineItems (blocker/dependency "
                     "edges), gh issue|pr via GraphQL until migrated (#801 slices 2-4). Unit landing "
                     "uses REST only: draft readiness (gh pr ready) and merge-queue landing are "
                     "unavailable here. This is detection only; REST migration is in progress, not complete. Override with "
                     "SIGMA_GH_GRAPHQL=on|off." % (res.get("source"), res.get("reason")))}]


def _awaiting_merge_row(sdlc_dir, now=None):
    """#255 LIVENESS: one row for every goal awaiting a merge. A goal that waits reports no error,
    ever -- an armed auto-merge whose required check failed never lands, a PR that can no longer be
    read stays `unknown`, and a machine where nothing runs `next` or the watcher never reads it --
    so AGE is what this row reads: not OK after the configured wait or successful-read age.
    A missing successful read can mean failed authentication as well as a stopped pass.
    The numbers come from `work.awaiting_merge_report`, the one source of
    both verdicts; this only words them. Never raises."""
    try:
        work = _load_loop_script("work")
        rows = work.awaiting_merge_report(sdlc_dir, now=now)
        line = work.awaiting_merge_line(sdlc_dir, now=now)
    except Exception as exc:                 # noqa: BLE001 - a detector that cannot run says so
        return _chk(f"goals awaiting merge: could not check ({type(exc).__name__})", True, "")
    if not rows:
        return _chk("goals awaiting merge: none", True, "")
    stuck = [r for r in rows if r["stuck"]]
    unwatched = [r for r in rows if r["unwatched"]]
    if not stuck and not unwatched:
        return _chk(f"goals {line}", True, "")
    parts = []
    if stuck:
        parts.append(f"{len(stuck)} waiting over {work._age(work.merge_liveness_policy(sdlc_dir)[0])}")
    if unwatched:
        oldest = max(unwatched, key=lambda r: r["unread"] if r["unread"] is not None else r["waited"])
        gap = oldest["unread"] if oldest["unread"] is not None else oldest["waited"]
        parts.append(f"no PR read for {work._age(gap)} (successful reads only)")
    prs = ", ".join(f"PR #{r['pr']}" for r in (stuck or unwatched)[:5])
    fix = (f"merge or close {prs} (an armed auto-merge whose required check failed never lands; a "
           "closed PR parks the goal), then run `python3 skills/sigma-loop/scripts/loop.py "
           "reconcile-merges .sdlc`")
    if unwatched:
        fix += ("; no successful PR read recently -- check gh authentication/connectivity and "
                "whether the watcher or `loop.py next` is running, then run that command")
    return _chk(f"goals {line} -- {'; '.join(parts)}", False, fix)


def _load_log_script(name):
    """`_load_loop_script`'s sibling, pointed at the `sigma-log` skill instead of `sigma-loop`
    (skills/sigma-doctor/scripts/doctor.py -> skills/sigma-log/scripts/<name>.py) — issue #1779 is
    the first doctor.py check to need a `sigma-log` primitive (`log.py`'s `_all_goal_stems`/
    `read_goal`, the local action-log read side), so no such loader existed yet. A dedicated
    function rather than a `skill=` parameter on `_load_loop_script` itself: every existing call
    of that one keeps its two-argument call shape byte-for-byte, and a reader of either loader
    sees its one fixed target directory in the function body, not threaded through a caller.

    Never raises here either — same convention as `_load_loop_script`: the caller treats a load
    failure exactly like any other hard failure (missing file, broken script), reported as
    "could not answer" rather than a false alarm."""
    path = _HERE.parent.parent / "sigma-log" / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _load_init_script(name):
    """Cross-load a script from the sibling sigma-init skill (#229's preflight), the same by-path
    idiom as `_load_loop_script`: /sigma-init and this doctor must run the SAME checks, so a copy here
    could drift from the one init prints. preflight.py itself cross-loads gh_session for #78's
    proxy-block diagnosis, which is how that remediation still reaches the "gh auth" row."""
    path = _HERE.parent.parent / "sigma-init" / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"doctor_{name}", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _load_setup_script():
    """Cross-load the sibling sigma-setup skill's `setup.py` (#614), the same by-path idiom as
    `_load_init_script`: setup.py owns hook-path policy, so the doctor row and /sigma-init's repair
    read ONE definition of a stale `core.hooksPath` and cannot drift apart."""
    path = _HERE.parent.parent / "sigma-setup" / "scripts" / "setup.py"
    spec = importlib.util.spec_from_file_location("doctor_setup", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


#: The #614 row's name, one spelling shared with its tests.
_STALE_HOOKS_ROW = "git hooks are not switched off by a stale core.hooksPath"


def _stale_hook_path_row(repo_root):
    """#614 AC-3: releases before #614 wrote a `core.hooksPath` naming a directory nothing creates,
    so git runs NO hooks in that repository -- a secret scanner or linter silently off. A row only
    when the key is stale (a healthy check list is unchanged); a detector that cannot run says so
    as an ok row, never a false alarm. Local git reads only, so it runs under `cheap_only` too."""
    try:
        setup = _load_setup_script()
        stale = setup.stale_hook_path(repo_root)
    except Exception as exc:                 # noqa: BLE001 - a detector that cannot run says so
        return {"name": f"stale core.hooksPath: could not check ({type(exc).__name__})", "ok": True,
                "fix": "git config --local --get core.hooksPath"}
    if not stale:
        return None
    return _chk(_STALE_HOOKS_ROW, False,
                f"core.hooksPath is `{setup.HOOKS_PATH}`, a directory that does not exist in this "
                f"repository (an earlier /sigma-init set it), so git runs none of this repository's "
                f"hooks. Fix: `{setup.UNSET_HOOKS_PATH}` (or re-run /sigma-init, which removes it).")


def _preflight_fix(check):
    """A failing preflight check as doctor's one-line fix: the command(s) THAT check prints (so a
    missing `gh` says install it, never `gh auth login`), then what Sigma does meanwhile."""
    cmds = " then ".join(check.get("commands") or [])
    head = f"run: {cmds}" if cmds else check.get("detail", "")
    extra = check.get("detail", "") if cmds else ""
    tail = " -- ".join(x for x in (extra, check.get("note") if check.get("note") not in
                                   ("skipped", "no-remote", "no-commit", "non-github") else "",
                                   ("meanwhile: " + check["meanwhile"]) if check.get("meanwhile")
                                   else "") if x)
    return head + (f" ({tail})" if tail else "")


def _preflight_rows(base, cfg, run, which, injected, cheap_only):
    """#229: the init preflight's checks as doctor rows -- git repository, git remote, base branch,
    gh installed, gh auth, gh token scopes, and (board on) gh project scope. Skipped checks (their
    prerequisite failed, or not run here) are not rows -- never a pass. `cheap_only` (the
    unconditional SessionStart wizard) runs `gh auth status` only where github discovery already
    opted into gh calls, and NEVER `git ls-remote` or the owner lookup (`gh api users/<owner>`):
    an ssh remote to a dead host stalled that hook 75s (review of PR #249); those two are
    /sigma-doctor's. Every network call is bounded by preflight's `network_timeout()` (<= 15s)."""
    pf = _load_init_script("preflight")
    if injected:
        def runner(argv, cwd=None, timeout=None):
            res = run(list(argv))
            return (0, str(res)) if res else (1, _failure_text(res))
    else:
        runner = None
    req = pf.requirements(cfg)
    network = (not cheap_only) or req["github"]
    checks = pf.preflight(str(base.parent), cfg, runner=runner, which=which, network=network,
                          deep=not cheap_only)
    rows, by_id = [], {c["id"]: c for c in checks}
    names = {"gh-installed": "gh installed", "gh-auth": "gh auth", "scopes": "gh token scopes"}
    for c in checks:
        if c.get("note") == "skipped":
            continue
        name = names.get(c["id"], c["name"])
        if c["id"] == "scopes" and c["ok"] is False and c.get("missing") == ["project"]:
            continue                      # the "gh project scope" row below names it
        rows.append(_chk(name if c["ok"] is not None else f"{name} (cannot verify)",
                         c["ok"] is True, _preflight_fix(c)))
    if req["board"]:
        sc = by_id.get("scopes") or {}
        have = sc.get("have")
        name = "gh project scope"
        if sc.get("note") != "skipped" and have is not None:
            ok = "project" in have
            fix = f"run: gh auth refresh -s project -h {sc.get('host') or 'github.com'}"
        else:
            blocker = next((c for c in checks if c["id"] in ("gh-installed", "gh-auth")
                            and c["ok"] is not True), None)
            ok = False
            fix = (("first: " + _preflight_fix(blocker)) if blocker else
                   ("cannot verify (the token reports no scopes); make sure it grants Projects: "
                    "write -- or run: gh auth refresh -s project -h "
                    f"{sc.get('host') or 'github.com'}"))
            # review block #2: a CANNOT VERIFY upstream (an unresolvable ssh alias, a token that
            # reports no scopes) is a cannot-verify row here too -- never the wizard-keyed name,
            # so it never becomes a first-run step demanding a command nobody can complete
            if (blocker is None and have is None) or (blocker is not None and blocker["ok"] is None):
                name += " (cannot verify)"
        rows.append(_chk(name, ok, fix))
    return rows


def _gh_runner(doctor_run):
    """Adapts doctor.py's own `run(full_argv_incl_binary)` convention (`_real_run`:
    `subprocess.run(args, ...)`; every call site in this file passes the binary, e.g.
    `_board_dup_risk`'s `run(["gh", "project", "list", ...])`) to `sources.fetch_comments`'s
    `run(args_excluding_binary)` convention (matching `sources._run_gh`/`ledger._run_gh`'s own shared
    convention, which prepends `"gh"` internally). The two are NOT interchangeable -- handing one
    directly to the other would double-prefix or mis-invoke `gh` -- confirmed by reading both
    conventions side by side, not assumed."""
    return lambda args: doctor_run(["gh", *args])


def _raising_gh(doctor_run):
    """#895 slice 2c: `_gh_runner`, but a FAILED call RAISES (a `RuntimeError` carrying gh's text as
    `.hint`) instead of returning the falsy `_RawFailure`. The REST list fetch does
    `json.loads(raw or "[]")`, so a failure handed to it unraised would read as an EMPTY list -- a
    success that is never classified, never falls back and never trips anything (the
    failure-as-empty trap). Built ON TOP of `_gh_runner` so there is no second `["gh", *args]`
    literal. A plain-string fake that returns "" for a failure is NOT a `_RawFailure`; it passes
    through as "" and `gh_api.list_issues_gh` refuses empty output (kind other, no fallback)."""
    runner = _gh_runner(doctor_run)

    def run(args):
        res = runner(args)
        if isinstance(res, _RawFailure):
            exc = RuntimeError("gh " + (args[0] if args else "") + " failed: " + (_failure_text(res)[:200] or "no output"))
            exc.hint = _failure_text(res)
            raise exc
        return res
    return run


_LIST_FETCH = []


def _list(run, gh_cfg, fields, labels, state="open", cap=200, sort="created", repo=None):
    """One doctor list read through `gh_api.list_issues_gh` (REST first, ONE `gh issue list` fallback on a
    rate limit / 5xx / transport failure, never in a cloud session). Returns the list, or `None` on ANY
    exception, so each site keeps its old falsy -> skip arm exactly. READ-ONLY by construction: no
    `sdlc_dir` is passed, so doctor never writes the REST breaker or the fallback log. Both legs use the
    SAME raising wrapper. Newest created first (what `gh issue list` returned) unless `sort="updated"`.
    Latency, derived from the 15 s per-call timeout and NOT measured: up to ~45 s per site on an outage
    (2 REST pages + 1 fallback); the multi-state scan ~135 s."""
    try:
        gh_api = _load_loop_script("gh_api")
        if not _LIST_FETCH:
            _LIST_FETCH.append(_load_loop_script("sources").fetch_issues_rest)
        raising = _raising_gh(run)
        return gh_api.list_issues_gh(raising, fields, repo=repo or gh_cfg.get("repo") or None,
                                     labels=labels, state=state, cap=cap, sort=sort,
                                     gql_run=raising, fetch=_LIST_FETCH[0])
    except Exception:
        return None


def _dependency_marker_scan(gh_cfg, bchk_cfg, run):
    """An issue with a comment matching `backlog_check._BLOCK_RE` ("blocked by #N" / "depends on #N"
    / ...) but NO matching marker in its own body is likely a human-authored dependency, left via the
    GitHub UI (bypassing `handoff.hand_off()` entirely), that `precheck()`'s auto-skip silently never
    sees -- mirror.py's own corpus fetch is title+body only, by design (comments are never fetched
    corpus-wide, for cost + secret-surface reasons). This is the doctor-side nudge for that blind
    spot: advisory only, nothing auto-parks from it.

    Returns (flagged: [issue numbers], scanned: int, total: int), or None on any hard failure (no
    gh, network down, sibling scripts unavailable) -- None means "could not run this check", reported
    as ok=True/silent rather than a false alarm, same fail-open convention as `_board_dup_risk` above.

    Cost design (R6, deliberate, not left to chance): candidates are issues WITHOUT a body marker --
    nearly every open goal issue in a real backlog, so `max_issues` bounds the TYPICAL per-run cost,
    not a rare worst case. Measured against a real repo, `gh issue view --json comments` averages
    ~0.62s/call; the ORIGINAL draft default of 30 would add ~18.5s to a routine `/sigma-doctor` run (a
    4-7x regression in what's supposed to be a fast setup check, since it's hit on nearly every real
    backlog). `_DEFAULT_DOCTOR_MAX_ISSUES = 10` keeps the typical added cost to ~6s, while
    `backlog_check.doctor_scan.max_issues` stays configurable for a repo that wants a wider scan and
    is willing to pay for it. The bound is embedded in the check's own `name` (`_chk()` always prints
    `name`, pass or fail) so it is visible on every run, never silently applied.

    The LIST call itself is capped at 200 (not `max_issues`) -- matching `mirror.py`'s own
    `_OPEN_LIMIT = 200` ceiling for the identical query shape: `total` is reported against THIS
    number, so capping the list call at the same small number as the expensive per-issue comment-fetch
    loop would make `total` itself silently truncated, exactly what "no silent truncation" is about.
    A backlog bigger than 200 open goal issues still under-reports `total` -- documented, not solved
    (matching mirror.py's own accepted ceiling), rather than silently assumed complete.

    Deliberately simpler than `_explicit_blockers()`'s own body-scan: this does NOT cross-check that a
    comment's `#N` reference is to a currently-open issue -- it flags on any `_BLOCK_RE` match in a
    comment, full stop. Doctor is advisory (nothing auto-parks from this), a false positive costs a
    human one glance to dismiss, and the extra precision would mean re-deriving the doctor's own
    "which issues are open" set from the same `gh issue list` call already being made (cheap, and
    worth doing if this proves noisy in practice -- not required for #389's acceptance criteria)."""
    try:
        sources = _load_loop_script("sources")
        block_re = _load_loop_script("backlog_check")._BLOCK_RE
    except Exception:
        return None
    scan_cfg = _block(bchk_cfg, "doctor_scan")
    max_issues = _int_cfg(scan_cfg, "max_issues", _DEFAULT_DOCTOR_MAX_ISSUES)
    max_comments = _int_cfg(scan_cfg, "max_comments", _DEFAULT_DOCTOR_MAX_COMMENTS)
    goal_label = gh_cfg.get("goal_label", "sdlc:goal")
    # 200, not max_issues, for the LIST call -- see the docstring above for why. `updated`/desc is the
    # REST-native form of the old `--search sort:updated-desc`; `candidates[:max_issues]` depends on it.
    issues = _list(run, gh_cfg, ["number", "body"], [goal_label], sort="updated")
    if issues is None:
        return None
    total = len(issues)
    candidates = [i for i in issues if not block_re.search(i.get("body") or "")][:max_issues]
    flagged = []
    for i in candidates:
        n = i.get("number")
        comments = sources.fetch_comments({"discovery": {"github": gh_cfg}}, n,
                                          run=_gh_runner(run), limit=max_comments)
        if any(block_re.search(c["body"]) for c in comments):
            flagged.append(n)
    return flagged, len(candidates), total


#: A label name that READS as a "blocked" convention (case-insensitive substring): covers
#: "blocked", "status:blocked", "blocked-by-legal", etc. Deliberately narrow -- there is no safe
#: generic way to recognize an arbitrary repo-local "do not auto-pick" phrasing (e.g. "spend-hold",
#: "legal-review") without false-positiving on unrelated labels, so this only catches the specific
#: convention #1205's own real-world case used, and the one the acceptance criteria names.
_BLOCKED_HINT = re.compile(r"block", re.IGNORECASE)


def _blocked_label_scan(gh_cfg, run):
    """#1205: `discovery.github.blocked_label` is what makes the label-queue path
    (`sources.py`'s `_fetch_pending`) actually honor a repo-local "do not auto-pick" label --
    unset (the default), nothing beyond `parked_label` ever excludes an issue from that picker (see
    `GitHubSource.__init__`'s own comment on `blocked_label` for the full mechanism). A repo that
    already hand-labels issues "blocked" (or a variant) without ever setting this key has a label
    that LOOKS like it gates the picker but is silently inert. This is the setup-time nudge for
    that gap: scan open `goal_label` issues for a label name matching `_BLOCKED_HINT`, naming the
    ones found.

    An issue already carrying `parked_label` is skipped -- it is excluded from the picker
    regardless of `blocked_label`, so flagging it would be a false alarm, not a real exposure.

    Returns a list of `(issue_number, matched_label)` pairs (possibly empty -- a clean read with
    nothing to flag), or `None` on any hard failure (no gh, network down, malformed/non-list JSON)
    -- `None` degrades to silent/ok, the same fail-open convention `_dependency_marker_scan` above
    uses, never a false alarm from a backlog this tool could not actually read."""
    goal_label = gh_cfg.get("goal_label", "sdlc:goal")
    parked_label = gh_cfg.get("parked_label", "sdlc:parked")
    issues = _list(run, gh_cfg, ["number", "labels"], [goal_label])
    if issues is None:
        return None
    flagged = []
    for i in issues:
        if not isinstance(i, dict):
            continue
        names = [(l.get("name") or "") if isinstance(l, dict) else str(l or "")
                 for l in (i.get("labels") or [])]
        if parked_label in names:
            continue           # already excluded from the picker either way -- not a real gap
        # #1393: skip the kit's OWN machine-managed labels. `_BLOCKED_HINT` is /block/i, which
        # matches `sdlc:blocked` and `sdlc:blocking` -- and since a blocked goal now KEEPS
        # `sdlc:goal`, this scan's `--label sdlc:goal` query returns every machine-blocked goal on
        # the board. Left unfiltered it would tell an adopter to configure `blocked_label` for a
        # label the loop already manages and already excludes, i.e. a check that fires on correct
        # state. The scan exists to spot a repo's OWN blocked-ish convention, not ours.
        ours = {gh_cfg.get("goal_blocked_label", "sdlc:blocked"),
                gh_cfg.get("blocking_label", "sdlc:blocking"),
                # #1468: another label the loop manages and already excludes from the queue. Left
                # out, `_BLOCKED_HINT` (/block/i) would not match it -- but the same reasoning
                # applies the moment an adopter renames it to something block-ish, and the set is
                # meant to enumerate what is OURS, not what happens to match today's regex.
                gh_cfg.get("needs_label_label", "sdlc:needs-label"),
                # #2263: same reasoning as `needs_label_label` immediately above, for its sibling.
                gh_cfg.get("needs_unit_label", "sdlc:needs-unit")}
        hit = next((n for n in names if n not in ours and _BLOCKED_HINT.search(n)), None)
        if hit:
            flagged.append((i.get("number"), hit))
    return flagged


def _blocked_label_collision(gh_cfg):
    """#1391 step 7: True iff the HUMAN hold label and the MACHINE-managed blocked state resolve to
    the SAME string. Live on the `os` adopter today (`blocked_label` == `goal_blocked_label` ==
    "sdlc:blocked"), where the team hand-set it on real issues as a deliberate "do not touch this".

    These are two different mechanisms that happen to share a default-ish name:
      - `discovery.github.blocked_label` (#1205) is a repo-local convention a HUMAN sets and the
        loop only ever READS. Nothing in the loop clears it.
      - `discovery.github.goal_blocked_label` (#1350) is machine-managed state the loop writes and
        an auto-resume sweep is expected to CLEAR once the named blocker closes.

    Collided, "clear the machine state" and "honour the human's hold" become the same write, so any
    reconciler that resolves the blocked state will silently override deliberate human intent.

    DOCTOR ONLY REPORTS. It cannot refuse anything -- every row here is advisory by construction
    (`_chk` has no failing mode that halts a caller), and pretending otherwise is how the plan came
    to claim doctor "protects" os when it cannot. The REFUSAL belongs at the site that would act on
    the collision: the reconciler must decline to run on a repo where this is true. This row exists
    so the operator learns about it before that ever matters."""
    if not isinstance(gh_cfg, dict):
        return None
    human = gh_cfg.get("blocked_label") or None
    machine = gh_cfg.get("goal_blocked_label", "sdlc:blocked")
    return human if (human and human == machine) else None


def _unreachable_blocker_scan(gh_cfg, run):
    """#1392: open issues carrying `blocking_label` but NOT `goal_label` -- blockers no queue can
    ever serve.

    `sdlc:blocking` (#1351) is DERIVED: `auto_unpark` puts it on any issue a live "Blocked by #N"
    marker names, whatever else is true of that issue. But nothing schedules on it alone.
    `_blocking_priority_pending` (#1352, and off by default besides) reuses `_fetch_pending`, whose
    base query ALWAYS carries `--label <goal_label>` -- and `gh issue list --label` ANDs repeated
    flags -- so `sdlc:blocking` is a TIE-BREAK among already-eligible issues, never a queue. The
    board path agrees: `_card_is_eligible` requires the goal label too.

    So a blocker without `goal_label` is unreachable, while `auto_unpark` refuses to resume whatever
    it blocks until that blocker CLOSES. Nothing breaks that cycle on its own -- and the commonest
    way in is completely ordinary: a hand-off filed `immediately_actionable=False` carries
    `sdlc:needs-confirmation` and deliberately no goal label (`handoff.py`), and something later
    declares itself blocked by it.

    Report-only, and deliberately so: auto-adding `goal_label` would defeat the very approval gate
    that put the issue in this state. `/sigma-promote`'s `deadlocked` bucket is the route out.

    Fail-open, matching every other scan in this file: an unreadable/malformed response yields `[]`
    (silent), never a false alarm."""
    blocking = gh_cfg.get("blocking_label", "sdlc:blocking")
    goal = gh_cfg.get("goal_label", "sdlc:goal")
    issues = _list(run, gh_cfg, ["number", "labels"], [blocking])
    if issues is None:
        return []
    flagged = []
    for i in issues:
        if not isinstance(i, dict):
            continue
        names = [(l.get("name") or "") if isinstance(l, dict) else str(l or "")
                 for l in (i.get("labels") or [])]
        if goal not in names:
            flagged.append(i.get("number"))
    return flagged


def _orphan_in_progress_scan(gh_cfg, config, run):
    """#1391 step 7: open issues carrying `in_progress_label` but NO primary lifecycle label at all.

    This class is INVISIBLE to `_multi_state_label_scan` by construction -- that check flags issues
    carrying MORE THAN ONE primary label, and these carry ZERO. It is invisible to the picker too:
    `_fetch_pending` queries `--label <goal_label>`, which cannot return an issue that lacks it. So
    nothing in the system can currently find one, which is precisely why they accumulate unnoticed
    (measured live: 2 on the `os` adopter, plus 4 more carrying in-progress with no goal label).

    Fail-open, matching every other scan in this file: an unreadable/malformed response yields `[]`
    (silent), never a false alarm."""
    in_progress = gh_cfg.get("in_progress_label", "sdlc:in-progress")
    # #1393: `goal_blocked_label` is NOT a membership label -- it is an OVERLAY, exactly like
    # `in_progress_label`. This scan is the one place the membership refactor missed, and the effect
    # was to hide the orphan class it exists to find: an issue carrying {in-progress, blocked} and
    # NO membership label passed the "does it have any primary?" test on the strength of its own
    # second overlay, so the very shape a half-applied park leaves behind was reported as fine.
    primary = (gh_cfg.get("goal_label", "sdlc:goal"),
               gh_cfg.get("parked_label", "sdlc:parked"),
               (_block(_block(config, "ledger"), "handoff").get("proposed_label")
                or "sdlc:needs-confirmation"))
    issues = _list(run, gh_cfg, ["number", "labels"], [in_progress])
    if issues is None:
        return []
    flagged = []
    for i in issues:
        if not isinstance(i, dict):
            continue
        names = [(l.get("name") or "") if isinstance(l, dict) else str(l or "")
                 for l in (i.get("labels") or [])]
        if not any(p in names for p in primary):
            flagged.append(i.get("number"))
    return flagged


def _multi_state_label_scan(gh_cfg, config, run):
    """#1354: the single-primary-state-label invariant (design doc §5, corrected per §10 finding
    2). The invariant is NOT "no issue has more than one sdlc:* label ever" -- `sdlc:in-progress`
    is an orthogonal, additive marker that legitimately coexists with `sdlc:goal` (the normal
    active-work signature, confirmed against a real, live issue on this repo during #1349's own
    research) and, as an accepted residual risk, occasionally with `sdlc:parked` too. Excluded from
    the scanned set entirely, on purpose -- flagging it would be constant, correct-behavior noise,
    not a real defect. The real invariant: no issue should carry more than one of the PRIMARY
    lifecycle labels -- `goal_label`, `parked_label`, the proposed/needs-confirmation label,
    `goal_blocked_label`.

    Four independent `gh issue list --label <X> --state open` queries (one per primary label,
    deduped by issue number) -- an OR across four distinct labels can't be expressed as one AND'd
    `--label` query, the identical reason `auto_unpark.py`'s `_fetch_parked_issues` already needs
    two separate calls for its own two-label case. FAIL-OPEN per label: any one query failing
    (transport, bad JSON, non-list payload) drops only that label's contribution, never the whole
    scan -- a partial read must not manufacture false violations from data it never actually saw,
    and it must never crash this optional, informational check.

    The proposed/needs-confirmation label is read the SAME way `handoff.proposed_label(config)`
    resolves it (`ledger.handoff.proposed_label`, default `sdlc:needs-confirmation`) -- duplicated
    here rather than cross-imported, matching this file's own standalone-diagnostic convention (see
    `_enforce_enabled`'s docstring): doctor.py has to keep working even if something else in the
    loop's own module graph is broken.

    Returns a list of `(issue_number, [matched_primary_labels])` pairs for every issue carrying
    MORE THAN ONE of the four -- possibly empty (a clean read with nothing to flag), and empty (not
    `None`) even when every query failed, matching `_blocked_label_scan`'s own caller-side
    convention (`if <result>:` treats both identically, so the distinction carries no signal here
    either)."""
    goal_label = gh_cfg.get("goal_label", "sdlc:goal")
    parked_label = gh_cfg.get("parked_label", "sdlc:parked")
    proposed_label = (_block(_block(config, "ledger"), "handoff").get("proposed_label")
                      or "sdlc:needs-confirmation")
    # #1393: `goal_blocked_label` is deliberately NOT in `primary` any more. `mark_blocked` now
    # KEEPS `goal_label`, so `sdlc:goal`+`sdlc:blocked` is the correct, normal shape of a blocked
    # goal -- flagging it would report every properly-blocked goal on the board as corruption,
    # which is both wrong and the exact kind of false alarm that teaches people to ignore a check.
    # Same rule that has always excluded `in_progress_label` (#1354): an overlay is not a state.
    # `primary` is now the membership-or-exit set: in the world / a human's exit / not admitted yet.
    primary = (goal_label, parked_label, proposed_label)
    seen = {}
    for label in primary:
        issues = _list(run, gh_cfg, ["number", "labels"], [label])
        if issues is None:
            continue
        for i in issues:
            if isinstance(i, dict) and "number" in i:
                seen[i["number"]] = i
    flagged = []
    for n, i in seen.items():
        names = [(l.get("name") or "") if isinstance(l, dict) else str(l or "")
                 for l in (i.get("labels") or [])]
        matched = [p for p in primary if p in names]
        if len(matched) > 1:
            flagged.append((n, matched))
    return flagged


def _pick_path_gate_state(disc, run):
    """#1207 (corrected per #1268 independent review): a plain statement of what actually gates the
    pick path for a github-discovery repo -- the board's Status field, or purely the label queue --
    so an operator moving a card to `Blocked` can tell, without reading `sources.py`, whether that
    protects the issue from being picked.

    The two config-only short-circuits below (`project.enabled`, `queue_source`) DO mirror
    `GitHubSource._ready_lane()`'s own first line byte for byte -- but, per the #1268 review finding,
    that line is NOT "the entire gate" as this docstring used to claim. `_ready_lane()` goes on past
    it to do a LIVE read: it confirms a board actually EXISTS (`_find_project`), then checks whether
    the Status field's options actually contain the configured `Ready` name -- and returns None
    (label queue, Blocked gates nothing) if either is missing, regardless of `project.enabled=True`
    and `queue_source="status"`. Two real states hit this: right after `sigma-init`, before the loop's
    first status WRITE (board creation is lazy, per sources.py's own comment); and any existing
    adopter who hasn't yet run `board_migrate.py` to add a `Ready` option -- `_ready_lane()`'s own
    docstring names that scenario "THE BACKWARD-COMPATIBILITY GATE". Reporting BOARD-GATED from
    config alone in either state is the exact false assurance issue #1207 was filed to prevent.

    So once config says "status", this reuses `_ready_lane()` itself (not a re-derived copy, to
    close off future drift the same way #1268's review closed this one) via a real `GitHubSource`
    built from the SAME config this check is examining. Strictly read-only, same guarantee
    `_ready_lane()`'s own docstring gives: nothing here can create a board or write anything.
    Fail-open, matching every other live check in this file (`_board_dup_risk` et al.): a can't-read
    (no auth, network down, `gh` missing, a raised exception) degrades to "not yet confirmed live",
    never to a false BOARD-GATED claim -- the "degrades cleanly when project/board state can't be
    read" requirement is met by treating unconfirmed the same as absent, not by skipping the read."""
    proj = _block(_block(disc, "github"), "project")
    if not bool(proj.get("enabled", False)):
        return ("LABEL-ONLY -- no board configured (discovery.github.project.enabled is not on), so "
                 "no board column -- Blocked included -- gates the pick path; only parked_label / "
                 "in_progress_label / blocked_label (if set) exclude an issue")
    queue_source = proj.get("queue_source", "status")
    if queue_source != "status":
        return (f"LABEL-ONLY -- queue_source is {queue_source!r}, not \"status\", so the pick path "
                 "never reads this board's columns at all: moving a card to Blocked (or any other "
                 "column) does NOT gate or protect it from being picked; only parked_label / "
                 "in_progress_label / blocked_label (if set) exclude an issue")
    try:
        sources = _load_loop_script("sources")
        ready_name = sources.GitHubSource({"discovery": disc}, run=_gh_runner(run))._ready_lane()
    except Exception:
        ready_name = None            # can't confirm live -- fail open to the not-yet-gated message
    if not ready_name:
        return ("LABEL-ONLY (not yet) -- queue_source is \"status\", but no live \"Ready\" option was "
                 "found on the board (either no board exists yet -- it is created lazily, on the "
                 "first status write, not at config time -- or this is an adopted board that has not "
                 "been migrated to add one): until then the pick path falls through to the label "
                 "queue exactly like queue_source=\"label\" -- moving a card to Blocked (or any other "
                 "column) does NOT gate or protect it from being picked. Run "
                 "`board_migrate.py --owner <owner> --project <n> --apply` if the board already "
                 "exists, or run the loop once to let it create one, then re-check.")
    return ("BOARD-GATED -- queue_source is \"status\" and a live \"Ready\" option was confirmed on "
            "the board: only a card in the Ready lane is picked; In Progress / QC / Done / Blocked "
            "(and any other column) are structurally un-pickable")


def _version_tuple(v):
    """Parse a plain dotted-integer version ("0.9.23") into a comparable tuple, or None for
    anything that doesn't parse cleanly — never raises."""
    try:
        return tuple(int(p) for p in str(v).strip().split("."))
    except (TypeError, ValueError):
        return None


# #1739/#2730: Sigma's OWN public repository -- the FALLBACK marketplace source only, used when the
# install's own marketplace record (`_installed_marketplace_repo`) cannot be read. Always this one
# string regardless of which repo is ADOPTING sigma (an adopter's own `discovery.github.repo` names
# THEIR repo, a different one entirely, and must never be read here). A hardcoded slug went stale
# once already, so this is the one place to update if the public repository moves -- and
# `tools/leak_scan.py` reads it from here (by `ast`, never by import) as the public slug.
_MARKETPLACE_REPO = "Agrim-Intelligence/sigmaloop"
#: The plugin's own name (#524), the half of an installed id before `@`. Every match of "is this the
#: Sigma Loop plugin" goes through this one constant, not a bare literal.
_PLUGIN = "sigmaloop"
_CODEX_PLUGINS_UNREAD = object()

#: An `owner/repo` slug as GitHub spells it; anything else read from a marketplace record is
#: untrusted and falls back to `_MARKETPLACE_REPO` rather than reaching a `gh api` path.
_SLUG_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9_.-]+$")
_CODEX_GIT_SOURCE_RE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:)([A-Za-z0-9-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$")


def _default_known_marketplaces_path():
    """`~/.claude/plugins/known_marketplaces.json`, honoring `CLAUDE_CONFIG_DIR` exactly as
    `_default_installed_plugins_path` does."""
    root = os.environ.get("CLAUDE_CONFIG_DIR") or str(pathlib.Path.home() / ".claude")
    return pathlib.Path(root) / "plugins" / "known_marketplaces.json"


def _slug_or_none(value):
    return value if isinstance(value, str) and _SLUG_RE.match(value) and ".." not in value else None


def _installed_marketplace_repo(marketplace, codex_entry=None, known_marketplaces_path=None):
    """The `owner/repo` the sigma plugin was INSTALLED from (#2730), or `_MARKETPLACE_REPO` when
    that cannot be read -- never raises.

    Claude: `marketplace` is the half after `@` in the installed id (`sigmaloop@<marketplace>`), looked
    up in Claude Code's `known_marketplaces.json`; only the `github` source shape is verified
    (`{"source": {"source": "github", "repo": "owner/repo"}}`), so a marketplace added by `git` URL,
    local directory or any other source falls back -- correct for an unmodified install, and the
    fallback (not a fork's own record) only for a fork added that way (stated limit, R1).
    Codex: `codex_entry["marketplaceSource"]` with `sourceType: git` and a GitHub URL, HTTPS or
    SSH (shape verified live); anything else falls back. A derived slug must be a plain
    `owner/repo` with no `..` before it reaches a `gh api` path."""
    try:
        if codex_entry is not None:
            source = codex_entry.get("marketplaceSource") if isinstance(codex_entry, dict) else None
            if isinstance(source, dict) and source.get("sourceType") == "git" \
                    and isinstance(source.get("source"), str):
                m = _CODEX_GIT_SOURCE_RE.match(source["source"])
                return _slug_or_none(m.group(1)) or _MARKETPLACE_REPO if m else _MARKETPLACE_REPO
            return _MARKETPLACE_REPO
        path = pathlib.Path(known_marketplaces_path or _default_known_marketplaces_path())
        registry = json.loads(path.read_text(encoding="utf-8"))
        record = registry.get(marketplace) if isinstance(registry, dict) else None
        source = record.get("source") if isinstance(record, dict) else None
        if isinstance(source, dict) and source.get("source") == "github":
            return _slug_or_none(source.get("repo")) or _MARKETPLACE_REPO
    except Exception:                        # noqa: BLE001 - unreadable record is "use the fallback"
        pass
    return _MARKETPLACE_REPO


def _plugin_host():
    """Use Codex's install inventory only in a Codex session; Claude wins if both mark it."""
    if not (os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE_SESSION_ID")) \
            and (os.environ.get("CODEX_SESSION_ID") or os.environ.get("CODEX_THREAD_ID")):
        return "codex"
    return "claude"


def _codex_plugins(run):
    """Codex's installed plugin records, or None if its CLI output cannot be trusted."""
    try:
        payload = json.loads(run(["codex", "plugin", "list", "--json"]) or "")
        entries = payload.get("installed") if isinstance(payload, dict) else None
        return entries if isinstance(entries, list) and all(isinstance(e, dict) for e in entries) else None
    except Exception:
        return None


def _codex_enabled(plugins, name):
    """One unambiguous enabled install, or None (absent, disabled, or duplicate)."""
    matches = [e for e in plugins if str(e.get("pluginId", "")).split("@")[0] == name]
    return matches[0] if len(matches) == 1 and matches[0].get("installed") is True \
        and matches[0].get("enabled") is True else None


def _plugin_versions(run, host="claude", codex_plugins=_CODEX_PLUGINS_UNREAD):
    """(installed, latest) version tuples for the sigma plugin itself, or None for either side
    that could not be determined — never raises; a can't-tell must never read as a false alarm (nor
    a false all-clear — see `check()`, which adds no entry at all unless BOTH sides resolve).

    `installed` comes from the selected host's plugin list (Claude's list records or Codex's
    enabled installed records), matched by the id's plugin-name prefix (e.g.
    "sigmaloop@sigmaloop" -> the part before "@"). `latest` comes from the current marketplace.json, on
    its default branch, of the repository the plugin was INSTALLED from
    (`_installed_marketplace_repo`: the marketplace half of the matched id, resolved through the
    host's own marketplace record), falling back to `_MARKETPLACE_REPO` -- the public repository --
    when that record cannot be read (#2730). There is no fallback on a failed fetch: a fork's
    version compared against upstream would be a false alarm, so a fetch that fails leaves
    `latest` None and no row is shown.

    The fetch is `gh api repos/<repo>/contents/<path> --jq .content` (#1739): authenticated with
    `gh`'s own already-configured credentials (the same ones every other check in this file already
    depends on), so it works for a private or a public repository alike, and returns the file's
    content base64-encoded, which is decoded before parsing -- no Claude Code API exposes "the
    latest available version" any other way. Auto-update itself is OFF by default for a
    non-Anthropic marketplace like this one, and even ON, it only fires on its own schedule at the
    next session launch — so a stale install can otherwise persist silently indefinitely; this is
    the awareness nudge that doesn't need auto-update to be on at all."""
    installed = None
    repo = _MARKETPLACE_REPO
    if host == "codex":
        plugins = _codex_plugins(run) if codex_plugins is _CODEX_PLUGINS_UNREAD else codex_plugins
        entry = _codex_enabled(plugins, _PLUGIN) if plugins is not None else None
        installed = _version_tuple(entry.get("version")) if entry else None
        if entry:
            repo = _installed_marketplace_repo(None, codex_entry=entry)
    else:
        try:
            for entry in json.loads(run(["claude", "plugin", "list", "--json"]) or "[]"):
                if isinstance(entry, dict) and str(entry.get("id", "")).split("@")[0] == _PLUGIN:
                    installed = _version_tuple(entry.get("version"))
                    plugin_id = str(entry.get("id", ""))
                    if "@" in plugin_id:
                        repo = _installed_marketplace_repo(plugin_id.split("@", 1)[1])
                    break
        except Exception:
            pass
    latest = None
    try:
        import base64
        raw_b64 = run(["gh", "api", "repos/%s/contents/.claude-plugin/marketplace.json"
                       % repo, "--jq", ".content"])
        raw = base64.b64decode(raw_b64) if raw_b64 else b""
        for entry in (json.loads(raw or b"{}").get("plugins") or []):
            if isinstance(entry, dict) and entry.get("name") == _PLUGIN:
                latest = _version_tuple(entry.get("version"))
                break
    except Exception:
        pass
    return installed, latest


#: The version floor, and where it comes from. `AGENTS.md` states it in prose ("If the installed
#: Sigma plugin is older than X, do not start the loop") and is the ONLY place that states it,
#: so it is parsed here rather than copied: a literal in this file is a second copy of a number
#: that moves every release, and a doctor still enforcing last release's floor is worse than one
#: enforcing none. AGENTS.md ships INSIDE the plugin (the install cache is a checkout of this repo),
#: so `_HERE.parent.parent.parent` resolves to the RUNNING plugin's own copy when doctor runs from
#: an install, and to the repo's copy when it runs from a checkout — either way the floor being
#: enforced is the one the running code was released with. test_doctor.py pins the sentence shape
#: against the real file, so a rewording that breaks this regex fails the suite loudly instead of
#: silently disabling the hard stop.
_AGENTS_MD = _HERE.parent.parent.parent / "AGENTS.md"
_FLOOR_RE = re.compile(r"plugin is older than\s+\**\s*(\d+(?:\.\d+)+)")


def _agents_floor(agents_md=None):
    """The minimum plugin version AGENTS.md forbids starting the loop below, as a comparable tuple
    — or None when the file or that sentence cannot be read. Never raises. None is a can't-tell,
    and its only effect is to DROP the row (see `_install_scope_row`); it never reads as an
    all-clear."""
    try:
        text = pathlib.Path(agents_md or _AGENTS_MD).read_text(encoding="utf-8")
    except Exception:                        # noqa: BLE001 - unreadable/absent is "cannot tell"
        return None
    m = _FLOOR_RE.search(text)
    return _version_tuple(m.group(1)) if m else None


def _codex_install_floor_row(plugins, agents_md=None):
    """Judge the active Codex install against the shipped floor, never Claude's scope file."""
    floor = _agents_floor(agents_md)
    if plugins is None or floor is None:
        return _chk("sigma Codex install: unverified", False,
                    "cannot verify the Codex plugin list or AGENTS.md floor; run `codex plugin list --json` "
                    "and inspect the installed, enabled Sigma version before starting the loop")
    entry = _codex_enabled(plugins, _PLUGIN)
    if entry is None:
        return _chk("sigma Codex install: absent or disabled", False,
                    "install or enable %s@%s in Codex before starting the loop" % (_PLUGIN, _PLUGIN))
    version = _version_tuple(entry.get("version"))
    if version is None:
        return _chk("sigma Codex install: version unverified", False,
                    "Codex reports no parseable installed Sigma version; check `codex plugin list --json` "
                    "before starting the loop")
    if version < floor:
        return _chk(f"sigma Codex install: {_vstr(version)} below AGENTS.md floor {_vstr(floor)}",
                    False, "refresh the Sigma Codex marketplace and reinstall or update the "
                    "Codex plugin before starting the loop")
    return _chk(f"sigma Codex install: {_vstr(version)} at or above AGENTS.md floor {_vstr(floor)}",
                True, "")


def _default_installed_plugins_path():
    """`~/.claude/plugins/installed_plugins.json`, honoring `CLAUDE_CONFIG_DIR` exactly as
    `_default_scheduled_tasks_dir` does — a machine that pins that env var must not make this scan
    silently read a different home's install records (or none at all)."""
    root = os.environ.get("CLAUDE_CONFIG_DIR") or str(pathlib.Path.home() / ".claude")
    return pathlib.Path(root) / "plugins" / "installed_plugins.json"


def _install_scopes(installed_plugins_path=None):
    """Every scope sigma is installed under, as `(plugin_id, scope, version_tuple, projectPath)`
    — or **None** when that cannot be determined, which is NOT the same as "installed nowhere" and
    must never be reported as one.

    THE SHAPE IS CLAUDE CODE'S, NOT OURS. `installed_plugins.json` holds one entry PER SCOPE under
    each plugin id: a `user` entry applies everywhere, while `project` (and `local`, which is a
    per-project scope too) carry a `projectPath` and shadow user scope FOR THAT PATH ONLY. Verified
    against a real file holding 6 sigma entries, 1 user + 5 project, each project entry a
    separate worktree of one repo — and against `update-sigma.sh`, which resolves precedence
    the same way (a per-project scope matching this repo root wins; user is the fallback).

    MATCHED ON THE ID'S PLUGIN HALF, not the whole id, mirroring `_plugin_versions` — the
    marketplace half is whatever the marketplace was named, so a fork or a rename must not make the
    row vanish. Unknown fields are ignored rather than rejected: this schema is versioned by someone
    else and WILL gain fields.

    `version` is a tuple or None; None means the recorded version did not parse, and the caller
    treats that as "not shown to clear the floor" rather than as safe."""
    try:
        raw = json.loads(pathlib.Path(
            installed_plugins_path or _default_installed_plugins_path()).read_text(encoding="utf-8"))
    except Exception:                        # noqa: BLE001 - missing/unreadable/unparseable
        return None
    plugins = raw.get("plugins") if isinstance(raw, dict) else None
    if not isinstance(plugins, dict):
        return None
    out = []
    for pid, entries in plugins.items():
        if str(pid).split("@")[0] != _PLUGIN or not isinstance(entries, list):
            continue
        for e in entries:
            if not isinstance(e, dict):
                continue
            path = e.get("projectPath")
            path = str(path) if isinstance(path, str) and path.strip() else None
            out.append((str(pid), str(e.get("scope") or "user"), _version_tuple(e.get("version")), path))
    return out or None                       # nothing readable == cannot tell, never "all clear"


#: The pre-launch plugin and marketplace name (#524), and the repository the pre-launch installs came
#: from. Spelled from fragments so the leftover-name check, which forbids the old install id outside
#: one document, does not flag this file.
_OLD_PLUGIN = "sig" + "ma"
_OLD_ID = _OLD_PLUGIN + "@" + _OLD_PLUGIN
_OLD_REPO = "Agrim-Intelligence/" + _OLD_PLUGIN


def _recorded_source(marketplace=None, codex_entry=None, known_marketplaces_path=None):
    """The `owner/repo` an install's own marketplace record names, or None when it cannot be read.
    Unlike `_installed_marketplace_repo` it never falls back: "cannot tell" must stay distinguishable
    from "it is the public repository"."""
    try:
        if codex_entry is not None:
            source = codex_entry.get("marketplaceSource") if isinstance(codex_entry, dict) else None
            if isinstance(source, dict) and source.get("sourceType") == "git" \
                    and isinstance(source.get("source"), str):
                m = _CODEX_GIT_SOURCE_RE.match(source["source"])
                return _slug_or_none(m.group(1)) if m else None
            return None
        path = pathlib.Path(known_marketplaces_path or _default_known_marketplaces_path())
        registry = json.loads(path.read_text(encoding="utf-8"))
        record = registry.get(marketplace) if isinstance(registry, dict) else None
        source = record.get("source") if isinstance(record, dict) else None
        if isinstance(source, dict) and source.get("source") == "github":
            return _slug_or_none(source.get("repo"))
        if isinstance(source, dict) and source.get("source") == "git" and isinstance(source.get("url"), str):
            m = _CODEX_GIT_SOURCE_RE.match(source["url"])         # a fork added by git URL is a fork
            return _slug_or_none(m.group(1)) if m else None
    except Exception:                        # noqa: BLE001 - an unreadable record is "cannot tell"
        pass
    return None


def _recorded_target(marketplace=None, codex_entry=None, known_marketplaces_path=None):
    """What `marketplace add` must be given to re-add the marketplace an install came from, or None:
    the github `owner/repo`, the git URL, or the local checkout path the record names. Printed, never run."""
    try:
        if codex_entry is not None:
            source = codex_entry.get("marketplaceSource") if isinstance(codex_entry, dict) else None
            value = source.get("source") if isinstance(source, dict) else None
            return value if isinstance(value, str) and value.strip() else None
        path = pathlib.Path(known_marketplaces_path or _default_known_marketplaces_path())
        registry = json.loads(path.read_text(encoding="utf-8"))
        record = registry.get(marketplace) if isinstance(registry, dict) else None
        source = record.get("source") if isinstance(record, dict) else None
        if not isinstance(source, dict):
            return None
        kind = source.get("source")
        value = source.get("repo") if kind == "github" else source.get("url") if kind == "git" \
            else source.get("path") if kind in ("directory", "file") else None
        return value if isinstance(value, str) and value.strip() else None
    except Exception:                        # noqa: BLE001 - an unreadable record is "cannot tell"
        return None


def _is_pre_launch_install(plugin_id, source):
    """An install recorded under the pre-launch plugin name AND from the pre-launch repository. A fork
    (any other recorded source) is not ours to migrate, even when it kept the manifest names; when the
    source cannot be read only the exact old id counts."""
    if str(plugin_id).split("@")[0] != _OLD_PLUGIN:
        return False
    if source is None:
        return str(plugin_id) == _OLD_ID
    return source.lower() == _OLD_REPO.lower()


def _old_install_row(host, ours, others_on_marketplace=(), new_present=False):
    """The row for installs recorded under the pre-launch id, or None. It PRINTS the removal and the
    reinstall for `host` and runs none of them: `ours` is `[(id, scope, projectPath, source)]`."""
    if not ours:
        return None
    import shlex
    q = shlex.quote
    ids = sorted({o[0] for o in ours})
    # What the install recorded; when it recorded nothing readable the row says so instead of naming the
    # public repository (which may not exist yet, and is not where a private copy came from).
    source = q(next((o[3] for o in ours if o[3]), None) or "") if any(o[3] for o in ours) \
        else "<the repository or checkout your install came from>"
    steps = []
    if host == "codex":
        for pid in ids:
            steps.append("codex plugin remove %s" % q(pid))
        if not new_present:
            steps += ["codex plugin marketplace add %s" % source,
                      "codex plugin add %s@%s" % (_PLUGIN, _PLUGIN)]
        note = ("the removal verb is the one codex-cli 0.154 lists; confirm with `codex plugin --help` on this "
                "host before running it. These Codex steps were not run end to end on a real host")
    else:
        for pid, scope, path, _src in sorted(ours, key=lambda o: (o[0], o[1], o[2] or "")):
            cmd = "claude plugin uninstall %s" % q(pid)
            if scope in ("project", "local"):
                cmd += " --scope %s" % scope
            elif scope != "user":
                steps.append("scope %s of %s is managed by whoever set it up; this command cannot remove it" % (q(scope), q(pid)))
                continue
            if path:
                cmd += " (run it from %s)" % q(path)
            steps.append(cmd)
        for market in sorted({pid.split("@", 1)[1] for pid in ids if "@" in pid}):
            if market in others_on_marketplace:
                steps.append("keep marketplace %s: other installed plugins use it" % market)
            else:
                steps.append("claude plugin marketplace remove %s" % q(market))
        if not new_present and not others_on_marketplace:
            steps += ["claude plugin marketplace add %s" % source,
                      "claude plugin install %s@%s" % (_PLUGIN, _PLUGIN)]
        note = ("Each uninstall is per scope; the reinstall is at user scope" + (
                "; with a marketplace shared by other plugins this exact sequence was not run end to end"
                if others_on_marketplace else ""))
    if others_on_marketplace and not new_present:
        note += (". The marketplace is shared, so it was kept and no add or install step is printed: re-adding the "
                 "same source would be a no-op under the old marketplace name, and %s@%s cannot install from it. "
                 "Decide what to do with that marketplace, then add the new one from %s and install %s@%s"
                 % (_PLUGIN, _PLUGIN, source, _PLUGIN, _PLUGIN))
    if new_present:
        note += (". %s is already installed here: do NOT add the old source again or install it again (the host "
                 "would repoint the working install at that source); remove the old install only" % _PLUGIN)
    return _chk("Sigma Loop plugin installed under the pre-launch id %s (it no longer receives updates)"
                % ", ".join(ids), False,
                "the plugin was renamed to %s, so an install under the old id silently stopped updating. "
                "Run, in order, yourself, one command at a time (the doctor prints these and removes nothing): %s. %s. "
                "Your `.sdlc/` data is kept as is. Details: docs/upgrading.md, 'From the pre-launch name'."
                % (_PLUGIN, "  ".join("(%d) %s" % (i, s) for i, s in enumerate(steps, 1)), note))


def _claude_old_install_row(installed_plugins_path=None, known_marketplaces_path=None):
    """Read Claude's install records and build the pre-launch row, or None (also on any read failure)."""
    try:
        raw = json.loads(pathlib.Path(
            installed_plugins_path or _default_installed_plugins_path()).read_text(encoding="utf-8"))
        plugins = raw.get("plugins") if isinstance(raw, dict) else None
        if not isinstance(plugins, dict):
            return None
        ours, ours_markets = [], set()
        for pid, entries in plugins.items():
            pid = str(pid)
            market = pid.split("@", 1)[1] if "@" in pid else ""
            if not isinstance(entries, list) or not _is_pre_launch_install(
                    pid, _recorded_source(market, known_marketplaces_path=known_marketplaces_path)):
                continue
            ours_markets.add(market)
            target = _recorded_target(market, known_marketplaces_path=known_marketplaces_path)
            for e in entries:
                if isinstance(e, dict):
                    path = e.get("projectPath")
                    ours.append((pid, str(e.get("scope") or "user"),
                                 str(path) if isinstance(path, str) and path.strip() else None, target))
        new_present = any(str(pid).split("@")[0] == _PLUGIN for pid in plugins)
        others = {str(pid).split("@", 1)[1] for pid in plugins
                  if "@" in str(pid) and str(pid).split("@", 1)[1] in ours_markets
                  and not any(o[0] == str(pid) for o in ours)}
        return _old_install_row("claude", ours, others, new_present)
    except Exception:                        # noqa: BLE001 - cannot read == cannot tell, never an alarm
        return None


def _codex_old_install_row(plugins):
    """The pre-launch row from Codex's already-fetched plugin list, or None."""
    try:
        ours = [(str(e.get("pluginId", "")), "user", None, _recorded_target(codex_entry=e))
                for e in (plugins or []) if isinstance(e, dict) and e.get("installed") is True
                and _is_pre_launch_install(e.get("pluginId", ""), _recorded_source(codex_entry=e))]
        new_present = any(isinstance(e, dict) and str(e.get("pluginId", "")).split("@")[0] == _PLUGIN
                          and e.get("installed") is True for e in (plugins or []))
        return _old_install_row("codex", ours, new_present=new_present)
    except Exception:                        # noqa: BLE001
        return None


def _realpath(p):
    try:
        return os.path.realpath(str(p))
    except Exception:                        # noqa: BLE001 - an unresolvable path compares as itself
        return str(p)


def _vstr(v):
    return ".".join(str(x) for x in v) if v else "?"


def _scope_desc(entry):
    pid, scope, ver, path = entry
    where = path if path else ("everywhere" if scope == "user" else "path not recorded")
    return f"{scope} {_vstr(ver)} ({where})"


def _scope_list(entries, cap=5):
    shown = ", ".join(_scope_desc(e) for e in entries[:cap])
    return shown + (f" (+{len(entries) - cap} more)" if len(entries) > cap else "")


def _install_scope_row(repo_root, installed_plugins_path=None, agents_md=None):
    """The install-scoping row, or None when it must be omitted (#1604).

    WHY THIS EXISTS. `claude plugin update --scope user` reports success while a project-scope entry
    for some other path keeps that path on an old version forever — measured on a real machine as a
    main checkout pinned to 1.1.2 while twelve other paths were at 1.3.7, in the one directory where
    AGENTS.md forbids starting the loop at all. The failure is silent by construction: the shadowed
    path just keeps running old code. `update-sigma.sh` cannot close it either — it deliberately
    skips an entry belonging to another project (correctly: a script must not mutate another
    project's install) and says nothing about it. So the report is the fix.

    TWO STALENESS CONDITIONS, DELIBERATELY NOT CONFLATED. Below the AGENTS.md floor is a hard stop
    and fails this row; merely behind the marketplace is informational and belongs to the separate
    "sigma up to date" nudge. A row that treated them alike would be ignored for the loud half
    and would panic people about the quiet half.

    READ-ONLY, ALWAYS. Nothing here installs, updates or removes anything; it names the gesture and
    stops. The gesture it names for a redundant override is REMOVAL, not "update N places forever":
    a project scope that exists for no reason is the hazard, and uninstalling it makes the path
    inherit user scope again.

    DEAD ENTRIES ARE COUNTED, NOT FLAGGED. Worktree-per-goal is the workflow this kit recommends, so
    deleted worktrees leave entries behind. Such an entry governs nothing, and its remedy (`cd` there
    and uninstall) is impossible — flagging it would be a permanently red row, which is how a check
    earns being ignored along with its true positives.

    OMITTED, NEVER GREEN, WHEN IT CANNOT SEE. A missing/unreadable/malformed `installed_plugins.json`
    and an unreadable floor both return None and drop the row entirely — the same convention
    `_secret_file_coverage` already established, because a green row meaning "we did not look" is the
    worst of the three outcomes."""
    floor = _agents_floor(agents_md)
    if floor is None:
        return None
    entries = _install_scopes(installed_plugins_path)
    if not entries:                          # None (cannot tell) and [] (nothing to say) alike
        return None

    floor_s, here = _vstr(floor), _realpath(repo_root)
    live, dead = [], []
    for e in entries:
        _pid, scope, _ver, path = e
        (dead if scope != "user" and path and not os.path.isdir(path) else live).append(e)

    users = [e for e in live if e[1] == "user"]
    overrides = [e for e in live if e[1] != "user"]
    bad = [e for e in live if e[2] is None or e[2] < floor]
    # "this run can fix it" is decided by whether the GESTURE works from here: `--scope user` runs
    # anywhere, and a per-project scope only where its own projectPath is. An entry with no path
    # recorded cannot be located, so it is not claimed as fixable here.
    here_bad = [e for e in bad if e[1] == "user" or (e[3] and _realpath(e[3]) == here)]
    # Partitioned by identity, not by value: two byte-identical entries (the same scope recorded
    # twice) must not both land in `here_bad` because one of them matched.
    _here_ids = {id(e) for e in here_bad}
    there_bad = [e for e in bad if id(e) not in _here_ids]

    head = f"user {_vstr(users[0][2])}" if users else "no user-scope install"
    head += (f" + {len(overrides)} project-scope override(s)" if overrides
             else " only, no project-scope overrides")
    if dead:
        head += f" ({len(dead)} recorded for a path that no longer exists)"

    if not bad:
        # Removal is only advised for an override that duplicates user scope. Advising it for one
        # that is AHEAD of user scope would be advising a silent downgrade.
        pid = (users or overrides)[0][0]
        redundant = bool(users) and any(e[2] == users[0][2] for e in overrides)
        tail = (f", all at or above the AGENTS.md floor {floor_s}" if overrides
                else f" — nothing can shadow it here (AGENTS.md floor {floor_s})")
        if redundant:
            tail += (f"; a redundant one is removable with `claude plugin uninstall {pid} "
                     "--scope project` from its own path")
        return _chk(f"sigma install scopes: {head}{tail}", True, "")

    # An unreadable version has not been SHOWN to clear the floor, so it fails the row (fail
    # closed) -- but calling it "below the floor" would be a claim we cannot make, so the verdict
    # softens to "not confirmed" the moment one is present.
    judged = ("below the" if all(e[2] is not None for e in bad)
              else "not confirmed at or above the")
    verdict = f"{len(bad)} {judged} AGENTS.md floor {floor_s}"
    parts = [f"{len(bad)} install scope(s) {judged} {floor_s} floor AGENTS.md sets for starting the "
             "loop -- a hard stop, not the 'a newer release exists' nudge. A project-scope entry "
             "shadows user scope for its own path, so a user-scope update reports success while "
             "that path keeps running old code."]
    if here_bad:
        # One command PER STALE SCOPE. Naming only the first would leave the others silently stale,
        # which is the exact failure this row exists to end.
        cmds = "; ".join(f"claude plugin update {pid} --scope {sc}"
                         for pid, sc in sorted({(e[0], e[1]) for e in here_bad}))
        parts.append("fixable from THIS directory: " + _scope_list(here_bad) +
                     f" -- run `{cmds}` here.")
        # An override that is BOTH stale and redundant will hide a stale version again next
        # release; removing it will not. Only offered when user scope actually clears the floor --
        # inheriting from a user scope that is itself below it fixes nothing.
        stale_here = [e for e in here_bad if e[1] != "user"]
        inherit = users[0] if users and users[0][2] is not None and users[0][2] >= floor else None
        if stale_here and inherit:
            parts.append("Better still, if that override exists for no reason, remove it rather "
                         f"than keep another install to maintain: `claude plugin uninstall "
                         f"{stale_here[0][0]} --scope project` here makes this path inherit user "
                         f"scope ({_vstr(inherit[2])}) again.")
    if there_bad:
        pid = there_bad[0][0]
        parts.append("NOT fixable from here -- each governs another path and the gesture must be "
                     "run from THAT directory: " + _scope_list(there_bad) +
                     f" -- and if the override exists for no reason, remove it rather than keep N "
                     f"installs in step: `claude plugin uninstall {pid} --scope project` there "
                     "makes the path inherit user scope again.")
    if dead:
        parts.append(f"{len(dead)} further entr{'y names' if len(dead) == 1 else 'ies name'} a path "
                     "that no longer exists; those govern nothing and are not judged against the floor.")
    return _chk(f"sigma install scopes: {head} — {verdict}", False, " ".join(parts))


#: Every placeholder in each north-star tier, from the scaffolded template (sdlc_init.py's
#: `_NORTH_STAR`) — short, distinctive prefixes (not the full strings) so a later wording tweak to
#: the trailing text doesn't silently break the check. "filled" must clear EVERY tier (F33/#358): a
#: north-star with only Vision written up used to read as done while Strategy/Design/Architecture
#: still held placeholder text. It must also clear EVERY placeholder WITHIN a tier (#445 F33
#: follow-up): Strategy scaffolds two (Priorities, Non-goals) and Architecture scaffolds three (the
#: intro line + two numbered example rules) — filling in only one of a tier's placeholders used to
#: read as that whole tier being "filled" because the check tested for just one representative
#: placeholder per tier.
_NORTH_STAR_TIERS = (
    ("Vision", ("<the change you want",)),
    ("Strategy", ("<the few things that matter", "<what we are deliberately NOT doing")),
    ("Design", ("<the experience + the principles",)),
    ("Architecture", ("<the shape of the system", "<e.g. the UI layer holds no business logic",
                       "<e.g. dependencies point inward")),
)


def _default_site_packages_dirs():
    """Every site-packages directory the CURRENTLY RUNNING interpreter would search — global +
    user (if enabled). Necessarily sees only ITS OWN interpreter's installs: a different local
    `python3` (pyenv, conda, a venv) is invisible here, the same "can only see what it can see"
    limit `_plugin_versions` above already accepts for its own installed-version check rather than
    trying to solve."""
    import site
    dirs = list(site.getsitepackages()) if hasattr(site, "getsitepackages") else []
    if site.ENABLE_USER_SITE:
        dirs.append(site.getusersitepackages())
    return dirs


def _has_local_python_project(root):
    """A Python package declared at `root` itself, or one level down — this repo's own shape, a
    package one level down, is a subdirectory, not the worktree root. Deep enough to catch the
    common monorepo-subproject layout without an unbounded recursive walk."""
    for marker in ("pyproject.toml", "setup.py", "setup.cfg"):
        if (root / marker).exists():
            return True
        if next(root.glob(f"*/{marker}"), None) is not None:
            return True
    return False


def _stray_worktree_installs(worktree_root, site_packages_dirs=None):
    """Locally-sourced (`pip install [-e] <path>`) distributions in site-packages whose declared
    top-level import name ALSO exists as real code under `worktree_root`, but whose install source
    is a DIFFERENT path. Each one is a same-named shadow: pip bakes an absolute source path in at
    install time (there is no "resolve to whichever worktree I'm in" mechanism), and `python3 -m` /
    pytest are the only invocations that prepend cwd ahead of site-packages — so a plain
    `python3 script.py` anywhere else that does `import <name>` silently gets THIS install's code
    instead of its own worktree's, no error of any kind.

    Correlating against `worktree_root`'s own directory listing (never a hardcoded package name)
    is what keeps this useful for every adopter, not just one dogfooding project on top of
    sigma: it only fires when a local install collides with code actually present in the repo
    being audited, so an unrelated globally-installed tool — or, on macOS, the Apple-bundled system
    packages living in this same site-packages tree — never lights this up.

    `site_packages_dirs` is DI for tests, mirroring `scheduled_tasks_dir` above."""
    import urllib.parse
    dirs = site_packages_dirs if site_packages_dirs is not None else _default_site_packages_dirs()
    worktree_root = pathlib.Path(worktree_root)
    found = []
    for dist in importlib.metadata.distributions(path=dirs):
        try:
            top_level = (dist.read_text("top_level.txt") or "").split()
            direct_url = dist.read_text("direct_url.json")
        except OSError:
            continue
        if not top_level or not direct_url:
            continue                          # no local source recorded -- an ordinary PyPI install
        try:
            url = json.loads(direct_url).get("url", "")
        except (json.JSONDecodeError, AttributeError):
            continue
        if not url.startswith("file://"):
            continue                          # VCS / http(s) source -- not a local path install
        # Decode file:// URL to local path. We cannot use urllib.request.url2pathname
        # (banned in skills/ by test_no_network_in_core.py) but must still handle Windows
        # drive-letter paths correctly: url2pathname strips the leading / before a drive
        # letter (e.g. /C:/ -> C:/) and converts / to \.
        path = urllib.parse.unquote(urllib.parse.urlparse(url).path)
        if sys.platform.startswith("win") or os.name == "nt":
            # On Windows, strip leading / before drive letter (e.g., /C:/ -> C:/)
            if len(path) > 2 and path[0] == "/" and path[2] == ":" and path[1].isalpha():
                path = path[1:]
            # Convert forward slashes to backslashes
            path = path.replace("/", "\\")
        source = pathlib.Path(path)
        for name in top_level:
            candidate = worktree_root / name
            if not candidate.exists():
                continue                      # this worktree has no code by that name -- not a shadow
            if os.path.realpath(source) == os.path.realpath(candidate):
                continue                      # this worktree's own install of its own package
            try:
                dist_name = dist.metadata["Name"]
            except Exception:
                dist_name = str(dist)
            found.append((dist_name, name, str(source)))
    return found


def _kg_last_attempt(path):
    """#2704: one clause for the auto-refresh row -- `none recorded`, or `<age> ago, ok`, or
    `<age> ago, FAILED: <the builder's own text>`. Garbage or a missing file reads as none
    recorded; never raises."""
    import time as _time
    try:
        rec = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        at = float(rec["at"])
    except Exception:                           # noqa: BLE001 -- absent/garbage -> "none recorded"
        return "none recorded"
    s = max(0, int(_time.time() - at))
    age = f"{s // 60}m" if s < 3600 else f"{s // 3600}h" if s < 86400 else f"{s // 86400}d"
    if rec.get("ok"):
        return f"{age} ago, ok"
    detail = " ".join(str(rec.get("detail", "")).split())[:300]
    return f"{age} ago, FAILED: {detail}"


def check(sdlc_dir=".sdlc", run=None, scheduled_tasks_dir=None, site_packages_dirs=None,
          installed_plugins_path=None, agents_md=None, cheap_only=False, which=None):
    """Return the setup checks relevant to this project's config; each is {name, ok, fix}.

    `site_packages_dirs` is DI for tests (mirrors `scheduled_tasks_dir`) — default `None` scans the
    real interpreter's site-packages.

    `scheduled_tasks_dir` is DI for tests (mirrors `run`) — default `None` scans the real
    `~/.claude/scheduled-tasks/` (or `CLAUDE_CONFIG_DIR`-relative equivalent).

    `installed_plugins_path` / `agents_md` are DI for tests (same idiom) — default `None` reads the
    real `~/.claude/plugins/installed_plugins.json` and the plugin's own shipped `AGENTS.md`.

    `cheap_only=True` SKIPS THE PLUGIN CLI and marketplace fetch: the `companions` presence rows,
    the `sigma up to date` nudge, and on Codex the install-floor row (Codex exposes its
    installed version through its plugin CLI). It
    exists for the guided setup wizard (issue #1560), whose SessionStart hook calls this
    unconditionally, in every repo, once per session — so an unconditional subprocess+network cost
    there is spent without the operator ever opting in, which AGENTS.md's SAFETY property forbids.
    preserves the pre-existing Claude wizard cost: the Claude scope-floor row is a local file read
    and still runs. Default `False` — `/sigma-doctor` performs the full sweep on either host.

    Deliberately narrow: the `gh` calls under `discovery.source == "github"` are NOT gated by this.
    They are the checks a github-mode repo exists to have run, they only fire because that repo's
    own config asked for github discovery (opt-in already), and the wizard's own hour-long clean
    cache is what bounds their frequency. `cheap_only` is about cost nobody asked for, not about
    all cost."""
    injected = run is not None
    # #229 `which` seam: an injected `run` stands in for every binary (the existing tests' contract),
    # so it defaults to "present" there; the real runner asks the real PATH.
    which = which or ((lambda name: name) if injected else shutil.which)
    run = run or _real_run
    plugin_host = _plugin_host()
    codex_plugins = _codex_plugins(run) if plugin_host == "codex" and not cheap_only else None
    base = pathlib.Path(sdlc_dir)
    cfg = _cfg(sdlc_dir)
    disc = _block(cfg, "discovery")
    kg = _block(cfg, "knowledge_graph")
    bchk = _block(cfg, "backlog_check")
    out = [_chk("project layer", (base / "config.json").exists(), "run /sigma-init to scaffold .sdlc/")]

    # #1200 gap 2: informational, always ok=True -- same "state embedded in the name, no fix"
    # idiom the "companions" rows below already use (a plugin being present/absent is neither a
    # pass nor a fail either). Unconditional (unlike the github-gated checks below it) since a
    # managing session can drive a local-goals `.sdlc` just as much as a github-backed one.
    out.append(_chk(f"managing session: {_managing_session_state(base, cfg)}", True, ""))

    # #2571: managed settings (org policy file). A core feature, and since #2575 every row in
    # this file is one -- anything installed alongside the core reports its own health itself.
    # Informational: absence is normal (not adopted), presence is reported as state.
    out.append(_chk(f"managed settings: {_managed_settings_state(base, cfg)}", True, ""))

    # ---- #240: coexistence with the plugin under the previous name (read-only: WARN, proceed).
    # Everything lives in sigma-loop's coexist.py; this block only appends its row. Local reads
    # only (no CLI, no network), so it is not gated by `cheap_only`.
    try:
        out.append(_load_loop_script("coexist").doctor_row(sdlc_dir))
    except Exception as exc:                 # noqa: BLE001 - a detector that cannot run says so
        out.append({"name": f"coexistence: could not check ({type(exc).__name__})", "ok": True,
                    "fix": "python3 skills/sigma-loop/scripts/coexist.py check <sdlc_dir>"})
    # #327: live legacy delta records (the previous plugin's post-conversion unit records). Local
    # reads only; the row appears only when there is one, so a healthy check list is unchanged.
    try:
        delta_row = _load_loop_script("feature_sync").legacy_delta_row(sdlc_dir)
        if delta_row:
            out.append(delta_row)
    except Exception as exc:                 # noqa: BLE001 - a detector that cannot run says so
        out.append({"name": f"legacy delta records: could not check ({type(exc).__name__})",
                    "ok": True, "fix": "python3 skills/sigma-loop/scripts/feature_sync.py show "
                                       "<sdlc_dir>"})
    # ---- end #240
    hooks_row = _stale_hook_path_row(base.parent)            # #614
    if hooks_row:
        out.append(hooks_row)
    # #144: a feature branch whose rebase upkeep is REFUSING a replay that would delete its content.
    # Emitted only when such a refusal is on record, so a healthy project's check list is unchanged.
    for branch, count, names, at in _rebase_blocks(base, _block(cfg, "work")):
        out.append(_chk(
            f"rebase upkeep of {branch} not blocked", False,
            f"since {at}, bringing {branch} forward would remove or roll back {count} tracked path(s) "
            f"({names}); nothing was pushed. Usually the base holds a revert of the branch's own "
            "commits -- see docs/branching-model.md §3b for the resolution, or set "
            '`work.rebase_upkeep: "off"` while it stands.'))

    # #936: a unit landing left pending (upkeep part C). Gated on the upkeep block and read-only: a closed gate
    # emits nothing, so a project without the block sees an unchanged check list.
    try:
        import time as _t
        landing_row = _load_loop_script("feature_upkeep_landing").doctor_row(base, cfg, int(_t.time()))
    except Exception:                     # noqa: BLE001 - a doctor row never crashes the doctor
        landing_row = None
    if landing_row:
        out.append(_chk(landing_row["name"], landing_row["ok"], landing_row["fix"]))

    # Part B, level 3: a unit whose rebase was PARKED on a conflict nobody resolved. Its own marker file and its own
    # wording -- never the would-drop row above, which would call a park "N tracked paths removed". Emitted only when a
    # park is on record (the marker exists only when the upkeep gate was open), so a project that never opted in sees
    # nothing, and the row names how long it has been waiting: age is the tell, not an error state.
    for branch, files, at, age in _rebase_parks(base, _block(cfg, "work")):
        out.append(_chk(
            f"rebase upkeep of {branch} not parked", False,
            f"parked since {at} ({age}) on a conflict in {files} file(s); nothing was pushed and the unit stays behind "
            "its base until a person resolves it -- the finding filed for it carries a brief. A later clean pass "
            "clears this row and closes the finding."))

    # A shared site-packages holds one slot per import name. A local `pip install [-e] <path>` bakes
    # that path in permanently, so on a machine running several worktrees of the same repo (this
    # project's own normal working style), whichever worktree last ran that command silently wins
    # the import for every OTHER worktree's plain `python3 script.py` invocations (never for `-m` /
    # pytest, which both prepend cwd ahead of site-packages). Gated on a pyproject.toml/setup.py/
    # setup.cfg existing at all, mirroring the north-star check below: an adopter with no local
    # Python package here pays nothing for a check that can never fire for them.
    worktree_root = base.parent
    if _has_local_python_project(worktree_root):
        shadows = _stray_worktree_installs(worktree_root, site_packages_dirs)
        if shadows:
            desc = ", ".join(f"{d} ({imp} -> {src})" for d, imp, src in shadows[:5])
            more = f" (+{len(shadows) - 5} more)" if len(shadows) > 5 else ""
            fix = (f"{desc}{more} -- installed from a DIFFERENT worktree's path, but this worktree "
                   "has its own same-named code too. A plain `python3 script.py` (not `-m`, not "
                   "pytest) here would silently import the wrong one. `pip uninstall <name>` each, "
                   "then use `python -m` or explicit sys.path insertion instead of a local install.")
        else:
            fix = ""
        out.append(_chk("no stray local install shadows this worktree's own packages",
                        not shadows, fix))

    # #229: git / remote / base / gh installed / gh auth / scopes (+ project scope with a board),
    # wherever work.enabled or github discovery needs them -- the same checks /sigma-init runs, so a
    # later regression is visible. Each fix comes from the FAILING check: gh absent says install it.
    if _block(cfg, "work").get("enabled") or disc.get("source") == "github":
        out.extend(_preflight_rows(base, cfg, run, which, injected, cheap_only))
        out.extend(_graphql_capability_rows(base, cfg))     # #801: advisory, detection only
    if disc.get("source") == "github":
        gh_disc = _block(disc, "github")
        if _block(gh_disc, "project").get("enabled"):
            dup = _board_dup_risk(gh_disc, run)
            if dup:
                out.append(_chk("project.number pinned (no duplicate-board risk)", False, dup))
            # #280: ONE field-list read, shared by the Status-columns row and the custom-fields
            # row below (the custom-fields row always made this read, cheap_only or not).
            board_fields = _board_field_list(gh_disc, run)
            # #235: a GraphQL-backed read, so never under cheap_only (the SessionStart wizard).
            # A read that failed is no row at all -- never a pass, never a false alarm.
            if not cheap_only:
                state, gone = _pinned_board_state(gh_disc, run)
                if state in ("ok", "gone"):
                    out.append(_chk("pinned board #%s reachable"
                                    % _block(gh_disc, "project").get("number"), state == "ok", gone))
                # #280: read-only, same gate. A column with no matching Status option makes that
                # card move a no-op; a case/spacing-only difference already matches.
                unmatched = (_board_columns_unmatched(gh_disc, run, board_fields)
                             if state == "ok" else None)
                if unmatched is not None:
                    out.append(_chk(
                        "board Status options match the loop's columns", not unmatched,
                        "pinned board #%s's Status field has no single matching option for %s, so "
                        "the loop cannot move a card there (it warns once per run). Add the option "
                        "on the board, or set discovery.github.project.columns.<key> to the "
                        "board's exact spelling."
                        % (_block(gh_disc, "project").get("number"), ", ".join(unmatched))))
            stale_cards = _item_closed_workflow_off(gh_disc, run)
            if stale_cards:
                out.append(_chk("board marks closed items Done", False, stale_cards))
            stranded_done = _open_issue_done_card(gh_disc, run)
            if stranded_done:
                out.append(_chk("no open issue stranded at board Done", False, stranded_done))
            unmapped = _unmapped_board_fields(gh_disc, run, board_fields)
            if unmapped is not None:
                out.append(_chk("board custom fields mapped", not unmapped,
                                "the board has single-select field(s) sigma won't set on issues it "
                                "creates: " + ", ".join(n for n in unmapped if n) + " - map them in "
                                "discovery.github.project.custom_fields (field -> option), or backfill by "
                                "hand, else a loop-created (hand-off) issue is left blank on them."))
        # Independent of board mirroring: auto_merge/require_review/branch-protection is a merge-gate
        # concern, not a board one — must not be gated on project.enabled.
        selfmerge = _self_merge_risk(gh_disc, _block(cfg, "work"), run)
        if selfmerge:
            out.append(_chk("an approval from someone other than the author is required before auto-merge", False, selfmerge))

        # #2452: independent of work.enabled -- a human working the main checkout by hand strands a
        # branch exactly as easily as the loop can. repo_root is base.parent, inlined the same way
        # the secret-coverage row below does (no new variable). sdlc_dir (the follow-up fix, see
        # `_open_unit_branch`) is `sdlc_dir` itself, unchanged from `check()`'s own parameter --
        # the same value `_cfg(sdlc_dir)` above already reads `.sdlc/config.json` relative to.
        stray = _stray_commits_after_merge(gh_disc, base.parent, run, sdlc_dir)
        if stray:
            out.append(_chk(
                "checked-out branch has no stray commits past its own PR's merge/close", False, stray))

        # #2593: every open unit's own landing PR (feature/<unit> -> base), not just the one the
        # current checkout happens to be on -- a conflicted landing PR runs ZERO CI (GitHub cannot
        # build a merge ref for it), reading identically to "not wired up" without this row.
        landing = _landing_pr_unverifiable_units(sdlc_dir, gh_disc, run)
        out.append(_chk(
            "every open unit's landing PR is CI-verifiable (not conflicted with zero checks)",
            landing is None, landing))

        # #389: NOT gated on backlog_check.enabled (nor on the `project` block above) -- a repo with
        # backlog_check OFF has zero auto-skip happening at all, so this is arguably more useful
        # there (a nudge toward turning it on); a repo with it ON benefits from catching the exact
        # blind spot that would otherwise waste a token on a goal that should have been parked.
        dm = _dependency_marker_scan(gh_disc, bchk, run)
        if dm is not None:
            flagged, scanned, total = dm
            dm_name = f"dependency markers: comments checked against body ({scanned}/{total} open goal(s))"
            dm_fix = ("comment-only dependency marker(s), no body marker, likely silently ignored by "
                      "precheck(): #" + ", #".join(str(n) for n in flagged[:10])
                      + (f" (+{len(flagged) - 10} more)" if len(flagged) > 10 else "")
                      + " -- re-file the dependency via handoff.py, or manually add a `Blocked by #N` "
                      "line to the issue body.")
            if bchk.get("enabled") is not True:
                # C1 (PR #480 review): this check is deliberately NOT gated on backlog_check.enabled
                # (see the comment above) -- but while it's off, precheck() returns "OFF" before ever
                # reaching cross_check(), so the advice above (re-file, or add a body marker) does
                # NOTHING yet: a body marker is ignored exactly as much as a comment-only one is. Say
                # so, or the fix text points at an action that fixes nothing.
                dm_fix += (' Also: backlog_check.enabled is off, so even a body marker won\'t '
                           'currently be honored by precheck() -- set `backlog_check: {"enabled": '
                           'true}` to fix that too.')
            out.append(_chk(dm_name, not flagged, dm_fix))

        # #1205: only a gap while blocked_label is unset -- once set, the picker already honors
        # whatever label the repo names, so a matching label still on some issue is no longer a
        # sign anything is inert and re-checking it would just be noise.
        if not gh_disc.get("blocked_label"):
            bl = _blocked_label_scan(gh_disc, run)
            if bl:
                bl_issues = ", ".join(f"#{n}" for n, _ in bl[:10])
                bl_more = f" (+{len(bl) - 10} more)" if len(bl) > 10 else ""
                bl_labels = ", ".join(sorted({label for _, label in bl}))
                out.append(_chk(
                    "blocked-ish label(s) found but discovery.github.blocked_label is unset",
                    False,
                    f"{bl_issues}{bl_more} carry a label that reads as a \"do not auto-pick\" "
                    f"convention ({bl_labels}), but the label-queue picker only ever excludes "
                    "parked_label -- set discovery.github.blocked_label to the label's exact name "
                    "to make the picker honor it."))

        # #1391 step 5a: the CENSUS row. doctor's own scans query BY LABEL and therefore cannot see
        # an issue whose defect is a MISSING label; the census enumerates the population instead.
        # Read-only, fail-open, and it says so out loud when it could not read everything -- "I
        # found nothing" and "I could not look" must never render the same.
        try:
            _rc = _load_loop_script("reconcile")
            _cen = _rc.census(sdlc_dir, cfg, run=_raising_gh(run))   # #895 2c: an outage must not read as an empty census
        except Exception:
            _cen = None
        if _cen and not _cen.get("skipped"):
            _anom = {b: ns for b, ns in _cen["issues"].items() if b != _rc.CLEAN}
            if _anom:
                _desc = "; ".join(
                    "%d %s (%s%s)" % (len(ns), b, ", ".join("#%s" % n for n in ns[:5]),
                                      "…" if len(ns) > 5 else "")
                    for b, ns in sorted(_anom.items()))
                out.append(_chk(
                    "backlog label state is coherent",
                    False,
                    f"{_desc}. Run `python3 skills/sigma-loop/scripts/reconcile.py census .sdlc` for "
                    "the full list. 'closed-with-state' is inert today but resurrects a false state "
                    "if the issue is reopened; 'zero-label' issues are invisible to the picker "
                    "entirely; 'multi-label' issues are excluded by whichever label wins."))
            elif not _cen.get("complete"):
                # #1579: the reason comes from the census itself now. A query that FAILED and a
                # query that came back at the --limit ceiling both clear `complete` and they do not
                # have the same remedy, so "at least one census query failed" was about to become
                # a false statement of the rule on the truncation path.
                out.append(_chk(
                    "backlog label census could be read in full",
                    False,
                    f"no anomalies were found, but {_rc._incomplete_reason(_cen)} — this is NOT a "
                    "clean bill of health, only an incomplete read."))

        # #1391 step 7: the two blocked labels collided into one string. See
        # `_blocked_label_collision` for why this is dangerous and why doctor can only REPORT it.
        collided = _blocked_label_collision(gh_disc)
        if collided:
            out.append(_chk(
                "blocked_label and goal_blocked_label are DIFFERENT labels",
                False,
                f"both resolve to {collided!r}, but they are two different mechanisms: "
                "discovery.github.blocked_label is a HUMAN-set 'do not auto-pick' hold the loop only "
                "reads (#1205), while goal_blocked_label is machine-managed state the loop writes and "
                "an auto-resume sweep is expected to CLEAR (#1350). Collided, clearing the machine "
                "state silently overrides a deliberate human hold. Rename one of them (and relabel the "
                "affected issues) before enabling any automatic reconciliation on this repo."))

        # #1391 step 7: the orphan class NOTHING else can see -- carries in-progress, carries no
        # primary lifecycle label, so neither the picker's query nor the multi-label invariant can
        # ever return it.
        orphans = _orphan_in_progress_scan(gh_disc, cfg, run)
        if orphans:
            shown = ", ".join(f"#{n}" for n in orphans[:10])
            more = f" (+{len(orphans) - 10} more)" if len(orphans) > 10 else ""
            out.append(_chk(
                "no issue carries sdlc:in-progress without a lifecycle label",
                False,
                f"{shown}{more} carry the in-progress label but NO primary lifecycle label, so they "
                "are invisible to the picker (which queries by the goal label) AND to the "
                "multi-label invariant (which only flags issues carrying more than one). Nothing in "
                "the loop can find or resume them. Re-add the label that reflects their real state, "
                "or close them."))

        # #1392: blockers nothing can pick -- see `_unreachable_blocker_scan` for why
        # `sdlc:blocking` is a tie-break rather than a queue, and why this is report-only.
        unreachable = _unreachable_blocker_scan(gh_disc, run)
        if unreachable:
            shown = ", ".join(f"#{n}" for n in unreachable[:10])
            more = f" (+{len(unreachable) - 10} more)" if len(unreachable) > 10 else ""
            out.append(_chk(
                "every sdlc:blocking issue is one the loop can actually pick",
                False,
                f"{shown}{more} carry sdlc:blocking but not sdlc:goal. Other work is blocked on "
                "them, yet no queue can serve them: sdlc:blocking only ever re-ranks issues that "
                "are ALREADY eligible, and the auto-unpark sweep will not resume what they block "
                "until they CLOSE -- so this is a deadlock, not a delay. Run `/sigma-promote` to see "
                "them in its `deadlocked` bucket and approve the ones that should be worked."))

        # #1354: the single-primary-state-label invariant (design doc §5, corrected per §10 finding
        # 2 -- NOT "no issue has more than one sdlc:* label ever": sdlc:in-progress is an
        # orthogonal, additive marker, excluded from this check entirely). Informational only, same
        # "warn, never fail" convention every other row in this function already follows.
        msl = _multi_state_label_scan(gh_disc, cfg, run)
        if msl:
            msl_desc = ", ".join(f"#{n} ({'/'.join(labels)})" for n, labels in msl[:10])
            msl_more = f" (+{len(msl) - 10} more)" if len(msl) > 10 else ""
            out.append(_chk(
                "no issue carries more than one primary sdlc:* state label",
                False,
                f"{msl_desc}{msl_more} carry more than one of "
                "{sdlc:goal, sdlc:parked, sdlc:needs-confirmation} at once "
                "(sdlc:in-progress, sdlc:blocked, sdlc:needs-label and sdlc:needs-unit are "
                "excluded -- they are orthogonal, additive OVERLAYS that ride alongside sdlc:goal, "
                "not states of their own) -- remove all "
                "but the one that reflects the issue's real current state."))

    if kg.get("enabled") is True:
        builder = kg.get("builder", "graphify")
        version_output = run([builder, "--version"])
        ok = bool(version_output)
        fix = "run: pip install graphifyy" if builder == "graphify" else f"install the '{builder}' graph builder"
        out.append(_chk(f"{builder} installed", ok, fix))
        if builder == "graphify":
            mismatch = _GRAPHIFY_SKILL_PACKAGE_MISMATCH.search(str(version_output))
            if mismatch:
                skill_version, package_version = mismatch.groups()
                out.append(_chk(
                    "graphify skill/package versions match", False,
                    f"graphify skill {skill_version} differs from package {package_version} — run `graphify install`"))
        # issue #1562. ONLY when auto_refresh is on: a project that builds the graph by hand with
        # /sigma-kg has nothing wrong with it, and flagging a never-built graph there would be a
        # false alarm on every run. With auto_refresh ON, though, "never built" is a real fault --
        # something claims to be maintaining this graph and demonstrably is not.
        #
        # issue #2055: presence alone is a WEAKER tell than AGENTS.md's LIVENESS property asks for --
        # "a component that has DIED must be distinguishable from one with nothing to do. Age is the
        # tell, not error state." A graph built once and then frozen (the trigger silently broken
        # again, the builder failing every run, an expired credential) read OK indefinitely under
        # the presence-only check that shipped in #2054. #1562 was itself a two-week version of
        # exactly this shape: `graph: not built` since 2026-08-24 with nothing surfacing it.
        #
        # Age IS available now: the corpus's own newest document is the natural denominator, because
        # it is what a rebuild would actually consume -- a fixed wall-clock interval would flag a
        # QUIET corpus (nothing new since the graph was built) as stale just because time passed,
        # which is not a fault. So three states, not two: absent (never built), stale (built, but a
        # corpus document is newer than the graph), fresh (built and caught up).
        #
        # The path -- and now the corpus glob -- are kg.py::status()'s / refresh()'s, restated
        # rather than imported, exactly as _enforce_enabled above restates loop.py's rule: doctor is
        # a standalone diagnostic that must keep working when something else in the kit is broken.
        # `refresh()` treats every `.md` anywhere under the corpus (including gaps.md) as part of
        # what a rebuild consumes (`corpus.rglob("*.md")` is its own emptiness check) -- mirrored
        # here so "the newest document in the corpus" means the same thing in both places. Keep the
        # three in lockstep.
        if kg.get("auto_refresh") is True:
            graph_path = base.resolve().parent / f"{builder}-out" / "graph.json"
            # #2704: `kg.py refresh` records its last real outcome (the builder's own words) in
            # `state/kg-refresh.json`; the row quotes that instead of guessing at the cause. Path
            # restated, not imported, for the same standalone-diagnostic reason as the glob above.
            attempt = _kg_last_attempt(base / "state" / "kg-refresh.json")
            if not graph_path.exists():
                out.append(_chk(
                    "knowledge graph auto-refresh is working", False,
                    f"auto_refresh is on but no graph has ever been built (absent) at "
                    f"{builder}-out/graph.json — run /sigma-kg once by hand and read what the "
                    f"builder reports; last auto-refresh attempt on this machine: {attempt}"))
            else:
                corpus = base / "knowledge"
                newest_doc = max((p.stat().st_mtime for p in corpus.rglob("*.md")), default=None) \
                    if corpus.exists() else None
                stale = newest_doc is not None and newest_doc > graph_path.stat().st_mtime
                out.append(_chk(
                    "knowledge graph auto-refresh is working", not stale,
                    f"the graph at {builder}-out/graph.json is STALE — the corpus has a document "
                    "newer than the graph itself, so auto-refresh has not kept up (a broken "
                    "trigger, a builder failing every run, and an expired credential all look "
                    "like this from the outside) — run /sigma-kg once by hand and read what the "
                    f"builder reports; last auto-refresh attempt on this machine: {attempt}"))

    ns = base / "context" / "north-star.md"
    if ns.exists():
        text = ns.read_text(encoding="utf-8")
        # issue #1484: TWO failure modes, not one. The original check asked only "does any scaffold
        # placeholder survive?" -- correct for a file /sigma-init scaffolded and someone then edited,
        # but vacuous for a HAND-WRITTEN north-star that never had placeholders: with nothing to
        # find, every tier read as complete. This repo's own north-star says "RULES-ONLY STUB. It
        # carries no vision, strategy or architecture section yet" in its second paragraph, has zero
        # of the four tier headings, and doctor reported OK on it.
        # A missing HEADING is checked first because it is the more fundamental absence -- a tier
        # with no heading has no placeholder to still be carrying either, so testing placeholders
        # first would report the wrong reason for the same file.
        # #358 ("every tier must clear") and #445 ("every placeholder WITHIN a tier must clear")
        # are both preserved: this only ADDS the heading requirement ahead of them.
        missing_heading = next(
            (tier for tier, _ in _NORTH_STAR_TIERS
             if not re.search(r"^#{1,6}\s+%s\b" % re.escape(tier), text, re.M | re.I)), None)
        placeholdered = next((tier for tier, placeholders in _NORTH_STAR_TIERS
                              if any(ph in text for ph in placeholders)), None)
        unfilled = missing_heading or placeholdered
        reason = ("tier has no section at all" if missing_heading
                  else "tier still has placeholder text")
        out.append(_chk("north-star filled", unfilled is None,
                        f"{unfilled} {reason} — run /sigma-vision to fill the tiers. Until Strategy "
                        "states real priorities/bets, /sigma-align has nothing to measure drift "
                        "against, so it renders no verdict and any alignment-drift view built on it "
                        "stays permanently dark"))

    # The ledger is switched on in config but the ops branch has to be created + pushed once per
    # clone; before that a teammate's `init` finds nothing to fetch. Flag it as a real setup gap with
    # the one command that fixes it — `/sigma-ledger` runs `sync.py bootstrap` (create + seed + push).
    if _block(cfg, "ledger").get("enabled") is True:
        out.append(_chk("team ledger initialized", (base / "ledger" / ".git").exists(),
                        "run /sigma-ledger — one command creates the ops branch, seeds your file + TEAM.md, and pushes"))

    # #2698: the same shape one channel over. Knowledge-graph analysis notes share an ops branch
    # only once `.sdlc/knowledge/analysis` is a worktree; until then `loop.py record` says so on
    # stderr at every goal end and the notes stay local. Both flags strict `is True`, mirroring
    # `sync.knowledge_enabled` — the row must never appear for a config that does not push.
    kgb = _block(cfg, "knowledge_graph")
    if kgb.get("enabled") is True and _block(kgb, "sync").get("enabled") is True:
        out.append(_chk("knowledge notes sync initialized",
                        (base / "knowledge" / "analysis" / ".git").exists(),
                        "run `sync.py bootstrap .sdlc --channel knowledge` once in this clone — creates the "
                        "ops branch, carries your local notes in, and pushes"))

    # #1456: a hand-off resolves its recipient from the repo's own CODEOWNERS, or from an explicit
    # `ledger.owners` map in config (owners.py:103, "An explicit ledger.owners map in config always
    # wins"). With NEITHER present, owner_of() returns None for every area, handoff.py records the
    # hand-off "unaddressed" with no recipient, and a reader that builds a hand-off graph or an
    # ownership-concentration view discards it (it needs a recipient). So the hand-off is written,
    # costs the work of filing an issue, and is invisible to every view that exists to read it.
    # handoff.py DOES warn at that moment ("no owner for area X -- recording unaddressed"), but
    # that is one stderr line during an autonomous run, long after the roster could have been
    # written; measured on this repo, one hand-off was ever opened and it landed unaddressed.
    # This is exactly the "your setup cannot produce this data" class doctor exists to surface.
    # Deliberately checks only that SOME roster exists, not that a particular area resolves: doctor
    # is standalone by convention (no cross-skill import of owners.py), and "no roster at all" is
    # the actual gap -- a partial roster is a judgement call about coverage, not a setup error.
    if _block(cfg, "ledger").get("enabled") is True:
        _owner_paths = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")   # owners.CODEOWNERS_PATHS
        _root = base.parent
        _has_roster = (any((_root / rel).exists() for rel in _owner_paths)
                       or bool(_block(cfg, "ledger").get("owners")))
        out.append(_chk(
            "hand-off owner roster configured", _has_roster,
            "no CODEOWNERS and no ledger.owners -- every hand-off resolves to '(unowned)' and "
            "is recorded with no recipient, which a hand-off graph or ownership-concentration view "
            "discards, so it stays permanently empty. Add .github/CODEOWNERS, "
            "or map your areas to handles under ledger.owners in .sdlc/config.json"))

    # #1321: enabled in config is not the same as USABLE — nothing acts on a ledger mention until
    # one of the two adapters (Desktop scheduled task, or CLI/Channels' channel_webhook_url) is
    # actually set up. Warn, never fail (same posture as every other row in this function) — a
    # half-configured feature is a setup gap to flag, not a hard blocker.
    if _block(_block(cfg, "ledger"), "autowatch").get("enabled") is True:
        out.append(_chk(
            "autowatch adapter wired up",
            _autowatch_adapter_wired(cfg, scheduled_tasks_dir),
            "ledger.autowatch is enabled in config but no adapter is set up yet — run the "
            "one-time setup for whichever surface you use (a Desktop scheduled task running "
            "autowatch.py tick, or set ledger.autowatch.channel_webhook_url for the CLI/Channels "
            "adapter) — otherwise a flagged mention/assignment/blocker is detected but nothing "
            "ever acts on it."))

    # #2339 (Component F, design #2329): mirrors the autowatch-adapter-wired row directly
    # above -- "configured" (enabled, a channel_id, both token envs actually set) alone is not
    # "actually running". A stale or absent heartbeat reads as "configured but not running", never
    # as an indistinguishable OK (AGENTS.md LIVENESS: "a component that has DIED must be
    # distinguishable from one with nothing to do").
    if _block(cfg, "slack_commands").get("enabled") is True:
        sc_state = _slack_commands_listener_state(base, cfg)
        out.append(_chk(
            "slack-commands listener wired up",
            sc_state.startswith("ON —"),
            "slack_commands is enabled but the listener isn't confirmed running (%s) — start it: "
            "python3 skills/sigma-loop/scripts/slack_commands_listen.py .sdlc (see "
            "SLACK_COMMANDS.md)" % sc_state))

    # The permanent-refusal trap: verify.enforce on with no command refuses EVERY `done` forever, and
    # it looks like a working gate, not a misconfig. Flag it (a per-goal `verify_command` also satisfies).
    verify = _block(cfg, "verify")
    if _enforce_enabled(verify):
        out.append(_chk("verify command present (enforce is on)",
                        bool(verify.get("command")) or _any_goal_verify_command(base),
                        "verify.enforce is on but no verify.command (and no goal sets verify_command) — "
                        "every `done` is refused. Fix: " + _python_command() + " <sigma>/skills/"
                        "sigma-init/scripts/verify_detect.py detect . lists candidates, then "
                        "`... confirm .sdlc <n> <id>` sets candidate n if it still has that id (it "
                        "re-reads the repo and refuses if it changed; nothing "
                        "pasted reaches a shell), or put your command in verify.command; or "
                        "`... decline .sdlc` to turn enforce off."))

    # #615: a configured verify command only runs once THIS checkout granted the git-local trust (#422);
    # git config is not cloned, so every migrated repo and every teammate's new clone starts without it
    # and its first `loop.py verify` is refused (with enforce on, every goal stalls). Command text is
    # repository input: it is never put in the fix line.
    if verify.get("command"):
        project = pathlib.Path(os.path.abspath(sdlc_dir)).parent
        trusted = _verify_trusted(project)
        if _in_git_worktree(project):
            gesture = ("inspect the verify command in .sdlc/config.json, then run once in this checkout: "
                       "git -C <project> config --local sigma.allowRepositoryShellCommands true "
                       "(git-local, so it is not committed or cloned; every checkout does this once). "
                       "Until then `loop.py verify` refuses the command.")
        else:
            gesture = ("verify.command is set but this is not a Git worktree, so it can never be trusted "
                       "and `loop.py verify` refuses it. Run Sigma inside the repository.")
        out.append(_chk("verify command trusted in this checkout", trusted, gesture))

    # A backlog cross-check whose park_threshold sits BELOW its candidate threshold parks EVERYTHING it
    # finds — the opposite of "confident hits only". Flag it (only when the feature is actually on).
    if bchk.get("enabled") is True:
        dup, park = bchk.get("dup_threshold", 0.72), bchk.get("park_threshold", 0.80)
        try:
            sane = float(park) >= float(dup)
        except (TypeError, ValueError):
            sane = True                 # a non-numeric threshold falls back to the default at runtime
        out.append(_chk("backlog cross-check thresholds sane",
                        sane,
                        "backlog_check.park_threshold (%r) < dup_threshold (%r): every candidate becomes "
                        "a confident PARK. Set park_threshold >= dup_threshold." % (park, dup)))
        # The dense/embedding layer switched on but with no embedder command silently runs lexical-only.
        embed = _block(bchk, "embed")
        if embed.get("enabled") is True:
            out.append(_chk("backlog cross-check embedder configured",
                            bool((embed.get("command") or "").strip()),
                            "backlog_check.embed is on but embed.command is empty — the dense layer "
                            "silently falls back to lexical-only. Set embed.command (an embedder that "
                            "reads text on stdin and prints a JSON vector), or turn embed.enabled off."))

    # With work.enabled, verify runs in a FRESH worktree that has none of your installed deps — a
    # relative interpreter path (.venv/bin/python3, node_modules/.bin) fails exit=127 on the first
    # real per-goal run. Flag it before it bites.
    vcmd = verify.get("command") or ""
    if _block(cfg, "work").get("enabled") is True and vcmd:
        out.append(_chk("verify.command resolves in the goal worktree",
                        not _WORKTREE_DEP.search(vcmd),
                        "verify.command has a RELATIVE .venv/venv/node_modules path — but work.enabled "
                        "runs it in a fresh worktree with NONE of your installed deps (fails exit=127). "
                        "Use an absolute interpreter path, a venv activated on PATH, or a wrapper script."))

    # #255 LIVENESS: goals `record review` left awaiting a merge. Local reads only (the work
    # records), so it is not gated by `cheap_only`; silent (no row) when work is off.
    if _block(cfg, "work").get("enabled") is True:
        out.append(_awaiting_merge_row(sdlc_dir))

    # #1555: BEFORE the loop ever commits. `work.commit()` stages with `git add -A`, which honours
    # .gitignore and nothing else, so "is this repo's `.env` ignored?" decides whether an unattended
    # run pushes a credential or merely refuses. Ungated on work.enabled on purpose: an unignored
    # credential is a hazard for the human's own `git add -A` too, and the row costs one git call.
    exposed = _secret_file_coverage(base.parent, cfg, run)
    if exposed is not None:
        shown = ", ".join(exposed[:5]) + (f" (+{len(exposed) - 5} more)" if len(exposed) > 5 else "")
        out.append(_chk(_SECRET_COVERAGE_ROW, not exposed,
                        f"secret-shaped file(s) NOT ignored here: {shown} -- `work.commit()` stages "
                        "with `git add -A`, so these are one unattended goal away from a PR (it "
                        "would refuse, and the run would stop). Add each to .gitignore (or "
                        ".git/info/exclude); if one is a deliberate fixture, name its exact path in "
                        'work.allow_secret_paths in .sdlc/config.json.'))


    # companions (optional): superpowers + code-review power phases 1/3/5/6 when present; Sigma's
    # portable sigma-* executors are the absent-safe fallback everywhere else — absent is never a failure.
    # Skipped under cheap_only: spawns the selected host's plugin CLI.
    if not cheap_only and (cfg.get("companions") or "auto") != "off":
        if plugin_host == "codex":
            for comp in ("superpowers", "code-review"):
                if codex_plugins is None:
                    out.append(_chk(f"{comp}: unverified", False,
                                    "Codex plugin inventory unavailable; run `codex plugin list --json`"))
                else:
                    here = _codex_enabled(codex_plugins, comp) is not None
                    out.append(_chk(f"{comp}: {'present' if here else 'absent — portable executor used'}",
                                    True, ""))
        else:
            plugins = run(["claude", "plugin", "list"]) or ""
            for comp in ("superpowers", "code-review"):
                here = comp in plugins
                out.append(_chk(f"{comp}: {'present' if here else 'absent — portable executor used'}", True, ""))

    # Awareness nudge (F10.5-5/#378): auto-update is off by default for a non-Anthropic marketplace
    # like this one, so a stale install can otherwise persist silently forever. Only reported when
    # BOTH versions actually resolve — an unreachable network or an unrecognized CLI output must
    # never show as either a false alarm or a false all-clear.
    # Skipped under cheap_only: the host plugin list + an authenticated `gh api` fetch of
    # the marketplace repo (measured 3.97s cold) is the single most expensive thing in this
    # function, and a version nudge is not a first-run setup gap — see this function's own
    # docstring.
    if not cheap_only:
        installed, latest = _plugin_versions(run, plugin_host, codex_plugins)
        if installed is not None and latest is not None:
            inst_s, latest_s = ".".join(map(str, installed)), ".".join(map(str, latest))
            fix = (f"{latest_s} is available (you're on {inst_s}) — run: claude plugin update {_PLUGIN}@{_PLUGIN}"
                   if plugin_host == "claude" else
                   f"{latest_s} is available (you're on {inst_s}) — refresh the Sigma Codex "
                   "marketplace and reinstall or update the Codex plugin")
            out.append(_chk(f"sigma up to date (installed {inst_s})",
                            installed >= latest,
                            fix))

    # #1604: the OTHER staleness, and the one that is silent by construction. The nudge above asks
    # "is this install behind the marketplace?"; this asks "which scopes is sigma installed
    # under at all, and is any of them below the floor AGENTS.md refuses to start the loop below?"
    # A project-scope entry shadows user scope for its own path, so `--scope user` can report
    # success while the one directory the loop is normally started in sits on an old version.
    # Omitted entirely — never green — when it cannot see; see `_install_scope_row`.

    # NOT gated by `cheap_only`, and that is deliberate against this function's own criteria:
    # it spawns no CLI and touches no network (a local JSON read plus the plugin's shipped
    # AGENTS.md), and a below-floor install is a SETUP GAP, not an awareness nudge — which is
    # precisely what the wizard exists to surface before a first run.
    if plugin_host == "codex":
        scope_row = _codex_install_floor_row(codex_plugins, agents_md) if not cheap_only else None
    else:
        scope_row = _install_scope_row(base.parent, installed_plugins_path, agents_md)
    if scope_row is not None:
        out.append(scope_row)
    # #524: an install recorded under the pre-launch plugin id stopped updating when the plugin was
    # renamed. Claude: a local JSON read, like the scope row. Codex: the list `check()` already fetched
    # (a subprocess), so the cheap pass skips it exactly as it skips the Codex floor row.
    old_row = (_codex_old_install_row(codex_plugins) if not cheap_only else None) if plugin_host == "codex" \
        else _claude_old_install_row(installed_plugins_path)
    if old_row is not None:
        out.append(old_row)
    return out


#: A cited path worth checking: backticked, has a separator, and is concrete. Anything with a glob or
#: a <placeholder> is a pattern, not a reference — flagging those would cry wolf, and a check nobody
#: trusts gets ignored along with the true positives.
_CITED = re.compile(r"`([^`\s]*/[^`\s]*)`")
_MDLINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_ABSTRACT = re.compile(r"[*?<>{}\[\]]|NNNN|YYYY|\.\.\.")
#: A single-segment leading-slash token -- the exact shape of a slash-command mention (`/sigma-doctor`),
#: never a real file citation (those name a deeper path, or omit the leading slash for a repo-root
#: entry like `goals/`). Anchored so a citation that merely STARTS with a command name but names
#: something deeper (`/sigma-doctor/scripts/doctor.py`) is excluded and still resolves normally.
_COMMAND_REF = re.compile(r"^/([^/]+)$")
#: A RELATIVE .venv/venv/node_modules path in verify.command — a worktree footgun once work.enabled.
#: An explicit `./` or `../` (repeatable) relative prefix is consumed before the dep name so
#: `./node_modules/…` and `../venv/…` are flagged too. The lookbehind still excludes a preceding
#: `/` or `.` so an ABSOLUTE path (/x/.venv/…) is never flagged.
_WORKTREE_DEP = re.compile(r"(?<![\w./])(?:\.\.?/)*(?:\.venv|venv|node_modules)/")


def _standing_docs(base):
    """The docs that describe the project and therefore rot when the project moves: the north-star
    tiers and project.md. Goals and plans are transient by design — not scanned."""
    docs = [base / "project.md"]
    docs += sorted((base / "context").glob("*.md")) if (base / "context").is_dir() else []
    return [d for d in docs if d.is_file()]


def _under(base, ref, root=None):
    """`ref` resolved under `base` — or None when it lands OUTSIDE `root` (default: `base` itself).
    None means "cannot be a reference to something in this repo", and every caller treats it the
    same way it treats a path that does not exist: flag it.

    A leading `/` means repo-root-relative, never the machine's filesystem root. `pathlib`'s `/`
    operator DISCARDS its left operand the moment the right side is absolute, so
    `repo_root / "/docs/architecture.md"` quietly became `/docs/architecture.md` and the reference
    was checked against the OS instead of the repo (#545). That broke the check both ways at once:
    the ordinary repo-root-relative citation form was reported STALE while the file sat right there,
    and any absolute path that happened to exist on the box running doctor passed silently.

    Stripping that slash was not enough on its own, because nothing stopped the reference climbing
    back out afterwards: `/../outside.md` became `<repo>/../outside.md`, a real file, and read as
    resolvable. #545 therefore TRADED an accidental catch for a real miss — before it, the leading
    slash sent that same reference to the OS root where nothing was, so it happened to be flagged.
    Deeper `../` chains escaped in both eras. Hence the containment check here rather than a bare
    join (#577). `_ABSTRACT` above filters `...` but not `..`, so these reach this function rather
    than being dismissed as patterns.

    Two details the obvious implementation gets wrong:

    * the boundary is `root`, NOT `base`. A north-star linking up to `../project.md` leaves the
      document's own directory while staying well inside the repo, and that is ordinary; only
      leaving the REPO is the problem.
    * THE BOUNDARY AND THE JOIN BASE MUST SHARE AN ORIGIN. That is the whole invariant, and it is
      what a lexical normalizer cannot deliver. `abspath` is lexical + cwd, so it reconciles neither
      of the two ways the sides can disagree: a relative `repo_root` resolves against the PROCESS
      CWD while the join base comes from `sdlc_dir`, and an alias path stays an alias while its
      partner is real. Both are reachable through `hygiene()`'s one-arg form (the `check` verb):
      running it from an unrelated directory, or on any checkout reached through a symlink — macOS
      `/tmp` is one, so every `/tmp` checkout — then flagged perfectly ordinary in-repo links.
      `realpath` collapses `..` AND symlinks on both sides, so the comparison is between two paths
      in the same form. It costs one stat per side: this function DOES touch the filesystem, which
      is the price of a verdict that does not depend on where the process happens to be standing.
      (`hygiene` also defaults the boundary from `sdlc_dir` rather than from cwd — same invariant,
      one layer up.)
      Resolving THROUGH symlinks has a consequence worth stating outright: repo content that
      physically lives elsewhere — a tracked symlink into a vendored tree, a `.sdlc` symlinked out
      of the working copy — resolves to its real location and is therefore reported. That is the
      policy, not an oversight: this check's contract is claims-about-THIS-repo, and a reference
      whose target sits outside the tree is not one this check can stand behind. Such a path
      usually EXISTS, so it is reported in its own words (`_OUTSIDE`) and never as "no such file" —
      telling someone a file they can open is missing is the crying wolf this check exists to avoid.

    A genuinely OS-absolute reference (`/usr/local/bin/foo`) is read as repo-relative and reported.
    Deliberate: a standing doc's cited paths are claims about THIS repo, and a check whose verdict
    depends on what else is installed on the runner is worse than one that is occasionally too
    strict — the same crying-wolf reasoning `_CITED` above is built on."""
    resolved = pathlib.Path(os.path.realpath(os.path.join(str(base), str(ref).lstrip("/"))))
    boundary = pathlib.Path(os.path.realpath(str(base if root is None else root)))
    return resolved if resolved.is_relative_to(boundary) else None


#: The two ways a reference fails, kept apart because they are different problems with different
#: fixes: one is repointed at the moved file, the other is a decision about what belongs in the repo.
#: `_OUTSIDE` deliberately does not say "missing" — see `_under`'s symlink note for why such a path
#: usually exists.
_MISSING = "no such file"
_OUTSIDE = "resolves outside the repo"


def _unresolved_reason(base, ref, root=None):
    """None when `ref` resolves fine, else WHY it did not."""
    target = _under(base, ref, root)
    if target is None:
        return _OUTSIDE
    return None if target.exists() else _MISSING


def _known_commands():
    """The shipped skill/slash-command names -- `/sigma-doctor`, `/sigma-loop`, etc -- read from the
    real `skills/` directory listing (#1210) rather than a hardcoded list that goes stale the next
    time a skill is added, renamed, or removed. `_HERE.parent.parent` is this plugin's OWN skills
    root regardless of which project's `.sdlc` is being audited -- the exact same layout fact
    `_load_loop_script` above relies on (skills/sigma-doctor/scripts/doctor.py -> two `.parent`s up
    -> skills/). A slash command is a property of the INSTALLED plugin, not of whatever repo_root a
    caller happens to be checking, so this deliberately does not vary with `_stale_paths`' own
    `repo_root` argument.

    Fails open to an empty set on any read error: nothing is then treated as a known command, every
    reference falls back through to the ordinary path-resolution check, and the worst case is the
    pre-#1210 behavior (a real command mention flagged) rather than a crash or a silent
    over-suppression."""
    try:
        return {p.name for p in _HERE.parent.parent.iterdir() if p.is_dir()}
    except OSError:
        return set()


def _stale_paths(text, repo_root):
    """[(ref, reason)] for every cited path that does not resolve — the reason travels with the ref
    so the fix line can name each one accurately rather than labelling the whole batch (#600).

    A backticked token containing `/` also matches a slash-command mention (`/sigma-doctor`), which
    can never resolve as a repo-root-relative file by design -- that false STALE is exactly what
    trained operators to ignore this check on their own best-written docs (#1210). `_COMMAND_REF`
    keeps the exemption narrow: only an EXACT single-segment `/<name>` where `<name>` is a real
    shipped command is skipped; `/sigma-doctor/scripts/doctor.py` and a bogus `/not-a-command` both
    still run the ordinary resolution check below."""
    out = []
    commands = _known_commands()
    for ref in _CITED.findall(text):
        if _ABSTRACT.search(ref) or "://" in ref or ref.startswith(("-", "$")):
            continue
        stripped = ref.rstrip("/")
        if (m := _COMMAND_REF.match(stripped)) and m.group(1) in commands:
            continue
        if reason := _unresolved_reason(repo_root, stripped):
            out.append((ref, reason))
    return out


def _dangling_links(text, doc_dir, repo_root):
    """A markdown target starting with `/` follows the same repo-root-relative convention as a cited
    path (that is what the form means in a repo's own docs); everything else stays relative to the
    document doing the linking. Either way the repo root is the containment boundary, so an ordinary
    `../` hop between docs still resolves while one that leaves the repo does not (#577)."""
    out = []
    for target in _MDLINK.findall(text):
        target = target.split()[0].split("#")[0].strip()      # drop a title and any anchor
        if not target or "://" in target or target.startswith(("#", "mailto:")):
            continue
        base = repo_root if target.startswith("/") else doc_dir
        if _ABSTRACT.search(target):
            out.append((target, _MISSING))                    # a pattern, not a real target
        elif reason := _unresolved_reason(base, target, repo_root):
            out.append((target, reason))
    return out


def hygiene(sdlc_dir=".sdlc", repo_root=None):
    """Content-rot over the standing docs: references that no longer resolve. Read-only, binary, and
    mechanical — the half of context maintenance a script can settle. The judgment half (demoting a
    rule that CI now enforces, archiving a superseded plan) belongs to `sigma-retro`, because it
    changes files and needs approval. Returns [] when there are no standing docs to scan, so a
    drop-in project sees nothing new.

    An omitted `repo_root` is derived from `sdlc_dir` — the plain reading of `check <path>/.sdlc`,
    and the same shared-origin invariant `_under` documents. It used to default to `"."`, which
    anchored the containment boundary to the PROCESS CWD while the docs were found relative to
    `sdlc_dir`: call the one-arg form from anywhere but the repo (as the `check` verb does) and
    ordinary in-repo links were flagged."""
    base = pathlib.Path(sdlc_dir)
    root = pathlib.Path(repo_root) if repo_root is not None else base.parent
    docs = _standing_docs(base)
    if not docs:
        return []
    stale, dangling = {}, {}
    for doc in docs:
        try:
            text = doc.read_text(encoding="utf-8")
        except OSError:                                        # fail-open: unreadable != rotten
            continue
        if bad := _stale_paths(text, root):
            stale[doc.name] = bad
        if bad := _dangling_links(text, doc.parent, root):
            dangling[doc.name] = bad
    return [
        _chk("standing docs: cited paths resolve", not stale, _detail(stale, _STALE_PATHS_TRAILER)),
        _chk("standing docs: links resolve", not dangling, _detail(dangling)),
    ]


#: #1210 AC4 — decision for the `goals/` case: `_stale_paths` anchors every cited path at the repo
#: root (see `_under`'s docstring), same as `_dangling_links` does for a LEADING-SLASH link target,
#: but unlike `_dangling_links`' OWN default for a relative one (the citing document's directory).
#: Flipping `_stale_paths` to match `_dangling_links` throughout was rejected: every other citation
#: in this codebase already relies on the repo-root reading (`src/live.py` in this file's own
#: tests, `skills/sigma-doctor/scripts/doctor.py` throughout this module's comments), so re-anchoring
#: would trade one false STALE for a much larger set of them. This trailer is what implements the
#: OTHER branch AC4 offers instead: the failure message states outright where a cited path is
#: resolved from, so a `.sdlc/project.md` that means `.sdlc/goals/` and writes `goals/` is still
#: flagged, but the operator now learns why without reading `_under`'s docstring.
_STALE_PATHS_TRAILER = ("Cited paths are resolved from the REPO ROOT, not the citing document's "
                         "own directory — update the reference (or drop it).")


def _detail(found, trailer="Update the reference or drop it."):
    """One fix line naming the offenders, each with its OWN reason. It used to take a single label
    for the whole batch, which meant a reference rejected for living outside the repo was announced
    as "no such file" — about a path that usually exists (#600). Capped — a wall of paths is a
    report nobody reads; the first few are enough to start, and re-running shows the rest.

    `trailer` lets a caller name ITS OWN resolution convention (#1210 AC4) rather than the two
    checks sharing one generic closing line — `_stale_paths` and `_dangling_links` resolve a
    relative reference against different bases (repo root vs. the citing document's own
    directory), so a trailer that is accurate for one is misleading for the other."""
    parts = []
    for doc, refs in sorted(found.items()):
        named = ", ".join(f"{ref} ({reason})" for ref, reason in refs[:3])
        parts.append(f"{doc}: {named}" + (f" (+{len(refs) - 3} more)" if len(refs) > 3 else ""))
    return f"unresolved — {'; '.join(parts)}. {trailer}"


def _ledger_entries(base):
    """Count committed ledger lines. Read-only and fail-open — the dashboard never breaks on a
    half-written file."""
    return _count_jsonl_lines(pathlib.Path(base) / "ledger" / "entries")


def _count_jsonl_lines(directory):
    """Non-blank lines across a directory's *.jsonl, or 0 if it isn't there. `errors="replace"`
    is load-bearing, not defensive dressing: a process killed mid-append truncates a multi-byte
    UTF-8 sequence, and the resulting UnicodeDecodeError is a ValueError, NOT an OSError, so it
    would sail past the catch and take the WHOLE dashboard down — every other row with it — on
    the next run. A half-written file is exactly what this is here to survive."""
    total = 0
    if not directory.exists():
        return 0
    for path in sorted(directory.glob("*.jsonl")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        total += sum(1 for line in text.splitlines() if line.strip())
    return total


def _python_command():
    """`python3`, else `python`, else the Windows `py` launcher -- whichever is on PATH (a lookup,
    not an execution), so the printed fix runs on an install without `python3` (#228)."""
    import shutil
    for name in ("python3", "python", "py"):
        if shutil.which(name):
            return name
    return "python3"


def _any_goal_verify_command(base):
    """True if any local goal declares its own `verify_command` in frontmatter — that satisfies
    verify.enforce even when the config command is empty, so it isn't the refusal trap."""
    goals = pathlib.Path(base) / "goals"
    if not goals.is_dir():
        return False
    for path in goals.glob("*.md"):
        try:
            # #228: a NON-EMPTY value -- `verify_command: ""` declares nothing, and loop.py's
            # verify reads it as absent (NO-COMMAND), so it must not satisfy this row either.
            if re.search(r'^verify_command:[ \t]*(?!["\']?[ \t]*["\']?[ \t]*$)\S',
                         path.read_text(encoding="utf-8", errors="replace"), re.MULTILINE):
                return True
        except OSError:
            continue
    return False


def _ignore_mechanism(repo_root):
    """Which git mechanism ignores the machine-written `.sdlc/` runtime dirs — the shared `.gitignore`,
    the local `.git/info/exclude`, or neither. Reported so an adopter catches a mismatch with intent
    (a local-only experiment shouldn't be editing the tracked .gitignore)."""
    root = pathlib.Path(repo_root)

    def covers(path):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        for raw in text.splitlines():
            line = raw.strip().strip("/")
            if line and not line.startswith("#") and (line == ".sdlc" or line.startswith(".sdlc/")):
                return True
        return False

    if covers(root / ".gitignore"):
        return "tracked .gitignore"
    if covers(root / ".git" / "info" / "exclude"):
        return "local .git/info/exclude (untracked — nothing the team sees)"
    return "NOT ignored — runtime dirs may get committed (run /sigma-setup, or setup.py ignore .)"


#: The row name, referenced by the check and by its tests — one spelling, not two.
_SECRET_COVERAGE_ROW = "secret-shaped files are ignored (nothing for `git add -A` to stage)"


def _secret_file_coverage(repo_root, cfg, run):
    """The untracked, UNIGNORED secret-shaped files sitting in this repo right now — the exact set
    `work.commit()`'s `git add -A` would stage and then refuse (#1555). `None` when git could not
    answer, which is NOT the same as "none found" and must not be reported as an all-clear.

    THIS EXISTS SO THE COMMON CASE IS FIXED AT ADOPTION RATHER THAN AT REFUSAL. `commit()`'s guard
    is the last line, and hitting it means an unattended run has already stopped. This one is
    readable before the loop ever commits, from a command an adopter runs anyway.

    IT CROSS-LOADS `work._is_offender` RATHER THAN COPYING THE RULE. A doctor-local copy is the
    hardened-sibling-divergence bug class `_load_loop_script` was carved out for in the first place:
    this would report coverage against one denylist while `commit()` refused against another, and
    every drift between them is either a false all-clear or a warning about something that would
    have committed fine. #1577 moved the call from `_secret_shaped` up one level to the WHOLE
    predicate — denylist, allowlist and the feature-registry exemption together — for exactly that
    reason: reassembling two of its three parts here is still a copy, and the part left out was the
    one that made a unit called `credentials` produce a red row whose first remedy ("add it to
    .gitignore") would have broken that adopter's registry.

    ONLY UNTRACKED-AND-UNIGNORED PATHS ARE REPORTED. `--exclude-standard` applies the repo's own
    ignore rules, so a repo that already did the right thing sees nothing — the row is about
    coverage, and an ignored file IS covered. Already-TRACKED secrets are deliberately out of
    scope: they are in the history already, so the remedy is rotation rather than an ignore rule,
    and listing a repo's test fixtures on a row nobody can clear is how a check earns being
    ignored."""
    try:
        work = _load_loop_script("work")
    except Exception:                        # noqa: BLE001 - a load failure is "could not answer"
        return None
    out = run(["git", "-C", str(repo_root), "ls-files", "--others", "--exclude-standard"])
    if not isinstance(out, str) or getattr(out, "raw", None) is not None:
        return None                          # not a git repo, or git failed -- never a false all-clear
    allowed = work._allowed_secret_paths(cfg)
    return sorted(line for line in out.splitlines() if work._is_offender(line, allowed))


def _auto_unpark_feature_state(cfg):
    """#1351/#1394: `discovery.auto_unpark.mode` — 'on' (**default since #1394**) | 'off'. GitHub discovery mode only,
    matching `sources._auto_unpark`'s own reach exactly (a local goal's "blocked by #N" is a file
    path, never a bare issue number the sweep's regex can match). Read directly from raw config
    here rather than cross-loading `sources.py`, matching this file's own standalone-diagnostic
    convention (see `_enforce_enabled`'s docstring) — an unrecognized/malformed value degrades to
    the DEFAULT here too, same fail-open direction `sources._auto_unpark` itself uses.

    #1394: this default must track `sources._auto_unpark`'s. This function reads raw config with its
    own copy of the rule, so leaving it at "off" would report a feature as off while the loop runs
    it -- config/reality drift, the exact class this branch spent 17 fixes removing."""
    disc = _block(cfg, "discovery")
    if disc.get("source") != "github":
        return "n/a — backlog source is not github"
    mode = _block(disc, "auto_unpark").get("mode")
    return ("ON (default) — sdlc:blocked goals auto-resume once their blockers close, and "
            "sdlc:blocking is kept in sync on the blocker issues they name. A sdlc:PARKED issue is "
            "never resumed automatically — that is a human's decision (#1394)") \
        if mode != "off" else "off — set discovery.auto_unpark.mode to \"on\" to re-enable"


def _blocking_priority_override_feature_state(cfg):
    """#1352/#1394: `discovery.blocking_priority_override` — bool, **default True since #1394**. GitHub discovery mode
    only, matching `sources._blocking_priority_override`'s own reach exactly (a local goal has no
    label state at all). Read directly from raw config here rather than cross-loading `sources.py`,
    matching this file's own standalone-diagnostic convention — only the literal `False` reads as
    OFF, mirroring `sources._blocking_priority_override`, so a typo'd value lands on the default
    here too rather than reporting a state that isn't live."""
    disc = _block(cfg, "discovery")
    if disc.get("source") != "github":
        return "n/a — backlog source is not github"
    return ("ON (default) — an sdlc:goal issue also carrying sdlc:blocking sorts ahead of every "
            "non-blocking issue, regardless of priority tier") \
        if disc.get("blocking_priority_override") is not False \
        else "off — set discovery.blocking_priority_override to true to re-enable"


def _ledger_delivery_state(base, cfg, now=None):
    """#1599: the clause that says whether a bootstrapped ledger is actually REACHING the team, or
    `""` when it demonstrably is. Appended to the row below.

    THE BLIND SPOT THIS CLOSES. The row used to stop at `.sdlc/ledger/.git` existing: past that
    point it printed `ON — N entries` no matter how much of that N had never left the machine, and
    no matter how long the watcher had been dead. #1391 fixed the case where NOTHING was ever
    published; this fixes the far commoner one where publishing started and then quietly stopped.
    Measured in the field: 174 entry files and 189 entries held back over ~2.5 weeks, with this row
    green throughout.

    IT CROSS-LOADS `sync` RATHER THAN COPYING THE RULES, the same argument `_secret_file_coverage`
    makes for `work._secret_shaped`: a doctor-local copy of "what counts as unpublished" or "when is
    a heartbeat stale" would drift from the write path's own copy, and every drift is either a false
    all-clear or a warning about something that would have published fine.

    IT OMITS ITS CLAIM RATHER THAN REPORTING HEALTH WHEN IT CANNOT SEE. A features row cannot omit
    itself the way `_install_scope_row` and `_secret_file_coverage` return `None` — the ledger's own
    state still has to be printed — so the omission is of the CLAIM: an unanswerable git says so
    out loud instead of falling through to the silent, clean-looking branch. `None` from
    `sync.unpublished` is "we did not look", never "we looked and found nothing".

    NOT `cheap_only`-GATED, and structurally cannot be: `cheap_only` is a parameter of `check()`,
    and the wizard's unconditional SessionStart hook calls only `check()` (setup_wizard.py's
    `_doctor_check`). `features()` is reached from `doctor.py`'s own CLI alone — i.e. from a
    `/sigma-doctor` the user typed. Judged against `check()`'s own criteria it would pass anyway:
    two local `git` calls in a directory that already exists, no `claude` CLI, no network, and
    strictly less than the `gh` reads `features()` already makes for `_pick_path_gate_state`."""
    try:
        sync = _load_loop_script("sync")
    except Exception:                       # noqa: BLE001 - a load failure is "could not answer"
        return " — COULD NOT CHECK what has been published (sync.py did not load); not an all-clear"
    try:
        pending = sync.unpublished(base, cfg, now=now)
    except Exception:                       # noqa: BLE001 - a diagnostic never crashes the dashboard
        pending = None
    if pending is None:
        return (" — COULD NOT CHECK what has been published (the ledger worktree did not answer "
                "git); not an all-clear")
    try:
        state, age = sync.watcher_liveness(base, cfg, now=now)
    except Exception:                       # noqa: BLE001 - as above
        state, age = "absent", None
    files, ahead = pending["files"], pending["unpushed_commits"]
    if not files and not ahead:
        if ahead is None:
            # Half-blind, so it says which half. No remote-tracking ref means this clone has never
            # fetched or pushed the ops branch, and reading that as "0 commits behind" would make
            # the never-published case look like the healthiest one on the dashboard.
            return (" — nothing unpublished in the worktree, but COULD NOT COMPARE against %s/%s, "
                    "so what actually reached the remote is unknown"
                    % (sync.remote(cfg), sync.branch(cfg)))
        return ""
    # THE ROW THAT MATTERS, and the watcher is named as the CAUSE rather than as a nag of its own:
    # a watcher that is not running is harmless while there is nothing waiting, so it is reported
    # here, where there is. Same reasoning `_install_scope_row` gives for not flagging dead entries.
    watcher = {
        "live": "the ledger watcher is live (last tick %s) and publishes once per tick"
                % sync._ago(age),
        "stale": "the ledger watcher's last tick was %s — it is not running, so nothing is going to "
                 "publish these (a loop trigger restarts it)" % sync._ago(age),
        "absent": "no ledger watcher has run here (a loop trigger starts one; `sync.py publish "
                  ".sdlc` publishes now)",
    }[state]
    parts = []
    if files:
        oldest = pending["oldest_age_seconds"]
        parts.append("%d entry file%s hold%s writes no other machine can see%s"
                     % (files, "" if files == 1 else "s", "s" if files == 1 else "",
                        "" if oldest is None else " (oldest %s)" % sync._ago(oldest)))
    if ahead:
        parts.append("%d commit%s never pushed" % (ahead, "" if ahead == 1 else "s"))
    return " — BEHIND: %s; %s" % ("; ".join(parts), watcher)


def _ledger_feature_state(base, cfg, now=None):
    """Dashboard line for the ledger. 'enabled' alone isn't 'working' — the ops branch still has to be
    created + pushed once, so an enabled ledger with NOTHING yet (no worktree and no entries) reports
    the gap and its one-command fix instead of a count that would imply it's live. Once it has a
    worktree or any entries, it's in use — show the count.

    #1599: a worktree that exists is still not 'working' either — see `_ledger_delivery_state`,
    which supplies the tail. `now` is DI for that tail's age arithmetic -- the same injectable
    clock seam every age-based row in this file uses, so none of them reads the wall clock
    directly."""
    if _block(cfg, "ledger").get("enabled") is not True:
        return "off (nothing is recorded)"
    base = pathlib.Path(base)
    n = _ledger_entries(base)
    published = (base / "ledger" / ".git").exists()
    if n == 0 and not published:
        return "ON but NOT set up — run /sigma-ledger to create + push the ops branch"
    if not published:
        # #1391 step 7: entry COUNT was masking the thing that actually matters. `.sdlc/ledger` is
        # supposed to be a git worktree on the ops branch; when it is a plain directory,
        # `sync.publish` never runs and NOTHING is shared -- the ledger is the only cross-machine
        # claim arbiter, so an unpublished one silently means there is no cross-machine coordination
        # at all. Measured live on this repo: 263 entries, zero of them ever on the
        # `sdlc-ledger` branch, and this row reported a healthy "ON — 263 entries" the whole time.
        return ("ON but LOCAL-ONLY — %d entr%s written, none published (.sdlc/ledger is a plain "
                "directory, not a git worktree, so sync.publish never runs and no other machine "
                "can see these claims) — run /sigma-ledger" % (n, "y" if n == 1 else "ies"))
    return "ON — %d entr%s in .sdlc/ledger/entries/%s" % (
        n, "y" if n == 1 else "ies", _ledger_delivery_state(base, cfg, now=now))


def _stop_file_state(base):
    """#416. A forgotten daemon stop-file reads as a dead daemon that has nothing to say; surface it
    with its age. Independent of every feature flag -- the file halts the daemon whether or not the
    feature is configured. Facts come from sigma-loop's stopfiles.py (also read by session_start.sh)."""
    try:
        text = _load_loop_script("stopfiles").line(base)
    except Exception:                       # noqa: BLE001 - a load failure is "could not answer"
        return "COULD NOT CHECK stop-files (stopfiles.py did not load); not an all-clear"
    return text or "none"  # a "COULD NOT CHECK ..." answer (unreadable state dir) comes back as text


def _ledger_watcher_state(base, cfg, now=None):
    """What the ledger watcher's own heartbeat says about itself, or why there is nothing to say.
    AGE-BASED, NOT ERROR-BASED -- the shape every daemon-liveness row in this project shares,
    because a daemon that DIED leaves a stale-but-healthy-looking file behind (#1509): zero
    failures, no error, and a timestamp drifting quietly into the past. Read
    against watch_daemon.py's own liveness signal (#1227/#1293's per-tick heartbeat file) rather than a
    status JSON -- watch_daemon.py has no status file, only a bare touched $STATE/watch.heartbeat.

    WHY THIS DIDN'T ALREADY EXIST. _ledger_feature_state (above) only ever checks that the ledger
    worktree was created once (`.git` exists) -- a permanent-once-true fact, not a freshness one.
    Confirmed live in this repo's own .sdlc/ during the session that added this function: the one
    watcher that ever ran had a live pid and had made zero progress for 18+ days (its last real
    tick: 2026-08-05), and _ledger_feature_state reported "published" the entire time -- the
    identical blind spot #1509/#1584 already closed for two other daemons, here on the
    third."""
    ledger_cfg = _block(cfg, "ledger")
    if ledger_cfg.get("enabled") is not True:
        return "off (no shared team ledger)"
    import time as _time

    # After the `off` early return, so a repo without a ledger never pays for this load. Guard
    # shape copied verbatim from `_ledger_delivery_state` 90 lines above -- `_load_loop_script`
    # itself does not catch, every call site does.
    try:
        sync = _load_loop_script("sync")
    except Exception:                       # noqa: BLE001 - a load failure is "could not answer"
        return ("ON, but COULD NOT CHECK the watcher's heartbeat (sync.py did not load, so the "
                "staleness bound is unknown); not an all-clear")
    interval = _block(ledger_cfg, "watch").get("interval_seconds")
    if not isinstance(interval, (int, float)) or isinstance(interval, bool) or interval <= 0:
        interval = 900
    # sync.stale_after_seconds is the one home for this rule (#2490); the interval guards above stay
    # doctor-local on purpose -- they are strictly safer than sync's config reader on a string/bool
    # interval, and converging them is a behaviour change (F-1).
    stale_after = sync.stale_after_seconds(interval)
    state_dir = pathlib.Path(base) / "state"
    try:
        hb_mtime = sync.heartbeat_path(base).stat().st_mtime
    except OSError:
        if not (state_dir / "watch.pid").exists():
            return "ON, but NEVER RUN — no watcher has started yet (a loop trigger starts one)"
        return ("DEAD — a watch.pid exists but has no heartbeat (predates #1293, or crashed "
                "before writing one); a fresh loop trigger replaces it")
    now = now or _time.time()
    age = now - hb_mtime
    if age >= stale_after:
        return ("STALE — last heartbeat %.1fh ago; the watcher is probably dead "
                "(a loop trigger replaces it automatically)" % (age / 3600.0))
    return "ON — last heartbeat %.0f min ago" % (age / 60.0) + _watch_log_size_clause(sync, cfg, state_dir)


def _fmt_size(n):
    """Adaptive units for a byte count: `B` below 1 KiB, `KiB` below 1 MiB, else `MiB` -- so a
    1 KiB test cap never prints as a meaningless `0.0 MiB`."""
    if n < 1024:
        return "%d B" % n
    if n < 1048576:
        return "%.1f KiB" % (n / 1024.0)
    return "%.1f MiB" % (n / 1048576.0)


def _watch_log_size_clause(sync, cfg, state_dir):
    """#2499. The size note appended to the `ON` watcher row ONLY -- `""` unless `state/watch.log`
    is at or above TWICE its effective cap (`sync.watch_log_cap_bytes`, the same function the daemon
    rolls by, so the two never disagree on the number).

    WHY 2 x CAP AND NOT THE CAP. `watch_daemon.rotate_log` runs at the START of a tick, so after
    the tick that crosses the cap a perfectly healthy log sits at cap + one tick's output for a whole
    interval; flagging `size > cap` would fabricate a fault on every healthy watcher once per cycle.
    At >= 2 x cap roughly a cap's worth of bytes accumulated with no roll, which only an old-plugin
    watcher (pre-rotation code), a rotation that keeps failing (D-6 absorbs it silently, by design),
    or one pathological tick can produce -- and the note names all three rather than guessing one.
    NEVER on the NEVER RUN / DEAD / STALE / could-not-check rows: their verdict already says the
    watcher is not running, and a size clause there would bury the finding that matters.
    Wrapped whole: a stat or cap failure here must not change the liveness verdict."""
    try:
        size = (pathlib.Path(state_dir) / "watch.log").stat().st_size
        cap = sync.watch_log_cap_bytes(cfg, state_dir)
        if size < 2 * cap:
            return ""
        return ("; watch.log %s exceeds twice its %s cap (not rotating — an old-plugin watcher, "
                "rotation failing, or one tick out-wrote the cap since the last roll)"
                % (_fmt_size(size), _fmt_size(cap)))
    except Exception:                       # noqa: BLE001 - a size note must never alter the verdict
        return ""


def _default_scheduled_tasks_dir():
    """`~/.claude/scheduled-tasks/`, honoring `CLAUDE_CONFIG_DIR` the same way the Desktop docs
    say the SKILL.md path itself does (desktop-scheduled-tasks.md: "or under CLAUDE_CONFIG_DIR if
    set") — a repo pinning that env var must not make this scan silently look in the wrong home."""
    root = os.environ.get("CLAUDE_CONFIG_DIR") or str(pathlib.Path.home() / ".claude")
    return pathlib.Path(root) / "scheduled-tasks"


#: #1337 review finding: a bare "autowatch.py" substring match false-positives on a stale/
#: unrelated task whose SKILL.md merely mentions the filename in passing (never actually invoking
#: it), permanently suppressing this check's own warning row. Matched against the exact invocation
#: loop.py's own setup nudge asks Desktop to create instead — narrows, does not eliminate, the
#: gap: a genuinely stale-but-once-real task's SKILL.md still reads as wired after being deleted/
#: paused via the Desktop UI, since that state isn't stored in SKILL.md at all (Desktop's own
#: docs: "Schedule, folder, model, and enabled state are not in this file"). Kept in sync with
#: autowatch.py's own `_DESKTOP_TASK_MARKER` — duplicated, not imported, per this file's own
#: standalone-diagnostic convention (see `_enforce_enabled`'s docstring).
_DESKTOP_TASK_MARKER = "autowatch.py tick"


def _autowatch_adapter_wired(cfg, scheduled_tasks_dir=None):
    """#1321: is EITHER adapter actually set up, not just enabled in config? A `channel_webhook_url`
    means the CLI/Channels adapter is configured; a scheduled task whose own SKILL.md prompt
    actually invokes autowatch.py (not just mentions it) means the Desktop adapter is. Fails open
    to "not wired" on anything unreadable (missing dir, unreadable file) — same direction as every
    other doctor.py scan: a false "not wired" costs one extra reminder, a false "wired" hides a
    real setup gap."""
    aw = _block(_block(cfg, "ledger"), "autowatch")
    if aw.get("channel_webhook_url"):
        return True
    tasks_dir = pathlib.Path(scheduled_tasks_dir) if scheduled_tasks_dir is not None \
        else _default_scheduled_tasks_dir()
    try:
        skill_files = list(tasks_dir.glob("*/SKILL.md"))
    except OSError:
        return False
    for skill_md in skill_files:
        try:
            if _DESKTOP_TASK_MARKER in skill_md.read_text(encoding="utf-8"):
                return True
        except OSError:
            continue
    return False


def _autowatch_feature_state(cfg, scheduled_tasks_dir=None):
    aw = _block(_block(cfg, "ledger"), "autowatch")
    if aw.get("enabled") is not True:
        return "off (no ledger-triggered unattended runs)"
    scope = aw.get("scope")
    # str() each element (matches every other .join() in this file) -- a hand-edited config.json
    # with a non-string scope entry (e.g. a stray number) must degrade to a readable row, not
    # crash features()/check() for the entire dashboard.
    scope_s = ",".join(str(s) for s in scope) if isinstance(scope, list) and scope \
        else "mentions,assignments,blockers"
    hop = aw.get("hop_limit", 1)
    wired = "adapter wired" if _autowatch_adapter_wired(cfg, scheduled_tasks_dir) \
        else "NOT yet wired to an adapter — see /sigma-doctor check"
    return f"ON — scope={scope_s}, hop_limit={hop} — {wired}"


#: #2339 (Component F, design #2329): duplicated, not imported, from
#: slack_commands_listen.py's own STATE_SUBDIR/HEARTBEAT_FILENAME/PID_FILENAME/
#: DEFAULT_APP_TOKEN_ENV/DEFAULT_BOT_TOKEN_ENV/DEFAULT_STALE_AFTER_SECONDS — matching this file's
#: own standalone-diagnostic convention (see `_enforce_enabled`'s docstring / `_DESKTOP_TASK_MARKER`
#: above): doctor.py must keep working even if slack_commands_listen.py's own module can't be
#: imported at all (e.g. its lazy `slack_sdk` dependency is simply missing on this machine), so it
#: reads the same on-disk shapes directly rather than importing the module that writes them.
_SLACK_COMMANDS_HEARTBEAT_FILENAME = "slack-commands.heartbeat.json"
_SLACK_COMMANDS_PID_FILENAME = "slack-commands.pid"
_SLACK_COMMANDS_DEFAULT_APP_TOKEN_ENV = "SIGMA_SLACK_BOT_SOCKET_TOKEN"
_SLACK_COMMANDS_DEFAULT_BOT_TOKEN_ENV = "SIGMA_SLACK_BOT_TOKEN"
_SLACK_COMMANDS_STALE_AFTER_SECONDS = 180


def _slack_commands_listener_state(base, cfg, now=None):
    """What the slack-commands listener's own heartbeat says about itself, or why there is
    nothing to say (#2339, Component F). Mirrors `_ledger_watcher_state`'s exact shape (age-based,
    not error-based) against `slack_commands_listen.py`'s own JSON heartbeat (`write_heartbeat`:
    `{"pid": ..., "last_seen": ...}`) rather than watch_daemon.py's bare-touch file — the write path
    differs, the freshness reasoning does not.

    Per this repo's own LIVENESS bar (AGENTS.md: "a component that has DIED must be
    distinguishable from one with nothing to do"), this tells apart states a bare "enabled" flag
    alone cannot: off (not configured), MISCONFIGURED (enabled but missing a real channel_id or
    either token env var actually set on this machine — the same "configured" bar
    `_autowatch_adapter_wired`'s own check() row draws, BR-21), configured but NEVER RUN, DEAD (a
    pidfile with no heartbeat at all), STALE (a heartbeat gone quiet past the listener's own
    freshness bound), and ON."""
    sc = _block(cfg, "slack_commands")
    if sc.get("enabled") is not True:
        return "off (no inbound Slack commands listener configured)"
    channel = sc.get("channel_id")
    if not (isinstance(channel, str) and channel.strip()):
        return "ON but MISCONFIGURED — no channel_id set (see SLACK_COMMANDS.md)"
    app_env = sc.get("app_token_env") or _SLACK_COMMANDS_DEFAULT_APP_TOKEN_ENV
    bot_env = sc.get("bot_token_env") or _SLACK_COMMANDS_DEFAULT_BOT_TOKEN_ENV
    missing = [name for name in (app_env, bot_env) if not _legacy().getenv(name)]
    if missing:
        return ("ON but MISCONFIGURED — missing env var(s): %s (see SLACK_COMMANDS.md)"
                 % ", ".join(missing))
    import time as _time
    now = now if now is not None else _time.time()
    state_dir = pathlib.Path(base) / "state"
    try:
        data = json.loads((state_dir / _SLACK_COMMANDS_HEARTBEAT_FILENAME).read_text(encoding="utf-8"))
        last_seen = data.get("last_seen")
        if not isinstance(last_seen, (int, float)) or isinstance(last_seen, bool):
            raise ValueError("last_seen missing or not numeric")
    except (OSError, ValueError):
        if not (state_dir / _SLACK_COMMANDS_PID_FILENAME).exists():
            return "configured, but NEVER RUN — no listener has started yet (see SLACK_COMMANDS.md)"
        return ("DEAD — a slack-commands.pid exists but has no heartbeat; restart the listener "
                 "(see SLACK_COMMANDS.md)")
    age = now - last_seen
    if age >= _SLACK_COMMANDS_STALE_AFTER_SECONDS:
        return ("STALE — last heartbeat %.1fh ago; the listener is probably dead (restart it — "
                 "see SLACK_COMMANDS.md)" % (age / 3600.0))
    return "ON — last heartbeat %.0f min ago" % (age / 60.0)


def _journal_shared_events_count(base):
    """Committed event lines under the SHARED .sdlc/ledger/events/ (ledger.py's pre-#244
    entries_dir(EVENTS), unchanged). Shares _ledger_entries' counter, so the fail-open behaviour
    can't drift between the two rows.

    #2574/S1-G3: KEPT, not deleted, even though the journal no longer writes here. An adopter's
    existing history lives in this directory for one release and a dashboard that stopped counting
    it would report a drop that never happened."""
    return _count_jsonl_lines(pathlib.Path(base) / "ledger" / "events")


def _journal_local_events_count(base):
    """Committed-nowhere, LOCAL-only event lines under .sdlc/events/ — ledger.py's
    local_events_dir(), a SIBLING of .sdlc/ledger/, not inside it (matches ledger.py byte-for-byte).
    Since #2574/S1-G3 this is where the journal always writes. Reuses the same fail-open
    _count_jsonl_lines() as the shared counter above so neither destination's row can crash the
    dashboard alone."""
    return _count_jsonl_lines(pathlib.Path(base) / "events")


#: #2738: the journal's config block before the rename, spelled from fragments so this shipped
#: file never carries it whole (test_no_private_names). Read only to SAY it is dead -- never
#: aliased to `journal` (D24).
_LEGACY_JOURNAL_BLOCK = "tele" + "metry"


def _journal_switch_state(base, cfg):
    """The journal row's on/off verdict, wrapped by `_journal_feature_state` (the wrapper is the
    row; #2574/S1-G3; it was named for the old feature, #138/#244).

    "On or off" is not the question. PRD §6.1 asks for FOUR readings, because WHO turned the
    journal on and HOW FRESH that claim is are what an operator actually needs:

      - off;
      - ON — on by your config;
      - ON — on by managed settings, refreshed N hours ago;
      - off — because the managed settings are N days old.

    Plus a fifth this file adds deliberately: a REFUSING managed-settings status (`access-revoked`,
    `locked-key-unverifiable`). §6.1 does not list it, and neither of its two "off" readings can
    carry it honestly — calling a revoked lock "off by your config" is false, and "off because the
    settings are N days old" names an age nobody measured. A separate line is the honest form.

    THE FOURTH STATE IS THE WHOLE POINT OF THE ROW. A stale lease turns the journal off terminally
    (plan Delta-1), and R-8 accepted that ceiling **on the strength of this line existing**: the
    mitigation for "an org's journal silently goes dark for 7+ days" is visibility, not code. Age
    is the tell — the five-day freeze of 2026-08-22 had zero errors the whole time.

    THE LEASE MATH IS NOT REDUPLICATED HERE. `ledger.journal_on` / `_lease_fresh` /
    `_parse_iso8601` are cross-loaded and CALLED (the `_load_loop_script` idiom this file already
    uses for `ledger` in three other checks). Doctor's own duplicated managed-settings read at
    `_managed_settings_state` belongs to the MANAGED SETTINGS row and is untouched: that row
    reports on the file, this one on the switch, and a second copy of the switch is how the two
    would come to disagree about what an adopter's machine is doing.

    Destination no longer routes: every event lands in `.sdlc/events/`. `.sdlc/ledger/events/` is
    named only when it still holds something — one release of legacy history that is read but never
    written, and a row that stopped counting it would report a drop that never happened.

    Fails open to "off" on a malformed block, same convention as every other block reader here;
    `journal_on` is isinstance-guarded on both sides itself."""
    try:
        ledger = _load_loop_script("ledger")
    except Exception:                      # noqa: BLE001 - fail-open, see docstring
        return "COULD NOT CHECK — ledger.py did not load; not an all-clear"

    import time as _time                 # module-local, this file's own convention (no top-level
                                         # `import time`; see `_budget_enforcement_state`)
    try:
        status, locked, refreshed_at = ledger._managed_read_cached(base)
    except Exception:                      # noqa: BLE001 - the row degrades, never raises
        status, locked, refreshed_at = ("not-adopted", None, None)

    locked_here = (status == "ok" and isinstance(locked, dict)
                   and "journal.enabled" in locked)
    if locked_here and not ledger._lease_fresh(refreshed_at):
        parsed = ledger._parse_iso8601(refreshed_at)
        age = ("%d days" % int((_time.time() - parsed) // 86400)) if parsed else "of unknown age"
        return ("off — the managed settings are %s old, so the org lock on the journal has "
                "expired (it turns back on when the lock is refreshed)" % age)
    if locked_here and locked.get("journal.enabled") is not True:
        return ("off — your organisation's managed settings lock the journal off; a locked "
                "`false` beats a local `true`")
    if status in ("access-revoked", "locked-key-unverifiable", "enrolled-policy-missing"):
        return ("off — your organisation's managed settings deny or cannot verify this lock "
                "(see the `managed settings` line in /sigma-doctor)")

    if not ledger.journal_on(base, cfg):
        return "off (nothing is recorded)"

    local = _journal_local_events_count(base)
    legacy = _journal_shared_events_count(base)
    where = "%d event%s in .sdlc/events/ — local and gitignored, never published" % (
        local, "" if local == 1 else "s")
    if legacy:
        where += (", plus %d legacy line%s still read in .sdlc/ledger/events/"
                  % (legacy, "" if legacy == 1 else "s"))
    if locked_here:
        parsed = ledger._parse_iso8601(refreshed_at)
        hours = int((_time.time() - parsed) // 3600) if parsed else None
        when = ("refreshed %d hour%s ago" % (hours, "" if hours == 1 else "s")
                if hours is not None else "lease age unreadable")
        return "ON — on by your organisation's managed settings, %s: %s" % (when, where)
    return "ON — on by your config (`journal.enabled`): %s" % where


def _journal_feature_state(base, cfg):
    """The journal row: `_journal_switch_state`'s verdict, plus a note when the config still
    carries the legacy block (#2738). The note is a suffix; it never changes the verdict."""
    state = _journal_switch_state(base, cfg)
    if isinstance(cfg, dict) and _LEGACY_JOURNAL_BLOCK in cfg:
        state += ("; legacy `%s` block present; the journal reads `journal.enabled` only; "
                  "move the value" % _LEGACY_JOURNAL_BLOCK)
    return state


#: Issue #1779: "last 30d" for the phase-boundary count, matching the window named in the issue's
#: own worked example. Not derived from any existing constant — this answers a DIFFERENT question
#: (how much recent history to cross-reference) from any single daemon's staleness threshold (how
#: old is too old for one last tick), so sharing a name with one would couple two thresholds that
#: happen to be unrelated today and may need to move independently tomorrow.
_DISPATCH_COMPLIANCE_WINDOW_SECONDS = 30 * 24 * 3600


def _read_events_from(directory, ledger):
    """Same tolerant multi-file JSONL union `ledger.read_all()` applies to `entries_dir(sdlc_dir,
    EVENTS)`, but pointed at an arbitrary directory — needed because `read_all()` itself has no
    seam for reading `ledger.local_events_dir()` instead of the shared directory it always reads
    (see `_dispatch_compliance_state`'s docstring for why BOTH must be checked). Mirrors
    `read_all()`'s own guards line for line rather than a narrower doctor-local reimplementation:
    `utf-8-sig` + `errors="replace"` (a plain-utf-8 read raises `UnicodeDecodeError` — a ValueError,
    NOT an OSError — on one invalid byte, silently killing the whole read); catching
    `(ValueError, RecursionError)` around `json.loads` (a deeply nested line raises
    `RecursionError`, a RuntimeError NOT a ValueError, which a narrower catch would let escape and
    discard every record already read); and `ledger._sort_key` for the final ordering, so this
    directory's records sort exactly the way `read_all()`'s own already do. `read_all()`'s own
    history (#175) is why each guard is kept, not trimmed as "the obvious narrower catch" —
    that exact narrowing is what took the whole ledger view down six separate times."""
    out = []
    try:
        paths = sorted(pathlib.Path(directory).glob("*.jsonl"))
    except OSError:
        return out
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if isinstance(item, dict) and item.get("kind"):
                out.append(item)
    out.sort(key=ledger._sort_key)
    return out


def _render_compliance_band(compliant, total, gaps, summary_suffix, all_good_band,
                             all_good_extra="", gap_key_fmt=lambda k: f"#{k[0]} ({k[1]})"):
    """Shared READY/LOGGED-ONLY/PARTIAL/MISSING band + gap-list renderer for the dispatch-
    (model-)compliance row family (#1779/#2514/#2557) -- extracted after this exact ~10-line block
    (sort, cap at 10, "+N more", `x<count>` suffix, MISSING-vs-PARTIAL by `compliant == 0`) had been
    copy-pasted verbatim a THIRD time (issue #2557's own review flagged it). `gaps` is a PRE-SORTED
    list of `((goal, subkey), count)` pairs -- the caller already has to build that list one way or
    another to compute its own denominator, so this takes it built rather than a raw dict/Counter,
    keeping this helper free of any assumption about how the caller derived it. `gap_key_fmt`
    covers the one real per-caller difference: a bare `(<phase>)` vs `(slice <thread>)`."""
    summary = f"{compliant}/{total} {summary_suffix}"
    if not gaps:
        return f"{all_good_band} — {summary}{all_good_extra}"
    gap_count = sum(count for _, count in gaps)
    gap_text = ", ".join(
        gap_key_fmt(key) + (f" x{count}" if count > 1 else "")
        for key, count in gaps[:10]
    )
    if len(gaps) > 10:
        gap_text += " (+%d more)" % (len(gaps) - 10)
    band = "MISSING" if compliant == 0 else "PARTIAL"
    return f"{band} — {summary} — {gap_count} gap{'' if gap_count == 1 else 's'}: {gap_text}"


def _dispatch_compliance_state(base, cfg, now=None):
    """Detection, never enforcement (issue #1779, a follow-up from #1703's own "no in-repo Python
    chokepoint the host's own Task-tool dispatch decision passes through, on any host" finding).
    Cross-references two ALREADY-hardened, already-existing read primitives to answer the question
    #1703 left silent: did a phase the ledger says ran also get dispatched as its own subagent —
    the maker!=checker discipline SKILL.md's per-phase-subagent design exists for — or did it run
    inline in the orchestrator with zero error, zero crash, the exact "no errors, nothing
    happening" shape AGENTS.md's LIVENESS property already names as the dangerous one (the
    five-day event-shipping freeze of 2026-08-22, "zero errors the whole time")?

    TWO EVENT KINDS IN TWO GENUINELY DIFFERENT STORES — a naive grep of one file for both finds
    nothing and falsely reports 100% compliance (confirmed empirically against this repo's own
    `.sdlc/state/log/1627.jsonl` while filing #1779: 36 real lines, zero `"kind": "phase"` rows):
      - the ledger's EVENTS stream, `kind == "phase"` — written by `phase_report.py` at every SDLC
        phase boundary, on every host, whether or not that phase was ever dispatched as a
        subagent. This is the ground truth for "a phase boundary happened at all". Read from BOTH
        destinations, UNIONED (#2574/S1-G3): `_read_events_from(ledger.local_events_dir(base))`
        for `.sdlc/events/`, where the journal writes, plus
        `ledger.read_all(base, stream=ledger.EVENTS)` for `.sdlc/ledger/events/`, which holds one
        release of legacy history and is never moved. Reading either one alone would silently
        under-report the other half as a confident compliance percentage; the two are disjoint
        directories, so the union cannot double-count.
      - each goal's own LOCAL action log (`.sdlc/state/log/<goal>.jsonl`, the `sigma-log` skill's
        own `_all_goal_stems`/`read_goal`) — `agent_dispatch`/`agent_done` entries with
        `role == "phase"`, written only when SKILL.md's own instruction actually ran the phase as
        a dispatched Task-tool subagent.

    off (not a gap) when `ledger.journal_on(base, cfg)` is false — there is nothing recorded to check
    compliance against, a DIFFERENT state from "checked, and it's missing" (the same "off, not
    FAILING" posture every autostart-gated row in this file takes).

    A "phase boundary" is counted once per `state == "end"` phase event within the last 30 days
    (`_DISPATCH_COMPLIANCE_WINDOW_SECONDS`) — `start` events are not separately counted, since
    every real `end` implies a prior `start` and counting both would double the denominator for no
    extra information. Grouped by `(goal, phase)`; a group counts as compliant only when that
    goal's local log carries AT LEAST ONE matching `agent_dispatch` AND AT LEAST ONE `agent_done`
    for that same phase — not correlated by timestamp (the two stores were never designed to line
    up to the millisecond, and detection of the gap is this issue's whole scope, not a forensic
    per-occurrence audit). A `(goal, phase)` pair that recurs (a resumed goal re-running the same
    phase) with no dispatch either time counts as 2 gaps, not 1 — the `x2` suffix below names it.

    READY when every counted boundary matched; MISSING when none did; PARTIAL between the two —
    mirrors the banded severity every sibling watcher-liveness check in this file already uses
    (off / degraded / healthy), just with names suited to a compliance count instead of an age.

    SCALABILITY, NAMED RATHER THAN DISCOVERED LATER: this reads every goal's ENTIRE local log once
    per doctor run — O(total historical log lines across the whole repo), unbounded by the 30-day
    window that already bounds the ledger side (since #457 `retention.py` prunes a goal's log once it
    is closed as done and older than `action_log.retention_days`, default 90; every other goal's log stays).
    At today's scale this is one more full-directory read alongside the `_ledger_entries`/
    `_journal_local_events_count` scans this file already runs on every `/sigma-doctor`; a repo
    with 10-100x more completed-goal history pays a proportionally larger one-time read on every
    invocation. No caching exists today, matching every other row in this file — a future
    hardening could cap the goal scan the same way the ledger side is already capped, by mtime."""
    try:
        ledger = _load_loop_script("ledger")
    except Exception:                      # noqa: BLE001 - fail-open, see docstring
        return "COULD NOT CHECK — ledger.py did not load; not an all-clear"
    try:
        logmod = _load_log_script("log")
    except Exception:                      # noqa: BLE001 - fail-open, see docstring
        return "COULD NOT CHECK — sigma-log's log.py did not load; not an all-clear"

    # The raw config, never `_block()`: `ledger.journal_on` reads only the `journal` block and is
    # isinstance-guarded on the config and on the block itself.
    if not ledger.journal_on(base, cfg):
        return "off (the journal is not enabled — no phase events are recorded to check against)"

    import time as _time
    now = now if now is not None else _time.time()
    cutoff = now - _DISPATCH_COMPLIANCE_WINDOW_SECONDS

    # #2574/S1-G3: BOTH destinations, unioned, never routed on config. The journal writes to
    # `.sdlc/events/`; `.sdlc/ledger/events/` holds one release of legacy history that is never
    # moved. Picking one from config would under-report the other half as a confident percentage.
    # The two paths are disjoint directories, so the union cannot double-count.
    phase_events = (_read_events_from(ledger.local_events_dir(base), ledger)
                    + ledger.read_all(base, stream=ledger.EVENTS))

    ends = {}
    for e in phase_events:
        if e.get("kind") != "phase" or e.get("state") != "end":
            continue
        if ledger._epoch(e.get("ts")) < cutoff:
            continue
        goal, phase = e.get("goal"), e.get("phase")
        if not goal or not phase:
            continue
        key = (str(goal), str(phase))
        ends[key] = ends.get(key, 0) + 1

    total = sum(ends.values())
    if total == 0:
        return "no phase boundaries recorded in the last 30d — nothing to check yet"

    dispatched, done = set(), set()
    for stem in logmod._all_goal_stems(base):
        for entry in logmod.read_goal(base, stem):
            if entry.get("role") != "phase":
                continue
            phase = entry.get("phase")
            if not phase:
                continue
            key = (str(entry.get("goal") or stem), str(phase))
            if entry.get("kind") == "agent_dispatch":
                dispatched.add(key)
            elif entry.get("kind") == "agent_done":
                done.add(key)
    matched = dispatched & done

    compliant = sum(count for key, count in ends.items() if key in matched)
    gaps = sorted((key, count) for key, count in ends.items() if key not in matched)
    return _render_compliance_band(
        compliant, total, gaps,
        "phase boundaries (last 30d) had a matching agent_dispatch/agent_done pair",
        all_good_band="READY")


def _dispatch_model_compliance_state(base, cfg, now=None):
    """#2514 -- a SIBLING to `_dispatch_compliance_state` immediately above, not a rewrite of it.
    That check answers "did a dispatch happen at all"; this one is the narrower, CONDITIONAL
    follow-on -- "given a dispatch happened, did it carry the resolved --model tier". Conflating
    the two into one function would make "no dispatch" and "dispatch, no model" indistinguishable
    in the output, so this is its own row.

    #2514's enforcement half lives in `actionlog.append()`, which now refuses to WRITE a bare
    `agent_dispatch --role phase/slice` with no `--model` at log time. This is the DETECTION half,
    for anything that reached the log by bypassing that refusal -- a hand-forced log line (as the
    unit tests do), a log written before this rule existed, or a future writer that skips the CLI
    and calls `actionlog.append`/`safe_append` directly. Per AGENTS.md's "run the control, or the
    check is decoration", a dispatch with no `model` must be seen to be FLAGGED here, not merely
    assumed impossible because the CLI now refuses it.

    Reuses the exact same two read primitives, window constant
    (`_DISPATCH_COMPLIANCE_WINDOW_SECONDS`), and READY/PARTIAL/MISSING banding/gap-list rendering
    as `_dispatch_compliance_state` -- see that function's own docstring for why both the shared
    ledger events dir and the local journal dir must both be read, and for the
    SCALABILITY note (this pays the same O(total historical log lines) cost, a second time)."""
    try:
        ledger = _load_loop_script("ledger")
    except Exception:                      # noqa: BLE001 - fail-open, see docstring
        return "COULD NOT CHECK — ledger.py did not load; not an all-clear"
    try:
        logmod = _load_log_script("log")
    except Exception:                      # noqa: BLE001 - fail-open, see docstring
        return "COULD NOT CHECK — sigma-log's log.py did not load; not an all-clear"

    # The raw config: `ledger.journal_on` reads only `journal`, isinstance-guarded.
    if not ledger.journal_on(base, cfg):
        return "off (the journal is not enabled — no phase events are recorded to check against)"

    import time as _time
    now = now if now is not None else _time.time()
    cutoff = now - _DISPATCH_COMPLIANCE_WINDOW_SECONDS

    # #2574/S1-G3: BOTH destinations, unioned, never routed on config. The journal writes to
    # `.sdlc/events/`; `.sdlc/ledger/events/` holds one release of legacy history that is never
    # moved. Picking one from config would under-report the other half as a confident percentage.
    # The two paths are disjoint directories, so the union cannot double-count.
    phase_events = (_read_events_from(ledger.local_events_dir(base), ledger)
                    + ledger.read_all(base, stream=ledger.EVENTS))

    ends = {}
    for e in phase_events:
        if e.get("kind") != "phase" or e.get("state") != "end":
            continue
        if ledger._epoch(e.get("ts")) < cutoff:
            continue
        goal, phase = e.get("goal"), e.get("phase")
        if not goal or not phase:
            continue
        key = (str(goal), str(phase))
        ends[key] = ends.get(key, 0) + 1

    total = sum(ends.values())
    if total == 0:
        return "no phase boundaries recorded in the last 30d — nothing to check yet"

    modeled = set()
    for stem in logmod._all_goal_stems(base):
        for entry in logmod.read_goal(base, stem):
            if entry.get("kind") != "agent_dispatch" or entry.get("role") != "phase":
                continue
            phase = entry.get("phase")
            if not phase or not entry.get("model"):
                continue
            key = (str(entry.get("goal") or stem), str(phase))
            modeled.add(key)

    compliant = sum(count for key, count in ends.items() if key in modeled)
    gaps = sorted((key, count) for key, count in ends.items() if key not in modeled)
    return _render_compliance_band(
        compliant, total, gaps,
        "phase boundaries (last 30d) had a matching agent_dispatch carrying model",
        all_good_band="LOGGED ONLY",
        all_good_extra="; this does not prove the host used that model (phase_report.py end "
                        "checks observed models when a transcript is available)")


def _dispatch_model_compliance_slice_state(base, cfg, now=None):
    """issue #2557 -- a SIBLING to `_dispatch_model_compliance_state` immediately above, not a
    widening of it: that row cross-references ledger `phase`/`end` events as its denominator (a
    clean 1:1 boundary signal, written by phase_report.py on every host regardless of dispatch
    method). Slice has no equivalent to cross-reference. The ledger's own `"slice"` EVENT_KINDS
    member is written only by `slices.py`'s `plan` subcommand, one row per manifest-DECLARED slice
    -- at PLANNING time, not dispatch or completion -- and per that file's own comment (~line
    550-554) has never fired in this repo: no plan has ever declared slices. Cross-referencing it
    would measure "was this slice planned", a different and decoupled question from "was it
    dispatched with a model". So this audits the local action log directly: every `agent_dispatch`
    entry with `role == "slice"` in the last `_DISPATCH_COMPLIANCE_WINDOW_SECONDS`, and whether it
    carries `model`. `actionlog.append()` has refused a bare `--role slice` dispatch since #2514/
    #2521 (already released) -- same posture as the phase row: this is the DETECTION half, for
    anything that reached the log by bypassing that refusal.

    NAMED LIMITATIONS, not silently accepted as equivalent to the phase row's coverage:
    (1) this cannot see a slice that ran with ZERO log entries at all -- the exact blind spot the
    phase row's ledger cross-reference exists to close; no ledger-side "a slice ran" signal exists
    today to close it here. (2) `thread` (see keying, below) is convention, not mechanically
    required the way `--model` now is, so two distinct slices dispatched without it in the same
    goal would collide under `(goal, "main")` and under-report as one gap instead of two -- itself
    a `--thread` compliance gap this row is not scoped to catch. (3) an entry whose `ts` does not
    parse (`logmod._epoch()` returns `None`) is silently excluded from both `total` and the gap
    list, not counted as a gap of its own -- chosen because an unparseable timestamp cannot be
    placed inside or outside the 30-day window at all, so counting it as "recent and missing"
    would itself be a fabricated claim; but it means the row's numbers do not account for that
    entry either way.

    Gated on `action_log.enabled` (NOT `journal.enabled`, unlike the sibling row) -- delegates to
    `actionlog.enabled()` itself (the same shared primitive `actionlog.append()` no-ops against
    before writing anything) rather than re-deriving the flag locally, so a future change to that
    idiom can't silently drift between the two. This check's denominator comes entirely from the
    local action log, independent of the journal -- gating on the journal here would be checking
    the wrong flag.

    Keyed by `(goal, thread)` -- `thread` carries the slice id per `running.md`'s documented
    invocation (`--thread <slice-id> --role slice --phase implement --model <tier>`). Counts every
    individual dispatch, not a per-key set membership test like the phase row's `modeled` set: a
    slice first dispatched with no model (a bug) and later re-dispatched with one (a fix) still
    contributes one gap from the first entry, on top of the compliant second one -- a real,
    historical violation the row does not retroactively forgive, unlike the phase row's "any
    compliant dispatch for this key clears it" semantics.

    SCALABILITY: a THIRD independent O(total historical log lines) full-repo log scan -- on top of
    the two `_dispatch_compliance_state`'s own docstring already names, which its text calls "a
    second time" for the first pair. Each additional role-scoped compliance row compounds this
    multiplicatively, not additively: at 10x/100x more completed-goal history, three unmerged scans
    cost 3x/30x/300x a single pass's read+parse work, not "one more" fixed increment. No caching
    exists for any of the three today (`logmod.read_goal()` re-reads and re-parses every call) --
    the same accepted-for-now, unbounded-by-mtime posture this file's other rows already carry, but
    now with a compounding multiplier worth naming rather than restating as if it were still one
    scan's cost."""
    try:
        logmod = _load_log_script("log")
    except Exception:                      # noqa: BLE001 - fail-open, see docstring
        return "COULD NOT CHECK — sigma-log's log.py did not load; not an all-clear"
    try:
        actionlog = _load_loop_script("actionlog")
    except Exception:                      # noqa: BLE001 - fail-open, see docstring
        return "COULD NOT CHECK — actionlog.py did not load; not an all-clear"

    if not actionlog.enabled(cfg):
        return "off (action_log.enabled is not True — no dispatch entries are recorded to check)"

    import time as _time
    now = now if now is not None else _time.time()
    cutoff = now - _DISPATCH_COMPLIANCE_WINDOW_SECONDS

    from collections import Counter
    total = 0
    compliant = 0
    gap_counts = Counter()
    for stem in logmod._all_goal_stems(base):
        for entry in logmod.read_goal(base, stem):
            if entry.get("kind") != "agent_dispatch" or entry.get("role") != "slice":
                continue
            ts_epoch = logmod._epoch(entry.get("ts"))
            if ts_epoch is None or ts_epoch < cutoff:
                continue
            total += 1
            if entry.get("model"):
                compliant += 1
            else:
                key = (str(entry.get("goal") or stem), str(entry.get("thread") or "main"))
                gap_counts[key] += 1

    if total == 0:
        return "no slice dispatches recorded in the last 30d — nothing to check yet"

    gaps = sorted(gap_counts.items())
    return _render_compliance_band(
        compliant, total, gaps,
        "slice dispatches (last 30d) carried model",
        all_good_band="LOGGED ONLY",
        all_good_extra="; this does not prove the host used that model (phase_report.py end "
                        "checks observed models when a transcript is available)",
        gap_key_fmt=lambda k: f"#{k[0]} (slice {k[1]})")


#: Same window as `_DISPATCH_COMPLIANCE_WINDOW_SECONDS` (30 days) but its own named constant --
#: sharing the literal value is a coincidence of "what's a reasonable recency window", not a
#: coupling the two checks should share code over; each is free to move independently.
_BUDGET_ENFORCEMENT_WINDOW_SECONDS = 30 * 24 * 3600


def _rate_card_coverage_state(base, now=None):
    """Report whether the last twenty durable phase ends included unpriced model turns.

    `phase_report.py end` writes ``model`` and ``unpriced_turns`` into each phase-end event. The
    direct event record matters: an stderr warning is visible only to the process that ended the
    phase, whereas `/sigma-doctor` is the host-agnostic recovery surface. This is deliberately a
    bounded read of *records*, not a time window: a quiet repository should still surface the last
    model that its operator actually used. Historical records lacking the additive fields are
    honestly counted as legacy/unknown coverage and do not manufacture a failure.
    """
    try:
        ledger = _load_loop_script("ledger")
    except Exception:                      # noqa: BLE001 - diagnostic failure is never all-clear
        return "COULD NOT CHECK — ledger.py did not load; not an all-clear"
    events = (_read_events_from(ledger.local_events_dir(base), ledger)
              + ledger.read_all(base, stream=ledger.EVENTS))
    ends = [event for event in events
            if event.get("kind") == "phase" and event.get("state") == "end"]
    if not ends:
        return "no phase boundaries recorded — no model-rate coverage to check yet"
    ends.sort(key=lambda event: str(event.get("ts") or ""))
    recent = ends[-20:]
    gaps = []
    for event in recent:
        try:
            unpriced = int(event.get("unpriced_turns") or 0)
        except (TypeError, ValueError):
            unpriced = 0
        if unpriced > 0:
            gaps.append(str(event.get("model") or "unknown"))
    if gaps:
        shown = ", ".join(dict.fromkeys(gaps))
        return ("MISSING — %d/%d recent phase record(s) include unpriced turns "
                "(model %s); add its price to skills/sigma-loop/rates/anthropic_list_prices.csv"
                % (len(gaps), len(recent), shown))
    legacy = sum("unpriced_turns" not in event for event in recent)
    suffix = ("; %d legacy record(s) predate this coverage field" % legacy) if legacy else ""
    return "READY — %d recent phase record(s) had no unpriced turns%s" % (len(recent), suffix)


def _budget_enforcement_state(base, cfg, now=None):
    """#2515 -- a SIBLING to the existing "budgets" row, not a rewrite of it. That row answers "is
    `budget.max_tokens` CONFIGURED" (a config read, always true/false, cheap); this one answers the
    narrower, CONDITIONAL follow-on -- "given it's configured, did this run credit priced phases"
    -- and conflating the two into one row would make "not configured" and
    "configured but inert" indistinguishable in the output, exactly the ambiguity #2514's own
    sibling-row precedent (`_dispatch_model_compliance_state`, immediately above) warns against for
    its own case.

    Before #2515's fix, `run_tokens` never moved off 0 on any host. Matching old `phase`/`spend`
    events therefore cannot prove enforcement. Current-run `attempt_id` values must also occur in
    STATE.md's durable `run_token_credits`; only `phase_report.py cmd_end` writes those credits.
    This path is independent of the journal, so this check's own "off" state is
    reserved for `max_tokens` itself not being configured; a journal-off host gets its own honest
    blind-spot note instead (below) rather than reading as "off", because the mechanism may well be
    working even though this check has nothing to read.

    Host-capability signal, explicit: MISSING keys off exactly the `source`/`measured` boolean
    `collect_phase_usage`/`cmd_end` already compute (`measured = result["source"] in
    ("claude-code", "claude-code-inline", "codex")`) -- surfaced here as "did any recent `phase`
    `end` event carry `tokens_in`/`tokens_out` at all", the durable, ledger-readable proxy for "this
    host has never actually measured spend"."""
    budget = _block(cfg, "budget")
    max_tokens = budget.get("max_tokens")
    if not max_tokens:
        return "off (budget.max_tokens not configured — nothing to enforce)"

    try:
        ledger = _load_loop_script("ledger")
        state = _load_loop_script("state")
    except Exception:                      # noqa: BLE001 - fail-open, see docstring
        return "COULD NOT CHECK — ledger/state did not load; not an all-clear"

    cursor = state.load_cursor(base)
    run_started_at = cursor.get("run_started_at", 0)
    credited_attempts = state.run_token_credits(base)

    # The raw config: `ledger.journal_on` reads only `journal`, isinstance-guarded.
    journal_note = ""
    if not ledger.journal_on(base, cfg):
        # NOT "off" like the dispatch-compliance sibling: state.add_tokens is fed directly by
        # phase_report.py, independent of the journal (Decision 2) -- so budget.max_tokens may well
        # be WORKING right now even though this doctor check has nothing to read. Say so.
        journal_note = (" (the journal is off, so this check has no events to read — "
                           "budget.max_tokens may still be enforced; check STATE.md's "
                           "run_tokens directly to confirm)")
        phase_events = []
    else:
        import time as _time
        now = now if now is not None else _time.time()
        cutoff = now - _BUDGET_ENFORCEMENT_WINDOW_SECONDS
        # #2574/S1-G3: both destinations, unioned -- see `_dispatch_compliance_state`.
        phase_events = (_read_events_from(ledger.local_events_dir(base), ledger)
                        + ledger.read_all(base, stream=ledger.EVENTS))
        phase_events = [e for e in phase_events if ledger._epoch(e.get("ts")) >= cutoff]
    if run_started_at:
        phase_events = [e for e in phase_events if ledger._epoch(e.get("ts")) >= int(run_started_at)]

    ends = [e for e in phase_events if e.get("kind") == "phase" and e.get("state") == "end"]
    spends = [e for e in phase_events if e.get("kind") == "spend"]
    measured_ends = [e for e in ends if e.get("tokens_in") is not None]

    if not ends:
        return "no phase boundaries recorded in the last 30d — nothing to check yet" + journal_note
    if not measured_ends:
        return ("MISSING — %d phase-end(s) recorded but NONE measured tokens (this host's "
                "transcript source was unavailable every time — see phase banners' 'cost: "
                "unavailable' reason); automatic phase credits cannot feed budget.max_tokens here"
                % len(ends)) + journal_note
    from collections import Counter
    def spend_key(event):
        return (event.get("attempt_id"), str(event.get("goal")), event.get("phase"),
                str(event.get("tokens_in")), str(event.get("tokens_out")))

    # Match multiplicities: one spend for two identical phase ends proves only one was priced.
    available = Counter(spend_key(e) for e in spends
                        if e.get("cost_cents") is not None
                        and e.get("attempt_id") in credited_attempts)
    priced = 0
    for event in measured_ends:
        key = spend_key(event)
        if event.get("attempt_id") in credited_attempts and available[key]:
            available[key] -= 1
            priced += 1
    if priced != len(ends):
        return ("PARTIAL — %d/%d current-run phase-end(s) have a matching priced `spend` event "
                "and a durable budget credit; older events alone cannot prove enforcement"
                % (priced, len(ends))) + journal_note
    return ("READY — %d/%d current-run phase-end(s) have matching priced `spend` events and "
            "durable budget credits"
            % (priced, len(ends))) + journal_note


def _managing_session_state(base, cfg):
    """Whether A managing session (a routine/cron firing, or a manual overnight run — loop.py's
    `session_start`/`session_active`, F10.5-4/#377) is currently registered for THIS `.sdlc` (#1200
    gap 2). Feeds an ALWAYS ok=True `check()` row — `_chk(f"managing session: {...}", True, "")` —
    the same "state embedded in the name, no fix" idiom the "companions" rows already use just below
    it in `check()`: neither "registered" nor "not registered" is wrong on its own (most adopters
    never run a routine at all, and this doctor invocation may itself be running FROM inside the one
    registered session), so this is a report, never a gate.

    Loads `loop` through `_load_loop_script` (this file's one sanctioned cross-skill loader, see its
    own docstring) rather than duplicating `session_active`'s liveness logic (pid_alive + lease TTL)
    locally — the same reasoning `_dependency_marker_scan` (and preflight's gh-auth check) give for reusing
    `gh_session`/`sources`/`backlog_check` verbatim: a local copy here could silently drift from
    loop.py's own, and for a liveness check, drift is the exact failure mode being avoided.

    `session_active` itself already reads "no marker ever written" as `False` without raising (see
    its own docstring) — so the ONLY thing guarded here is the cross-load itself (a broken or
    missing loop.py), matching this file's fail-open convention: a load failure degrades to
    "unknown", never a crash and never a false "not registered" claim.

    Deliberately does NOT claim to detect a SECOND concurrent session — `session_active` cannot
    distinguish two sessions today (the second `session_start` overwrites the first's marker; either
    `session_end` deletes the shared marker) — see #1199, filed alongside this. Reports only what
    CAN be reported today: whether A session is registered."""
    try:
        loop = _load_loop_script("loop")
    except Exception:                             # noqa: BLE001 - fail-open, see docstring
        return "unknown — could not load loop.py"
    try:
        active = loop.session_active(str(base), cfg)
    except Exception:                             # noqa: BLE001 - fail-open, see docstring
        return "unknown — session_active() raised"
    return ("registered (active)"
            if active else "not registered")


def _backlog_check_state(cfg):
    """OFF unless backlog_check.enabled is strictly True (a truthy string must not switch a pick-path
    behavior on). A non-dict block degrades to off — same convention as every other reader here."""
    b = _block(cfg, "backlog_check")
    if b.get("enabled") is not True:
        return "off (a picked goal is worked without a backlog cross-check)"
    how = ("parks a confident duplicate/obsolete/blocked goal (with proof), else annotates"
           if b.get("action", "park") == "park" else "only annotates (flag mode — never parks)")
    return "ON — pre-work cross-check " + how


def _goal_decompose_state(cfg):
    """OFF unless goal_decompose.enabled is strictly True (same truthy-string guard as
    _backlog_check_state above — a pick-path behavior must not switch on a non-bool truthy value).
    A non-dict block degrades to off; an unrecognized mode string mirrors decompose_check's own
    fallback to 'log' (loop.py) rather than reporting a state the guard itself would never take."""
    g = _block(cfg, "goal_decompose")
    if g.get("enabled") is not True:
        return "off (a picked goal is never size-checked)"
    mode = g.get("mode") or "log"
    if mode not in ("log", "park", "file"):
        mode = "log"
    how = {
        "park": "parks an oversized goal for a human to split",
        "file": 'parks an oversized goal AND files one idempotency-guarded "Decompose #N" meta-issue',
    }.get(mode, "only annotates (log mode — never parks)")
    return "ON — pre-work size classifier " + how


def _no_dangling_goal_state(base, cfg):
    """Is discovery.no_dangling_goal (and its live-judge opt-in) on, correctly configured, and
    pointed at a real registered unit? #2429: no doctor row existed for this feature at all, unlike
    every other opt-in in this kit — a typo'd `core` catch-all name, or a `rounds` value the
    config-reader silently corrects (#2428), sat invisible until the classifier actually ran
    against a real issue and either mis-attributed it or quietly wasted a live-judge call.

    Cross-loads `sources`/`feature_registry` from the sibling sigma-loop skill (`_load_loop_script`,
    a deliberate, narrow exception to this file's no-cross-skill-import convention — see that
    loader's own docstring), so the row reads the SAME functions that decide the real behaviour —
    never a doctor-local reimplementation that could quietly drift from them.

    THE CATCH-ALL IS VALIDATED, NOT RE-VALIDATED. `_no_dangling_goal_core` itself does no
    shape-checking (a plain string read); the real check is `feature_registry.resolve_open_unit`,
    the SAME function `feature_classify.classify`/`classify_for_filing` resolve the name through at
    actual classify-time. This row calls that one function rather than re-deriving the rule, so a
    typo surfaces here proactively instead of only reactively, the moment a human runs `/sigma-doctor`
    rather than the moment a goal happens to reach the classifier.

    THE ROUNDS NOTE COMPARES RAW CONFIG AGAINST THE RESOLVED VALUE, deliberately — reading only
    `_no_dangling_goal_live_judge_rounds(cfg)`'s own return can never tell "configured 3 directly"
    apart from "configured 1, silently corrected to 3" (#2428), because that function's whole job
    is to make the two indistinguishable to every OTHER caller. Doctor is the one reader with a
    reason to want the raw value too, precisely so a human sees their own typo instead of a number
    that quietly stopped meaning what they typed."""
    try:
        sources = _load_loop_script("sources")
        feature_registry = _load_loop_script("feature_registry")
    except Exception:                     # noqa: BLE001 - a diagnostic must never crash the dashboard
        return "could not check (sigma-loop scripts unavailable)"
    # THE MALFORMED-CONFIG GUARD, AND WHY IT LIVES HERE RATHER THAN IN `sources.py`. Every
    # `sources._no_dangling_goal_*` reader trusts `config["discovery"]` to already be a dict (or
    # falsy) -- true of every OTHER caller, which all construct a `GitHubSource` from a config this
    # kit's own loaders already validated. Doctor is different: `features()` is the one tool run
    # BECAUSE a config is suspected broken, and its own fuzz coverage sets `discovery` itself to a
    # bare string/int/bool/list -- `(cfg.get("discovery") or {})` then evaluates to that TRUTHY
    # non-dict value unchanged (`True or {}` is `True`), and `.get("no_dangling_goal")` on it
    # raises. `_block()` is this file's own answer to exactly that shape, so a SANITIZED config is
    # built once, here, and handed to every cross-loaded reader below -- never the raw `cfg`.
    safe_cfg = {"discovery": _block(cfg, "discovery")}
    if not sources._no_dangling_goal_enabled(safe_cfg):
        return "off (a goal declaring no unit at all is picked exactly as before this feature existed)"
    parts = []
    core = sources._no_dangling_goal_core(safe_cfg)
    if core:
        if not feature_registry.registry_dir(str(base)).is_dir():
            parts.append("catch-all %r is configured, but .sdlc/features/ does not exist yet — "
                        "the whole feature is inert on this repo until it adopts the registry"
                        % core)
        else:
            resolved = feature_registry.resolve_open_unit(str(base), core)
            if resolved is None:
                parts.append("catch-all %r does NOT resolve to a real, open registered unit — "
                            "check for a typo, or a unit that is closed or never opened" % core)
            else:
                parts.append("catch-all %r resolves to the registered unit %r" % (core, resolved))
    else:
        parts.append("no catch-all configured — an undeclared goal is SET ASIDE under sdlc:needs-unit")
    if sources._no_dangling_goal_live_judge_enabled(safe_cfg):
        raw_rounds = _block(safe_cfg["discovery"], "no_dangling_goal")
        raw_rounds = _block(raw_rounds, "live_judge").get("rounds")
        rounds = sources._no_dangling_goal_live_judge_rounds(safe_cfg)
        ceiling = sources._no_dangling_goal_live_judge_spend_ceiling_usd_per_day(safe_cfg)
        note = ""
        if raw_rounds is not None:
            try:
                corrected = int(raw_rounds) != rounds
            except (TypeError, ValueError):
                corrected = True
            if corrected:
                note = " (configured %r was invalid — corrected to %d)" % (raw_rounds, rounds)
        parts.append("live judge ON — rounds=%d%s, spend ceiling $%.2f/day" % (rounds, note, ceiling))
    else:
        parts.append("live judge off (classification never reaches tiers 1/3)")
    return "ON — " + "; ".join(parts)


def _decision_gate_state(base, cfg):
    """Count the ACTIVE decisions, not the entries. A registry whose decisions are all superseded
    enforces nothing, and reporting it as ON would be exactly the false assurance this gate exists
    to remove."""
    reg = pathlib.Path(base) / "decisions.json"
    if not reg.exists():
        return "off (no registry — nothing is enforced)"
    # #622: the hook is inert without the adoption marker. Deliberately simpler than
    # gate_state.adopted_root (no upward walk; doctor is handed the .sdlc dir itself) and pinned by a test.
    if not (pathlib.Path(base) / "config.json").exists():
        return "registry present but repo NOT adopted (no .sdlc/config.json) — the hook enforces nothing; run /sigma-init"
    if _block(_block(cfg, "gates"), "decision_gate").get("enabled") is False:
        return "DISABLED by config (registry present but not enforced)"
    try:
        decisions = json.loads(reg.read_text(encoding="utf-8")).get("decisions") or []
    except Exception:
        return "registry present but UNREADABLE — the gate fails open, so nothing is enforced"
    active = [d for d in decisions if isinstance(d, dict) and d.get("status", "active") == "active"]
    inv = sum(1 for d in active if d.get("class") == "invariant")
    if not active:
        return "registry present but NO active decisions — nothing is enforced"
    return f"ON — {inv} invariant(s) deny, {len(active) - inv} recipe(s) ask"


def _automerge_state(wk):
    """Mirrors work.policy() without importing it — doctor stays standalone, and a dashboard that
    lied about which merge policy is live would be worse than no dashboard."""
    if wk.get("enabled") is not True:
        return "off (per-goal worktrees are off)"
    value = wk.get("auto_merge")
    chosen = "always" if value is True else (str(value).strip().lower() if value else "off")
    method = wk.get("merge_method") or "squash"
    return {
        "always": "ALWAYS (%s) — merges even where nothing is enforced on the base" % method,
        "protected": "PROTECTED (%s) — merges only where the base REQUIRES checks/reviews" % method,
    }.get(chosen, "off (a clean, safe PR is left for a human)")


#: #144: `feature_rebase.BLOCKED_SUFFIX`, duplicated rather than imported (skills do not import each
#: other's Python, north-star architecture rule 3). `test_doctor_blocked_suffix_matches_feature_rebase`
#: reads the real constant from its file path and fails if this literal drifts from it.
_REBASE_BLOCKED_SUFFIX = ".rebase-blocked.json"


def _upkeep_off(wk):
    """Mirrors `feature_rebase.switch()`: only `off` or boolean false is off; anything else is on."""
    value = (wk or {}).get("rebase_upkeep")
    return value is False or (isinstance(value, str) and value.strip().lower() == "off")


def _unit_can_be_upkept(base, unit):
    """Is `unit` still a registered, OPEN unit -- i.e. will a pass ever run for it again and clear
    its marker? `False` only on a positive answer that it will not (closed, or no longer registered);
    a registry that cannot be read keeps the row, failing towards visibility."""
    try:
        fr = _load_loop_script("feature_registry")
        features_dir = fr.registry_dir(str(base))
        if not features_dir.is_dir():
            return False
        entry = fr.read_unit(features_dir, unit)
    except Exception:                     # noqa: BLE001 - unanswered keeps the row
        return True
    return isinstance(entry, dict) and entry.get("open") is not False


def _rebase_blocks(base, wk=None):
    """[(branch, dropped_count, first names, at)] for every unit whose rebase upkeep is REFUSING
    (#144) -- the markers `feature_rebase` writes on `would-drop` and removes on the next clean
    pass. Read-only and total: an unreadable marker is still reported, as unreadable.

    A marker is cleared ONLY by a clean pass, so one that no pass will ever revisit would otherwise
    fail `/sigma-doctor` forever: none is reported while `work.rebase_upkeep` is off (the switch a
    person sets precisely to stop the retries), nor for a unit that is closed or no longer in the
    registry. The marker file is left as it is -- the doctor is read-only; turning upkeep back on
    or reopening the unit shows it again until a pass clears it."""
    found = []
    if _upkeep_off(wk):
        return found
    try:
        paths = sorted((pathlib.Path(base) / "state" / "features").glob("*" + _REBASE_BLOCKED_SUFFIX))
    except OSError:
        return found
    for path in paths:
        try:
            got = json.loads(path.read_text(encoding="utf-8"))
            unit = str(got.get("unit") or path.name[:-len(_REBASE_BLOCKED_SUFFIX)])
            if not _unit_can_be_upkept(base, unit):
                continue
            found.append((str(got.get("branch") or path.name), int(got.get("dropped_count") or 0),
                          ", ".join((got.get("dropped") or [])[:3]), str(got.get("at") or "?")))
        except Exception:                 # noqa: BLE001 - a doctor row never crashes the doctor
            found.append((path.name, 0, "marker unreadable", "?"))
    return found


_REBASE_PARKED_SUFFIX = ".rebase-parked.json"


def _age_text(at):
    """`3d 4h` / `5h` / `12m` for an ISO UTC stamp, or `age unknown`. Total."""
    import calendar
    import time as _time
    try:
        seconds = max(0, int(_time.time() - calendar.timegm(_time.strptime(str(at), "%Y-%m-%dT%H:%M:%SZ"))))
    except Exception:                     # noqa: BLE001
        return "age unknown"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    return (f"{days}d {hours}h" if days else f"{hours}h" if hours else f"{rest // 60}m")


def _rebase_parks(base, wk=None):
    """[(branch, files, at, age)] for every unit whose rebase is PARKED (part B, level 3) -- the markers
    `feature_rebase` writes on a park and removes on the next clean pass. Same posture as `_rebase_blocks`: read-only,
    total, nothing while `work.rebase_upkeep` is off, nothing for a closed or unregistered unit, an unreadable marker
    still reported."""
    found = []
    if _upkeep_off(wk):
        return found
    try:
        paths = sorted((pathlib.Path(base) / "state" / "features").glob("*" + _REBASE_PARKED_SUFFIX))
    except OSError:
        return found
    for path in paths:
        try:
            got = json.loads(path.read_text(encoding="utf-8"))
            unit = str(got.get("unit") or path.name[:-len(_REBASE_PARKED_SUFFIX)])
            if not _unit_can_be_upkept(base, unit):
                continue
            at = str(got.get("at") or "?")
            found.append((str(got.get("branch") or path.name), int(got.get("files") or 0), at, _age_text(at)))
        except Exception:                 # noqa: BLE001 - a doctor row never crashes the doctor
            found.append((path.name, 0, "?", "age unknown"))
    return found


def _rebase_upkeep_state(base, wk):
    """`work.rebase_upkeep`, and -- the part a person needs -- whether any unit's upkeep is
    currently REFUSING a net-destructive replay (#144). Mirrors `feature_rebase.switch()`: only
    `off` or boolean false is off; anything else is on."""
    if _upkeep_off(wk):
        return "off"
    blocks = _rebase_blocks(base, wk)
    if not blocks:
        return "ON — feature branches are brought forward on each pick; none is blocked"
    return "ON — BLOCKED: " + "; ".join(
        "%s would lose %d tracked path(s) (%s) since %s, nothing was pushed" % b for b in blocks)


def _review_gate_state(wk):
    """Mirrors work.review_mode() without importing it. A REAL PR-review gate independent of branch
    protection — worth showing because it's the difference between 'auto-merge respects a human's
    Request-changes' and 'it merges straight over it' on an unprotected base."""
    value = wk.get("require_review")
    mode = "approval" if value is True else (str(value).strip().lower() if value else "off")
    return {
        "changes": "ON (changes) — parks on CHANGES_REQUESTED, an unresolved thread, or a `sigma:block`",
        "approval": ("ON (approval) — the loop reviews its own PR and posts sigma:approve/block "
                     "(work.py post-review); merges only an approved PR. A human can use the markers too"),
    }.get(mode, "off — auto-merge only respects reviews the base branch's protection REQUIRES")


#: A resolver that failed must not read as "independent, mechanism unremarkable" -- that is the
#: pre-#1983 string this row was changed to stop printing. Absence names its own remedy.
_UNKNOWN_MECHANISM = " (mechanism: UNKNOWN — the resolver failed; run reviewer.py resolve .sdlc)"


def _resolved_mechanism(sdlc_dir):
    """-> " (mechanism: X, host: Y)" or "". Shells out rather than importing, because doctor is a
    DIFFERENT skill from sigma-loop (north-star Architecture Rule 3). Never raises: a doctor that
    crashes because the resolver is unhappy is worse than one that reports a little less."""
    script = (pathlib.Path(__file__).resolve().parent.parent.parent
              / "sigma-loop" / "scripts" / "reviewer.py")
    try:
        proc = subprocess.run([sys.executable, str(script), "resolve", str(sdlc_dir)],
                              capture_output=True, text=True, timeout=10)
        if proc.returncode != 0:
            return _UNKNOWN_MECHANISM
        got = json.loads(proc.stdout)
        return " (mechanism: %s, host: %s)" % (got["mechanism"], got["host"])
    except Exception:               # noqa: BLE001 - name the absence, never crash
        return _UNKNOWN_MECHANISM


def _review_independence_state(cfg, sdlc_dir=".sdlc"):
    """Is the maker kept out of its own review? Worth showing because the failure is silent: a maker
    that reviews its own plan/diff reads as "reviewed", and on a lower tier it rubber-stamps.

    #1983: the flag alone is no longer the whole answer. `independent: true` on a machine whose
    resolver returns `inline` says a fresh reviewer is spawned when none is, so the RESOLVED
    mechanism is reported alongside it. The default keeps every existing single-argument caller."""
    if _block(cfg, "review").get("independent") is False:
        return "off (INLINE — the maker reviews its own work; a fresh reviewer is not spawned)"
    return ("ON — asks for a fresh, author-blind reviewer per gate (advisory; unproved), grounded in the north-star + whole repo"
            + _resolved_mechanism(sdlc_dir))


def _handoff_row_state(cfg):
    """#2521: the report-row half of `loop.py`'s `_handoff_ceiling` — deliberately a SECOND, hand-
    kept literal rather than `import loop` (skills do not import each other's Python; north-star.md
    architecture rule #3), so `test_doctor_handoff_default_matches_loop_pys_own_constant` is the
    sync mechanism instead: it reads loop.py's real `DEFAULT_HANDOFF_AFTER_GOALS` from its file path
    and fails loudly if this literal ever drifts from it."""
    block = cfg.get("handoff")
    block = block if isinstance(block, dict) else {}
    if block.get("enabled") is False:
        return "off"
    after_goals = block.get("after_goals", 20)   # DEFAULT_HANDOFF_AFTER_GOALS, mirrored here rather
                                                    # than imported -- doctor.py does not import loop.py
                                                    # (skills do not import each other's Python;
                                                    # north-star.md architecture rule #3)
    return f"after_goals={after_goals}" if after_goals else "off"


def _model_max_tier(cfg):
    """`model_selection_max_tier` as predict.py's own `max_tier()` resolves it (#2564), for the
    report row only. DUPLICATES that parser deliberately (4 lines) rather than importing it: skills
    do not import each other's Python, and a doctor row is not worth the first cross-skill import.
    Kept trivially in lockstep -- if predict.py ever grows a fifth tier, this falls back to the
    default and under-reports rather than crashing the doctor run."""
    raw = cfg.get("model_selection_max_tier") if isinstance(cfg, dict) else None
    tier = raw.strip().lower() if isinstance(raw, str) else ""
    return tier if tier in ("haiku", "sonnet", "opus", "fable") else "opus"


_LEGACY_MODULE = []


def _legacy():
    """`skills/sigma-loop/scripts/legacy.py`, the ONE reader of the plugin's previous name (#239),
    loaded once. The name is spelled from fragments there (a guarded private name, #2729)."""
    if not _LEGACY_MODULE:
        _LEGACY_MODULE.append(_load_loop_script("legacy"))
    return _LEGACY_MODULE[0]


#: The previous env prefix, from the helper (never re-spelled here).
_RETIRED_ENV_PREFIX = _legacy().RETIRED_ENV_PREFIX


def _legacy_env_names_in_config(cfg, prefix=""):
    """[(dotted.key.path, value)] for every `*_env` value still naming the previous env prefix.
    Delegates to `legacy.legacy_env_values` so this row and `migrate.py` share one walker."""
    return _legacy().legacy_env_values(cfg, prefix)


def features(sdlc_dir=".sdlc", run=None, scheduled_tasks_dir=None):
    """The capability dashboard: every optional feature, its CURRENT state, and the one-line
    enable. Informational (never a failure) — the answer to "what is on right now?".

    `run` (new, #1268 review): threaded to `_pick_path_gate_state`, which needs one live `gh` read
    to tell BOARD-GATED apart from the false-assurance case where config says "status" but no live
    Ready lane exists yet. Defaults to `_real_run`, same convention as `check()`.

    `scheduled_tasks_dir` (#1321): DI for tests, threaded to `_autowatch_feature_state`."""
    import os
    run = run or _real_run
    cfg = _cfg(sdlc_dir)
    base = pathlib.Path(sdlc_dir)
    budget = _block(cfg, "budget")
    verify = _block(cfg, "verify")
    gates = _block(cfg, "gates")
    # RAW, not `_block`ed (#2116). `_block` flattens a non-dict to `{}`, and `gates.hard_plan_gate`
    # is the BLOCK path an Org locks -- so `{"gates": {"hard_plan_gate": true}}` is a legal policy
    # that `work.py`'s enforcement point reads as ON, and coercing it here would report `off` for a
    # gate that is genuinely refusing pull requests. `_gate_enabled` is total, so it takes the raw
    # value; `_effective_window` still wants a dict, which is what `gate_window` is for.
    gate = gates.get("hard_plan_gate")
    gate_window = gate if isinstance(gate, dict) else {}
    # RAW too (#2116), for the same reason and to the same end as `hard_plan_gate` above: this diff
    # made `hooks/completion_gate.sh` and `triage._bucket_context` read a scalar `stop_gate` block
    # for its plain intent, so leaving `_block`'s coercion here would make THIS row the one reader
    # reporting `off` for a gate that is genuinely refusing session end. `stop_gate` is not
    # org-lockable, but the three readers agreed before this change and must still agree after it.
    sg = gates.get("stop_gate")
    sg_window = sg if isinstance(sg, dict) else {}
    # RAW like `stop_gate` (#258): `work._plan_review_on` reads a scalar leaf for its plain intent.
    pr_gate = gates.get("plan_review")
    par = _block(cfg, "parallel")
    goals_par = _block(par, "goals")           # SIBLING block, slice parallelism's own parent (#1200)
    wk = _block(cfg, "work")
    # #2564 code review: computed ONCE here rather than twice inline in the row below (the row used
    # to call `_model_max_tier(cfg)` a second time just to re-derive the same value for its own
    # ternary -- cheap today, but a future edit to one call site alone would silently desync the
    # label from the value it's paired with, the same drift class every other conditional row in
    # this list avoids by computing its own state once). `_tier_configured` is the raw KEY presence,
    # not the resolved value -- an explicit `"model_selection_max_tier": "opus"` (or a malformed
    # value that _model_max_tier falls back from) must not be mislabeled "(default)" just because
    # it resolves to the same tier an absent key would.
    _tier = _model_max_tier(cfg)
    _tier_configured = bool(str(cfg.get("model_selection_max_tier") or "").strip())
    rows = [
        ("model+effort auto-selection",
         "AUTO (per-goal `resolve` + per-step `resolve-step`)"
         if (cfg.get("model_selection") or "off") == "auto" else "off",
         'config: "model_selection": "auto"'),
        # #2564: the ceiling is ON by default and absent from every config written before it
        # existed (templates are applied once at /sigma-init and never re-synced), so without a row
        # here the opt-out is a key no existing install has ever seen. Duplicated rather than
        # imported: skills do not import each other's Python, the same rule that keeps the router
        # reachable only through `predict.py`'s own CLI.
        ("model tier price ceiling",
         "%s%s" % (_tier, "" if (_tier_configured or _tier != "opus")
                   else " (default — fable unreachable)"),
         'config: "model_selection_max_tier": "haiku|sonnet|opus|fable"'),
        ("machine-checked done (verify.enforce)",
         "ON — `record done` refused without fresh `loop.py verify` evidence"
         if _enforce_enabled(verify) else "off (prose gate only)",
         'config: "verify": {"enforce": true}'),
        ("hard plan-gate (deny source edits w/o fresh plan)",
         _hard_plan_gate_state(base, cfg, gate, gate_window, wk),
         'config: "gates": {"hard_plan_gate": {"enabled": true}}'),
        ("Stop gate (refuse to end a session with unplanned source)",
         f"ON ({_effective_window(sg_window)}h window)" if _gate_enabled(sg) else "off",
         'config: "gates": {"stop_gate": {"enabled": true}}'),
        # #258: enforced only inside `work.py pr`, so work off means ON-in-config but inert here.
        ("plan-review gate (PR push needs an approving review of the exact plan)",
         ("off" if not _gate_enabled(pr_gate)
          else "ON — `work.py pr` refuses a plan with no approving `record-plan-review` record for "
               "its exact bytes (every host)" if _work_enabled(wk)
          else "ON in config but NOT ENFORCED here — work.enabled is off, so `work.py pr` never runs"),
         'config: "gates": {"plan_review": {"enabled": true}}'),
        ("decision gate (deny edits that break a registered invariant)",
         _decision_gate_state(base, cfg),
         "in an adopted repo (.sdlc/config.json), author .sdlc/decisions.json (see /sigma-decide) — authoring it is the opt-in"),
        ("decision tier (advisory park-detail classifier)",
         "AUTO — needs_decision/irreversible/unknown parks get an autonomous/escalate_l1/escalate_l0 "
         "tier (decision_tier.py), surfaced in the ledger, review-queue.md and the park comment"
         if (cfg.get("decision_tier") or "off") == "auto" else "off",
         'config: "decision_tier": "auto"  (advisory only — never changes what the loop DOES)'),
        ("pipeline report card + propose",
         "DECLARED (.sdlc/pipeline.json present)" if (base / "pipeline.json").exists() else "not declared",
         "declare stages in .sdlc/pipeline.json, then: pipeline.py card .sdlc"),
        ("budgets",
         "iterations=%s minutes=%s tokens=%s codex_raw=%s" % (
             budget.get("max_iterations") or "off",
             budget.get("max_minutes") or "off",
             ("%s (priced Claude-equivalent or explicit spend)" % budget["max_tokens"])
             if budget.get("max_tokens") else "off",
             budget.get("max_codex_raw_tokens") or "off"),
         'config: "budget": {"max_minutes": N, "max_tokens": N, '
         '"max_codex_raw_tokens": N} (Codex counts measured phases only)'),
        ("budget enforcement (is max_tokens actually being fed? #2515)",
         _budget_enforcement_state(base, cfg),
         'detection only -- enforcement is `phase_report.py cmd_end` feeding '
         '`state.add_tokens` directly with each phase\'s real cost-equivalent tokens, '
         'unconditional on the journal being on; this row corroborates that via the '
         'journal\'s phase/spend events when it IS on, and says so honestly when it is not'),
        ("hand-off (context bound, resets the session after N goals)",
         _handoff_row_state(cfg),
         'config: "handoff": {"enabled": false}  (or set "after_goals")'),
        ("prompt-gate scope",
         "GLOBAL (env override)" if _legacy().getenv("SIGMA_GATE_GLOBAL") == "1"
         else "repo-scoped (speaks only where .sdlc/ exists)",
         "env SIGMA_GATE_GLOBAL=1 restores always-on"),
        ("SessionStart policy brief",
         "ON — injects the SDLC brief + install self-check at session start"
         if _block(cfg, "session_start").get("enabled") is True else "off",
         'config: "session_start": {"enabled": true}'),
        ("knowledge graph",
         "enabled" if _block(cfg, "knowledge_graph").get("enabled") is True else "off",
         'config: "knowledge_graph": {"enabled": true}'),
        ("backlog source",
         _block(cfg, "discovery").get("source") or "local-goals",
         'config: "discovery": {"source": "github"}'),
        ("pick-path board gating (queue_source)",
         _pick_path_gate_state(_block(cfg, "discovery"), run)
         if _block(cfg, "discovery").get("source") == "github"
         else "n/a -- backlog source is not github; there is no board/label queue distinction",
         'config: "discovery": {"github": {"project": {"queue_source": "status"}}} to make board '
         'columns (Ready/Blocked/etc.) authoritative for picking'),
        ("auto-unpark sweep (#1129)",
         _auto_unpark_feature_state(cfg),
         'config: "discovery": {"auto_unpark": {"mode": "on"}} — github discovery mode only'),
        ("blocking-priority picker override (#1352)",
         _blocking_priority_override_feature_state(cfg),
         'config: "discovery": {"blocking_priority_override": true} — github discovery mode only'),
        ("team ledger",
         _ledger_feature_state(base, cfg),
         'config: "ledger": {"enabled": true}, then /sigma-ledger to create + push it'),
        ("daemon stop-files (state/*.stop)",
         _stop_file_state(base),
         "delete the named file under .sdlc/state/ to let that daemon run again"),
        ("ledger watcher (keeps the shared ledger pushed)",
         _ledger_watcher_state(base, cfg),
         'config: "ledger": {"enabled": true, "watch": {"interval_seconds": 900, '
         '"log_max_bytes": 1048576}}'),
        ("ledger autowatch",
         _autowatch_feature_state(cfg, scheduled_tasks_dir),
         'config: "ledger": {"autowatch": {"enabled": true}} (see .sdlc/config.json\'s '
         '"_autowatch" key for the full schema), then wire a Desktop scheduled task or '
         'channel_webhook_url'),
        ("slack-commands listener (inbound Slack commands)",
         _slack_commands_listener_state(base, cfg),
         'config: "slack_commands": {"enabled": true, "channel_id": "C..."} — see '
         'SLACK_COMMANDS.md for the full one-time setup'),
        ("journal (local event records)",
         _journal_feature_state(base, cfg),
         'config: "journal": {"enabled": true} — or an organisation-managed lock on '
         '`journal.enabled`, which beats the config either way. Written to .sdlc/events/, '
         'local and gitignored; nothing publishes it anywhere'),
        ("local action-log trace (per-goal audit trail, #2741)",
         "ON — file/model-choice/dispatch/note calls persist to .sdlc/state/log/<goal>.jsonl "
         "(read via /sigma-log)"
         if _block(cfg, "action_log").get("enabled") is True else "off",
         'config: "action_log": {"enabled": true}'),
        ("dispatch compliance (agent_dispatch/agent_done vs ledger phase boundaries, #1779)",
         _dispatch_compliance_state(base, cfg),
         'detection only, never enforcement (#1703) -- needs the journal on ("journal": '
         '{"enabled": true}, or an org lock) to have anything to cross-reference, and '
         'SKILL.md\'s own `loop.py log agent_dispatch/'
         'agent_done --role phase` calls to be worth anything once it does'),
        ("dispatch model log coverage — phase (not observed host model, #2514)",
         _dispatch_model_compliance_state(base, cfg),
         'detection only, --role phase dispatches only -- enforcement lives in actionlog.append(), '
         'which now refuses a `--role phase` agent_dispatch with no --model at log time; this row '
         'catches anything that reached the log another way (a hand-forced line, a pre-#2514 '
         'log). Needs the journal on ("journal": {"enabled": true}) to have anything to '
         'cross-reference. '
         'This row checks log presence only; phase_report.py end verifies observed models when '
         'the host exposes a readable transcript. See the slice row below for --role slice (#2557)'),
        ("dispatch model log coverage — slice (not observed host model, #2514/#2521, #2557)",
         _dispatch_model_compliance_slice_state(base, cfg),
         'detection only -- enforcement lives in actionlog.append(), which refuses a `--role '
         'slice` agent_dispatch with no --model at log time (#2514/#2521). Needs '
         '"action_log": {"enabled": true} to have anything to read -- NOT the journal: this row has '
         'no ledger cross-reference (no execution-time ledger signal exists for slices), see its '
         'own docstring. --role goal-slot is deliberately not covered: nothing requires --model '
         'there on main today (#2557)'),
        ("rate card prices the models this repo used",
         _rate_card_coverage_state(base),
         'add the observed model to skills/sigma-loop/rates/anthropic_list_prices.csv; '
         'unpriced turns are warned at every phase end and never count toward budget.max_tokens'),
        ("pre-work backlog cross-check",
         _backlog_check_state(cfg),
         'config: "backlog_check": {"enabled": true}'),
        ("pre-work oversized-goal classifier",
         _goal_decompose_state(cfg),
         'config: "goal_decompose": {"enabled": true}'),
        ("no-dangling-goal unit classification (#2429)",
         _no_dangling_goal_state(base, cfg),
         'config: "discovery": {"no_dangling_goal": {"enabled": true, "core": "<a registered unit '
         'name>"}} — requires .sdlc/features/; add "live_judge": {"enabled": true} for a real, '
         'metered model call backing tiers 1/3'),
        ("slice parallelism",
         ("ON — up to %s concurrent slices per wave" % par.get("max_concurrent", 3))
         if par.get("enabled") is True else "off (a goal's slices run one after another)",
         'config: "parallel": {"enabled": true, "max_concurrent": 3}'),
        ("goal parallelism",
         # A SIBLING of the "slice parallelism" row above, same shape, deliberately read the same
         # inline way (not cross-loaded via loop.goals_parallel()) so the two adjacent rows never
         # drift in style — #1200 gap 1: this is the setting that actually governs how many goals a
         # run dispatches concurrently, and doctor said nothing about it. 3 matches
         # loop.py's own DEFAULT_GOALS_MAX_CONCURRENT (== slices.py's DEFAULT_MAX_CONCURRENT, by
         # loop.py's own comment) -- not re-derived here to avoid a cross-load for one constant.
         ("ON — up to %s concurrent goals per wave" % goals_par.get("max_concurrent", 3))
         if goals_par.get("enabled") is True else "off (goals run one after another, no worktree fan-out)",
         'config: "parallel": {"goals": {"enabled": true, "max_concurrent": 3}}'),
        ("per-goal worktree + PR",
         "ON — a worktree/branch/PR per goal; verify runs in it"
         if wk.get("enabled") is True else
         "off — the loop writes NOTHING to git: a done goal's changes stay in your working tree, no PR",
         'config: "work": {"enabled": true}  (or run /sigma-init)'),
        ("runtime dirs ignored via",
         _ignore_mechanism(pathlib.Path(base).parent),
         "run /sigma-setup (or setup.py ignore .) — never clobbers an ignore rule you already set"),
        ("auto-merge a clean AND safe PR",
         _automerge_state(wk),
         'config: "work": {"auto_merge": "protected"}  (off | protected | always)'),
        ("feature-branch rebase upkeep (#144 refuses a replay that would delete content)",
         _rebase_upkeep_state(base, wk),
         'config: "work": {"rebase_upkeep": "off"} to stop it; a BLOCKED unit clears on the next '
         'clean pass -- see docs/branching-model.md §3b'),
        ("PR review gate (independent of branch protection)",
         _review_gate_state(wk),
         'config: "work": {"require_review": "approval"}  (off | changes | approval)'),
        ("independent review (advisory; the code cannot prove who reviewed)",
         _review_independence_state(cfg, sdlc_dir),
         'config: "review": {"independent": true, "host": "auto", "command": "", "timeout_seconds": 900}'),
        ("skill selection vs platform built-ins",
         "advisory — a plugin can't disable a built-in; Sigma prefers its own skills via sharp "
         "descriptions + per-skill resolution headers (no runtime API to detect a live conflict)",
         'if a standalone built-in shadows a Sigma skill: settings.json "skillOverrides": '
         '{"<name>": "off"}; if it is a plugin: /plugin disable <plugin>'),
    ]
    # #2729 (D24) shipped no compatibility layer; #239 superseded that: `legacy.getenv` now reads a
    # `*_env` value naming the previous prefix (and prefers its SIGMA_* spelling when set), so this
    # row is informational, and its remedy is the one-shot migration that now ships.
    _legacy_env = _legacy_env_names_in_config(cfg)
    rows.append((
        "legacy env-var names in config",
        "; ".join("legacy env-var name in config: %s=%s; rename to its SIGMA_* spelling" % (k, v)
                  for k, v in _legacy_env) if _legacy_env else "none",
        "run: python3 skills/sigma-doctor/scripts/migrate.py .sdlc (dry run), then --apply; "
        "and rename the environment variable it names to match",
    ))
    return rows


USAGE = "usage: doctor.py check [sdlc_dir] | features [sdlc_dir] | hygiene [sdlc_dir] [repo_root]"


def main(argv):
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    if len(argv) >= 2 and argv[1] == "features":
        for name, state, enable in features(argv[2] if len(argv) > 2 else ".sdlc"):
            print(f"  {name}: {state}\n      enable/change: {enable}")
        return 0
    if len(argv) >= 2 and argv[1] == "check":
        checks = check(argv[2] if len(argv) > 2 else ".sdlc")
        gaps = [c for c in checks if not c["ok"]]
        for c in checks:
            # #1513 review fix: show `fix` whenever it's non-empty, not just when `ok` is False.
            # Every row built via _chk() already guarantees fix=="" when ok=True ("fix": "" if ok
            # else fix), so this is a no-op for every pre-existing row -- it only changes display
            # for a hand-built row that deliberately carries advisory text alongside ok=True.
            # THE RULE OUTLIVED ITS EXAMPLE: the row it was written for moved out of this file in
            # #2575 and no row here carries an ok=True `fix` today, but the rule is _chk-independent
            # and costs nothing, and the next hand-built row that needs it should not have to
            # rediscover that check() computed the advisory correctly and the printer dropped it
            # (confirmed live by Review, not read from a diff -- see PR discussion #1513).
            print(f"  [{'OK ' if c['ok'] else 'MISSING'}] {c['name']}" + (f"  ->  {c['fix']}" if c["fix"] else ""))
        print(f"\nsigma-doctor: {len(checks) - len(gaps)}/{len(checks)} ready"
              + ("." if not gaps else f"; {len(gaps)} need the one-liner shown above."))
        print("\nfeatures (doctor.py features for the enable one-liners):")
        for name, state, _ in features(argv[2] if len(argv) > 2 else ".sdlc"):
            print(f"  {name}: {state}")
        _print_hygiene(argv[2] if len(argv) > 2 else ".sdlc")
        return 0
    if len(argv) >= 2 and argv[1] == "hygiene":
        # `None`, not ".", when no repo root is given: let hygiene() derive it from the .sdlc path
        # so the boundary shares an origin with the docs rather than with the caller's cwd (#577).
        rows = hygiene(argv[2] if len(argv) > 2 else ".sdlc", argv[3] if len(argv) > 3 else None)
        if not rows:
            print("  no standing docs to scan (.sdlc/project.md, .sdlc/context/*.md)")
            return 0
        for c in rows:
            print(f"  [{'OK  ' if c['ok'] else 'STALE'}] {c['name']}" + ("" if c["ok"] else f"\n      {c['fix']}"))
        return 0
    print(USAGE, file=sys.stderr)
    return 2


def _print_hygiene(sdlc_dir):
    """Surfaced inside `check` so the rot scan actually runs — a maintenance command nobody
    remembers to type is the failure mode this exists to avoid. Kept in its own section: setup
    readiness and content rot are different questions and must not share a score."""
    rot = [c for c in hygiene(sdlc_dir) if not c["ok"]]
    if rot:
        print("\nstanding-doc hygiene (doctor.py hygiene for detail):")
        for c in rot:
            print(f"  [STALE] {c['name']}\n      {c['fix']}")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
