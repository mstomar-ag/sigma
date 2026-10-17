#!/usr/bin/env python3
"""Read side of the local-only action log (skills/sigma-loop/scripts/actionlog.py writes it, opt-in
via config `action_log.enabled`). Zero-dep, ZERO import of actionlog.py — reads the same JSONL
format independently, format-only coupling, exactly `sigma-status`'s own relationship to `state.py`'s
STATE.md ("the two skills are independently installable units; sharing a lib across them would
over-couple them").

Answers, from the log alone, no live agent inspection needed:
  status <dir>            -- "where are we right now" (every ACTIVE goal, newest activity first)
  goal <dir> <goal>        -- "which thread, what's the status on THIS (possibly large) goal"
  slots <dir>              -- the same ACTIVE-goal derivation as Block A of docs/output-contract.md

A goal counts as ACTIVE (for `status`) iff its file has a `claimed` entry and its last entry,
across every thread, is not `recorded` (a `recorded result=review` — PR awaiting merge, #255 — is
still active: done means merged) — purely log-derived, no ledger read, no liveness probe
(that is a different, deliberately out-of-scope watcher). `status` prints "last activity: <N> ago"
precisely so a human can judge staleness themselves, never a liveness claim this tool can't back
up. An empty/absent `.sdlc/state/log/` is a legitimate, obviously-off state, reported as a one-line
empty state pointing at `action_log.enabled`, never an error.

`slots` (#2113) RENDERS THAT SAME DERIVATION AS BLOCK A, and adds no derivation of its own: the
ACTIVE set, its ordering and its "N ago" all come from `active()` above, unchanged. What it adds is
the six contract fields per slot, the six-slot ceiling, and — the part that matters most — an
honest answer when it has nothing to say.

  THREE STATES, NOT TWO. `action_log.enabled` defaults to FALSE and the log is local-only and
  gitignored, so "no rows" has three different causes and they are three different facts:

    off        the config was read and says the log is not on -> `action log off -- slot detail
               unavailable`. It claims NOTHING about what is in flight, because it knows nothing.
    unknown    the config could not be read at all (no `config.json` — which is exactly what a
               goal WORKTREE looks like) -> `action log state unknown`. Reporting that as "off"
               would be #1778's defect verbatim: a check that read "file missing" as "this project
               has none" when the file was merely unreachable from a worktree.
    on         the config says the log is on, and the block that follows is what the log holds —
               including the two empty cases (nothing logged yet / logged but nothing active),
               which are again separate lines, because they are separate facts.

  NO ARROW, EVER. §3 asks a slot description to end `→ <what unblocks next>` "whenever there is a
  next thing to name — never invent one where there is not" (#2124 settled that exception). This
  log records what HAPPENED; it has no record of what unblocks anything. So no description built
  here carries an arrow, and the tail says so in one line rather than leaving a reader to wonder.

  THE MARKER COMES OFF CODE-WRITTEN ENTRIES ONLY. `actionlog.INTERNAL_KINDS` cannot be reached
  from any CLI; `AGENT_KINDS` are whatever an agent typed. A liveness marker read off an agent row
  would launder a claim into a measurement (design #2030, D-2) — and would also be
  wrong in practice, since a finished goal's log routinely ends with an `agent_done` written after
  the code's own `recorded`.

  NO PHASE MEANS NO SLOT LINE. §2: "The phase is always present. If you genuinely cannot name one,
  the work is not in the SDLC and does not belong in a slot line." Only agent-emitted rows carry a
  `phase`, so a goal may genuinely have none — those goals are WITHHELD and COUNTED in the tail,
  never given an inferred phase. The loop's first phase is not derivable from `claimed`.

  THE SHAPE IS RENDER.PY'S JOB, NOT THIS MODULE'S. `skills/sigma-loop/scripts/render.py` is SHELLED
  OUT (never imported — this skill imports nothing from `skills/sigma-loop/scripts/`, and render.py
  loads no sibling of its own so that it can be run from here with no `sys.path` dance). This
  module therefore does NOT re-validate what render.py validates: a ref, a URL or a title this
  module hands over malformed comes back as a typed REFUSAL, and that refusal is REPORTED — on
  stdout, verbatim, with its code — beside the facts, never swallowed and never hand-shaped into a
  look-alike block. A refusal costs you the SHAPE; it must never cost you the FACTS.
"""
import calendar
import json
import os
import pathlib
import re
import subprocess
import sys
import time

try:                    # portable output: matches every other script in this repo
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:
    pass


def _stem(goal):
    """Local copy of work.py's `stem()` — this skill is independently installable and must not
    import skills/sigma-loop/scripts/ (format-only coupling, see module docstring)."""
    p = pathlib.Path(str(goal))
    return p.stem if p.suffix == ".md" else str(goal)


def _unsafe_goal_reason(stem):
    """Local copy of `state.py`'s `unsafe_goal_reason` (skills/sigma-loop/scripts/state.py) — kept
    byte-identical on purpose, not a divergent reimplementation; this skill deliberately does not
    import `skills/sigma-loop/scripts/` at all (format-only coupling, see module docstring), so it
    cannot import the shared original either. Independent review of #486/PR #487 found `read_goal`
    below embedded a caller-supplied goal into a path with zero validation, reachable via the
    agent-facing `sigma-log goal <dir> <goal>` CLI — reproduced live as an arbitrary-file-disclosure
    bug (a crafted traversal goal read an unrelated planted file's content into command output)."""
    text = str(stem)
    if any(c in text for c in ("/", "\\", ":")) or ".." in text:
        return "must not contain '/', '\\', ':', or '..' once reduced to a path component"
    return None


