"""chain_waits.py -- waits that no edge reader sees, read for the chain and listed for the person (#1902).

The gate, the map and the label drain read a dependency only from an explicit form in the title or body
(`blocker_scan.EXPLICIT_TRIGGERS`). Real tickets say more: "Blocked by: #N added" in a comment,
"hold on #N before any paid run", "only once #A, #B and #C are closed", "blocked on #N and #M". A chain
planned from edges alone starts on work that cannot proceed (and forecasts a run that finishes everything).
Widening the shared vocabulary is the wrong fix (a phantom edge parks a goal for good and writes to an
unrelated issue, see `blocker_scan`), so Chain's plan READS these texts itself, for this chain only:

  unconfirmed wait  a directional form (below) in the BODY, or written in a COMMENT by the issue's assignee or
                    an OWNER/MEMBER/COLLABORATOR (the gate's own trust set, `sources.TRUSTED_ASSOCIATIONS`).
                    Chain reads it as an edge (tag `unconfirmed`, a wait is the safe direction: nothing
                    starts early) and the sheet lists every one in ONE block, with the two ways to settle
                    it: `deps.py mark` (writes the marker every reader trusts) or the existing dismissal
                    marker, which removes it at the next plan.
  weak mention      follow-up wording (advisory, as `blocker_scan` says): shown, counted, never an edge.

Only Chain's plan reads them as edges: the gate, the map and the label drain are untouched, so a goal outside
the chain is never held by one. `WithExtra` is how the live closure follows them; the map holds none of this
text, so a map plan that finds one falls back to the live read (`chain.py`).

Comments from anyone else are ignored and counted (the gate does the same, #709). Sigma's own comments
(a marker, or a fixed head below) are skipped; a park or fail prefix is stripped, as the gate does.

Loose forms (closed set; each is a SHAPE with a direction, none is a bare mention):
  list          `#M` / `#K` after an explicit or loose reference: "blocked on #N and #M", "#A, #B and #C"
  far           blocked by|on|until / depends on|upon, then up to 100 characters (clause punctuation allowed)
  only-once     "only once|when|after ... #N"          ("starts only once #N has landed")
  land-before   "#N ... must land|be merged ... before"  ("#N must land on main before this change")
  plug-into     "plug|hook into ... #N"                  ("these plug into #N's port")
  hold          "hold on|off|until|for ... #N"           ("hold on #N before any paid run")
  is-blocker    "#N is (now) a blocker"
  waiting-for   "waits|waiting|wait for ... #N"
  waits-on      "this|it ... waits on ... #N"             ("What this waits on: #N and #M"; the subject must be THIS
                issue: "E20 itself waits on #N" and "stage 1 also waits on #N" are another issue's waits)
  behind        "this|it ... sits|stands|queued behind ..." then EVERY reference to the end of that sentence (a list
                that names its members in words: "sits behind E11 (#N), which ...; E3 (#M), which ...")
  once-done     "once|as soon as ... #N (has|is) merged|landed|closed|done"  ("lands again as soon as #N has merged")
  lands-before  "#N [and ... #M] land(s) ... before this|the|its"          ("#N and #M land on main before this run")
  slice-chain   "S2a, S2b and S2c must precede|land before S2d" (#1940): this issue waits on each earlier code, read only
                when EVERY code of the chain resolves through a `<code> (#N)` map written in the same text; one
                unresolved code reads nothing from that sentence
  slice-on      "S4b on S4a (#N)": the number written after the second code
  waiting-on    "waiting on #N" (#1940), COMMENTS only (a trusted author, as above), any subject
  gates         "#N gates #M" (#1940), COMMENTS only; PROVISIONAL (owner to ratify): both #N and #M are read as waits
  reversed-waits-on  "the story #N waits on" at a clause end (#1940): #N, unless a reference follows in that clause
  follow-up     a follow-up form (advisory; never an unconfirmed wait)

Not read, by design (nothing to resolve; docs/chain.md names both): a slice code that has no issue number, and a
ruling named in words ("depends on rulings in the <name> item").

A trigger inside a parenthetical gloss that closes before the reference ("#A (what #B is blocked on), #C (...)")
reads nothing: the reference is outside the clause the trigger governs. A NEGATION cancels a forward form or one of the two new ones when `not`, `never`, `nor`, `n't`, `no longer` or
`without` stands within two words before the trigger ("this link does NOT wait for #N", "it no longer waits for
#N"): such a sentence says the goal does not wait, and reading it as a wait parked a goal that could run. A
negation in an earlier sentence, or further back than that, cancels nothing; the windows below stop at a sentence
end, and the test looks back no further than the sentence start.

Bounds: at most MAX_READS members are read (an issue GET plus the pages of its newest COMMENT_LIMIT comments,
so at most 3 requests each; none when the issue is known to have no comment); each comment is read to
COMMENT_CHARS and each body to BODY_CHARS; at most MAX_ITEMS findings are kept. Whatever was not read, or
cut, is named in `waits_read` and the plan is INCOMPLETE rather than silently thinner. Not measured: network
time; the call counts are arithmetic from the code and the counting fake.
"""
import importlib.util
import pathlib
import re

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


