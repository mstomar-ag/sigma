#!/usr/bin/env python3
"""A parsed, code-enforced permission marker for the loop's "park on an irreversible or expensive
action" rule (#722, Part 1). Zero LLM, zero spend, stdlib only.

THE PROBLEM. `skills/sigma-loop/SKILL.md` tells every agent the loop runs to park on "an irreversible
or expensive action (deploy, delete, overwrite, spend, migrate) -- NEVER run one unattended". That
decision is made by the AGENT, from prose. The only code that touches it is `loop._reason_class`,
which labels the park reason AFTER the agent already parked. So an operator's explicit written
"yes, run the paid job" was one more sentence for the agent to weigh against a NEVER, and the NEVER
won, every time. There was no marker, label or key that exempted a goal.

THE MECHANISM. `loop.py spend-approval <dir> <goal> --action "<what>"` is the ONE gesture the SKILL
now tells an agent to run before it parks on that rule. It answers, in code and in the same words on
every host: `APPROVED <label>` (exit 0), `DENIED <reason>` (exit 3) or `OFF` (exit 4). The first
word is the whole machine contract; anything after it is for the human.

WHAT MAKES IT AN EXEMPTION RATHER THAN A SUGGESTION. Each of these is a fail-CLOSED guard, and the
default for every unreadable, malformed or ambiguous input is DENIED -- park, exactly as today:

  * OPT-IN. `spend_approval.enabled` must be exactly `True` (not "true", not 1). Off by default.
  * WHO. The issue's AUTHOR must be one of `spend_approval.approvers` (case-insensitive logins) AND
    GitHub must report the author's association as one of `sources.TRUSTED_ASSOCIATIONS` (OWNER,
    MEMBER, COLLABORATOR); an absent or unknown association is DENIED. An empty or malformed approver
    list means nobody can authorise anything. This is what stops an issue filed by a bot or a
    third party from granting itself the exemption.
  * WHERE. The marker is honoured on the FIRST LINE of the issue BODY only -- never in a comment,
    never later in the body -- mirroring `design_goal.DESIGN_OF_MARKER`'s "a deliberate declaration,
    not a substring" convention. See `parse_marker` for why first-line-only is a security decision
    and not a style one. A first line that mentions the marker but is not exactly it is DENIED.
  * PER GOAL. It is read from THIS goal's body; no config key, label or other issue's marker is
    consulted.
  * SINGLE USE per goal+label. Granting posts an audit comment carrying
    `sigma:spend-approval-used=<label>`; any later call for the same label is DENIED. The human
    re-authorises by writing a NEW label. A comment can only ever make this check stricter (a
    forged used-marker denies), never looser.
  * AUDITED BEFORE GRANTED. The audit comment (marker line, issue author, the action, a body hash,
    the issue's `updated_at`, UTC time) must post successfully BEFORE `APPROVED` is returned; a post
    that fails is DENIED, so there is never an unaudited grant. The same entry is also written to
    the ledger, best effort.
  * RACE. After posting, the comments are read again; more than one used-marker for the label means
    two callers raced, and this one DENIES.

WHAT IT DOES NOT DO -- stated plainly, because a half-guarantee described as a whole one is the
failure AGENTS.md's SAFETY property exists to prevent:

  (a) It cannot stop an agent that ignores the verb. Sigma has no code gate over an agent's own
      tool calls, so a paid step run without asking is not something this file can prevent; the
      SKILL prose is the accelerator and this verb is the authority it consults.
  (b) The author check is on who OPENED the issue, not on who last edited the body. ANY repo
      writer can add the marker to an issue an approver opened (in Sigma every collaborator is
      already in the trust set, so `approvers` only NARROWS who may open one; an editor check via
      userContentEdits is an optional follow-up, not built: it costs a GraphQL read); the audit comment's body hash and `updated_at` make
      that detectable afterwards, not impossible beforehand.
  (b2) Single-use state lives in issue comments only: whoever can delete the audit comment can
      re-arm a label, and any commenter can burn a label by posting its used-marker (deny-only). The ledger entry is written best-effort and never consulted. A transient
      retry inside `source.note` that double-posts burns the label (two used-markers) -- safe
      direction; the human writes a new one.
  (b3) The post-write re-read is eventually consistent: a re-read that does not yet show our own
      comment is DENIED although the audit comment stands (the label is burned; write a new one).
      A grant that LOSES the post-write race (or whose `note` retry double-posted) leaves its
      "honoured" audit comment on the issue even though the answer was DENIED; the comment's
      used-marker is what burns the label. The trail over-reports rather than under-reports.
  (c) Same-identity forgery. On a host where the loop runs under the operator's own GitHub login,
      the loop itself could append the marker to its own goal's body and then pass the author
      check. This verb authenticates "an approver wrote this issue", not "a human typed this
      line". The audit trail is what makes that visible. Closing it needs a human-only signal
      (a second identity, or a signed approval) and is NOT built here.
  (d) PART 2 IS OUT OF SCOPE. This is a boolean, per-use go-ahead. There is no dollar cap and no
      metering of external spend (a GPU host, a video-generation API, ...): Sigma has no hook into
      that billing, and `budget.max_tokens` meters only the loop's own token counter. A marker
      PARSING a cap would be one line; ENFORCING it needs spend reported back into a ledger or a
      provider balance check, a separate follow-up.

GITHUB QUOTA. The issue (body, author, `updated_at`) and the comments are read over REST
(`GitHubSource.fetch_issue_for_approval` / `fetch_comment_bodies`), never `gh issue view --json`,
for the reason `fetch_title_body` documents (#1808): GraphQL shares a far more easily exhausted
hourly budget. The one write is `source.note`, which already falls back to REST itself (#1657).
"""
import datetime
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import sys

