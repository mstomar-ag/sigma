"""Local-goals backlog discovery (the file source). Source selection lives in sources.py, which also
implements the GitHub-issues source; this module is the zero-dep local-files adapter.

Also resolves a goal's LANE — the ceremony tier the Research phase measured it into. Kept here with
the other goal-metadata reads (status) rather than in frontmatter.py, which is a parser with no goal
semantics."""
import sys, pathlib, importlib.util

_HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("frontmatter", _HERE / "frontmatter.py")
frontmatter = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(frontmatter)

_TERMINAL = {"done", "parked", "failed"}
# proposed = detector-suggested, awaiting HUMAN promotion (edit status -> pending).
# Not terminal, but never auto-picked: proposing work is safe, running it is gated.
_SKIP = _TERMINAL | {"proposed"}


#: Ordering keys for the pending queue. `priority` sorts by the goal's own priority first and falls
#: back to filename order within a tier; `created` is the historical strict filename order.
ORDERS = ("priority", "created")
DEFAULT_ORDER = "priority"

PRIORITIES = ("P0", "P1", "P2", "P3", "P4")
#: Absent / unrecognised priority sorts AFTER every real one. This is the whole backward-compatibility
#: hinge (#698): on a backlog where nothing carries a priority, every goal lands in this one bucket and
#: the sort collapses to the historical key, so ordering is bit-for-bit unchanged for such a repo.
UNPRIORITISED = len(PRIORITIES)


def priority_rank(value, aliases=None):
    """Sort rank for a priority: 0 for P0 … 4 for P4, `UNPRIORITISED` for absent or unrecognised.

    Deliberately total and never-raising. It accepts a bare tier (`P1`), any case (`p1`), and a
    value that still carries the GitHub label prefix (`priority:P1`) — so the GitHub side can hand
    it a raw label name and the local side a raw frontmatter value, without either having to know
    the other's spelling. A typo (`priority:high`, `P9`) is NOT guessed at: it ranks as
    unprioritised, which is the safe direction — a goal nobody can classify must not jump the
    queue on the strength of a misspelling.

    `aliases` (optional, default None) is a `{alternate spelling: canonical P0-P4 tier}` dict — an
    adopter's OWN vocabulary (`{"critical": "P0", "high": "P1", ...}`), config-driven (see
    `discovery.priority_aliases` in sources.py), so this stays a typo-safe exact match rather than
    a guess: an unlisted spelling still ranks UNPRIORITISED, same as always. Checked case-
    insensitively, after the label-prefix strip, and ONLY when the literal P0-P4 match already
    failed — a literal tier ALWAYS wins over any alias, full stop, even one whose key happens to
    collide with a real tier spelling (a plausible misconfiguration: an example map copy-pasted and
    only the value edited, leaving a stray `"P0": "P4"`-shaped key behind). Without this ordering,
    such a config would silently remap the meaning of every literal occurrence of that tier
    repo-wide — a far larger blast radius than the bug aliases exist to fix, and the opposite of
    what "just another spelling of a tier" is supposed to mean. Omitting `aliases` (the default) is
    byte-identical to every call site before this parameter existed."""
    text = str(value or "").strip().lower()
    if ":" in text:                      # tolerate a raw label, e.g. "priority:P1"
        text = text.rsplit(":", 1)[1].strip()
    for rank, name in enumerate(PRIORITIES):
        if text == name.lower():
            return rank
    if aliases:
        canon = {str(k).strip().lower(): v for k, v in aliases.items()}.get(text)
        if canon:
            canon_text = str(canon).strip().lower()
            for rank, name in enumerate(PRIORITIES):
                if canon_text == name.lower():
                    return rank
    return UNPRIORITISED


def priority_of(goal, aliases=None):
    """The priority rank for a LOCAL goal file (or raw goal text) — its `priority:` frontmatter key.

    Mirrors `lane_of` one screen down, including its tolerance: a path that is not a readable file
    is treated as raw text, and anything unreadable or unrecognised ranks as `UNPRIORITISED` rather
    than raising. `aliases` passes through to `priority_rank` unchanged — see its docstring."""
    text = goal
    try:
        p = pathlib.Path(goal)
        if p.is_file():
            text = p.read_text(encoding="utf-8")
    except OSError:
        return UNPRIORITISED
    return priority_rank(frontmatter.get(text, "priority"), aliases)


