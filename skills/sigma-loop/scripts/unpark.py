#!/usr/bin/env python3
"""unpark.py (#1392) -- the human half of unparking: an interview that turns a park into either a
recorded decision or a re-pickable goal.

NOT `auto_unpark.py`. That module reverses the ONE park class it can PROVE is stale -- a named
blocker that has since closed -- and deliberately touches nothing else, because textual shape
cannot tell a genuinely-stale block from a human's deliberate checkpoint. This module handles
everything else, which is every park that needed a person in the first place: `needs_decision`,
`irreversible`, a failing check, a park whose reason is free prose. The two are complements, not
alternatives, and they share a repo: a `keep-parked` decision here writes `auto_unpark`'s own
`KEEP_PARKED_MARKER`, so a human decision recorded through this path is never re-litigated by the
automatic sweep on its next pass.

WHY AN INTERVIEW AND NOT A BUTTON. A park is a question nobody answered. Unparking without
answering it produces the worst outcome available: the goal is picked, an agent spends real effort
rediscovering the same obstacle, and parks it again -- with a fresh comment nobody reads either.
So the answers are the artifact, and the label change is a consequence of having them.

WHERE THE ANSWERS GO, AND THE TRAP IN PUTTING THEM THERE. They go in the issue BODY, not only in a
comment, because the next agent to pick this goal reads the body -- hunting comments is exactly how
context gets lost. But `backlog_check._BLOCK_RE` triggers on ordinary English ("needs", "after",
"requires", "waiting on", "depends on") within 40 characters of a `#N`, so an answer like "waiting
on the design sign-off, see #1234" would plant a PHANTOM blocker in the body of the goal that was
just unblocked and get it re-parked on the very next pick. Every recorded block is therefore fenced
in `backlog_check.UNPARK_QA_START`/`_END`, which `_blocker_haystack` strips before any scan -- the
same shape #1186 used against Sigma's own park boilerplate. A real "Blocked by #99" written
anywhere else in the body is still caught, unchanged.

WHERE THE QUESTIONS COME FROM, AND WHY NOT THE LEDGER. `reason_class` and `decision_tier` are
recorded ONLY into the ledger (`loop._record`), and `ledger.enabled` ships FALSE -- while
`decision_tier.resolve()` is itself gated on a `decision_tier: "auto"` key the template ships as
`"off"`. Reading either would make this whole module inert on a stock adopter. So both are
RE-DERIVED from text that is always present on the issue: `loop._reason_class` over the park
comment (prefix-stripped), and `decision_tier.classify` -- the UNGATED, pure classifier, never
`resolve`. The ledger, when a repo has it on, is optional enrichment and nothing more.

    python3 unpark.py list <sdlc_dir> [--assignee X] [--json]
    python3 unpark.py brief <sdlc_dir> <issue> [--json]
    python3 unpark.py resolve <sdlc_dir> <issue> --answers <file.json>
                              --decision keep-parked|unpark [--dry-run]
"""
import json, pathlib, sys, importlib.util

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


sources = _load("sources")
triage = _load("triage")
auto_unpark = _load("auto_unpark")
backlog_check = _load("backlog_check")
legacy = _load("legacy")          # #239: a Q&A block written under the previous name
decision_tier = _load("decision_tier")
qkind = _load("qkind")            # #994: a kind declared on the park comment
decision_record = _load("decision_record")   # slice 8: store record + attributed comment
loop = _load("loop")

#: The fixed heading of the recorded block, in the body and in the comment alike. Deliberately free
#: of every `_BLOCK_RE` trigger word -- the block is fenced and stripped anyway, but a heading that
#: could itself read as a blocker phrase would be one more thing depending on the fence being right.
QA_HEADING = "## Unpark review"