blocker_scan = _load("blocker_scan")
legacy = blocker_scan.legacy

MAX_READS = 100
COMMENT_LIMIT = 100
COMMENT_CHARS = 2000
BODY_CHARS = 65536          # GitHub's own limit on an issue body: a body is read to its end, never cut short
MAX_ITEMS = 300
MAX_MORE = 20
TRUSTED = ("OWNER", "MEMBER", "COLLABORATOR")
#: A comment that starts like this is Sigma's own fixed text, not a person's remark.
BOT_COMMENT_HEADS = ("Sigma is arming", "Auto-unparked by Sigma", "Auto-resumed by Sigma", "Reconciled by Sigma",
               "Backlog cross-check:", "Promoted to", "Returned to", "Parked via /sigma-promote",
               "Dependency marker added")
#: Sigma's park and fail prefixes contain "needs"; they are stripped, the reason after them is kept.
OFFBOARD_PREFIXES = ("Parked by Sigma — needs human review: ",
                     "Failed in the Sigma loop — needs a fix (not a decision): ")

_I = re.IGNORECASE
_SIGMA_MARKER = re.compile(legacy.MARKER_PREFIX_RE + ":", _I)     # a `sigma:` marker: Sigma wrote this comment
_WIN = r"[^\n#,;.!?]{0,40}?"
#: the plain window, or a LIST of short codes ("E11, E14 and E24 (#N)"): commas are allowed only between codes of at
#: most 12 characters, so a clause with its own punctuation ("field has landed on main (its S0, issue #N") is not crossed
_LIST_WIN = r"(?:" + _WIN + r"|(?:[ \t]+[\w.-]{1,12}(?:,|[ \t]+and\b|[ \t]*&)?){1,12}[ \t]*\(?)"
#: forward forms: a trigger, a short window, then the first reference (group 1).
_FORWARD = {
    "far": re.compile(r"\b(?:blocked[ \t]+(?:by|on|until)|depends[ \t]+(?:on|upon))\b[^\n#]{0,100}?#(\d+)\b", _I),
    "only-once": re.compile(r"\bonly[ \t]+(?:once|when|after)\b" + _LIST_WIN + r"#(\d+)\b", _I),
    "plug-into": re.compile(r"\b(?:plugs?|plugged|hooks?|hooked)[ \t]+(?:in)?to\b" + _WIN + r"#(\d+)\b", _I),
    "hold": re.compile(r"\bhold[ \t]+(?:on|off|until|for)\b" + _WIN + r"#(\d+)\b", _I),
    "waiting-for": re.compile(r"\bwait(?:s|ing)?[ \t]+for\b" + _WIN + r"#(\d+)\b", _I),
    "waits-on": re.compile(r"\b(?:waits?|is[ \t]+waiting|are[ \t]+waiting)[ \t]+on\b" + _WIN + r"#(\d+)\b", _I),
    "waiting-on": re.compile(r"\bwaiting[ \t]+on\b" + _WIN + r"#(\d+)\b", _I),
    "once-done": re.compile(r"(?<!only )(?<!only\t)\b(?:as[ \t]+soon[ \t]+as|once)\b" + _LIST_WIN + r"#(\d+)\b[^\n#;.!?]{0,25}?"
                            r"\b(?:has|have|is|are)[ \t]+(?:been[ \t]+)?(?:merged|landed|closed|done|finished|resolved)\b",
                            _I),
}
#: reference-first forms: the reference (group 1) comes before the words that give the direction.
_REF_FIRST = {
    "land-before": re.compile(r"#(\d+)\b[^\n#.!?;]{0,60}?\b(?:must|has[ \t]+to|needs[ \t]+to|should)[ \t]+"
                              r"(?:first[ \t]+)?(?:land|be[ \t]+(?:merged|closed|done|landed|resolved|finished))"
                              r"\b[^\n#.!?;]{0,30}?\bbefore\b", _I),
    "is-blocker": re.compile(r"#(\d+)\b[^\n#.!?;]{0,30}?\b(?:is|are)[ \t]+(?:(?:now|also|still)[ \t]+)*"
                             r"(?:(?:a|the)[ \t]+)?(?:hard[ \t]+)?blockers?\b", _I),
}
#: a landing stated without a modal: "#N [and lever 1 (#M)] land(s) ... before this ...". Group 1 is the whole run of
#: references (up to six) that stand before the verb; group `t` is the verb, where a negation is looked for.
_SPAN = {
    "lands-before": re.compile(r"((?:#\d+\b[^\n#.!?;]{0,40}?){1,6})(?P<t>\blands?\b)[^\n#.!?;]{0,30}?"
                               r"\bbefore\b\W{0,4}(?:this|the|its|it|any)\b", _I),
}
#: forms that are a wait only when the sentence's subject is THIS issue ("What this waits on: #N", "it waits on #N"),
#: because "E20 itself waits on #N" and "stage 1 also waits on #N" say what ANOTHER issue waits on
_OWN_SUBJECT = frozenset({"waits-on", "behind"})
_OWN = re.compile(r"\b(?:this|it)\b(?:[ \t]+[\w'*]+){0,3}[ \t]*$", _I)
#: a negation shortly before a trigger: the goal does not wait, so the sentence is not a wait.
_NEGATED = re.compile(r"(?:\b(?:not|never|nor|without)\b|n't\b|\bno[ \t]+longer\b)\W*(?:\w+\W+){0,2}$", _I)
_SENTENCE_END = re.compile(r"[.!?;\n]")
#: "sits behind <a sentence that lists what it sits behind>": group `s` is the rest of that sentence (300 characters
#: at most, to the first full stop or line end); every reference in it is read. Group `t` is the trigger.
_SENTENCE_FORMS = {
    "behind": re.compile(r"(?P<t>\b(?:sits?|sat|stands?|stood|queued?)[ \t]+(?:also[ \t]+)?behind\b)(?P<s>(?:[^\n.]|\.(?![ \t\n]|$)){0,300})", _I),
}
#: the tail of a list: ", #M", ", and #M", " and #M", " & #M", each with an optional short code before the reference
#: (", and E22 #M") -- tight, one reference per step.
_MORE = re.compile(r"[ \t]*(?:,[ \t]*(?:and[ \t]+)?|and[ \t]+|&[ \t]*)(?:[A-Za-z][\w.-]{0,7}[ \t]+)?#(\d+)\b", _I)
#: forms read only from a trusted COMMENT, never from a body (#1940)
_COMMENT_ONLY = frozenset({"waiting-on", "gates"})
#: slice codes (#1940): a letter, digits, up to two lower-case letters; case-sensitive so prose words are not codes
_CODE = r"[A-Z]\d+[a-z]{0,2}"
_CODE_MAP = re.compile(r"\b(" + _CODE + r")[ \t]*\(#(\d+)\)")
_SLICE_CHAIN = re.compile(r"\b(" + _CODE + r"(?:(?:[ \t]*,[ \t]*(?:and[ \t]+)?|[ \t]+and[ \t]+|[ \t]*&[ \t]*)" + _CODE + r")*)"
                          r"[ \t]+(?:must[ \t]+|need[ \t]+not[ \t]+)?(?P<t>precedes?|(?:lands?|be[ \t]+merged)[ \t]+before)"
                          r"[ \t]+(" + _CODE + r")\b")