def next_pending(goals_dir, skip=(), order=DEFAULT_ORDER, aliases=None):
    """The next runnable goal: a *.md file whose status is not done/parked/failed/proposed, chosen by
    `order`. None if there is none. Files without frontmatter (e.g. README.md) are not goals. `skip`
    holds goals a claim lease has assigned to another loop this pass — they are passed over so two
    loops don't start the same one.

    `order='priority'` (default) sorts by the goal's `priority:` frontmatter first, filename second;
    `order='created'` is the historical strict filename order. Ordering only ever changes WHICH
    eligible goal comes first — never which goals are eligible: the status filter below is applied
    exactly as before, so no priority can promote a terminal or `proposed` goal. `aliases` passes
    through to `priority_rank` unchanged — omitted (the default), a goal written `priority: Critical`
    ranks UNPRIORITISED exactly as it always has."""
    skip = {str(s) for s in skip}
    pending = []
    for path in sorted(pathlib.Path(goals_dir).glob("*.md")):
        if str(path) in skip:
            continue
        text = path.read_text()
        status = frontmatter.get(text, "status")
        if status and status not in _SKIP:
            if order == "created":
                return str(path)                       # historical fast path: first match wins
            pending.append((priority_rank(frontmatter.get(text, "priority"), aliases), str(path)))
    if not pending:
        return None
    return min(pending)[1]                             # (rank, path): filename breaks ties, as before


def goal_index(goals_dir):
    """#1550: `{key: [(path, status), ...]}` over every goal file (a `*.md` with a `status`, the
    same goal rule as `next_pending`), for resolving a local `Blocked by` ref. Keys per file: its
    stem, its leading filename digits as `str(int(...))`, and an all-digit frontmatter `id` the same
    way. A key carrying two paths is AMBIGUOUS and the caller holds on it. Paths come from the same
    glob as `next_pending`, so comparing one with a picked goal is a plain string compare. Cost: one
    read per file, built at most once per pick and only when a candidate declares a ref."""
    index = {}
    for path in sorted(pathlib.Path(goals_dir).glob("*.md")):
        text = path.read_text()
        status = frontmatter.get(text, "status")
        if not status:
            continue
        lead = path.name[:len(path.name) - len(path.name.lstrip("0123456789"))]
        fid = str(frontmatter.get(text, "id") or "")
        keys = {path.stem} | {str(int(v)) for v in (lead, fid) if v.isdigit()}
        for key in keys:
            index.setdefault(key, []).append((str(path), status))
    return index


#: Valid values for the `discovery.blocker_promotion.mode` config key. Resolving/validating the raw
#: config value is sources.py's job (this module stays config-dict-free per the module docstring
#: above) -- but the valid-values tuple and its safe default live here, next to the ranking domain
#: they describe, mirroring ORDERS/DEFAULT_ORDER above.
BLOCKER_PROMOTION_MODES = ("off", "smart", "always")
#: "always" is the default (#1406): a blocker's priority must never be below what it blocks, so an
#: adopter who never sets this key gets the full rule. An explicit "off" stays byte-identical to
#: the behaviour before the rule existed (a blocker's rank is never touched).
DEFAULT_BLOCKER_PROMOTION_MODE = "always"