def log_dir(sdlc_dir):
    return pathlib.Path(sdlc_dir) / "state" / "log"


def read_goal(sdlc_dir, goal):
    """Every entry for one goal, oldest-first. A malformed line is skipped, never fatal — local
    copy of actionlog.py's own `read_goal` (format-only coupling, see module docstring). An unsafe
    `goal` (see `_unsafe_goal_reason`) degrades to "no entries" — the correct answer either way:
    nothing was ever validly logged under a goal id like that."""
    stem = _stem(goal)
    if _unsafe_goal_reason(stem):
        return []
    path = log_dir(sdlc_dir) / f"{stem}.jsonl"
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and item.get("kind"):
            out.append(item)
    out.sort(key=lambda e: e.get("ts", ""))
    return out


def _all_goal_stems(sdlc_dir):
    d = log_dir(sdlc_dir)
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.jsonl"))


def _last_by_thread(entries):
    """{thread: last_entry} across `entries` (already oldest-first, so a later line for the same
    thread always overwrites the earlier one) — the most recent line for each distinct thread."""
    out = {}
    for e in entries:
        out[e.get("thread") or "main"] = e
    return out


#: `actionlog.py`'s own `_stamp()` shape, matched independently (format-only coupling, see module
#: docstring) — `%Y-%m-%dT%H:%M:%S.mmmZ`.
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\.(\d{3})Z$")


def _epoch(ts):
    """A log `ts` back to epoch seconds (float, millisecond precision), or `None` if unparseable
    — never raises. Mirrors `ledger.py`'s own `_epoch()` shape (whole-second there; this log keeps
    the sub-second part, matching what it writes)."""
    m = _TS_RE.match(str(ts or ""))
    if not m:
        return None
    try:
        base = calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None
    return base + int(m.group(2)) / 1000.0


def _format_ago(delta_seconds):
    s = max(0, int(delta_seconds))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d} ago"


def _awaiting_age(ago):
    """`3d 04h` / `5h 02m` / `12m` -- how long a goal has waited on its merge. The same format as
    sigma-loop's `work._age` (#255); copied, not imported, because this skill imports nothing from
    sigma-loop (see the module docstring)."""
    s = max(0, int(ago or 0))
    if s >= 86400:
        return f"{s // 86400}d {(s % 86400) // 3600:02d}h"
    if s >= 3600:
        return f"{s // 3600}h {(s % 3600) // 60:02d}m"
    return f"{s // 60}m"


def is_awaiting_merge(entry):
    """#255: `recorded result=review` -- the loop finished its work and the goal's PR is awaiting a
    merge. It is NOT closed: done means merged, and until the merge-reconcile pass records `done`
    the goal is still in flight, however quiet. The row's own age is how long it has waited, which
    is the only tell that tells a stuck PR (an armed auto-merge whose check failed) from a fresh one."""
    return entry is not None and entry.get("kind") == "recorded" and entry.get("result") == "review"


def _describe(entry, ago=None):
    """A short, one-line rendering of a single entry's own fields (everything but the envelope
    fields ts/goal/thread/actor/kind), e.g. `worktree_start branch=sdlc/158` or
    `file src/bar.py (edit)`."""
    kind = entry.get("kind", "?")
    if is_awaiting_merge(entry) and ago is not None:
        return f"awaiting merge for {_awaiting_age(ago)} (recorded result=review)"
    if kind == "file":
        return f"file {entry.get('path', '?')} ({entry.get('op', '?')})"
    extra = " ".join(f"{k}={v}" for k, v in entry.items()
                     if k not in ("ts", "goal", "thread", "actor", "kind"))
    return f"{kind} {extra}".rstrip()


def active(sdlc_dir, now=None):
    """[(goal, thread, entry, ago_seconds_or_None), ...] for every ACTIVE goal, one row per
    (goal, thread), newest-activity-first — the data `status()`'s rendering below reads. See module
    docstring for the exact active/inactive rule."""
    now = time.time() if now is None else now
    rows = []
    for stem in _all_goal_stems(sdlc_dir):
        entries = read_goal(sdlc_dir, stem)
        if not entries:
            continue
        kinds = {e.get("kind") for e in entries}
        if "claimed" not in kinds:
            continue
        if entries[-1].get("kind") == "recorded" and not is_awaiting_merge(entries[-1]):
            continue                        # this goal has finished — not "active" anymore
        for thread, entry in sorted(_last_by_thread(entries).items()):
            epoch = _epoch(entry.get("ts"))
            ago = (now - epoch) if epoch is not None else None
            rows.append((stem, thread, entry, ago))
    rows.sort(key=lambda r: (r[3] if r[3] is not None else float("inf")))
    return rows


