---
name: sigma-define
description: Open a new feature unit with branch, label, registry, and issues. Use when starting a new workstream or /sigma-define.
allowed-tools: Bash(python3 *), Bash(git *), Bash(gh label *), Bash(gh issue *), Bash(gh repo *), Read, Grep
---

## Codex path resolution

Codex has no `CLAUDE_SKILL_DIR`. In each command, replace `${CLAUDE_SKILL_DIR}` with the
absolute directory of this installed `SKILL.md` (shown in the skill catalog); never run an
empty path. Claude keeps its provided value.

# sigma-define

Detailed selection triggers: [selection](references/selection.md).

`docs/branching-model.md` lets a goal **belong** to a unit of work: it declares one, Sigma
resolves its base to `feature/<name>`, records it in the registry, and keeps that branch current.
Every part of that presupposes the unit already exists — and until this skill, **nothing created
one**. §14 documents opening a unit as four commands typed by hand, in a fixed order, spread across
`git`, `gh`, `mkdir` and an issue body, with the ordering requirement carried in a human's head.

`sigma-define` is that gesture. It is the counterpart to `/sigma-scope`: scope turns an idea into
issues on the branch you are already on; **define opens a branch for the idea first, and then puts
scope's issues inside it.**

---

## The nine steps, in this order

The order is **not** yours to optimise. See "Why the order is a requirement" below.

1. **Type** — `feature`, `bug` or `refactor`. Exactly three; ask, do not infer. The type is
   *metadata* (see below), so getting it wrong is cheap — but it is still the user's call.
2. **Name** — one git branch segment. Validated by `features._is_unit_name`, the same predicate
   every other consumer uses; never eyeball it.
3. **Branch** — `feature/<name>`, pushed. Remote is the truth, so the push is what makes the unit
   exist.
4. **Label** — `feature:<name>`.
5. **Registry** — `.sdlc/features/`, once per repository.
6. **Intake** — the details, in whatever form the person has them.
7. **Issues** — generated, each carrying **both** declarations.
8. **Assignment** — asked.
9. **Start now, or leave it on the board** — asked.

### Steps 1–5: `define.py open`

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/define.py" open .sdlc \
    --name <name> --type <feature|bug|refactor> [--base <ref>] [--remote <remote>]
```

One JSON line: `ok`, `unit`, `kind`, `branch`, `label`, `registry`, `steps` and `why`. `steps` is as
long as the gesture actually got — **a failing step stops the ones after it**, so the report names
where it stopped and the recovery differs per step (push access, label permissions, a wrong `.sdlc`
path). Read `why` back to the user verbatim; it is written for a person who just typed a name.

Base precedence is the loop's own: `--base`, else `work.base`, else the branch you are on.

Two outcomes need a human rather than a retry:

- **`refused` on `label`** — the repository already carries the same label in a different casing.
  Open the unit under the existing spelling, or rename that label first. Do **not** retry with a
  different casing; see the registry section for why two casings of one unit is a real cost.
- **`refused` on `branch`** — this checkout is not on a branch and no base was given. Name one.

### Step 6: intake — hand it to `sigma-scope`'s resolver

Do not write a second intake. `brainstorm.py` already handles all four forms — inline free text, a
local `.md` path, a fuzzy reference to an existing issue, and a direct `#N`:

```bash
python3 "${CLAUDE_SKILL_DIR}/../sigma-scope/scripts/brainstorm.py" .sdlc "<raw invocation text>"
```

Then follow **`skills/sigma-scope/SKILL.md` steps 2–6 exactly** — load the repo as context, dedup
against the board, confirm the target, ask the genuinely clarifying questions, draft the plan.
Everything that skill says about clarifying-questions judgment applies here unchanged.

### Step 7: stamp the plan, then compile it

`sigma-scope`'s `compile_plan` deliberately does **not** stamp (§10 note 4: "there is no calling goal
to inherit from — it is a human turning an idea into a plan"). This skill *is* that human, so it
supplies the unit before the plan is compiled:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/define.py" stamp \
    .sdlc/plans/scope/<slug>.plan.json --unit <name>
```

This rewrites every issue body **and the epic's** to carry the bare two-line marker, verified by
feeding it back through the real reader. `warnings` is non-empty only when a body could not be made
readable or already declared a rival unit — surface those to the user; do not paper over them.

Then compile exactly as `sigma-scope` step 6 does (`scope.py --plan ... --report ...`), and attach
the label half afterwards:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/define.py" declare .sdlc --unit <name> --issues <n,n,n>
```

`declare` attaches `feature:<name>` to each freshly-filed issue and then **reads each one back
through `features.read`**, reporting a per-issue outcome: `declared`, `case-drift`, `not-declared`,
`ambiguous`, `unreadable` or `failed`. Anything other than `declared` on every row is a finding to
report, not a detail to omit — a marker that landed somewhere unreadable produces no error anywhere
else in the system.

### After `declare` succeeds: priority (optional)

Once `declare`'s own per-issue report has been read back, ask one more question —
`AskUserQuestion` where the host has it, plain conversation otherwise (`sigma-dossier`'s own
convention for its one terminal question): **"Give `feature:<name>` a priority? (P0-P4, or skip)"**

