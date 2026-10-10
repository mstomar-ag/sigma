# Running the loop unattended

Status: what was observed, not a guarantee. This page records the permission setup that an unattended
(`claude -p`, overnight) run needs, as found in one GitHub-mode drill on a scratch repository (tracked
as #721). The loop itself is the same product the predecessor shipped; the permission behaviour below is
inherited from it and is not new in Sigma.

## What goes wrong

The loop skills tell the agent to pass its own process id with a shell expansion (`--session-pid "$PPID"`).
In headless `acceptEdits` mode the permission layer cannot analyse that expansion, so the call is denied.
In the drill, 3 of 5 loop runs worked around it and 2 stopped with nothing done.

## What to do today

- **Supervised runs (recommended):** use the normal permission mode and approve the loop's commands once
  with "always allow". You will see prompts for the first goal and few after that.
- **Headless runs:** pre-allow the loop's own commands, `Bash(python3 *)`, `Bash(git *)` and `Bash(gh *)`,
  in the project's `.claude/settings.local.json` (a gitignored, per-machine file), and pass a literal
  numeric pid for `--session-pid` instead of `"$PPID"`. Run it on a scratch or trusted repository first.
- **Never** add a wildcard delete such as `Bash(rm -rf *)` to make prompts go away. A narrow rule scoped to
  a scratch directory is the safe form.
- A repository you did not write can carry its own `.sdlc/config.json`. Sigma now refuses repository-supplied
  commands, git options and symlinked state unless you opt in (see `docs/threat-model.md`), but do not run
  an unattended loop on a repository you do not trust.

## What is not done yet

Defaulting the process id so no shell expansion is needed, and a test that no documented command needs one,
are open in #721. Until then the literal-pid form above is the workaround.

## The conflict resolver session

Separate from the permission setup above: with `conflicts.resolve: agent` the upkeep job may start one headless
`claude -p` session to resolve a unit conflict (`skills/sigma-loop/scripts/feature_upkeep_launcher.py`). It is off by default,
so the kit starts no unattended model session unless that setting is on. The shipped model catalog holds only a placeholder id,
so the resolver stays closed until a validated override is supplied. The launcher passes only `-p`, `--output-format` and
`--model` (alias form), flags already used in the tree and not probed by this change; every other design flag
(`--max-budget-usd`, `--permission-mode`, `--allowedTools`, `--disallowedTools`, `--tools`, `--settings`,
`--setting-sources`, `--strict-mcp-config`, `--no-session-persistence`, `--add-dir`, `--bare`, `--append-system-prompt`) is
UNVERIFIED and refused. Also UNVERIFIED: the variable that moves the configuration directory and the place and shape of
the session transcript. The ledger guard keeps 10,000 entry files as its ceiling of record; the working limit is derived
from a timed sample against a time budget. Measured on one warm local disk: 0.2 s at 1,000 files, 0.5 to 1.8 s at 10,000;
cold and network disks were not measured.