_SLICE_ON = re.compile(r"\b" + _CODE + r"[ \t]+on[ \t]+" + _CODE + r"[ \t]*\(#(\d+)\)")
#: "#N gates #M": both ends (group 1, 2); group `t` is the verb, where a negation is looked for
_GATES = re.compile(r"#(\d+)\b[^\n#.!?;]{0,20}?(?P<t>\bgates?\b)[ \t]+#(\d+)\b", _I)
#: "the story #N waits on" ending its clause; the optional words between are for "does not wait on"
_REVERSED = re.compile(r"\bthe[ \t]+[\w-]{2,20}[ \t]+#(\d+)\b[ \t]+(?:[\w']+[ \t]+){0,2}?(?P<t>wait(?:s|ing)?[ \t]+on)\b"
                       r"(?=[ \t]*(?:[\n.,;:!?)]|$))", _I)
#: a reference still ahead in the same clause: "the story #N waits on, per #M, ..." is about #M
_REF_AHEAD = re.compile(r"[^\n.!?;]{0,60}?#\d", _I)
FORMS = ("explicit", "list", "follow-up") + tuple(_FORWARD) + tuple(_REF_FIRST) + tuple(_SPAN) + tuple(_SENTENCE_FORMS) + \
    ("slice-chain", "slice-on", "gates", "reversed-waits-on")


