# Platform support and quality

What is validated on each host, and how quality is guarded. Back to the [README](../README.md).

## Status (honest)

Sigma's **full SDLC run remains validated on Claude Code**. The Codex adapter has been exercised
in a real Codex session through its first loop command, and its plugin inventory and rollout usage
were checked against the real Codex CLI/session store. A complete Codex goal through PR merge has
not yet been measured. Cursor scaffolding remains experimental and has not been run in Cursor; see
[Other platforms supported](#other-platforms-supported).

The **git half** of per-goal worktrees is verified end-to-end against a real repo (worktree cut from
`<remote>/<base>`, `verify_command` running in the goal's tree, the main checkout left untouched). The
**`gh` half** — opening the PR and the merge gate — is verified end-to-end against real, live, merged
PRs with independent post-PR review + auto-merge (confirmed via 9+ merged PRs showing an
independent-review approving comment from the same account that merged, as of 2026-08-06; a REST re-count
on 2026-10-13 of the source repository found 97 such PRs merged by that date, all merged by the account that
posted the approving comment; those comments carry the same approve marker under its pre-rename prefix
(78 of the 97) or a bare APPROVE heading (19), not the current `sigma:approve` spelling). The
gate fires reliably in production use on this very repo.

## Other platforms supported

The full pipeline is validated on **Claude Code**. Codex now has a project adapter and measured
component checks; a complete Codex goal through PR merge is still unverified. Cursor scaffolding is
unit-tested but has not been run in Cursor.

### Cursor (experimental)

> **Not yet verified in a live Cursor session.** The scaffolding is built and unit-tested, but
> Sigma has **not been run end-to-end inside Cursor.** Treat this as experimental — the `.mdc` rule
> format follows Cursor's documented convention, but real-session behavior is unverified.

Sigma ships no Cursor plugin manifest (whether Cursor's plugin support could load one is unverified),
and Cursor has no `UserPromptSubmit` hook or `code-review` plugin. From the root of your
repository, with `<installed-sigma>` your Sigma checkout:

```
python3 <installed-sigma>/skills/sigma-init/scripts/init_flow.py . --cursor --demo
```

That writes **`.cursor/rules/sdlc.mdc`** — intended as an *always-applied* Cursor rule carrying the full
7-phase discipline (Cursor's analog of the Claude hook) — and **`.cursor/rules/output-contract.mdc`**,
which points status output at `render.py` instead of at prose to imitate, scaffolds the `.sdlc/` layer,
and pins `companions: off` so each phase would run via the **portable `sigma-*` executors** instead of the
Claude-only companions. The loop, model-selection, status and KG **helpers are plain zero-dep
`python3`** — run them from Cursor's terminal (e.g.
`python3 <installed-sigma>/skills/sigma-loop/scripts/loop.py next .sdlc`). Once verified in a live session, the
goal is the same spine, executors, and audit trail without Claude — **help testing this is welcome.**

### Codex (partial live validation)

From an installed Sigma skill in Codex, run `/sigma-init` or its scaffolder with `--codex`:

```sh
python3 <installed-sigma>/skills/sigma-init/scripts/sdlc_init.py . --codex
```

This writes or refreshes a managed `AGENTS.md` block while preserving rules outside it and leaving
Claude's project configuration alone. Each skill explains how Codex resolves the installed skill
directory in commands that use `CLAUDE_SKILL_DIR`. `/sigma-doctor` checks Codex's installed, enabled
Sigma version; the review
resolver recognizes a Codex session and offers `codex exec --sandbox read-only --ephemeral` as a fresh
review process. Codex never receives the shared `haiku`/`sonnet`/`opus`/`fable` ledger tiers as model IDs:
`predict.py host-model codex` resolves them, or the explicit `off` fallback, through the plugin's versioned map and `model_host_overrides.codex`. Slice sessions use the existing goal worktree and the
main checkout's manifest; without both paths they refuse to print a runnable command. Codex slice
planning requires `work.enabled` and a goal worktree; without them, run the goal as one unit. Slices
sharing a checkout run sequentially.

The first `loop.py start` gesture succeeded in a disposable real Codex CLI session. Codex phase
usage data was checked against real rollout records; older rollout IDs without a bounded date lookup
report usage as unavailable. **A complete Codex goal through PR merge has not
yet been measured**. The separate Codex supervisor is available as an opt-in
path after the bounded smoke in [the migration guide](codex-autonomy.md);
the Claude-specific unattended watcher still has no Codex budgeted backend.

## Quality & drift (`evals/`)

The kit's "output" is agent *behavior*, so quality is guarded in two tiers, re-run on every change to
catch drift (see [`evals/README.md`](../evals/README.md)):

- **Tier 1 — deterministic behavioral gate (free, in CI):** `python3 <installed-sigma>/evals/run.py` runs the intent hook
  over a behavioral corpus (`evals/fixtures.json`) — a deterministic proxy for *"the agent got the right
  discipline signal"* — scores it, and **fails the build if the score drops below `evals/baseline.json`.**
  That drop is the drift signal.
- **Tier 2 — LLM-judge behavioral evals (opt-in, parked):** run the agent on each fixture goal and have
  an LLM judge score the transcript against its rubric. The runner + injectable `agent`/`judge` seam are
  built and tested; the real LLM wiring is withheld until the API budget is greenlit, so `--live` prints
  a parked notice instead of spending.

## Requirements

- **Runtime:** bash + python3 (stdlib) — zero dependencies. Two optional features additionally need
  the [`gh`](https://cli.github.com) CLI, authenticated (`gh auth login`): the **GitHub backlog
  source**, and the **PR + merge gate** half of per-goal worktrees (`work.enabled` on its own needs
  only `git`; `work.py pr` / `merge` are what need `gh`). The default local source stays zero-dep.
- **Knowledge graph (optional):** the graph builder — default `graphify` (`pip install graphifyy`);
  off unless `knowledge_graph.enabled` is set.
- **Companion (optional):** `code-review` — an extra Review pass when installed. Never required:
  the `sigma-*` executors run every phase.
- **Dev/test:** `pip install pytest`, then `pytest tests/ -v`. **CI** (GitHub Actions) runs the full
  suite — including the **leakage gate** (`tools/leak_scan.py`: home paths, secret shapes,
  private-key bodies and links into the owner's other repositories; its docstring lists what it
  cannot see), the **hook behavioral-spec**, and the **Tier-1 quality
  gate** (`evals/run.py`) — weekly and on demand, on Linux with Python 3.10, 3.11, 3.12 and 3.13 and on macOS with Python 3.12; nightly, one macOS Python 3.12 leg runs the suite minus the heaviest test files (`nightly`); every push and pull request runs one macOS Python 3.12 leg (the `test` job, the short smoke set in `.github/smoke-tests.txt`). The `test` job also runs the safety check (`evals/safety/check.py`: secret shapes, sinks, hostile fixtures). CI neither measures nor
  enforces code coverage. A one-off local run of the suite under coverage.py (Python 3.12, macOS) gave last measured
  line coverage of **90.9%** over `skills/` and `hooks/`; it understates, because scripts tests copy elsewhere and
  children started with a scrubbed environment are not traced. A Linux trial of the same run took 2.1 times as long
  as the plain leg and tripped a timing test, so it is not a gate yet. Commands, the trial and what is not
  measured: [docs/launch/coverage.md](launch/coverage.md).

## Install verification

The marketplace this repository adds is named `sigmaloop`, so the plugin id is `sigmaloop@sigmaloop` on
Claude Code and on Codex. The Claude Code install was measured in an isolated profile for this name; see
[onboarding-control.md](onboarding-control.md). Codex reads the same `.claude-plugin/marketplace.json`:
Sigma Loop ships no other manifest. On 2026-10-08 the marketplace URL and plugin id were tested in an
isolated `CODEX_HOME` against the 1.0.3 feature branch; the enabled inventory and installed skill
resolved to the same clean commit. A full Codex goal-to-merge run has not yet been measured. Sigma ships
no Cursor plugin manifest; whether Cursor's own plugin support could install Sigma is unverified.
