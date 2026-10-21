"""Read every issue reference in a goal's ticket, and turn a prose dependency into a marker (#1885).

The pick gate and the dependency map read an edge only from an explicit form
(`blocker_scan.EXPLICIT_TRIGGERS`: `**Blocked by:** #N`, `depends on #N`, `follow-up to #N`, ...), and
the map reads the BODY only. A ticket can still say what it waits for in words no pattern should be
trusted with ("this can only start once the zone work in #12 is merged"), or say it in a comment the
map never reads. Widening the pattern is the wrong fix: the residue is documented in `blocker_scan`
and a phantom edge writes to an unrelated issue. So this module hands the WHOLE ticket to the agent:

  report  every `#N` in the title, the body and every comment, each classified by what the machinery
          already does with it, with the sentence it sits in. The agent reads each one and decides.
  mark    writes the one form everything reads, `**Blocked by:** #N`, into the goal's BODY through
          `source.append_to_body` (which refuses a cycle before writing and inserts the edge into the
          stored map), and leaves a note saying why. Idempotent: an edge the body already carries is
          `ALREADY`, and nothing is written. The referenced number must be an OPEN issue or pull request
          (one read); a closed, missing or unreadable one is `REFUSED`, so a typo never becomes an edge.
          A checkpoint (#1904, label `sdlc:checkpoint`) is an open issue like any other, so a wait on it is
          written the same way; the answer says the issue is a checkpoint.

The judgment is the agent's, and it costs one read of the ticket per goal (one issue read plus its
comment pages through `gh_api`), paid once at pick time. What makes it safe is that the agent's
decision lands as the canonical marker, which every reader treats the same way, instead of as a
new inference rule inside the scanners.

`read_as` values, per referenced `#N` (never the goal itself, never `owner/repo#N`):
  edge          an explicit form in the title or body: the gate and the map already hold it
  comment-edge  an explicit form only in a comment: the map never reads it, and the gate reads it only
                in the newest 20 comments' first 500 characters, and never a follow-up form there
  weak          a weak word (`needs`, `after`, `requires`, `waiting on`) before it, or a loose phrasing
                `chain_waits` knows ("only once #N", "hold on #N", "#N is a blocker", ...): advisory only
  mention       any other reference: read by nothing

GitHub mode only: a local-goals source prints `UNSUPPORTED` and exits 2. Snippets go to stdout for
the agent and are never stored; they pass through `scrub.scrub` first.

Usage:
  deps.py report <sdlc_dir> <goal>
  deps.py report <sdlc_dir> --label <label> [--label <label> ...] [--sheet]   every open member of the run label(s)
  deps.py report <sdlc_dir> --chain <N> [--sheet]   goal N and every open prerequisite the chain planner finds
  deps.py mark <sdlc_dir> <goal> <ref> <reason...>
  deps.py unmark <sdlc_dir> <goal> <ref> <reason...>

Batch (#1944): `--label` / `--chain` print one JSON line per member (the exact single-goal object, ordered by
issue number) from ONE process. Members come from what already lists them: the label drain's scope check and
REST listing (`sources.scope_to_label` + `fetch_issues_rest`), and `chain._resolve` (the planner's own closure,
which writes no record). The listing carries bodies but not comments, so each member is still read with the
single form's `_read_issue` inside this process: no subprocess per member, but NOT one read in total.
Reads: 1 listing (label) or the planner's reads (chain), plus per member one issue read and its comment pages.
MEASURED: process count only (one), by fake-source tests; no timing and no speedup is claimed. An unreadable
member prints `{"error": ..., "goal": N}` and the exit code is 2 at the end; none is skipped silently.

Advisory flags (#1990, #1995), never a write and never a reason for `mark` to refuse. The other issue's text
decides the DIRECTION: only text in which that issue says IT waits on this goal counts. `reverse` on an edge
or comment-edge ref says so (a two-way wait, a deadlock no marker line can show); on a `weak` ref the same
text gives `likely-dependent` instead (the goal's weak read is probably the wrong way round: the other issue
is a dependent, and the sheet says so in words), never `reverse`. A CLOSED ref is not reverse-read; the report
counts them in `reverse_skipped_closed` and the sheet prints the count. The reverse read alone also counts
`prerequisite(s)` phrasings ("#N stays a prerequisite", "Prerequisite: #N") as weak; the edge parser and
blocker_scan's tables are unchanged. `quoted` on an `edge` says the body sentence names a slice code, `#M` or
the word slice as carrying the marker (an epic describing its children, not its own wait). Cost: one reverse read per non-mention ref,
at most `REVERSE_READ_MAX` (10) per goal, each one read plus ceil(comments/100) comment pages (a closed ref
is learned from that same read and adds nothing more); at 10x refs the cost stays at that number and the rest
are listed in `reverse_unchecked` (`reverse_reads` counts the reads made). A batch (`--label`/`--chain`)
multiplies by its members: a 63-goal band is at most 63 + 630 = 693 issue reads plus comment pages, which
is a ceiling, not a measurement. Measured on fixtures only.

`--label` may repeat: the union of the open members, each once, ordered by number. `--sheet` (#1991) prints a markdown
review sheet instead of JSON lines: per goal its non-`edge` refs (comment-edge, weak, mention) with snippets, and a tally
of the edges; a review aid, only `mark` writes. Without `--sheet` the JSON lines are byte-identical to before.

Scale: `report` is one issue read plus ceil(comments/100) comment pages, whatever the board size;
`mark` adds one goal read, one read of the referenced issue, one body write and one comment.
"""
import importlib.util
import json
import pathlib
import re
import sys

