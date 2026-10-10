---
name: sigma-research
description: Map a goal's code blast radius, existing debt, and size into a short dossier. Use in Research or /sigma-research.
allowed-tools: Bash(python3 *), Bash(git *), Bash(grep *), Bash(rg *)
---

## Codex path resolution

Codex has no `CLAUDE_SKILL_DIR`. In each command, replace `${CLAUDE_SKILL_DIR}` with the
absolute directory of this installed `SKILL.md` (shown in the skill catalog); never run an
empty path. Claude keeps its provided value.

# sigma-research

Detailed selection triggers: [selection](references/selection.md).

## Context-efficient inspection

Locate before opening: use `rg --files` for paths and `rg -n` for symbols or claims.
Read only the relevant lines with a bounded range; expand outward only when the
boundary is unclear. Do not dump whole files or broad search output into context.
For bulky tests or diagnostics, save full output to a scratch file, then inspect
its exit status and focused matches or a bounded tail. Do not hide failures, and
keep secrets out of the scratch file. This is guidance, not a measured savings claim.

The **Research** phase executor — the step between restating a goal and planning it. A goal states an
intention; this grounds it in the code: what the change will actually touch, what debt already lives
there, and how much ceremony it earns.

## Executor resolution (host-aware)
No `superpowers` / `code-review` companion covers blast-radius research, so this is **always
Sigma's own** (same as `sigma-retro`). Pure `grep` / `git` / markdown discipline — it degrades
gracefully on any host.

**Read-only.** It never edits code, plans, or standing docs. The dossier and the questions it raises
are the only outputs — except the one-line `lane:` write-back in step 5.

## Proportion first
Research is a cost, not a virtue. Before starting, ask whether this goal earns it: a typo fix, a
rename, or a one-file change does **not** — record a one-line radius in the goal's journey note and go
straight to Plan. Run the full pass when the goal touches a contract, crosses a module boundary,
changes shared state, or you cannot yet name every file it will touch. Skipping it is a legitimate
outcome; say so explicitly rather than producing a ceremonial dossier.

## 1. Seed
Start from the goal text (`.sdlc/goals/NNNN-*.md` or the issue body) plus, if the knowledge graph is
on, the `sigma-context` brief — prior art beats re-derivation. Pull the nouns out of the goal: the
modules, functions, fields, endpoints, commands, and behaviors it implies. Those are your seeds.

If the goal already names files or proposes a design, treat every claim as a **hypothesis to verify**,
not a fact to copy. Mark each confirmed / corrected / not-found with the evidence.

## 2. Blast radius — and record the query
For each seed, sweep the codebase for every site that touches it: definition sites, call sites, tests,
config, docs, and serialized/persisted shapes. Watch for naming boundaries the repo actually has (a
field spelled one way in one language and another way across a serialization boundary is the classic
missed site).

**Write down the exact commands you ran, verbatim, in the dossier.** This is the point of the step:
coverage is guaranteed by *re-running the query at review time*, not by trusting that today's list was
complete. Sites that land after research are caught by the re-scan; sites that were never queried are
caught by nothing.

One row per site, each with a disposition:
- **in-scope** — this goal changes it
- **deferred** — real, but a separate follow-up (say why, and leave a tracked item)
- **out-of-scope** — adjacent but deliberately untouched (say why)
- **verify** — unsure whether it's affected; list it rather than dropping it

**Reviving something?** If the goal re-adds a capability that once existed, search history too
(`git log --all --oneline -- <path>`, `git log -S"<symbol>"`). Deleted code is both a reference for the
real contract and a constraint — the reason it was removed usually still applies.

**Anticipate conditional risk.** If the radius touches a surface that earns a dedicated review beyond
code quality — auth/PII, a public API or exported contract, or a DB migration — record in the dossier
that Review must run the matching skill: `sensitive` → `/sigma-security-review`, `contract` →
`/sigma-contract-check`, `migration` → `/sigma-migration-check`. The `risk-detect.sh` collector confirms it
against the real diff at Review (§Review wires it); here you flag it so the Plan budgets for it.

## 3. Tech debt already in the radius
Within the blast radius only — this is not a whole-repo audit — find the debt the goal is about to
build on: duplicated sources of truth that disagree, hard-coded assumptions, dead branches, swallowed
errors, declared-but-unenforced constraints. Per item recommend **reconcile-now** (default — carrying a
known structural defect through a change usually costs more than fixing it), **defer-with-ticket**
(out of reach, but tracked — never silent), or **out-of-scope**.

You propose; the user owns the scope call.

## 4. Constraints and unknowns
- **Fast-moving surfaces are verified, not recalled.** If the radius touches a dependency that moves
  faster than training data — a framework's current major version, a provider's live model registry,
  a recently-rewritten API — check it against the current official source and cite what you checked.
  An unverified claim is written down as **unverified**, not guessed around.
- **Open questions** — split them: **blocking** (the plan cannot be written until this is settled;
  cite the real tension) and **non-blocking** (a scope or design call for planning). Don't pad the
  blocking list; a blocking question cites a specific conflict, not a vague worry.