def _foreign(text, ref_start):
    """`owner/repo#N`: the character before `#` belongs to a name (the rule `blocker_scan.is_cross_repo` has)."""
    i = ref_start - 2
    return i >= 0 and (text[i].isalnum() or text[i] in "_/-.")


def _own_subject(text, pos):
    """True when `this` or `it` stands within three words before `pos`, in the same sentence."""
    head = text[max(0, pos - 60):pos]
    ends = list(_SENTENCE_END.finditer(head))
    if ends:
        head = head[ends[-1].end():]
    return bool(_OWN.search(head))


def _negated(text, pos):
    """True when a negation stands within two words before `pos`, in the same sentence."""
    head = text[max(0, pos - 60):pos]
    ends = list(_SENTENCE_END.finditer(head))
    if ends:
        head = head[ends[-1].end():]
    return bool(_NEGATED.search(head))


def _leaves_paren(segment):
    """True when `segment` (the text from a trigger to its reference) closes a parenthesis it did not open: the
    trigger sat inside a gloss ("#A (what #B is blocked on), #C (...)") and the reference is outside it."""
    depth = 0
    for ch in segment:
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                return True
            depth -= 1
    return False


def _tail(text, pos):
    """Every reference in the list that continues at `pos`, at most MAX_MORE."""
    out = []
    while len(out) < MAX_MORE:
        m = _MORE.match(text, pos)
        if not m:
            break
        out.append(m.group(1))
        pos = m.end()
    return out


def find(text, explicit=True, comment=False):
    """Pure. -> ordered `[(ref, form)]`, ref-and-form deduplicated, for every reading this module has over
    `text`. `explicit` False leaves out the explicit forms (a body already holds those as edges). `comment` True
    says `text` is a trusted comment: only then are the comment-only forms (`waiting-on`, `gates`) read."""
    text = blocker_scan.strip_unpark_qa(text or "")
    found, plain = [], set()
    for m in blocker_scan._EXPLICIT_BLOCK_RE.finditer(text):
        if _foreign(text, m.start(2)):
            continue
        if not blocker_scan.is_explicit(text, m, 0):             # a follow-up form: advisory only
            found.append((m.group(2), "follow-up"))
            continue
        if _negated(text, m.start()):                            # "is not blocked by #N any more"
            continue
        plain.add(m.group(2))
        if explicit:
            found.append((m.group(2), "explicit"))
        found += [(r, "list") for r in _tail(text, m.end())]
    for form, rx in _FORWARD.items():
        if form in _COMMENT_ONLY and not comment:
            continue
        for m in rx.finditer(text):
            if _foreign(text, m.start(1)) or (form == "far" and m.group(1) in plain):
                continue                                  # `far` only adds what the explicit window missed
            if _leaves_paren(text[m.start():m.start(1)]):
                continue                                  # the trigger was inside a gloss that closed before the reference
            if _negated(text, m.start()) or (form in _OWN_SUBJECT and not _own_subject(text, m.start())):
                continue                                  # "does not wait for #N" says the opposite; or another issue's wait
            found.append((m.group(1), form))
            found += [(r, "list") for r in _tail(text, m.end())]
    for form, rx in _REF_FIRST.items():
        for m in rx.finditer(text):
            if not _foreign(text, m.start(1)):
                found.append((m.group(1), form))
    for form, rx in _SENTENCE_FORMS.items():
        for m in rx.finditer(text):
            if _negated(text, m.start("t")) or (form in _OWN_SUBJECT and not _own_subject(text, m.start("t"))):
                continue
            found += [(r.group(1), form) for r in re.finditer(r"#(\d+)\b", m.group("s"))
                      if not _foreign(text, m.start("s") + r.start(1))][:MAX_MORE + 1]
    modal = {r for r, f in found if f == "land-before"}           # "must land ... before" is that form already
    for form, rx in _SPAN.items():
        for m in rx.finditer(text):
            if _negated(text, m.start("t")):
                continue
            found += [(r.group(1), form) for r in re.finditer(r"#(\d+)\b", m.group(1))
                      if r.group(1) not in modal and not _foreign(text, m.start(1) + r.start(1))]
    found += _find_1940(text, comment)
    out, seen = [], set()
    for ref, form in found:
        if (ref, form) not in seen:
            seen.add((ref, form))
            out.append((ref, form))
    return out