def status(sdlc_dir, now=None):
    """The rendered `status` output — a string, ready to print."""
    d = log_dir(sdlc_dir)
    if not d.is_dir() or not any(d.glob("*.jsonl")):
        return ('action log: no entries yet (config needs "action_log": {"enabled": true} — '
                'see /sigma-log)')
    rows = active(sdlc_dir, now=now)
    if not rows:
        return "active goals: 0"
    lines = [f"active goals: {len({r[0] for r in rows})}"]
    for goal, thread, entry, ago in rows:
        left = f"  {goal} [{thread}]"
        ago_text = _format_ago(ago) if ago is not None else "? ago"
        problem = merge_tracking_problem(sdlc_dir, goal, read_goal(sdlc_dir, goal))
        description = f"merge reconciliation unavailable: {problem}" if problem else _describe(entry, ago)
        lines.append(f"{left:<20}{description:<45} — {ago_text}")
    return "\n".join(lines)


def goal_view(sdlc_dir, goal):
    """The rendered `goal <id>` output — a string, ready to print."""
    stem = _stem(goal)
    unsafe_reason = _unsafe_goal_reason(stem)
    if unsafe_reason:
        return f"goal {goal}: refused as unsafe ({unsafe_reason})"
    entries = read_goal(sdlc_dir, goal)
    if not entries:
        return (f'no log entries for {goal} (config needs "action_log": {{"enabled": true}} — '
                 'see /sigma-log)')
    threads = sorted({e.get("thread") or "main" for e in entries})
    lines = [f"{goal}: {len(entries)} entries across {len(threads)} thread(s) "
             f"({', '.join(threads)})"]
    for e in entries:
        lines.append(f"  [{e.get('actor', '?')},{e.get('thread') or 'main'}] "
                     f"{e.get('ts', '?')} {_describe(e)}")
    return "\n".join(lines)


# --------------------------------------------------------------------- Block A (#2113)

#: `skills/sigma-loop/scripts/render.py` — the ONE module that constructs a status line (#2111).
#: SHELLED OUT, never imported: this skill deliberately imports nothing from
#: `skills/sigma-loop/scripts/` (see `_unsafe_goal_reason` and the module docstring), and render.py
#: loads no sibling script of its own precisely so it can be run from here with no `sys.path`
#: dance. Same relative-path convention `skills/sigma-model/scripts/predict.py` already uses in the
#: other direction, and the same one every `${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/...` line in
#: this kit's SKILL.md files uses.
RENDER_PY = (pathlib.Path(__file__).resolve().parent.parent.parent
             / "sigma-loop" / "scripts" / "render.py")

#: The three states `action_log.enabled` can be in — see the module docstring on why "off" and
#: "unknown" must not collapse into one.
LOG_ON, LOG_OFF, LOG_UNKNOWN = "on", "off", "unknown"

#: The exact wording issue #2113 specifies for the off state. It is a statement of IGNORANCE, not
#: a report of emptiness: nothing here has looked at a single log file when this line is printed.
OFF_LINE = "action log off -- slot detail unavailable"

#: Its sibling for a config that could not be READ. Same consequence, different cause, and a
#: reader has to be able to tell them apart — the whole point of #1778.
UNKNOWN_LINE = "action log state unknown -- slot detail unavailable"

#: `actionlog.INTERNAL_KINDS`, verbatim — the kinds only a Python call site can write, never
#: reachable from any CLI, so an agent cannot forge one. Copied rather than imported (format-only
#: coupling, module docstring); `test_code_written_kinds_match_the_writers_own_list` pins the two
#: lists together so a kind added there and missed here turns a test red rather than silently
#: demoting a goal's marker.
CODE_WRITTEN_KINDS = ("claimed", "worktree_start", "verify_run", "recorded", "gate",
                      "merge_armed", "merged", "decompose_check", "released", "agent_reclaimed",
                      "phase", "verdict", "phase_waived")

#: The §2 phase vocabulary (`ledger.PHASE_KINDS`, and the kind half of `render.PHASE_TOKENS`).
#: Copied for the same reason; pinned by `test_phase_kinds_match_the_renderers_own_table`. Values
#: OUTSIDE it that appear in real logs (`code_review`, `pr_review` — sub-agent roles written before
#: `loop.py log` began enforcing the vocabulary) are NOT §2 phases and are never mapped onto one.
PHASE_KINDS = ("goal", "research", "plan", "plan_review", "implement", "review", "retro")

#: §2 Axis 1, read off the newest CODE-WRITTEN entry. Only three kinds say more than "something is
#: happening": a claim with no code-written action after it IS §2's "claimed, not started";
#: `merge_armed`/`merged` are §2's "approved; merge handler or CI in flight". `🟣 review` is
#: deliberately unreachable — a `gate` row records a verdict a gate ALREADY returned, never a
#: review in flight, and stamping one from it would be a claim the log cannot back.
MARKER_BY_KIND = {"claimed": "queued", "merge_armed": "merging", "merged": "merging"}

#: `recorded` carries the outcome the loop itself wrote, so it is the one row that can honestly
#: close a slot. Anything other than these two — `failed`, or a result this table has not seen — is
#: `blocked`, §2's "failed, or needs a human decision", never silently `running`.
MARKER_BY_RESULT = {"done": "done", "parked": "parked", "review": "review"}   # #232: PR awaits merge
MARKER_RECORDED_OTHER = "blocked"

#: What a goal gets when no code-written row says more: work is in flight. Also the answer when
#: the log holds ONLY agent rows, because that is genuinely all that is known.
MARKER_DEFAULT = "running"

#: §3: "Max 6 slots. More than 6 means collapse, not scroll." Collapse here is exact and stated:
#: the six MOST RECENTLY ACTIVE goals are rendered and the remainder is COUNTED in the tail, so the
#: block never grows past six lines of slot and the reader is never left to guess how many they are
#: not being shown. Pinned to `render.MAX_SLOTS` by
#: `test_the_slot_ceiling_and_title_cap_match_the_renderers`.
MAX_SLOTS = 6

