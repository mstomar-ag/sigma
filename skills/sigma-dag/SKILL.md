---
name: sigma-dag
description: "Report the stored dependency map: state, stuck and active chains, cycles. Use for map questions or /sigma-dag."
argument-hint: "[<N> | --feature <unit>] [--html]"
allowed-tools: Bash(python3 *)
---

## Codex path resolution

Codex has no `CLAUDE_SKILL_DIR`. In each command, replace `${CLAUDE_SKILL_DIR}` with the
absolute directory of this installed `SKILL.md` (shown in the skill catalog); never run an
empty path. Claude keeps its provided value.

# sigma-dag

Triggers: [selection](references/selection.md). `D` is `python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/dag.py"`.

- No argument: run `D report --sdlc .sdlc`, relay stdout verbatim. Stored map only (no network, no write).
- `<N>`: `D report --goal <N> --sdlc .sdlc`, the chain behind N.
- `--html`: `D titles --sdlc .sdlc` then `D html --all --sdlc .sdlc`; with `<N>`, `D html --goal <N>` instead (it also draws the open work waiting on N, muted). Print the path it names and open it where the host can.
- `--feature <unit>`: `D report --feature <unit> --sdlc .sdlc`, the unit's open members and their open blockers, outside ones labelled; with `--html`, `D titles` then `D html --feature <unit>` instead.

One line with a fix (`rebuild`, `refresh --force`, the config key) is the answer when there is nothing to read: relay it, run nothing else.