def _find_1940(text, comment):
    """The #1940 readings that are not a single trigger and a reference: slice codes, "gates", the reversed wait."""
    found = []
    codes = {c: n for c, n in _CODE_MAP.findall(text)}
    for m in _SLICE_CHAIN.finditer(text):
        if _negated(text, m.start("t")):
            continue
        members = re.findall(_CODE, m.group(1))
        if codes and all(c in codes for c in members):                    # EVERY code resolves, or nothing is read
            found += [(codes[c], "slice-chain") for c in members]
    for m in _SLICE_ON.finditer(text):
        if not _foreign(text, m.start(1)):
            found.append((m.group(1), "slice-on"))
    if comment:
        for m in _GATES.finditer(text):
            if _negated(text, m.start("t")) or _foreign(text, m.start(1)) or _foreign(text, m.start(3)):
                continue
            found += [(m.group(1), "gates"), (m.group(3), "gates")]
    for m in _REVERSED.finditer(text):
        if _foreign(text, m.start(1)) or _negated(text, m.start("t")) or _REF_AHEAD.match(text, m.end()):
            continue
        found.append((m.group(1), "reversed-waits-on"))
    return found


def loose_refs(text):
    """Pure. The references `text` mentions in a loose or advisory way (everything `find` reads except the
    explicit forms): what `deps.py report` shows as `weak`."""
    return {ref for ref, form in find(text, explicit=False)}


def _dismissed(backlog_check, bodies):
    """References a trusted person dismissed as `blocked-by` (the marker `backlog_check.py dismiss-text` prints)."""
    return {ref for b in bodies for kind, ref in backlog_check._DISMISS_RE.findall(b or "")
            if kind == "blocked-by"}


def _clean(body):
    """-> the comment text to scan, or None when it is Sigma's own fixed text."""
    body = body or ""
    if _SIGMA_MARKER.search(body):
        return None
    head = body.lstrip()
    for p in OFFBOARD_PREFIXES:
        if head.startswith(p):
            return head[len(p):COMMENT_CHARS + len(p)]
    if any(head.startswith(h) for h in BOT_COMMENT_HEADS):
        return None
    return head[:COMMENT_CHARS]


def _role(comment, assignees):
    """-> "assignee" | "owner" | "member" | "collaborator" | None for a comment nobody should trust."""
    login = str(((comment.get("user") or {}).get("login")) or "").lower()
    if login and login in assignees:
        return "assignee"
    assoc = comment.get("author_association")
    return assoc.lower() if assoc in TRUSTED else None


def _assignees(issue):
    names = [a.get("login") for a in (issue.get("assignees") or []) if isinstance(a, dict)]
    names.append((issue.get("assignee") or {}).get("login") if isinstance(issue.get("assignee"), dict) else None)
    return {str(n).lower() for n in names if n}