- **A priority is given.** Record it on the unit's OWN registry entry — never on any member
  issue:
  ```bash
  python3 "${CLAUDE_SKILL_DIR}/scripts/define.py" set-priority .sdlc \
      --unit <name> --priority <P>
  ```
  This is the answer to "prioritise this feature": one value, read by the comparator
  (`feature_rank`) as a tie-break among issues that are already eligible, and it never touches a
  member issue's own `priority:` label — so nobody's individually-set tier is overwritten by
  answering this question. Report what the call returns (`ok`, `changed`, `written`).
- **Skipped, or no interactive host to ask.** Do nothing further. `set-priority` is only ever
  called on a real answer to this question — there is no other code path that reaches it — so
  skipping leaves this skill byte-identical to its behavior before this question existed.

Asked after `declare` for consistency with `sigma-goal-review`'s own feature-ification step, which
asks the identical question at the identical point in its own flow — not because `set-priority`
itself needs the member issues to exist; recording a value on the unit's registry entry never reads
them.

**Re-prioritising a unit that already exists doesn't need the nine steps at all.** If `<name>` is
already a known unit (`define.py`'s own registry read, matched case-insensitively — see
`_registered_unit`), skip straight to setting its priority instead of attempting steps 1–7 again:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/define.py" .sdlc <name> <P0..P4>
```

Three bare positional args, no `--unit`/`--priority` flags — this is what `/sigma-define <name>
<priority>` should route to when `<name>` already exists. It calls `set-priority`'s own logic
unchanged and prints the identical report shape; if `<name>` is NOT yet a unit it refuses with a
clear message pointing at the full `open`/`declare` flow rather than ever guessing at
branch/label/registry creation from two bare args (#2331).

A DIFFERENT verb, `bump-priority`, still exists for the different, still-legitimate job of
genuinely levelling a unit's members' own `priority:` labels to match by hand — idempotent,
human-invoked, and never called from this flow. It is not what this question asks for; see
`define.py`'s own module docstring if that is genuinely what you want.

### Steps 8–9: assignment and the execution path

`sigma-scope` step 7, unchanged — `assign.py resolve` for the options, then `assign.py execute` with
`file-and-stop` / `start-now-self` / `start-now-handoff`. Both are **questions**. Opening a unit
never starts work by itself.

### Step 0, if this repository has never carried a unit before

§14 requires every agent working a participating repository to know the no-direct-commits rule, and
an agent that has not been told it is the actor most likely to break it. Check, don't remember:

```bash
grep -Fq 'Nobody commits directly to a feature branch.' AGENTS.md CLAUDE.md 2>/dev/null \
    && echo "states the rule"
