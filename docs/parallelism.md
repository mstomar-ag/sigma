# Parallelism

Running slices of one goal, or several goals, at once. Back to the [README](../README.md).

## Slice parallelism (optional, off by default)

A goal is planned as slices, and the loop runs them one after another — even when three of them touch
nothing in common. That isn't only slow: a long goal spends **one session's context** on work that had
no reason to share a window, so the last slice starts on a flushed one.

Turn it on:

```json
"parallel": { "enabled": false, "max_concurrent": 3 }
```

Then declare the slices beside the goal's plan, in `.sdlc/plans/<goal-stem>.slices.json`:

```json
[
  {"id": "s0", "title": "extract the config reader", "files": ["config/**"]},
  {"id": "s1", "title": "rewrite the loader", "needs": ["s0"], "files": ["engine/loader.py"]},
  {"id": "s2", "title": "new CLI flag",       "needs": ["s0"], "files": ["cli/**"]},
  {"id": "s3", "title": "migrate the schema", "needs": ["s1"], "files": ["db/**"], "size": "large"}
]
```

`needs`, `files`, `size` (`small` · `large`) and `status` (`pending` · `done`) are all optional.

```bash
slices.py plan     .sdlc "$goal" [--max N]   # the dispatch plan, wave by wave
slices.py frontier .sdlc "$goal"             # what is runnable right now, one id per line
slices.py check    .sdlc "$goal"             # validate only — exit 1 with the problems listed
```

**Waves, not a thread pool.** `plan` takes the runnable frontier (not done, every `needs` already
done), packs a **wave** of mutually non-conflicting slices capped at `max_concurrent`, and repeats.
Widest fan-out goes first, so a wave is never spent on leaves while the critical path waits, and the
ordering is fully deterministic — you can read the plan before anything is dispatched. `check` reports
unknown dependencies, duplicate ids, and **dependency cycles by their members** (you have to know
which edge to break).

**The conflict rule is deliberately paranoid.** Two slices conflict when their `files` globs *can*
overlap — `fnmatch` in both directions plus a literal-prefix check, so `engine/**` and
`engine/graph.py` are correctly seen as the same blast radius. **A slice that declares no files
conflicts with everything** and runs alone: an unknown blast radius is not something you may
parallelise, and one lost edit costs far more than one extra wave. Every slice that *does* declare
files is dispatched with **`isolation: worktree`**, so concurrent siblings can't see or stomp each
other's half-finished work.

**Where this stops, honestly.** `/sigma-loop` runs a wave's slices as **subagents** — fresh isolated
context each, which is a documented, dependable primitive. It does **not** drive the Claude desktop
app's "chips": that mechanism is app-internal and **not a public API for plugins**, so building on it
would be building on something that can move without notice. So a slice marked `"size": "large"` — too
big for one subagent's context — is dispatched as `session`, and the plan **prints the exact command
for you to start**:

```
claude --worktree 0007-cache-s3
```

The loop never runs it for you. It also never shells out to an unattended `claude -p`: that's uncapped
spend, and it would put a second worker on one `.sdlc`, which every state file in the kit assumes never
happens. Leave `parallel` off, or ship no manifest, and every goal runs as one unit exactly as before.

## Goal-level parallelism (optional, off by default)