def blocker_promotion_rank(own_rank, dependents=(), mode=DEFAULT_BLOCKER_PROMOTION_MODE):
    """The PROMOTED rank for a blocker, given the rank and pickability of every issue it directly
    blocks (#900). Kept as a config-dict-free pure function, like the rest of this module, so the
    ranking rule is unit-testable without a goal file, a config dict, or a blocking graph.

    `own_rank` is the blocker's own current rank (whatever `priority_rank` returned for it -- this
    function has no opinion on where the rank came from, only that lower means more urgent). `mode`
    is the already-resolved `discovery.blocker_promotion.mode` value -- see BLOCKER_PROMOTION_MODES.
    `dependents` is an iterable of `(dependent_rank, has_other_unblocked_work)` pairs, one per issue
    this blocker directly blocks:
      - `dependent_rank` is that dependent's own rank.
      - `has_other_unblocked_work` is whether the dependent ALSO has other unblocked/pickable work
        sitting at its own tier already -- i.e. promoting this blocker would not change what the loop
        picks next for that dependent's tier, so "smart" mode has nothing to gain by promoting on
        that dependent's account.

    Mode semantics:
      - "off" (default; also the fallback for any unrecognised or missing mode -- fail safe, not fail
        loud, matching this module's existing conservative-default posture): `own_rank` unchanged.
        Callers should typically skip calling this at all when mode is "off", to keep that path as
        cheap and obviously-correct as possible -- this branch exists so a defensive caller never
        NEEDS to skip it.
      - "always": the minimum (most urgent) rank among `own_rank` and every dependent's rank. When
        every dependent ranks at or behind `own_rank` already, that minimum IS `own_rank`, so nothing
        changes -- "if no dependent is more urgent, return own_rank unchanged" falls out of `min()`
        rather than needing its own branch.
      - "smart": the same `min()` computation as "always", but a dependent only enters the pool when
        its `has_other_unblocked_work` is False. A dependent with other pickable work at its own tier
        is excluded ENTIRELY from consideration for this evaluation -- not merely capped -- so a
        highly urgent but ineligible dependent cannot pull the blocker's rank at all, even partway.

    Multi-dependent tie-break, "always" and "smart" alike: the minimum rank among the eligible
    candidates wins. No further tie-break logic is needed -- a lower rank is unconditionally more
    urgent no matter how many dependents share it or which one happens to be listed first.

    ONE LEVEL ONLY, BY DESIGN: this function looks at a single blocker and its DIRECTLY-blocked
    dependents. It does not walk a blocking graph, and it is not this function's job to. A transitive
    chain (A blocked by B blocked by C) propagates correctly when the CALLER walks outward from the
    most-blocked issue and calls this function once per edge, feeding one level's returned rank in as
    the next level's `own_rank` -- e.g. compute B's promoted rank from A first, then compute C's
    promoted rank using B's *promoted* (not original) rank as C's dependent-rank input. Building that
    walk -- plus resolving `discovery.blocker_promotion.mode` from config and writing the promoted
    rank back out -- belongs in sources.py, a different slice; this function is only the one-level
    ranking rule that walk will call once per edge.
    """
    if mode not in BLOCKER_PROMOTION_MODES or mode == "off":
        return own_rank
    ranks = [own_rank]
    for dependent_rank, has_other_unblocked_work in dependents:
        if mode == "smart" and has_other_unblocked_work:
            continue
        ranks.append(dependent_rank)
    return min(ranks)