```

If it is absent, paste §14's verbatim block into whichever file this repository's agents actually
read, and track that edit on **that repository's** board — never as an untracked edit from here.

---

## Why the order is a requirement, not a suggestion

Declaring a unit in an issue body whose label does not yet exist causes the pick to be **refused**:
the goal keeps `sdlc:goal`, gains `sdlc:needs-label`, gets a flag comment, and is set aside until a
human acts. §14 measured it twice — and measured it on a repository with **no registry at all**,
because the label check never looks at `.sdlc/features/`. It is not an exotic case; it is what the
*natural* adoption order produces.

So: branch, then label, then registry, then declare. `define.py` enforces it structurally rather
than by prose — `_STEPS` is a constant, the executor is a loop over it, and a failing step returns
before the next one runs. That last part is the one an implementation gets wrong by trying to be
helpful: running all three and reporting failures at the end leaves a label minted for a branch that
does not exist (and **feature labels are never deleted**, so there is no cleanup path), or the
registry directory created — which arms the registry half for the *whole repository* — on the
strength of a gesture that did not complete.

## This is the one place a `feature:*` label may be created

The kit's standing rule is **attach, never create** (§7). Sigma attaches an existing feature
label and never mints one, because a single typo in a body marker would mint junk that outlives the
unit, and there is no deletion path. `sources.GitHubSource._run` enforces it at the single chokepoint
every `gh` call in that class passes through.

**`sigma-define` is the exception, and it is the only one.** The reason it is defensible here and
nowhere else: this is the *human's* gesture, made once per unit, against a name this skill has
already validated, *before* any issue body exists to contain a typo. A model whose units can only be
opened by hand is a model with half a life.

Two things follow, and neither is optional:

- **The rule is not weakened anywhere else.** Nothing in this skill touches `feature_labels.py`,
  `sources.py`, or the chokepoint. If you find yourself wanting a feature label created from any
  other skill, the answer is no — send the user here.
- **The create deliberately does not route through `GitHubSource`.** That class *refuses* the call
  and returns `""` — the **success** shape for a create. Routing through it would report a created
  label for one that does not exist, and every issue this flow then filed would be refused at its own
  pick. `tests/test_define.py` feeds this skill's own argv to the kit's own refusal predicate, so the
  exception cannot quietly stop describing the same call.

## The type is metadata, never a branch prefix

A `bug` unit's branch is `feature/<name>`. So is a `refactor` unit's. **There is no per-kind
branch prefix** — the kind never appears in a branch name at all — and inventing one would break the
model outright: base resolution matches on `features.BRANCH_PREFIX`, so a unit whose branch did not
carry it would be invisible to the one mechanism the unit exists for. The type survives where
metadata belongs — on the label's description, and in the issue prose.

## Both declarations, and the body one must be bare

Each issue carries the `feature:<name>` **label** *and* the two-line **body marker**:

```
Feature: <name>
Branch: feature/<name>
```

(Shown in a fence here, which is exactly why fenced content is never parsed.) The marker must be at
the **left margin, on its own line** — four spaces, a fence, an HTML comment, a `>` or a `-` each
make it declare nothing, silently. You never write it by hand: `define.py stamp` composes it through
`feature_stamp.stamp_body`, which builds it from the reader's own constants, feeds the candidate back
through `features.parse_body`, falls back to the top of the body when the natural placement does not
read back, and returns the body unstamped **with a warning** when no placement does.

The two halves are written by different means, and the asymmetry is deliberate (§10): the **body**
marker is composed in **before** creation, so it is atomic with the issue existing; the **label** is
attached **after**, because `gh issue create --label` fails the *whole create* when the label is
missing. Attaching afterwards degrades to `body_only` — recoverable — instead of losing the issue.

---

## The registry decision (#1576, and what #1638 still costs)

**Step 5 creates `.sdlc/features/`, and this skill is deliberately the thing that does it.** That was
gated behind **#1576** — "FIX LAST", because the directory's absence was the safety net keeping
**#1564**'s composition defects inert. Recording the reasoning, because it should not be re-derived
and it should not be assumed:

**Why it is defensible now.** #1564 named four adoption-blocking defects: the unreadable-shard
clobber, case-split units, the scope gate disarmed by a transport blip, and the scope-expansion gate
that could not fire in the case it was built for. **All four are fixed.** The condition #1576 states
for itself — *fix it last, because fixing it first arms all the others* — is satisfied: the others
are no longer armed.

**What #1638 left, and where it now stands.** It left three derived keys that folded a unit name
inconsistently. All three are now fixed, in one unit of work: `feature_propagate.sibling_path` —
the worst of them, which wrote `units/Voice.json` into a **sibling repository** whose own picks
write `units/voice.json` — by #1672, and `feature_doc.doc_path` and `feature_rebase.worktree_path`
by #1673. **Read that as the state of a measured set, not as a guarantee:**
`tests/test_feature_registry.py` discovers every derived unit key and fails on one nobody has
classified, and it is the authority. This paragraph is prose, and prose is what went stale three
times before the inventory existed.

It does not block step 5 because **it needs two spellings of one unit to reach it**, and that is
precisely what this skill removes. A unit opened here has one name, validated once, from which the
branch, the label and every issue's body marker are all *derived* — there is no second place for a
second spelling to originate. The defect's live precondition is a human typing the unit's name a
second time, differently.

**And the two doors that could still let one in are watched, so this is a check rather than an
argument:**

- `open_unit` refuses when the repository already carries the same label in a **different casing**,
  naming the existing spelling, instead of retrying into a second one.
- `declare` compares the unit read back to the unit opened **exactly**, not just case-insensitively,
  and reports `case-drift`. `features.read` folds case and returns the *body's* spelling, so a body
  saying `Voice-Interview` under `feature:voice-interview` is a legitimate `agree` — legitimate, and
  still two casings. This is the moment it would enter, so this is where it is named.

**What creating the directory actually turns on**, so an adopter is not surprised: base resolution,
the registry sync, the scope gate, the ownership gate, rebase upkeep, unit completion and the
cross-repo access check each open with the same `registry_dir(sdlc).is_dir()` test and return
`not-adopted` before spending anything. After step 5 they run. Measured in §6e: a goal declaring a
unit costs **3 gh calls** without the directory and **15** with it.

**Two consequences to tell the user**, both from §14:

- The directory is **empty**, so git will not carry it. It becomes real for everyone else the moment
  the first pick writes `units/<name>.json` and `<name>.md` into it **and those are committed**.
  A clone taken before that commit is unadopted again.
- **Goals picked before the directory existed are never backfilled.**

---

## Caveats

- **Nothing rolls back.** If `open_unit` stops at the label step, the branch is already pushed. That
  is recoverable and harmless (an empty feature branch costs nothing), but say so rather than
  implying the gesture was atomic.
- **`declare` needs a source with a label surface.** `LocalSource` has none — there is no label index
  over goal files — so in local-goals mode it degrades to `not-supported` and says so. The body
  marker still landed, which is the half that matters locally.
- **Issues are armed or parked at filing.** (Triage off: the legacy `sdlc:needs-confirmation`.) Step 9 decides the rest.
- **One unit per run.** Opening a second unit is a second run; the flow deliberately has no batch
  mode, because every step of it is a decision a person is making.
- **This skill does not merge anything.** Nothing merges a feature branch (§15) — landing the unit is
  still a human's call.
