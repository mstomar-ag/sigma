---
name: sigma-model
description: Predict a goal's model tier for phase dispatch. Use for model selection or /sigma-model.
allowed-tools: Bash(python3 *)
---

## Codex path resolution

Codex has no `CLAUDE_SKILL_DIR`. In each command, replace `${CLAUDE_SKILL_DIR}` with the
absolute directory of this installed `SKILL.md` (shown in the skill catalog); never run an
empty path. Claude keeps its provided value.

# sigma-model

Detailed selection triggers: [selection](references/selection.md).

These four tiers are shared ledger values, not universal model names. Sigma first selects one
**canonical portable effort** with the tier; Claude receives that pair directly, while Codex
translates only the tier into a real model ID. Before every Codex dispatch, preserve the selected
effort when resolving the host model:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/predict.py" host-model codex "<tier-or-off>" .sdlc --effort "<effort>"
```

It prints `model=<Codex model ID> effort=<low|medium|high>`. Pass those exact values to the Codex
subagent call and pass the model ID to `phase_report.py start --host-model`. It refuses an unknown
host, tier, effort, or malformed configured override rather than passing a portable tier such as
`sonnet` through as a Codex model ID. The current model defaults are `haiku` → `gpt-5.6-luna`,
`sonnet` → `gpt-5.6-terra`, and `opus`/`fable` → `gpt-6-astra`; the requested effort remains the
canonical portable effort, never the model mapping's old default. An operator can replace any
default in `model_host_overrides.codex` with another approved Codex ID from this plugin release
(`gpt-5.5`, `gpt-5.6-luna`, `gpt-5.6-sol`, `gpt-5.6-terra`, or `gpt-6-astra`). A host catalog change
needs a plugin update; an override never admits an arbitrary or Claude model string.

Match the model to the work. A one-line rename doesn't need Opus; a schema migration shouldn't run on
Haiku. This predicts the tier **once from the goal** — then the goal's phases run at that tier (the
design: "the rest of the steps will be executed with that model").

## Recommend a tier for a goal

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/predict.py" "<goal text>"     # or a .sdlc/goals/NNNN-*.md path
```

Prints one of `haiku | sonnet | opus | fable`:

| Tier | When | Signals |
|------|------|---------|
| **opus** | hard / risky / high blast-radius | migrate, architecture, security, auth, concurrency, performance, breaking change, payments |
| **fable** | creative / writing-heavy — **opt-in, see the price ceiling below** | vision, narrative, storytelling, blog, marketing copy |
| **haiku** | trivial / mechanical — **title only** | typo, rename, whitespace, reformat, docstring, dead code |
| **sonnet** | everything else (default) | ordinary implementation |

Deterministic (regex over the goal text — no LLM, no cost, no drift). Conflicts resolve **upward**:
"fix the typo in the security module" → `opus`, because under-powering a hard goal costs more than
over-powering a trivial one.

**A haiku signal counts only in the title (#2827).** The title is the text's first line (for a goal
file path: its frontmatter `title:`, else its file stem). A real issue body almost always mentions a
comment, a docstring or a lint, so a haiku stem found only in the body is ignored and the goal gets
the `sonnet` default. `resolve-step` is unchanged: a mechanical step's downgrade is what it is for.

**An opus or fable signal found only in the body needs two hits (#390).** A title hit still wins on
one. In the body, one mention is usually a citation (a doc path, a test path, a `feature/...` branch,
"authorization" meaning spend approval, a JSON key, a shipped migration recalled in a retro), not a
description of the work, so the goal falls through to the `sonnet` default; two or more occurrences
of the stem (or of any opus/fable stem — occurrences, not distinct words) win. A match lying wholly
inside a backtick code span, a fenced block or a path-shaped token (`\S+/\S+`, so `a/b` and
`and/or` count as paths) never counts, in the title or the body. For scale, and as the predecessor's
measurement on its own two boards, not Sigma's (Sigma's boards have not been measured): 61 of 167
open goals routed to opus, 58 of those 61 from one body word, and 16 of 21 hand-read cases were
false positives. `why` says where the signal was found and how many hits backed it —
`model=haiku in=title hits=1 signal=typo`, `model=opus in=body hits=2 signal=migrat` — and the
signal stays the text after `signal=`.

### The price ceiling (`model_selection_max_tier`, default `opus`)

Upward resolution assumes over-powering is cheap. That is true of `opus` at 2.5x sonnet and **false
of `fable`, which is priced at 5x** — so a single creative stem anywhere in a goal's title or body
used to promote a typo fix to the most expensive tier, and in #2564 an unattended overnight run
exhausted an account's credits that way.