#: The question bank, keyed by `ledger.REASON_CLASSES`. These are SLOTS, not prose an agent
#: improvises per run: improvised phrasing is neither reviewable nor consistent, and the whole
#: complaint that produced this module was that agent questions are hard to understand. The rules
#: every entry follows -- also stated in SKILL.md, for the questions an agent has to compose itself:
#:
#:   - one sentence, no clause stacking
#:   - name the concrete thing, never "the aforementioned"
#:   - no Sigma vocabulary: never "reason_class", "decision_tier", "offboard", "slot", "park"
#:   - ask for a DECISION or a FACT, never for approval of an analysis
#:   - offer the real options when there are two or three; free text otherwise
#:
#: `{ref}` is filled with the first still-open blocker when the class has one.
QUESTIONS = {
    "dependency": [
        {"id": "route", "ask": "#{ref} is still open. Do you want to wait for it, work around it, "
                               "or drop this?",
         "options": ["wait for it", "work around it", "drop this"]},
        {"id": "workaround", "ask": "If we work around it, what should we do instead?",
         "when": {"route": "work around it"}},
    ],
    "needs_decision": [
        {"id": "decision", "ask": "This stopped for a decision: {reason} Which way do you want it?"},
        {"id": "why", "ask": "Anything the next person should know about why?"},
    ],
    "irreversible": [
        {"id": "go_ahead", "ask": "This step cannot be undone once it runs. Do you want to go "
                                  "ahead?", "options": ["go ahead", "do not", "change the plan"]},
        {"id": "guardrail", "ask": "What should we check first to be safe?",
         "when": {"go_ahead": "go ahead"}},
    ],
    "merge_conflict": [
        {"id": "route", "ask": "This branch conflicts with the base branch. Rebase it, or start "
                               "fresh?", "options": ["rebase it", "start fresh"]},
    ],
    "failing_check": [
        {"id": "route", "ask": "A check is failing: {reason} Fix it as part of this, or file it "
                               "separately?", "options": ["fix it here", "file it separately"]},
    ],
    "no_evidence": [
        {"id": "route", "ask": "There is no test proving this works. Add one here, or accept it "
                               "as is?", "options": ["add a test", "accept as is"]},
    ],
    "review_cap": [
        {"id": "route", "ask": "This stopped on a limit, not a problem. Give it more room, or "
                               "split the work?", "options": ["more room", "split it"]},
    ],
    "budget": [
        {"id": "route", "ask": "This stopped on a limit, not a problem. Give it more room, or "
                               "split the work?", "options": ["more room", "split it"]},
    ],
    "quota": [
        {"id": "route", "ask": "This stopped on GitHub's own rate limit, not on the goal's work. "
                               "Wait for the quota window to reset, or retry now against a "
                               "different token/account?", "options": ["wait it out", "retry now"]},
    ],
    # #2521: like "budget" above, this is a run-level run_stop reason, never actually written by a
    # real `park` -- kept in the bank rather than exempted (tests/test_unpark.py's own docstring:
    # "a class the loop can actually produce and that we simply forgot is not" a real answer),
    # matching "budget"'s own precedent for the identical shape rather than growing the
    # ("backlog-empty",) exemption tuple.
    "handoff": [
        {"id": "route", "ask": "This session paused to hand off, not because of a problem. Keep "
                               "going with a fresh session, or look into it first?",
         "options": ["keep going", "look into it first"]},
    ],
    "unknown": [
        {"id": "next", "ask": "Here is what it stopped on: {reason} What should happen next?"},
    ],
}
#: Asked LAST for every class, once the class-specific slots are answered. Not part of the bank
#: above because it is not diagnostic -- it is the decision the answers were gathered FOR.
#: Its id is `resume`, NOT `decision` -- review bug_003. `needs_decision`'s own first slot is
#: `decision` (it asks which way to go on the thing that stopped the goal), and the two ids collided.
#: Both failure modes were silent: on round one the two answers share a key in the `answers` dict
#: and one overwrites the other, so the recorded artifact loses either the product decision or the
#: routing one; on resume, `_questions_for`'s `CLOSING_QUESTION["id"] not in answered` test sees the
#: class answer and drops the closing question entirely -- the interview reads as complete having
#: never asked the one question SKILL.md says must always be asked.
#:
#: `needs_decision` is the class `loop._reason_class` produces most often ("changes requested",
#: "unresolved review thread", "not approved yet", "sigma:block", "needs manual decomposition"
#: all map to it), so this was the common path, not an edge. `test_no_bank_slot_id_collides_with_the
#: _closing_question` now makes the whole class of collision impossible to reintroduce for a future
#: entry.
CLOSING_QUESTION = {"id": "resume",
                    "ask": "Do you want to start this again now, or leave it parked with these "
                           "notes for later?",
                    "options": ["start it again", "leave it parked"]}

