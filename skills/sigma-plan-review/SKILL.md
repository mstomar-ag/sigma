---
name: sigma-plan-review
description: Adversarially verify a plan against code and post-ship risks before implementation. Use in Plan-Review or /sigma-plan-review.
---

## Codex path resolution

Codex has no `CLAUDE_SKILL_DIR`. In each command, replace `${CLAUDE_SKILL_DIR}` with the
absolute directory of this installed `SKILL.md` (shown in the skill catalog); never run an
empty path. Claude keeps its provided value.

# sigma-plan-review

Detailed selection triggers: [selection](references/selection.md).

## Context-efficient inspection

Locate before opening: use `rg --files` for paths and `rg -n` for symbols or claims.
Read only the relevant lines with a bounded range; expand outward only when the
boundary is unclear. Do not dump whole files or broad search output into context.
For bulky tests or diagnostics, save full output to a scratch file, then inspect
its exit status and focused matches or a bounded tail. Do not hide failures, and
keep secrets out of the scratch file. This is guidance, not a measured savings claim.

The last gate before code. Review the active plan with two lenses; finish with one verdict.

**The reviewer must not be the author** — and that is a property of how this review is RUN, not a
claim you can make about yourself. Resolve it before judging anything:

`python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/reviewer.py" resolve .sdlc`

It prints one JSON object; branch on `mechanism`:
- **`subagent`** — assemble the brief with `review_context.py brief .sdlc "<goal>" --for plan-review
  [--artifact <path|PR#>]`, hand a FRESH subagent **only that**, and let it review. You do not.
- **`process`** / **`command`** — run the checked brief with `reviewer.py run` below. It invokes the
  resolved command in a fresh process and enforces `review.timeout_seconds`.
- **`inline`** — no mechanism is available on this machine. Review inline, and say so in the verdict:
  you are not independent, and asserting otherwise does not make you so.

**If a resolved `process`/`command` FAILS** — unauthenticated, non-zero exit, timeout, empty output —
it does **not** silently become an inline self-review. Fall back to inline, stamp the provenance
accordingly, and name the failure in the verdict.

**If this brief already reached you, you ARE the resolved reviewer — do not re-resolve and do not
dispatch again.** Resolution happens once, at the site that spawns the review.

Whatever the mechanism, the brief is the reviewer's ONLY input. `brief` prints to stdout, so redirect
it and check the file, naming the scratch directory the maker can write:

```
python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/review_context.py" brief .sdlc "<goal>" \
  --for plan-review > /tmp/brief-<goal>.md
python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/reviewer.py" check /tmp/brief-<goal>.md \
  --scratch "$(pwd)/.sdlc/work/<goal>"
```

For **`process`** / **`command`** only, execute the checked brief. `run` repeats the leak check on
the exact text it sends, stops the process group on timeout, and rejects non-zero or empty output:

```
python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/reviewer.py" run .sdlc /tmp/brief-<goal>.md \
  --scratch "$(pwd)/.sdlc/work/<goal>"
```

It exits non-zero if the brief NAMES a shared scratch path — a file the maker can write and the
reviewer would read is a maker→reviewer channel whatever the artifact is. It deliberately does **not**
flag implementation content: a breaker must never see the implementation, but a reviewer must — the
diff is the artifact under review.

A maker reviewing its own plan just confirms it. You
have full read access to the repo: use it. The diff-to-come is the change; the codebase is the impact
surface.

## 1. Forensic verification
Every claim in the plan is a hypothesis. For each file path, function, line, or behavior the plan
asserts — open the real code and confirm it. Classify each: Correct / Partially correct / Incorrect,
each backed by a `file:line`. "The plan says X" is not evidence; the file showing X is.

## 2. Adversarial robustness (assume it ships and a bug surfaces in two weeks)
- **Caller sites (trace the blast radius across the WHOLE repo):** for every function/contract the plan changes, grep *all* callers — not just the files the plan names. You have full repo access; a plan that lists three files it touches has a blast radius of every site that calls them. Are they all handled, or is this a one-site patch with broken siblings?
- **Regression risk:** what working behavior could break? Name the test that would catch it, or flag the gap.
- **Negative scenarios:** empty/null input, stale/partial state, concurrent/out-of-order, boundary sizes. Which defeat the plan?
- **Loopholes:** where can invalid state enter without hitting the new guard (defaults, alternate code paths, deserialization, trust boundaries)?

## 3. Scope & fit
Does each step serve the goal? What's over-built (YAGNI)? Does it contradict the project's own
rules, conventions, or stated direction? Quote the specific rule if so.