**By default `fable` is therefore unreachable.** When the winning tier is priced above the ceiling,
the tier clamps to `sonnet` and **the signal that fired is still reported**, so `why` still tells you
what happened:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/predict.py" why "Draft the launch blog series"
model=sonnet in=title hits=1 signal=blog  # `blog` fired; the tier was capped
```

A repo whose work genuinely is creative writing opts the ceiling up, in `.sdlc/config.json`:

```json
{ "model_selection": "auto", "model_selection_max_tier": "fable" }
```

Any of `haiku | sonnet | opus | fable`; anything else is a typo and falls back to `opus` rather than
silently uncapping. Lowering it below `sonnet` also binds the default tier, so `haiku` runs
everything on the cheapest tier — including goals the router rated `opus`.

## Automatic selection in the loop (config-gated)

`.sdlc/config.json` → `model_selection`:
- `"off"` (default) — portable tier prediction is disabled. Inline work uses the session model;
  before every dispatched Codex phase, pass `off` to `host-model`, which selects the versioned
  ordinary-work Codex mapping rather than inheriting a parent session's model.
- `"auto"` — `/sigma-loop` predicts a tier per goal and runs that goal's phases at it.

**A `model:*` label on the issue is not an input to any of this.** The prediction reads the goal's
*text*; the gate is the config key above. Sigma defines no `model:*` values and reads none — a
`model:opus` label is an annotation `/sigma-triage` will display, never a setting that runs the goal
on Opus. See [the label model](../../docs/label-model.md), §12.

The backward-compatible tier-only gesture remains:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/predict.py" resolve "<goal>" .sdlc   # prints a tier, or "off"
```

For a dispatch, use the canonical pair instead so Claude and Codex receive the same selection:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/predict.py" resolve-profile "<goal>" .sdlc
# model=<tier-or-off> effort=<low|medium|high>
```

For a GitHub issue number, pass its actual title and body as the first argument — **the title on
the first line**, e.g. from
`gh api repos/{owner}/{repo}/issues/N --jq '.title + "\n\n" + (.body // "")'`
(POSIX shell, run by the calling skill; `--jq` puts the title first) — and the issue
number as a final argument: `python3 "${CLAUDE_SKILL_DIR}/scripts/predict.py" resolve
"<title-then-body text>" .sdlc "<issue-number>"`. This classifies the text while recording
`model_choice` under the issue ID. The three-argument local-file form above is unchanged.

### When a signal is your domain vocabulary, not a judgement (issue #1601)

The signals are fixed regexes, and a few of them are creative terms on most repos but ordinary
**domain nouns** on some: a storytelling product has a *narrative* stage and a *prose* renderer, and
`vision` collides with Sigma's own vision-first vocabulary — `"Align the retry logic with the
north-star vision doc"` routed to `fable` on the word `vision` alone. Under `model_selection: auto`
that is not cosmetic: the tier is the goal's ceiling and each phase runs as a subagent at it.