_ISSUE_FIELDS = "number,title,body,labels"


def _github(source):
    return all(hasattr(source, a) for a in ("_swap_labels", "_repo_args", "_run"))


def _names(issue):
    return {(l.get("name") or "") for l in (issue.get("labels") or [])}


def _park_reason(comments):
    """The park/fail REASON, with Sigma's own fixed boilerplate prefix removed -- the newest
    such comment wins, because a goal parked twice was last parked for the second reason.

    Reuses `sources.OFFBOARD_COMMENT_PREFIXES` rather than matching on prose, so a change to the
    park template can never silently stop this from finding the reason."""
    for text in reversed(list(comments or [])):
        for prefix in sources.OFFBOARD_COMMENT_PREFIXES:
            if text.startswith(prefix):
                return text[len(prefix):].strip(), True
    return ((comments[-1].strip(), False) if comments else ("", False))


def _fetch_issue(source, number):
    data = source._read_issue(number, _ISSUE_FIELDS.split(",") + ["comments", "state"])   # #895: REST first
    if not data.get("state"):
        raise ValueError("could not read #%s" % number)
    return data


def list_parked(sdlc_dir, config, source=None, run=None, assignee=None):
    """Open issues carrying `parked_label` or `goal_blocked_label`, OLDEST FIRST (by issue number,
    the only age signal available without a second query per issue).

    Reuses `auto_unpark._fetch_parked_issues_status` -- the same two-query, independently fail-open
    fetch, including its `complete` flag, so a partial read is never rendered as "nothing is
    parked". `assignee` defaults to `discovery.github.assignee`, which a bare `/sigma-init` scaffold
    SHIPS AS NULL (#2255 -- not yet decided; `/sigma-setup` fills it in with `@me`) and which reads
    as empty either way on an install that never ran `/sigma-setup`: the default scope is every
    parked issue, and `render_list` says so, because an empty result under an unstated scope reads
    as "I have nothing parked" when it may mean "nobody set a filter"."""
    source = source or sources.GitHubSource(config, run=run)
    if not _github(source):
        return {"issues": [], "complete": False, "assignee": None,
                "degraded": ["unparking needs github discovery mode"]}
    gh = ((config or {}).get("discovery") or {}).get("github") or {}
    assignee = assignee if assignee is not None else (gh.get("assignee") or None)
    issues, complete = auto_unpark._fetch_parked_issues_status(source)
    rows = []
    for it in sorted(issues, key=lambda i: int(i.get("number") or 0)):
        names = _names(it)
        rows.append({"number": str(it.get("number")), "title": it.get("title") or "",
                     "state": (source.goal_blocked_label if source.goal_blocked_label in names
                               else source.parked_label),
                     "labels": sorted(names)})
    return {"issues": rows, "complete": complete, "assignee": assignee,
            "degraded": [] if complete else
                        ["one of the two label queries failed — this list may be incomplete"]}


