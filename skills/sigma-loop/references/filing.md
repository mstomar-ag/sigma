# Filing work you find mid-goal

The detail behind step 3's hand-off and follow-up paths in [`../SKILL.md`](../SKILL.md):
handing a cross-area blocker to its owner, filing a same-area follow-up through
`handoff.py track` instead of `gh issue create`, and how a finding about Sigma itself
is routed away from this project's board.

---

**Blocked on someone else's AREA? Hand it off before you park.** A dependency in code another
person owns is not a decision for the user — parking it silently tells nobody and the work
stalls until a human happens to notice. Instead:
`python3 "${CLAUDE_SKILL_DIR}/scripts/handoff.py" open .sdlc "$goal" --area <area> --why "<what
is needed>" [--priority P0|P1|P2]`. It resolves the owner from the repo's CODEOWNERS, opens an
issue in their area **assigned to them and carrying the goal label** — so their own loop picks
it up — records it in the team ledger addressed to them, and links it from this issue. THEN park
this goal as normal. Degrades honestly: with no owner, no `gh`, or a local backlog it still
records the ledger entry.

**Found something worth tracking that isn't a cross-area blocker? Never call `gh issue create`
directly.** A same-area follow-up, a review finding, anything worth its own issue but not a
decision for the user right now, still needs a label and an assignee or it is orphaned — the
majority-real-world shape of this bug, worse than the cross-area case above because nothing
documented the right command for it until now. Use `handoff.py track` the same way a cross-area
blocker already goes through `handoff.py open`:
`python3 "${CLAUDE_SKILL_DIR}/scripts/handoff.py" track .sdlc "$goal" --area <area> --why "<what
you found>" --queue actionable|queued --assignee same-area|cross-area --blocks yes|no [--priority
P0|P1|P2] [--label model:<tier>] [--title T] [--body-file F]`. All three value-flags are
required, with no default, on purpose. **Triage decides, by default.** Unless
`ai_filed.triage.enabled` is false, an AI-filed issue is checked against a deny-list (hard-stop
words and shapes, secret shapes, a recorded-permission marker on the first body line) and then
either ARMED at its bucket priority (urgent P2, routine P3, unclear P4 — always below a
human-filed P1) or PARKED with a declared question kind. `--queue queued` means "let triage
decide"; `--queue actionable` still runs the deny-list and arms at most as urgent as the bucket (a
`--priority` may lower urgency, never raise it). Nothing gets the old needs-confirmation label any
more; a leftover one stays inert. Caps stop a runaway: `ai_filed.max_per_goal`, `.max_per_run`
and `.max_depth` park the excess. With triage switched off the old rule returns: `queued` files
the issue withholding `sdlc:goal` for a human to promote. **`--blocks yes` is what should drive
`--queue actionable`, not a separate judgment call** — the ONLY things `--queue actionable` is for
are (1) a genuine blocking dependency (`--blocks yes`), or (2) work a human EXPLICITLY asked for
by name in this exact session — never a vibe of "this seems urgent" on a finding nobody asked
for; otherwise it is `queued`.
Getting `--blocks` wrong in either direction is a real bug: `yes` on a merely-related finding
incorrectly parks unrelated work; `no` on a genuine blocker leaves the current goal silently
stuck with nothing surfacing it as the reason. **`--blocks yes --queue queued` is contradictory
and is no longer accepted as written**: it says "real work is stalled behind this" and "nobody
may pick this up" at once, which used to file a blocker no queue could serve while the goal it
blocked waited for it to close — a deadlock with neither side able to move. It is armed
(never queued); a deny-listed blocker is parked, and the goal it blocks waits on that park. Pick `--queue` deliberately
anyway; a warning you learn to ignore is not a guardrail. Choose `--assignee same-area` to file it to
yourself — you're already working this area; `--assignee cross-area` routes it through
CODEOWNERS like `open` does. `sdlc:followup` is applied automatically to every issue this
command files — you do not need to (and no longer can meaningfully) pass it yourself via
`--label`; use `--label` only for anything ADDITIONAL, e.g. `model:<tier>` — pass it once
carrying the whole comma-separated list, a repeated `--label` flag still keeps only the last
one. `--queue` is what sets the goal label — putting `sdlc:goal` in `--label` yourself overrides
that and makes a queued issue pickable; let `--queue` own it. **The UNIT OF WORK is inherited
automatically too**: when the goal you are filing from belongs to a `feature:<name>` unit, the
new issue is stamped with that unit's body marker *and* its label without you passing anything.
Do not pass `--label feature:<name>` yourself and do not write a `Feature:` marker into
`--body-file` by hand — both halves come from the goal's own declaration, and a second,
disagreeing one added by hand is the single state no reader downstream can resolve.
**When the goal you are filing from has no unit either** (and this repo has opted into
`discovery.no_dangling_goal` with a configured `.core` catch-all), the issue is classified
automatically — no flag needed, and this only ever engages when neither an inherited unit nor an
explicit `--target-unit` exists. Pass `--target-unit <name>` yourself when *you* are confident this
finding belongs to a specific, already-open unit that is not the one the filing goal inherits —
that explicit choice always wins outright and classification is never even attempted once you give
one.

**What "classified automatically" actually resolves to today.** `handoff.create_tracked_issue`'s
classification step (`feature_classify.classify_for_filing`, `docs/branching-model.md` §18) is
narrower here than at pick time by design — it can only target an already-open unit or the
configured catch-all, never set the new issue aside or flag it, since there is nothing on the board
yet for either overlay to attach to. It also defaults to a judge that never guesses a specific
unit — it abstains — because nothing in the kit supplies a real one yet. So as of this writing, "the
issue is classified automatically" in practice means it lands on the configured catch-all (`core`,
or whatever `discovery.no_dangling_goal.core` names) exactly as if you had typed
`--target-unit <that name>` yourself, not a live judgment about which existing unit this specific
finding belongs to. `--target-unit` remains the only way to route a same-area follow-up to a
*different* already-open unit until a real judge is wired in.

**A finding about SIGMA ITSELF is routed away from this board automatically — cite the
path.** The board you are filing on is the list of work *this project* needs. A finding about the
kit's own internals does not belong on it: it competes with their features for their attention,
and it never reaches anybody who maintains the kit. So `track` and `open` both classify every
finding first, and a kit one is withheld from this board — written in full to
`.sdlc/state/withheld/<goal>.md`, raised in the ledger addressed to whoever is running the loop,
and filed on `ledger.handoff.upstream_repo` when one is configured and this account has confirmed
write access to it. You do not invoke anything different and there is no flag to pass. **What you
control is the evidence**: the detection is structural — a path in your `--why`/`--title`/
`--body-file` that (a) exists under the plugin's install path, (b) sits under one of the
plugin's **signature** directories `skills/`, `hooks/`, `.claude-plugin/`, and (c) sits under a
top-level directory this repo does not have. So write `skills/sigma-loop/scripts/loop.py
mishandles …`, not `loop.py mishandles …` — a bare filename names no path. Nothing under
`docs/`, `tests/`, `evals/`, `examples/`, a private package's directory or `.github/` is ever eligible, even though
the kit ships files under all of them: those are names an ordinary repo has too, and treating
them as the kit's took *"there is no CI, add .github/workflows/ci.yml"* off a young repo's own
board. That direction is deliberate:
withholding a real finding about *this* project would be worse than filing a kit one, so anything
ambiguous stays local. In this repository (Sigma developing Sigma) the kit's files *are*
the project's files, and everything files normally.
