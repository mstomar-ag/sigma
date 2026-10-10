# Quality evals — catch drift on every change

## Benchmark harness: arms, isolation and the smoke control

The benchmark method is fixed in
[`docs/bench/preregistration.md`](../docs/bench/preregistration.md).  Three live arms exist, run in
this order for every task: **A1 `sigma`** (Sigma loaded from a clean `git archive` export of a pinned
commit, never the repository), **A2 `plain`** (the agent alone) and **A3 `matched`** (the plain agent
retried in fresh workdirs until the visible tests pass or token spend reaches A1's token spend for that task;
the first visible-passing attempt is scored, else the last).  There is no other arm.  A human launches the
harness; it refuses CI and background execution and nothing here has been run against a real model.

```bash
python3 evals/bench/bench.py run --manifest <tasks/manifest.json> \
  --hidden-root <external-hidden-bundles> --results <results.json> \
  --max-tokens <token-ceiling> --batch-pairs 3 --isolation-launcher </absolute/operator-sandbox> \
  --arm all --model <pinned-model-id> --permission-mode <mode> --sigma-commit <sha>
# after a batch or a rate-limit stop (exit 75): the same command plus --resume
```

The benchmark runs on the owner's Max **subscription** (owner decision, 2026-10-06): the measured fact is tokens read from
the host's own usage records, the ceiling is a token ceiling, and dollars in a results file are indicative (tokens priced
at list rates), never a bill. The run is batched and resumable; the design, the measured basis of the ceiling
(210,000,000 tokens) and its uncertainty are in [`docs/bench/token-budget.md`](../docs/bench/token-budget.md).

Add `--dry-run` to validate everything and print each arm's *planned* isolation facts without starting
anything.  `--fake-arm` replaces `--arm` for the zero-spend smoke control (two local tasks, no `claude`).
`--max-tokens`, `--model`, `--permission-mode` (identical for every arm) and, for the sigma arm,
`--sigma-commit` have no defaults.  `--scratch-root` must be empty.

What the harness enforces, and what it does not:

- **Isolated profile.** Every run, and every A3 attempt, gets a fresh `HOME`, `CLAUDE_CONFIG_DIR` and
  `CODEX_HOME`, and `TMPDIR` points inside that profile (an agent writing to a hard-coded `/tmp` path still shares that
  channel unless the launcher prevents it).  The
  rest of the harness environment is forwarded WHOLE, not allow-listed: the subscription token
  (`CLAUDE_CODE_OAUTH_TOKEN`) set by the operator reaches `claude`, and so does any other credential in that
  environment (cloud keys, `GH_TOKEN`, and an `ANTHROPIC_API_KEY`, which `claude -p` prefers and which would silently
  bill per token), readable by the agent and by agent-authored test code at scoring; launch the benchmark
  from an environment holding only what it needs (the launcher refuses a missing token and an API-key config).  Only values containing the hidden root and the
  harness's own `SIGMA_BENCH_*` variables are dropped.
- **Operator plugins untouched.** The operator's `~/.claude/plugins` (and `$CLAUDE_CONFIG_DIR/plugins`)
  is content-hashed once before anything starts, again before every run and every A3 attempt, and after
  the last run.  Any change aborts the benchmark; do not run other Claude sessions that update plugins
  meanwhile.  Measured on one machine: 332 MB in 15,953 files hashed in about two seconds, so about 200 hashes over
  15 tasks is about 7 minutes, linear in the directory's size (not measured at 10x or 100x).
- **Clean export.** The sigma arm loads `.claude-plugin`, `skills` and `hooks` of the pinned commit.  The
  export is refused if it holds links, escapes its directory, has other top-level entries, or has a path
  component named `evals` or `tests` (a future skill folder with that name fails A1 loudly).  It omits
  `docs/`, `tools/` and `contract/`; how A1 behaves without them is not measured.
- **Hidden tests.** The hidden root must be outside the repository, every task source and the scratch
  root, and the scratch root must be outside the repository.  Each run directory (worktree, profile, transcripts, scoring copy with the hidden bundle) is
  deleted right after scoring, so a later arm or attempt cannot read an earlier solution or the bundle.
  Scoring commands run through the launcher, bounded by `--scoring-timeout-seconds`.
