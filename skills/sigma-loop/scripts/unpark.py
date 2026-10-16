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

    python3 unpark.py list <sdlc_dir> [--assignee X] [--undeclared] [--json]
    python3 unpark.py report <sdlc_dir> [--json]
    python3 unpark.py declare <sdlc_dir> <issue> <kind>
    python3 unpark.py declare <sdlc_dir> --from-suggestions [--kind K] [--dry-run]
    python3 unpark.py brief <sdlc_dir> <issue> [--json]
    python3 unpark.py resolve <sdlc_dir> <issue> --answers <file.json>
                              --decision keep-parked|unpark [--dry-run]
"""
import datetime, json, pathlib, re, sys, importlib.util

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
gate_hold = _load("gate_hold")    # #1005: the hold guard shared with promote
decision_record = _load("decision_record")   # slice 8: store record + attributed comment
rulebook_cfg = _load("rulebook_cfg")         # #1342: public records keep free text out of issue text
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
         "ask_no_ref": "A blocking issue is still open. Do you want to wait for it, work around it, "
                       "or drop this?",
         "options": ["wait for it", "work around it", "drop this"],
         "gestures": {"wait for it": "keep-parked", "work around it": "none", "drop this": "none"}},
        {"id": "workaround", "ask": "If we work around it, what should we do instead?",
         "when": {"route": "work around it"}},
    ],
    "needs_decision": [
        {"id": "decision", "ask": "This stopped for a decision: {reason} Which way do you want it?"},
        {"id": "why", "ask": "Anything the next person should know about why?"},
    ],
    "irreversible": [
        {"id": "go_ahead", "ask": "This step cannot be undone once it runs. Do you want to go "
                                  "ahead?", "options": ["go ahead", "do not", "change the plan"],
         "gestures": {"go ahead": "none", "do not": "none", "change the plan": "none"}},
        {"id": "guardrail", "ask": "What should we check first to be safe?",
         "when": {"go_ahead": "go ahead"}},
    ],
    "merge_conflict": [
        {"id": "route", "ask": "This branch conflicts with the base branch. Rebase it, or start "
                               "fresh?", "options": ["rebase it", "start fresh"],
         "gestures": {"rebase it": "unpark", "start fresh": "none"}},
    ],
    "failing_check": [
        {"id": "route", "ask": "A check is failing: {reason} Fix it as part of this, or file it "
                               "separately?", "options": ["fix it here", "file it separately"],
         "gestures": {"fix it here": "unpark", "file it separately": "none"}},
    ],
    "no_evidence": [
        {"id": "route", "ask": "There is no test proving this works. Add one here, or accept it "
                               "as is?", "options": ["add a test", "accept as is"],
         "gestures": {"add a test": "unpark", "accept as is": "none"}},
    ],
    "review_cap": [
        {"id": "route", "ask": "This stopped on a limit, not a problem. Give it more room, or "
                               "split the work?", "options": ["more room", "split it"],
         "gestures": {"more room": "none", "split it": "none"}},
    ],
    "budget": [
        {"id": "route", "ask": "This stopped on a limit, not a problem. Give it more room, or "
                               "split the work?", "options": ["more room", "split it"],
         "gestures": {"more room": "none", "split it": "none"}},
    ],
    "quota": [
        {"id": "route", "ask": "This stopped on GitHub's own rate limit, not on the goal's work. "
                               "Wait for the quota window to reset, or retry now against a "
                               "different token/account?", "options": ["wait it out", "retry now"],
         "gestures": {"wait it out": "unpark", "retry now": "unpark"}},
    ],
    # #2521: like "budget" above, this is a run-level run_stop reason, never actually written by a
    # real `park` -- kept in the bank rather than exempted (tests/test_unpark.py's own docstring:
    # "a class the loop can actually produce and that we simply forgot is not" a real answer),
    # matching "budget"'s own precedent for the identical shape rather than growing the
    # ("backlog-empty",) exemption tuple.
    "handoff": [
        {"id": "route", "ask": "This session paused to hand off, not because of a problem. Keep "
                               "going with a fresh session, or look into it first?",
         "options": ["keep going", "look into it first"],
         "gestures": {"keep going": "unpark", "look into it first": "keep-parked"}},
    ],
    # #1579: like "handoff", a run-level run_stop reason (a `--label` drain that stopped with open
    # members it cannot pick), never written by a real `park`.
    "label-held": [
        {"id": "route", "ask": "This run stopped with goals still open in its label. Look at what "
                               "holds them, or leave them for now?",
         "options": ["look into it first", "leave them"],
         "gestures": {"look into it first": "keep-parked", "leave them": "keep-parked"}},
    ],
    # #1005: the gate-hold kinds. Releasing either without the registry edit sends the goal back for
    # exactly one pick, so `resolve` refuses until the registry allows it.
    "scope_hold": [
        {"id": "route", "ask": "This goal is in a repository its unit does not list. Add that "
                               "repository to the unit in the registry, or drop the goal?",
         "options": ["add the repository", "drop this"],
         "gestures": {"add the repository": "none", "drop this": "none"}},
    ],
    "owner_hold": [
        {"id": "route", "ask": "The account that opened this is not a recorded owner of its unit. "
                               "Authorize it in the registry, correct the recorded owner, or drop "
                               "the goal?", "options": ["authorize it", "correct the owner", "drop this"],
         "gestures": {"authorize it": "none", "correct the owner": "none", "drop this": "none"}},
    ],
    "reviewer_down": [
        {"id": "route", "ask": "The independent reviewer did not work, so no review was recorded: {reason} "
                               "Fix the reviewer and re-run it (reviewer.py run), review with a different "
                               "host, or choose inline review, which you record with reviewer.py "
                               "accept-inline after setting review.host to inline?",
         "options": ["fix and re-run the reviewer", "choose inline review"],
         "gestures": {"fix and re-run the reviewer": "none", "choose inline review": "none"}},
    ],
    "review_post_ambiguous": [
        {"id": "route", "ask": "A review comment may or may not have reached the PR: {reason} Check the PR's "
                               "comments by hand for the sigma-review-evidence marker. Is the comment there "
                               "(confirm it: work.py reconcile-review-post), or is it not (delete any stray "
                               "copy, then work.py abandon-review-post)?",
         "options": ["the comment is there", "it is not there"],
         "gestures": {"the comment is there": "none", "it is not there": "none"}},
    ],
    # #1578: asked once per run label by `label_drain.py plan`, never by a park. The first slot is the
    # route question; the other two are the pulled-blocker questions the same plan asks.
    "label_set": [
        {"id": "route", "ask": "Arm the unarmed issues in this set?",
         "options": ["arm them", "arm only the ones that can start", "leave them"],
         "gestures": {"arm them": "none", "arm only the ones that can start": "none", "leave them": "none"}},
        {"id": "taken", "ask": "Another person has this issue and this set waits on it: take it over, or "
                               "leave it and wait?", "options": ["take it over", "leave it and wait"],
         "gestures": {"take it over": "none", "leave it and wait": "none"}},
        {"id": "upper", "ask": "This set waits on a larger piece of work that is never picked directly: "
                               "close that dependency, or replace it?",
         "options": ["close the dependency", "replace the dependency"],
         "gestures": {"close the dependency": "none", "replace the dependency": "none"}},
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
                    "options": ["start it again", "leave it parked"],
                    # What the applier may do with each closing answer: 'start it again' only
                    # recommends (a person starts it), 'leave it parked' is keep-parked (#1495).
                    "gestures": {"start it again": "none", "leave it parked": "keep-parked"}}

def route_slot(kind):
    """The first bank slot of `kind` that has options (its route question), or None (#1495)."""
    for slot in QUESTIONS.get(kind) or []:
        if slot.get("options"):
            return slot
    return None


def valid_options(kind):
    """The options a rule or a recorded answer may use for `kind`: the route slot's options, else
    (a kind with no route question) the closing answers. Empty for a kind the bank does not know."""
    if kind not in QUESTIONS:
        return []
    slot = route_slot(kind)
    return list((slot or CLOSING_QUESTION)["options"])


def option_gestures(kind):
    """`{option: 'unpark'|'keep-parked'|'none'}` for `valid_options(kind)` -- the one place an
    option's meaning is declared. Derived from the bank, never stored in a rule."""
    if kind not in QUESTIONS:
        return {}
    return dict((route_slot(kind) or CLOSING_QUESTION).get("gestures") or {})


def normalise_answer(text):
    """Lower-case, whitespace collapsed, a trailing '(recommended)' removed."""
    flat = " ".join(str(text or "").split()).lower()
    if flat.endswith("(recommended)"):
        flat = flat[:-len("(recommended)")].rstrip()
    return flat


def bank_option(kind, text):
    """The bank option of `kind` that `text` names (case-insensitive after normalising), else None."""
    want = normalise_answer(text)
    for opt in valid_options(kind):
        if opt.lower() == want:
            return opt
    return None


# #1547: the routes that WAIT for the park to clear on its own; a keep-parked on one posts no sweep
# opt-out marker, so the automatic sweep still resumes the goal. Every other route posts it.
WAITING_ROUTES = {"dependency": "wait for it", "quota": "wait it out"}


def route_answer(kind, answers):
    """The normalised route answer for `kind`: a bank option, or 'free'; None when the kind has no
    route question or none was answered."""
    slot = route_slot(kind)
    if slot is None and kind in QUESTIONS and kind != "unknown":
        slot = CLOSING_QUESTION                 # #1521: no route question, so the closing answer is the key
    raw = (answers or {}).get(slot["id"]) if slot else None
    if raw is None or not str(raw).strip():
        return None
    return bank_option(kind, raw) or "free"


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


def _suggested(source, number, title):
    """(declared?, suggested kind) for one parked issue: one REST read. The suggestion is the needle
    guess, then the keyword table, and is never a declaration."""
    data = _fetch_issue(source, number)
    comments = data.get("comments") or []
    if qkind.resolve(comments, goal=number):
        return True, None
    reason, _ = _park_reason([sources.defuse_body(c.get("body") or "", c.get("authorAssociation"),
                                                  (c.get("author") or {}).get("login"), number)
                              for c in comments])
    return False, loop._suggest_kind(qkind.strip_line(reason) or title)


def list_parked(sdlc_dir, config, source=None, run=None, assignee=None, undeclared=False):
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
        suggested = None
        if undeclared:                                  # one read per parked issue: linear, never cached
            try:
                declared, suggested = _suggested(source, it.get("number"), it.get("title") or "")
            except Exception:                           # noqa: BLE001 - an unreadable park is not "declared"
                declared, suggested = False, "unknown"
            if declared:
                continue
        rows.append({**({"suggested": suggested} if undeclared else {}), **{"number": str(it.get("number")), "title": it.get("title") or "",
                     "state": (source.goal_blocked_label if source.goal_blocked_label in names
                               else source.parked_label),
                     "labels": sorted(names)}})
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
    declared = qkind.resolve(data.get("comments") or [], goal=number)   # #1498: one resolver, trusted lines
    if reason:
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
    current, block = park_stamp(data.get("comments")), _block_stamp(body)
    if current and block != current and (block or decision_record.enabled(config)):
        # M-01: that block answered an earlier park; #1522: with records open an unstamped (legacy) one did too
        answered = {}
    questions = _questions_for(reason_class, reason, open_refs, answered)
    if not declared and "kind" not in answered:         # #1498: ask for the kind first, suggestion on top
        guess = loop._suggest_kind(reason)
        questions.insert(0, {"id": "kind", "ask": "Which kind of question is this?",
                             "options": [guess] + [k for k in qkind.QKINDS if k not in (guess, "label_set")]})
    learning = _load("autonomy").learning_line(sdlc_dir, reason_class, config)
    extra = _recommendation(sdlc_dir, config, declared, data.get("labels"))
    return {"number": str(number), "title": data.get("title") or "",
            "state": data.get("state"), "labels": sorted(names),
            "park_reason": reason, "park_reason_is_sigmas": from_sigma,
            "reason_class": reason_class, "kind_declared": bool(declared), "decision_tier": tier, "tier_signal": tier_signal,
            "blockers": blockers, "prior_answers": answered,
            "questions": questions, "degraded": [], **({"learning": learning} if learning else {}), **extra}


def _recommendation(sdlc_dir, config, kind, labels):
    """#1496: `{"recommendation": ..., "pair": ...}` for a declared, learnable kind with the rules part
    open and one approved rule whose option is on offer; `{}` otherwise (closed gates add nothing)."""
    try:
        rb = _load("rulebook")
        if not kind or kind == "unknown" or not rb.enabled(config) or not route_slot(kind):
            return {}
        area = decision_record.area_of(labels)
        pre = rb.prefill(sdlc_dir, kind, area, valid_options(kind), config)
        if not pre:
            return {}
        due = pre["rule"] in rb.due_for_reask(rb.load(sdlc_dir, config),
                                              datetime.datetime.now(datetime.timezone.utc), config)
        out = {"recommendation": {"rule_id": pre["rule"], "option": pre["recommend"],
                                  "gesture": option_gestures(kind).get(pre["recommend"], "none"), "due": due}}
        au = _load("autonomy")
        if au.enabled(config):
            row = au._state(sdlc_dir, kind, area, config)
            row.update(au._need(row, config))
            out["pair"] = {k: row.get(k) for k in ("level", "answers", "comparisons", "agreement",
                                                     "next_level", "answers_needed", "comparisons_needed",
                                                     "agreement_needed")}
        return out
    except Exception:                                   # noqa: BLE001 - a recommendation never breaks a brief
        return {}


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
                if not slot.get("ask_no_ref"):
                    continue                  # nothing concrete to name -- an unnamed ref is noise
                text = slot["ask_no_ref"]     # #1495: the route answer must exist for every park of the kind
            else:
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


#: M-01: the body block names the park it answers, so a later park never reads an earlier park's answers.
_STAMP_RE = re.compile(r"<!-- sigma-park: ([A-Za-z0-9_.-]+) -->")


def park_stamp(comments):
    """A stable name for the NEWEST park comment (its id, else its position), or None when there is
    none. `comments` are the raw issue comment dicts."""
    found = None
    for i, c in enumerate(comments or []):
        body = (c.get("body") or "") if isinstance(c, dict) else ""
        if any(body.startswith(pre) for pre in sources.OFFBOARD_COMMENT_PREFIXES):
            cid = c.get("id") or c.get("databaseId")
            found = re.sub(r"[^A-Za-z0-9_.-]", "", str(cid)) if cid else "c%d" % i
    return found or None


def _block_stamp(body):
    at, _spelled = legacy.find_marker(body or "", backlog_check.UNPARK_QA_START)
    if at == -1:
        return None
    m = _STAMP_RE.search((body or "")[at:])
    return m.group(1) if m else None


PRIVATE_VALUE = "recorded privately"


def _keys_only(config):
    """#1342: True only with the records part open AND the repo declared public. Anything else
    (gate closed, private, no config) keeps today's output byte for byte."""
    try:
        return (config is not None and rulebook_cfg.load(config).part_enabled("records")
                and rulebook_cfg.visibility(config) == "public")
    except Exception:                                   # noqa: BLE001 - total: fall back to today's text
        return False


def render_block(answers, questions_asked=(), config=None, park=None):
    """The fenced record, identical in the body and in the comment so the two can never disagree.

    `questions_asked` carries the question TEXT alongside each id, so a reader six weeks later sees
    what was actually asked rather than a bare key -- the answers are only meaningful next to them.
    With `config` public and records on (#1342) each value is `recorded privately`; the key and the
    question text stay.
    """
    keys_only = _keys_only(config)
    asked = {q["id"]: q["ask"] for q in (questions_asked or [])}
    lines = [backlog_check.UNPARK_QA_START, QA_HEADING, ""]
    if park and decision_record.enabled(config):        # M-01: only with records open; closed bytes unchanged
        lines.insert(2, "<!-- sigma-park: %s -->" % park)
    for key, value in answers.items():
        if key in asked:
            lines.append("_%s_" % asked[key])
        lines.append("- **%s** — %s" % (key, PRIVATE_VALUE if keys_only else " ".join(str(value).split())))
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
            questions_asked=(), kind=None, rule_id=None, extra=None):
    """Record the answers, then either keep the park or undo it.

    `kind` (#1498): a kind to declare; its machine line rides inside the decision comment and the
    record is filed under it.

    `decision`:
      `keep-parked` -- record, and post `auto_unpark.KEEP_PARKED_MARKER` so the automatic sweep
                       never re-litigates a decision a human just made. No label changes at all.
      `unpark`      -- ONE atomic swap (`+goal_label`, `-parked_label`/`-goal_blocked_label`), then
                       the board card to `ready`, then the record in the body AND as a comment.

    ORDER, and why: keep-parked changes no label, so its record is the gesture. On the unpark path
    the decision record is written only AFTER the label swap landed (#1304); a failed swap keeps the
    typed answers (plain comment + body) but records no decision. An issue that is not parked is
    refused on both paths and records nothing.

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
                "detail": "could not read the current state of #%s: %s" % (n, sources._fx(exc))}
    if str(data.get("state") or "").upper() == "CLOSED":
        return {"number": n, "decision": decision, "outcome": "failed",
                "detail": "#%s is closed — reopen it first if it should be worked" % n}

    names = _names(data)
    if source.parked_label not in names and source.goal_blocked_label not in names:
        # #1304: a gesture on a goal that is not parked changes nothing, so it records nothing --
        # a "kept-parked" or "start it again" record for it would teach the Rulebook a decision
        # that never happened.
        return {"number": n, "decision": decision, "outcome": "failed",
                "detail": "#%s is not parked (no %s or %s label) — nothing was recorded"
                          % (n, source.parked_label, source.goal_blocked_label)}
    if decision == "unpark" and gate_hold.declared_gate_kind(
            [c.get("body") or "" for c in (data.get("comments") or [])]):
        # #1005: the hold guard `/sigma-promote` already has. A gate hold is released by a registry edit,
        # not by a label; unparking without it returns the goal for one pick and it is parked again.
        held = gate_hold.gate_hold_blocks(source, n, config, sdlc_dir=sdlc_dir, names=names,
                                          gesture="unpark")
        if held:
            return {"number": n, "decision": decision, "outcome": "failed", "detail": held}
    stamp = park_stamp(data.get("comments"))
    block = render_block(answers, questions_asked, config, park=stamp)
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
        # F-02(4) / #1547: a waiting route (`WAITING_ROUTES`) posts NO opt-out marker, so the sweep
        # still resumes a dependency park when its blocker closes; a quota park has no blocker
        # refs, so the sweep skips it and it stays parked until unparked by hand. #1549: a
        # dependency wait also posts the waiting note, which is what lets the sweep resume a park.
        declared = _declared_kind(data)
        waits = declared in WAITING_ROUTES and route_answer(declared, answers) == WAITING_ROUTES[declared]
        err = _comment_decision(sdlc_dir, config, source, n, block + (("\n\n" + auto_unpark.WAITING_NOTE if declared == "dependency" else "") if waits else "\n\n"
                                + auto_unpark.keep_parked_comment(_one_line(
                                    PRIVATE_VALUE if _keys_only(config) else _substantive_reason(answers)))),
                                "keep-parked", answers, data, kind, rule_id, extra)
        _append_block(source, n, data.get("body") or "", block)
        return {"number": n, "decision": decision, "outcome": "kept-parked",
                "detail": "answers recorded; the automatic sweep will leave it alone" + err}

    try:
        source._swap_labels(n, add=add, remove=remove)
    except Exception as exc:                            # noqa: BLE001 - reported, never raised
        # Review bug_001: keep the typed answers (comment + body block) so a retry asks nothing.
        # #1304: but the DECISION did not happen, so no decision record, store file or applier
        # close is written; those come only from the landed path below.
        _comment(source, n, block)
        _append_block(source, n, data.get("body") or "", block)
        return {"number": n, "decision": decision, "outcome": "failed",
                "detail": "label write did not land: %s — the answers were recorded, so re-running "
                          "this will not ask them again" % sources._fx(exc)}
    detail = triage._swap_detail(add, remove)
    if not source._set_board_status(n, source.col["ready"]) and source.board_active:
        detail += ("; the label landed but the board card did not move to %r — move it by hand if "
                   "this repo picks from the board" % source.col["ready"])
    detail += _comment_decision(sdlc_dir, config, source, n, block, "unpark", answers, data, kind, rule_id, extra)
    _append_block(source, n, data.get("body") or "", block)
    return {"number": n, "decision": decision, "outcome": "unparked", "detail": detail}


def _declared_kind(data):
    """The kind a trusted park or declaration comment declared (the newest wins), else `unknown`."""
    return qkind.resolve(data.get("comments") or [], goal=data.get("number")) or "unknown"


def _comment_decision(sdlc_dir, config, source, number, text, gesture, answers, data, kind=None, rule_id=None,
                      extra=None):
    """Post `text`. With the records part open it is posted WITH the decision record's id and the
    record is stored; closed, this is exactly `_comment` (byte-identical). Returns "" or a
    "; <error>" suffix for the caller's detail."""
    try:                                                # #1178: a person's answer closes the applier's ask
        _load("applier").close_for_issue(sdlc_dir, config, number, str(
            (answers or {}).get(CLOSING_QUESTION["id"]) or gesture), refuse=gesture == "keep-parked")
    except Exception:                                   # noqa: BLE001 - never block the gesture
        pass
    if kind:
        text = text + "\n" + qkind.render_line(kind)    # #1498: the declaration rides in the decision
    tried = []                                          # the comment was attempted (never post twice)
    warn = ""
    try:
        if decision_record.enabled(config):
            choice = (answers or {}).get(CLOSING_QUESTION["id"]) or gesture
            kind = kind or _declared_kind(data)
            rec = decision_record.build(gesture, number, kind, choice,
                                        _substantive_reason(answers), decision_record.how_for(gesture),
                                        area=decision_record.area_of(data.get("labels")),
                                        hardstop_class=qkind.resolve_hardstop(data.get("comments") or [],
                                                                              goal=number))
            ans = route_answer(kind, answers)
            if rule_id:                                 # #1496: the person was shown this rule's recommendation
                rec["rule_id"], rec["shown"] = str(rule_id), True
            if ans and not (ans == "free" and decision_record.visibility(config) == "public"):
                rec["answer"] = ans                     # F-01: the key rules and comparisons share
            if rule_id:
                shown = next((r for r in _load("rulebook").load(sdlc_dir, config) if r.get("id") == rule_id), None)
                rec["accepted"] = bool(shown) and decision_record.key_of(rec) == shown.get("option")
            if isinstance(extra, dict):                 # #1580: fields a caller adds (`scope`, `qid`)
                rec.update({k: v for k, v in extra.items() if k not in rec})
            park = park_stamp(data.get("comments"))
            if park:
                rec["park"] = park
                if decision_record.answered_park(sdlc_dir, config, number, park):
                    rec["repeat"] = True                # M-01: one counted answer per (issue, park)
            if ans is None and route_slot(kind):
                warn = "; warning: no route answer was given (%s), so none is recorded" % route_slot(kind)["id"]

            def _post(num, body):
                tried.append(num)
                source._issue_comment(num, body)
            out = decision_record.write_all(sdlc_dir, rec, config, source, text=text, poster=_post)
            return warn + ("; " + out["error"] if out["error"] else "")
    except Exception as exc:                            # noqa: BLE001 - never break the gesture
        if not tried:
            _comment(source, number, text)
        return "; decision record failed: %s" % exc
    _comment(source, number, text)
    return ""