def _read(ro, repo, number, issue, cache=None, disk=None, stamp=None):
    """-> (issue with its body and assignees, newest-first comment dicts, maybe_older). A known-empty issue costs
    no request; otherwise the issue and the pages of its newest COMMENT_LIMIT comments (at most 3 requests).
    `cache` (number -> that answer) makes a repeat read free: the live closure collects once per round.
    `disk` (#1936: `(chain_read_cache, store, now)` or None) answers from the on-disk cache when the issue's current
    `stamp` (its `updated_at`) equals the cached one; None leaves the read exactly as it was."""
    if cache is not None and number in cache:
        return cache[number]
    if issue is not None and isinstance(issue.get("comments"), int) and issue["comments"] == 0:
        got = issue, [], False
    else:
        got = disk[0].get(disk[1], number, stamp) if disk is not None else None
        if got is None:
            gh_api = _load("gh_api")
            full = gh_api.view_issue(ro, number, repo or None, comments=COMMENT_LIMIT)
            comments = [c for c in (full.get("comments") or []) if isinstance(c, dict)]
            got = full, list(reversed(comments)), len(comments) >= COMMENT_LIMIT
            if disk is not None:
                disk[0].put(disk[1], number, stamp, got, disk[2])
    if cache is not None:
        cache[number] = got
    return got


def collect(ro, repo, numbers, issues=None, have=None, state_of=None, cache=None, disk=None, stamps=None):
    """Read the bodies and newest comments of `numbers` (at most MAX_READS) and report what they wait on that no
    edge says. `issues` maps number -> the issue already read (skips a request for a known-empty one);
    `have` maps number -> blockers already known as edges (a ledger edge the body does not carry);
    `state_of(ref)` -> "open" | "closed" | "unknown". `disk` and `stamps` (number -> `updated_at`, else the issue's
    own) are the optional on-disk cache of #1936. -> {"unconfirmed", "weak", "read"}."""
    issues, have, stamps = issues or {}, have or {}, stamps or {}
    state_of = state_of or (lambda ref: "unknown")
    backlog_check = _load("backlog_check")
    members = {int(n) for n in numbers}
    items, not_read, older, ignored, dropped = {}, [], [], 0, 0
    todo = sorted(members)
    skipped = todo[MAX_READS:]
    for n in todo[:MAX_READS]:
        try:
            stamp = stamps.get(n) or (issues.get(n) or {}).get("updated_at")
            issue, comments, maybe_older = _read(ro, repo, n, issues.get(n), cache, disk, stamp)
        except Exception:                      # noqa: BLE001 - say so rather than guess
            not_read.append(n)
            continue
        if maybe_older:
            older.append(n)
        assignees = _assignees(issue)
        body = (issue.get("body") or "")[:BODY_CHARS]
        known = {int(i["ref"]) for i in blocker_scan.extract_refs(body, self_ref=n)}
        known |= {int(b) for b in have.get(n, ())}
        trusted, texts = [], []
        for c in comments:
            role = _role(c, assignees)
            if role is None:
                ignored += 1
                continue
            trusted.append(str(c.get("body") or ""))
            text = _clean(c.get("body"))
            if text is not None:
                texts.append((role, text))
        gone = {int(r) for r in _dismissed(backlog_check, trusted)}
        sources = [("body", None, body)] + [("comment", role, t) for role, t in texts]
        for where, role, text in sources:
            for ref, form in find(text, explicit=(where == "comment"), comment=(where == "comment")):
                r = int(ref)
                if r == n or r in known or r in gone:
                    continue
                kind = "weak" if form == "follow-up" else "unconfirmed"
                item = {"goal": n, "ref": r, "kind": kind, "form": form, "where": where, "by": role,
                        "state": state_of(r), "in_chain": r in members}
                cur = items.get((n, r))
                if cur is None or (cur["kind"] == "weak" and kind == "unconfirmed"):
                    if cur is None and len(items) >= MAX_ITEMS:
                        dropped += 1
                        continue
                    items[(n, r)] = item
    rows = [items[k] for k in sorted(items)]
    return {"unconfirmed": [i for i in rows if i["kind"] == "unconfirmed"],
            "weak": [i for i in rows if i["kind"] == "weak"],
            "read": {"members_read": len(todo[:MAX_READS]) - len(not_read), "not_read": sorted(not_read + skipped),
                     "maybe_older_comments": older, "untrusted_ignored": ignored, "dropped": dropped}}


def attach(out, ro, repo, numbers, issues=None, have=None, state_of=None, cache=None, disk=None, stamps=None):
    """Put the findings on the plan dict `out` and make the plan INCOMPLETE when anything was not read or cut."""
    res = collect(ro, repo, numbers, issues, have, state_of, cache, disk, stamps)
    out["unconfirmed_waits"], out["weak_waits"], out["waits_read"] = res["unconfirmed"], res["weak"], res["read"]
    r = res["read"]
    out["complete"] = bool(out.get("complete")) and not r["not_read"] and not r["dropped"]