## 4. Strategic & architectural alignment (vision-first projects)
**Never test for the file yourself.** `.sdlc/` is gitignored, so inside a goal worktree — where the
loop runs this phase — `.sdlc/context/north-star.md` does not exist even on a project that has one
(#1778: 11,523 bytes in the main checkout, no `context/` dir at all in the worktree). Ask instead:

```
python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/north_star.py"
```

One line, first token the verdict. Branch on it — the three are **not** interchangeable:

- **`present <path>`** — read THAT path and hold the plan to it on two axes:
  - **Strategy / Non-goals** — does it serve a stated priority? Does any step **advance a declared
    non-goal** or **contradict the strategy**?
  - **Architecture rules** — does any step **violate a numbered architecture rule** (layering,
    dependency direction, module boundaries, "where new code goes")?

  A plan that fights the north-star — its strategy, a non-goal, or an architecture rule — is
  **FIX-FIRST**; quote the line it violates.
- **`absent <path>`** — a genuine drop-in project: skip this check, it really is a no-op.
- **`unreachable <why>`** — neither of the above, and never a silent skip. The north-star could not
  be located, so **say in the verdict that strategic alignment was not judged, and why**, then judge
  correctness alone. A check that could not look reads ABSENT, never PASS.

Other reference documents come from the repo's `context.documents` key and arrive in the brief,
each inlined or given as a pointer; a document the brief says was not read or not found is a stated gap.

## Verdict
Open with **how this review was run** — `Reviewed by: dispatched subagent | fresh process (<host>) |
operator command | inline, author self-review`. Then one of: **SOUND** (implement as-is) /
**SOUND-WITH-REFINEMENTS** (list them) / **FIX-FIRST**
(blocking issues). Be specific and opinionated; don't pad with praise. If you didn't try to break
it, you didn't review it.

**Record it** — the site that dispatched this review does, never the dispatched reviewer; from the
main checkout; before anything edits the plan. It READS the brief file written at dispatch above,
never a rebuilt one (a rebuild hashes the edited plan, so the check could never fail):

```
python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/work.py" record-plan-review .sdlc "<goal>" \
  --verdict SOUND|SOUND-WITH-REFINEMENTS|FIX-FIRST \
  --plan-sha256 "$(awk '/^Plan sha256: /{print $3; exit}' /tmp/brief-<goal>.md)"
```

It records SOUND → `pass`, SOUND-WITH-REFINEMENTS → `warn`, FIX-FIRST → `block` (a FIX-FIRST is
recorded too), and it is the only emitter of the `plan_review` journal gate: do not also `loop.py
emit` one. It refuses, writing nothing, when the plan no longer hashes to that `Plan sha256:` line.
With `gates.plan_review.enabled`, `work.py pr` refuses to push unless an approving verdict (SOUND or
SOUND-WITH-REFINEMENTS) is recorded for the exact bytes of the plan on the branch.

**SOUND-WITH-REFINEMENTS:** record the verdict as returned, BEFORE editing the plan. Refinements
carried into Implement without editing the plan file stay inside the approval (note each
disposition with `loop.py note`). Edit the plan FILE and the approval no longer covers it: run a
fresh plan-review of the refined plan and record that verdict.

The record is agent-written: it proves a verdict was recorded for these bytes, not that an
independent reviewer produced it. Without a work record (`/sigma-goal` never runs `work.py start`)
it keeps no file, still mirrors the verdict to the journal, exits 0 and prints a note; with
`gates.plan_review` on, `pr` then refuses unless the verdict is recorded from the main checkout
that ran `work.py start`.

## 5. Disposition — closing the loop on a FIX-FIRST
A verdict that sends the plan back is only half the gate. When the revised plan returns, **give every
finding an explicit disposition** — otherwise the loop either swallows findings silently or obeys a
wrong one. Each disposition needs its own `file:line`; "the review seems right" is not evidence.

- **Accept** — the finding is real. Confirm it in the code first, then change the plan and cite what
  confirmed it.
- **Reject** — the finding is wrong or reads stale code. Confirm the *plan* was right, keep it
  unchanged, and record why with evidence.
- **Partially accept** — the concern is real but the suggested fix isn't. Adapt it; cite both the
  concern and why the adjusted approach is better.

**The review can also be wrong.** It is a hypothesis exactly like the plan was in §1. A finding that
claims a file or function doesn't exist gets checked against the filesystem before it is accepted —
the reviewer misreads code too, and a plan patched to satisfy a false finding is worse than the
original.

**Structural over patchwork.** If a finding proposes a one-site patch where a shared fix is feasible,
reject the approach and propose the structural one — the same root-cause discipline §2's caller-site
sweep applies. If patchwork is genuinely the right scope, say so explicitly and leave a tracked item
for the structural fix; never let it pass unnamed.

**Regen threshold.** If more than half the findings are substantive (not surface corrections like a
stale line number or a renamed symbol), the plan has structural problems — say so and recommend
regenerating it from the goal rather than patching. Counting matters here: three real defects in five
findings is a different situation from three in twenty.

Carry the dispositions with the revised plan so the next review round starts from what was already
settled. When a later round contradicts an earlier decision, re-read the code, pick one direction on
the evidence, and record the reversal — don't flip-flop.

**Under `/sigma-loop`, a revision the current model tier cannot finish is escalated, not parked** —
token budget and tier size are not park reasons (#2828). Run
`python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/loop.py" escalate .sdlc "$goal" <tier> --after plan-review`
and re-dispatch the plan revision at the tier its `ESCALATE <next>` names; on `CEILING`/`OFF` the
revision loop above continues at the current tier. Interactive runs have a human at every send-back.

Record the rejections in the goal's audit trail —
Run `python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/loop.py" note .sdlc "<goal>" - <<'SIGMA_NOTE'` with body `plan-review: <what was rejected and why>` (stdin, quoted heredoc; #713).
— which comments the issue in github mode and appends to `.sdlc/journey/` locally. A *rejected*
finding is the one worth writing down: the accepted ones are visible in the revised plan, while the
reasoning for overruling a reviewer exists nowhere else, and it is the first thing anyone asks when
the same objection comes back a month later.

> On Claude with the companion installed, `superpowers:receiving-code-review` applies this same
> "verify before you agree" discipline to code review; this is its plan-stage equivalent.