def declare(sdlc_dir, config, number, kind, source=None, run=None):
    """Declare the kind of an existing park (#1498): one comment, `Question kind set: <kind>` plus the
    machine line. Changes no label, writes no ledger event. -> (ok, detail)."""
    if kind not in qkind.QKINDS:
        return False, "unknown kind %r (expected one of %s)" % (kind, ", ".join(qkind.QKINDS))
    source = source or sources.GitHubSource(config, run=run)
    if not _github(source):
        return False, "unparking needs github discovery mode"
    try:
        data = _fetch_issue(source, number)
    except Exception as exc:                            # noqa: BLE001 - a usable refusal
        return False, "could not read #%s: %s" % (number, sources._fx(exc))
    names = _names(data)
    if source.parked_label not in names and source.goal_blocked_label not in names:
        return False, "#%s is not parked — nothing was declared" % number
    try:
        source._issue_comment(number, qkind.DECLARATION_PREFIX + kind + "\n" + qkind.render_line(kind))
    except Exception as exc:                            # noqa: BLE001
        return False, "the declaration comment did not post: %s" % sources._fx(exc)
    return True, "declared %s on #%s" % (kind, number)


#: #1562: kinds a bulk declare never writes: a hard stop is a human's call, never a guess.
BULK_NEVER = ("irreversible", "unknown", "scope_hold", "owner_hold", "reviewer_down", "review_post_ambiguous")