def brief(sdlc_dir, config, number, source=None, run=None):
    """Everything the interview needs, in one payload -- so an agent asks about THIS park rather
    than about parks in general, and never re-asks something the issue already answers.

    `questions` is already filtered: a slot whose `when` precondition cannot be met is dropped, and
    a slot already answered by a previous round (found in the body's fenced block) is dropped too.
    An interrupted interview therefore resumes instead of restarting."""
    source = source or sources.GitHubSource(config, run=run)
    if not _github(source):
        return {"number": str(number), "degraded": ["unparking needs github discovery mode"],
                "questions": []}
    data = _fetch_issue(source, number)
    body = data.get("body") or ""
    names = _names(data)
    comments = [sources.defuse_body(c.get("body") or "", c.get("authorAssociation"),
                                    (c.get("author") or {}).get("login"), number)
                for c in (data.get("comments") or [])]
    reason, from_sigma = _park_reason(comments)

    # #1392 / PR-1: BOTH re-derived from text that is always on the issue. `loop._reason_class` and
    # `decision_tier.classify` are pure functions; the ledger fields are unreachable on a stock
    # config (ledger.enabled ships false) and `decision_tier.resolve` is gated off besides.
    declared = qkind.parse_line(reason) if reason else None      # #994: a declared kind wins
    if declared:
        reason = qkind.strip_line(reason)
    reason_class = declared or (loop._reason_class(reason) if reason else "unknown")
    tier, tier_signal = decision_tier.classify(reason) if reason else (None, None)

    goal_doc = {"ref": str(number), "raw": (data.get("title") or "") + "\n" + body}
    extra = "\n".join(backlog_check._strip_offboard_prefixes(
        backlog_check._filter_dismissal_comments(comments)))
    refs = sorted(backlog_check._referenced_blocker_refs(goal_doc, extra), key=int)
    blockers = [{"ref": r, "open": auto_unpark._ref_is_open(source, r, {})} for r in refs]
    open_refs = [b["ref"] for b in blockers if b["open"]]

    answered = _recorded_answers(body)
    questions = _questions_for(reason_class, reason, open_refs, answered)
    return {"number": str(number), "title": data.get("title") or "",
            "state": data.get("state"), "labels": sorted(names),
            "park_reason": reason, "park_reason_is_sigmas": from_sigma,
            "reason_class": reason_class, "decision_tier": tier, "tier_signal": tier_signal,
            "blockers": blockers, "prior_answers": answered,
            "questions": questions, "degraded": []}


def _questions_for(reason_class, reason, open_refs, answered):
    """The slots still worth asking. `{ref}`/`{reason}` are filled here, so the caller never has to
    know the templates. A slot whose `when` precondition names an answer the human has not given is
    dropped rather than asked speculatively -- and `CLOSING_QUESTION` is appended LAST, always,
    because the decision is what the rest was gathered for."""
    ref = open_refs[0] if open_refs else None
    out = []
    for slot in QUESTIONS.get(reason_class) or QUESTIONS["unknown"]:
        if slot["id"] in answered:
            continue
        when = slot.get("when") or {}
        if any(answered.get(k) != v for k, v in when.items()):
            continue
        text = slot["ask"]
        if "{ref}" in text:
            if not ref:
                continue                      # nothing concrete to name -- an unnamed ref is noise
            text = text.replace("{ref}", ref)
        text = text.replace("{reason}", _one_line(reason))
        out.append({"id": slot["id"], "ask": text, "options": slot.get("options") or []})
    if CLOSING_QUESTION["id"] not in answered:
        out.append(dict(CLOSING_QUESTION))
    return out


def _substantive_reason(answers):
    """The answer worth quoting in the keep-parked marker: the first one that is NOT the closing
    routing question.

    "leave it parked" is the DECISION, not the REASON — pasting it into
    `auto_unpark.keep_parked_comment` produces "this park is a deliberate checkpoint, not a stale
    block — leave it parked", which says nothing a reader did not already know. What they need is
    the substantive answer that led there."""
    for key, value in (answers or {}).items():
        if key != CLOSING_QUESTION["id"] and str(value).strip():
            return value
    return ""