_HERE = pathlib.Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


blocker_scan = _load("blocker_scan")
_scrub = _load("scrub")
chain_waits = _load("chain_waits")
_hsg = _load("hard_stop_guard")

_REF_RE = re.compile(r"#(\d+)\b")
SNIPPET_CHARS = 160
SNIPPETS_PER_REF = 2
REVERSE_READ_MAX = 10
_QUOTE_RE = re.compile(r"(?i)(?:\bS\d+\b|#\d+|\bslices?\b)[^.\n!?]*\bcarr(?:y|ies)\b")
_RANK = {"edge": 0, "comment-edge": 1, "weak": 2, "mention": 3}
USAGE = ("usage: deps.py report <sdlc_dir> <goal> | deps.py report <sdlc_dir> --label <label> [--label <label>...] [--sheet] | "
         "deps.py report <sdlc_dir> --chain <N> | deps.py mark <sdlc_dir> <goal> <ref> <reason...> | "
         "deps.py unmark <sdlc_dir> <goal> <ref> <reason...>")


def _snippet(text, start, end):
    a = max(0, start - SNIPPET_CHARS // 2)
    b = min(len(text), end + SNIPPET_CHARS // 2)
    return _scrub.scrub(" ".join(text[a:b].split()))


def _is_foreign(text, start):
    """`owner/repo#N` or `repo#N`: the character before `#` belongs to a name (same rule as
    `blocker_scan.is_cross_repo`, which takes a trigger match rather than a bare reference)."""
    i = start - 1
    return i >= 0 and (text[i].isalnum() or text[i] in "_/-.")


def _quoted(text, start):
    """True when the sentence before the blocking phrase at `start` names a slice code, `#M` or the word
    slice and says it carries/carry something: the text describes ANOTHER issue's marker. A phrase that
    begins its line (the goal's own marker line) is never quoted."""
    line = text.rfind("\n", 0, start) + 1
    if not text[line:start].strip(" \t>*-_`"):
        return False
    cut = max(text.rfind(c, 0, start) for c in ".!?\n") + 1
    return bool(_QUOTE_RE.search(text[cut:start]))


# #1995: phrases only the REVERSE read adds to `weak` ("#N stays a stage-2 prerequisite", "Prerequisite: #N").
# The forward parse (`analyse` with reverse=False) and blocker_scan's tables never see them.
_PREREQ_AFTER_RE = re.compile(r"(?i)\bprerequisites?\b\s*[:\-]?\s*#(\d+)\b")
_PREREQ_BEFORE_RE = re.compile(r"(?i)#(\d+)\b[^.\n#]{0,30}?\bprerequisites?\b(?!\s+(?:of|for)\s+(?:#|issue\b))")


def _prereq_refs(text):
    return ({m.group(1) for m in _PREREQ_AFTER_RE.finditer(text)}
            | {m.group(1) for m in _PREREQ_BEFORE_RE.finditer(text)})


def analyse(goal, title, body, comments, reverse=False):
    """Pure. `comments` is a list of comment body strings. -> list of
    `{"ref", "read_as", "where", "snippets"}`, ordered by `read_as` (edge first), then ref. `reverse=True`
    (#1995, the reverse read only) also counts `prerequisite` phrasings as weak."""
    goal = str(goal).lstrip("#")
    texts = [("body", blocker_scan.strip_unpark_qa((title or "") + "\n" + (body or "")))]
    texts += [("comment", blocker_scan.strip_unpark_qa(c or "")) for c in comments or []]
    refs, quote = {}, {}
    for where, text in texts:
        explicit = set()
        for m in blocker_scan._EXPLICIT_BLOCK_RE.finditer(text):
            if blocker_scan.is_cross_repo(text, m):
                continue
            explicit.add(m.group(2))
            if where == "body":
                q = quote.setdefault(m.group(2), [])
                q.append(_quoted(text, m.start()))
        weak = ({m.group(2) for m in blocker_scan._BLOCK_RE.finditer(text)
                 if not blocker_scan.is_cross_repo(text, m)} | chain_waits.loose_refs(text)
                | (_prereq_refs(text) if reverse else set())) - explicit   # #1902
        for m in _REF_RE.finditer(text):
            ref = m.group(1)
            if ref == goal or _is_foreign(text, m.start()):
                continue
            kind = ("edge" if where == "body" else "comment-edge") if ref in explicit else (
                "weak" if ref in weak else "mention")
            rec = refs.setdefault(ref, {"ref": ref, "read_as": kind, "where": [], "snippets": []})
            if _RANK[kind] < _RANK[rec["read_as"]]:
                rec["read_as"] = kind
            if where not in rec["where"]:
                rec["where"].append(where)
            if len(rec["snippets"]) < SNIPPETS_PER_REF:
                rec["snippets"].append(_snippet(text, m.start(), m.end()))
    for ref, rec in refs.items():
        if rec["read_as"] == "edge":
            rec["quoted"] = bool(quote.get(ref)) and all(quote[ref])
    return sorted(refs.values(), key=lambda r: (_RANK[r["read_as"]], int(r["ref"])))


def _read(source, goal):
    return _read_full(source, goal)[:3]


def _read_full(source, goal):
    data = source._read_issue(goal, ["title", "body", "state", "comments"], comment_limit=None)
    comments = [c.get("body") or "" for c in (data.get("comments") or []) if isinstance(c, dict)]
    return data.get("title") or "", data.get("body") or "", comments, str(data.get("state") or "")


def _names_goal(refs, goal):
    return any(r["ref"] == goal and r["read_as"] != "mention" for r in refs)


def report(source, goal, max_reverse=REVERSE_READ_MAX):
    """Advisory (#1990, #1995): each ref read as edge/comment-edge/weak is read once more, up to `max_reverse`
    refs (rank order, then number). The reverse text decides the DIRECTION: only a ref whose own text says
    it waits on the goal counts. On an edge/comment-edge that is `reverse: true`; on a `weak` ref it is
    `likely-dependent: true` (the goal's weak read is probably the wrong way round), never `reverse`. A
    closed ref is not reverse-read and is counted in `reverse_skipped_closed`. The eligible refs beyond the
    bound are listed in `reverse_unchecked`; an unreadable one is `reverse: null`."""
    goal = str(goal).lstrip("#")
    title, body, comments = _read(source, goal)
    refs = analyse(goal, title, body, comments)
    counts = {k: sum(1 for r in refs if r["read_as"] == k) for k in _RANK}
    eligible = [r for r in refs if r["read_as"] != "mention"]
    closed = 0
    for r in eligible[:max_reverse]:
        try:
            t, b, c, state = _read_full(source, r["ref"])
            if state.upper() == "CLOSED":
                closed += 1
                continue
            waits = _names_goal(analyse(r["ref"], t, b, c, reverse=True), goal)
            if r["read_as"] == "weak" and waits:
                r["likely-dependent"] = True
            else:
                r["reverse"] = waits
        except Exception:
            r["reverse"] = None
    return {"goal": goal, "comments_read": len(comments), "counts": counts,
            "reverse_reads": min(len(eligible), max_reverse), "reverse_skipped_closed": closed,
            "reverse_unchecked": [r["ref"] for r in eligible[max_reverse:]], "refs": refs}


def _plain(reason):
    """A reason with every `#` removed, so the note can never itself mint an edge at check time."""
    return " ".join(str(reason or "").replace("#", "issue ").split())[:300]


def mark(source, goal, ref, reason):
    """-> ("MARKED"|"ALREADY"|"REFUSED", detail). Writes nothing unless the result is MARKED."""
    goal, ref = str(goal).lstrip("#"), str(ref).lstrip("#")
    if not ref.isdigit() or ref == goal:
        return "REFUSED", "the reference must be another issue's number"
    _title, body, _comments = _read(source, goal)
    if any(r["ref"] == ref for r in blocker_scan.extract_refs(body, self_ref=goal)):
        return "ALREADY", "the body already carries an explicit edge to issue %s" % ref
    try:
        data = source._read_issue(ref, ["state", "labels"])
        state = str(data.get("state") or "").upper()
        names = {(l.get("name") if isinstance(l, dict) else str(l)) for l in (data.get("labels") or [])}
    except Exception as exc:                       # noqa: BLE001 - unreadable is a refusal, never a guess
        return "REFUSED", "issue %s could not be read (%s)" % (ref, type(exc).__name__)
    if state != "OPEN":
        return "REFUSED", "issue %s is not open (state %s)" % (ref, state or "unknown")
    if not _hsg.is_sigma_issue(source, goal):       # #1984: read-only; a foreign issue gets no half a gesture
        return "REFUSED", ("issue %s was not created by Sigma and carries no Sigma label, so nothing was written: "
                           "write the `Blocked by:` line by hand, as the dependency-writing rule allows, or give "
                           "the issue a Sigma label first" % goal)
    try:
        source.append_to_body(goal, "**Blocked by:** #%s" % ref)
    except ValueError as exc:                      # dag_insert.CycleRefused is a ValueError
        return "REFUSED", str(exc)
    note = "Dependency marker added to the body for issue %s, read from the ticket's prose." % ref
    if _plain(reason):
        note += " Reason: " + _plain(reason)
    try:
        source.note(goal, note)
    except Exception as exc:                       # noqa: BLE001 - the marker is the record; the note is narration
        return "MARKED", "marker written; the explanatory note failed (%s)" % type(exc).__name__
    checkpoint = getattr(source, "checkpoint_label", None)
    if checkpoint and checkpoint in names:          # #1904: a wait on a checkpoint is an ordinary edge
        return "MARKED", ("issue %s is a checkpoint and now holds this goal until its unit lands or a person "
                          "closes it" % ref)
    return "MARKED", "issue %s now blocks this goal" % ref


def unmark(source, goal, ref, reason):
    """-> ("UNMARKED"|"ABSENT"|"REFUSED", detail). Writes nothing unless the result is UNMARKED."""
    goal, ref = str(goal).lstrip("#"), str(ref).lstrip("#")
    if not ref.isdigit() or ref == goal:
        return "REFUSED", "the reference must be another issue's number"
    if not _hsg.is_sigma_issue(source, goal):       # #1984: read-only; a foreign issue gets no half a gesture
        return "REFUSED", ("issue %s was not created by Sigma and carries no Sigma label, so nothing was written: "
                           "edit the `Blocked by:` line by hand, or give the issue a Sigma label first" % goal)
    _title, body, _comments = _read(source, goal)
    marker = "**Blocked by:** #%s" % ref
    if not any(line.strip() == marker for line in body.split("\n")):
        if any(r["ref"] == ref for r in blocker_scan.extract_refs(body, self_ref=goal)):
            return "ABSENT", ("issue %s is written in prose, not as a `Blocked by:` line; it was left alone, "
                              "edit the sentence by hand" % ref)
        return "ABSENT", "the body carries no `Blocked by:` line for issue %s" % ref
    try:
        source.remove_from_body(goal, ref)
    except ValueError as exc:
        return "REFUSED", str(exc)
    note = "Dependency marker for issue %s removed from the body." % ref
    if _plain(reason):
        note += " Reason: " + _plain(reason)
    try:
        source.note(goal, note)
    except Exception as exc:                       # noqa: BLE001 - the removal is the record; the note is narration
        return "UNMARKED", "marker removed; the explanatory note failed (%s)" % type(exc).__name__
    return "UNMARKED", "issue %s no longer blocks this goal" % ref


def _open_source(sdlc_dir):
    state, sources = _load("state"), _load("sources")
    return sources.get_source(sdlc_dir, state.load_config(sdlc_dir))


def _label_members(source, label, fetch=None):
    """-> sorted open member numbers of run label `label`: the label drain's own scope check and REST listing."""
    sources = _load("sources")
    sources.scope_to_label(source, label)                     # raises LabelScopeError, as the drain does
    fetch = fetch or (lambda run, repo, labels, cap: sources.fetch_issues_rest(run, repo, labels, cap))
    cap = _load("label_drain").MAX_MEMBERS
    listed = fetch(getattr(source, "_run", None), source.repo, [label], cap + 1)
    if len(listed) > cap:
        raise ValueError("label %r has more than %d open members; refusing rather than truncating" % (label, cap))
    return sorted(int(i["number"]) for i in listed)


def _chain_members(sdlc_dir, goal, resolve=None, config=None, now=None):
    """-> sorted numbers of goal and its open prerequisites: the rows `chain.plan` would record, not re-walked."""
    import time
    config = config if config is not None else _load("state").load_config(sdlc_dir)
    resolve = resolve or _load("chain")._resolve
    out, _ro = resolve(sdlc_dir, int(goal), config, None, time.time() if now is None else now)
    return sorted(int(r["number"]) for r in out["members"])


def _batch_args(args):
    """-> (labels, chain or None, sheet) for `--label L` (repeatable), `--chain N`, `--sheet`; None if malformed.
    `--chain` may be given once and not beside `--label`, as before (#1944)."""
    labels, chain, sheet, i = [], None, False, 0
    while i < len(args):
        if args[i] == "--sheet":
            sheet, i = True, i + 1
        elif args[i] == "--label" and i + 1 < len(args):
            labels.append(args[i + 1]); i += 2
        elif args[i] == "--chain" and i + 1 < len(args) and chain is None:
            chain, i = args[i + 1].lstrip("c#"), i + 2
        else:
            return None
    if (not labels and chain is None) or (labels and chain is not None):
        return None
    return labels, chain, sheet


_SHEET_ORDER = ("comment-edge", "weak", "mention")


def sheet_lines(rec):
    """Markdown for one report object (#1991): non-`edge` refs first, comment-edge then weak then mention, each with
    its snippets; the `edge` refs are a one-line tally (they are already written)."""
    if "error" in rec:
        return ["## #%s" % rec["goal"], "", "unreadable: %s" % rec["error"], ""]
    refs = rec["refs"]
    out = ["## #%s" % rec["goal"], ""]
    for kind in _SHEET_ORDER:
        for r in (x for x in refs if x["read_as"] == kind):
            flags = "".join(", %s" % k for k in ("reverse", "quoted", "likely-dependent") if r.get(k))
            note = (" -- probably waits on this goal; the weak read is likely the wrong way round"
                    if r.get("likely-dependent") else "")
            out.append("- #%s (%s%s): %s%s" % (r["ref"], kind, flags, " | ".join(r["snippets"]).replace("\n", " "), note))
    tally = sum(1 for r in refs if r["read_as"] == "edge")
    out += ["", "%d ref(s) already read as an edge: not listed." % tally]
    if rec.get("reverse_skipped_closed"):
        out.append("%d closed ref(s) not reverse-read." % rec["reverse_skipped_closed"])
    out.append("")
    return out


SHEET_HEADER = ("# Dependency review sheet\n\nThis is a review aid: it changes nothing. Decide per ref, then only "
                "`deps.py mark` writes a `Blocked by` line.\n")


def _report_batch(source, numbers, sheet=False):
    code = 0
    if sheet:
        print(SHEET_HEADER)
    for n in sorted(set(int(x) for x in numbers)):
        try:
            rec = report(source, n)
        except Exception as exc:                   # noqa: BLE001 - an unreadable member is reported, never skipped
            rec, code = {"goal": str(n), "error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}, 2
        if sheet:
            print("\n".join(sheet_lines(rec)))
        else:
            print(json.dumps(rec, ensure_ascii=False, sort_keys=True))
    return code


def main(argv):
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    if len(argv) < 4 or argv[1] not in ("report", "mark", "unmark") or (argv[1] != "report" and len(argv) < 5):
        print(USAGE, file=sys.stderr)
        return 2
    source = _open_source(argv[2])
    if not hasattr(source, "_read_issue") or not hasattr(source, "append_to_body") \
            or (argv[1] == "unmark" and not hasattr(source, "remove_from_body")):
        print("UNSUPPORTED deps.py reads GitHub issues only; this project uses local goal files")
        return 2
    if argv[1] == "report" and argv[3].startswith("--"):
        opts = _batch_args(argv[3:])
        if opts is None:
            print(USAGE, file=sys.stderr)
            return 2
        labels, chain, sheet = opts
        try:
            numbers = set()
            for label in labels:
                numbers.update(_label_members(source, label))
            if chain is not None:
                numbers.update(_chain_members(argv[2], chain))
        except Exception as exc:                   # noqa: BLE001 - a refusal from the shared pieces, shown as is
            print("REFUSED %s" % str(exc)[:300], file=sys.stderr)
            return 2
        if not numbers:
            print("no open members for %s" % " ".join(argv[3:]), file=sys.stderr)
            return 2
        return _report_batch(source, numbers, sheet)
    if argv[1] == "report":
        print(json.dumps(report(source, argv[3]), ensure_ascii=False, sort_keys=True))
        return 0
    verdict, detail = (mark if argv[1] == "mark" else unmark)(source, argv[3], argv[4], " ".join(argv[5:]))
    print("%s %s" % (verdict, detail))
    return 0 if verdict in ("MARKED", "ALREADY", "UNMARKED", "ABSENT") else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