- **Tokens, batches and resume.** The ceiling `--max-tokens` is TOKENS (input + output + cache read + cache write, summed
  over a run's transcripts, subagents included) and is checked between runs, because the host has no per-run token cap:
  one run can overshoot, and the overshoot is counted. Each `claude -p` also gets `--max-budget-usd` of `--belt-usd`, the
  host's client-side estimate (indicative under a subscription), as a runaway guard. A run with no readable usage record,
  or zero tokens, stops the benchmark before it is scored ("authentication is missing or the transcripts cannot be read").
  The results file is the resume cursor: rewritten atomically after every pair, always listing every planned pair as
  `completed`, `failed` or `not-run` (`complete` is true only when none is not-run), with an `in_flight` marker while a
  pair runs. `--batch-pairs N` (default 3) stops cleanly after N pairs; `--resume` re-attempts only not-run pairs, carries
  A1's token count to A3 and the tokens already spent, and refuses unless the conditions (manifest hash, arms, model,
  permission mode, Sigma commit, `claude --version`, belt, attempt bound, deadlines, hidden-bundle digest) match the first
  batch; a ceiling may be raised on resume, never lowered. A host rate-limit record on a run that did not exit cleanly (or a session that ended on it) stops
  the harness (exit 75, reset time in the message): that pair is recorded not-run, never scored and never a failure. Exit 76
  is the token ceiling. A run that exits non-zero with real work done and no rate-limit record to explain it (the host's
  rate-limit signal under `-p` is unmeasured, and so are overloaded and auth errors) is not scored either: it is recorded
  not-run and the harness stops (exit 77), so an infrastructure fault is never scored as the arm failing; exit 78 is
  not-run pairs nothing else explains. A `<results>.lock` refuses a second invocation on the same cursor. Every other stop keeps the rows
  already paid for with an `aborted` object. A pair killed in flight is counted on resume as an unknown-token run
  (`unknown_runs`, and `tokens_spent` is then a lower bound). `bench.py summarize --results <file>` prints relative outcome
  and relative token cost per arm and refuses an incomplete file. The last A3 attempt can overshoot A1's token spend by up
  to one attempt; A3 stops on `--max-attempts`, the remaining ceiling or the run deadline too (one deadline shared by all
  its attempts, where A1 and A2 each get a full one), and its row's reason names which. Usage is read from the transcript
  under the profile, so it is exactly as honest as the launcher.
- **Commands are bounded.** Every launched command runs in its own process group with a timeout and the
  whole group is killed on expiry or normal exit (a launcher, or a process an agent or Sigma's own
  hooks start in a new session, escapes this; a watcher the sigma arm's hooks leave behind can outlive
  its run directory).
  SIGTERM and SIGHUP unwind the harness the same way (children killed, paid rows kept; a second signal
  during the unwind can cut it short); SIGKILL or a power loss cannot, and a surviving `claude` is then bounded only by its own `--max-budget-usd` belt.

[`evals/bench/launcher/`](bench/launcher/README.md) is the operator launcher written for this harness (install it outside the
repository; its README states what it does and does not guarantee).  The launcher is an **operator-owned external sandbox or privilege-separation boundary**: Sigma validates
its location and executable bit, but cannot infer that an arbitrary executable's bytes contain an agent,
so hidden-test confidentiality at scoring time (the bundle sits beside agent-authored code) and the
agent's reach into the real filesystem depend entirely on it.  Arbitrary in-process `Arm` subclasses are
refused; only the exact in-tree arm classes run.  The `claude` flags (`--plugin-dir`, `--session-id`,
`--max-budget-usd`, `--permission-mode`, prompt on stdin) are listed by this host's `claude --help` but
have not been exercised end to end, and the first live run will stop at the no-usage guard until the
operator supplies the subscription token.

Sigma's "output" is agent *behavior* (does it follow the spine, plan before editing, verify before
claiming done). Behavior is non-deterministic, so quality is guarded in tiers. Run the whole thing
on every change; a drop below the committed baseline fails the build.

### The task set (`evals/bench/tasks/`)