def _one_line(text, limit=140):
    flat = " ".join(str(text or "").split())
    if not flat:
        return "(no reason was recorded)"
    flat = flat if len(flat) <= limit else flat[:limit - 1].rstrip() + "…"
    return flat if flat.endswith((".", "?", "!", "…")) else flat + "."


def _recorded_answers(body):
    """Answers already recorded in the body's fenced block, `{id: answer}`. Parsed back out of the
    rendered block rather than stored in a parallel file: the issue is the only place both a human
    and a future session are guaranteed to look, and a sidecar could drift from it."""
    text = body or ""
    at, _spelled = legacy.find_marker(text, backlog_check.UNPARK_QA_START)
    if at == -1:
        return {}
    span = text[at:]
    stop, _end = legacy.find_marker(span, backlog_check.UNPARK_QA_END)
    span = span[:stop] if stop != -1 else span
    out = {}
    for line in span.splitlines():
        line = line.strip()
        if line.startswith("- **") and "** — " in line:
            key = line[4:line.index("**", 4)]
            out[key] = line.split("** — ", 1)[1].strip()
    return out


def render_block(answers, questions_asked=()):
    """The fenced record, identical in the body and in the comment so the two can never disagree.

    `questions_asked` carries the question TEXT alongside each id, so a reader six weeks later sees
    what was actually asked rather than a bare key -- the answers are only meaningful next to them.
    """
    asked = {q["id"]: q["ask"] for q in (questions_asked or [])}
    lines = [backlog_check.UNPARK_QA_START, QA_HEADING, ""]
    for key, value in answers.items():
        if key in asked:
            lines.append("_%s_" % asked[key])
        lines.append("- **%s** — %s" % (key, " ".join(str(value).split())))
    lines += ["", backlog_check.UNPARK_QA_END]
    return "\n".join(lines)


def _replace_block(body, block):
    """Idempotent body append: a SECOND round replaces the fenced span rather than stacking another
    copy under it. Without this, three rounds of questions produce three blocks and the reader has
    to work out which one is current."""
    text = body or ""
    at, _spelled = legacy.find_marker(text, backlog_check.UNPARK_QA_START)   # #239: either spelling
    if at != -1:
        # Review bug_002: `text.index(end)` searched from position 0, so a stray end marker in prose
        # ABOVE the real block made `tail` start inside the old block -- and the new body then
        # carried BOTH, breaking the one invariant this function exists for. The guard checked the
        # right region; the slice did not. Scoped now, byte-identical to the shape
        # `_recorded_answers` above already uses.
        head = text[:at]
        span = text[at:]
        stop, end = legacy.find_marker(span, backlog_check.UNPARK_QA_END)
        tail = span[stop + len(end):] if stop != -1 else ""
        return (head.rstrip() + "\n\n" + block + ("\n" + tail.lstrip() if tail.strip() else "")
                ).rstrip() + "\n"
    return (text.rstrip() + "\n\n" + block).lstrip() + "\n"