def declare_from_suggestions(sdlc_dir, config, only=None, apply=True, run=None):
    """Declare every undeclared park whose suggestion is a confident kind, through `declare` (one comment
    each). `only` restricts to parks suggesting that kind. -> (lines, failed): `[declared] #N: kind` or
    `[skipped] #N: reason`; `apply=False` prints the same lines and writes nothing."""
    listing = list_parked(sdlc_dir, config, run=run, undeclared=True)
    lines, failed = [], bool(listing.get("degraded"))
    for row in listing["issues"]:
        number, kind = row["number"], row.get("suggested") or "unknown"
        if kind in BULK_NEVER:
            why = "hard-stop kind %s is never bulk-declared" % kind if kind == "irreversible" else \
                "no confident suggestion (%s)" % kind
            lines.append("[skipped] #%s: %s" % (number, why))
        elif only and kind != only:
            lines.append("[skipped] #%s: suggestion %s is not --kind %s" % (number, kind, only))
        elif not apply:
            lines.append("[declared] #%s: %s" % (number, kind))
        else:
            ok, detail = declare(sdlc_dir, config, number, kind, run=run)
            failed = failed or not ok
            lines.append("[declared] #%s: %s" % (number, kind) if ok else "[skipped] #%s: %s" % (number, detail))
    return lines, failed


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
                             "will not see them inline\n" % (number, sources._fx(exc)))
        except Exception:
            pass
        return False