#: §3: "Title ≤ 88 characters", and 88 is `phase_report.TITLE_MAX` — the producer's own bound. This
#: module is a producer, so it ELIDES at the cap with a visible `…` exactly as
#: `phase_report.clean_title` does, rather than handing render.py a title it must refuse.
TITLE_MAX_CHARS = 88

#: What a title field says when no source ON THIS MACHINE knows the title. `title` is required and
#: not nullable in render.py's fact set, so the absence has to be SAID; the manner is
#: `render.TIER_UNRECORDED`'s and `phase_report`'s `cost: unavailable on this host (<reason>)`.
TITLE_UNAVAILABLE = "title unavailable on this host"

#: `mirror.MIRROR_REL` / `phase_report.MIRROR_REL` — the gitignored NDJSON snapshot of the backlog
#: that the pick which claimed a goal already wrote. READ, never fetched: this command makes no
#: network call, on any host.
MIRROR_REL = "state/board-mirror.ndjson"

#: How far into the mirror to scan before giving up. `mirror.py` caps itself at 200 open + 200
#: recently-closed records, so a complete file is ~400 lines; 4,000 is a factor of ten of headroom
#: over that and still O(1) work per slot at any backlog size. A miss prints the honest
#: `TITLE_UNAVAILABLE`, which is what an unmirrored goal already gets.
MIRROR_SCAN_LINES = 4000

#: §3: "Description ≤ 20 words." Enforced HERE as well as in render.py, and not as belt-and-braces:
#: this module composes the description, so a description that cannot fit is this module's bug to
#: handle, not a reason for one odd goal to cost the whole block its shape.
DESCRIPTION_MAX_WORDS = 20

#: Each field VALUE in a description is collapsed to one line and elided here. A log value is
#: capped at 500 characters by the writer, which is four sentences — far past what a ≤20-word line
#: can hold.
VALUE_MAX_CHARS = 32

#: Said out loud when the two lines above still are not enough (a value carrying a dozen spaces).
#: The reduction is VISIBLE: a reader must be able to tell a short description from a shortened
#: one, and the full entry is one `log.py goal <id>` away.
ELIDED = "(fields elided)"

#: The fields of each log kind that are BOUNDED enough to sit in a ≤20-word line, in the order they
#: are printed. Deliberately a subset of `actionlog.ALL_FIELDS`: every free-text field is left out
#: (`gate.why` and `recorded.detail` are routinely whole paragraphs, `note.text` is arbitrary), and
#: leaving them out is a display choice, not a data loss — `log.py goal <id>` still prints all of
#: it. A kind this table does not know renders as the bare kind, which is what keeps a NEW
#: actionlog kind from breaking this reader (the exact cross-skill failure #1626's own retro
#: recorded).
BOUNDED_FIELDS = {
    "claimed": (),
    "worktree_start": ("branch",),
    "verify_run": ("ok", "exit"),
    "recorded": ("result",),
    "gate": ("gate", "verdict"),
    "merge_armed": ("pr",),
    "merged": ("pr",),
    "decompose_check": ("verdict", "mode"),
    "released": (),
    "agent_reclaimed": ("pid",),
    "phase": ("phase", "state"),
    "verdict": ("phase", "verdict", "route"),
    "phase_waived": ("phase",),
    "file": ("path", "op"),
    "model_choice": ("model",),
    "agent_dispatch": ("role", "phase"),
    "agent_done": ("role", "phase", "result"),
    "note": (),
    "action": (),                       # free text: never rendered as a bounded field
    "permission_use": (),
}

#: `owner/name`, and nothing that could break the `**[ref](url)**` markdown render.py builds.
_REPO_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def _read_config(sdlc_dir):
    """`(config, problem)` — exactly one is `None`. A missing, unreadable or non-object
    `config.json` is a PROBLEM, never an empty config: "I could not look" and "I looked and it says
    no" are different answers and this function refuses to collapse them (#1778). Local copy of
    `state.load_config`'s read, not an import (module docstring)."""
    path = pathlib.Path(sdlc_dir) / "config.json"
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"no config.json at {path}"
    except (OSError, ValueError) as exc:
        return None, f"config.json at {path} could not be read ({exc})"
    if not isinstance(parsed, dict):
        return None, f"config.json at {path} is not a JSON object"
    return parsed, None


def _enabled(config):
    """Strict `is True`, mirroring `actionlog.enabled()` exactly — a truthy string or a stray 1
    must not read as an opt-in here when it does not write one there."""
    settings = (config or {}).get("action_log")
    settings = settings if isinstance(settings, dict) else {}
    return settings.get("enabled") is True


def action_log_state(sdlc_dir):
    """`(state, detail, config)` where state is `LOG_ON` / `LOG_OFF` / `LOG_UNKNOWN`. `detail`
    always names the file the answer came from, so the reader can go and look; `config` is `None`
    for anything but `LOG_ON`, because nothing else has a config to hand on."""
    config, problem = _read_config(sdlc_dir)
    if problem is not None:
        return LOG_UNKNOWN, problem, None
    path = pathlib.Path(sdlc_dir) / "config.json"
    if _enabled(config):
        return LOG_ON, str(path), config
    return LOG_OFF, f"action_log.enabled is not true in {path}", None