def resolve(sdlc_dir, config, number, answers, decision, source=None, run=None, apply=True,
            questions_asked=()):
    """Record the answers, then either keep the park or undo it.

    `decision`:
      `keep-parked` -- record, and post `auto_unpark.KEEP_PARKED_MARKER` so the automatic sweep
                       never re-litigates a decision a human just made. No label changes at all.
      `unpark`      -- ONE atomic swap (`+goal_label`, `-parked_label`/`-goal_blocked_label`), then
                       the board card to `ready`, then the record in the body AND as a comment.

    ORDER, and why: the record is written BEFORE the label swap on the keep-parked path (nothing
    depends on it) but AFTER on the unpark path, so a failure between them leaves a goal that is
    pickable-with-context rather than one whose context landed and whose unpark did not. Both
    failure directions cost a redo; neither loses the answers, because the comment is posted on
    both paths.

    Returns `{"number", "decision", "outcome", "detail"}` -- `outcome` is
    `unparked`/`kept-parked`/`would`/`failed`."""
    source = source or sources.GitHubSource(config, run=run)
    n = str(number)
    if not _github(source):
        return {"number": n, "decision": decision, "outcome": "failed",
                "detail": "unparking needs github discovery mode"}
    if decision not in ("keep-parked", "unpark"):
        return {"number": n, "decision": decision, "outcome": "failed",
                "detail": "decision must be keep-parked or unpark"}
    try:
        data = _fetch_issue(source, n)
    except Exception as exc:                            # noqa: BLE001 - unreadable == refuse
        return {"number": n, "decision": decision, "outcome": "failed",
                "detail": "could not read the current state of #%s: %s" % (n, exc)}
    if str(data.get("state") or "").upper() == "CLOSED":
        return {"number": n, "decision": decision, "outcome": "failed",
                "detail": "#%s is closed — reopen it first if it should be worked" % n}

    names = _names(data)
    block = render_block(answers, questions_asked)
    # #1393: `proposed_label` joins the removal set. An issue can be BOTH awaiting approval and
    # parked (a proposal a human parked rather than ruled on), and an unpark that only dropped
    # `sdlc:parked` would leave `sdlc:goal` + `sdlc:needs-confirmation` -- the half-promoted state,
    # picked by nothing, which /sigma-promote then has to repair. Unparking IS the human decision;
    # it settles the approval question in the same gesture.
    # Review bug_003: `in_progress_label` belongs here too, and its absence was a straight
    # divergence from the sister sweep (`auto_unpark.compute_unpark_actions`, which clears it and is
    # pinned by `test_an_unpark_clears_a_stale_in_progress_claim_too`). An issue in the half-applied
    # {parked, in-progress} shape -- real and measured on live boards -- would post-swap to
    # {goal, in-progress}, which `not_eligible_labels()` refuses on BOTH queue paths. The human
    # would be told "unparked", the card would move to Ready, and the goal would be pickable by
    # nothing until the stale-claim reclaimer eventually timed the lease out: an unpark that unparks
    # nothing, on the very command the README names as the sanctioned way to re-queue a goal.
    remove = [l for l in (source.parked_label, source.goal_blocked_label, source.proposed_label,
                          source.in_progress_label) if l in names]
    add = [source.goal_label] if source.goal_label not in names else []

    if not apply:
        detail = ("record the answers and keep it parked" if decision == "keep-parked"
                  else triage._swap_detail(add, remove) + ", record the answers")
        return {"number": n, "decision": decision, "outcome": "would", "detail": detail}

    if decision == "keep-parked":
        # `auto_unpark.keep_parked_comment` is the EXISTING opt-out convention -- reused live, not
        # reimplemented, and not approximated by pasting its marker. A human deciding "leave it
        # parked" here is making exactly the decision that comment exists to record, so the
        # automatic sweep must see it in the form it already knows how to read.
        err = _comment_decision(sdlc_dir, config, source, n, block + "\n\n"
                                + auto_unpark.keep_parked_comment(_one_line(_substantive_reason(answers))),
                                "keep-parked", answers, data)
        _append_block(source, n, data.get("body") or "", block)
        return {"number": n, "decision": decision, "outcome": "kept-parked",
                "detail": "answers recorded; the automatic sweep will leave it alone" + err}

    if not add and not remove:
        # Nothing to swap -- but the answers are still worth recording, and this is not a failure.
        err = _comment_decision(sdlc_dir, config, source, n, block, "unpark", answers, data)
        _append_block(source, n, data.get("body") or "", block)
        return {"number": n, "decision": decision, "outcome": "unparked",
                "detail": "#%s was already pickable — answers recorded" % n + err}
    try:
        source._swap_labels(n, add=add, remove=remove)
    except Exception as exc:                            # noqa: BLE001 - reported, never raised
        # Review bug_001: record the answers ANYWAY. This path used to return straight out, which
        # discarded an interview a human had just typed — and contradicted this function's own
        # docstring promise that "neither loses the answers, because the comment is posted on both
        # paths". The label write is the retryable half; the answers are not, and re-asking a person
        # the same five questions because GitHub returned a 502 is the one cost here that cannot be
        # automated away. Recording first also makes the retry cheap: `brief` reads the block back,
        # so the second attempt has nothing left to ask.
        err = _comment_decision(sdlc_dir, config, source, n, block, "unpark", answers, data)
        _append_block(source, n, data.get("body") or "", block)
        return {"number": n, "decision": decision, "outcome": "failed",
                "detail": "label write did not land: %s — the answers were recorded, so re-running "
                          "this will not ask them again" % exc + err}
    detail = triage._swap_detail(add, remove)
    if not source._set_board_status(n, source.col["ready"]) and source.project_enabled:
        detail += ("; the label landed but the board card did not move to %r — move it by hand if "
                   "this repo picks from the board" % source.col["ready"])
    detail += _comment_decision(sdlc_dir, config, source, n, block, "unpark", answers, data)
    _append_block(source, n, data.get("body") or "", block)
    return {"number": n, "decision": decision, "outcome": "unparked", "detail": detail}


