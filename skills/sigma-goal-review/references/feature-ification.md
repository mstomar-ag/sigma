# Feature-ification — steps a through g

The detail behind step 5 of [`../SKILL.md`](../SKILL.md), once `<epic>` is resolved: asking (5a,
Product path only), naming and opening the unit (5b-5d), stamping the label (5e-5f), the
feature-priority ask, and the outcome comment templates (5g).

---

**a. Ask, exactly once — but ONLY on the Product path.** Check the path from step 1 first,
mechanically, never a guess about who's watching: on the **Tech-side retrofit path**, skip the ask entirely,
straight to DEFERRED below — unattended by definition, and feature-ification is product-tier, not
AGENTS.md's technical-decision stop (#2271).

On the **Product path** — a human ran `/sigma-goal-review` directly, before `/sigma-loop` is
involved — ask exactly once: *"Promote `<epic>` into a `feature:<name>` unit of work?"* —
`AskUserQuestion` where the host has it, else plain conversation (`sigma-dossier`'s convention).

Three answers reach step g (always runs):

- **Accept** (Product path only) → continue at b. Step g comments with its PROMOTION template.
- **Decline** (Product path only) → skip b–f, go to g's DECLINE template — differs from "never considered", told apart only by the comment.
- **DEFERRED** — reached two ways: the Retrofit path (always, per the check above), or Product
  path with no human available (rare) → treat it as decline; say DEFERRED, not declined, in g's note
  (`feature:*` has no deletion path, §14 — a name guessed unattended is worse than asked later).

On all three, step 6 continues, and `/sigma-define` remains available against `<epic>` by hand any
time — say so in the comment; it's the whole recovery path.

**b. On accept, fetch naming material — never re-author it:**
```
python3 "${CLAUDE_SKILL_DIR}/../sigma-scope/scripts/brainstorm.py" .sdlc "#<epic>"
```
`resolve_target` recognizes the bare issue reference as its direct-issue-number form and returns
`<epic>`'s own title+body with no free-text re-authoring (contract §9) — the same material a human
would read before typing a name at `sigma-define`'s own step 2.

**c. Ask two more questions, both pre-answerable, neither skipped** (`sigma-define`'s own steps 1-2):
**type** (`feature`/`bug`/`refactor` — exactly three, ask, never infer) and **name** (one git
branch segment; offer a slug of `<epic>`'s title from step b as a starting suggestion, but the
human's own answer is what gets used — `define.py open` validates it and reports why on a refusal,
so this step never re-implements that check itself).

**d. Open the unit:**
```
python3 "${CLAUDE_SKILL_DIR}/../sigma-define/scripts/define.py" open .sdlc \
    --name <name> --type <type>
```
No `--base` needed — `work.base` is `"main"` in this repo's own config, so the default precedence
(`--base`, else `work.base`, else the current branch) already resolves to `main` regardless of
which branch this pick happens to be running on. On anything other than `"ok": true`, read `why`
back verbatim and stop — a `refused` on `label` or `branch` needs a human's next move, never a
silent retry (`sigma-define`'s own SKILL.md).

**e. Confirm the label exists, THEN stamp the body marker onto every already-filed ticket.**

```
gh auth switch --user <the account that owns the board> && gh auth status --active
gh label list --search "feature:<name>" --json name --jq '.[].name'
```
Step d's `define.py open` creates that label and reports `refused` if it could not, so on a clean
run this is a re-check rather than the only check — but the body marker is the half that does NOT
self-heal. `AGENTS.md` is explicit: an issue body declaring a unit whose `feature:` label does not
exist sets the goal aside and writes `sdlc:needs-label` **on any repository, adopted or not**. Stamp
first and find out afterwards, and you have set aside every ticket in this pass at once. If the
label is not listed, stop and report it — do not stamp, and do not create the label by hand here
(`define.py open` owns that).

Then stamp `<epic>` and, if 4b ran, every child in `report["issues"]` — their numbers are already
known from 4b's own report file. `define.py stamp` does **not** apply here: it rewrites a plan
JSON's own bodies *before* `compile_plan.py` files them, and by this point every one of these tickets already exists — filed
by 4b, or (on the Product path) by `sigma-dossier` itself. Use the already-tested `append_to_body`
primitive instead, the same one `compile_plan.py`'s own `Tracks #N`/`Part of epic #N` markers use,
via the import-only `python3 -c` pattern `skills/sigma-wizard/SKILL.md` already establishes for a
function with no CLI wrapper of its own:
```
python3 -c "
import sys
sys.path.insert(0, '${CLAUDE_SKILL_DIR}/../sigma-loop/scripts')
import state, sources
config = state.load_config('.sdlc')
src = sources.get_source('.sdlc', config)
marker = 'Feature: <name>\n' 'Branch: feature/<name>'
for n in ('<epic>', '<child1>', '<child2>'):   # every ticket from this pass, epic first
    with sources.hsg.filing_park(n):
        src.append_to_body(str(n), marker)
    print('stamped #%s' % n)
"
```
The append runs inside `filing_park` because an epic or Dossier ticket carries only the `epic` label, which the hard-stop guard does not count as Sigma's own, so with hard stops on the bare append raises; these tickets were just filed by this pass, the same scope `compile_plan.py` uses for its epic patch, and a foreign issue outside the scope is still refused.

The two lines are written literally — `docs/branching-model.md`'s stable, public marker format
(bare, no fence, no leading indent, its own line) — no import of `feature_stamp.py` needed for text
this pinned and this short.

**f. Declare the label, on every ticket, in one call — and verify:**
```
python3 "${CLAUDE_SKILL_DIR}/../sigma-define/scripts/define.py" declare .sdlc \
    --unit <name> --issues <epic>,<child1>,<child2>
```
Explicit on **every** ticket, not just the ones that would eventually self-heal:
`feature_labels.attach_at_pick` would attach the label on any stamped CHILD the first time
`sigma-loop` picks it (it reads exactly the body-only state step e just produced), but `<epic>`
itself never carries `sdlc:goal` and is therefore never picked — `attach_at_pick` never runs for
it. Rather than leaving the epic and its children in two different eventual-vs-immediate states,
declare all of them together now; `declare` already accepts a list, so this costs nothing extra.
Report anything other than `declared` per row as a finding, exactly as `sigma-define`'s own SKILL.md
documents — most likely `not-supported` on a `LocalSource`-backed repo (no label surface; the body
marker from step e still landed there, which is the half that matters locally).

**Then, once `declare` reports `declared` on every row, ask about feature priority** — the identical
question `/sigma-define` asks at its own `declare` step, in the same wording: *"Give
`feature:<name>` a priority? (P0-P4, or skip)"* — `AskUserQuestion` where the host has it, plain
conversation otherwise, step a's own convention. A real answer calls the identical verb
`/sigma-define` calls, on the same terms:
```
python3 "${CLAUDE_SKILL_DIR}/../sigma-define/scripts/define.py" set-priority .sdlc \
    --unit <name> --priority <P>
```
Records the value on `<name>`'s own registry entry, read by the comparator as a tie-break — never
a write to `<epic>`'s or any child's own `priority:` label, so it needs nothing from step f beyond
the unit itself existing; asked here only to match `/sigma-define`'s own placement. Report what it
returns (`ok`/`changed`/`written`). Skipping the question — or no interactive host to ask — calls
nothing further: the rest of this step and step g below run byte-identical to today's
feature-ification flow.

**g. Comment the outcome on `<epic>` — on every one of step a's three answers.** A decline is a
DECISION about `<epic>`, and an undecided ticket and a deliberately-not-promoted one look identical
without it; the unattended case is worse still, because the question has to be re-asked and nothing
else records that it was ever raised. One `note` call, one of three templates:

*On accept, after f:*
```
python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/loop.py" note .sdlc <epic> \
    "feature-ification: promoted to feature:<name> (branch feature/<name>) -- <declare outcome>"
```

*On an explicit human decline:*
```
python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/loop.py" note .sdlc <epic> \
    "feature-ification: declined -- asked and declined; <epic> stays on work.base. \
Re-open the question any time with /sigma-define against #<epic>."
```

*On no human available (an unattended pick) — DEFERRED, not declined on the merits:*
```
python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/loop.py" note .sdlc <epic> \
    "feature-ification: deferred -- no human available on this pick, so nothing was promoted. \
A feature:* label has no deletion path (docs/branching-model.md §14), so the name was not guessed. \
Run /sigma-define against #<epic> to decide it."
```

Nothing else in b–f runs on either decline path, so this comment is the entire output of step 5 in
those two cases.