def goal_ref(stem):
    """`#2113` for an issue number, the bare stem for a local goal file — `phase_report.goal_ref`'s
    rule, kept identical: `#0004-vision-first-onramp` would be a lie, there is no such issue."""
    return f"#{stem}" if str(stem).isdigit() else str(stem)


def issue_url(config, stem):
    """The goal's issue URL, or `None`. Built ONLY when the config says these stems ARE issue
    numbers (`discovery.source == "github"`) and names a repo that cannot break the markdown link.
    `None` is legal and honest — §3: "Bold-without-link only when no URL exists yet"."""
    discovery = (config or {}).get("discovery")
    discovery = discovery if isinstance(discovery, dict) else {}
    if discovery.get("source") != "github":
        return None
    github = discovery.get("github")
    github = github if isinstance(github, dict) else {}
    repo = str(github.get("repo") or "")
    if not str(stem).isdigit() or not _REPO_RE.match(repo):
        return None
    return f"https://github.com/{repo}/issues/{stem}"


def _clean_title(raw):
    """One line, collapsed whitespace, elided at `TITLE_MAX_CHARS` with a visible `…` — the same
    rule as `phase_report.clean_title`, on purpose. `""` for anything empty."""
    text = " ".join(str(raw or "").split())
    if not text:
        return ""
    return text if len(text) <= TITLE_MAX_CHARS else text[:TITLE_MAX_CHARS - 1].rstrip() + "…"


def title_from_mirror(sdlc_dir, stem):
    """The goal's title out of the local board mirror, or `""` for every miss. Misses are ordinary,
    not exceptional: local mode never writes a mirror, and a goal filed since the last pick is not
    in one. Bounded by `MIRROR_SCAN_LINES` and by a substring pre-filter, so the common case parses
    one record rather than the whole file. Never raises."""
    try:
        if not str(stem).isdigit():
            return ""                       # the mirror is keyed by ISSUE NUMBER
        path = pathlib.Path(sdlc_dir) / MIRROR_REL
        if not path.is_file():
            return ""
        number = int(stem)
        needle = str(number)
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index >= MIRROR_SCAN_LINES:
                    break
                if needle not in line:
                    continue                # cheap pre-filter: no JSON parse for a non-candidate
                try:
                    record = json.loads(line)
                except ValueError:
                    continue                # one bad line never blinds the whole lookup
                if isinstance(record, dict) and record.get("number") == number:
                    return _clean_title(record.get("title"))
    except (OSError, TypeError, ValueError):
        return ""
    return ""


def newest_code_written(entries):
    """The most recent row an `actionlog` PYTHON call site wrote, or `None` if a goal's log holds
    only agent rows. Agent rows are skipped — not because they are unimportant (they are the
    description) but because everything derived here is a CLAIM, and `AGENT_KINDS` are agent-typed.

    One definition, two readers: the liveness marker and the closed/in-flight split below. Two
    copies of "which row do we believe" would be two chances to disagree."""
    for entry in reversed(entries):
        if entry.get("kind") in CODE_WRITTEN_KINDS:
            return entry
    return None


def marker_state(entries):
    """§2's liveness state for one goal, read off its newest code-written entry.

    This is the branch that keeps a finished goal from reading as `running`: a goal's log routinely
    ends with an `agent_done` written after the loop's own `recorded`, and it is the `recorded`
    that knows the outcome."""
    entry = newest_code_written(entries)
    if entry is None:
        return MARKER_DEFAULT
    kind = entry.get("kind")
    if kind == "recorded":
        return MARKER_BY_RESULT.get(entry.get("result"), MARKER_RECORDED_OTHER)
    return MARKER_BY_KIND.get(kind, MARKER_DEFAULT)


def is_closed(entries):
    """True when the loop has CLOSED this goal — its newest code-written row is `recorded`.

    `recorded` is the loop's own write that finishes with a goal, whatever the result, so one rule
    covers done, parked and failed alike and none of it can be forged. Deliberately NOT derived
    from the marker: `🔴 blocked` and `⏸️ parked` are both closed here, because the loop has moved
    on from them either way. They are never hidden — they take a free slot when one is free, and
    they are counted in the tail when none is."""
    entry = newest_code_written(entries)
    return entry is not None and entry.get("kind") == "recorded" and not is_awaiting_merge(entry)




def phase_kind(entries):
    """The newest §2 SDLC phase this goal's log records, or `None`. `None` is not a defect and it
    is never defaulted: §2 says work with no nameable phase does not belong in a slot line, and
    nothing in a `claimed` row says which phase the loop enters next."""
    for entry in reversed(entries):
        phase = entry.get("phase")
        if phase in PHASE_KINDS:
            return phase
    return None


def model_tier(entries):
    """The newest agent-recorded model tier, or `None` (which render.py prints as an explicit
    `model tier unrecorded`). It is rendered carrying BOTH of render.py's labels — predicted, and
    agent-set — because `model_choice` is an `AGENT_KINDS` row."""
    for entry in reversed(entries):
        if entry.get("kind") == "model_choice" and entry.get("model"):
            return entry["model"]
    return None