Slice parallelism above runs ONE goal's implementation concurrently. This is a sibling capability one
level up: running MULTIPLE BACKLOG GOALS concurrently in a single session, each all the way through
its own research-through-PR lifecycle in its own worktree+branch. The target scenario is ONE person on
ONE machine working through a stack of their own assigned issues — not a team coordination mechanism
(the [team ledger](action-log-and-ledger.md#the-team-ledger-optional-off-by-default) already owns that).

Turn it on:

```json
"parallel": { "goals": { "enabled": false, "max_concurrent": 3 } }
```

`loop.py next-batch` returns up to `max_concurrent` goals in one call instead of `next`'s one — with
it off, or only one goal available, it's byte-identical to `next`. That one-goal case is
no longer run inline on hosts with subagents — every returned goal, batch of one included, is
dispatched to its own subagent; `parallel.goals.enabled` controls only how many can run
*concurrently* in one pass, and only with `work.enabled` on (one worktree per goal; off, `next-batch` clamps to one). Each returned goal is dispatched as
its own subagent running the full loop for that one goal; as each finishes, refill the freed slot with
`loop.py next --skip <the other still-live goals>`. **`--skip` is not optional on a refill** — the
existing writer-identity claim check can only tell whether the specific short-lived `loop.py` process
that wrote a claim is still literally running, and that process exits within moments of writing it,
regardless of whether a subagent is still actively working the goal minutes later. Omitting it risks
two subagents picking up the same goal — worse than not parallelising at all. Unlike slices, goals
carry no file-conflict graph: each gets its own worktree and PR, so overlap surfaces later as an
ordinary PR-rebase, not a silently lost edit.

On a host without subagents, `/sigma-loop` calls `loop.py next` for one goal at a time and runs it
inline. The code-level `HANDOFF` stops after 20 goals by default and directs a fresh `/sigma-loop`
session, bounding context on that host. With concurrent goal slots, the cap counts goals already
completed and goals still running; a `HANDOFF` stops new claims while the orchestrator waits to
record its active slots. Independent review still follows `reviewer.py resolve`.

A goal `next-batch` claims but never actually dispatches this run — more picks than free slots, or
one you deliberately leave out of a refill — is not cleaned up on its own: `mark_in_progress`'s
`sdlc:in-progress` label (or, local-goals mode, `status: in_progress`) sticks with no sanctioned way
to clear it short of hand-editing the issue. `loop.py release <dir> <goal> [reason]` is the sanctioned
counterpart to `mark_in_progress` — it removes the in-progress label/board card (or resets the local
status) and leaves an audit comment, without touching the goal label, closing the issue, or spending
any of `budget.max_iterations` (the goal was never actually worked, so it stays exactly as pending as
before it was claimed and can be picked right back up).

See [Zero-touch routines](routines.md#zero-touch-routines-optional-off-by-default) below for how a routine/cron
trigger uses this together with the session-active marker to drain a backlog unattended.

## Memory & sizing (read before running many goals in parallel)

Sigma itself cannot leak resident memory in the classic sense — every verb (`loop.py`, `work.py`,
`handoff.py`, the hooks) is a short-lived CLI that exits after each call; nothing stays resident
between them.

**The dominant cost of driving N goals via [goal-level parallelism](#goal-level-parallelism-optional-off-by-default)
is the N agent sessions themselves, not sigma.** An active session driving tool calls through a
goal is far heavier than an idle chat session — ten idle sessions is not the same memory footprint as
ten active goal slots. `parallel.goals.max_concurrent` is therefore effectively a **memory knob**: size
it to what your machine can sustain running that many *active* agent sessions concurrently, not just
to how many issues happen to be open.

**Token use is a separate sizing axis.** Codex goal and phase children start with fresh context;
Claude and Codex pass durable artifact paths and short outcomes between phases, while independent
review still reads the full relevant evidence. Keep stable instructions before varying goal text
where the host exposes prompt caching, and do not paste prior transcripts into new prompts. Cache
controls and compaction belong to the host, not this plugin; Sigma does not claim to set a
cache TTL or a model context limit. [OpenAI's model guidance](https://developers.openai.com/api/docs/guides/latest-model)
and [Anthropic's prompt caching guide](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)
describe the underlying mechanisms. Measure input, cached input, output, model mix and completed
goals before claiming savings; a cached-token total alone is not a quota or bill.

### The resource guard (derived limits, holds, shed)

Sigma reads the machine (memory, load, pressure, cores) and derives its own limits: a memory reserve, a share cap
for all leases together and a CPU reserve, each a ratio of THIS machine (the keys are in
[configuration](configuration.md); every default is **uncalibrated**, see the evidence below). Nothing is a
constant copied from a laptop, and nothing here ever pauses a process.

- **Admission, not interruption.** A goal pick is gated by the machine's limits, and a Sigma-spawned model session
  and a `loop.py verify` each take a lease first. When the machine has no room the answer is **HOLD** with a
  reason and a retry time: the picker leaves goals unclaimed, a verify exits 5 (`RESOURCE-HOLD`) without running or writing evidence, and the goal is
  not failed. The hold lifts by itself after consecutive healthy samples (hysteresis), so a dip and a hover do not
  flap. `resource.mode` is `enforce`, `observe` (logs the would-be hold, admits) or `off`.
- **Verify workers.** The granted worker count reaches the proving command as a `{workers}` token (replaced by an
  integer before the shell sees it), as `SIGMA_VERIFY_WORKERS`, and as `PYTEST_XDIST_AUTO_NUM_WORKERS` for
  `-n auto`. A literal `-n N` is never lowered: a warm cost record that cannot fit N holds instead.
- **Shed.** With `resource.shed` at `verify` (the default) and `enforce`, `resource.shed_after` (default 3) critical samples in a row stop the
  verify process group Sigma itself started (TERM, a grace, then KILL). The result is `RESOURCE-HOLD (shed)`,
  exit 5, no evidence, the goal not failed; rerun once admission reopens. A session is never signalled.
- **What the guard cannot cover.** Tests you run by hand outside the verify wrapper; memory used by other
  programs; a host's own subagent fan-out; Windows (the guard refuses there rather than guess).
- **Measured or not.** The macOS and Linux calibration runs are recorded in
  `docs/launch/evidence/resource-guard-macos-calibration.md` and
  `docs/launch/evidence/resource-guard-linux-calibration.md`; the macOS record is a partial run (one verify, held by the memory gate on an already loaded host, no footprint measured) and the Linux record says NOT RUN.
  Every clause and the test that covers it is in `docs/launch/evidence/resource-guard-controls.md`.

Two things genuinely grow with a repo's own lifetime and are worth knowing about:

- **Per-process ledger entry files** (`.sdlc/ledger/entries/<actor>-<host>.<pid>.jsonl`, when
  `ledger.enabled`) — one new file per writing process, never compacted, so the count only ever goes
  up: an external audit of one adopter machine found 175 for a single actor after two and a half
  weeks, and `read_all()` unions every one of them at over a dozen call sites, including every claim
  and pre-work check. This is correctness-neutral by design (nothing reads them by name) but it is
  the read cost that grows. Entry-file compaction/coalescing is recorded as later work, not yet
  built, since it touches shared-branch semantics.
- **Append-only local logs** (`.sdlc/state/log/<goal>.jsonl`, when `action_log.enabled`) — grow for the
  life of a goal by design (the log IS the trace); nothing prunes them.

The embeddings cache (`.sdlc/state/embeddings.json`, used by `backlog_check.embed.enabled`) is the one
growth vector that IS bounded: a write drops the previous embedder identity's entries wholesale on a
provider/model swap, and each identity's own entries are capped with evict-oldest, so neither an
identity swap nor ordinary text-edit churn can grow the file without bound. One trade-off worth
knowing: swapping BACK to a previously-used identity no longer comes back cache-warm — it re-embeds
like any other fresh identity, since the old entries are dropped rather than kept around against a
possible return. The cap must also exceed the working corpus, or it never converges: a backlog even
one issue bigger than the cap re-embeds the overflow (`corpus − cap` calls) on every run instead of
settling down, so raise the cap rather than let it thrash.
