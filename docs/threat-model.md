# Threat model

Sigma runs hooks and scripts on developer machines, can read and write GitHub
state, and can start optional background workers. This model states what it
protects, where untrusted data enters, and the mitigation actually present at
the cited source location. It is a security inventory, not a claim that every
risk has been eliminated.

## Assets

1. The user's source code and git history.
2. GitHub state: issues, PRs, branches, board, labels.
3. Credentials: the `gh` token, `SIGMA_SLACK_BOT_TOKEN`, `SIGMA_SMTP_PASS`, and
   the host's model credentials.
4. The developer machine: processes and files.
5. The user's model spend.

## Trust boundaries

| boundary | crossing and entry points |
| --- | --- |
| T1 | Issue, PR, and comment prose enters goal prompts and parsers: `skills/sigma-loop/scripts/blocker_scan.py:130`, `skills/sigma-loop/scripts/comment_watch.py:88`, `skills/sigma-loop/scripts/features.py:356`, `skills/sigma-loop/scripts/review_context.py:49`, and `skills/sigma-loop/scripts/slack_commands_listen.py:524`. |
| T2 | Repository content enters commands through configured checks: `skills/sigma-loop/scripts/pipeline.py:68`, `skills/sigma-loop/scripts/backlog_check.py:791`, `skills/sigma-loop/scripts/loop.py:5222`, `skills/sigma-rebase/scripts/verify_merge.py:122`, and `skills/sigma-loop/scripts/reviewer.py:239`. |
| T3 | Host hooks execute at prompts and tool calls: `hooks/hooks.json:3`, repo adoption is checked by `hooks/sigma_gate.sh:20`, and session-start's watcher/doctor diagnostic layer begins at `hooks/session_start.sh:131`. |
| T4 | Network destinations are GitHub, Slack, a configured webhook, SMTP, and the host's model process, enumerated in `docs/privacy.md:9`. |
| T5 | Background boundaries include detached watcher startup `skills/sigma-loop/scripts/loop.py:75`, the watcher loop `skills/sigma-loop/scripts/watch_daemon.py:906`, Slack listener lifecycle `skills/sigma-loop/scripts/slack_commands_listen.py:757`, and model subprocesses `skills/sigma-loop/scripts/autowatch.py:915`. |
| T6 | Local policy comes from managed settings `skills/sigma-loop/scripts/managed_settings.py:237` and the direct-edit sentinel is described in `docs/enforcement.md:54`. |
| T7 | Model-spawned sessions include `skills/sigma-loop/scripts/feature_judge.py:155`, `skills/sigma-loop/scripts/autowatch.py:915`, and `skills/sigma-loop/scripts/supervise_daemon.py:1`. |

## Threats and current mitigations

`none` means no current technical mitigation at this crossing. Medium and high
`none` rows point to an open GitHub issue; those issues are implementation work,
not evidence that the risk is already fixed. STRIDE letters are S (spoofing), T
(tampering), R (repudiation), I (information disclosure), D (denial of service),
and E (elevation of privilege).

