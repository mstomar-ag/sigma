#!/usr/bin/env python3
"""#2336 (slice 1 of Epic #2335, design #2329): the inbound Slack
commands listener's FOUNDATION -- Socket Mode connection, the channel-membership authorization
gate, the structured command-grammar parser, a real `--help`, lifecycle scaffolding
(pidfile/heartbeat/stop-file/log), and the `slack_commands` config block.

#2337 (slice 2) wires up the two READ-ONLY commands, `--drift` and `--list [page]` -- both answered
directly by the listener, in-process, with no headless drive and no claim/worktree machinery
(Component C, "Called on-demand, directly by the listener ... rather than only on a TTL tick").
`--drift` reuses `drift_watch.py`'s own two-signal sweep primitives (`_open_units`, `_commit_delta`,
`_pr_status`, `_summarize_unit` -- BR-19) directly, never `sweep()` itself: `sweep()`'s own
TTL-due/ledger-dedup/configured-channel machinery exists for the PASSIVE tick's own "post once per
window to the drift-watch channel" problem, which an on-demand Slack reply -- answered every time,
straight back to whichever channel the command arrived on -- does not have. `--list [page]` reuses
`feature_registry.read_index` directly (BR-18, the design's own explicit citation for this command),
the same chart-sheet read `drift_watch._open_units` itself already uses for the identical open-unit
enumeration question.

#2340 (slice 4, Component D) wires up `--rebase <name>` for real, dispatched to a real, headless
drive -- see "`--rebase <name>` (#2340, slice 4, Component D)", below, for what it concretely does.

#2341 (slice 5, Component D's REVISED text -- "verify-and-report only, never a landing action", the
round-1 REJECT fix that narrowed this command's whole scope) wires up `--merge <name>` for real --
see "`--merge <name>` (#2341)", below, for what it concretely does and why it never spends a real
driven Claude session to do it.

WHY A NEW DEPENDENCY (D-3, Component A). Python's stdlib has no websocket client at any version, so
the zero-dependency posture `channel_notify.py`/`slack_client.py` both hold cannot extend to a
persistent, bidirectional Socket Mode connection. `slack_sdk` (Slack's own official SDK) is the
dependency this design accepts, installed via a documented, one-time `pip install
"slack_sdk[socket-mode]"` (see `SLACK_COMMANDS.md`, D-3) -- there is no requirements.txt/pyproject.toml
mechanism for `skills/sigma-loop/scripts/` in this repo to declare it in (only a private package's
pyproject exists, a different, off-limits lane), mirroring the CLI/Channels adapter's own `bun install`
precedent exactly.

SLACK_SDK IS LAZILY IMPORTED, ON PURPOSE. Every function in this module EXCEPT `_build_client` and
`_socket_mode_response_cls` is fully testable, right now, on a machine with no `slack_sdk` installed
at all -- config gating, the channel gate, the whole command parser, the lifecycle scaffolding, and
`_on_request`'s own event-handling logic (exercised with a plain duck-typed fake request/client, DI
via `response_cls`/`client_factory`, mirroring `slack_client.post_message`'s own `post=` parameter).
Only the two functions that construct a REAL `slack_sdk` object ever import it, and only at call
time -- so importing this module, or running its test suite, never requires the dependency to be
installed. A live Socket Mode connection cannot be exercised in this automated suite regardless (no
real Slack app token in CI); it is mocked here the same way `test_slack_client.py` mocks the network
POST, and is validated for real only once a human has followed `SLACK_COMMANDS.md` and supplied a
real App-Level Token.

CHANNEL-MEMBERSHIP GATE, NOT A USER ALLOWLIST (decision #2, locked in via a comment on #2329). A
command is authorized if and only if it arrived on the ONE configured `slack_commands.channel_id`
-- never "the token was valid," never a per-user allowlist. An unauthorized channel is dropped
silently (log-only, no reply); a malformed command on an AUTHORIZED channel is refused loudly,
quoting every valid form -- the two are deliberately different postures for two different failure
modes (Component A).

DISPATCH INFRASTRUCTURE (#2338, slice 3 of Epic #2335, Components B/H/I of design #2329). The shared
drive/idempotency machinery `--rebase <name>` (#2340) and `--merge <name>` (#2341) both build on --
both are wired to it now. Four pieces, reused rather than reinvented, in the same order Component
H's own write path (`loop.py`'s `_next()`) already uses:

  1. `try_claim`/`finish_claim` -- the SAME `ledger.claim_belongs_to_me` arbitration + `loop.py`'s
     `_claim`/`_try_acquire_claim_lock` write path every ordinary goal claim already uses, keyed on
     `slack-cmd-<name>` (a HYPHEN, not a colon -- round-1 review found a colon makes
     `state.unsafe_goal_reason` raise on every call, silently failing the same-machine flock open),
     with a real, on-demand `sync.py pull`/`publish` at the moment of the check and the moment of
     the write (round-1 review: the passive `ledger.watch.interval_seconds` tick alone left a
     ~900s cross-machine race).
  2. `cut_worktree`/`teardown_worktree` -- the SAME ephemeral, detached-worktree pattern
     `feature_rebase.py`'s own automatic upkeep pass already uses for `name`, under that pass's own
     per-unit rebase lock, cut by the LISTENER itself before any drive is ever launched (round-1
     review: a prompt instruction the driven session might skip is not a guarantee; the isolation
     has to be structural).
  3. `drive_outcome`/`new_completion_marker` -- the same REAL-OUTCOME-VERIFICATION discipline
     #1332 established for `autowatch.py` (a driven subprocess's own exit code is never trusted
     alone), generalised to "did the driven session leave its OWN completion marker in the
     ledger", since this infrastructure does not yet know which specific command dispatched it.
  4. `dispatch` -- ties the three together: claim, cut worktree, drive (`run_drive` defaults to
     `autowatch._run_drive`, a real driven Claude session, registering a REAL liveness marker for
     the driven CHILD's pid via its new `on_spawn` hook -- round-2 review: the claim's own pid is
     the LISTENER's, not the child's, so a listener crash mid-drive must not let a sibling reclaim
     and tear down a worktree an orphaned child is still mutating -- but `run_drive` is an
     injectable seam, not a hardcoded call: `--merge` (#2341, below) supplies its OWN, matching
     `_run_drive`'s exact signature, that never spends a real session at all), check the outcome,
     release the claim, tear the worktree down.

`--rebase <name>` (#2340, slice 4, Component D) is wired to this infrastructure now: `_rebase_reply`
below calls `dispatch(..., command="--rebase", extra_prompt=_REBASE_EXTRA_PROMPT)` and turns the
returned `DispatchResult` into a real Slack reply. The Python in THIS module never runs `git rebase`
itself -- it never even calls `rebase_brief.py`'s own functions directly; `_REBASE_EXTRA_PROMPT`
instructs the DRIVEN session (running inside the isolated worktree `dispatch` already cut for it) to
do that, and to leave a genuine conflict stopped exactly where git left it rather than invoke
`conflict_walk.py`'s interactive walker unattended (BR-15 -- there is no human on the Slack side to
answer its per-file prompts).

`--merge <name>` (#2341). Component D's own round-1 REJECT fix narrowed this command down to
exactly two READ-ONLY calls (BR-10: "`--merge <name>` reuses ONLY `verify_command`/
`run_verify_command` -- read-only, safe unattended") plus `feature_rebase.rebase_stopped` as a
precondition (mirroring `verify_merge.py main()`'s own identical guard). "Safe unattended" is a
LOWER bar than "needs real judgment" (Component B's own title for why `--merge`/`--rebase` dispatch
through a driven session at all) -- so rather than spend a real `claude -p` session just to run a
shell command and read a git ref, `--merge` supplies `dispatch()` its OWN deterministic `run_drive`
(`_merge_run_drive`, below), which runs the check directly, in this process, against the SAME
isolated worktree Component I already cuts. This is what makes the single most important
constraint in this slice -- "`--merge <name>` must NEVER actually land a merge, in any
configuration" (`unit_completion.py`'s own test-pinned human-only-merge boundary, `docs/
branching-model.md` §13) -- genuinely, structurally testable: `ensure_landing_pr`/`merge_pr`/
`verify_and_offer_merge`/`_interactive_decide` (`verify_merge.py`) are never imported by name and
never called anywhere on this path, pinned by a real call-list spy
(`tests/test_slack_commands_listen.py::test_merge_check_never_calls_any_landing_or_merge_function`),
matching `test_unit_completion.py::test_neither_mode_ever_merges_anything`'s own style -- real
handlers installed for every `gh pr merge`/`gh pr create`/`gh pr ready` call so a code path that
reached for one would get an ANSWER, not an import error, and the call list is asserted empty of
all three regardless. A prompt telling an opaque driven session "never do this" would not meet
AGENTS.md's own "run the control, or it's decoration" bar for a guarantee this load-bearing; not
spending a driven session at all does."""
import collections
import importlib.util
import json
import os
import pathlib
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name, directory=None):
    directory = pathlib.Path(directory) if directory else _HERE
    spec = importlib.util.spec_from_file_location(name, directory / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


#: `verify_merge.py` lives in a SIBLING skill's own `scripts/` dir, not this one -- the identical
#: cross-skill load `verify_merge.py` itself already uses for `feature_rebase` (its own
#: `_LOOP_SCRIPTS = _HERE.parent.parent / "sigma-loop" / "scripts"`), mirrored here in the other
#: direction. This loads its OWN separate copy of `feature_rebase` internally (no shared module
#: identity with this file's own `feature_rebase` below) -- harmless: that module holds no
#: process-global mutable state, every lock it takes is file-based (`_acquire`/`_release`).
_REBASE_SCRIPTS = _HERE.parent.parent / "sigma-rebase" / "scripts"

ledger = _load("ledger")
legacy = _load("legacy")   # #239: token variables under the previous prefix
feature_registry = _load("feature_registry")
slack_client = _load("slack_client")
scrub = _load("scrub")
logroll = _load("logroll")              # #460: size-capped rotation for slack-commands.log
drift_watch = _load("drift_watch")
unit_completion = _load("unit_completion")
autowatch = _load("autowatch")          # #2338: _run_drive/_drive_cmd -- the headless drive primitive
feature_upkeep = _load("feature_upkeep")  # #939: the gate, read only through this module
feature_rebase = _load("feature_rebase")  # #2338: worktree cut/lock/teardown (Component I)
sync = _load("sync")                    # #2338: the LEDGER's own ops-branch pull/publish (Component H,
                                         # BR-32) -- NOT feature_rebase's own `feature_sync` (aliased
                                         # `sync` INSIDE that module); this is skills/.../sync.py itself
loop = _load("loop")                    # #2338: _claim/_try_acquire_claim_lock/agent_start/agent_end
verify_merge = _load("verify_merge", _REBASE_SCRIPTS)  # #2341: verify_command/run_verify_command ONLY
                                         # -- see the module docstring's own "--merge <name>" section

DEFAULT_SDLC_DIR = ".sdlc"
#: The real deployed setup: Socket Mode's App-Level Token is added to the SAME Slack App the drift
#: watcher (#2311) already posts through -- not a second, dedicated app -- and this feature reuses
#: that app's own bot token (`SIGMA_SLACK_BOT_TOKEN`) to post replies, so there is no second
#: bot token to create or copy. (design #2329's original decision #4/Doubt D-6 called for a genuinely
#: separate app; the real operator setup is this single-app one instead.)
DEFAULT_APP_TOKEN_ENV = "SIGMA_SLACK_BOT_SOCKET_TOKEN"
DEFAULT_BOT_TOKEN_ENV = "SIGMA_SLACK_BOT_TOKEN"

#: Lifecycle scaffolding filenames, all under `<sdlc_dir>/state/` -- mirrors `watch_daemon.py`'s own
#: `state/watch.{pid,heartbeat,stop}` and `supervise_daemon.py`'s own stop-file/log conventions (Component
#: F, BR-20/BR-22/BR-23), adapted for a process started ONCE by a human/supervisor rather than
#: re-invoked on a tick.
STATE_SUBDIR = "state"
PID_FILENAME = "slack-commands.pid"
HEARTBEAT_FILENAME = "slack-commands.heartbeat.json"
STOP_FILENAME = "slack-commands.stop"
LOG_FILENAME = "slack-commands.log"
LOCK_DIRNAME = "slack-commands.lock"

#: Mirrors `sync.MIN_STALE_AFTER_SECONDS`, the floor of the ledger watcher's own `STALE_AFTER`
#: (`INTERVAL * 3`, floored at 180s -- `sync.stale_after_seconds`, which watch_daemon.py and doctor.py
#: both call since #2490) -- there is no `interval_seconds` for a persistent connection to derive a
#: multiple from, so this listener uses the same floor value directly, the bound sync.py's own comment
#: already argues is a reasonable minimum for "a real slow call still gets a chance before eviction."
DEFAULT_STALE_AFTER_SECONDS = 180

_COMMANDS = ("--drift", "--merge", "--unsafe-merge", "--list", "--rebase", "--help")
VALID_FORMS = ("--drift", "--merge <name>", "--unsafe-merge <name>", "--list [page]",
               "--rebase <name>", "--help")
USAGE = "valid commands: " + " | ".join(VALID_FORMS)

HELP_TEXT = (
    "Sigma Slack commands:\n"
    "  --drift              show the current feature-branch drift summary\n"
    "  --merge <name>       verify <name> is clean and ready to land (never merges it -- land it "
    "yourself)\n"
    "  --unsafe-merge <name>  runs the SAME verify check as --merge, then LANDS it immediately if "
    "it passes -- no confirmation, no review wait. Only the confirmation step is skipped; a "
    "failing verify still blocks the merge, same as --merge. Run at your own discretion. With unit "
    "upkeep on it goes through the landing engine instead and needs a local unit approval first.\n"
    "  --list [page]        list open feature units, 10 per page (default page 1)\n"
    "  --rebase <name>      rebase <name> onto its base; reports and stops on a real conflict\n"
    "  --help               show this message\n"
    "<name> must be a currently OPEN unit in .sdlc/features/ -- an unrecognized or closed name is "
    "refused with the real list of open units. Any command also works as a mention, e.g. "
    "`@ls-bot --help`, with identical replies."
)

_PLACEHOLDER = (
    "recognized `%s` -- dispatch for this command isn't wired up in this slice yet (Epic #2335's "
    "later slices handle it). Nothing has run."
)

#: Shared between `_open_units_clause` (a `--merge`/`--rebase` refusal) and `--list`'s own empty
#: reply (#2337) -- one string, not two independently-drifting copies of the same fact.
NO_OPEN_UNITS_MESSAGE = "No open units are currently registered."

#: `--list [page]`: 10 per page, per design #2329's own slice-count estimate and
#: `SLACK_COMMANDS.md`'s command-grammar table -- not configurable in this slice.
LIST_PAGE_SIZE = 10

#: `_on_request` is the one function that ever needs this text, but it is also what `run()` prints
#: when a token env var is missing at startup -- one string, not two independently-drifting copies.
_MISSING_DEPENDENCY_MESSAGE = (
    'slack_sdk is not installed. Run: pip install "slack_sdk[socket-mode]" -- see '
    "skills/sigma-loop/SLACK_COMMANDS.md for the full one-time setup."
)


# --------------------------------------------------------------------------- config & gating


def _slack_commands_config(config):
    return (config or {}).get("slack_commands") or {}


def app_token_env(config):
    """The env var NAME (never a literal secret, `pass_env` convention, BR-25) holding the
    App-Level Token (`connections:write`) -- `slack_commands.app_token_env`, default
    `SIGMA_SLACK_BOT_SOCKET_TOKEN`."""
    value = _slack_commands_config(config).get("app_token_env")
    return value if isinstance(value, str) and value.strip() else DEFAULT_APP_TOKEN_ENV


def bot_token_env(config):
    """The env var NAME holding the bot token used to post replies -- the SAME token the drift
    watcher already uses (`SIGMA_SLACK_BOT_TOKEN`), reused rather than a second, dedicated
    app's own token -- `slack_commands.bot_token_env`, default `SIGMA_SLACK_BOT_TOKEN`."""
    value = _slack_commands_config(config).get("bot_token_env")
    return value if isinstance(value, str) and value.strip() else DEFAULT_BOT_TOKEN_ENV


def channel_id(config):
    """The one configured automation channel id, or `None` when unset/blank -- `None` means "not
    configured," never "any channel is fine."""
    value = _slack_commands_config(config).get("channel_id")
    return value if isinstance(value, str) and value.strip() else None


def enabled(config):
    """Strict `is True` on `slack_commands.enabled` (never a truthy string, matching every other
    opt-in watch-tick's own idiom) AND a real `channel_id` configured -- Component A's own gate
    needs a real value to compare an inbound event's channel against regardless of what else is
    set, so an `enabled: true` with no `channel_id` is exactly as inert as `enabled: false`."""
    return _slack_commands_config(config).get("enabled") is True and channel_id(config) is not None


# --------------------------------------------------------------------------- channel gate (Component A)


def is_authorized_channel(event_channel_id, config):
    """Component A's channel-membership gate: the ONLY question asked is "did this arrive on the
    one configured channel id" -- never "is the token valid" (Slack already answered that by
    delivering the event at all) and never a per-user allowlist (decision #2, explicitly out of
    scope for v1). An unconfigured `channel_id` refuses everything, rather than silently trusting
    whatever channel an event happens to name."""
    expected = channel_id(config)
    return expected is not None and event_channel_id == expected


# --------------------------------------------------------------------------- mention support (#2353)
#
# `@ls-bot --help` alongside a plain `--help`. Slack fires an `app_mention` event AND a separate
# `message.channels`/`message.groups` event for the SAME message when the bot is mentioned in a
# channel it's in -- `_on_request` answers the `app_mention` copy only (below), and skips the
# `message` copy of anything opening with this bot's own mention, so it is answered exactly once.
# Slash commands (`/ls-bot ...`) are a different Slack mechanism entirely and are explicitly OUT of
# scope here -- see #2353: the channel-membership authorization gate above doesn't extend to them
# (a slash command can be typed from any channel a user is in), a separate decision, not bundled in.


def _leading_mention_id(text):
    """The user id inside a LEADING `<@USERID>` mention token (Slack's own fixed mention shape), or
    None if `text` doesn't open with one. No regex needed -- the shape never varies."""
    if not text or not text.startswith("<@"):
        return None
    end = text.find(">")
    return text[2:end] or None if end != -1 else None


def _strip_leading_mention(text, bot_user_id):
    """Strip a LEADING `<@bot_user_id>` mention token (plus one following space, if present) from
    `text`. Only strips a mention of THIS bot, specifically at the very start -- matches
    `parse_command`'s own 'first token only' structured grammar (module docstring, Doubt D-2): a
    mention appearing mid-sentence, or naming someone else, is left alone and the raw text is
    refused normally as an unrecognized command, never guessed at."""
    mentioned = _leading_mention_id(text)
    if bot_user_id is None or mentioned != bot_user_id:
        return text
    rest = text[text.find(">") + 1:]
    return rest[1:] if rest.startswith(" ") else rest


# --------------------------------------------------------------------------- command grammar (Component A)


class CommandError(Exception):
    """A malformed inbound command. `.message` is the exact, reply-ready refusal text."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


ParsedCommand = collections.namedtuple("ParsedCommand", ["command", "name", "page"])


def _parse_list_page(rest):
    if len(rest) > 1:
        raise CommandError("--list takes at most one page number. %s" % USAGE)
    if not rest:
        return 1
    try:
        page = int(rest[0])
    except ValueError:
        raise CommandError("--list's page must be a whole number, got %r. %s" % (rest[0], USAGE))
    if page < 1:
        raise CommandError("--list's page must be 1 or greater, got %d. %s" % (page, USAGE))
    return page


def _open_units_clause(sdlc_dir):
    registry = feature_registry.read(feature_registry.registry_dir(sdlc_dir))
    names = sorted(name for name, entry in registry.items() if entry.get("open") is True)
    if names:
        return "Open units: %s" % ", ".join(names)
    return NO_OPEN_UNITS_MESSAGE


def parse_command(text, sdlc_dir=DEFAULT_SDLC_DIR):
    """Structured-only parsing (Doubt D-2: no natural-language understanding, by design) of one
    inbound Slack message into a `ParsedCommand`, or raises `CommandError` carrying the exact
    reply-ready refusal text.

    `shlex.split` (Component A), so a quoted unit name round-trips. The FIRST token must be exactly
    one of the five valid forms; an unknown flag, a missing/extra argument, or free-form language
    are all refused the SAME way -- quoting every valid form verbatim, never a silent no-op.

    `--merge <name>`/`--unsafe-merge <name>`/`--rebase <name>`: `<name>` must resolve against a
    real, currently-OPEN unit in
    `.sdlc/features/index.json` (`feature_registry.resolve_open_unit`, BR-18) -- an unrecognized (or
    CLOSED -- `resolve_open_unit` answers the same `None` for both, deliberately, per its own
    docstring) name is refused with the real list of open unit names, never fuzzy-matched or
    guessed. The registry's OWN spelling of the name is what gets carried forward, not the caller's
    casing -- the same rule `resolve_open_unit` already applies to itself."""
    try:
        tokens = shlex.split(text or "")
    except ValueError as exc:
        raise CommandError("could not parse that command (%s). %s" % (exc, USAGE))
    if not tokens:
        raise CommandError("empty command. %s" % USAGE)
    head, rest = tokens[0], tokens[1:]
    if head not in _COMMANDS:
        raise CommandError("unrecognized command %r. %s" % (head, USAGE))
    if head in ("--help", "--drift"):
        if rest:
            raise CommandError("%s takes no arguments. %s" % (head, USAGE))
        return ParsedCommand(head, None, None)
    if head == "--list":
        return ParsedCommand("--list", None, _parse_list_page(rest))
    # --merge / --unsafe-merge / --rebase: exactly one <name>, resolved against a real open unit.
    if len(rest) != 1:
        raise CommandError("%s needs exactly one unit name. %s" % (head, USAGE))
    resolved = feature_registry.resolve_open_unit(sdlc_dir, rest[0])
    if resolved is None:
        raise CommandError("%r is not a known open unit. %s" % (rest[0], _open_units_clause(sdlc_dir)))
    return ParsedCommand(head, resolved, None)


def _format_unit_line(name, entry):
    """One `--list` row: `- <name> — <title> (owner: <x>, priority: <y>)`. Missing `title` drops
    the em-dash clause entirely rather than printing an empty one; missing `owner`/`priority` get
    honest, explicit defaults ("unowned"/"unprioritised") -- `None` is a real, distinct fact
    (`feature_registry.normalise_entry`'s own docstring: "Absent is absent"), not the same as a
    printed blank, so this names it rather than leaving a ragged gap in the line."""
    title = entry.get("title") or ""
    owner = entry.get("owner") or "unowned"
    priority = entry.get("priority") or "unprioritised"
    clause = " — %s" % title if title else ""
    return "- %s%s (owner: %s, priority: %s)" % (name, clause, owner, priority)


def _list_reply(sdlc_dir, page):
    """`--list [page]` (Component C, BR-18): a paginated, 10-per-page read of every OPEN unit in
    `.sdlc/features/index.json`, via `feature_registry.read_index` directly -- the design's own
    explicit citation (design #2329: "paginated `feature_registry.read_index`, 10/page"),
    and the same chart-sheet read `drift_watch._open_units` already uses for the identical
    enumeration question, so `--drift` and `--list` agree on what "open" means without either
    reimplementing the other's read.

    A page past the real last one is REFUSED with the true page count, never a silent empty reply
    -- the same "refuse loudly rather than degrade" idiom `parse_command`'s own grammar already
    applies to a malformed command (module docstring)."""
    registry = feature_registry.read_index(feature_registry.registry_dir(sdlc_dir))
    units = sorted((name, entry) for name, entry in registry.items() if entry.get("open") is True)
    if not units:
        return NO_OPEN_UNITS_MESSAGE
    total = len(units)
    total_pages = -(-total // LIST_PAGE_SIZE)          # ceil division, no float rounding
    if page > total_pages:
        return ("page %d requested, but there %s only %d page%s of %d open unit%s. Try `--list %d`."
                 % (page, "is" if total_pages == 1 else "are", total_pages,
                    "" if total_pages == 1 else "s", total, "" if total == 1 else "s", total_pages))
    start = (page - 1) * LIST_PAGE_SIZE
    page_units = units[start:start + LIST_PAGE_SIZE]
    header = "Open feature units (page %d of %d, %d total):" % (page, total_pages, total)
    lines = [header] + [_format_unit_line(name, entry) for name, entry in page_units]
    return "\n".join(lines)


def _drift_reply(sdlc_dir, config, run=None):
    """`--drift` (Component C, BR-19): the ON-DEMAND version of `drift_watch.py`'s own two-signal
    sweep -- `_open_units`, `_commit_delta`, `_pr_status`, `_summarize_unit` called directly, the
    exact same read-only primitives the passive tick calls, never reimplemented here. Deliberately
    does NOT call `sweep()` itself: that function's own `enabled()`/`due()`/`_already_posted()`/
    `_channel_id()` machinery exists to solve the PASSIVE tick's problem (post a summary to the
    drift-watch channel at most once per TTL window, from possibly many independent watchers) --
    problems an on-demand command answered straight back to the channel it arrived on, gated by
    `slack_commands.enabled` alone (Component A), does not share. So this command runs regardless
    of whether `drift_watch.enabled` is even turned on.

    Three honest non-drift replies, never a silent empty string (a Slack command always gets SOME
    reply): no open units at all, `work.base` unconfigured (nothing to compare against), or every
    open unit already current. A unit whose branch could not be read at all (`_commit_delta`
    returning `None` -- e.g. never pushed) is skipped exactly as `sweep()` already skips it, not
    reported as drifted."""
    units = drift_watch._open_units(sdlc_dir)
    if not units:
        return NO_OPEN_UNITS_MESSAGE
    work_settings = config.get("work") if isinstance(config, dict) else None
    work_settings = work_settings if isinstance(work_settings, dict) else {}
    base = (feature_rebase.state_safe_ref("work.base", work_settings.get("base")) or "").strip()
    if not base:
        return "work.base is not configured -- nothing to compare against."

    runner = run or drift_watch._run
    cwd = str(pathlib.Path(sdlc_dir).parent)
    remote = (feature_rebase.state_safe_ref("work.remote", work_settings.get("remote")) or "").strip() or "origin"
    drift_watch._fetch(runner, cwd, remote, [base] + [branch for _, branch in units])

    repo_ref = owner_ref = None
    reports = []
    for name, branch in units:
        delta = drift_watch._commit_delta(runner, cwd, remote, base, branch)
        if delta is None or delta["count"] == 0:
            continue                      # current, or unreadable -- neither is worth a gh call
        if repo_ref is None:
            repo_ref, owner_ref = unit_completion._repo_ref(config, runner, cwd, remote)
        pr = drift_watch._pr_status(runner, cwd, repo_ref, owner_ref, branch)
        reports.append({"unit": name, "branch": branch, "delta": delta, "pr": pr})

    if not reports:
        return "No drift -- %d open unit(s) are all current with `%s`." % (len(units), base)
    return "*Branch drift detected:*\n\n%s" % drift_watch._summarize(reports)


def build_reply(parsed, config=None, sdlc_dir=DEFAULT_SDLC_DIR, run=None, run_drive=None,
                 session_pid=None, requester=None):
    """The reply text for a CLEANLY parsed command. `--help`, `--drift`, `--list` (#2337), `--merge`
    (#2341), `--unsafe-merge` (#2359, an explicit operator follow-up to #2341) and `--rebase`
    (#2340) all get real replies now -- Epic #2335's whole command surface is wired up.

    `config`/`sdlc_dir`/`run` are read by `--drift`/`--list`/`--rebase`; `run_drive` is read by
    `--merge`, `--unsafe-merge`, and `--rebase`; `session_pid` is read by `--rebase` alone, threaded
    straight to `dispatch` (#2338) (`--help` ignores all of them) -- kept as trailing
    optional/defaulted arguments so every existing `--help`-only call site is unaffected. `run`/
    `run_drive`/`session_pid` are DI for tests (`--drift`'s own git/gh calls; `--merge`'s/
    `--unsafe-merge`'s own `dispatch()` call; `--rebase`'s own worktree cut and headless drive),
    mirroring every other injectable seam in this kit; production code always omits them and gets
    the real `drift_watch._run`/`feature_rebase._run`/`autowatch._run_drive`/`os.getpid()`."""
    if parsed.command == "--help":
        return HELP_TEXT
    if parsed.command == "--drift":
        return _drift_reply(sdlc_dir, config or {}, run=run)
    if parsed.command == "--list":
        return _list_reply(sdlc_dir, parsed.page)
    if parsed.command == "--merge":
        return _merge_reply(sdlc_dir, config or {}, parsed.name, run_drive=run_drive)
    if parsed.command == "--unsafe-merge":
        return _unsafe_merge_reply(sdlc_dir, config or {}, parsed.name, run_drive=run_drive,
                                   requester=requester)
    if parsed.command == "--rebase":
        return _rebase_reply(sdlc_dir, config or {}, parsed.name, run=run, run_drive=run_drive,
                              session_pid=session_pid)
    raise AssertionError("unreachable: unknown parsed command %r" % (parsed.command,))


def handle_message_event(event, config, sdlc_dir=DEFAULT_SDLC_DIR, run=None, run_drive=None,
                          session_pid=None):
    """Pure, network-free handling of one inbound Slack message event -- fully unit-testable with
    no `slack_sdk` installed at all (a real `--merge`/`--rebase` dispatch's own headless drive is
    itself injected via `run_drive`, so this stays true even for those commands -- see
    `_merge_reply`/`_rebase_reply`). `event` is the plain dict shape a real Slack `message` event
    carries (`channel`, `text`, `user`, ...); this function reads only `channel` and `text`.
    `run`/`run_drive`/`session_pid` are DI for tests, threaded straight through to `build_reply`'s
    own `--drift`/`--merge`/`--rebase` handling.

    -> `(authorized, reply_text_or_None, parsed_or_None)`:
      * unauthorized channel -> `(False, None, None)` -- log-only, no reply (Component A).
      * authorized, bad grammar -> `(True, <refusal text>, None)`.
      * authorized, parsed cleanly -> `(True, <reply text>, ParsedCommand)`."""
    if not is_authorized_channel(event.get("channel"), config):
        return False, None, None
    try:
        parsed = parse_command(event.get("text") or "", sdlc_dir)
    except CommandError as exc:
        return True, exc.message, None
    return True, build_reply(parsed, config, sdlc_dir, run=run, run_drive=run_drive,
                              session_pid=session_pid, requester=event.get("user")), parsed


def handle_app_mention_event(event, bot_user_id, config, sdlc_dir=DEFAULT_SDLC_DIR, run=None,
                              run_drive=None, session_pid=None):
    """Pure, network-free handling of one inbound `app_mention` event (#2353) -- identical to
    `handle_message_event` in every respect (same authorization gate, same grammar, same replies)
    except the text has its leading `<@bot_user_id>` mention stripped first, via
    `_strip_leading_mention`. Delegates straight to `handle_message_event` on a shallow copy of
    `event` with `text` replaced, so the two paths can never drift apart in behavior."""
    text = _strip_leading_mention(event.get("text") or "", bot_user_id)
    mention_event = dict(event)
    mention_event["text"] = text
    return handle_message_event(mention_event, config, sdlc_dir, run=run, run_drive=run_drive,
                                 session_pid=session_pid)


# --------------------------------------------------------------------------- lifecycle (Component F)


def state_dir(sdlc_dir):
    return pathlib.Path(sdlc_dir) / STATE_SUBDIR


def pid_path(sdlc_dir):
    return state_dir(sdlc_dir) / PID_FILENAME


def heartbeat_path(sdlc_dir):
    return state_dir(sdlc_dir) / HEARTBEAT_FILENAME


def stop_path(sdlc_dir):
    return state_dir(sdlc_dir) / STOP_FILENAME


def log_path(sdlc_dir):
    return state_dir(sdlc_dir) / LOG_FILENAME


def lock_dir_path(sdlc_dir):
    return state_dir(sdlc_dir) / LOCK_DIRNAME


def stop_requested(sdlc_dir):
    """`.sdlc/state/slack-commands.stop` -- mirrors `supervise_daemon.py`'s own stop-file convention
    (BR-20). A human (or a supervisor) `touch`es this path; the listener notices it between polls
    and disconnects cleanly."""
    return stop_path(sdlc_dir).exists()


def _atomic_write_text(path, text):
    """Temp file in the SAME directory, then `os.replace` -- mirrors
    `feature_registry._atomic_write_text` exactly, so a reader never observes a half-written
    heartbeat or pidfile."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:                     # noqa: S110 - cleanup must not mask the original failure
            pass
        raise


#: Set by the SIGTERM/SIGHUP handler (#424) so a Socket Mode request thread cannot recreate the
#: heartbeat between the handler's release and the process dying; cleared by the uninstall.
_DYING = False


def write_heartbeat(sdlc_dir):
    """`.sdlc/state/slack-commands.heartbeat.json`: `{"pid": <int>, "last_seen": <epoch seconds>}`
    -- exactly the two fields Component F specifies, mirroring `watch_daemon.py`'s own
    `$STATE/watch.heartbeat` freshness idiom (BR-22) rather than inventing a fourth liveness shape.
    Written at startup, on EVERY confirmed Socket Mode request (`_on_request`, any type -- a ping/
    hello/disconnect frame is as much "still alive" as an actual command), and once more right
    before the connect-time log line."""
    if _DYING:      # the signal handler has released our markers; a request thread must not recreate one
        return
    _atomic_write_text(heartbeat_path(sdlc_dir),
                        json.dumps({"pid": os.getpid(), "last_seen": time.time()}))


def heartbeat_liveness(sdlc_dir, stale_after_seconds=DEFAULT_STALE_AFTER_SECONDS, now=None):
    """`("live"|"stale"|"absent", age_seconds|None)` for THIS listener's own heartbeat (#2396) --
    the SAME three-way, age-based shape `sync.watcher_liveness` already uses for the ledger
    watcher, reused rather than a fresh two-way alive/dead check invented for this command. AGE,
    NOT EXISTENCE (#1227, `sync.watcher_liveness`'s own docstring): a dead listener's pidfile can
    be handed to an unrelated live process by the kernel, so only the heartbeat's own age can tell
    a live listener apart from a dead one. `absent` covers every reason the heartbeat cannot be
    trusted at all -- missing, unreadable, malformed JSON, or a non-numeric/boolean `last_seen` --
    never folded into `stale`: absent means no listener has ever recorded one here (the remedy is
    START), which is a different fact from one that recorded one and then stopped (the remedy is
    RESTART) -- `ensure()`, below, acts on exactly that distinction. Never raises."""
    try:
        data = json.loads(heartbeat_path(sdlc_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ("absent", None)
    last_seen = data.get("last_seen")
    if not isinstance(last_seen, (int, float)) or isinstance(last_seen, bool):
        return ("absent", None)
    age = (time.time() if now is None else now) - last_seen
    return (("live" if age < stale_after_seconds else "stale"), age)


def _heartbeat_fresh(sdlc_dir, stale_after_seconds):
    return heartbeat_liveness(sdlc_dir, stale_after_seconds)[0] == "live"


def _read_pid(sdlc_dir):
    try:
        return int(pid_path(sdlc_dir).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


#: (#2396) How long a losing racer must see the lock dir sit with NO pidfile inside it before
#: treating that as proof of a genuine crash rather than a live winner an instant past its own
#: `os.mkdir`. `acquire_single_instance`'s own ORIGINAL docstring argued this function never needed
#: `watch_daemon.py`'s second, nested reclaim-mutex layer because "this listener is started AT MOST ONCE by
#: a human or a supervisor" -- #2396's own `ensure()` (below) is exactly what ends that guarantee: a
#: CLI meant to be run repeatedly, possibly by a cron entry and a human at once, is a real "several
#: near-simultaneous launches" caller this function's own reasoning said could not exist. Confirmed
#: empirically, not theorised: a 20-racer burst (real `os.fork`ed-equivalent OS processes, via
#: `subprocess.Popen`, genuinely concurrent) against the code as it stood before this constant
#: existed produced two simultaneous winners in 1 of 20 rounds -- the exact double-start this whole
#: function exists to prevent, caused by a second racer reading `_read_pid` in the narrow window
#: after the FIRST racer's `os.mkdir` had already succeeded but before its own `_atomic_write_text`
#: had run, and concluding (wrongly) that an empty pidfile meant a crashed prior holder rather than
#: a live one mid-write. Mirrors `watch_daemon.py`'s own `$MUTEX.reclaim` age gate (30s, guarding that
#: script's much shorter-lived decide-mutex) at a smaller bound sized to THIS function's own critical
#: section -- two local, no-network disk writes, not a `git`/`gh` round trip -- while staying wide
#: relative to real scheduler jitter: the same 20-racer burst produced zero double-wins across 20
#: repeated rounds once this gate was added. #2751 closed the NEXT window (pidfile written,
#: heartbeat not yet) by writing the heartbeat first, and clears a crashed holder's leftover pidfile
#: before either write; the reclaim `rmdir`/`mkdir` race between two stale-judging racers remains
#: (see #2751 plan §7).
_LOCK_RECLAIM_GRACE_SECONDS = 5


def _lock_dir_age_seconds(lock_dir, now=None):
    """Seconds since `lock_dir` (the single-instance mutex directory) was created, or `None` if it
    cannot be stat'd (already gone -- a sibling's own cleanup won the read). AGE, not existence --
    the same #1227 lesson `heartbeat_liveness`/`sync.watcher_liveness` already apply to a heartbeat,
    applied here to the lock directory itself."""
    try:
        mtime = lock_dir.stat().st_mtime
    except OSError:
        return None
    return (time.time() if now is None else now) - mtime


def acquire_single_instance(sdlc_dir, stale_after_seconds=DEFAULT_STALE_AFTER_SECONDS):
    """One listener per `.sdlc`, at a time (Component F: "A single-instance guard on start uses
    Python's own atomic `os.mkdir` (raises `FileExistsError` on collision) -- the same POSIX
    primitive `watch_daemon.py`'s own mutex (BR-23) is built on, applied without re-deriving that file's
    own hard-won TOCTOU lesson.").

    ONE atomic `os.mkdir` on the SAME directory, held for the process's whole lifetime, still does
    all the work: `os.mkdir` raises `FileExistsError` on a live sibling's own held directory exactly
    as it would on a genuine concurrent racer, so the property that matters -- at most one caller's
    `os.mkdir` call can ever succeed against a given path -- always holds. What #2396 added is the
    `_LOCK_RECLAIM_GRACE_SECONDS` gate right below: a losing racer that finds the lock held but no
    pidfile written YET must not treat that absence alone as proof of a crash (see that constant's
    own comment for the empirical double-win this closes) -- it is exactly as ambiguous as a
    momentarily-missing heartbeat, and gets the same "refuse loudly, do not guess" treatment.

    #2751 closed the window that gate could not see: with the pidfile written BEFORE the heartbeat,
    a racer arriving between the two writes read a live pid and no heartbeat and reclaimed (a real
    double-start, 2/20 rounds under load). The heartbeat is now written first, and a crashed
    holder's leftover pidfile is removed before either write, so a racer that can read our pid
    always finds a fresh heartbeat, and one that finds no pid always hits the age gate. The cost is
    a crash between the two writes leaving a fresh heartbeat with no pidfile: `ensure()`, `status`
    and doctor then read "running" (pid None) for at most `DEFAULT_STALE_AFTER_SECONDS` before the
    heartbeat's own age tells the truth, and the next acquire reclaims after the grace window.

    -> `(True, None)` once the lock dir, pidfile and heartbeat are all ours.
    -> `(False, reason)` when a live sibling (same machine, live pid via `ledger.pid_alive` -- the
    same primitive `loop.py`/`work.py` already trust for this exact question -- and a heartbeat
    fresher than `stale_after_seconds`) already holds it, when a sibling's lock is too young to
    tell apart from one still finishing its own startup write, or when a stale holder's reclaim
    loses a race. Never raises -- refuse loudly, do not crash, matching every other gate in this
    kit."""
    sdir = state_dir(sdlc_dir)
    sdir.mkdir(parents=True, exist_ok=True)
    lock_dir = lock_dir_path(sdlc_dir)
    try:
        os.mkdir(lock_dir)
    except FileExistsError:
        existing_pid = _read_pid(sdlc_dir)
        if (existing_pid is not None and ledger.pid_alive(existing_pid)
                and _heartbeat_fresh(sdlc_dir, stale_after_seconds)):
            return False, ("slack_commands: already running (pid %d) -- refusing a second listener "
                            "for %s" % (existing_pid, sdlc_dir))
        if existing_pid is None:
            age = _lock_dir_age_seconds(lock_dir)
            if age is None or age < _LOCK_RECLAIM_GRACE_SECONDS:
                # The lock exists but nothing has been written into it yet -- structurally
                # indistinguishable, on evidence alone, from "the current holder is mid-startup, an
                # instant past its own os.mkdir" versus a genuinely abandoned lock, until enough
                # time has passed that even a slow, contended host could have finished writing its
                # own pidfile by now. Never reclaim on a hunch (see _LOCK_RECLAIM_GRACE_SECONDS).
                return False, "slack_commands: a sibling is already starting -- back off"
        # Stale: the prior holder's pid is dead, its heartbeat has gone quiet past the bound, or its
        # lock has sat with no pidfile at all past _LOCK_RECLAIM_GRACE_SECONDS -- longer than any
        # live holder still inside its own startup window could plausibly need.
        # Reclaim exactly once -- a genuinely live sibling that loses THIS race gets the identical
        # honest refusal a racer against a freshly-created lock would, never a silent double-start.
        try:
            os.rmdir(lock_dir)
        except OSError:
            pass
        try:
            os.mkdir(lock_dir)
        except FileExistsError:
            return False, "slack_commands: a sibling is already starting -- back off"
    # A leftover pidfile from a crashed holder must not be readable while we hold the lock but
    # have not yet written our own markers (see _LOCK_RECLAIM_GRACE_SECONDS): with it gone, a racer
    # reads "no pid" and hits the grace gate instead of judging a dead pid and reclaiming. Guarded
    # because this function never raises -- a permission error here is not worth a crash.
    try:
        pid_path(sdlc_dir).unlink(missing_ok=True)
    except OSError:
        pass
    # Heartbeat BEFORE pidfile (#2751): a racer that can read our pid is then guaranteed to find a
    # fresh heartbeat and refuse; a racer that finds no pid hits the grace gate. Never the reverse.
    write_heartbeat(sdlc_dir)
    _atomic_write_text(pid_path(sdlc_dir), str(os.getpid()))
    return True, None


def _owns_markers(sdlc_dir):
    """True when the markers on disk are THIS process's. The pidfile is the identity, except in the
    window `acquire_single_instance` leaves on purpose -- heartbeat written, pidfile not yet (#2751) --
    where our own pid in the heartbeat is the only proof. A successor's heartbeat names the successor,
    so it is never claimed here."""
    pid = _read_pid(sdlc_dir)
    if pid is not None:
        return pid == os.getpid()
    # FRESH only: a stale heartbeat of ours with no pidfile is a stalled holder a successor is mid-way
    # through reclaiming, whose lock dir we must not rmdir out from under it.
    if heartbeat_liveness(sdlc_dir)[0] != "live":
        return False
    try:
        return json.loads(heartbeat_path(sdlc_dir).read_text(encoding="utf-8")).get("pid") == os.getpid()
    except (OSError, ValueError, AttributeError):
        return False


def release_single_instance(sdlc_dir):
    """Best-effort, idempotent cleanup. Ownership-checked -- the same ownership rule `watch_daemon.py`
    applies in its own cleanup -- so a delayed cleanup from a process that has already been superseded
    (its own reclaim lost, or it is simply exiting late) can never rip the lock, pidfile or heartbeat
    out from under a live successor.

    Reached from `run()`'s `finally` on a normal exit AND from the SIGTERM/SIGHUP handler
    `_install_signal_cleanup` installs (#424): those signals terminate the interpreter without
    unwinding any `finally`, which used to strand all three markers."""
    if not _owns_markers(sdlc_dir):
        return
    for path in (pid_path(sdlc_dir), heartbeat_path(sdlc_dir)):
        try:
            path.unlink()
        except OSError:
            pass
    try:
        os.rmdir(lock_dir_path(sdlc_dir))
    except OSError:
        pass


def _install_signal_cleanup(sdlc_dir):
    """SIGTERM/SIGHUP cleanup (#424), modelled on `watch_daemon.py::_install_cleanup`: run
    `release_single_instance`, restore the default disposition, then re-raise the signal at ourselves
    so the exit status is still death-by-signal. Returns an `uninstall()` that restores the previous
    handlers (a no-op where nothing was installed).

    * Release runs BEFORE the default is restored, so a second signal mid-release re-enters a handler
      that is idempotent and ownership-checked rather than killing us half-cleaned.
    * `getattr(signal, name, None)`: SIGHUP does not exist on Windows. A signal already ignored
      (`nohup`-style launcher) stays ignored. SIGINT is untouched: KeyboardInterrupt already unwinds.
    * POSIX only in effect: Windows never delivers SIGTERM to a Python handler (it is
      `TerminateProcess`), so there the markers still age out via the heartbeat; the install is harmless.
    * Main thread only (`signal.signal` raises elsewhere); in-process callers elsewhere get a no-op.
    * Installed BEFORE `acquire_single_instance` so no signal lands in the acquire window with the
      default disposition. The one residual is the instruction span between `os.mkdir` and the heartbeat
      write inside acquire: a lock dir with no heartbeat and no pidfile, which the next acquire reclaims
      after `_LOCK_RECLAIM_GRACE_SECONDS`.
    * Residuals, all self-healing and none a wrong deletion: a request thread already inside
      `_atomic_write_text` when the handler runs can leave one stale heartbeat (it ages out); a signal
      after the heartbeat unlink but before the `rmdir` leaves the lock dir (reclaimed after
      `_LOCK_RECLAIM_GRACE_SECONDS`).
    * No `atexit`: `run()`'s `finally` already covers every unwinding exit, and a per-call registration
      would leak across in-process callers.
    * A main thread stuck inside a C call that never checks signals defers the handler; SIGKILL stays
      the lever there, and its markers age out via the heartbeat (`heartbeat_liveness`)."""
    if threading.current_thread() is not threading.main_thread():
        return lambda: None

    def _on_signal(signum, _frame):
        global _DYING
        _DYING = True
        try:
            release_single_instance(sdlc_dir)
        finally:
            signal.signal(signum, signal.SIG_DFL)
            try:
                os.kill(os.getpid(), signum)     # die BY the signal, as before this handler existed
            finally:
                os._exit(128 + signum)           # never resume a process whose markers are gone

    previous = {}
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            old = signal.getsignal(sig)
            if old == signal.SIG_IGN:
                continue
            signal.signal(sig, _on_signal)
            previous[sig] = old
        except (ValueError, OSError):
            continue

    def uninstall():
        global _DYING
        _DYING = False
        for sig, old in previous.items():
            try:
                signal.signal(sig, old if old is not None else signal.SIG_DFL)
            except (ValueError, OSError, TypeError):
                pass
        previous.clear()
    return uninstall


def _utc_now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _log(sdlc_dir, message):
    """One timestamped, scrubbed line -- appended to `.sdlc/state/slack-commands.log` (mirrors
    `watch_daemon.py`'s own `$STATE/watch.log`) and echoed to stderr for foreground visibility. Never
    raises: a log write failing must not crash the listener over its own housekeeping. #460: the
    file is rolled to `.1`..`.3` at its cap before the append (`logroll`), so it is bounded."""
    text = scrub.scrub(message) if message else message
    line = "%s slack_commands: %s" % (_utc_now_iso(), text)
    print(line, file=sys.stderr)
    try:
        path = log_path(sdlc_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        logroll.append(path, line + "\n")
    except OSError:
        pass


#: A driven session's raw stdout can be arbitrarily long (a full transcript, in the worst case) --
#: capped for `_log` (#2355) at a length generous enough to carry a real error message plus useful
#: surrounding context without turning one dispatch into a multi-megabyte log line. The TAIL is
#: kept, not the head: an error is almost always the LAST thing a crashing process prints.
_LOG_STDOUT_MAX_CHARS = 4000


def _truncate_for_log(stdout):
    text = str(stdout or "").strip()
    if not text:
        return "(empty)"
    if len(text) <= _LOG_STDOUT_MAX_CHARS:
        return text
    return "...(truncated, showing last %d of %d chars)... %s" % (
        _LOG_STDOUT_MAX_CHARS, len(text), text[-_LOG_STDOUT_MAX_CHARS:])


# --------------------------------------------------------------------------- claim arbitration (Component H)


#: The claim key's own prefix -- a HYPHEN, not a colon (round-1 REJECT fix, finding 1: a colon
#: makes `state.unsafe_goal_reason` raise on every single call this key reaches -- `_claim_lock_path`,
#: `_agent_marker_path`, `actionlog.log_path` all run that same check -- which silently fails the
#: same-machine flock (Component H step 3) open for every unit name, forever). `features._UNIT_RE`
#: permits only `[A-Za-z0-9._-]` in a real unit name, so `slack-cmd-<name>` can never collide with
#: an ordinary numeric goal claim (always digits-only) or a real unit's own claim.
CLAIM_PREFIX = "slack-cmd-"

#: The ledger kinds `ledger._held_full` recognises as CLOSING a lease -- `finish_claim` below may
#: only ever be called with one of these, or the claim it is meant to release stays open until the
#: TTL backstop instead.
_TERMINAL_CLAIM_KINDS = ("done", "failed", "parked", "release")


def claim_key(name):
    """The ledger claim key for a Slack-triggered `--merge`/`--rebase <name>` -- the feature UNIT's
    own name (Component H's 2026-09-10 amendment), not a synthetic per-command id and not a
    `(command, name)` pair: `--rebase <name>` and a later `--merge <name>` against the SAME unit
    both mutate the same branch, so they must serialise against EACH OTHER, not only a second copy
    of themselves."""
    return CLAIM_PREFIX + str(name)


ClaimResult = collections.namedtuple("ClaimResult", ["ok", "key", "holder_actor", "holder_writer"])


def try_claim(sdlc_dir, config, name, session_pid=None):
    """Component H's write path, REUSED, not reimplemented, from `loop.py`'s own `_next()`
    ordering (loop.py:1878-1975): pull the shared ledger live first (round-1 REJECT fix, finding 2
    -- `sync.py pull` on demand, not the passive `ledger.watch.interval_seconds` tick), read the
    lease (`loop._lease`, the SAME primitive `_next()` itself uses), apply `claim_belongs_to_me`
    corroborated by the driven-child liveness marker (`loop._goal_has_registered_worker`, the
    PER-GOAL-ONLY check `_resume_blocked_by_a_live_sibling` already passes here too -- never the
    OR'd, goal-agnostic `_claimed_goal_has_live_worker`), then the same-machine flock
    (`loop._try_acquire_claim_lock`), then write the claim's LOCAL half only (`mark=False` -- this
    key is never a real GitHub issue `mark_in_progress` could label) and publish it immediately so
    another machine's next `pull()` sees it within one git round trip, not the next tick.

    -> `ClaimResult(ok=True, key, None, None)` on a winning claim.
    -> `ClaimResult(ok=False, key, holder_actor, holder_writer)` when a DIFFERENT, still-live
       writer already holds it -- for a "who's handling it" reply naming who.
    -> `ClaimResult(ok=False, key, None, None)` when a local sibling won the SAME-instant race (the
       ledger shows no holder at all yet -- there is nobody to name).

    NEVER RAISES: every read/write here is already individually fail-open (`loop._lease`,
    `ledger.safe_append` via `loop._claim`, `loop._try_acquire_claim_lock`); `sync.py pull`/
    `publish` are the two exceptions (their OWN `git fetch`/`add`/`commit` calls can raise), so
    both are wrapped here rather than trusted to degrade on their own."""
    key = claim_key(name)
    try:
        sync.pull(sdlc_dir, config)
    except Exception:                       # noqa: BLE001 - best-effort; the ledger read below is local
        pass
    me, my_writer, lease = loop._lease(sdlc_dir, config)
    holder_actor, holder_writer = lease.get(key, (None, None))
    if holder_actor and not ledger.claim_belongs_to_me(
            holder_actor, holder_writer, me, my_writer,
            live_worker_check=lambda: loop._goal_has_registered_worker(sdlc_dir, key, config)):
        return ClaimResult(False, key, holder_actor, holder_writer)
    lock_fd = loop._try_acquire_claim_lock(sdlc_dir, key)
    if lock_fd is None:
        return ClaimResult(False, key, None, None)   # a local sibling won this exact instant
    try:
        loop._claim(sdlc_dir, None, key, config,
                    session_pid=session_pid if session_pid is not None else os.getpid(), mark=False)
        try:
            sync.publish(sdlc_dir, config)
        except Exception:               # noqa: BLE001 - best-effort; the local claim is already durable
            pass
    finally:
        loop._release_claim_lock(lock_fd)
    return ClaimResult(True, key, None, None)


def finish_claim(sdlc_dir, config, key, kind, **fields):
    """Component H step 6: the claim's own TERMINAL ledger entry -- `kind` must be one of
    `_TERMINAL_CLAIM_KINDS`, the set `ledger._held_full` recognises as closing a lease, or the
    claim this is meant to release stays open until the TTL backstop instead. Also clears the
    driven-child liveness marker (`loop.agent_end`, round-2 REJECT fix -- a marker must never
    outlive the command it was registered for) and publishes both writes immediately (`sync.py
    publish`) so the release reaches every OTHER machine's next `pull()` promptly, the same
    synchronous step that already writes the outcome reply to Slack.

    NEVER RAISES: every step here is individually best-effort, matching every other terminal-write
    site in this kit -- a release that could not fully complete must never crash the caller that
    is trying to report an outcome."""
    if kind not in _TERMINAL_CLAIM_KINDS:
        raise ValueError("finish_claim: %r is not a lease-closing kind (%s)"
                          % (kind, ", ".join(_TERMINAL_CLAIM_KINDS)))
    try:
        ledger.safe_append(sdlc_dir, kind, key, config=config, **fields)
    except Exception:                   # noqa: BLE001 - safe_append already never raises; belt only
        pass
    try:
        loop.agent_end(sdlc_dir, key)
    except Exception:                   # noqa: BLE001 - agent_end already never raises; belt only
        pass
    try:
        sync.publish(sdlc_dir, config)
    except Exception:                   # noqa: BLE001 - best-effort; the local terminal write already landed
        pass


# --------------------------------------------------------------------------- outcome verification (Component B)


def _entries_for_key(sdlc_dir, key):
    try:
        return [e for e in ledger.read_all(sdlc_dir) if str(e.get("goal")) == str(key)]
    except Exception:                   # noqa: BLE001 - an unreadable ledger has no entries to report
        return []


def _entry_ids_for(sdlc_dir, key):
    """A "before" snapshot of every ledger entry id already recorded for `key` -- mirrors
    `autowatch._terminal_outcome_ids`'s own pattern (#1332), so a caller can tell a marker the
    driven session writes DURING this run apart from one that already existed (the claim itself,
    written by `try_claim` moments earlier)."""
    return {e.get("id") for e in _entries_for_key(sdlc_dir, key)}


def new_completion_marker(sdlc_dir, key, before_ids):
    """The MOST RECENT ledger entry recorded for `key` that did not exist in `before_ids`, or
    `None` if none exists yet -- `read_all` returns entries oldest-first, so the last match is the
    driven session's latest word, not merely its first. `None` is the honest answer when the
    driven session never wrote one at all."""
    marker = None
    for entry in _entries_for_key(sdlc_dir, key):
        if entry.get("id") not in before_ids:
            marker = entry
    return marker


def drive_outcome(sdlc_dir, key, exit_code, before_ids):
    """#1332's own REAL-OUTCOME-VERIFICATION discipline (Component B step 4), generalised: the
    driven subprocess's own exit code is never trusted alone. -> `(state, marker_or_None)`:

      * a new ledger entry for `key` exists -> its own `kind`, when that is one this dispatcher
        recognises as terminal (`"done"|"failed"|"parked"`), is TRUSTED over the exit code -- the
        driven session's own record IS the outcome. Any other kind is `"unclear"` (evidence
        something happened, but not conclusively what).
      * no such entry, `exit_code == 0` -> `"unclear"` -- #1332's own repro case exactly: a
        subprocess that exits 0 having done real work but never reached its own terminal record.
        Reported as "unclear -- check the repo directly," never as a false "done".
      * no such entry, `exit_code != 0` -> `"failed"` -- a nonzero exit is real evidence on its
        own, marker or not."""
    marker = new_completion_marker(sdlc_dir, key, before_ids)
    if marker is not None:
        kind = marker.get("kind")
        return (kind if kind in ("done", "failed", "parked") else "unclear"), marker
    return ("failed" if exit_code != 0 else "unclear"), None


# --------------------------------------------------------------------------- isolated worktree (Component I)


class WorktreeBusy(Exception):
    """`name`'s own rebase-upkeep lock (`feature_rebase.lock_path`) is already held -- an ordinary
    pick's automatic upkeep for a goal in this unit, or a second in-flight dispatch for the SAME
    unit. Nothing was touched; the caller's own claim (if any) is still held and must be released."""


class WorktreeUnavailable(Exception):
    """The ephemeral worktree could not be cut: the remote tip could not be read, or `git worktree
    add` failed twice (Component I). The lock (if taken) has already been released before this is
    raised -- see `cut_worktree`."""


def cut_worktree(sdlc_dir, config, name, run=None):
    """Component I: the LISTENER's OWN precondition to `_run_drive`, never a prompt instruction the
    driven session might skip or misread (round-1 REJECT fix, finding 4 -- "run the control, or
    it's decoration"). Cuts (or recovers) the SAME ephemeral worktree `feature_rebase.py`'s own
    automatic upkeep pass already uses for `name` -- `.sdlc/state/rebase/<unit>` -- under that
    pass's own per-unit rebase lock (`feature_rebase.lock_path`), so a Slack-triggered dispatch and
    this repo's OWN ordinary automatic upkeep for a goal in the SAME unit cannot physically collide
    over one checkout, on top of Component H's own ledger-claim arbitration.

    Checked out at `sha` (the remote tip) then given a LOCAL branch named `feature/<name>` --
    structurally, here, not left as a bare `--detach` (#2355: a detached worktree leaves only the
    remote-tracking ref resolvable, and `rebase_brief.py`'s `assemble_brief`/`attempt_rebase`
    (#2340's own consumers) need `branch` itself to resolve for their `merge-base`/`rev-list`
    comparisons -- confirmed by direct reproduction that a bare `--detach` checkout crashes there
    with `fatal: Not a valid object name`). The branch names the exact commit already checked out;
    it moves nothing and cannot diverge from `sha`.

    -> `(path, lock_fd)` on success -- the lock stays HELD for the whole dispatch; `teardown_worktree`
    below is the paired release, called by the SAME caller once the drive returns, success or not.

    Raises `WorktreeBusy` (the lock is already held -- nothing on disk was touched) or
    `WorktreeUnavailable` (a git failure cutting the checkout itself). Either way the lock is
    released before the exception propagates, so a failed cut never leaks it."""
    run = run or feature_rebase._run
    project_root = str(pathlib.Path(sdlc_dir).resolve().parent)
    remote = feature_rebase._remote(config)
    lock_path = feature_rebase.lock_path(sdlc_dir, name)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = feature_rebase._acquire(lock_path, timeout=feature_rebase.LOCK_TIMEOUT)
    if lock_fd is None:
        raise WorktreeBusy(
            "%s's own rebase-upkeep lock is already held -- an ordinary pick's automatic upkeep, "
            "or another in-flight dispatch for this unit" % name)
    try:
        path = feature_rebase.worktree_path(sdlc_dir, name)
        if path.exists():
            # WITH the lock held, an existing worktree cannot be another pass's -- stale by
            # construction (mirrors `_upkeep`'s own "drop any stale worktree found at this path on
            # entry, under lock, before any early return").
            feature_rebase._drop_worktree(run, project_root, path)
        branch = feature_rebase.features.BRANCH_PREFIX + name
        sha = feature_rebase.remote_tip(run, project_root, remote, branch)
        if sha is None:
            raise WorktreeUnavailable(
                "could not read %s's remote tip on %s -- the remote may be unreachable" % (branch, remote))
        try:
            run(project_root, ["git", "worktree", "add", "--detach", str(path), sha])
        except Exception as first:      # noqa: BLE001 - one retry, behind a full drop (mirrors _rebase_feature)
            feature_rebase._drop_worktree(run, project_root, path)
            try:
                run(project_root, ["git", "worktree", "add", "--detach", str(path), sha])
            except Exception as second:                                 # noqa: BLE001
                raise WorktreeUnavailable(
                    "git worktree add failed twice for %s: %s / %s" % (name, first, second)) from second
        # A LOCAL branch named `branch` -- structurally, here, rather than left to a prompt
        # instruction the driven session might skip or misread (this function's OWN stated
        # principle, above: "never a prompt instruction... round-1 REJECT fix, finding 4").
        # `--detach` alone leaves ONLY the remote-tracking ref resolvable in this worktree;
        # `rebase_brief.py`'s `assemble_brief`/`attempt_rebase` (#2340's own consumers) call
        # `git merge-base <branch> ...`/`git rev-list --count <branch>..` and need `branch`
        # (`feature/<name>`) to resolve as a real ref -- confirmed by direct reproduction (#2355):
        # `git merge-base feature/<name> main` raises `fatal: Not a valid object name` against a
        # bare `--detach` checkout. `checkout -B` here just NAMES the commit already checked out
        # (`sha`, above) -- it moves nothing and cannot diverge from it. `-B`, not `-b`:
        # `teardown_worktree` removes the WORKTREE, never the local branch ref this creates, so a
        # second dispatch for the same unit finds that ref still there -- `-B` resets it to the
        # fresh `sha` either way, rather than failing on "branch already exists".
        run(str(path), ["git", "checkout", "-B", branch])
        return path, lock_fd
    except Exception:
        feature_rebase._release(lock_fd)
        raise


def teardown_worktree(sdlc_dir, path, lock_fd, run=None):
    """The paired release for `cut_worktree` -- torn down the SAME way `_drop_worktree` does
    (`git worktree remove --force`, then `rmtree`, then `git worktree prune`), called from the same
    caller's `finally` so a crash on either side of the drive still leaves nothing stranded; the
    SAME recovery-on-next-entry behaviour `cut_worktree` already performs is the backstop if even
    this teardown is skipped by a process killed outright. Releasing the lock always runs, even if
    the drop itself raises."""
    run = run or feature_rebase._run
    project_root = str(pathlib.Path(sdlc_dir).resolve().parent)
    try:
        feature_rebase._drop_worktree(run, project_root, path)
    finally:
        feature_rebase._release(lock_fd)


# --------------------------------------------------------------------------- dispatch (Components B/H/I)


def _dispatch_prompt(command, name, worktree_path, key, extra_prompt=""):
    """Component I step 4: names the command, argument and worktree path explicitly (defense in
    depth, matching every other SDLC phase's own convention) -- the STRUCTURAL guarantee is `cwd`
    itself (Component I's own fix), not this sentence, but a driven session that reads it gets the
    full picture regardless, including the completion-marker contract `drive_outcome` reads back."""
    base = (
        "A Slack command `%s %s` was dispatched by the inbound-commands listener (Epic #2335, "
        "design #2329). Work ONLY inside the isolated worktree already cut for you at "
        "`%s` -- a real checkout of `feature/%s`'s own remote tip, on a local branch of that exact "
        "name. NEVER touch the shared root checkout. Call the underlying functions directly (e.g. "
        "`attempt_rebase`/`run_verify_command`/`feature_rebase.rebase_stopped` with `cwd=%r`), "
        "never either script's own CLI `main()`, which hardcodes a different `cwd`. When you are "
        "genuinely done, write your own completion marker to the ledger for goal %r "
        "(`ledger.safe_append(sdlc_dir, \"done\"|\"failed\", %r, config=config, why=...)`) before "
        "you stop -- that marker, not your own exit code, is what the listener trusts."
        % (command, name, worktree_path, name, worktree_path, key, key)
    )
    return ("%s %s" % (base, extra_prompt)).strip() if extra_prompt else base


DispatchResult = collections.namedtuple(
    "DispatchResult",
    ["state", "detail", "holder_actor", "holder_writer", "exit_code", "stdout", "worktree"])


def dispatch(sdlc_dir, config, name, command, timeout=None, run_drive=None, run=None,
             extra_prompt="", session_pid=None):
    """The shared drive/idempotency machinery every mutating command runs through (#2338).
    `--rebase <name>` (#2340) calls this directly, supplying `command="--rebase"` (for the
    prompt/claim context) and its own `extra_prompt` (`_REBASE_EXTRA_PROMPT`); `--merge <name>`
    (#2341) is the other caller, not wired up yet.

    -> a `DispatchResult`:
      * `state="busy"` -- claimed by someone else (`holder_actor`/`holder_writer` set) or a local
        sibling won the same-instant race (both `None`); nothing was touched. `detail` is `None`
        here -- there is no `why` to report, only who (if known) already holds it.
      * `state="worktree-busy"` -- the unit's OWN rebase-upkeep lock is held; the ledger claim is
        released again (`kind="release"`) -- this dispatch never got far enough to need it.
        `detail` names the lock contention plainly.
      * `state="worktree-unavailable"` -- the remote tip could not be read, or `git worktree add`
        failed twice; the ledger claim is released as `kind="failed"`. `detail` names the git
        failure.
      * `state="done"|"failed"|"parked"` -- the drive returned and left its own completion marker
        naming that outcome; trusted over the exit code (#1332). `detail` is that marker's own
        `why` -- the driven session's own account of what happened, written for exactly this
        purpose (see `_dispatch_prompt`) -- falling back to `"exit code %s"` when the marker
        carries no `why` at all.
      * `state="unclear"` -- the drive exited 0 but left no completion marker (#1332's own repro
        case) -- report this to Slack, never a false "done". `detail` is `"exit code %s"` (or a
        non-terminal marker's own `why`, if one happened to exist).

    NEVER RAISES -- every failure mode above is a returned state, not an exception. Structured in
    two layers, mirroring `feature_rebase.upkeep`'s own split from its `_upkeep` (""never raises"
    has to be total" -- that function's own words):

      1. `try_claim` itself first, OUTSIDE any "close the claim on failure" handler -- it is
         already, individually, fail-open end to end (every read is `loop._lease`'s own fail-open
         contract; every write is `ledger.safe_append`/`loop._try_acquire_claim_lock`, also
         fail-open; `sync.py pull`/`publish`, the two calls that CAN raise, are wrapped inside
         `try_claim` itself). An exception here means nothing was ever claimed, so there is no
         claim for a safety net to close -- closing one anyway would write a stray, unearned
         `failed` entry for a key this process never actually won.
      2. Once a claim is genuinely held, everything after it (`_dispatch`) runs inside a total
         `try/except`: an exception `_dispatch` itself did not anticipate still closes the SAME
         claim this call is holding (as `failed`) rather than leaving it open until the ledger's
         TTL backstop, and is reported as a returned state, never raised."""
    key = claim_key(name)
    try:
        claim = try_claim(sdlc_dir, config, name, session_pid=session_pid)
    except Exception as exc:                # noqa: BLE001 - try_claim is already fail-open; this is
        return DispatchResult("failed", str(exc), None, None, None, None, None)  # the backstop only
    if not claim.ok:
        return DispatchResult("busy", None, claim.holder_actor, claim.holder_writer, None, None, None)
    try:
        return _dispatch(sdlc_dir, config, name, command, key, timeout, run_drive, run, extra_prompt)
    except Exception as exc:                                   # noqa: BLE001 - "never raises" is total
        try:
            finish_claim(sdlc_dir, config, key, "failed", why="dispatch raised: %s" % exc)
        except Exception:                                      # noqa: BLE001 - best-effort even here
            pass
        return DispatchResult("failed", str(exc), None, None, None, None, None)


def _dispatch(sdlc_dir, config, name, command, key, timeout, run_drive, run, extra_prompt):
    try:
        worktree, wt_lock = cut_worktree(sdlc_dir, config, name, run=run)
    except WorktreeBusy as exc:
        finish_claim(sdlc_dir, config, key, "release", why=str(exc))
        return DispatchResult("worktree-busy", str(exc), None, None, None, None, None)
    except WorktreeUnavailable as exc:
        finish_claim(sdlc_dir, config, key, "failed", why=str(exc))
        return DispatchResult("worktree-unavailable", str(exc), None, None, None, None, None)
    try:
        before_ids = _entry_ids_for(sdlc_dir, key)
        drive = run_drive or autowatch._run_drive
        cmd_str = autowatch._drive_cmd(autowatch._autowatch_settings(config))
        prompt = _dispatch_prompt(command, name, str(worktree), key, extra_prompt)
        env = dict(os.environ)
        if not env.get("SIGMA_RUN_ID"):
            env["SIGMA_RUN_ID"] = "slack-cmd-%d-%d" % (os.getpid(), int(time.time()))
        drive_timeout = timeout or autowatch.DEFAULT_DRIVE_TIMEOUT_SECONDS
        exit_code, stdout = drive(
            cmd_str, prompt, str(worktree), env, drive_timeout,
            on_spawn=lambda pid: loop.agent_start(sdlc_dir, key, pid, config))
        state, marker = drive_outcome(sdlc_dir, key, exit_code, before_ids)
        if state != "done":
            # #2355: the driven session's own stdout was captured but never surfaced anywhere --
            # once this process returns, it is gone, and a non-"done" outcome is exactly the case
            # where a human needs it most to diagnose what actually happened. `_log` scrubs it the
            # same way every other logged line is scrubbed (SAFETY: never a raw secret in a log
            # file), and this is local-disk-only -- never relayed to Slack itself (the Slack reply
            # stays `why`/`detail`, never raw driven-session output, which can be arbitrarily long
            # or shaped like a chat transcript rather than a Slack message)."""
            _log(sdlc_dir, "drive stdout for %r (state=%s, exit=%s): %s" % (
                key, state, exit_code, _truncate_for_log(stdout)))
        finish_kind = state if state in ("done", "failed", "parked") else "release"
        why = (marker or {}).get("why") or ("exit code %s" % exit_code)
        finish_claim(sdlc_dir, config, key, finish_kind, why=why)
        # `why` is also the caller's `detail` (#2340) -- the driven session's own account of what
        # happened is the one substantive fact a command's Slack reply has to relay; without this,
        # a caller has no way to tell a clean rebase apart from an already-current one, or to name
        # which files conflicted, short of re-reading the ledger itself.
        return DispatchResult(state, why, None, None, exit_code, stdout, str(worktree))
    finally:
        teardown_worktree(sdlc_dir, worktree, wt_lock, run=run)


# --------------------------------------------------------------------------- --merge <name> (#2341)
#
# Component D's REVISED text (round-1 REJECT fix, finding 3) -- see the module docstring's own
# "`--merge <name>` (#2341)" section for the full argument. In one sentence: `--merge <name>` never
# lands anything, in any configuration, and that guarantee is made structural rather than
# prompt-based by never spending a driven Claude session on this command at all -- the check is
# fully deterministic (BR-10: "read-only, safe unattended"), so it runs directly in the listener's
# own process, against the SAME isolated worktree Component I already cuts for `--rebase`.


def _merge_check(cwd, config):
    """Component D's revised `--merge <name>`, steps 2-5: `feature_rebase.rebase_stopped` first
    (BR-31, mirrors `verify_merge.py main()`'s own identical guard before it ever runs verify), then
    `verify_merge.py`'s verify step ONLY -- `verify_command(config)` then `run_verify_command(cmd,
    cwd)` (BR-10). NEVER calls `ensure_landing_pr`/`merge_pr`/`verify_and_offer_merge`/
    `_interactive_decide` -- not imported by name, not referenced, anywhere in this function; see
    `test_merge_check_never_calls_any_landing_or_merge_function` for the real call-list proof, not
    just an absence-of-reference reading of this docstring.

    -> `(kind, why)`. `kind` is always one of `"parked"|"failed"|"done"` -- the exact set
    `_TERMINAL_CLAIM_KINDS` (minus `"release"`) already recognises as a real completion, so the
    caller can hand it straight to `ledger.safe_append` with no translation. `why` is the ONE
    report string this function's caller both records to the ledger AND replies to Slack verbatim
    -- never reformatted a second time:

      * a rebase is stopped in this worktree -> `"parked"` -- a human's own call (`--rebase <name>`
        or `/sigma-rebase` locally), not this command's to resolve.
      * no `verify.command` configured -> `"failed"` (mirrors `verify_merge.py main()`'s own
        `NO_COMMAND` wording) -- nothing could be checked.
      * `verify.command` ran and failed -> `"failed"`, `verify_merge.format_verify_report`'s own
        PASS/FAIL wording, unmodified.
      * `verify.command` ran and passed -> `"done"` -- verified clean, ready to land; the reply
        says so and tells the human to land it themselves. This command never lands it."""
    run = feature_rebase._run
    if feature_rebase.rebase_stopped(run, cwd):
        return "parked", ("a rebase is stopped here -- resolve it with `--rebase <name>` or "
                           "`/sigma-rebase` locally first, then try `--merge` again.")
    cmd = verify_merge.verify_command(config)
    if not cmd:
        return "failed", "no `verify.command` is configured in `.sdlc/config.json` -- nothing to check."
    result = verify_merge.run_verify_command(cmd, cwd)
    report = verify_merge.format_verify_report(cmd, cwd, result)
    if not result["ok"]:
        return "failed", report
    return "done", (
        "%s\n\nverified clean, ready to merge -- run `/sigma-rebase` locally (step 6) or `gh pr "
        "merge` yourself to land it. This command never lands anything itself, in any "
        "configuration." % report)


def _merge_run_drive(sdlc_dir, config, key):
    """The `run_drive` #2341 supplies to `dispatch()` for `--merge <name>` -- `dispatch()`'s own
    `run_drive` parameter is an injectable seam (its docstring: "`--merge`/`--rebase` ... supplying
    their own `command` word ... and `extra_prompt`"), never hardcoded to `autowatch._run_drive`'s
    real-subprocess default. This matches `_run_drive`'s exact `(cmd_str, prompt, cwd, env, timeout,
    on_spawn=None) -> (exit_code, stdout)` contract (BR-5) so `dispatch()`'s claim/worktree/
    outcome-verification/teardown machinery (#2338) runs completely unchanged underneath it -- it
    just never spends a real driven session, and so never runs anything that COULD type `gh pr
    merge` on its own initiative.

    Writes the SAME completion marker a real driven session is instructed to leave for itself
    (`_dispatch_prompt`'s own contract) -- `ledger.safe_append(sdlc_dir, kind, key, config=config,
    why=why)` -- so `drive_outcome` (#2338) reads this call's own verdict back exactly as it would
    a real driven session's, with no special-casing needed on that side. `stdout` returned here IS
    `why` (there is no subprocess transcript to return instead) -- `DispatchResult.stdout` is what
    `_merge_reply` replies to Slack verbatim, one string, not two independently-drifting copies.

    `on_spawn`, when given (production always gives one -- `_dispatch`'s own liveness-marker hook),
    is called with THIS process's own pid: there is no child process, so the "child" the liveness
    marker names is this same listener process running the check synchronously."""
    def run_drive(cmd_str, prompt, cwd, env, timeout, on_spawn=None):
        if on_spawn is not None:
            on_spawn(os.getpid())
        try:
            kind, why = _merge_check(cwd, config)
        except Exception as exc:            # noqa: BLE001 - a crashed check is a reported failure,
            kind, why = "failed", "the merge check itself crashed: %s" % exc  # never an unreported one
        ledger.safe_append(sdlc_dir, kind, key, config=config, why=why)
        return 0, why
    return run_drive


_MERGE_BUSY_KNOWN = "`--merge %s` is already being handled by %s -- try again once that finishes."
_MERGE_BUSY_UNKNOWN = "`--merge %s` was just claimed by another session -- try again shortly."
_MERGE_WORKTREE_BUSY = ("%s's own rebase worktree is busy right now (an ordinary pick's automatic "
                         "upkeep, or another in-flight command) -- try `--merge %s` again shortly.")
_MERGE_UNCLEAR = "`--merge %s` ran but left no clear outcome -- check the repo directly."

#: #2430, a real gap found live and closed here: every acting command's `unclear` outcome is
#: `drive_outcome`'s OWN documented repro shape for a disabled ledger (`new_completion_marker`
#: reads back an entry `ledger.safe_append` never wrote, because `ledger.enabled` is false -- see
#: that function's own "a ledger problem must never break a run" docstring). A real rebase/merge
#: can genuinely succeed and still report `unclear`, with nothing telling the operator why. This
#: is the ONE line appended to every `unclear` reply, never the busy/worktree-busy/failed ones --
#: those already carry a real, ledger-independent reason.
_LEDGER_OFF_UNCLEAR_NOTE = (
    " `ledger.enabled` is false on this repo, so the completion marker this reply depends on could "
    "never be written -- turn on `\"ledger\": {\"enabled\": true}` in `.sdlc/config.json` for "
    "acting commands (--rebase/--merge/--unsafe-merge) to report a real outcome instead of this.")


def _with_ledger_off_note(config, text):
    """Append `_LEDGER_OFF_UNCLEAR_NOTE` to an `unclear` reply iff the ledger is genuinely off --
    the likely, nameable cause of #2430's own repro, rather than leaving a human to guess. Never
    changes any OTHER reply text; `unclear` is the one outcome this diagnosis actually explains."""
    return text + _LEDGER_OFF_UNCLEAR_NOTE if not ledger.enabled(config) else text


def _merge_reply(sdlc_dir, config, name, run_drive=None):
    """`--merge <name>` (Component D, #2341): dispatches via #2338's shared `dispatch()` -- claim
    arbitration (Component H), the SAME isolated worktree `--rebase` uses (Component I), this
    command's own deterministic `run_drive` (`_merge_run_drive`, above), then teardown -- and turns
    the resulting `DispatchResult` into one honest reply, never a bare "ok"/"not ok" (covers every
    state `dispatch()`'s own docstring documents, so no `AssertionError` fallback is needed here)."""
    key = claim_key(name)
    drive = run_drive or _merge_run_drive(sdlc_dir, config, key)
    result = dispatch(sdlc_dir, config, name, "--merge", run_drive=drive)
    if result.state == "busy":
        if result.holder_actor:
            return _MERGE_BUSY_KNOWN % (name, result.holder_actor)
        return _MERGE_BUSY_UNKNOWN % name
    if result.state == "worktree-busy":
        return _MERGE_WORKTREE_BUSY % (name, name)
    if result.state == "worktree-unavailable":
        return "could not prepare an isolated checkout for `%s`: %s" % (name, result.detail)
    if result.state == "unclear":
        return _with_ledger_off_note(config, _MERGE_UNCLEAR % name)
    # done / failed / parked -- `result.stdout` IS the report (`_merge_run_drive` returns `why` as
    # its own `stdout`). Falls back to `detail` (set when `dispatch()`'s OWN generic exception
    # handler produced this "failed" before `_merge_run_drive` ever ran -- `try_claim`/`_dispatch`
    # raising something neither `WorktreeBusy` nor `WorktreeUnavailable`) rather than the generic
    # "unclear" text, so a real crash reason is never swallowed behind an uninformative reply.
    return result.stdout or ("`--merge %s` failed: %s" % (name, result.detail or "no further detail."))


# --------------------------------------------------------------------------- --unsafe-merge <name> (#2359)
#
# Explicit, deliberate operator request (2026-09-10), after live-testing `--merge`: a SEPARATE,
# unmistakably-named command that actually LANDS the branch -- never a flag on `--merge` itself,
# which keeps its own deliberately human-only-report behavior (#2341's own round-1-reviewed
# boundary) completely unchanged. Reuses `verify_merge.py`'s EXISTING, already-tested
# `verify_and_offer_merge`/`ensure_landing_pr`/`merge_pr` machinery verbatim -- the SAME functions
# a human runs by hand via `verify_merge.py land`. The ONE thing this skips is the interactive
# "merge now? [y/N]" confirmation (`decide=lambda: True` in place of `_interactive_decide`'s real
# `input()`) -- `verify.command` still runs and still gates everything after it, structurally:
# `verify_and_offer_merge`'s own `decide()` call is unreachable until verify is green, so this is
# "skip the are-you-sure prompt", never "skip the tests too".


def _unsafe_merge_check(cwd, sdlc_dir, config, name):
    """`--unsafe-merge <name>`: the ONE difference from a human running `verify_merge.py land`
    themselves in this exact worktree is `decide` -- `lambda: True` here, `input()` there.
    Everything else (verify first, landing-PR resolution via `ensure_landing_pr`, the actual `gh pr
    merge` via `merge_pr`, the ledger's own `record_merge`) is the SAME, already-tested function
    this repo's own real CLI already uses -- never reimplemented here.

    -> `(kind, why)`, mirroring `_merge_check`'s own return shape so the SAME dispatch/ledger
    plumbing (#2338) handles both commands unchanged. `kind` is one of `"parked"|"failed"|"done"`."""
    branch = feature_rebase.features.BRANCH_PREFIX + name
    base = feature_rebase.state_safe_ref("work.base", (feature_rebase._settings(config) or {}).get("base"))
    run = feature_rebase._run
    result = verify_merge.verify_and_offer_merge(
        run, cwd, sdlc_dir, config, branch, base, decide=lambda: True)
    stage, outcome = result.get("stage"), result.get("outcome")
    if stage == "verify":
        if outcome == verify_merge.NO_COMMAND:
            return ("failed",
                    "no `verify.command` is configured in `.sdlc/config.json` -- nothing to check.")
        cmd = verify_merge.verify_command(config)
        report = verify_merge.format_verify_report(cmd, cwd, result.get("verify") or {})
        return "failed", report
    landing = result.get("landing") or {}
    if stage == "land":
        if outcome == verify_merge.ALREADY_MERGED:
            return "done", landing.get("why") or "already merged -- nothing to do."
        if outcome == verify_merge.DECLINED:
            return ("parked", landing.get("why") or
                    "the landing PR was already closed by a human -- not reopened.")
        return "failed", landing.get("why") or "could not resolve a landing pull request."
    # stage == "merge"
    merge = result.get("merge") or {}
    if outcome == verify_merge.MERGED:
        return "done", ("%s %s" % (landing.get("why") or "", merge.get("why") or "")).strip()
    return "failed", merge.get("why") or "the merge itself failed."


def _unsafe_merge_run_drive(sdlc_dir, config, name, key):
    """Mirrors `_merge_run_drive` (#2341) exactly -- deterministic, never spends a real driven
    session (this is a `gh pr merge` call, not something needing judgment); `on_spawn` names THIS
    process's own pid, since there is no child. The one difference from `_merge_run_drive`: this
    needs `name` itself (to resolve `branch`), not just `cwd` -- `ensure_landing_pr` looks the
    landing PR up by branch NAME over the GitHub API, not by inspecting the worktree's checkout."""
    def run_drive(cmd_str, prompt, cwd, env, timeout, on_spawn=None):
        if on_spawn is not None:
            on_spawn(os.getpid())
        try:
            kind, why = _unsafe_merge_check(cwd, sdlc_dir, config, name)
        except Exception as exc:            # noqa: BLE001 - a crashed check is a reported failure,
            kind, why = "failed", "the unsafe-merge check itself crashed: %s" % exc  # never unreported
        ledger.safe_append(sdlc_dir, kind, key, config=config, why=why)
        return 0, why
    return run_drive


_UNSAFE_MERGE_BUSY_KNOWN = ("`--unsafe-merge %s` is already being handled by %s -- try again once "
                             "that finishes.")
_UNSAFE_MERGE_BUSY_UNKNOWN = "`--unsafe-merge %s` was just claimed by another session -- try again shortly."
_UNSAFE_MERGE_WORKTREE_BUSY = ("%s's own rebase worktree is busy right now (an ordinary pick's "
                                "automatic upkeep, or another in-flight command) -- try "
                                "`--unsafe-merge %s` again shortly.")
_UNSAFE_MERGE_UNCLEAR = "`--unsafe-merge %s` ran but left no clear outcome -- check the repo directly."


def _upkeep_gate_open(config):
    """The unit-upkeep project door, read only through `feature_upkeep`. A gate that cannot answer is closed."""
    try:
        return bool(feature_upkeep.enabled(config))
    except Exception:                       # noqa: BLE001 - closed on any doubt
        return False


_REQUESTER = re.compile(r"[UW][A-Z0-9]{2,20}\Z")


def _requester_id(requester):
    """The Slack user id of the message, or `unknown`: only the id shape is ever written to the ledger."""
    return requester if isinstance(requester, str) and _REQUESTER.match(requester) else "unknown"


def _engine_land():
    return _load("feature_land")


def _engine_reply(name, out, who):
    """Engine outcome -> (ledger kind, chat reply). Every outcome the engine returns has a branch; an unknown one reads
    as unclear, never as success and never as a bare failure."""
    outcome = str(out.get("outcome") or "")
    detail = out.get("detail") or ""
    by = " (requested by %s)" % who
    if outcome == "merged":
        return "done", "`--unsafe-merge %s`: landed%s." % (name, by)
    if outcome == "already-landed":
        return "done", "`--unsafe-merge %s`: already landed%s -- nothing to do." % (name, by)
    if outcome == "merged-with-warning":
        return "done", "`--unsafe-merge %s`: landed with a warning%s -- %s Check the base branch." % (
            name, by, detail or "the base moved while landing.")
    if outcome == "armed":
        return "done", "`--unsafe-merge %s`: auto-merge is armed%s; the host lands it when its checks pass." % (name, by)
    if outcome.startswith("refused"):
        hint = ""
        if outcome in ("refused:guard", "refused:unattended-no-approval"):
            hint = " Approve the unit locally first, then ask again."
        return "failed", "`--unsafe-merge %s` was refused%s: %s%s%s" % (
            name, by, outcome.partition(":")[2] or "no reason", (" -- " + detail) if detail else ".", hint)
    if outcome.startswith("unconfirmed"):
        return "failed", ("`--unsafe-merge %s` could not confirm the landing%s: %s. Nothing was retried; check the "
                          "repository before asking again." % (name, by, outcome.partition(":")[2] or "unknown"))
    return "failed", _UNSAFE_MERGE_UNCLEAR % name


def _unsafe_merge_engine_reply(sdlc_dir, config, name, requester):
    """Gate open: claim the unit (same arbitration), run the landing engine with NO consent flag and a driven
    fingerprint so the unit approval is required, finish the claim, map the outcome. No chat worktree is cut: the engine
    takes the unit's rebase lock itself and would find a held one busy."""
    key = claim_key(name)
    who = _requester_id(requester)
    claim = try_claim(sdlc_dir, config, name)
    if not claim.ok:
        if claim.holder_actor:
            return _UNSAFE_MERGE_BUSY_KNOWN % (name, claim.holder_actor)
        return _UNSAFE_MERGE_BUSY_UNKNOWN % name
    try:
        engine = _engine_land()
        fingerprint = engine._load("feature_land_approval").FINGERPRINTS[0]
        out = engine.land(config, sdlc_dir, name, argv=(), environ={fingerprint: "chat"}, merge=True)
        kind, reply = _engine_reply(name, out if isinstance(out, dict) else {}, who)
    except Exception as exc:                # noqa: BLE001 - a crashed landing is a reported failure
        kind, reply = "failed", "the landing engine crashed: %s" % exc
    finish_claim(sdlc_dir, config, key, kind, why=reply)
    return reply


def _unsafe_merge_reply(sdlc_dir, config, name, run_drive=None, requester=None):
    """`--unsafe-merge <name>` (#2359): dispatches via #2338's shared `dispatch()` -- SAME claim
    arbitration (keyed on the unit NAME, so this correctly serialises against a concurrent
    `--rebase <name>`/`--merge <name>` on the SAME unit, not only a second copy of itself), the SAME
    isolated worktree `--rebase`/`--merge` use, this command's own deterministic `run_drive`
    (`_unsafe_merge_run_drive`, above) that ACTUALLY lands the branch, then teardown."""
    if run_drive is None and _upkeep_gate_open(config):
        return _unsafe_merge_engine_reply(sdlc_dir, config, name, requester)
    key = claim_key(name)
    drive = run_drive or _unsafe_merge_run_drive(sdlc_dir, config, name, key)
    result = dispatch(sdlc_dir, config, name, "--unsafe-merge", run_drive=drive)
    if result.state == "busy":
        if result.holder_actor:
            return _UNSAFE_MERGE_BUSY_KNOWN % (name, result.holder_actor)
        return _UNSAFE_MERGE_BUSY_UNKNOWN % name
    if result.state == "worktree-busy":
        return _UNSAFE_MERGE_WORKTREE_BUSY % (name, name)
    if result.state == "worktree-unavailable":
        return "could not prepare an isolated checkout for `%s`: %s" % (name, result.detail)
    if result.state == "unclear":
        return _with_ledger_off_note(config, _UNSAFE_MERGE_UNCLEAR % name)
    return result.stdout or (
        "`--unsafe-merge %s` failed: %s" % (name, result.detail or "no further detail."))


# --------------------------------------------------------------------------- --rebase <name> (#2340, Component D)


#: `dispatch`'s own `extra_prompt` for `--rebase <name>`. The Python in this module never runs
#: `git rebase`/`git push` itself -- Component I's whole point is that the ISOLATION is structural
#: (the worktree `cwd` `dispatch` already cut), not something a prompt has to be trusted to enforce;
#: this text is the second, belt-and-suspenders half (`_dispatch_prompt`'s own module comment: "the
#: prompt still names the specific command... defense in depth"), telling the driven session WHAT
#: to do with that isolation once it has it, in the one place this design draws a hard line: a real
#: conflict is reported, never resolved (BR-13/BR-15 -- `sigma-rebase`'s own "not automatic merge...
#: any human may run it" contract, and there is no Slack-side conversational channel in v1 to
#: receive `conflict_walk.py`'s own per-file choice regardless).
_REBASE_EXTRA_PROMPT = (
    "This is `--rebase <name>` (Epic #2335 slice 4, #2340, Component D of design #2329): "
    "rebase `feature/<name>` onto `work.base` and push -- but ONLY on a genuinely clean rebase. "
    "Load `skills/sigma-rebase/scripts/rebase_brief.py` by path (the same `_load` pattern every "
    "sibling script in this kit already uses) and call its own `assemble_brief`/`attempt_rebase` "
    "functions directly -- never either script's CLI `main()`, which hardcodes a different `cwd`. "
    "Resolve `remote` via `feature_rebase._remote(config)` and `base` via "
    "`feature_rebase._settings(config).get(\"base\")`, and pass `branch=\"feature/<name>\"` "
    "explicitly to both -- the worktree is already checked out on that exact local branch "
    "(Component I's own precondition), so this just names what `assemble_brief`/`attempt_rebase` "
    "compare against; do not re-derive it another way. Then, on `attempt_rebase`'s own "
    "outcome: CURRENT or REBASED (a real, clean rebase, already pushed) -- write a `\"done\"` "
    "completion marker whose `why` states plainly which of the two happened. CONFLICT -- do NOT "
    "resolve it and do NOT call `skills/sigma-rebase/scripts/conflict_walk.py`'s interactive walker "
    "(there is no human on the other end of this Slack command to answer its per-file prompts); "
    "leave the rebase stopped exactly where git left it -- nothing force-pushed, nothing lost -- "
    "and write a `\"parked\"` completion marker whose `why` names every conflicted file and says to "
    "run `/sigma-rebase` locally to resolve it. Any other FAILED outcome (e.g. a force-with-lease "
    "push refused because someone else moved the branch) -- write a `\"failed\"` completion marker "
    "whose `why` is the real error, unmodified. In every case, `why` is relayed verbatim back to "
    "the human in Slack -- write it as the whole, self-contained answer, not a sentence fragment. "
    "If ANYTHING raises before you reach one of those four outcomes -- an import error, a git "
    "command failing outside `attempt_rebase`'s own try/except, anything unexpected -- that is "
    "still a real outcome, not a reason to stop silently: write a `\"failed\"` completion marker "
    "whose `why` is that error's own text, unmodified, before you exit. A clean exit with NO "
    "completion marker at all is read as \"unclear\" and tells the human nothing useful -- never "
    "let that be the outcome when you know exactly what happened."
)


def _rebase_reply(sdlc_dir, config, name, run=None, run_drive=None, session_pid=None):
    """`--rebase <name>` (#2340): dispatches `name` through the shared claim/worktree/drive
    machinery (`dispatch`, #2338) with `_REBASE_EXTRA_PROMPT` as its command-specific instructions,
    then turns the returned `DispatchResult` into one honest Slack reply -- never a placeholder,
    never a bare exit code. `run`/`run_drive`/`session_pid` are DI, threaded straight through to
    `dispatch` (see `build_reply`'s own docstring)."""
    result = dispatch(sdlc_dir, config, name, "--rebase", run_drive=run_drive, run=run,
                       extra_prompt=_REBASE_EXTRA_PROMPT, session_pid=session_pid)
    if result.state == "busy":
        holder = (" -- already claimed by %s" % result.holder_actor if result.holder_actor
                  else " -- claimed by another process a moment ago")
        return "could not rebase `%s`%s. Nothing new started." % (name, holder)
    if result.state in ("worktree-busy", "worktree-unavailable"):
        return "could not rebase `%s` -- %s" % (name, result.detail)
    if result.state == "done":
        return "`--rebase %s`: %s" % (name, result.detail)
    if result.state == "parked":
        return "`--rebase %s` stopped for a human -- %s" % (name, result.detail)
    if result.state == "failed":
        return "`--rebase %s` failed -- %s" % (name, result.detail)
    if result.state == "unclear":
        return _with_ledger_off_note(config,
            "`--rebase %s` finished with an unclear outcome (%s) -- check the repo directly."
            % (name, result.detail))
    raise AssertionError("unreachable: unknown dispatch state %r" % (result.state,))


# --------------------------------------------------------------------------- Socket Mode wiring


def _socket_mode_response_cls():
    """The ONLY place `slack_sdk.socket_mode.response.SocketModeResponse` is ever imported --
    lazily, at call time, so importing this module never requires `slack_sdk` to be installed."""
    try:
        from slack_sdk.socket_mode.response import SocketModeResponse
    except ImportError as exc:
        raise RuntimeError(_MISSING_DEPENDENCY_MESSAGE) from exc
    return SocketModeResponse


def _ack(client, req, response_cls=None):
    """ACK an inbound Socket Mode request immediately -- an unacked request is retried by Slack.
    `response_cls` is DI for tests, mirroring `slack_client.post_message`'s own `post=` parameter:
    when omitted, lazily imports the real `slack_sdk` class."""
    cls = response_cls or _socket_mode_response_cls()
    client.send_socket_mode_response(cls(envelope_id=req.envelope_id))


def _on_request(client, req, config, sdlc_dir, bot_user_id=None, response_cls=None):
    """The Socket Mode request handler. Acks immediately, refreshes the heartbeat on EVERY request
    (Component F: "on every confirmed Socket Mode activity" -- a ping/hello/disconnect frame counts,
    not only an actual command), then hands an actual channel message off to the pure, network-free
    `handle_message_event`/`handle_app_mention_event` -- the only function above this line that ever
    touches a real `slack_sdk` type is this one and `_ack`, both fully exercised in tests via
    `response_cls`/a duck-typed fake `req`/`client`, never a real Socket Mode connection.

    `app_mention` (#2353, `@ls-bot --help`) is handled here too, alongside the plain `message` path
    -- but Slack fires BOTH event types for the same mentioning message, so a `message` event whose
    text opens with THIS bot's own mention is skipped here (not answered a second time): the
    `app_mention` branch below is the one that answers it. `bot_user_id` is None when it could not
    be fetched at startup (`_bot_user_id`'s own graceful degradation) -- mentions then simply never
    match and are refused like any other unrecognized command; plain `--help`/etc. are unaffected."""
    _ack(client, req, response_cls=response_cls)
    write_heartbeat(sdlc_dir)
    if getattr(req, "type", None) != "events_api":
        return
    event = (getattr(req, "payload", None) or {}).get("event") or {}
    event_type = event.get("type")
    if event_type == "app_mention":
        if event.get("subtype") is not None or event.get("bot_id"):
            return
        authorized, reply_text, parsed = handle_app_mention_event(event, bot_user_id, config,
                                                                    sdlc_dir)
        _log(sdlc_dir, "mention on %r authorized=%s command=%r" % (
            event.get("channel"), authorized, getattr(parsed, "command", None)))
        if authorized and reply_text:
            slack_client.post_message(event.get("channel"), reply_text, config,
                                       token_env=bot_token_env(config))
        return
    if event_type != "message" or event.get("subtype") is not None or event.get("bot_id"):
        # SAFETY: never react to an edit/delete/bot-authored message -- in particular, never react
        # to this listener's OWN reply, which would otherwise re-trigger itself in a loop.
        return
    if bot_user_id is not None and _leading_mention_id(event.get("text")) == bot_user_id:
        # This exact message ALSO arrives as the app_mention event handled above -- skip the
        # message copy so it's answered exactly once, not twice.
        return
    authorized, reply_text, parsed = handle_message_event(event, config, sdlc_dir)
    _log(sdlc_dir, "event on %r authorized=%s command=%r" % (
        event.get("channel"), authorized, getattr(parsed, "command", None)))
    if authorized and reply_text:
        slack_client.post_message(event.get("channel"), reply_text, config,
                                   token_env=bot_token_env(config))


def _bot_user_id(web_client):
    """This bot's own Slack user id (`auth.test`'s `user_id`), fetched once at startup so mention
    stripping/dedup (#2353) can match it exactly rather than hardcode an id that differs per
    workspace installation. Degrades to None on any failure -- mentions simply won't be recognized
    then, but plain `--help`/`--drift`/etc. via a direct message are completely unaffected; startup
    must never crash over this."""
    try:
        return web_client.auth_test().get("user_id")
    except Exception:                            # noqa: BLE001 - degrade, never crash startup
        return None


def _build_client(app_token, bot_token, config, sdlc_dir):
    """Lazily imports `slack_sdk` (D-3) and wires one `SocketModeClient` whose only registered
    listener is `_on_request` above -- the ONLY place a real `slack_sdk.socket_mode.SocketModeClient`
    or `slack_sdk.WebClient` is ever constructed. `run()` takes this as an injectable
    `client_factory` for tests; production code never passes one, so this is what actually runs."""
    try:
        from slack_sdk import WebClient
        from slack_sdk.socket_mode import SocketModeClient
    except ImportError as exc:
        raise RuntimeError(_MISSING_DEPENDENCY_MESSAGE) from exc
    web_client = WebClient(token=bot_token)
    bot_user_id = _bot_user_id(web_client)
    client = SocketModeClient(app_token=app_token, web_client=web_client)
    client.socket_mode_request_listeners.append(
        lambda c, req: _on_request(c, req, config, sdlc_dir, bot_user_id=bot_user_id))
    return client


def _missing_token_envs(config):
    """The token env var NAMES (`app_token_env`/`bot_token_env`) that are unset on THIS machine --
    shared by `run()`'s own startup guard and `ensure()`'s pre-spawn fast-fail (#2396), so the two
    checks can never independently drift apart."""
    app_env, bot_env = app_token_env(config), bot_token_env(config)
    return [name for name, tok in ((app_env, legacy.getenv(app_env, "")),
                                    (bot_env, legacy.getenv(bot_env, "")))
            if not tok]


def run(sdlc_dir, config, client_factory=None, sleep=time.sleep, poll_seconds=1):
    """Start the listener and block until the stop-file appears (or a caller-injected `sleep`
    decides otherwise, for tests). `client_factory(app_token, bot_token, config, sdlc_dir) ->
    client` is DI, mirroring every other injectable seam in this kit's own watch-tick scripts;
    production code omits it and gets the real `_build_client`.

    Refuses loudly and returns non-zero, without ever calling `client_factory`, when: the feature
    is disabled (`enabled(config)` is False -- a quiet, expected no-op, not a refusal); either
    token env var is unset (SAFETY: never attempt a partial start); or a live sibling already holds
    the single-instance lock (`acquire_single_instance`).

    Polls `stop_requested` rather than blocking on the connection forever -- the stop-file is the
    one lever a human (or a supervisor) has to ask this process to disconnect cleanly, mirroring
    `supervise_daemon.py`'s own stop-file convention (BR-20); Slack's own reconnect/keepalive protocol is
    handled internally by `slack_sdk`'s `SocketModeClient` and needs no polling loop of its own
    (Doubt D-5)."""
    if not enabled(config):
        _log(sdlc_dir, "disabled in config -- nothing to do")
        return 0
    missing = _missing_token_envs(config)
    if missing:
        _log(sdlc_dir, "refusing to start -- missing env var(s): %s (see SLACK_COMMANDS.md)"
             % ", ".join(missing))
        return 1
    app_token = legacy.getenv(app_token_env(config), "")
    bot_token = legacy.getenv(bot_token_env(config), "")
    uninstall = _install_signal_cleanup(sdlc_dir)     # BEFORE acquire: no unhandled window (#424)
    try:
        ok, reason = acquire_single_instance(sdlc_dir)
        if not ok:
            _log(sdlc_dir, reason)
            return 1
        build = client_factory or _build_client
        client = build(app_token, bot_token, config, sdlc_dir)
        client.connect()
        write_heartbeat(sdlc_dir)
        _log(sdlc_dir, "connected -- listening on channel %s" % channel_id(config))
        while not stop_requested(sdlc_dir):
            sleep(poll_seconds)
        _log(sdlc_dir, "stop-file present -- disconnecting")
        closer = getattr(client, "close", None) or getattr(client, "disconnect", None)
        if closer:
            closer()
        return 0
    finally:
        try:
            release_single_instance(sdlc_dir)
        finally:
            uninstall()


# --------------------------------------------------------------------------- status / ensure (#2396)
#
# The FIRST of the two shapes #2396 named -- auto-check-and-restart wired into `_ensure_watcher`'s
# own every-loop-trigger call sites -- is explicitly OUT of scope here: that shape auto-starts a
# held-open Socket Mode connection on every single loop trigger (including inside a tight multi-goal
# drain), a materially bigger and more consequential change than a command a human or a scheduled
# task invokes deliberately, and #2396 itself says the design question ("does a Slack-connected
# process belong inside `_ensure_watcher`'s own call sites at all") needs its own resolution first.
# This is the SECOND shape only: a manual, explicit command -- `status` reports current liveness,
# `ensure` starts the listener if it is not running, or replaces it if it is genuinely dead.
#
# Neither call site in `loop.py`/`watch_daemon.py` is touched by this change -- grep confirms it (see
# `test_ensure_is_never_wired_into_ensure_watcher_or_watch_sh` below).


def _spawn_listener(sdlc_dir):
    """The real detached spawn `ensure()` uses to (re)start the listener as a background process --
    mirrors `loop.py`'s own `_ensure_watcher` idiom exactly
    (`subprocess.Popen([...], start_new_session=True, stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL)`) rather than a fresh, unreviewed shape -- `start_new_session=True` so
    the child outlives this short-lived CLI invocation's own controlling terminal/session, the same
    reason `nohup`/`launchd`/`systemd` all detach a supervised process from whatever started it.
    DEVNULL, not a pipe: nothing here reads the child's stdout/stderr, and an unread pipe can fill
    and deadlock a long-running child -- the child's OWN `_log` already persists everything that
    matters to `.sdlc/state/slack-commands.log` regardless of what its OS-level streams are attached
    to (the one gap this shares with `_ensure_watcher`'s identical choice: a bare traceback from
    before `_log` ever runs is lost, not a new risk this command introduces). `cwd` is `sdlc_dir`'s
    own parent (the project root), matching every sibling script's own worktree-relative
    convention."""
    return subprocess.Popen(
        [sys.executable, str(_HERE / "slack_commands_listen.py"), str(sdlc_dir)],
        cwd=str(pathlib.Path(sdlc_dir).resolve().parent),
        start_new_session=True, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


#: How long `ensure()` waits for a freshly spawned child to prove itself alive (its own pid +
#: heartbeat, written by `acquire_single_instance` before any network call) before giving up and
#: reporting a failed start. Generous relative to what it actually waits ON -- two local disk
#: writes, not a Slack connection -- so a slow-but-genuinely-working host still gets a fair chance;
#: the exact value is not load-bearing to any test, all of which inject a fake `spawn`.
DEFAULT_ENSURE_TIMEOUT_SECONDS = 10
DEFAULT_ENSURE_POLL_SECONDS = 0.2

EnsureResult = collections.namedtuple("EnsureResult", ["action", "pid", "detail"])

#: `EnsureResult.action` values `main()`'s CLI treats as a successful (exit 0) outcome -- everything
#: else (`"misconfigured"`, `"failed"`) is a real problem the operator has to act on.
_ENSURE_OK_ACTIONS = ("disabled", "already-running", "started", "restarted")


def ensure(sdlc_dir, config=None, spawn=None, start_timeout_seconds=DEFAULT_ENSURE_TIMEOUT_SECONDS,
           poll_interval_seconds=DEFAULT_ENSURE_POLL_SECONDS, now=None):
    """Check the listener's current state and start or restart it on request (#2396's own SECOND,
    smaller shape -- see the section comment above). Never spends a real Slack connection itself;
    it only ever decides whether to spawn ANOTHER process that might.

    -> `EnsureResult(action, pid, detail)`:
      * `"disabled"` -- `slack_commands.enabled`/`channel_id` isn't configured. `pid` is `None`;
        `detail` says so. Nothing is spawned. A deliberate no-op (exit 0), matching `run()`'s own
        "disabled in config -- nothing to do" -- the operator hasn't asked for this yet.
      * `"misconfigured"` -- `slack_commands` IS enabled, but a token env var it needs isn't set on
        this machine, so spawning would only fail. `pid` is `None`; `detail` names which var(s).
        Nothing is spawned. Unlike `"disabled"`, this IS a real problem (exit 1) -- the operator
        thinks this is set up and it silently isn't; mirrors `doctor.py`'s own MISCONFIGURED row.
      * `"already-running"` -- a fresh heartbeat says a live instance already holds it. Nothing is
        spawned -- the one property #2396 itself demands ("no risk of double-starting a second live
        listener alongside a real one"), checked HERE rather than trusted to the spawned child's own
        guard, so a caller can see this outcome without ever launching a process.
      * `"started"` -- no heartbeat had ever been recorded (`heartbeat_liveness` reported "absent");
        a new instance was spawned and proved itself alive within `start_timeout_seconds`.
      * `"restarted"` -- the prior heartbeat was STALE (a genuinely dead instance, not merely an
        absent one -- the exact "absent vs stale vs live" distinction `sync.watcher_liveness`
        already draws for the ledger watcher, reused here rather than a new two-way check); a new
        instance was spawned and proved itself alive the same way.
      * `"failed"` -- a spawn was attempted and did not pan out: the child exited before ever
        reporting a heartbeat (`detail` names its exit code and the log to check), or no heartbeat
        appeared within `start_timeout_seconds` (a hang, not a crash -- `detail` says so).

    THE ONE RACE THIS STILL HAS TO HANDLE HONESTLY: between this function's own liveness check and
    its spawn attempt, a concurrent `ensure()` invocation (a second operator, a second cron tick)
    can win first. `acquire_single_instance`'s own `os.mkdir` mutex (#2396 also hardened against a
    genuine double-win there -- see `_LOCK_RECLAIM_GRACE_SECONDS`) guarantees the LOSING spawned
    child exits quickly rather than corrupting shared state; this function's own poll loop re-checks
    liveness once the child exits, and reports the honest `"already-running"` (a benign race, not a
    failure) rather than `"failed"` when that is what actually happened."""
    config = config if config is not None else _read_config(sdlc_dir)
    if not enabled(config):
        return EnsureResult("disabled", None,
                             "slack_commands is not enabled (slack_commands.enabled/channel_id in "
                             ".sdlc/config.json) -- see SLACK_COMMANDS.md step 7.")
    missing = _missing_token_envs(config)
    if missing:
        return EnsureResult("misconfigured", None,
                             "MISCONFIGURED -- missing env var(s): %s -- see SLACK_COMMANDS.md "
                             "step 5." % ", ".join(missing))
    state, _ = heartbeat_liveness(sdlc_dir, now=now)
    if state == "live":
        return EnsureResult("already-running", _read_pid(sdlc_dir), None)
    spawner = spawn or _spawn_listener
    try:
        proc = spawner(sdlc_dir)
    except Exception as exc:                        # noqa: BLE001 - report, never raise at a CLI caller
        return EnsureResult("failed", None, "could not launch the listener: %s" % exc)
    deadline = time.time() + start_timeout_seconds
    while True:
        exit_code = proc.poll()
        if exit_code is not None:
            live_state, _ = heartbeat_liveness(sdlc_dir)
            if live_state == "live":
                return EnsureResult("already-running", _read_pid(sdlc_dir),
                                     "a concurrent start already claimed it")
            return EnsureResult(
                "failed", None,
                "the listener exited immediately (code %s) -- see %s"
                % (exit_code, log_path(sdlc_dir)))
        pid = _read_pid(sdlc_dir)
        live_state, _ = heartbeat_liveness(sdlc_dir)
        if pid == proc.pid and live_state == "live":
            return EnsureResult("started" if state == "absent" else "restarted", pid, None)
        if time.time() >= deadline:
            return EnsureResult(
                "failed", None,
                "timed out after %ss waiting for a heartbeat -- see %s"
                % (start_timeout_seconds, log_path(sdlc_dir)))
        time.sleep(poll_interval_seconds)


def status_line(sdlc_dir, config=None, now=None):
    """One clear line describing the listener's current liveness -- `status_requested`/CLI `status`
    verb (#2396). `config`/`now` are DI for tests; production omits both."""
    config = config if config is not None else _read_config(sdlc_dir)
    if not enabled(config):
        return "slack_commands: disabled in config (slack_commands.enabled/channel_id) -- see SLACK_COMMANDS.md."
    missing = _missing_token_envs(config)
    if missing:
        return ("slack_commands: MISCONFIGURED -- missing env var(s): %s (see SLACK_COMMANDS.md)."
                 % ", ".join(missing))
    state, age = heartbeat_liveness(sdlc_dir, now=now)
    pid = _read_pid(sdlc_dir)
    if state == "live":
        return "slack_commands: running (pid %s, heartbeat %s)." % (pid, sync._ago(age))
    if state == "stale":
        return ("slack_commands: DEAD -- heartbeat stale (%s, last pid %s) -- run "
                "`slack_commands_listen.py ensure %s` to restart it." % (sync._ago(age), pid, sdlc_dir))
    return ("slack_commands: not running -- no heartbeat recorded yet -- run "
            "`slack_commands_listen.py ensure %s` to start it." % sdlc_dir)


def ensure_line(result):
    """One clear line reporting what `ensure()` did -- matches `status_line`'s own single-line
    convention (and this file's own `_log`: "one timestamped, scrubbed line")."""
    if result.action in ("disabled", "misconfigured"):
        return "slack_commands: %s" % result.detail
    if result.action == "already-running":
        extra = " (%s)" % result.detail if result.detail else ""
        return "slack_commands: already running (pid %s)%s -- nothing to do." % (result.pid, extra)
    if result.action == "started":
        return "slack_commands: was not running -- started it (pid %s)." % result.pid
    if result.action == "restarted":
        return "slack_commands: was dead -- restarted it (pid %s)." % result.pid
    return "slack_commands: failed to start -- %s" % (result.detail or "unknown error.")


def _read_config(sdlc_dir):
    """`ledger._config(sdlc_dir)`, degrading to `{}` rather than crashing -- the SAME fallback
    `main()`'s legacy `run()` path already used, shared with `status`/`ensure` (#2396) so a missing
    or unreadable `config.json` reports as "disabled", never a traceback, at every one of this
    file's three CLI verbs."""
    try:
        return ledger._config(sdlc_dir)
    except Exception as exc:                        # noqa: BLE001 - degrade, don't crash on bad config
        print("slack_commands_listen: could not read %s/config.json (%s)" % (sdlc_dir, exc),
              file=sys.stderr)
        return {}


_STATUS_ENSURE_VERBS = ("status", "ensure")


CLI_USAGE = "usage: slack_commands_listen.py [sdlc_dir] | status [sdlc_dir] | ensure [sdlc_dir]"


def main(argv):
    """`python3 slack_commands_listen.py [sdlc_dir]` -- the manual-start entry point (Component F's
    own Doubt D-5: OS-level auto-restart-after-crash is explicitly deferred; a human, or a later
    supervisor, starts this directly). `sdlc_dir` defaults to `.sdlc`, matching every sibling
    watch-tick script's own `argv[1] if len(argv) > 1 else ".sdlc"` idiom.

    Two more verbs (#2396): `python3 slack_commands_listen.py status [sdlc_dir]` prints one line
    describing current liveness and exits 0 only when genuinely live; `... ensure [sdlc_dir]` starts
    the listener if nothing is running, restarts it if the prior instance is genuinely dead, and
    exits 0 for every outcome except a real failure to start. Dispatched on `argv[1]` being exactly
    `"status"`/`"ensure"` -- neither is a real `sdlc_dir` value anywhere in this kit's own
    convention (always a path like `.sdlc`), so this cannot collide with the legacy single-positional
    form below, which every existing caller (SLACK_COMMANDS.md step 8, the worked launchd/systemd
    units) keeps using completely unchanged."""
    if argv[1:] in (["-h"], ["--help"]):
        print(CLI_USAGE)
        return 0
    if len(argv) >= 2 and argv[1] in _STATUS_ENSURE_VERBS:
        sdlc_dir = argv[2] if len(argv) > 2 else DEFAULT_SDLC_DIR
        config = _read_config(sdlc_dir)
        if argv[1] == "status":
            print(status_line(sdlc_dir, config))
            state, _ = heartbeat_liveness(sdlc_dir)
            ok = enabled(config) and not _missing_token_envs(config) and state == "live"
            return 0 if ok else 1
        result = ensure(sdlc_dir, config)
        print(ensure_line(result))
        return 0 if result.action in _ENSURE_OK_ACTIONS else 1
    sdlc_dir = argv[1] if len(argv) > 1 else DEFAULT_SDLC_DIR
    config = _read_config(sdlc_dir)
    return run(sdlc_dir, config)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
