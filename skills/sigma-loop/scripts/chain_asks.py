"""chain_asks.py -- the questions Chain asks BEFORE a run about members that need a person's word, and what each
answer does. Library only; no main.

THE RULE. A chain sheet asks only what changes what `arm` or `run` does, and asks all of it at once, deep members
included. Four things used to be left to the run: a member that is parked, a wait read only from prose, a member
somebody else holds, and a paid step marked only by a label. The run stopped on them with no question asked. Each
is now ONE question on the first sheet, and each answer is enacted (below), never "recorded here only":

  park:N          #N is parked. Why (its recorded park question, else the first line of its park note or body, cut
                  short; wording withheld when the records are keys-only). `keep it parked` (recommended): the chain
                  stops before it and the members that wait on it, named. `release it`: `arm` releases it through the
                  existing atomic unpark gesture (`unpark.resolve`, decision `unpark`), declaring the kind with the
                  recommended guess when none is declared. It replaces the old per-kind question (and its 13-option
                  "which kind is this?"): the kind is only needed to release, and then it is declared, not asked.
  wait:A-B        #A waits on #B, read from prose (`chain_waits`), and #B is open and the wait changes the order of
                  the chain (an edge the explicit edges already imply is not asked). `confirm it` (recommended):
                  `arm` writes the marker every reader trusts, the same safe write `deps.py mark` does. `dismiss
                  it`: `arm` posts the dismissal marker (`backlog_check.dismiss_comment`, posted as `loop.py note`
                  posts it), and the plan, the forecast and the run drop the wait. A wait on a CLOSED issue is
                  informational and is not asked.
  elsewhere:N     #N is assigned to someone else, or in progress in another session. `wait for them` (recommended):
                  the run pauses there and names whose it is. `take it over`: `arm` assigns the runner and removes
                  the old assignee, one assignee per issue. Never offered for a member in progress: then only
                  `wait for them` is, and the sheet says why.
  irreversible:N  the existing spend question, now also asked for a member that carries a label listed in
                  `chain.paid_labels` (default none), not only for one whose body declares a paid line.

`view(rec)` is the record as the answers leave it: a released member is armed, a kept-parked one keeps a verdict of
its own, a taken-over one is no longer someone else's, a dismissed wait is no edge. The sheet's forecast, `arm` and
`run` read the view, so the forecast is computed from the answers and equals the run. The record itself is never
rewritten (an answer can be changed). Every function here is pure: it reads the record, never GitHub.
"""
PARK, WAIT, ELSEWHERE = "park", "wait", "elsewhere"
KEEP_PARKED, RELEASE = "keep it parked", "release it"
CONFIRM, DISMISS = "confirm it", "dismiss it"
WAIT_FOR_THEM, TAKE_OVER = "wait for them", "take it over"
PARK_OPTIONS = [KEEP_PARKED, RELEASE]                  # the first of each is the recommended answer: nothing starts unasked
WAIT_OPTIONS = [CONFIRM, DISMISS]
ELSEWHERE_OPTIONS = [WAIT_FOR_THEM, TAKE_OVER]
ELSEWHERE_WAIT_ONLY = [WAIT_FOR_THEM]
#: the kinds that are one decision each about named members, whose text is what the person decides on (a group
#: question prints only "N questions share these options" and would hide it): the sheet never folds them into a group
UNGROUPED = (PARK, WAIT, ELSEWHERE, "irreversible", "closed_gate", "cross_unit")
WHY_CHARS = 140
NAMES_SHOWN = 8
KEYS_ONLY_WHY = "(wording withheld: this repository's decision records are keys-only)"
NO_WHY = "(no park note was recorded)"

def paid_labels(config):
    """-> the labels `chain.paid_labels` lists (default none): a member carrying one gets the spend question even
    though its body declares no paid line. Raises ValueError for a value that is not a list of label names, so a
    misspelt key is a refusal, never a silently unasked spend question."""
    block = (config or {}).get("chain")
    if not isinstance(block, dict) or block.get("paid_labels") is None:
        return []
    v = block["paid_labels"]
    if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
        raise ValueError("chain.paid_labels must be a list of label names")
    return [x.strip() for x in v]