- **North-star early warning** — **resolve it, never stat it.** `.sdlc/` is gitignored, so inside the
  goal worktree this phase runs in, `.sdlc/context/north-star.md` is missing even on a project that
  has one (#1778). Run `python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/north_star.py"` and branch
  on its first token: on **`present <path>`** read that path, and if the radius appears to trip a
  numbered architecture rule or a declared non-goal, say so *now*, while it's cheap, and treat it as
  a lane-escalating signal. **`absent`** is a drop-in project — skip. **`unreachable`** is neither:
  record it in the dossier as an input you could not read, never as clear. The binding gate is still
  `sigma-plan-review` §4 — this is the early warning, not a second verdict.
  Further reference documents a repo names under `context.documents` in its config also ride in the
  reviewer brief; read those the brief points to.

## 5. Lane — size it from what you found
Classify the goal, then **record the lane where this project's backlog actually lives**:
- **local goals** — edit the goal file's frontmatter (`lane: auto` → the lane you chose). This is what
  `auto` means: the engine sizes the goal once research has measured it.
- **github goals** — the goal is an issue and has no frontmatter, so the lane goes in the phase note
  below, where every other phase records. Say it in the first line so it's visible without expanding
  the timeline.

- **small** — fits the existing structure, few files, one module. Skip the heavy ceremony: plan
  briefly and go.
- **medium** — fits the existing structure but spans multiple modules or a contract between them. The
  normal full pass.
- **large** — introduces new structure: a new source of truth, a new boundary or layer, a contract
  change with many callers, or anything that contradicts a numbered architecture rule. Earns extra
  design work before planning, and is a candidate for splitting into several goals.

**Size by structural footprint, not by guessed calendar time.** The lane answers "how much ceremony
does this earn?" — justify it with the counts you actually measured (sites touched, modules crossed,
whether a new source of truth appears), never with an invented duration. If a calendar figure is
genuinely wanted, `/sigma-velocity` derives one from real throughput; do not fabricate one here.

The lane is a *starting* call. If implementation reveals the goal is bigger, escalate it and say so.

**Who reads this.** Both orchestrators route on it after Research — `/sigma-goal` under *Lane routing*,
`/sigma-loop` in its per-goal step, each resolving it with `discovery.py lane "<goal>"`. So the lane you
write here decides how much ceremony the remaining phases apply; it is not a label. Plan-Review is the
one phase that runs in full at every lane.

## Output
Write `.sdlc/research/<goal-slug>.md` (create the directory on first use):

```markdown
# Research: <goal title>
**Goal**: <path or #issue> · **Lane**: small | medium | large · **Date**: <YYYY-MM-DD>

## Goal in one paragraph
## Claim verification (only if the goal proposed a design)
| Claim | Verdict | Evidence (file:line, or a line range) |
| --- | --- | --- |

## Blast radius
**Queries (re-run these at Review):**
- `<the exact command>`

| ID | Site (file:line, or a line range) | What it is | Why affected | Disposition |
| --- | --- | --- | --- | --- |
| BR-1 | … | … | … | in-scope / deferred / out-of-scope / verify |

## Tech debt in the radius
| ID | Debt | Evidence (file:line, or a line range) | Recommendation |
| --- | --- | --- | --- |

## Constraints & unknowns
- Unverified: <claim + why it could not be confirmed>
- Blocking: <question + the conflict it cites>
- Non-blocking: <question>

## Lane rationale
<the measured footprint that justifies it>
```

The `| --- |` rows are part of the skeleton, not decoration — copy a table without one and it
renders on GitHub as a run of literal pipes, not a table. And a cited location is a `file:line`
**or a line range** (`models.py:8-13`); most real ones are ranges, in every column that asks for
one.

Then record the phase note, which is what puts research in the audit trail:
Run `python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/loop.py" note .sdlc "<goal>" - <<'SIGMA_NOTE'` with body `research: <summary>` (stdin, quoted heredoc; #713).

The note routes itself — it comments the **issue timeline** in github mode and appends to
`.sdlc/journey/` in local mode — so make it carry what a *teammate* needs without opening your working
copy: **the lane, the site count, and every blocking question.** The full dossier stays a local file
either way (it's a working artifact, like the radar digest); the note is what makes it visible on a
shared backlog. In github mode a research pass that leaves no comment is invisible to everyone but you.

Keep the dossier out of `.sdlc/plans/` — the plan gate treats any recent file there as a fresh plan,
and a research note is not a plan.

## Constraints (non-negotiable)
- **No fabrication.** Every row cites a real `file:line`. If you can't cite it, drop it. A
  confident-but-wrong dossier is worse than a thin one, because Plan and Plan-Review both trust it.
- **Store the queries verbatim.** The coverage guarantee is the re-scan; without the query there is
  no re-scan.
- **Surface, don't resolve.** Tensions and blocking questions go to the user. Don't pick a
  reconciliation direction yourself.
- **Exhaustive over neat.** A site you were unsure about belongs in the table marked `verify`, not
  left out for tidiness. Missing a site here is the one failure later phases cannot catch.

## Snapshot
```
[research ✓] <N> sites (BR-1..N) · <M> debt items · lane: <lane>
Blocking: <K> — <the questions, or "none">
Wrote: .sdlc/research/<goal-slug>.md
```