def _declared_kind(data):
    """The kind a park comment declared (the last one wins), else `unknown`."""
    found = None
    for c in (data.get("comments") or []):
        found = qkind.parse_line(c.get("body") or "") or found
    return found or "unknown"


def _comment_decision(sdlc_dir, config, source, number, text, gesture, answers, data):
    """Post `text`. With the records part open it is posted WITH the decision record's id and the
    record is stored; closed, this is exactly `_comment` (byte-identical). Returns "" or a
    "; <error>" suffix for the caller's detail."""
    tried = []                                          # the comment was attempted (never post twice)
    try:
        if decision_record.enabled(config):
            choice = (answers or {}).get(CLOSING_QUESTION["id"]) or gesture
            rec = decision_record.build(gesture, number, _declared_kind(data), choice,
                                        _substantive_reason(answers), decision_record.how_for(gesture))

            def _post(num, body):
                tried.append(num)
                source._issue_comment(num, body)
            out = decision_record.write_all(sdlc_dir, rec, config, source, text=text, poster=_post)
            return "; " + out["error"] if out["error"] else ""
    except Exception as exc:                            # noqa: BLE001 - never break the gesture
        if not tried:
            _comment(source, number, text)
        return "; decision record failed: %s" % exc
    _comment(source, number, text)
    return ""


def _comment(source, number, text):
    try:
        source._issue_comment(number, text)             # REST first through gh_api (#895 slice 3a)
    except Exception:                                   # noqa: BLE001 - audit trail is best-effort
        pass


def _append_block(source, number, body, block):
    """Write the fenced record into the BODY. Best-effort and LOUD on failure: the comment already
    carries the same text, so nothing is lost -- but the body is the copy the next agent actually
    reads, and silently not having it is how a goal gets re-parked for the reason just answered."""
    try:
        source._issue_edit_body(number, _replace_block(body, block))   # REST first (#895 3a)
        return True
    except Exception as exc:                            # noqa: BLE001
        try:
            sys.stderr.write("sigma: recorded the unpark answers as a comment on #%s but could "
                             "not write them into the body (%s) — the next agent to pick this goal "
                             "will not see them inline\n" % (number, exc))
        except Exception:
            pass
        return False


def render_list(result):
    scope = result.get("assignee")
    lines = ["parked work: %s" % ("scoped to @%s" % scope if scope
                                  else "every parked issue (discovery.github.assignee is not set)")]
    for r in result["issues"]:
        lines.append("  #%s [%s] %s" % (r["number"], r["state"], r["title"]))
    if not result["issues"]:
        lines.append("  none")
    for note in result.get("degraded") or []:
        lines.append("NOTE: %s" % note)
    return "\n".join(lines)