`manifest.json` (schema `sigma.benchmark-tasks/v1`, the file `--manifest` takes) is derived from the per-task
`task.json` files and is frozen (the freeze commit is in the pre-registration's Deviations); the three traps are
**agent-authored** by an independent subagent, not by a person outside the Sigma team (see
[`docs/bench/task-sourcing.md`](../docs/bench/task-sourcing.md), "Trap authorship"). A run needs the hidden root and, for each external task, a materialized tree. `tools/readiness/bench_tasks.py`
checks and verifies it:

```bash
python3 tools/readiness/bench_tasks.py check [--hidden-root ~/.sigma-ops/bench/hidden]
python3 tools/readiness/bench_tasks.py verify --hidden-root ~/.sigma-ops/bench/hidden --external --scratch <empty dir>
python3 -m pytest tests/test_bench_tasks.py -q
```

Internal tasks commit their starting repository (`<id>/repo/`). External tasks commit only `fetch.json` (repository,
base and fix commit, pull request, license, stars, dependencies, a digest of the base tree); `bench_tasks.py
materialize <id>` fetches the base commit's tree, without history or the fix, into the git-ignored `<id>/repo/`,
which the harness then copies like any other source. Hidden bundles never enter the repository: they live under the
operator's hidden root, and their per-file hashes are in `task.json`, so `tests/test_bench_tasks.py` goes red naming a
hidden file copied anywhere under `evals/bench/`. `verify` scores each task through the harness's own
`_command_passed` and `_hidden_passed` behind a pass-through launcher (a real isolation launcher is the operator's);
the hidden tests must fail on the starting tree (pytest exit 1) and pass on the reference fix, and the visible result must be
the recorded one. Every task is scored in one environment, because the harness resolves every argv on one PATH:
`evals/bench/tasks/environment.lock` (resolved by `bench_tasks.py lock`, hash and interpreter in the manifest) is what the run
must build and put first on that PATH; the harness does not enforce it.

## Golden tasks

`evals/golden/` holds small, self-checking tasks (slice 1 of epic #873 ships the verifier; `T3`, a multi-file refactor
with a decoy vendored file and a generated file, is the first shipped task, and T1 and T2 follow in their own slices). It has nothing to do with `contract/golden/`, which is a different thing (the golden config and
goal frontmatter the contract tests read). One gesture, no flags:

```bash
python3 evals/golden/verify.py            # verify every task under evals/golden/
python3 evals/golden/verify.py --only ID  # one task; a mistyped ID exits 2
```

A task `<id>/` holds `task.json`, `repo/` (the start tree), `reference/` (the complete fixed tree),
`allowed_paths.json`, `rubric.json` and `hidden/` (`files/`, `verify.json`, optional `naive/`).
`task.json` carries `id` (equal to the directory name), `kind`, `origin` (`planned` or `issue:<n>`), `prompt`,
`visible_command` (a non-empty list starting with `python` or `python3`; validated, never run),
`hidden_sha256` and `trap`. `hidden_sha256` is a map `{path relative to hidden/: sha256 hex}` whose keys are
exactly the regular files under `hidden/` (including `verify.json` and `naive/`); a missing, extra or
mismatched entry is red and names the file. `__pycache__` directories, `*.pyc` and `.DS_Store` are ignored.

Each failure prints `RED <task> <property>: <detail>`. Properties: `task-json`, `id`, `origin`,
`visible_command`, `hidden-sha256`, `verify-json` (the `command` must be a `python -m pytest` argv with
`-p no:cacheprovider` and `.sigma-hidden/files`), `rubric` (a JSON object), `allowed_paths` (a list of strings),
`symlink` (any link under `repo/`, `reference/` or `hidden/`), `hidden-on-start` (hidden tests must exit 1 on
`repo/`), `hidden-on-reference` (exit 0 on `reference/`), `reference-diff` (every added, changed or deleted path
between `repo/` and `reference/` must be in `allowed_paths.json`, as an exact path or a `dir/` prefix) and
`naive` (if `hidden/naive/` exists it is overlaid on `repo/` and the hidden tests must exit 1). The bundle
the tests run from never contains `naive/`.

Exit codes: 0 all green; 1 any red line; 2 unreadable or invalid input (bad JSON, unknown `--only`, a bad
`SIGMA_GOLDEN_TIMEOUT`). With no task directories it exits 0 and says `0 golden tasks found ... (nothing
verified)`: that is not a pass, and a directory without `task.json` is red, never skipped. Unix only: it
refuses on Windows (exit 2). Each hidden run has a 120 s ceiling (a hung-test limit, not a measured time);
the whole process group is killed and the run is reported red `timeout`. `SIGMA_GOLDEN_TIMEOUT` (1 to 600
seconds) overrides it and exists for the tests only. Controls: `python3 -m pytest tests/test_golden_verify.py tests/test_golden_t3.py -q`.

Measured wall time (macOS, `/usr/bin/time -p`, five runs each, one planted task with a naive patch, so 3 pytest
runs): 0.45 / 0.47 / 0.65 s (min / median / max) under the Python 3.12 venv; 3.42 / 3.77 / 3.94 s under the
machine's system Python 3.9.6, whose pytest start-up is slower. Cost is linear in tasks and runs are sequential.
The figure is for ONE task: slices 2-4 add tasks and will change it, and nothing beyond one task is measured.

## Tier 0 — structural gate over every `SKILL.md` (free, runs in CI)

The product *is* the skill prompts, and Tier 1 below scores the intent hook, not one word of them.
Tier 0 is the cheapest thing that can say anything about the prose itself: mechanically checkable
structure, no LLM, no network, same answer every run (`evals/skill_structure.py`, asserted by
`tests/test_skill_structure.py`, so it runs inside the ordinary `pytest tests/` gate).

```bash
python3 evals/skill_structure.py            # report + gate (exit 1 on a finding)
python3 evals/skill_structure.py --table    # the measurement table only, never fails
```

## Per-skill smoke runner — every skill directory has a card (`evals/skills/smoke.py`)

Slice 1 of #1043: the runner and the card schema, proven on fixture trees only
(`tests/test_skill_smoke.py`). It lists every child directory of the skills directory itself and
compares it with `<cards-dir>/*.json` in both directions: a directory with no card, a card with no
directory, and a directory with no `SKILL.md` are each a named finding. A card gives `kind`
(`exercised`, `pinned` or `described`), `gates` (strings that must lie in the kept prefix of
`SKILL.md`, the part Claude Code re-attaches after compaction), `scripts` (each on-disk `*.py` /
`*.sh` directly under `scripts/` appears in exactly one entry, role `gesture` or `library`) and
`artifacts` (each `producer` is an existing repo path, or the literal `agent`). A script `fixture` is a
shell-free argv list run from the repo root; it passes on exit 0 and, if the card gives `expect`, that
text in stdout. `--fixture-timeout` (seconds, default 60) bounds it; a timeout is a finding.
`exercised` with no passing fixture is a finding: label it `described` instead. Paths in a card are
relative to the repo root and may not be absolute or contain `..`. Exit 1 on any finding.

```bash
python3 evals/skills/smoke.py --root <skills> --cards-dir <cards>
```

The bare form with no flags reads the real `skills/` and the real `evals/skills/cards/`. No real
cards exist yet, so it stays red until slice 3 writes them, and the runner is deliberately NOT part of
the `pytest tests/` gate until then. Not measured: behaviour on the real tree.

## Phase context budget — instruction bill by agent

`phase_context_budget.py` measures the files each phase is told to load, plus the real review
briefs rendered against `examples/hello-sdlc/`.  It is a measurement, not a prompt change and not
a claim about a consumer repository whose own project documents may be larger.

```bash
python3 evals/phase_context_budget.py          # table + down-only ceiling gate
python3 evals/phase_context_budget.py --table  # table only
```

The committed JSON records the date and source commit of the measurement.  A phase's current
measurement may not exceed its ceiling, and a ceiling may be no more than five percent above the
current measurement.  That makes prompt reductions easy to ratchet into the record while refusing
to silently absorb later growth.  The test suite deliberately appends 500 words to
`sigma-loop/references/running.md` and runs the documented command to prove that this gate fails.

It asserts three things:

1. **Nothing important falls off the compaction cliff.** Claude Code re-attaches only the **first
   5,000 tokens** of each skill after a conversation is summarised; the rest is discarded silently.
   Every skill must fit that, and a skill waived for size must still keep its uppercase
   `MUST`/`NEVER`/`ALWAYS` gates inside the kept prefix.
2. **Every reference file is reachable.** Each `*.md` beside a `SKILL.md` must be reachable by
   following links from it (transitively), and every local `*.md` link must resolve.
3. **No instruction text is lost in a restructure** — see below.

Skills that breach today are recorded in `skill_budget_waivers.json`, each frozen at its measured
size and named to the issue that fixes it. A waived skill may **shrink, never grow**; a waiver whose
skill is back inside every budget **fails the build until it is deleted**; and the number of waivers
is itself capped by a test, so a fourth breach cannot be absorbed by adding a line.

**What Tier 0 cannot do, stated plainly: preserving text does not preserve attention.** It catches
*"an instruction vanished"* and *"the gates fell past the cap"*. It **cannot** catch the failure that
matters most after a `SKILL.md` is split — the agent no longer *choosing* to open a reference file it
was pointed at. A skill can pass every check here and still have got worse. Only Tier 2 could tell,
and Tier 2 is parked (#1616, path (b), re-parked 2026-09-03), so **a green Tier 0 is not
evidence that a skill still works**, and #1611's restructure proceeds with that attention risk
explicitly accepted rather than silently assumed away. The clean run prints this caveat next to its
own pass, so nobody meets the verdict without it.

### `--preserved` — the restructure instrument

```bash
python3 evals/skill_structure.py --preserved <git-ref> [skill ...]
```

Concatenates a skill's whole `*.md` corpus at `<git-ref>` and in the working tree, and reports every
instruction unit present in the first and missing from the second. It is deliberately blind to which
*file* text lives in, to re-wrapping, to heading level and to emphasis — so a restructure that
**moves** prose reports nothing, and one that **drops** prose reports exactly what it dropped. Run it
inside the restructuring PR, against the branch point:

```bash
python3 evals/skill_structure.py --preserved origin/main sigma-loop
```

This is a tool rather than a committed snapshot on purpose: a snapshot of the 4,100-odd instruction
units in this corpus would be legitimately red on most days in a repo this active, and a gate that is
red for good reasons is a gate somebody turns off. What CI pins instead is the *instrument* —
including a dress rehearsal that splits the real `sigma-loop` corpus and proves nothing is reported
lost, and its control that drops a gate paragraph and proves it is.

## Tier 1 — deterministic behavioral gate (free, runs in CI)

The intent hook (`hooks/sigma_gate.sh`) is a deterministic proxy for *"the agent got the right discipline
signal"*: a code request must trigger the full spine, a read-only question may be answered directly.
`run.py` runs the hook over the behavioral corpus (`fixtures.json`), scores it, and **fails if the score
drops below `baseline.json`** — that drop is the drift signal. No LLM, no cost, identical every run.

```bash
python3 evals/run.py          # score the corpus, gate on drift vs baseline
```

Add a fixture (`{id, prompt, expect: code|ask|standard, tier2_rubric}`) whenever you add or change a
discipline signal. Raise the baseline only when you add harder fixtures — never lower it to green a red
build without justifying the regression.

## Tier 2 — LLM-judge behavioral evals (opt-in, needs API budget) — PARKED

The only way to measure *actual output quality*: run the agent on each fixture goal, then have an LLM
judge score the transcript against that fixture's `tier2_rubric` (did it plan before editing? did
plan-review catch the planted flaw? did review find the planted bug? did it park on the irreversible
action?). Track scores over time; a drop below baseline is a quality regression.

The runner and its **injectable `agent`/`judge` seam are already here and tested** (`run_tier2`,
`test_tier2_seam_hermetic`) — wiring a real LLM is a one-function change. It is **withheld on purpose**:
it costs money per run and needs a judge-model + rubric decision, so `--live` prints a parked notice
instead of spending. Turn it on once the budget is greenlit; run it nightly / pre-release, not per-PR
(cost + non-determinism).

**Re-parked 2026-09-03**, deliberately and with the reason recorded, not by omission — operator
decision on #1616. A nightly judge is a recurring, unbounded spend committed on an estimate; what
would change the answer is a measured cost figure from a **single** trial pass, plus the judge-model
choice. Until then the honest position is that **nothing in this repo can tell whether a `SKILL.md`
edit degraded agent behaviour**, and that is accepted rather than paid for. Tier 0 above narrows what
can silently go wrong; it does not close this.

```bash
python3 evals/run.py --live    # parked until a real judge is wired
```

## Regression checker

`python3 evals/regression/check.py <record> <properties.json>` checks one record written by `evals/regression/record.py`
against the property list in `evals/regression/properties.json` and prints `PASS`, `FAIL` or `NOT EVALUABLE` per property.
Exit 0 means no failure; 1 means a failure or a hard property with no evidence; 2 means the input was refused. The committed
run and three mutated copies in `evals/regression/fixtures/` come from a synthetic run, so they test the checker, not Sigma.