def _value(text):
    """One line, collapsed, elided at `VALUE_MAX_CHARS`."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= VALUE_MAX_CHARS else flat[:VALUE_MAX_CHARS - 1].rstrip() + "…"


def describe(entry, ago, extra_threads=0):
    """The `↳` line for one slot: what the newest entry says, and how long ago it was said.

    NO ARROW — see the module docstring. Over the word cap the field values are dropped and the
    reduction is announced, rather than letting one odd value cost the whole block its shape."""
    kind = str(entry.get("kind") or "?")
    if is_awaiting_merge(entry) and ago is not None:
        # #255 LIVENESS: age is the tell -- a goal awaiting merge for 3d reads as exactly that.
        return f"awaiting merge for {_awaiting_age(ago)}, PR not merged yet"
    thread = entry.get("thread") or "main"
    fields = [f"{name}={_value(entry[name])}"
              for name in BOUNDED_FIELDS.get(kind, ())
              if entry.get(name) not in (None, "")]
    where = [] if thread == "main" else [f"[{thread}]"]
    if extra_threads:
        where.append(f"(+{extra_threads} thread{'s' if extra_threads > 1 else ''})")
    when = _format_ago(ago) if ago is not None else "? ago"
    text = " ".join([kind] + fields + where + ["—", when])
    if len(text.split()) > DESCRIPTION_MAX_WORDS:
        text = " ".join([kind, ELIDED] + where + ["—", when])
    return text


def slot_rows(sdlc_dir, now=None):
    """`[(stem, entries, newest_entry, ago, extra_threads), ...]` — ONE row per ACTIVE GOAL,
    newest-activity first.

    The ACTIVE set, its order and its `ago` are `active()`'s, untouched; this only folds that
    function's per-`(goal, thread)` rows down to one per GOAL, which is the unit `status()` itself
    already counts (`active goals: {len({r[0] for r in rows})}`) and the unit a slot line is.

    Cost: one extra read of each ACTIVE goal's own small per-goal file, on top of the read
    `active()` already performs. That is a constant factor on a pass that is already O(goal files)
    — 74 files and 22 active on this repo today, so 96 reads instead of 74 — and it does not change
    with backlog size, only with how many goals have ever been logged."""
    rows = active(sdlc_dir, now=now)
    threads, newest, order = {}, {}, []
    for stem, _thread, entry, ago in rows:
        threads[stem] = threads.get(stem, 0) + 1
        if stem not in newest:
            newest[stem] = (entry, ago)
            order.append(stem)
    out = []
    for stem in order:
        entry, ago = newest[stem]
        out.append((stem, read_goal(sdlc_dir, stem), entry, ago, threads[stem] - 1))
    return out


def merge_tracking_problem(sdlc_dir, stem, entries, config=None):
    """Local context for a recorded review, never a claim that its PR merged.

    One work-record read per review row; no network and no loop imports. Missing tracking or
    disabled work cannot self-reconcile. Keep it visible as blocked outside the active slots.
    An unreadable config/record is also an explicit diagnostic, not inferred success.
    """
    if not is_awaiting_merge(newest_code_written(entries)):
        return ""
    if config is None:
        try:
            config = json.loads((pathlib.Path(sdlc_dir) / "config.json").read_text())
        except (OSError, ValueError):
            return "config unreadable; restore config"
    work = config.get("work") if isinstance(config, dict) else None
    if not isinstance(work, dict) or not work.get("enabled"):
        return "work disabled; enable work"
    try:
        record = json.loads((pathlib.Path(sdlc_dir) / "state" / "work" / f"{stem}.json").read_text())
    except (OSError, ValueError):
        return "tracking missing; restore tracking or record parked"
    if not isinstance(record, dict) or not isinstance(record.get("awaiting_merge"), dict) or not record.get("pr"):
        return "tracking missing; restore tracking or record parked"
    return ""


def slot_facts(sdlc_dir, config, stem, entries, entry, ago, extra_threads):
    """render.py's closed Block A fact set for one goal, or `None` when §2 forbids a slot line for
    it (no nameable phase). Every key render.py requires is present; `url` and `model_tier` are the
    two it allows to be null, and a null here is a stated absence, never a forgotten one."""
    phase = phase_kind(entries)
    if phase is None:
        return None
    problem = merge_tracking_problem(sdlc_dir, stem, entries, config)
    return {
        "marker": "blocked" if problem else marker_state(entries),
        "ref": goal_ref(stem),
        "url": issue_url(config, stem),
        "phase": phase,
        "title": title_from_mirror(sdlc_dir, stem) or TITLE_UNAVAILABLE,
        "description": (f"merge reconciliation unavailable: {problem}" if problem
                        else describe(entry, ago, extra_threads)),
        "model_tier": model_tier(entries),
    }


def render_block_a(facts, render_py=None):
    """`(ok, block, why)` — shell out to render.py, which is the ONLY thing in this kit allowed to
    construct a status line.

    Facts go over STDIN rather than argv: it is render.py's own documented input and it has no
    length ceiling to reason about. Every failure mode returns a `why` a human can act on and is
    never raised: a missing renderer (this skill is independently installable, so its sibling may
    genuinely not be here), an unrunnable one, a non-zero exit carrying a typed refusal, and the
    one case that would be worst of all — exit 0 with nothing on stdout."""
    path = pathlib.Path(render_py) if render_py else RENDER_PY
    if not path.is_file():
        return False, "", f"the renderer is not on this host (no file at {path})"
    try:
        done = subprocess.run([sys.executable, str(path), "status"],
                              input=json.dumps(facts), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=30)
    except Exception as exc:                # noqa: BLE001 - a status read must never raise
        return False, "", f"the renderer could not be run ({exc})"
    if done.returncode != 0:
        why = (done.stderr or "").strip()
        return False, "", why or f"render.py exited {done.returncode} with no message"
    block = (done.stdout or "").strip("\n")
    if not block:
        return False, "", "render.py exited 0 but produced no block"
    return True, block, ""


def select(renderable):
    """Which goals get the six slots, and which are merely counted (#2113, review).

    `active()` orders by recency alone, and on a real log that is not "what is happening": this
    repo's own block came back six slots deep with ONE live goal and five that had finished up to
    174 hours earlier, because a `recorded` goal stays in `active()`'s set forever once any row
    lands after it. That derivation is not this module's to change (issue filed separately). WHICH
    SIX THE CAP TAKES is presentation, and it is.

    THE RULE: while anything is in flight, the slots are ITS. A closed goal is counted in the tail,
    never shown. With nothing in flight there is nothing to displace, so the most recently closed
    fill the block and the HEADLINE says that is what they are.

    WHY NOT "closed goals may take the slots in-flight goals leave free", which is the obvious
    softer rule: it was written, run against this repo's real log, and DEFEATED THERE. One goal
    (#495) sat in flight with its last activity 200 hours old — claimed and never `recorded`, so
    in-flight by the log's own account — and every "is this recent enough" boundary derived from
    the live set stretched to 200 hours with it, letting five goals that had finished a week
    earlier back into the block. A rule that a single stale claim can switch off is not a rule.
    This one has no boundary to stretch.

    Nothing is lost by it. §5's trigger table already routes a landing to Block B ("A merge lands →
    B"); finished work is that block's job, and Block A is the live view. And a closed goal is
    never hidden even here — the tail counts it, and `log.py status` still lists every row.

    Returns `(shown, collapsed, finished_not_shown, in_flight_count)`."""
    # Optional third field: an unresolved review whose merge tracking cannot run.
    # It is neither live work nor finished; keep it visible in spare slots and in the tail.
    blocked = [row for row in renderable if len(row) > 2 and row[2]]
    ordinary = [row for row in renderable if not (len(row) > 2 and row[2])]
    in_flight = [row for row in ordinary if not row[1]]
    closed = [row for row in ordinary if row[1]]
    considered = (in_flight + blocked) or closed
    shown = considered[:MAX_SLOTS]
    finished_not_shown = len(closed) - (0 if in_flight or blocked else len(shown))
    return shown, len(considered) - len(shown), finished_not_shown, len(in_flight)


def _headline(in_flight, active, blocked=0):
    if in_flight:
        return f"Status — {in_flight} goal(s) in flight, of {active} active in the action log"
    if blocked:
        return f"Status — nothing in flight; {blocked} blocked merge reconciliation goal(s)"
    return (f"Status — nothing in flight; showing the most recently finished, of {active} active "
            "in the action log")


def _tail(shown, collapsed, stale, withheld, blocked_hidden=0):
    """§3: "Tail is exactly one line." It carries the accounting a capped block owes its reader —
    how many were dropped and, for each, why — and the one standing caveat: no arrow, because the
    log knows only the past."""
    parts = [f"{shown} shown"]
    if collapsed:
        parts.append(f"{collapsed} more collapsed at the {MAX_SLOTS}-slot cap")
    if stale:
        parts.append(f"{stale} finished, not shown")
    if blocked_hidden:
        parts.append(f"{blocked_hidden} blocked merge reconciliation, not shown (run log.py status)")
    if withheld:
        parts.append(f"{withheld} withheld (no SDLC phase recorded)")
    return "; ".join(parts) + " — the action log records what happened, never what unblocks next."


def loop_heartbeat_tail(sdlc_dir, now=None):
    """The newest managing-loop age, or an honest empty string when no loop has registered.

    This reader deliberately does not infer life from a PID: a fresh heartbeat says an idle loop is
    still checking, while an old one is the visible evidence a human needs to investigate.
    """
    now = time.time() if now is None else now
    directory = pathlib.Path(sdlc_dir) / "state" / "heartbeat"
    newest = None
    for path in directory.glob("*.json") if directory.is_dir() else ():
        try:
            seen = json.loads(path.read_text()).get("last_seen")
            if isinstance(seen, (int, float)) and not isinstance(seen, bool):
                newest = max(newest, seen) if newest is not None else seen
        except (OSError, ValueError, TypeError):
            pass
    return "" if newest is None else "loop heartbeat: " + _format_ago(now - newest)


def admission_tail(config, now=None, env=None):
    """#1604: `admission HELD 12m: memory` while the resource guard holds new work, else an empty string.

    Reads `hold.json` in the resource state folder (the path rule is sigma-loop's `resource_guard.hold_path`,
    copied: this skill imports nothing from sigma-loop). Silent when the file is absent, open, unreadable, or
    `resource.mode` is "off" (a left-over file nobody refreshes must not read as a live hold)."""
    env = os.environ if env is None else env
    block = config.get("resource") if isinstance(config, dict) else None
    if env.get("SIGMA_RESOURCE_MODE", (block or {}).get("mode") if isinstance(block, dict) else None) == "off":
        return ""
    base = env.get("SIGMA_RESOURCE_DIR")
    if not base:
        state = env.get("XDG_STATE_HOME") or os.path.join(env.get("HOME") or os.path.expanduser("~"), ".local", "state")
        base = os.path.join(state, "sigma", "resource")
    try:
        record = json.loads(pathlib.Path(base, "hold.json").read_text())
        since = record["since"]
        if record["state"] != "hold" or isinstance(since, bool) or not isinstance(since, (int, float)):
            return ""
    except (OSError, ValueError, KeyError, TypeError):
        return ""
    now = time.time() if now is None else now
    reason = record.get("reason")
    return "admission HELD %dm%s" % (max(0, now - since) // 60, ": %s" % reason if isinstance(reason, str) and reason else "")


def slots(sdlc_dir, now=None, render_py=None):
    """The rendered `slots` output — a string, ready to print, in every state including the ones
    where it has nothing to render.

    Nothing this function READS can raise past it: an unreadable config, an absent log directory,
    a malformed JSONL line, a title no host knows and a renderer that refuses or is missing are all
    ANSWERS here, each with its own line. That is a claim about the reads, not a blanket one — a
    caller passing something `pathlib.Path()` itself rejects is still a caller's bug, and this
    module does not pretend otherwise."""
    now = time.time() if now is None else now
    state, detail, config = action_log_state(sdlc_dir)
    if state == LOG_UNKNOWN:
        return f"{UNKNOWN_LINE} ({detail})"
    if state == LOG_OFF:
        return f"{OFF_LINE} ({detail})"

    directory = log_dir(sdlc_dir)
    stems = _all_goal_stems(sdlc_dir)
    if not stems:
        return f"action log on -- nothing logged yet (no goal files under {directory})"
    rows = slot_rows(sdlc_dir, now=now)
    if not rows:
        return (f"action log on -- 0 active goals ({len(stems)} goal file(s) under {directory}, "
                "none claimed-and-unrecorded)")

    renderable, withheld = [], []
    for stem, entries, entry, ago, extra_threads in rows:
        facts = slot_facts(sdlc_dir, config, stem, entries, entry, ago, extra_threads)
        if facts is None:
            withheld.append(stem)
        else:
            blocked = is_awaiting_merge(newest_code_written(entries)) and facts["marker"] == "blocked"
            renderable.append((facts, is_closed(entries), blocked))
    if not renderable:
        return (f"action log on -- {len(rows)} active goal(s), none renderable as a slot: no SDLC "
                f"phase is recorded for any of {', '.join(withheld)} "
                "(docs/output-contract.md §2)")

    chosen, collapsed, stale, in_flight = select(renderable)
    shown = [row[0] for row in chosen]
    blocked = sum(bool(row[2]) for row in renderable)
    blocked_hidden = blocked - sum(bool(row[2]) for row in chosen)
    block = {
        "headline": _headline(in_flight, len(rows), blocked),
        "slots": shown,
        "tail": _tail(len(shown), collapsed, stale, len(withheld), blocked_hidden)
                + (("; " + loop_heartbeat_tail(sdlc_dir, now)) if loop_heartbeat_tail(sdlc_dir, now) else "")
                + (("; " + admission_tail(config, now)) if admission_tail(config, now) else ""),
    }
    ok, rendered, why = render_block_a(block, render_py=render_py)
    if ok:
        return rendered
    return (f"action log on -- Block A not constructed, so the shape is lost and the facts are "
            f"not: {why}\n"
            f"{len(renderable)} goal(s) would have been slots, {len(withheld)} withheld; run "
            f"`log.py status {sdlc_dir}` for the same rows unshaped.")


USAGE = "usage: log.py status <sdlc_dir> | slots <sdlc_dir> | goal <sdlc_dir> <goal>"


def main(argv):
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    if len(argv) >= 3 and argv[1] == "status":
        print(status(argv[2]))
        return 0
    if len(argv) >= 3 and argv[1] == "slots":
        # Exit 0 in every branch, including a refusal. This command answered the question it was
        # asked -- what is in flight -- and only the SHAPING can fail; a non-zero exit would invite
        # a caller to discard the stdout that still carries the facts, which is exactly the
        # vanishing "a refusal must not make the whole status vanish" forbids.
        print(slots(argv[2]))
        return 0
    if len(argv) >= 4 and argv[1] == "goal":
        print(goal_view(argv[2], argv[3]))
        return 0
    print(USAGE, file=sys.stderr)
    return 2


def _state_guard(argv):
    """#708: refuse a committed symlink under .sdlc/state or .sdlc/journey (inline: this module is
    zero-dep and loads no sibling). Mirrors sigma-loop's state.guard_argv."""
    import os
    for arg in argv[1:]:
        for top in ("state", "journey"):
            root = os.path.join(arg, top)
            if not os.path.isdir(arg) or not (os.path.lexists(root)):
                continue
            for cur, dirs, files in os.walk(root, followlinks=False):
                for name in dirs + files:
                    if os.path.islink(os.path.join(cur, name)) or os.path.islink(root):
                        print(f"log.py: REFUSED: {os.path.join(cur, name)} is a symlink under .sdlc/ "
                              f"(#708). Fix: `git rm` it.", file=sys.stderr)
                        return 2
            if os.path.islink(root):
                print(f"log.py: REFUSED: {root} is a symlink under .sdlc/ (#708). Fix: `git rm` it.",
                      file=sys.stderr)
                return 2
    return 0


if __name__ == "__main__":
    sys.exit(_state_guard(sys.argv) or main(sys.argv))