_HERE = pathlib.Path(__file__).resolve().parent

#: The body marker. The whole FIRST body line: `sigma:spend-approved=<label>`, optionally `<!-- ... -->`.
MARKER = "sigma:spend-approved="
#: Posted inside the audit comment; its presence for a label means that label has been used.
USED_MARKER = "sigma:spend-approval-used="

_LABEL = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
_MARK = "(" + re.escape(MARKER) + "(" + _LABEL + ")(?::([0-9a-f]{12}))?)"
_BARE = re.compile(_MARK)
_WRAPPED = re.compile(r"<!--[ \t]*" + _MARK + r"[ \t]*-->")
_ACTION_MAX = 200

EXIT_APPROVED, EXIT_DENIED, EXIT_OFF = 0, 3, 4

#: The one class this permission may clear (slice 7, PROVISIONAL). The marker is forgeable under a
#: shared login (see (c) below), so a class that is not spend is refused loudly until a human-only
#: signal exists. `spend_approval.classes` (default this tuple) can only NARROW it, never widen it.
#: Extension point: a later release adds a class here once that signal exists.
HONOURED_CLASSES = ("spend",)
#: Per-machine use counter, in the gitignored state directory; survives a deleted audit comment.
COUNTER_FILE = "spend-approval-used.json"


def parse_marker(body, full=False):
    """-> (status, label, line). status: "ok" | "none" | "malformed". An optional `:<12 hex>` digest
    suffix is accepted by `_parse_full`; this function keeps the three-field shape.

    FIRST BODY LINE ONLY, exactly like `design_goal.DESIGN_OF_MARKER` / `goal_size`'s markers (a
    deliberate declaration, not a substring found somewhere). This is a security decision, not a
    style one: an earlier version honoured a marker on any line outside code fences, and five
    adversarial review rounds each found another way GitHub's markdown renders such a line as code,
    a quote, a comment or a table cell while the parser saw prose (a fence in a list item, a fence
    opened inside an HTML comment, a multi-line code span, a comment re-opened on its own closing
    line, ...). Emulating CommonMark is an unbounded game; the first line of a body has NO preceding
    context that can change how it renders, so there is nothing to emulate.

    The first line, after normalising CRLF/CR, must be EXACTLY `sigma:spend-approved=<label>`,
    optionally wrapped in a complete `<!-- ... -->` (accepted, and note that wrapped form renders as an
    invisible comment); 0-3 leading spaces and any trailing space/tab are tolerated, nothing else (not NBSP, not a list/quote prefix, not a leading blank line or BOM, no trailing text).
    A first line that mentions the marker string but is not exactly that is MALFORMED (a typo must
    never read as an approval, nor silently as "no marker"). The marker anywhere else in the body is
    simply not honoured -- a document that quotes it grants nothing."""
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    first = text.split("\n", 1)[0]
    if "sigma:spend-approved" not in first:
        return ("none", None, None) if not full else ("none", None, None, None)
    lead = first[:len(first) - len(first.lstrip(" \t"))]
    if "\t" in lead or len(lead) > 3:         # 0-3 leading spaces only: a tab (even after spaces)
        return _bad(full)                    # expands to column 4+ = an indented code block
    line = first.strip(" \t")
    mm = _BARE.fullmatch(line) or _WRAPPED.fullmatch(line)
    if not mm:
        return _bad(full)
    return ("ok", mm.group(2), mm.group(1)) if not full else ("ok", mm.group(2), mm.group(1),
                                                                mm.group(3))