Narrowing the global pattern (what #350 did for bare `story`) cannot fix this, because those terms
really are creative on other repos. So the opt-out is **per repo** — `.sdlc/config.json`:

```json
"model_selection_signal_excludes": ["narrativ", "vision"]
```

Find the values by asking the router itself, and copy the literal it prints:

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/predict.py" why "Align the retry logic with the north-star vision doc"
model=sonnet in=title hits=1 signal=vision  # -> add "vision" (tier capped; copy what follows signal=)
```

What it keys on, and what that buys:

- an entry names a **signal** (the matched literal), not a word in the goal and not a phrase — so
  every wording of your domain noun is covered, not just the one that misrouted;
- matching is case-insensitive and either-direction, so the natural word (`narrative`) works as well
  as the router's stem (`narrativ`), and one stem covers the prefixed forms a pattern admits;
- an excluded signal **falls through** to the rest of that tier and then the lower tiers, so the
  tier is not switched off: a genuinely creative goal still reaches the tier via `storytell`,
  `blog`, `marketing copy` or `tagline`. Note that reaching `fable` *also* requires the price
  ceiling above to be opted up — the two knobs are independent, and on stock config the ceiling is
  what decides;
- the accepted cost: an excluded signal is excluded repo-wide, including where you *did* mean it
  creatively. That is the trade — it is opt-in, and declared by the person who knows the vocabulary.

Empty by default, so a repo that configures nothing routes byte-identically to before. `why` and the
bare `predict.py "<goal>"` form both read the same key (each takes an optional trailing `sdlc_dir`),
so the recommendation you are shown is the one the loop will act on.

**Why per-phase subagents, one tier per goal:** the main session cannot switch its own model mid-run,
but work run as a **subagent** can take a `model` override. So under `auto`, `/sigma-loop` runs **each of
the goal's phases as its own subagent** at the predicted tier (the design: "the rest of the steps run
with that model") — one subagent per phase, **not one for the whole goal**. That lets a **review phase run as a fresh sibling of the maker phase it checks** (`config.review.independent`) and a mechanical
step drop to a cheaper tier via `resolve-step`. `/sigma-goal` surfaces the recommendation and pauses
at each gate; if an approved phase runs inline it uses the session model, while every dispatched
Codex phase passes its tier (or `off`) and the canonical effort to `host-model`. With `off`, that resolver selects the versioned ordinary-work default (`sonnet` → Terra).

**Reviewer floor (#2020).** Under `auto`, plan-review, review and pr-review phases run at
`max(goal tier, floor)`: `predict.py resolve-phase <phase> <tier> .sdlc` prints it (`off` when not
auto). `model_selection_reviewer_floor` defaults to `sonnet`; the price ceiling still wins. New
installs ship `auto`; a config without the key stays `off`. Saving: not measured.

## Two granularities, two axes (0.6)

- `predict.py resolve '<goal>' .sdlc` — the GOAL ceiling tier (bare tier or `off`; backward-compatible).
- `predict.py resolve-profile '<goal>' .sdlc` — the GOAL pair `model=<tier-or-off> effort=<...>`
  used for both Claude and Codex dispatch.
- `predict.py resolve-step '<step>' .sdlc` — the per-STEP pair `model=<tier> effort=<low|medium|high>`,
  so a mechanical step inside a hard goal (tests, watcher, lint) runs cheaper than the ceiling.
- Both honor the same gate: `config.json` → `"model_selection": "auto"` (default `off` disables
  portable prediction). A Codex dispatcher still maps its explicit `off` fallback to a concrete
  model and effort; inline work stays on the session model.

## Recording the choice (issue #1030)

`resolve`/`resolve_step` don't just predict — under `auto`, each also **records**, in code, before
returning. Internally, once a real tier is being returned, they run the equivalent of
`python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/loop.py" emit .sdlc "$goal" model_choice --model <tier> --signal <signal>`,
writing a `model_choice` ledger event so a downstream model-tier effectiveness metric has a
real `model_choice` event row to join against outcome + cost. This is **not** a second
instruction for a calling skill to remember — every current caller (`sigma-loop`, `sigma-goal`) and
every future one gets it automatically, the same way `resolve`'s own tier prediction is automatic
today. (The failure mode this avoids: `sigma-retro`'s own SKILL.md carried an identical prose
"also call `loop.py emit ... retro`" instruction for a long time, and #1013 measured zero retro
events across 559 real ledger files.) Fails open by construction: with no ledger/journal
configured, or `resolve`/`resolve_step` called with no goal identifier at all (every caller before
this issue), nothing is written and nothing raises — a bare `tmp_path` fixture is exactly as safe
as a fully configured repo. `why` (the third CLI verb) stays pure and unchanged — it writes
nothing, by design, so an exploratory call always has zero side effects.

**A second, independent recorder exists too (issue #1627).** `sigma-loop`'s own `loop.py` `_next()`
now calls `why` itself at pick time and makes ONE literal `ledger.safe_append(..., "model_choice",
...)` call directly — a genuine, code-detectable call site, unlike `resolve`'s own
subprocess-shells-to-`loop.py-emit` path above, which is why `model_choice` no longer needs a
static-checker waiver on that side either. This does not replace `resolve`/`resolve_step`'s own
recording — both can fire for the same goal (harmless: a duplicate ledger entry) — it closes the gap where NEITHER fired
unless a calling skill's own prose remembered to invoke `resolve` by hand, which issue #1627
measured as happening almost never.

## Re-resolving from the measured lane, after Research (#390)

Everything above resolves a tier from the goal's TEXT, before any code has been examined: the best
evidence available at pick time, and no more. Research (P2) then measures the goal's structural
footprint into a **lane** (`small`/`medium`/`large`, `discovery.py`'s `LANES`), and that measurement
used to be thrown away for tiering: P3 onward kept the pre-P2 text tier even when Research found the
goal much smaller (or much bigger) than its wording suggested.

`python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/loop.py" lane-tier .sdlc "<goal>" <lane>
<current-tier>` re-resolves the goal's ceiling from that lane, right after Research and before P3 is
dispatched; `<current-tier>` is the SAME pick-time tier already resolved above, never a fresh `why`
call. It lives in `sigma-loop`, not here, because it must cooperate with that skill's escalation
floor (`loop.py escalate`, `.sdlc/state/escalation/<goal>.json`): a floor already recorded there
still wins over a lane cap. The rule:

| Lane | Result | |
|------|--------|---|
| **small** | `current-tier` capped at `sonnet` | a CAP, not a floor: a title-only `haiku` match is left alone; only `opus`/`fable` are pulled down |
| **medium** | `sonnet`, always | matches this skill's own `sonnet` default, whatever the text guessed |
| **large** | `opus`, always | can RAISE a goal: a structural change earns the top tier even when nothing in the text says so |

`model_selection_max_tier` (above) still clamps the result, and a floor this goal already escalated
to (via a `plan-review`/`code-review`/`pr-review` send-back) still wins over the lane cap; the verb
reads that floor itself. Prints `LANE <tier>` (the ceiling from P3 onward; Research keeps the pre-P2
tier it was dispatched with) or `OFF` when `model_selection` is not `"auto"` for the running host,
and exits 2 with nothing recorded when the Codex host mapping refuses the tier. Records
`model_choice` with signal `lane:<lane>`; nothing to log by hand. The exact call site is "Match the
ceremony to the goal" in `sigma-loop/references/running.md`.