def _applier_verdict(sdlc_dir, config, ap, number, kind, declared, area, comments, now, asks, used):
    """(rule, option, gesture, verdict, why) for one park, in the order `applier.open_for_park` and
    `applier.tick` decide: what would stop the applier first is named. Nothing is written."""
    hs, rb = _load("hard_stop"), _load("rulebook")
    if not ap.enabled(config):
        return None, None, None, "never", "the applier is off"
    if not declared:
        return None, None, None, "never", "kind not declared (a suggested kind never acts)"
    if hs.is_hardstop_kind(kind, None, config):
        return None, None, None, "never", "hard-stop kind"
    if kind in ("unknown", "scope_hold", "owner_hold"):
        return None, None, None, "never", "kind %s is never applied automatically" % kind
    rules = rb.load(sdlc_dir, config)
    ask = next((a for a in asks if ap._issue_key(a.get("issue")) == ap._issue_key(number)
                and a.get("state") == "pending" and a.get("answer") is None), None)
    # tick's own call (the pending ask's options, no config); open_for_park's when there is no ask yet
    m = (rb.match(kind, area, ask.get("options"), rules) if ask is not None
         else rb.match(kind, area, None, rules, config))
    if m is None:
        return None, None, None, "never", "no approved rule for %s / %s" % (kind, area)
    gesture = ap.option_gesture(kind, m.option)
    if gesture not in ap.GESTURES:
        return m.rule_id, m.option, gesture or "none", "never", "the rule only recommends"
    out = (m.rule_id, m.option, gesture)
    park = park_stamp(comments)
    if park and decision_record.answered_park(sdlc_dir, config, number, park):
        return (*out, "never", "a person already answered this park")
    auto_rule = ap._auto_rule(rules, m.rule_id, config)       # #1555: tick's order, veto then level
    if auto_rule is not None and ap._vetoed(sdlc_dir, config, kind, area, auto_rule):
        return (*out, "never", "held: an owner vetoed this auto-approved rule; it waits for a hand reconfirmation")
    level = ap._level(sdlc_dir, kind, area, config)
    if not ap._level_ok(sdlc_dir, config, kind, area, auto_rule):
        return (*out, "never", "the pair is at level %s, below 3" % level)
    if m.rule_id in set(rb.due_for_reask(rules, now, config)):
        return (*out, "never", "the rule is due to be reconfirmed by an owner")
    rule = next((r for r in rules if r.get("id") == m.rule_id), None)
    if auto_rule is None and not ap._rule_has_own_support(sdlc_dir, kind, area, rule, config):
        return (*out, "never", "the rule lacks its own live comparisons")
    if ask is None:
        return (*out, "later", "no ask yet: it opens at the next sync, then waits its window")
    if "armed_at" in ask and ask.get("armed_at") is None:
        return (*out, "later", "arms at the next tick, then waits its window")
    if ap.window_open(ask, config, now):
        return (*out, "later", "inside the window, an answer or no still wins")
    if ap._quota_wait_pending(ask, m, now):
        return (*out, "later", "waiting for the quota to reset")
    if used >= ap.settings(config)["max_auto_applied_per_day"]:
        return (*out, "later", "the daily cap is used up")
    return (*out, "now", "window passed")


