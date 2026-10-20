---
name: sigma-dag
description: Report the stored dependency map: state, stuck and active chains, cycles. Use for map questions or /sigma-dag.
allowed-tools: Bash(python3 *)
---

## Codex path resolution

Codex has no `CLAUDE_SKILL_DIR`. In each command, replace `${CLAUDE_SKILL_DIR}` with the
absolute directory of this installed `SKILL.md` (shown in the skill catalog); never run an
empty path. Claude keeps its provided value.

# sigma-dag

Detailed selection triggers: [selection](references/selection.md). `D` is `python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/dag.py"`.

- No argument: run `D report --sdlc .sdlc` and relay stdout verbatim. It reads the stored map only (no network, no write).
- `<N>`: run `D report --goal <N> --sdlc .sdlc`, the chain behind N.
- `--html`: run `D html --all --sdlc .sdlc`; `<N> --html`: `D html --goal <N> --sdlc .sdlc`. Print the path it names (the index, or the page) and open it where the host can.

One line with a fix (`rebuild`, `refresh --force`, or the config key) is the answer when there is nothing to read: relay it, run nothing else.
