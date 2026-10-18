---
name: sigma-chain
description: Solve one end goal and every open prerequisite it waits on, in order, with all decisions asked up front. Use for 'solve #N', 'do whatever #N needs' or /sigma-chain.
allowed-tools: Bash(python3 *)
---

## Codex path resolution

Codex has no `CLAUDE_SKILL_DIR`. In each command, replace `${CLAUDE_SKILL_DIR}` with the
absolute directory of this installed `SKILL.md` (shown in the skill catalog); never run an
empty path. Claude keeps its provided value.

# sigma-chain

Detailed selection triggers: [selection](references/selection.md). Full reference: [chain](../../docs/chain.md).

Arguments: `<N | issue URL of this repository>`, `--longest`, `--dry-run`, `--cap-usd N`, `--parallel N`,
`--no-grow`, `--defaults`, `status`, `resume`, `stop`. `C` below is
`python3 "${CLAUDE_SKILL_DIR}/../sigma-loop/scripts/chain.py"`; `D` is the repository's `.sdlc` directory.

1. **Identify.** With `--longest`, run `C plan D --longest` and offer its top three end goals. Otherwise
   `C plan D <N>` (read-only toward GitHub). A refusal (cycle, over the member bound) is relayed as is.
2. **Ask.** `C sheet D <N>` lists every decision at once. Put them to the user in batches of up to four
   (`AskUserQuestion` where the host has it, a numbered text reply otherwise); record them with
   `C answer D <N> --answers <file>`. `--defaults` skips asking: `C answer D <N> --defaults` takes each
   recommended option, and the cap has none, so it is still asked. Then `C approve D <N>`.
3. **Arm.** `C arm D <N> --dry-run` shows the writes; `C arm D <N>` makes them. With `--dry-run`, stop here.
4. **Solve.** Call `C run D <N>` again until it reports done. Exit 3 is capped or stuck, with one reason:
   relay it, never work around it. `C stop D <N>` and `C resume D <N> [--cap N]` change only the local record.
5. **Report.** Relay what `run` printed: claimed, added, closed, and why it stopped.

Not available yet: `status` (use `C sheet D <N>` and the last `run` line), `--parallel`, `--no-grow`, and
`--cap-usd` as a flag (set `chain.max_usd` in config; see the reference). Say so; do not emulate them.