def report(sdlc_dir, config, source=None, run=None, assignee=None, now=None):
    """Every parked or blocked issue and what the applier would do with it. READ-ONLY: the same two
    label queries as `list`, then one REST read per park (as `list --undeclared` does); no gesture,
    comment, label or file is written. `totals` counts the outcomes now, later and never."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    source = source or sources.GitHubSource(config, run=run)
    listed = list_parked(sdlc_dir, config, source=source, assignee=assignee)
    ap = _load("applier")
    asks = ap._asks(sdlc_dir) if ap.enabled(config) else []
    used = ap._applied_in_last_day(asks, now) if asks else 0
    au = _load("autonomy")
    rows = []
    for it in listed["issues"]:
        number = it["number"]
        row = {"number": number, "title": it["title"], "state": it["state"]}
        try:
            data = _fetch_issue(source, number)
            raw = data.get("comments") or []
            declared = qkind.resolve(raw, goal=number)
            reason, _ = _park_reason([sources.defuse_body(c.get("body") or "", c.get("authorAssociation"),
                                                          (c.get("author") or {}).get("login"), number)
                                      for c in raw])
            kind = declared or loop._suggest_kind(qkind.strip_line(reason) or it["title"])
            area = decision_record.area_of(data.get("labels"))
            rule, option, gesture, verdict, why = _applier_verdict(
                sdlc_dir, config, ap, number, kind, bool(declared), area, raw, now, asks, used)
            row.update(kind=kind, declared=bool(declared), area=area, rule=rule, option=option,
                       gesture=gesture, level=au.level(sdlc_dir, kind, area, config), verdict=verdict, why=why)
        except Exception as exc:                        # noqa: BLE001 - one unreadable park never hides the rest
            row.update(kind=None, declared=False, area=None, rule=None, option=None, gesture=None,
                       level=None, verdict="never", why="could not be read: %s" % sources._fx(exc))
        rows.append(row)
    totals = {k: sum(1 for r in rows if r["verdict"] == k) for k in ("now", "later", "never")}
    return {"issues": rows, "totals": totals, "complete": listed["complete"], "degraded": listed["degraded"]}


def render_report(result):
    lines = []
    for r in result["issues"]:
        kind = "%s" % r["kind"] if r["declared"] else "not declared (suggested: %s)" % r["kind"]
        rule = ("rule %s -> %s" % (r["rule"], r["gesture"])) if r["rule"] else "no rule"
        lines.append("#%s [%s] %s" % (r["number"], r["state"], r["title"]))
        lines.append("  kind: %s; area: %s; %s; level %s" % (kind, r["area"], rule, r["level"]))
        lines.append("  applier: %s -- %s" % (r["verdict"], r["why"]))
    if not result["issues"]:
        lines.append("none parked")
    for note in result.get("degraded") or []:
        lines.append("NOTE: %s" % note)
    lines += ["%s: %d" % (k, result["totals"][k]) for k in ("now", "later", "never")]
    return "\n".join(lines)


def render_list(result):
    scope = result.get("assignee")
    lines = ["parked work: %s" % ("scoped to @%s" % scope if scope
                                  else "every parked issue (discovery.github.assignee is not set)")]
    for r in result["issues"]:
        lines.append("  #%s [%s] %s%s" % (r["number"], r["state"], r["title"],
                                         "  (suggested: %s)" % r["suggested"] if r.get("suggested") else ""))
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
    if b.get("learning"):
        lines.append("  %s" % b["learning"])
    rec, pair = b.get("recommendation"), b.get("pair")
    if rec:
        lines.append("  approved rule %s recommends: %s (will %s)%s" % (
            rec["rule_id"], rec["option"], {"unpark": "unpark", "keep-parked": "keep parked"}.get(
                rec["gesture"], "only recommend"), "; due to be re-asked" if rec.get("due") else ""))
    if pair:
        have = "none yet" if pair["agreement"] is None else "%d%%" % round(pair["agreement"] * 100)
        lines.append("  pair: level %s, %s answers, %s comparisons, agreement %s%s" % (
            pair["level"], pair["answers"], pair["comparisons"], have,
            "; level %s needs %s answers, %s comparisons at %d%%" % (
                pair["next_level"], pair["answers_needed"], pair["comparisons_needed"],
                round(pair["agreement_needed"] * 100)) if pair.get("next_level") and pair.get("agreement_needed") is not None
            else ""))
    lines.append("  questions:")
    for q in b["questions"]:
        opts = ("  [%s]" % " / ".join(q["options"])) if q.get("options") else ""
        lines.append("    - %s%s" % (q["ask"], opts))
    return "\n".join(lines)


_USAGE = ("usage: unpark.py list <sdlc_dir> [--assignee X] [--undeclared] [--json]\n"
          "       unpark.py report <sdlc_dir> [--json]\n"
          "       unpark.py declare <sdlc_dir> <issue> <kind>\n"
          "       unpark.py declare <sdlc_dir> --from-suggestions [--kind K] [--dry-run]\n"
          "       unpark.py brief <sdlc_dir> <issue> [--json]\n"
          "       unpark.py resolve <sdlc_dir> <issue> --answers <file.json> "
          "--decision keep-parked|unpark [--qkind K] [--dry-run]")


def _value(tail, name):
    if name in tail:
        i = tail.index(name)
        return tail[i + 1] if i + 1 < len(tail) else None
    return None



def main(argv, run=None):
    try:
        return _main(argv, run)
    except triage.state.ConfigInvalid as exc:      # D7: the file, the line and the fix
        print(f"unpark.py: {exc}", file=sys.stderr)
        return 2


def _main(argv, run=None):
    if argv[1:] in (["-h"], ["--help"]):
        print(_USAGE)
        return 0
    if len(argv) >= 3 and argv[1] == "list":
        sdlc_dir, tail = argv[2], argv[3:]
        blocked = triage.needs_github(triage.config_strict(sdlc_dir), "unparking", 1, run, None)
        if blocked is not None:
            return blocked
        result = list_parked(sdlc_dir, triage.config_strict(sdlc_dir), run=run,
                             assignee=_value(tail, "--assignee"),
                             undeclared="--undeclared" in tail)
        print(json.dumps(result, indent=2) if "--json" in tail else render_list(result))
        return 0 if result["complete"] else 1
    if len(argv) >= 3 and argv[1] == "report":
        sdlc_dir, tail = argv[2], argv[3:]
        blocked = triage.needs_github(triage.config_strict(sdlc_dir), "unparking", 1, run, None)
        if blocked is not None:
            return blocked
        result = report(sdlc_dir, triage.config_strict(sdlc_dir), run=run)
        print(json.dumps(result, indent=2) if "--json" in tail else render_report(result))
        return 0 if result["complete"] else 1
    if len(argv) >= 4 and argv[1] == "declare" and "--from-suggestions" in argv[3:]:
        sdlc_dir, tail = argv[2], argv[3:]
        only = _value(tail, "--kind")
        if only is not None and only not in qkind.QKINDS:
            print("unpark.py declare: unknown --kind %r (expected one of %s)" % (only, ", ".join(qkind.QKINDS)),
                  file=sys.stderr)
            return 2
        blocked = triage.needs_github(triage.config_strict(sdlc_dir), "unparking", 1, run, None)
        if blocked is not None:
            return blocked
        lines, failed = declare_from_suggestions(sdlc_dir, triage.config_strict(sdlc_dir), only=only,
                                                 apply="--dry-run" not in tail, run=run)
        print("\n".join(lines))
        return 1 if failed else 0
    if len(argv) >= 5 and argv[1] == "declare":
        sdlc_dir, number, kind = argv[2], argv[3], argv[4]
        if kind not in qkind.QKINDS:
            print("unpark.py declare: unknown kind %r (expected one of %s)" % (kind, ", ".join(qkind.QKINDS)),
                  file=sys.stderr)
            return 2
        blocked = triage.needs_github(triage.config_strict(sdlc_dir), "unparking", 1, run, None)
        if blocked is not None:
            return blocked
        ok, detail = declare(sdlc_dir, triage.config_strict(sdlc_dir), number, kind, run=run)
        print(detail, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    if len(argv) >= 4 and argv[1] == "brief":
        sdlc_dir, number, tail = argv[2], argv[3], argv[4:]
        blocked = triage.needs_github(triage.config_strict(sdlc_dir), "unparking", 1, run, None)
        if blocked is not None:
            return blocked
        try:
            b = brief(sdlc_dir, triage.config_strict(sdlc_dir), number, run=run)
        except Exception as exc:                        # noqa: BLE001 - a usable refusal, not a trace
            # Names the ISSUE, not just the transport error: "simulated gh failure: issue view 5"
            # tells a human nothing they can act on, and a bare traceback tells them less.
            print("unpark.py brief: could not read #%s — %s" % (number, sources._fx(exc)), file=sys.stderr)
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
            print("unpark.py resolve: could not read --answers %r: %s -- write the interview answers as "
                  '{"answers": {...}} to that file (`unpark.py brief` prints the questions), then '
                  "re-run" % (answers_path, exc), file=sys.stderr)
            return 2
        answers = payload.get("answers") if isinstance(payload, dict) else None
        if not isinstance(answers, dict):
            print("unpark.py resolve: --answers must be a JSON object with an \"answers\" object",
                  file=sys.stderr)
            return 2
        blocked = triage.needs_github(triage.config_strict(sdlc_dir), "unparking", 1, run, None)
        if blocked is not None:
            return blocked
        kind = _value(tail, "--qkind")
        if kind is not None and kind not in qkind.QKINDS:
            print("unpark.py resolve: unknown --qkind %r (expected one of %s)" % (kind, ", ".join(qkind.QKINDS)),
                  file=sys.stderr)
            return 2
        result = resolve(sdlc_dir, triage.config_strict(sdlc_dir), number, answers, decision, run=run,
                         apply="--dry-run" not in tail, kind=kind,
                         questions_asked=(payload.get("questions") or []),
                         rule_id=payload.get("rule_id") if isinstance(payload.get("rule_id"), str) else None)
        print("[%s] #%s: %s" % (result["outcome"], result["number"], result["detail"]))
        return 1 if result["outcome"] == "failed" else 0
    print(_USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