def _bad(full):
    return ("malformed", None, None, None) if full else ("malformed", None, None)


def scrub_marker(body):
    """-> (body, changed). A body whose FIRST line carries the permission marker would, filed as-is,
    read as a permission; the line becomes a code span (backticks removed first) so it is quoted
    text. Nothing else is touched; a marker on a later line is already inert."""
    text = body if isinstance(body, str) else ""
    first, sep, rest = text.partition("\n")
    if "sigma:spend-approved" not in first:
        return text, False
    return "`" + first.replace("`", "").strip() + "`" + sep + rest, True


def _action_digest(action):
    return hashlib.sha256(" ".join(str(action).split()).encode("utf-8")).hexdigest()[:12]


def _counter_path(sdlc_dir):
    return pathlib.Path(str(sdlc_dir)) / "state" / COUNTER_FILE


def _counter_read(sdlc_dir):
    p = _counter_path(sdlc_dir)
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("counter is not an object")
    return data


def _counter_write(sdlc_dir, data):
    p = _counter_path(sdlc_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".%d.tmp" % os.getpid())
    tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def _used_count(comments, label):
    pat = re.compile(re.escape(USED_MARKER + label) + r"(?![A-Za-z0-9._-])")
    return sum(len(pat.findall(c)) for c in comments if isinstance(c, str))


def _sanitise_action(action):
    """One line, no markup that could ping a third party or forge a marker, bounded length."""
    text = " ".join(str(action).split())
    junk = ("@", "<", ">", "[", "]", "(", ")", "#", "sigma:")
    prev = None
    while prev != text:            # to a fixpoint: `sigsigma:ma:` must not reassemble into a marker
        prev = text
        for j in junk:
            text = text.replace(j, "")
    return text[:_ACTION_MAX]


def _approvers(config):
    cfg = (config or {}).get("spend_approval") if isinstance(config, dict) else None
    if not isinstance(cfg, dict) or cfg.get("enabled") is not True:
        return None, False
    raw = cfg.get("approvers")
    if (not isinstance(raw, list) or not raw
            or not all(isinstance(a, str) and a.strip() for a in raw)):
        return [], True
    return [a.strip().lower() for a in raw], True


def check(sdlc_dir, goal, action, config, source, hardstop_class=None):
    """The verb's brain. Returns one line whose FIRST WORD is the contract: `APPROVED <label>`,
    `DENIED <reason>` or `OFF`. Never raises -- an unexpected error is DENIED, because the only safe
    answer to "may I run the irreversible step?" when something broke is no."""
    try:
        return _check(sdlc_dir, goal, action, config, source, hardstop_class)
    except Exception as exc:                                      # noqa: BLE001 - fail closed
        return f"DENIED unexpected {type(exc).__name__} -- failing closed (park as before)"


def _classes(config):
    raw = config["spend_approval"].get("classes", list(HONOURED_CLASSES))
    if not isinstance(raw, list) or not all(isinstance(c, str) for c in raw):
        return []
    return [c for c in raw if c in HONOURED_CLASSES]