| id | boundary | threat (STRIDE letter) | example | existing mitigation (file:line) or `none` | residual risk low/med/high | issue |
| --- | --- | --- | --- | --- | --- | --- |
| TM-01 | T1 | Prompt injection (E) | An issue asks a phase agent to execute a command or edit labels. | none | high | #362 |
| TM-02 | T1 | Phantom blocker write (T) | A confident `_BLOCK_RE` match writes a blocker edge to a third issue. | none | high | #362 |
| TM-03 | T1 | Parser confusion (T) | A hostile `Feature:` marker or Slack argument changes routing. | none | med | #362 |
| TM-04 | T2 | Repository-supplied value reaches a process or a file (E) | Any repository-supplied value that reaches a process or a file: a configured shell string or executable (`verify.command`, `review.command`, `knowledge_graph.builder`, `autowatch.drive_cmd`, `.sdlc/risk-detect.conf`), a git remote or branch that becomes an option (`work.remote`, `work.base`, `ledger.remote`, `ledger.branch`), an SMTP host or password variable (`notify.email`), and a committed symlink under `.sdlc/` that a write follows (#708). | `shell_policy.py` refuses repository-configured commands until an operator sets the Git-local opt-in at every sink above; a leading `-` in a git remote or branch is refused; `state.safe_state_open` refuses symlinks under `.sdlc/`; repo SMTP needs `SIGMA_ALLOW_REPO_SMTP=1` | med | #422, #707, #708, #710 |
| TM-05 | T3 | Hook-triggered command execution (E) | A host invokes a configured hook on a prompt or tool call. | `hooks/sigma_gate.sh:20` | med | — |
| TM-06 | T4 | Credential exposure in logs (I) | A token-shaped value reaches a command or comment diagnostic. Coverage of every `gh` comment site is unverified. | `skills/sigma-loop/scripts/scrub.py:150` | med | — |
| TM-07 | T4 | Remote webhook egress (I) | `channel_webhook_url` names an Internet host instead of the documented local adapter. | none | high | #358 |
| TM-08 | T5 | Stale watcher appears idle (D) | A dead watcher reports no errors and is mistaken for an idle one. | `hooks/session_start.sh:131` | med | — |
| TM-09 | T5 | Listener cleanup omission (D) | SIGTERM or SIGHUP leaves Slack pid, heartbeat, and lock markers, preventing restart. | `skills/sigma-loop/scripts/slack_commands_listen.py:806` | low | — |
| TM-10 | T5 | Orphaned model descendant (D) | A timed-out, signalled, or SIGKILLed autowatch tick leaves a model descendant running. Residual: a descendant that calls `setsid` itself escapes the group, and a SIGKILL of the lifeline sentinel removes the backstop for a later SIGKILL of the tick. | `skills/sigma-loop/scripts/autowatch.py:772` terminates the driven session's whole process group (SIGTERM, grace, SIGKILL), a lifeline sentinel repeats that if the tick dies, and hosts without process groups refuse | low | #425 |
| TM-11 | T6 | Managed-policy deletion (T) | Removing the file makes an enrolled checkout read as unadopted. | `skills/sigma-loop/scripts/managed_settings.py:237` refuses an enrolled checkout whose file is gone, via markers outside the file. Residual: a checkout writer who also deletes the markers or runs `unenroll` is not bound (README, Managed settings) | med | — |
| TM-12 | T6 | Direct-edit bypass (T) | A local sentinel bypasses a hard plan gate when the key is not organization-locked. | `docs/enforcement.md:54` | med | — |
| TM-13 | T7 | Unbounded model spend (D) | A spawned model process spends beyond a sensible per-invocation budget. | `skills/sigma-loop/scripts/feature_judge.py:167` | low | — |
| TM-14 | T7 | Unsafe Slack merge (E) | `--unsafe-merge` removes interactive confirmation from a merge request. With unit upkeep off, a non-bot message in the authorized channel lands the unit with no approval; with it on, chat landing goes through the landing engine and needs the same local single-use unit approval, bound to the verified head, and the requester id is recorded. Residual: authority is channel membership alone, and an approval made locally cannot be tied to the person who then types the command. | `skills/sigma-loop/scripts/slack_commands_listen.py:1564` | med | — |

## Operational reading

The mitigations above are deliberately narrow. For example, `scrub.py` redacts
known patterns but does not prove that every GitHub comment call is scrubbed;
watcher restart and stale detection reduce a liveness gap but do not make a
stopped process healthy. The linked follow-ups retain the owning acceptance
criteria and must be independently verified before this document can describe
their mitigation as present.

## Repository-configured shell commands

Pipeline checks, backlog embedders, and verification commands are repository
input. Sigma refuses them by default, including in a freshly cloned or adopted
checkout. After inspecting a project an operator may enable the trusted-project
compatibility path with:

```sh
git -C <trusted-project> config --local sigma.allowRepositoryShellCommands true
```

The setting is Git-local rather than `.sdlc/config.json`, so it is neither
committed nor supplied by a clone. It intentionally restores shell semantics
for every linked worktree of that trusted project; do not set it for a checkout
whose repository configuration you have not reviewed.

`/sigma-init`'s explicit `verify_detect.py confirm` and `set` gestures record
the same Git-local setting only after the operator confirms or supplies the
command. They refuse to enable a command outside a Git worktree. `decline` and
initial scaffolding never grant it.

## The conflict resolver session

The upkeep conflict resolver (`conflicts.resolve: agent`) is a deliberate, capped, opt-in headless model session, launched
by `skills/sigma-loop/scripts/feature_upkeep_launcher.py`. It is off by default and the library is imported by nothing
while the gate is closed. Its confinement: the environment is built from nothing (no repository-location variables, no
inherited credential; a credential is passed only when the operator names one, and only to the realpath-verified binary);
the prompt goes on stdin with untrusted text in nonce-delimited blocks; the directory must not sit under an ancestor holding
an instruction file and must be outside the real home and the repository; the process group is killed at a wall-clock cap;
every file in the directory is hashed before and after, and any change outside the conflicted set discards the result and
still charges it. A killed or unmetered run is charged the full per-run cap. Only flags in the launcher's confirmed-flag
table are passed; every other design flag is UNVERIFIED and refused. The team spend ceiling is eventually consistent (N
clones can overshoot by up to N - 1 times the per-run cap). Nothing here was run against the real command line: the
transcript location and format are UNVERIFIED, and an unreadable transcript is charged the cap.