def blocker_required_ranks(edges, ranks):
    """The shared blocker-priority rule (#1406): a blocker's priority is never below what it blocks.

    Signature: `blocker_required_ranks(edges, ranks) -> (required, cycles)`.
      - `edges`: an iterable of `(blocker_ref, blocked_ref)` pairs, one per direct "blocker blocks
        blocked" edge. Refs are any hashable value; a duplicate or self edge is ignored.
      - `ranks`: a mapping of ref to its CURRENT rank (the `priority_rank` scale, lower is more
        urgent). A ref missing from the mapping, or mapped to None, is unprioritised: the lowest
        urgency (`UNPRIORITISED`).
      - returns `required`: a dict mapping each blocker whose rank must RISE to
        `(new_rank, cause_ref)`, where `cause_ref` is the most urgent direct dependent (the first
        one in edge order on a tie). Raise only: a ref already at least as urgent as everything it
        blocks is absent. Transitive: C blocks B blocks A raises C to A's rank when A is the most
        urgent.
      - returns `cycles`: a sorted list of every ref that sits in a blocking cycle, each reported
        once. A cycle never loops and never hangs: a back-edge contributes no pressure beyond the
        re-entered ref's own current rank.
    Pure: neither argument is mutated, and no I/O happens. Callers do the reading and the writing.
    """
    ranks = ranks or {}

    def rank_of(ref):
        value = ranks.get(ref)
        return UNPRIORITISED if value is None else value

    blocks = {}
    for by, blocked in edges:
        if by == blocked:
            continue
        lst = blocks.setdefault(by, [])
        if blocked not in lst:
            lst.append(blocked)

    resolved, in_progress, stack, cycles = {}, set(), [], set()

    def resolve(root):
        # Iterative (an explicit stack): a long chain must not hit the interpreter recursion limit.
        # Same visiting order and the same cycle / partial-answer behaviour as the recursive walk.
        if root in resolved:
            return resolved[root]
        in_progress.add(root)
        stack.append(root)
        frames = [[root, iter(blocks.get(root, ())), rank_of(root)]]
        result = None
        while frames:
            frame = frames[-1]
            if result is not None:
                frame[2] = min(frame[2], result)
                result = None
            descended = False
            for dep in frame[1]:
                if dep in resolved:
                    frame[2] = min(frame[2], resolved[dep])
                elif dep in in_progress:
                    cycles.update(stack[stack.index(dep):])
                    frame[2] = min(frame[2], rank_of(dep))
                else:
                    in_progress.add(dep)
                    stack.append(dep)
                    frames.append([dep, iter(blocks.get(dep, ())), rank_of(dep)])
                    descended = True
                    break
            if descended:
                continue
            ref, _it, best = frames.pop()
            stack.pop()
            in_progress.discard(ref)
            resolved[ref] = best
            result = best
        return resolved[root]

    for ref in list(blocks):
        resolve(ref)
    # #1415: the walk above finds the cycles, but a ref resolved while its ring was still open keeps a
    # partial answer that depends on the entry point. Relax to the fixed point so the result is the same
    # from any entry: each ref takes the most urgent rank among itself and everything it blocks.
    changed = True
    while changed:
        changed = False
        for ref in blocks:
            best = min([resolved[ref]] + [resolved.get(d, rank_of(d)) for d in blocks[ref]])
            if best < resolved[ref]:
                resolved[ref], changed = best, True
    required = {}
    for ref, new_rank in resolved.items():
        if ref not in blocks or new_rank >= rank_of(ref):
            continue
        cause = next((d for d in blocks[ref] if resolved.get(d, rank_of(d)) == new_rank), None)
        required[ref] = (new_rank, cause)
    return required, sorted(cycles, key=str)


LANES = ("small", "medium", "large")
#: Unsized goals get the FULL pass. `lane: auto` means Research hasn't measured it yet (or was
#: skipped), and guessing "small" on an unknown goal would skip ceremony the goal might need —
#: the one direction where being wrong is expensive. Unknown fails toward more rigour, not less.
DEFAULT_LANE = "medium"


def lane_of(goal):
    """The ceremony tier for a goal: 'small' | 'medium' | 'large'.

    `goal` is a path to a local goal file, or raw goal text. Anything unrecognised — absent, `auto`,
    a typo, an unreadable file — resolves to DEFAULT_LANE.

    LOCAL MODE ONLY, by design: this module is the local-files adapter, and a GitHub issue number
    carries no lane (Research records it in the issue timeline, which sources.py owns). Passing an
    issue number here returns DEFAULT_LANE — safe, but it is not a lane lookup, so github mode reads
    the lane from the phase note instead. Both modes apply the same unknown-goal default.
    """
    text = goal
    try:
        p = pathlib.Path(goal)
        if p.is_file():
            text = p.read_text(encoding="utf-8")
    except OSError:
        return DEFAULT_LANE
    value = (frontmatter.get(text, "lane") or "").strip().lower()
    return value if value in LANES else DEFAULT_LANE


USAGE = "usage: discovery.py lane <goal-path-or-text>"


def main(argv):
    if argv[1:] in (["-h"], ["--help"]):
        print(USAGE)
        return 0
    if len(argv) >= 3 and argv[1] == "lane":
        print(lane_of(argv[2]))
        return 0
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    # The script-time floor: this run's wall time, recorded to the session resolved at exit. This
    # module has no sibling loader of its own, so the store is loaded here, by path, CLI-only.
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("timing_store", _HERE / "timing_store.py")
    _ts = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_ts)
    sys.exit(_ts.timed_main(main, sys.argv, "discovery"))