def carries_paid_label(names, config):
    """True when one of the member's label `names` is listed in `chain.paid_labels` (case-insensitive)."""
    want = {x.casefold() for x in paid_labels(config)}
    return bool(want) and any(str(n).casefold() in want for n in names)


KEPT_PARKED = "kept-parked"            # the verdict `view` gives a parked member the person answered to keep parked


def _ids(nums):
    return ", ".join("#%d" % n for n in nums)


def _cut(text, limit=WHY_CHARS):
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[:limit - 1].rstrip() + "…"


def park_why(brief, keys_only=False):
    """-> the one short line that says why a member is parked, from an `unpark.brief` payload: its recorded park
    question (the park reason), else the first line of its body. Cut to WHY_CHARS. Secret-shaped text is
    redacted; with keys-only records the wording is withheld. Never a title."""
    if keys_only:
        return KEYS_ONLY_WHY
    scrub = _scrub()
    text = _cut(brief.get("park_reason")) or _cut(brief.get("body_head"))
    return scrub(text) if text else NO_WHY


def _scrub():
    import importlib.util
    import pathlib
    here = pathlib.Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location("scrub", here / "scrub.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m.scrub


def kind_guess(brief):
    """-> the recommended kind of an undeclared park (the first option of the brief's own kind question), else None."""
    for q in brief.get("questions") or []:
        if q.get("id") == "kind" and q.get("options"):
            return q["options"][0]
    return None


def _waits(rec):
    return {r["number"]: list(r.get("waits_on") or []) for r in rec["members"]}


def dependents_of(rec, n):
    """-> every member that waits on `n`, directly or through other members, sorted."""
    waits, seen, todo = _waits(rec), set(), [n]
    while todo:
        cur = todo.pop()
        for m, bs in waits.items():
            if cur in bs and m not in seen:
                seen.add(m)
                todo.append(m)
    return sorted(seen)


def _named(nums):
    nums = list(nums)
    shown = _ids(nums[:NAMES_SHOWN])
    return shown + (" and %d more" % (len(nums) - NAMES_SHOWN) if len(nums) > NAMES_SHOWN else "")


# --- park ---------------------------------------------------------------------------------------------------

def park_questions(rec):
    """-> one question per parked member of the plan, in number order."""
    out = []
    for r in sorted(rec["members"], key=lambda x: x["number"]):
        if r["verdict"] != "parked":
            continue
        n = r["number"]
        behind = dependents_of(rec, n)
        why = r.get("park_why") or NO_WHY
        stop = ("keep it parked and the chain stops before it%s" % (" and before %s, which wait%s on it" % (
            _named(behind), "s" if len(behind) == 1 else "")) if behind else "keep it parked and the chain stops before it")
        out.append({"id": "%s:%d" % (PARK, n), "kind": PARK, "area": None,
                    "ask": "#%d is parked: %s Release it into this chain, or %s?" % (
                        n, why if why[-1:] in ".?!…)" else why + ".", stop),
                    "options": list(PARK_OPTIONS), "unblocks": sorted([n] + behind)})
    return out


# --- wait ----------------------------------------------------------------------------------------------------

def _implied(rec, blocker, dependent):
    """True when `dependent` already waits on `blocker` through explicit edges of other members (so the prose wait
    cannot change the order). Pure graph search over the record's explicit edges, skipping the edge itself."""
    explicit = {}
    for e in rec.get("edges") or []:
        if e.get("tag") == "explicit":
            explicit.setdefault(e["to"], set()).add(e["from"])
    seen, todo = set(), [x for x in explicit.get(dependent, ()) if x != blocker]
    while todo:
        cur = todo.pop()
        if cur == blocker:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        todo += list(explicit.get(cur, ()))
    return False


def _where(item):
    if item.get("where") == "comment":
        return "a comment by the %s" % (item.get("by") or "assignee")
    return "the body"


def open_waits(rec):
    """-> the unconfirmed waits the sheet asks about: the blocker is OPEN and in the chain, the wait is not already
    implied by explicit edges. A wait on a closed issue is informational."""
    members = {r["number"] for r in rec["members"]}
    out = []
    for i in rec.get("unconfirmed_waits") or []:
        if i.get("state") != "open" or i["ref"] not in members or i["goal"] not in members:
            continue
        if _implied(rec, i["ref"], i["goal"]):
            continue
        out.append(i)
    return out


def wait_questions(rec):
    out = []
    for i in open_waits(rec):
        out.append({"id": "%s:%d-%d" % (WAIT, i["goal"], i["ref"]), "kind": WAIT, "area": None,
                    "ask": "#%d waits on #%d (read from %s): confirm it, or dismiss it?" % (
                        i["goal"], i["ref"], _where(i)),
                    "options": list(WAIT_OPTIONS), "unblocks": sorted({i["goal"], i["ref"]})})
    return out


def dismissed_waits(rec):
    """-> {(dependent, blocker)} the person answered `dismiss it`."""
    ans = rec.get("answers") or {}
    return {(i["goal"], i["ref"]) for i in open_waits(rec) if ans.get("%s:%d-%d" % (WAIT, i["goal"], i["ref"])) == DISMISS}


def confirmed_waits(rec):
    ans = rec.get("answers") or {}
    return {(i["goal"], i["ref"]) for i in open_waits(rec) if ans.get("%s:%d-%d" % (WAIT, i["goal"], i["ref"])) == CONFIRM}


# --- elsewhere -----------------------------------------------------------------------------------------------

def whose(row):
    """-> who holds a member, in words that carry logins and no title: `assigned to bob`, `in progress in another
    session`, or both. Empty when it is nobody's."""
    parts = []
    logins = row.get("assignees") or []
    if logins:
        parts.append("assigned to %s" % ", ".join(logins))
    if row.get("in_progress"):
        parts.append("in progress in another session")
    return " and ".join(parts)


def held_elsewhere(row, runner):
    """True when a run by `runner` cannot take the member because someone else holds it: its verdict (or the
    verdict before the assignee clause) is assigned-elsewhere, an unarmed or PARKED member assigned to someone else
    (releasing the parked one would hand the run a member somebody else holds, so that is asked up front too), or the
    in-progress label is on it (a session holds it, whoever it is assigned to)."""
    if row["verdict"] in ("checkpoint", "closed", "upper-tier"):
        return False
    if row["verdict"] == "assigned-elsewhere":
        return True
    if row.get("in_progress"):          # the label says a session holds it, whoever it is assigned to
        return True
    others = [x for x in row.get("assignees") or [] if not runner or x.casefold() != runner.casefold()]
    return bool(runner) and bool(others) and (
        row["verdict"] in ("unarmed", "parked", KEPT_PARKED) or row["verdict"] in _label_drain().GATED_KIND)


def elsewhere_questions(rec):
    runner = rec.get("runner")
    out = []
    for r in sorted(rec["members"], key=lambda x: x["number"]):
        if not held_elsewhere(r, runner):
            continue
        n = r["number"]
        behind = dependents_of(rec, n)
        tail = " The run pauses there and names whose it is%s." % (
            "; %s wait%s behind it" % (_named(behind), "s" if len(behind) == 1 else "") if behind else "")
        if r.get("in_progress"):
            ask = ("#%d is %s. It is in progress in another session, so it cannot be taken over from here: wait "
                   "for them.%s" % (n, whose(r), tail))
            opts = list(ELSEWHERE_WAIT_ONLY)
        else:
            ask = "#%d is %s%s. Wait for them, or take it over (assign %s and remove the others)?%s" % (
                n, whose(r), " and also parked: this matters only if you release it" if r["verdict"] == "parked" else "",
                runner or "the runner", tail)
            opts = list(ELSEWHERE_OPTIONS)
        if rec.get("planner") and rec["planner"] != runner:        # #1935: the plan was made by one login for another
            ask += " Planned by %s for %s." % (rec["planner"], runner or "the runner")
        out.append({"id": "%s:%d" % (ELSEWHERE, n), "kind": ELSEWHERE, "area": None, "ask": ask, "options": opts,
                    "unblocks": sorted([n] + behind)})
    return out


def taken_over(rec):
    """-> the members the person answered `take it over` about and that may be taken over: not in progress, and not
    one that stays parked or left out (`_inert`)."""
    ans = rec.get("answers") or {}
    return {r["number"] for r in rec["members"]
            if held_elsewhere(r, rec.get("runner")) and not r.get("in_progress") and not _inert(rec, r)
            and ans.get("%s:%d" % (ELSEWHERE, r["number"])) == TAKE_OVER}


def waiting_for(rec):
    """-> {member: whose} for the members held elsewhere that the person chose to wait for (or could only wait for)."""
    taken = taken_over(rec)
    return {r["number"]: whose(r) for r in rec["members"]
            if held_elsewhere(r, rec.get("runner")) and r["number"] not in taken}


# --- parked answers --------------------------------------------------------------------------------------------

def released(rec):
    ans = rec.get("answers") or {}
    return {r["number"] for r in rec["members"]
            if r["verdict"] == "parked" and ans.get("%s:%d" % (PARK, r["number"])) == RELEASE}


def kept_parked(rec):
    """-> the parked members the person answered `keep it parked` about (also in a `view`, where they carry KEPT_PARKED)."""
    ans = rec.get("answers") or {}
    return {r["number"] for r in rec["members"]
            if r["verdict"] == KEPT_PARKED
            or (r["verdict"] == "parked" and ans.get("%s:%d" % (PARK, r["number"])) == KEEP_PARKED)}


# --- a unit boundary the person answered "stop here" about --------------------------------------------------------------

STOP_HERE = "stop here"


def stopped_here(rec):
    """-> {dependent: blocker}: a cross-unit edge the person answered `stop here` about. The dependent is held (never
    armed, skipped by every run cycle) and what waits behind it with it; `keep going` holds nothing."""
    ans = rec.get("answers") or {}
    return {x["to"]: x["from"] for x in rec.get("cross_unit") or []
            if ans.get("cross_unit:%d-%d" % (x["from"], x["to"])) == STOP_HERE}


def stop_context(rec):
    """-> what the words of a stop need beyond the members: {"kept": parked members kept parked, "waiting": {member:
    whose} for those held elsewhere that the person waits for, "stopped": {dependent: blocker} at a unit boundary}.
    Words only: the reason of a stop never depends on it, so the forecast's and the run's stop lines stay equal."""
    return {"kept": kept_parked(rec), "waiting": waiting_for(view(rec)), "stopped": stopped_here(rec)}


# --- the record as the answers leave it ----------------------------------------------------------------------------

_LD = []


def _label_drain():
    if not _LD:
        import importlib.util
        import pathlib
        spec = importlib.util.spec_from_file_location("label_drain", pathlib.Path(__file__).resolve().parent / "label_drain.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        _LD.append(m)
    return _LD[0]


def _gate(runner, assignees):
    """-> the verdict word of the assignee clause (`label_drain.assignee_gate`), or None; no logins known: None."""
    if assignees is None:
        return None
    got = _label_drain().assignee_gate(list(assignees), runner)
    return got[0] if got else None


def promoted(rec):
    """-> the members left out of the chain (not a goal, waiting for its owner) that the person answered with the one
    option that arms them (`label_drain.ARM_ANSWER`). Also true of a `view`'s rows once the verdict has been turned."""
    ld, ans = _label_drain(), rec.get("answers") or {}
    return {r["number"] for r in rec["members"] if r["verdict"] in ld.GATED_KIND
            and ans.get("%s:%d" % (ld.GATED_KIND[r["verdict"]], r["number"])) == ld.ARM_ANSWER[ld.GATED_KIND[r["verdict"]]]}


def _inert(rec, row):
    """True when the member stays as it is unless the person activates it: parked and not released, or left out of the
    chain and not promoted. Such a member is not taken over (a member that stays put changes nobody's assignment)."""
    if row["verdict"] in ("parked", KEPT_PARKED):
        return row["number"] not in released(rec)
    return row["verdict"] in _label_drain().GATED_KIND and row["number"] not in promoted(rec)


def view(rec):
    """-> a copy of `rec` with the person's answers applied: a released member is armed (`arm` releases it before
    the run reads it), a kept-parked member has the verdict KEPT_PARKED, a taken-over member is no longer someone
    else's (it is assigned to the runner), a dismissed wait is no edge. Everything else is shared with `rec`."""
    out = dict(rec)
    rel, kept, took = released(rec), kept_parked(rec), taken_over(rec)
    promo = promoted(rec)
    gone = dismissed_waits(rec)
    rows = []
    for r in rec["members"]:
        n = r["number"]
        r = dict(r)
        if n in promo:                       # arm promotes it: it is armed, and somebody else's if it is somebody else's
            r["verdict"] = "armed"
            r.pop("base_verdict", None)
            gate = _gate(rec.get("runner"), r.get("assignees")) if rec.get("assignee_filter") else None
            if gate:
                r["verdict"], r["base_verdict"] = gate, "armed"
        if n in rel:
            r["verdict"] = "armed"
            r.pop("base_verdict", None)
            r["released"] = True
            gate = _gate(rec.get("runner"), r.get("assignees")) if rec.get("assignee_filter") else None
            if gate:                            # released, but somebody else's: the run stops there, as `_Judge` would say
                r["verdict"], r["base_verdict"] = gate, "armed"
        elif n in kept:
            r["verdict"] = KEPT_PARKED
        if n in took:
            if r["verdict"] == "assigned-elsewhere":
                r["verdict"] = r.get("base_verdict") or "armed"
            r["assignees"] = [rec["runner"]] if rec.get("runner") else []
            r["taken_over"] = True
        if gone:
            r["waits_on"] = [b for b in r.get("waits_on") or [] if (n, b) not in gone]
        rows.append(r)
    out["members"] = rows
    if gone:
        out["edges"] = [e for e in rec.get("edges") or [] if (e["to"], e["from"]) not in gone]
    return out


def answered_cap(rec):
    """-> the cap the person answered on the sheet (a positive whole number), else None. The run enforces it as
    `chain.max_goals`, the cap `chain_spend.admit` already checks before any claim, and the forecast models it."""
    text = str((rec.get("answers") or {}).get("cap:c%d" % rec["goal"]) or "")
    return int(text) if text.isascii() and text.isdigit() and int(text) > 0 else None


def effective_cap(rec, config=None):
    """-> the goal cap a run enforces when `resume --cap` gave none: the tighter of the answered cap and the
    configured `chain.max_goals`, else None (then `chain_spend.admit` applies its own default)."""
    block = (config or {}).get("chain") if isinstance((config or {}).get("chain"), dict) else {}
    conf = block.get("max_goals")
    conf = conf if isinstance(conf, int) and not isinstance(conf, bool) and conf > 0 else None
    got = [x for x in (answered_cap(rec), conf) if x]
    return min(got) if got else None


# --- what `arm` will write for the answers (the sheet lists it; `arm --dry-run` prints the same) --------------------

def release_line(n):
    return "#%d: release it through the unpark gesture (the Q&A block, one comment, then its labels change)" % n


def take_over_line(n, runner, old):
    return "#%d: assign %s and remove %s (one assignee)" % (n, runner or "you", ", ".join(old) or "the others")


def confirm_line(dependent, blocker):
    return "#%d: write `Blocked by #%d` in its body (the marker `deps.py mark` writes) and one note" % (dependent, blocker)


def dismiss_line(dependent, blocker):
    return "#%d: post one comment, the dismissal marker for #%d" % (dependent, blocker)


def write_lines(rec):
    """-> the lines the sheet's "Writes that follow approval" gets for the answers given so far: one per enacted
    answer. A kept-parked or waiting member has no line of its own (it is labelled and never armed, as before)."""
    by = {r["number"]: r for r in rec["members"]}
    out = [release_line(n) for n in sorted(released(rec))]
    out += [take_over_line(n, rec.get("runner"), [x for x in by[n].get("assignees") or [] if x != rec.get("runner")])
            for n in sorted(taken_over(rec))]
    out += [confirm_line(a, b) for a, b in sorted(confirmed_waits(rec))]
    out += [dismiss_line(a, b) for a, b in sorted(dismissed_waits(rec))]
    return out
