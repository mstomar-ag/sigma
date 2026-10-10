# On CONFIRM — the overlay, the tickets, and the phantom-blocker guard

The detail behind step 4 of [`../SKILL.md`](../SKILL.md): writing the `sdlc:designed` overlay,
building the Spec/Epic plan JSON and compiling it, the phantom-blocker wording rule and its
runnable control, and the `--forbid-priority` enforcement.

---

## 4. On CONFIRM

**a. Write the overlay:**
```
python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/loop.py" mark-designed .sdlc <n>
```
This is the ONLY place `sdlc:designed` is ever written — `design_check` (#1825) only ever reads it.
Prints `OK`/`FAILED` (best-effort — see `mark_designed`'s own docstring in `sources.py`) or
`UNSUPPORTED` on a source with no story/epic tickets (e.g. `LocalSource`), in which case stop here:
there is nothing further this skill can do on that source.

**b. If the design's own slice-count estimate is more than one independently-implementable unit**,
produce the Spec/Epic ticket and its children — reusing `compile_plan.py` exactly as it stands
today (contract §8: the markers are code-written and trigger-word-free), never hand-rolled:

- Build a plan JSON from the design write-up's slice breakdown: one `issues[]` entry per slice
  (`key`, `title`, `body` — the slice's own scope from the write-up, plus a pointer back to
  `.sdlc/design/<n>.md` and to `#<n>`, **worded per the warning below** — and `blocked_by` edges from
  any sequencing the design states, plus 2c's open-item edges), and an `epic` object (`title`,
  `body` — the design's own summary plus a pointer to `.sdlc/design/<n>.md`, and
  `originates_from: <n>` — #1827: `compile_plan.py`'s `_create_epic` writes the trigger-word-free
  `Originates from Story #{n}.` line onto the epic's own body itself, at creation, from this field;
  do not hand-type that sentence into `body`). Write it to `.sdlc/state/goal-review/<n>.plan.json`
  — `mkdir -p .sdlc/state/goal-review` first, and see "Where those two files live" below for why
  there and not beside the design write-up.

- **`Depends on` → `blocked_by`, and a token that is not a slice id is a schema violation —
  adjudicate it, never drop it (#1956).** Read each cell by splitting on commas and stripping the
  tokens; every one of them must be a `Slice` value from that same table. `goal-design` §5 forbids
  anything else there — but a real pass wrote `3, and B-1` (a slice edge AND a Blockers id) and the
  `B-1` was patched away by hand between the two stages, silently. A `B-n` in that cell is a
  **pre-filled blocking claim**: the design has already named a slice that cannot start and what it
  is waiting for, which is 2a's own operational test. So put it through 2a's three buckets and write
  down which one it landed in:
  - **blocking** → you are not at step 4 at all. 2b makes that a REJECT; step 3's findings name the
    slice and the blocker it waits on.
  - **resolved** → the edge is discharged. Leave it out of `blocked_by` entirely, and say in 4d's
    outcome comment which slice, which blocker, and what discharged it.
  - **open, not blocking** → `blocked_by` keeps that slice's slice keys and nothing else, and 2c's
    comment line carries the item, naming the slice it bears on.

  **Never pass the token through, and never invent a plan key for it.** Passed through,
  `_validate_and_order` refuses the WHOLE plan before any `gh` call —
  `compile_plan: issue '4' is blocked_by unknown key(s) ['B-1']` — so nothing at all is created and
  the pass stops with the design already confirmed. Invented as a key, it files a real,
  immediately-pickable ticket for a design blocker that has no scope of its own.

- **Carry a capped sweep onto the epic body, as its first line (#1952).** Where the design's
  `Sweep` field reads `capped`, the `epic` object's `body` OPENS with the design's own
  incomplete-sweep statement — the budget it ended on, and how many seeds are still unswept —
  worded per the phantom-blocker warning below, so no trigger word lands within 40 characters of a
  `#N`. This is the whole point of reading the field in step 1: the Epic reaches everyone with board
  access, while `.sdlc/design/<n>.md` reaches only whoever has the repo checked out — and not even
  them on a repo that ignores `.sdlc/`. An Epic that reads as complete over a partial design is
  worse than no design at all.
  Where it reads `converged`, write nothing — the absence is the claim, and there it is a true one.

- **Never restate a premise the design falsified — on the epic body or on any child (#1976).**
  Where `## Premise check` carries a `falsified` row, the bodies you write here state the
  **corrected** premise, and the epic body says in one clause that the source's own framing moved
  (`PC-n`, and what is actually true). These bodies are written from the design's `Intent`, which
  §1 already required to carry the correction, so this is a check rather than fresh authorship —
  but it is the check that stops the error propagating, because every child ends up pointing back
  at `Story #<n>` whose body still says the wrong thing. Worded per the phantom-blocker warning
  below, like everything else here.

- **Omit `priority` — on the epic and on every child (#1922).** `compile_plan.py` applies
  `DEFAULT_PRIORITY` (which IS `handoff.DEFAULT_PRIORITY`, `"P1"`) to any entry that does not carry
  one, so omitting it is a defined single-constant behaviour, not a gap. Do not derive one either:
  `goal-design`'s artifact format carries no priority anywhere, so any number written here is your
  own judgement applied to a document that never expressed it — and it lands as a REAL
  `priority:P<n>` label and, on a project-enabled repo, a real board field, where two passes over
  the same design would disagree with each other. The ordering information the design DOES carry is
  its **sequencing**, and sequencing belongs in `blocked_by` edges, which are checkable. If a slice
  genuinely warrants a different priority, that is a human's one-gesture edit on the board
  afterwards, or a change to `goal-design`'s own artifact so the number has a source — never a guess
  at this step.

  **This is enforced now, not merely stated (#2027).** The 2026-09-01 validation run wrote
  `"priority": "P2"` into all six children of story #2017's plan with this bullet already on the
  page, because nothing measured it. The invocation below therefore carries `--forbid-priority`,
  and `compile_plan.py` **refuses** a plan whose epic or any child has the key — every offender
  named in one message, before a single `gh` call, so **exit 2 with zero tickets is the correct
  output of a plan that broke the rule, not a tool failure**. The fix is to delete the keys from
  `.sdlc/state/goal-review/<n>.plan.json` and re-run the one command; `sdlc:designed` and
  `.sdlc/design/<n>.md` are already durable and are not lost. Presence is what is refused, so an
  explicit `"P1"` is refused too — it is still a number you chose, it merely collides with the
  default. **The flag is belt and the path is braces:** a plan file living under
  `.sdlc/state/goal-review/` turns the refusal on by itself, so forgetting the flag does not
  quietly restore the honour system.

**Warning — a hand-authored body containing `#N` can mint a phantom blocker (#1920).**
`blocker_scan._compile` matches any of `blocked by`, `depends on`, `depends upon`, `needs`, `after`,
`requires`, `waiting on` followed within **40 characters** by a `#N`, with no clause punctuation
(`, ; . ! ?`), newline or `#` in between. So an ordinary sentence of scope prose — *"this needs the
model field from #3"* — is read as a dependency edge at confidence 1.0. That is not advisory:
`loop.py`'s `_resolve_blockers_for_park` hands a confident finding to `blockers.resolve`, which
**writes to the referenced issue** — an `sdlc:proposed` → `sdlc:goal` label swap, a board move and a
comment on a third, unrelated ticket — and `auto_unpark.compute_blocking_actions` then stamps
`sdlc:blocking` on it. `compile_plan.py` is meticulous about this for the text IT writes (`Tracks
#N`, `Part of epic #N.`, `Originates from Story #N.` are all deliberately trigger-word-free, per its
own docstring); the bodies YOU write here get no such care by default.

- **Write the pointers in that same shape** — `Design: .sdlc/design/<n>.md` and `Story #<n>.`, each
  on its own line. Never *"needs … #N"*, *"after #N"*, *"requires #N"*, *"waiting on #N"*,
  *"depends on #N"*, *"blocked by #N"*.
- **Express a real dependency as a `blocked_by` edge, never as a sentence.** `compile_plan.py`
  writes the canonical `**Blocked by:** #N` marker itself, one per line, from the edge — that marker
  is SUPPOSED to match, and it is the one form the machinery reads correctly.
- **The same wording rule governs every COMMENT this skill writes, not only the bodies.** The
  check-time scan's haystack is the body excerpt PLUS the goal's own comment text —
  `backlog_check._goal_comment_text` feeds `_explicit_blockers` as `extra_text`, through the one
  shared `_blocker_haystack`, with the FULL trigger set. So 2c's open-item lines in step 4d, step
  3's REJECT findings and step 5g's templates are scanned exactly like a body: *"…which is waiting
  on #1830 landing first"* in an outcome comment mints the same confident edge. The control below
  reads the plan FILE, and a comment has no file to scan — for comments the wording is the only
  guard, so re-read each one before posting it.

Then run the control before compiling anything — a guard nobody has watched fail is decoration
(`AGENTS.md`). Scan the plan's INPUT bodies, i.e. the file as you wrote it, before `compile_plan.py`
appends its own markers:

```
python3 -c "
import json, sys
sys.path.insert(0, '${CLAUDE_SKILL_DIR}/../sigma-loop/scripts')
import blocker_scan as bs
plan = json.load(open(sys.argv[1]))
rows = [('epic', plan.get('epic') or {})] + [(i['key'], i) for i in plan['issues']]
hits = 0
for key, item in rows:
    text = (item.get('title') or '') + '\n' + (item.get('body') or '')
    for phrase, ref in bs._BLOCK_RE.findall(bs.strip_unpark_qa(text)):
        print('PHANTOM %s: %r ... #%s' % (key, phrase, ref)); hits += 1
print('clean -- no phantom edge' if not hits else '%d phantom edge(s): reword before compiling' % hits)
" .sdlc/state/goal-review/<n>.plan.json
```

Prove it can fail before you trust a `clean`: paste `this needs the model field from #3` into one
body, re-run, watch it print `PHANTOM`, then take it back out. `_BLOCK_RE` alone covers both live
scans — `EXPLICIT_TRIGGERS` is a PREFIX of `TRIGGERS` and both patterns come out of the same
`_compile`, so anything the fetch-time scan could match this one matches too. It is deliberately
STRICTER than reality (the check-time scan only ever sees a title plus the first
`mirror._EXCERPT_CHARS` characters of a body), so reword whatever it flags rather than reasoning
about whether the sentence falls past the cap.

- Run it, asking for the machine-readable report (#1919):
  ```
  python3 "${CLAUDE_SKILL_DIR}/../sigma-scope/scripts/compile_plan.py" .sdlc \
      --plan .sdlc/state/goal-review/<n>.plan.json --actionable --json --forbid-priority \
      > .sdlc/state/goal-review/<n>.report.json
  ```
  `--actionable` because these slices came out of a CONFIRMED design and should be immediately
  pickable (`sdlc:goal`, not the legacy `sdlc:needs-confirmation`). `--json` because every step below is
  written against `report["epic"]` and `report["issues"]`, and without it this CLI emits neither:
  stdout carries prose (`epic: #2`, `created 's1' as #3`) in dependency-topological rather than plan
  order, and the warnings go to stderr interleaved with `FAILED`/`SKIPPED` lines. With `--json`,
  stdout is exactly one JSON object — the whole report — so `json.loads` on it is the entire parse,
  and reading it BY KEY makes the ordering irrelevant. The stderr diagnostics are unchanged by the
  flag and are not folded into the JSON's place: **read them too**, they are how you find out a
  child did not land. `--forbid-priority` (#2027) makes the CLI refuse the whole plan, before any
  `gh` call, if any entry carries a `priority` — see the "Omit `priority`" bullet above for why.

  **Where those two files live.** Both under `.sdlc/state/goal-review/`, which is in
  `setup.RUNTIME_IGNORES` and is therefore gitignored on any repo `/sigma-setup` touched — the plan
  is a one-shot compile input and the report is its receipt, and neither is a source artifact.
  `.sdlc/design/<n>.md` is deliberately NOT in that list: the design write-up is the durable,
  reviewable record, and the traceable link back from every created ticket is `Originates from Story
  #<n>.` on the epic and `Story #<n>.` in each child body — never the plan file.

- **Stamp the new Epic ticket itself (`report["epic"]`, read from that report file) with
  `sdlc:designed` too** — `mark-designed .sdlc <epic-number>`. Proposal §3's SPEC/EPIC box names
  this overlay directly on the Epic tier ("Label: `epic` ... Overlay: `sdlc:designed`"); step 4a
  above only stamped `<n>` (the Dossier/Story this Epic was produced from, or the retrofit goal it
  was produced from), which is a DIFFERENT ticket than the Epic `compile_plan.py` just created. Skip
  only if `report["epic"]` is `null` (a single-issue plan — see 4c instead).
- **Stamp every child `compile_plan.py` reports created with `sdlc:designed` too** — one
  `mark-designed` call per number in `report["issues"]`. This is not optional: if
  `goal_design.enabled` is also on, the very next `sigma-loop` pick of an unstamped child re-triggers
  the retrofit gate on a slice that was already produced by a confirmed design, reopening the exact
  coverage hole `goal-review` exists to close. Anything in `report["failed"]` or `report["skipped"]`
  was never created — do not stamp it; report it.

**c. If the design concludes no further slicing is needed** (a single coherent unit — §4's light
path on the Dossier side, and the common outcome on the retrofit side): **no Epic. That is not the
same as no ticket, and which one it means depends on the path** (#1954).

- **Retrofit path — `sdlc:designed` alone is the whole output.** Skip 4b entirely. `#<n>` is
  already the tech goal the slice would have been, so there is nothing to create. A human's
  `/sigma-unpark` (never a raw label edit — see `docs/label-model.md`) is what restores `sdlc:goal`
  on `#<n>` if the retrofit gate had parked it; this skill does not perform that swap itself.
- **Dossier path — run 4b with a ONE-ROW plan and no `epic` object.** `#<n>` is `story`-labelled and
  never `sdlc:goal` (`sigma-dossier`: the loop must never try to execute a Dossier), so skipping 4b
  here would leave a confirmed design on a ticket **nothing can ever pick** — the idea dies at this
  gate with the overlay on it, which is the exact opposite of what a light path is for. Build the
  plan as 4b describes with a single `issues[]` entry and **omit `epic` entirely**;
  `compile_plan.py` creates the Epic wrapper only for a plan carrying more than one issue
  (`if epic_data and len(items) > 1`), so `report["epic"]` comes back `null` — which is precisely
  the case 4b already tells you to skip the Epic stamp for — and `report["issues"]` carries the one
  `sdlc:goal` child to stamp and report. A small idea therefore costs a story and a slice,
  **2 tickets**, where the six-component run cost 7.

**d. First, land the Stage-1 design PR** (#2482 — every CONFIRM branch reaches this point,
which is why it lives here rather than inside 4b/4c):
```
python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/work.py" merge-design .sdlc <n>
```
Prints one line — `merged PR #<n>`, `no open design PR found -- nothing to land` (the ordinary,
sanctioned outcome for a branchless/local-only design, or one already landed by hand), a
`PR #<n> has conflicts -- land it by hand` refusal, `work is off ... -- land this PR by hand`
(merge-design, unlike close-design, honors `config.work.enabled` — an automated merge to the base
branch is exactly what that gate exists to govern), or a `could not merge ... (see stderr for detail)`
failure. Capture that line; it becomes part of the comment below. **Never** a reason to stop or
retry the comment, or to unwind the Epic/slices already created above — a merge hiccup here is a
line of text, not a gate.

**Then comment on `#<n>`** naming the outcome — the epic number and its children where one was
created, the single slice's number where 4c's Dossier branch created just that, or that no further
slicing was needed if 4c applied on the retrofit path — and, per 2c, every item the adjudication
left **open, not blocking**, one line each with the slice it bears on, plus the design-PR outcome
just captured:
```
python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/loop.py" note .sdlc <n> \
    "goal-review: CONFIRMED (<k> open questions carried) [sweep: converged|CAPPED] -- <outcome> -- design PR: <merge-design's own result>"
```
The count is written even when it is zero, and the `sweep:` marker is written on both outcomes for
the same reason — one of `converged` or `CAPPED`, never omitted, because an absent marker reads as
the good news (#1952). This comment is the only place an open-but-not-blocking item is recorded
against the ticket, so an item omitted here is an item dropped.
