# Sigma Loop

[![CI](https://github.com/Agrim-Intelligence/sigmaloop/actions/workflows/ci.yml/badge.svg)](https://github.com/Agrim-Intelligence/sigmaloop/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Guardrails and an overnight autopilot for your AI coding agent.** Sigma Loop makes the agent plan
before it codes, has every plan reviewed before any edit, and can work through a whole backlog
unattended, with each goal finished, verified and recorded.

It is a plugin for **Claude Code** (fully validated), with adapters for **Codex** (partially
validated) and **Cursor** (experimental). It needs only `bash` and `python3`, with no other runtime
dependencies.

> [!IMPORTANT]
> **Already have Sigma installed?** If the installed plugin is older than 1.0.0, do not run the loop
> until you have updated it:
>
> ```bash
> claude plugin update sigmaloop@sigmaloop
> ```
>
> Then restart the session and run `/sigma-doctor` to confirm the version. Mixed versions writing
> `sdlc:*` labels on one board is the one setup to avoid. (`claude plugin marketplace update` only
> refreshes the listing; it does not upgrade the plugin.)

## Contents

- [What it does](#what-it-does)
- [Requirements](#requirements)
- [Quickstart](#quickstart)
- [Using Sigma](#using-sigma)
- [Configuration](#configuration)
- [Documentation](#documentation)
- [Platform support](#platform-support)
- [Measured results and known limits](#measured-results-and-known-limits)
- [Privacy and uninstalling](#privacy-and-uninstalling)
- [Contributing, support and security](#contributing-support-and-security)
- [Credits](#credits)
- [License](#license)

## What it does

Without guardrails, a coding agent tends to jump straight to editing. Sigma puts every non-trivial
change through seven phases:

**Goal → Research → Plan → Plan-Review → Implement → Review → Retrospective**

- **Plans are reviewed before any edit.** An adversarial review checks the plan against the real
  code, and against your product strategy if you have written one down.
- **"Done" is checked, not just claimed.** With the setup `/sigma-init` writes, the autonomous loop
  will not mark a goal done until the action log shows every phase ran and both reviews approved,
  and, once you confirm a test command, until it passes. Some rules are checked by code and some are
  instructions to the agent; [docs/enforcement.md](docs/enforcement.md) says which.
- **It can run your backlog overnight.** Queue goals as local files or GitHub issues. The loop works
  through them (several at once, if you opt in), opens a PR per goal, moves cards across a GitHub Projects board,
  and parks anything that needs a human instead of guessing.
- **It leaves an audit trail.** Every phase is recorded on the issue (or in a local journal), with
  its cost where the host reports it.
- **It runs on its own.** Sigma's built-in `sigma-*` skills run every phase. There is nothing else to
  install; the `code-review` plugin is an optional extra Review pass.

The full list of capabilities is in [docs/features.md](docs/features.md).

## Requirements

- `git`, `bash` and `python3` (standard library only).
- Optional: the [GitHub CLI `gh`](https://cli.github.com), logged in, if you want GitHub issues as the
  backlog, a Projects board, or a pull request per goal. A local-files backlog needs none of it.
- Optional: `graphify` (`pip install graphifyy`) for the [knowledge graph](docs/knowledge-graph.md).

## Quickstart

Install the plugin for your host, then run `/sigma-init` in the repository you want Sigma to work on.

> [!NOTE]
> `<installed-sigma>` is the directory Sigma Loop's scripts live in on your machine: the plugin
> directory your host shows for the installed `sigma-init` skill (Claude Code, Codex), or your Sigma
> Loop checkout (Cursor). Run every `python3 <installed-sigma>/...` command from the root of your
> repository, where `.sdlc/` lives. Inside a Claude Code session the skills run these for you.

### Claude Code

Inside a Claude Code / Claude Desktop session:

```
/plugin marketplace add https://github.com/Agrim-Intelligence/sigmaloop
/plugin install sigmaloop@sigmaloop
```

Or from a terminal (the same two commands in CLI form, useful for a setup script):

```
claude plugin marketplace add https://github.com/Agrim-Intelligence/sigmaloop
claude plugin install sigmaloop@sigmaloop
```

Restart the session, then, from the root of the repository you want Sigma to work on:

```
/sigma-init --demo     # checks access, asks mode + verify command, scaffolds .sdlc/, queues a demo goal
/sigma-loop            # runs the demo goal Goal → Research → … → Review
```

### Codex

```
codex plugin marketplace add https://github.com/Agrim-Intelligence/sigmaloop
codex plugin add sigmaloop@sigmaloop
```

Then run the `sigma-init` skill, or its flow directly, with `--codex` so `AGENTS.md` carries the
standing rules. Codex support is partially validated
([details](docs/platform-support.md#codex-partial-live-validation)):

```
python3 <installed-sigma>/skills/sigma-init/scripts/init_flow.py . --codex --demo
```

### Cursor

On Cursor, Sigma runs from a clone of this repository, which is your `<installed-sigma>`. Cursor
support is experimental ([details](docs/platform-support.md#cursor-experimental)):

```
git clone https://github.com/Agrim-Intelligence/sigmaloop <installed-sigma>
python3 <installed-sigma>/skills/sigma-init/scripts/init_flow.py . --cursor --demo
```

### Run `/sigma-init` first

Run **`/sigma-init`** once in every repository, on every host (`/sigma-setup` is an alias). It checks
git and `gh` access, asks where your backlog lives and which command runs your tests, scaffolds
`.sdlc/`, and ends by printing the next command to run. In GitHub mode it also creates the `sdlc:*`
labels, limits the loop to issues assigned to you (`@me`), and offers a Projects board and the team
ledger.

It is safe to re-run: it does not ask an answered question again, and it never changes a setting in
`.sdlc/config.json` unless you pass the flag for it on that run.

On Codex and Cursor there is no interactive question: the flow prints each open one as an `[ask]`
line with the flag that answers it (`--mode`, `--verify`, `--board`, `--ledger`, `--local-only`),
and `--yes` takes the safe defaults (the detected mode, ledger off; never a board, a verify command
or a work flip) for questions `.sdlc/config.json` does not already answer.

### What `/sigma-init` will ask you

- **Mode.** Local goal files in `.sdlc/goals/` (`--mode local-goals`) or GitHub issues labelled
  `sdlc:goal` (`--mode github`; the default when `origin` is a GitHub repository). The scaffolded
  example goal ships `status: proposed`, so nothing is picked until you queue a goal or pass
  `--demo`. Flags add to that: `--vision` to start from a product vision, `--demo` to queue a demo
  goal, `--codex` / `--cursor` for those hosts' standing rules. A PR per goal
  (`work.enabled`) is on by default. If this repository has no usable remote, init prints a
  DECISION: add the remote, use another existing remote, or go local-only (the loop then edits this
  checkout directly, with no worktree, branch, push or PR; `--local-only`). Nothing is flipped for
  you.
- **Board.** Github mode creates the `sdlc:*` and `priority:P0`–`P3` labels on the repository, then
  OFFERS a Projects board and creates nothing unless you say yes (`--board yes`):
  `board_setup.py create` makes `<repo> — SDLC` with Status and Priority, links it and pins
  `project.number` ([board](skills/sigma-init/references/board.md)). Or point `project.number` at an
  existing board. `--github` also copies issue templates, an auto-add-to-project workflow and a
  label guide into `.github/`.
- **Verify command.** Init lists the test commands it detects, each with a number and an id. Confirm
  one, give your own, or decline. Confirming turns `verify.enforce` on; declining leaves it off and
  records why. With enforce off and no command there is nothing to prove, so nothing demands verify
  evidence: `record done` and, in github mode, the PR merge still pass their other gates (review,
  CI, done-means-merged). `--verify` takes only `N:ID` of a candidate init printed, never a literal
  command (detected text must not become a shell line), so the first run lists the candidates and
  stops with an `[ask]`, and you confirm with a second invocation, for example
  `/sigma-init --verify 1:9f3a2c1b4d5e` with the pair printed beside your chosen candidate. The
  gestures, with `<n>` and `<id>` copied from the printed list (init prints them with both paths
  already filled in):

  ```
  python3 <installed-sigma>/skills/sigma-init/scripts/verify_detect.py confirm .sdlc <n> <id>
  python3 <installed-sigma>/skills/sigma-init/scripts/verify_detect.py set .sdlc --command-file <file>
  python3 <installed-sigma>/skills/sigma-init/scripts/verify_detect.py decline .sdlc
  ```

- **Access.** Init checks, before the loop needs them: a git repository, the `origin` remote, the
  base branch pushed there, `gh` installed, `gh auth status`, and the token's scopes (`repo`;
  `workflow` when work is on; `read:org` when the owner is an organization; `project` when a board is
  on). A directory that is not a git repository is refused before anything is written. Re-run the
  check any time:

  ```
  python3 <installed-sigma>/skills/sigma-init/scripts/preflight.py check . --sdlc .sdlc
  ```

Run these from the root of your repository.

### What init leaves in `git status`

Right after init, `git status` shows `.gitignore` and `.sdlc/` as untracked in a fresh repository.
That is expected. Init git-ignores the machine-written runtime directories (`.sdlc/state/`,
`.sdlc/ledger/`, `.sdlc/work/`, `.sdlc/knowledge/`, and a few more it lists). Review `git status`, then commit `.gitignore`
and the durable files: `.sdlc/goals/`, `.sdlc/project.md` and `.sdlc/config.json` (plans, research and
acceptance records are durable evidence too, and are committed with the goal that wrote them). Do not
ignore `.sdlc/` as a whole.

### If `/sigma-init` says you lack access

Each `[FAIL]` or `[CANNOT VERIFY]` line prints its own fix. These are the commands it prints, with
your host and base branch filled in; run them from the root of your repository. `gh auth login` and
`gh auth refresh` are interactive, so run them yourself; Sigma never runs them for you.

| Init reports | Run |
|---|---|
| not inside a git repository | `git init` then `git commit --allow-empty -m "initial commit"` |
| no commit yet | `git commit --allow-empty -m "initial commit"` |
| no remote named `origin` | `git remote add origin <url-of-your-repository>` |
| base branch not on the remote | `git push -u origin <base>` |
| `gh` not installed | install it from https://cli.github.com, then re-run the check |
| gh not logged in | `gh auth login -h github.com -s repo,workflow,read:org` |
| token missing scopes | `gh auth refresh -s <missing scopes> -h github.com` |
| a fine-grained token (scopes cannot be read) | `gh auth login -h github.com -s <required scopes>` |
| you want another remote | `python3 <installed-sigma>/skills/sigma-init/scripts/preflight.py use-remote .sdlc <remote>` |
| no GitHub remote at all, and you want to go on without one | `python3 <installed-sigma>/skills/sigma-init/scripts/preflight.py local-only .sdlc` |

`<required scopes>` is the list init prints for this repository, by the rule under **Access** above
(for example `repo,workflow,read:org` for an organization's repository with work on). A fine-grained
token has no scopes to read, so init names the permissions it needs instead: Contents, Pull requests
and Workflows write, plus Projects write when a board is on and access to the organization when
the owner is one.

If your organization enforces SAML SSO, the scopes line also names the token settings page where
you authorize the token for it.

### Adopting into an existing repo

For a real project (existing code, a GitHub board, a team), run `/sigma-init` (or its alias
`/sigma-setup`) in github mode:

```
/sigma-init --mode github --ledger yes
```

It writes a config scoped to **your own issues (`@me`), with a PR per goal**, **creates the core
`sdlc:*` lifecycle labels on the repo** so nothing is left configured but unpickable, and prints the
ledger's bootstrap line (it pushes an ops branch, so you run it). It deliberately avoids three traps
real adoptions hit: it never enables `verify.enforce` without a real `verify.command` (that refuses
every `done` forever); it never clobbers or narrows a git-ignore rule you already set (pass
`--ignore-scope local` to keep the repo's tracked files untouched); and label creation never
recolours an existing label or applies a label to any issue — deciding which
issues become pickable stays a deliberate, separate, human triage call. If your repo already gates
source edits behind its own `PreToolUse` hook, note that Sigma's Implement-phase edits go
through it too — make sure whatever it expects is satisfied.

The plugin installs machine-wide, but its hooks only speak in repos that adopted Sigma (the prompt
hook checks for `.sdlc/`; the gates and the setup wizard for `.sdlc/config.json`). The `sigma-*` skills run
every phase ([details](docs/companions.md)) — **nothing else to install**.

See the **[worked walkthrough](examples/hello-sdlc/)** for a runnable end-to-end example.

## Using Sigma

There are two ways to run work. Both use the same seven phases.

| | `/sigma-goal <goal>` — interactive | `/sigma-loop` — autonomous |
|---|---|---|
| Scope | one goal | the whole backlog |
| At a checkpoint | pauses and asks you | parks the goal for you and moves on |
| Stops when | the goal is done, or you stop it | the backlog is empty or a budget is reached |

Everyday commands:

| Command | What it does |
|---|---|
| `/sigma-goal <goal>` | Run one goal, approving each phase yourself. |
| `/sigma-loop` | Work through the backlog autonomously. |
| `/sigma-status` | Backlog counts, and whether anything is waiting for you. |
| `/sigma-unpark` | Answer the question that parked a goal, then requeue it. |
| `/sigma-promote` | Approve an issue the loop filed, so it can be picked up. |
| `/sigma-scope <idea>` | Turn a rough idea into planned, prioritised issues. |
| `/sigma-triage` | Plan a dependency-ordered run over the backlog. |
| `/sigma-chain <N>` | Solve the whole chain of open blockers behind one goal: plan, ask once, arm, run. |
| `/sigma-doctor` | Check the setup and print the exact fix for anything wrong. |
| `/sigma-audit` · `/sigma-align` | Audit the codebase's health, or check shipped work against your strategy. |

### Before you start `/sigma-loop`: independent review

The loop asks a fresh, independent reviewer to review every PR it opens, and records the approval
with `work.py post-review`. With `work.require_review: approval` the merge needs that recorded
approval; the shipped default (`changes`) does not require one, so check your setting before you
rely on it ([enforcement](docs/enforcement.md)). A host may refuse that step: on
Claude Code in auto mode, the permission classifier denied the approval as self-approval in a first
live run, and the loop stopped after opening the PR with no recorded review. Sigma does not work
around a host refusal and never fakes an approval. The loop's instructions say to park the goal and
not retry (no code produces that park; it is instruction text), and the **attended fallback** is yours to take: read the PR, then comment `sigma:approve`
from your own account (a comment from an owner, member or collaborator counts; GitHub's formal
approval needs a second identity because the loop opens the PR under yours), then re-queue the goal
with `/sigma-unpark`. To let the loop post the approval itself you need a host
permission rule you set yourself; a stock install has none, so plan for attended review.

### Small goals, and what you will see

A small goal still gets acceptance before the work: `acceptance.py record` captures the `## Done when`
criteria first, never after the work, and a goal with none gets a drafted section recorded first. The
only phases you may skip are research and retro, with
`loop.py waive-phases .sdlc <goal> research,retro --reason "<why>"` (recorded in the log); plan,
plan-review, implement and review always run and show. `work.py pr --no-tests "<reason>"` is a
different thing: where `verify.enforce` is on, it excuses only the test-first proof for a change a
test cannot see, and it is not a waiver of any phase.

At each phase boundary the loop runs `phase_report.py start` and `phase_report.py end`; `end` prints
the phase's cost line (or says why the host cannot report one). No cost line means those two commands
did not run or the host hid their output; `loop.py phases .sdlc <goal>` shows from the action log
which phases were recorded. After a merge, `loop.py reconcile-merges .sdlc` prints nothing when there is nothing
left to record, and that silence is success; a newly merged goal prints exactly one line such as
`1 done (PR #12 merged)`.

To leave the loop running overnight with automatic restarts, see
[Running the loop unattended](docs/running-unattended.md). How the loop picks goals, the budgets that
stop it and what makes it park are in [Running the loop](docs/running-the-loop.md).

## Decision Rulebook and Upkeep

Two optional features ship off and open one gate at a time. With every gate closed, Sigma behaves exactly as it
did before they existed.

**Decision Rulebook.** When the loop parks a goal because it needs a decision, your answer through `/sigma-unpark`
can be kept as a decision record in the repository. Enough agreeing answers of one kind in one area earn a higher
autonomy level. At low levels the loop only records and recommends; at the top two levels, and only if you open the
applier, it may answer a question nobody answered within a window, using a rule an owner approved, tell you on the
issue, and take your `no` as a veto. A hard-stop net can also park dangerous actions, a drift judge can compare a
question to your reference documents (shadow only), and past decisions can be imported. Public repositories keep
free text out of records. A learning loop ranks which question and area pairs deserve a rule and pools agreement
across machines. Full guide, including how to turn it on step by step and what it will never do:
[docs/decision-rulebook.md](docs/decision-rulebook.md).

**Upkeep.** Keeps long-lived unit branches (`feature/<name>`) rebased onto their base. On a 12-hour window, the first
session start (or the watcher tick) starts one bounded background sweep of every unit that is behind; each rewrite
keeps the old tip as a backup ref you can restore or prune; commits made directly on a unit branch are refused rather
than overwritten. A changelog or append-only log conflict is unioned, and an optional agent conflict level is off by default;
with no spend key set it runs with no spend limit. It needs three opt-ins (`upkeep.enabled`, `ledger.enabled`, and `SIGMA_UPKEEP_JOB=1` on one machine).
Full guide: [docs/upkeep.md](docs/upkeep.md).

## Configuration

Settings live in `.sdlc/config.json`, which `/sigma-init` writes for you. The ones most people change:

| Setting | Default | What it does |
|---|---|---|
| `discovery.source` | `github` when `origin` is on GitHub, else local | Where goals come from: `.sdlc/goals/` files or GitHub issues labelled `sdlc:goal`. |
| `verify.enforce` | on once you confirm a test command | Refuses "done" until your test command passes in this run. |
| `work.enabled` | on | One worktree, branch and pull request per goal. |
| `work.auto_merge` | `"off"` | `"protected"` or `"always"` lets the loop merge a clean, reviewed PR. |
| `budget.max_iterations` / `max_minutes` / `max_tokens` | 20 / 480 / 100,000,000 | Per-run limits on goals, wall-clock time and spend. |
| `model_selection` | off | `"auto"` picks a model tier per goal (haiku, sonnet, opus or fable). |
| `ledger.enabled` | off | A shared, committed record of what each person's loop did. |
| `ledger.host_alias` / `salt_host_token` | `null` / `false` | Keep the host name hash out of ledger file names, or replace it with a label. |
| `session_release` | 15 and 30 minutes | Hands back goals a session claimed but never started. |

Every flag and its default: [configuration reference](docs/configuration.md). Run `/sigma-doctor` to
see which features are live for your config.

## Documentation

| Guide | Read it when you want to… |
|---|---|
| [Getting started](docs/getting-started.md) | start from a product vision, or upgrade a repo set up under the plugin's previous name |
| [Running the loop](docs/running-the-loop.md) | understand the seven phases, budgets, parking and model selection |
| [Running the loop unattended](docs/running-unattended.md) | leave it running overnight |
| [Backlog: local files or GitHub issues](docs/backlog.md) | use GitHub issues and a Projects board, priorities, blockers |
| [Label model](docs/label-model.md) | understand or hand-edit `sdlc:*` labels |
| [The dependency map](docs/dag.md) | see what waits on what, ask for a goal's chain, and read the shadow-to-on criterion |
| [Worktrees, PRs and merging](docs/worktrees-and-merging.md) | control how goals become pull requests and get merged |
| [Planning the backlog](docs/planning-the-backlog.md) | use `/sigma-scope` and `/sigma-triage` |
| [Action log and team ledger](docs/action-log-and-ledger.md) | trace what the loop did, or coordinate a team |
| [Parallelism](docs/parallelism.md) | run several goals or slices at once, and size it to your machine |
| [Knowledge graph](docs/knowledge-graph.md) | give the loop memory across goals |
| [Companion plugins](docs/companions.md) | the optional `code-review` plugin |
| [Managed settings](docs/managed-settings.md) | enforce settings across an organisation |
| [Zero-touch routines](docs/routines.md) | trigger the loop on a schedule |
| [What is new in 1.0.16](docs/whats-new-1.0.16.md) | see every change in this release and what changes by default when you upgrade |
| [Decision Rulebook](docs/decision-rulebook.md) | record decisions, earn autonomy levels, add hard stops, import past decisions |
| [Upkeep](docs/upkeep.md) | keep unit branches rebased on a 12-hour window, with backups |
| [Configuration reference](docs/configuration.md) | see every flag and default |
| [Features](docs/features.md) | see everything Sigma provides |
| [Invocation adapter](docs/invocation-adapter.md) | let a host you trust approve the loop's review post and relay phase ends (opt-in) |
| [What is enforced](docs/enforcement.md) | know which rules are checked by code and which are advice |
| [Architecture](docs/architecture.md) | see how the hooks, phases and loop fit together |
| [Platform support](docs/platform-support.md) | check what is validated on Claude Code, Codex and Cursor |
| [Measured results](docs/launch/evidence/measured-results.md) | see what the maintainers measured: cost, review, tests, CI (no comparison with other tools) |
| [Threat model](docs/threat-model.md) | review the security boundaries |

## Platform support

| Host | Status |
|---|---|
| Claude Code | Run end to end, including PRs and merges, by the maintainers on one machine class (macOS) with no outside users; see [Measured results](docs/launch/evidence/measured-results.md). |
| Codex | Partially validated: install and first loop command run in a real session; a full goal through to merge has not yet been measured. |
| Cursor | Experimental: scaffolding is unit-tested but has not been run in Cursor. |

CI runs the full suite on Linux with Python 3.10, 3.11, 3.12, and 3.13, and on macOS with Python 3.12. That full matrix runs weekly and on demand; a nightly run covers the suite minus its heaviest files on macOS Python 3.12; pull requests and merges to main run a short smoke set (`.github/smoke-tests.txt`) on one macOS Python 3.12 leg (results: [Measured results](docs/launch/evidence/measured-results.md)).
Details, including what each host has been measured to do: [docs/platform-support.md](docs/platform-support.md).

## Measured results and known limits

What the maintainers have measured, with the source file and date for each figure, is in
[Measured results](docs/launch/evidence/measured-results.md). It makes no comparison with any other tool.

Known limits:

- Running the loop unattended uses your host account; check your plan's terms for automated use. The vendor's
  documentation (<https://code.claude.com/docs/en/legal-and-compliance>, read 2026-10-12) recommends API-key
  authentication for developers building products or services on Claude and describes subscription sign-in as
  designed for ordinary use; this README makes no claim about what the terms permit.
- Budgets are per clone, and a supervisor relaunch after a budget stop starts with a fresh budget. One machine
  can therefore spend up to `SIGMA_SUPERVISE_MAX_RUNS` (default 48) times `budget.max_tokens` (default
  100,000,000 in the scaffolded config).

## Privacy and uninstalling

Sigma Loop does not report usage data. Its network behaviour, opt-in integrations and local data are
documented in [docs/privacy.md](docs/privacy.md). To remove it from a repository, follow the
[uninstall guide](docs/uninstall.md).

## Contributing, support and security

- Ask questions or report bugs through [SUPPORT.md](SUPPORT.md).
- Report security vulnerabilities privately as described in [SECURITY.md](SECURITY.md), never in a
  public issue.
- Development setup and rules are in [CONTRIBUTING.md](CONTRIBUTING.md). Outside contributions are
  not accepted yet; issues are welcome.
- Coexistence with the predecessor tool on one shared ledger is not claimed at launch; do not run
  both against the same ledger (tracked as #616, after launch).
- Release history is in [CHANGELOG.md](CHANGELOG.md).

## Credits

- **[code-review](https://github.com/anthropics/claude-plugins-official)** by Anthropic supplies the
  `/code-review` skill used in the Review phase. Optional companion.

## License

MIT: see [LICENSE](LICENSE). If this checkout carries a `NOTICE` file, it lists any path under a
different licence. The companion plugins have their own licences.

Claude Code and Claude are trademarks of their respective owners, as are Codex, Cursor and GitHub. Sigma Loop is an
independent project and is not affiliated with, endorsed by or sponsored by them.