class WithExtra:
    """`blocker_scan` plus the references Chain read as unconfirmed waits: `extract_refs(body, self_ref=n)` also
    returns `extra[n]`. `label_drain.closure` calls nothing else on its scanner except the cross-repository helper,
    which is delegated untouched. A stand-in for the scan module, used only inside Chain's plan."""

    def __init__(self, base, extra):
        self._base, self._extra = base, extra

    def __getattr__(self, name):
        return getattr(self._base, name)

    def extract_refs(self, text, self_ref=None):
        items = self._base.extract_refs(text, self_ref=self_ref)
        have = {str(i["ref"]) for i in items}
        return items + [{"phrase": "unconfirmed wait", "ref": str(r)} for r in self._extra.get(self_ref, ())
                        if str(r) not in have]


# --- text for the plan and the sheet ---------------------------------------------------------------------

def _ids(nums):
    return ", ".join("#%d" % n for n in nums)


def _how(i):
    where = "in a comment by the %s" % i["by"] if i["where"] == "comment" else "in the body"
    return "%s (%s; %s; %s)" % ("#%d" % i["ref"], i["form"], where,
                                "%s, %s" % (i["state"], "in the chain" if i["in_chain"] else "not in the chain"))


def plan_lines(p):
    """The plan printout's lines for the three fields; empty when a record predates them."""
    un, weak, rd = p.get("unconfirmed_waits"), p.get("weak_waits"), p.get("waits_read")
    if un is None or rd is None:
        return []
    out = ["unconfirmed waits (read as edges for this chain, not confirmed): %d" % len(un)]
    out += ["  #%d waits on %s" % (i["goal"], _how(i)) for i in un]
    out.append("weak mentions (follow-up wording; never an edge): %d" % len(weak or []))
    out += ["  #%d ~ %s" % (i["goal"], _how(i)) for i in weak or []]
    out += _read_lines(rd)
    return out


def _read_lines(rd):
    out = []
    if rd.get("not_read"):
        out.append("comments NOT read for %s; the plan is incomplete" % _ids(rd["not_read"]))
    if rd.get("dropped"):
        out.append("%d finding(s) over the cap of %d were dropped; the plan is incomplete" % (rd["dropped"], MAX_ITEMS))
    if rd.get("maybe_older_comments"):
        out.append("only the newest %d comments were read for %s" % (COMMENT_LIMIT, _ids(rd["maybe_older_comments"])))
    if rd.get("untrusted_ignored"):
        out.append("%d comment(s) from people who are neither assignee nor owner/member/collaborator were ignored"
                   % rd["untrusted_ignored"])
    return out


def sheet_block(rec):
    """The ONE block of the sheet: every unconfirmed wait together, then the weak mentions, then how to settle
    them, led by a blank line. Empty when the record has none of it. Pure: reads only the record."""
    un, weak, rd = rec.get("unconfirmed_waits") or [], rec.get("weak_waits") or [], rec.get("waits_read") or {}
    notes = _read_lines(rd)
    if not un and not weak and not notes:
        return []
    out = ["Unconfirmed waits (%d): written only in comments or loose wording; read as edges in this plan:" % len(un)]
    out += ["  #%d waits on %s" % (i["goal"], _how(i)) for i in un] or ["  none"]
    if un:
        out += ["  A wait on an open issue in this chain is a question below (wait:<goal>-<ref>): confirm it or dismiss it; "
                "`chain.py arm` writes the marker or the dismissal. A wait on a closed issue is only shown.",
                "  Confirm each real one: python3 skills/sigma-loop/scripts/deps.py mark <sdlc_dir> <goal> <ref> <reason...>",
                "  Dismiss a wrong one: python3 skills/sigma-loop/scripts/backlog_check.py dismiss-text blocked-by <ref>,",
                "  then post that text with python3 skills/sigma-loop/scripts/loop.py note <sdlc_dir> <goal> \"<text>\"",
                "  Then run chain.py plan again; a confirmed wait is a body edge, a dismissed one is dropped."]
    if weak:
        out.append("Weak mentions (never block, not asked): " + "; ".join("#%d ~ %s" % (i["goal"], _how(i)) for i in weak))
    out += ["  " + n for n in notes]
    return [""] + out