def render_brief(b):
    lines = ["#%s %s" % (b["number"], b.get("title") or "")]
    if b.get("degraded"):
        return "\n".join(lines + ["NOTE: %s" % n for n in b["degraded"]])
    lines.append("  parked because: %s" % _one_line(b.get("park_reason")))
    lines.append("  kind: %s%s" % (b.get("reason_class"),
                                   " / %s" % b["decision_tier"] if b.get("decision_tier") else ""))
    for blocker in b.get("blockers") or []:
        lines.append("  blocker #%s: %s" % (blocker["ref"],
                                            "still open" if blocker["open"] else "closed"))
    if b.get("prior_answers"):
        lines.append("  already answered: %s" % ", ".join(sorted(b["prior_answers"])))
    lines.append("  questions:")
    for q in b["questions"]:
        opts = ("  [%s]" % " / ".join(q["options"])) if q.get("options") else ""
        lines.append("    - %s%s" % (q["ask"], opts))
    return "\n".join(lines)


_USAGE = ("usage: unpark.py list <sdlc_dir> [--assignee X] [--json]\n"
          "       unpark.py brief <sdlc_dir> <issue> [--json]\n"
          "       unpark.py resolve <sdlc_dir> <issue> --answers <file.json> "
          "--decision keep-parked|unpark [--dry-run]")


def _value(tail, name):
    if name in tail:
        i = tail.index(name)
        return tail[i + 1] if i + 1 < len(tail) else None
    return None


def main(argv, run=None):
    if argv[1:] in (["-h"], ["--help"]):
        print(_USAGE)
        return 0
    if len(argv) >= 3 and argv[1] == "list":
        sdlc_dir, tail = argv[2], argv[3:]
        result = list_parked(sdlc_dir, triage._config(sdlc_dir), run=run,
                             assignee=_value(tail, "--assignee"))
        print(json.dumps(result, indent=2) if "--json" in tail else render_list(result))
        return 0 if result["complete"] else 1
    if len(argv) >= 4 and argv[1] == "brief":
        sdlc_dir, number, tail = argv[2], argv[3], argv[4:]
        try:
            b = brief(sdlc_dir, triage._config(sdlc_dir), number, run=run)
        except Exception as exc:                        # noqa: BLE001 - a usable refusal, not a trace
            # Names the ISSUE, not just the transport error: "simulated gh failure: issue view 5"
            # tells a human nothing they can act on, and a bare traceback tells them less.
            print("unpark.py brief: could not read #%s — %s" % (number, exc), file=sys.stderr)
            return 1
        print(json.dumps(b, indent=2) if "--json" in tail else render_brief(b))
        return 0
    if len(argv) >= 4 and argv[1] == "resolve":
        sdlc_dir, number, tail = argv[2], argv[3], argv[4:]
        answers_path, decision = _value(tail, "--answers"), _value(tail, "--decision")
        if not answers_path or not decision:
            print(_USAGE, file=sys.stderr)
            return 2
        try:
            payload = json.loads(pathlib.Path(answers_path).read_text(encoding="utf-8"))
        except Exception as exc:                        # noqa: BLE001
            print("unpark.py resolve: could not read --answers %r: %s" % (answers_path, exc),
                  file=sys.stderr)
            return 2
        answers = payload.get("answers") if isinstance(payload, dict) else None
        if not isinstance(answers, dict):
            print("unpark.py resolve: --answers must be a JSON object with an \"answers\" object",
                  file=sys.stderr)
            return 2
        result = resolve(sdlc_dir, triage._config(sdlc_dir), number, answers, decision, run=run,
                         apply="--dry-run" not in tail,
                         questions_asked=(payload.get("questions") or []))
        print("[%s] #%s: %s" % (result["outcome"], result["number"], result["detail"]))
        return 1 if result["outcome"] == "failed" else 0
    print(_USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