def _check(sdlc_dir, goal, action, config, source, hardstop_class=None):
    approvers, enabled = _approvers(config)
    if not enabled:
        return "OFF"
    if hardstop_class is not None and hardstop_class not in _classes(config):
        return (f"DENIED class {hardstop_class!r} cannot be cleared by a recorded permission "
                f"(only {', '.join(HONOURED_CLASSES)}, and only when listed in "
                "spend_approval.classes) -- park as before")
    if not approvers:
        return ("DENIED spend_approval.approvers is empty or malformed -- nobody can authorise; "
                "park as before")
    if not isinstance(action, str) or not action.strip():
        return "DENIED --action <what the step is> is required (it is written into the audit trail)"
    if not re.fullmatch(r"[0-9]{1,9}", str(goal)):
        return "DENIED goal must be a GitHub issue number"
    if not (hasattr(source, "fetch_issue_for_approval") and hasattr(source, "fetch_comment_bodies")
            and hasattr(source, "note")):
        return "DENIED this backlog source has no issue tracker to authenticate and audit against"

    issue = source.fetch_issue_for_approval(goal)
    if not isinstance(issue, dict) or not isinstance(issue.get("body"), str):
        return "DENIED could not read the issue body -- park as before"
    status, label, line, want = parse_marker(issue["body"], full=True)
    if status == "none":
        return "DENIED no sigma:spend-approved=<label> marker on the FIRST line of this goal's body"
    if status == "malformed":
        return ("DENIED the first body line mentions sigma:spend-approved but is not exactly "
                "`sigma:spend-approved=<label>` (label [A-Za-z0-9][A-Za-z0-9._-]{0,63})")
    author = issue.get("author")
    if not isinstance(author, str) or not author.strip():
        return "DENIED could not determine the issue author"
    if author.strip().lower() not in approvers:
        return "DENIED the issue author is not in spend_approval.approvers"
    association = issue.get("author_association")
    trusted = _load("sources").TRUSTED_ASSOCIATIONS
    if association not in trusted:
        # Sigma's red-team rule (#650): a marker is an AUTHORIZATION, and on a public repository
        # anyone can open an issue. A login on the approver list is not enough on its own; GitHub
        # must also report the author as OWNER, MEMBER or COLLABORATOR. Absent/unknown: fail closed.
        return (f"DENIED the issue author's association is {association or 'unknown'!r}, not one of "
                f"{', '.join(trusted)} -- a spend approval is an authorization and is honoured "
                "only from a trusted author")

    if want is not None and want != _action_digest(action):
        return (f"DENIED the action text does not match the digest bound to marker {label!r} "
                "-- the permission was written for a different step")
    key = f"{goal}:{label}"
    if key in _counter_read(sdlc_dir):
        return (f"DENIED marker {label!r} was already used for this goal (single use, recorded "
                "locally) -- write a new label to authorise again")
    if _used_count(source.fetch_comment_bodies(goal), label):
        return (f"DENIED marker {label!r} was already used for this goal (single use) -- write a "
                "new label to authorise again")

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = hashlib.sha256(issue["body"].encode("utf-8")).hexdigest()
    audit = (
        "Spend / irreversible-action approval honoured for this goal (single use).\n"
        f"- marker: `{line}`\n"
        f"- issue author: {author.strip()} ({association}; listed in spend_approval.approvers)\n"
        f"- action: {_sanitise_action(action)}\n"
        f"- body: sha256:{digest}, issue updated_at {issue.get('updated_at') or 'unknown'}\n"
        f"- granted: {stamp}\n"
        f"<!-- {USED_MARKER}{label} -->")
    try:
        source.note(goal, audit)
    except Exception as exc:                                      # noqa: BLE001
        return (f"DENIED the audit comment could not be posted ({type(exc).__name__}) -- no "
                "unaudited grant")

    try:
        counter = _counter_read(sdlc_dir)
        counter[key] = {"at": stamp, "class": hardstop_class or "spend"}
        _counter_write(sdlc_dir, counter)
    except Exception as exc:                                      # noqa: BLE001
        return (f"DENIED the use counter could not be recorded ({type(exc).__name__}) -- no "
                "grant without a durable single-use record")

    if _used_count(source.fetch_comment_bodies(goal), label) != 1:
        # >1: two callers raced (or `note`'s own retry double-posted: the label is burned, which is
        # the safe direction). 0: our own audit comment is not visible on re-read, so the single-use
        # record cannot be trusted. Either way: no grant.
        return (f"DENIED concurrent or unconfirmed approval of {label!r} on re-read -- park as "
                "before; write a new label to authorise again")

    try:
        ledger = _load("ledger")
        ledger.safe_append(sdlc_dir, "note", goal, config=config,
                           why=f"spend-approval granted: {line} by {author.strip()} for: "
                               f"{_sanitise_action(action)}")
    except Exception:                                             # noqa: BLE001 - best effort
        pass
    return f"APPROVED {label}"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def run_verb(sdlc_dir, goal, rest, config, source):
    """`loop.py spend-approval <dir> <goal> --action "<text>"` -> (stdout line, exit code)."""
    action = None
    hardstop_class = None
    if len(rest) >= 4 and rest[2] == "--class":
        hardstop_class = rest[3]
    if len(rest) >= 2 and rest[0] == "--action":
        # `--action -` reads the step text from stdin (a quoted heredoc): the step derives from the
        # issue, so it must never be interpolated into a shell command line (#713/#717).
        action = sys.stdin.read(4 * _ACTION_MAX) if rest[1] == "-" else rest[1]
    out = check(sdlc_dir, goal, action, config, source, hardstop_class)
    word = out.split(None, 1)[0] if out else "DENIED"
    return out, {"APPROVED": EXIT_APPROVED, "OFF": EXIT_OFF}.get(word, EXIT_DENIED)


if __name__ == "__main__":
    print("usage: loop.py spend-approval <dir> <goal> --action \"<text>\"  "
          "(spend_approval.py is a library driven by that verb)")
    sys.exit(0)
