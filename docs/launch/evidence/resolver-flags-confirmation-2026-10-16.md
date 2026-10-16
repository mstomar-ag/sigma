# Resolver flags: confirmation against the installed CLI (16 October 2026)

This note moves the design flags that `docs/launch/evidence/resolver-cli-probe.md` probed from "unverified" to "confirmed" in `feature_upkeep_launcher.py`. It adds no new probe of its own.

## What was checked

- The probe record (`resolver-cli-probe.md`, the earlier build) used each flag in one real headless call.
- This change then ran `claude --help` only (the installed build, macOS) and looked for each flag in the listing. No prompt was sent and no model was called.

| Flag | In `claude --help` ((installed build)) | Probe result ((earlier build)) |
|---|---|---|
| `-p` / `--print`, `--output-format`, `--model` | listed | in use in the tree; probed |
| `--max-budget-usd` | listed | works; a runaway guard checked between turns, not a hard ceiling |
| `--permission-mode`, `--allowedTools`, `--disallowedTools`, `--tools` | listed | works |
| `--settings`, `--setting-sources`, `--strict-mcp-config` | listed | works |
| `--no-session-persistence`, `--add-dir`, `--append-system-prompt` | listed | works |
| `--bare` | listed | does NOT authenticate with the subscription login; stays refused by name |

## What this does not show

- The help listing shows that a flag exists, not what it does. Behaviour rests on the earlier probe, one macOS machine, single observations.
- Nothing was measured on Windows or Linux, and the probe was run on (earlier build), not (installed build).
- The live end to end run of the agent level against a real model has NOT been done. It is owner-gated: it needs an explicit go-ahead for the spend, and until it is run the level is tested only against a stand-in command in the test suite.
- The optional caps (null = no limit) mean an unconfigured level is bounded only by `timeout_minutes` per run. Whether a subscription login is billed for such a run was not measured.
