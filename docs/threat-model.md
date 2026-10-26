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
| T1 | Issue, PR, and comment prose enters goal prompts and parsers: `skills/sigma-loop/scripts/blocker_scan.py:139`, `skills/sigma-loop/scripts/comment_watch.py:88`, `skills/sigma-loop/scripts/features.py:356`, `skills/sigma-loop/scripts/review_context.py:50`, and `skills/sigma-loop/scripts/slack_commands_listen.py:524`. |
| T2 | Repository content enters commands through configured checks: `skills/sigma-loop/scripts/pipeline.py:68`, `skills/sigma-loop/scripts/backlog_check.py:810`, `skills/sigma-loop/scripts/loop.py:5222`, `skills/sigma-rebase/scripts/verify_merge.py:122`, and `skills/sigma-loop/scripts/reviewer.py:243`. |
| T3 | Host hooks execute at prompts and tool calls: `hooks/hooks.json:3`, repo adoption is checked by `hooks/sigma_gate.sh:20`, and session-start's watcher/doctor diagnostic layer begins at `hooks/session_start.sh:173`. |
| T4 | Network destinations are GitHub, Slack, a configured webhook, SMTP, and the host's model process, enumerated in `docs/privacy.md:7-15`. |
| T5 | Background boundaries include detached watcher startup `skills/sigma-loop/scripts/loop.py:85`, the watcher loop `skills/sigma-loop/scripts/watch_daemon.py:906`, Slack listener lifecycle `skills/sigma-loop/scripts/slack_commands_listen.py:757`, and model subprocesses `skills/sigma-loop/scripts/autowatch.py:923`. |
| T6 | Local policy comes from managed settings `skills/sigma-loop/scripts/managed_settings.py:237` and the direct-edit sentinel is described in `docs/enforcement.md:59`. |
| T7 | Model-spawned sessions include `skills/sigma-loop/scripts/feature_judge.py:155`, `skills/sigma-loop/scripts/autowatch.py:923`, and `skills/sigma-loop/scripts/supervise_daemon.py:1`. |

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
| TM-08 | T5 | Stale watcher appears idle (D) | A dead watcher reports no errors and is mistaken for an idle one. | `hooks/session_start.sh:173` | med | — |
| TM-09 | T5 | Listener cleanup omission (D) | SIGTERM or SIGHUP leaves Slack pid, heartbeat, and lock markers, preventing restart. | `skills/sigma-loop/scripts/slack_commands_listen.py:806` | low | — |
| TM-10 | T5 | Orphaned model descendant (D) | A timed-out, signalled, or SIGKILLed autowatch tick leaves a model descendant running. Residual: a descendant that calls `setsid` itself escapes the group, and a SIGKILL of the lifeline sentinel removes the backstop for a later SIGKILL of the tick. | `skills/sigma-loop/scripts/autowatch.py:773` terminates the driven session's whole process group (SIGTERM, grace, SIGKILL), a lifeline sentinel repeats that if the tick dies, and hosts without process groups refuse | low | #425 |
| TM-11 | T6 | Managed-policy deletion (T) | Removing the file makes an enrolled checkout read as unadopted. | `skills/sigma-loop/scripts/managed_settings.py:237` refuses an enrolled checkout whose file is gone, via markers outside the file. Residual: a checkout writer who also deletes the markers or runs `unenroll` is not bound (README, Managed settings) | med | — |
| TM-12 | T6 | Direct-edit bypass (T) | A local sentinel bypasses a hard plan gate when the key is not organization-locked. | `docs/enforcement.md:59` | med | — |
| TM-13 | T7 | Unbounded model spend (D) | A spawned model process spends beyond a sensible per-invocation budget. | `skills/sigma-loop/scripts/feature_judge.py:167` | low | — |
| TM-14 | T7 | Unsafe Slack merge (E) | `--unsafe-merge` removes interactive confirmation from a merge request. With unit upkeep off, a non-bot message in the authorized channel lands the unit with no approval; with it on, chat landing goes through the landing engine and needs the same local single-use unit approval, bound to the verified head, and the requester id is recorded. Residual: authority is channel membership alone, and an approval made locally cannot be tied to the person who then types the command. | `skills/sigma-loop/scripts/slack_commands_listen.py:1564` | med | — |
| TM-15 | T3 | Risky agent host configuration (E) | A host config grants every command, pipes a download into a shell, starts an unpinned MCP package, holds a literal credential in an `env` block, or turns the sandbox and approvals off. | `skills/sigma-doctor/scripts/host_config_scan.py:1` reads the project, user and installed-plugin configs of Claude Code, Codex and Cursor read-only and reports by file, rule and fixed fix text, never a value. Limits: report only, it does not block; prompt and skill prose are not scanned; the rule set is provisional. | med | — |

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

## Credential-shaped lines at `work.py commit`

`work.py commit` stages the goal worktree with `git add -A` and refuses on two axes before anything
reaches a branch. This section is the operator's reference for both. The loop agent's rule is shorter
and is printed by the refusal itself: remove the literal or park the goal, never write an allowlist
entry.

**Secret-shaped names (#1555).** The name refusal covers `.env` and its variants,
`*.pem`/`*.key`/`*.p12`/`*.pfx`/`*.jks`/`*.keystore`, `id_rsa` and its siblings, `credentials.json`
and service-account JSON. The public half of a keypair and a staged deletion are never refused. A
repo that already ignores its `.env` never sees this refusal, because `git add -A` honours
`.gitignore`; `/sigma-doctor` reports the still-unignored ones before the loop's first commit.
`work.allow_secret_paths` clears an exact NAME, never content.

**Added content.** Every added staged line is matched against the commit shapes in `scrub.py`. For
the anchored `credential-assignment` rule (a credential-named key, then `:` or `=`, then a run of 4
or more characters) a gate-only filter (#961) judges the value, failing closed. The value it judges
is the rule's own run, which ends at the first space, quote, `<`, `>` or `&`; anything after that run
is judged only by the three line-wide checks below (a secret-like token, an assigning `=` that starts
a literal, a statement that continues onto the next line):

- Refused in every file: a quoted value directly after the separator (a plain `"` or `'`: see the
  caveat below); a bare identifier or a
  number as the whole value; a `${...}` or `$(...)` template; any line with an assigning `=` after the separator whose
  right side starts a quoted literal of any length or wraps onto the next line (an annotated,
  chained or keyword-argument default); any line with a secret-like token after the separator
  (a known provider prefix, 3 or more letter/digit alternations, a run of 24 or more letters and
  digits, or 6 or more characters mixing letters and digits outside an upper-snake name); and any
  line whose statement continues past the value onto the next line, where a literal has no key and
  no rule sees it. A statement continues when, after the separator, an opener `(`, `[` or `{` is
  left open (a closer of another kind closes nothing), a quoted literal is left open at the end, or
  the code ends, before a `#`, `//` or `/*` comment, in `\`, `=`, `?` (so `??`), `||`, `&&`, `+`,
  `or`, `and`, `if`, `else`, `do` (with or without `|<params>|`), or a `:` after a `?` (a ternary or
  `?:`). A trailing `,`, `.`, `!`, lone `|` or bare `:` does not count: each ends an argument, a
  docstring sentence, a Swift, Kotlin or TypeScript statement, a Markdown table row or a Python
  block opener.
- Passes in a code file only: an expression (a call, an attribute or subscript read) and, after
  `:`, a type name from a closed set (`str`, `bytes`, `bool`, `int`, `float`, `Any`, `SecretStr`,
  `string`, `boolean`, `number`, `any`, `unknown`, `String`, or a `|` union of them with `None`,
  `null` or `undefined`).
- Passes in every file: a whole boolean or null word (`True`, `False`, `true`, `false`, `None`,
  `null`, `nil`, `undefined`) and Sigma's own redaction markers (`[REDACTED]`, `[REDACTED:<rule>]`).

A code file is one whose basename has a stem and whose last suffix, compared case-sensitively, is in
`scrub.COMMIT_CODE_SUFFIXES`: Python, JavaScript and TypeScript, the JVM languages, Go, Rust, Ruby,
PHP, C#, Swift, Dart, C and C++, Objective-C, Lua and Elixir. Every other file (config, data, docs,
a suffix nobody listed, an upper-case variant) reads an unquoted value as a literal.

The quoted-value refusal reads a plain `"` or `'` directly after the separator only. A prefixed or
triple-quoted string (`f"..."`, `b"..."`, `r"..."`, `"""..."""`, `'''...'''`), a parenthesised
`= ("...")`, Go's `:=`, Ruby's and PHP's `=>`, C#'s `@"..."` and Rust's `: &str = "..."` never reach
the anchored rule or its quoted twin, before #961 or after: a 32-character hex value in each passed
the gate before #961 and this one alike (measured, each in its own language's file). Only the other
shapes in the table judge those lines.

The continuation check costs little, measured over every line #961 exempts, against this filter
without the check: 0 of 30 in this repository, 2 of 91 in the Python 3.13 standard library, 0 of 36
in the global npm modules and 23 of 1,442 in a `site-packages` tree are refused again. The gate
before #961 refused every one. All but one is a wrapped call or subscript; the one is a C++ block
opener (`if (auto <key> = weak.lock()) {`), refused because a `{` cannot be told from a Ruby, Kotlin
or Swift block whose body is the default.

**Residual misses, measured against this filter.** In a code file, once the value's first word is
an expression or a closed-set type name, a quoted literal anywhere later on the line passes when none
of its tokens is secret-like and no assigning `=` starts it, whatever its total length. The old gate
refused each of these, and each passes in a `.py` and a `.ts` file: a positional call argument or a
getenv default (`<key> = os.getenv("<NAME>", "<words>")`, `<key> = SecretStr("<words>")`), a
fallback after `||`, `??` or `or` (`<key> = process.env.X || "<words>"`), a concatenation
(`<key> = cfg.prefix + "<words>"`), a ternary branch (`<key> = cfg.x ? "<words>" : y`) and a block
default such as Ruby's `<key> = ENV.fetch("<NAME>") { "<words>" }`. So does an unquoted dotted value
made only of expression characters.
The check judges tokens, not the literal: the `[A-Za-z0-9_]+` runs of the line and the
whitespace-separated words of each quoted literal, and its length clause needs an unbroken run of 24
letters and digits. So a value with no digit is never secret-like without a provider prefix or such a
run, and a value broken by spaces, hyphens or other punctuation is judged in short pieces. On 2,000
random unbroken letter/digit runs per length (seed 961, a fresh generator per length, each character
drawn uniformly from the 62 ASCII letters and digits), a run placed as a getenv default in a `.py`
file was refused 64.0% of the time at 6 characters, 74.2% at 8, 91.4% at 14, 96.8% at 20 and 100% at
24, 32 and 64; every miss is a run with no digit at all or an upper-snake run with fewer than 3
alternations. That 24-character bound
holds for an unbroken run only. Measured on this filter at the token boundary (2,000 random values
per shape, fixed seed, `scrub.commit_secret_hits` with an `s.py` path; the gate before #961 refused
all of them): a passphrase of 3 to 8 dictionary words (13 to 70 characters) joined by spaces, and one
joined by hyphens, each passed 100% of the time as a getenv default and as a `SecretStr(...)`
argument; a no-digit value of 8 to 40 letters and symbols passed 99.8%, refused only when it held an
unbroken run of 24 or more letters; the same symbol values with one digit added passed 0%. Quoted
directly after the separator, the hyphen-joined and symbol values were refused 100% and the
space-joined ones 98.5%: the 1.5% open with a word under 4 characters, which the rule's own run never
matched, before #961 or after. A literal on the NEXT line is judged only through its key line, and
one line cannot show a continuation that starts on the next one. So a key line that looks complete
passes even when the next line continues it with a leading operator or an indent: Kotlin's
`?: "<hex>"` under `<key> = System.getenv("<NAME>")`; the `or "<hex>",` that Black 26.1 starts on its
own line under a wrapped keyword argument `<key>=os.environ.get("<NAME>")` (measured); a leading-dot
chain; a key line ending in `.` (not counted, above); and a YAML plain scalar continued on a
more-indented line (`<key>: null`, then `  <hex>`, loads as the one string `null <hex>`, measured with
PyYAML). The gate before #961 refused each of those key lines only because it refused every value of
4 or more characters. A plain `<key> = (` or `<key> = """` never matched the rule (its run is under 4
characters or opens with a quote), so its next-line literal passes, before #961 or after. Closing
these misses is tracked by #978. Provider rules
(`aws-key`, `gh-token`, `stripe-key` and the rest) still fire on their own shapes and have no
allowlist: a synthetic value that matches one parks the goal.

**Further misses, found in review and confirmed against this filter.** The old gate refused each of
these, and each follows from judging only the first word as the value. In every file, a boolean or
null first word, or one of Sigma's redaction markers, passes with a quoted literal later on the line
(`<key> = None or "<letters>"`, `<key> = True if x else "<letters>"`, `<key> = null ?? "<letters>"`,
`<key> = [REDACTED] or "<letters>"`), and so does YAML's `<key>: true <letters>` or
`<key>: [REDACTED] <letters>`. In a code file, an annotated default passes whatever unquoted number or
identifier it assigns, when the annotation is an expression or a closed-set type of 4 or more
characters (`<key>: Optional[int] = <8 digits>`, `<key>: float = <digits>`; a shorter annotation
never reached the old rule either). The trailing value is still judged by the secret-like check:
on 500 random runs per length, a letters-only trailing literal and a digit-only annotated default
each passed 99-100% of the time at 4 to 23 characters and 0% at 24, and a trailing literal mixing
letters and digits passed only when a draw held no digit (8% at 8 characters, 0% at 16). Those
bounds, too, hold for an unbroken run only: after `<key> = None or`, the space- and hyphen-joined
passphrases and the no-digit symbol values measured above passed 100%, 100% and 99.8% in a `.yml`
file and in a `.py` file alike (2,000 each, the old gate refused all), at every length drawn.

**The allowlist, `work.allow_secret_content`.** A line that is still refused and holds no credential
is cleared only by an entry in the MAIN checkout's `.sdlc/config.json`, and an entry is an OPERATOR
ruling: a loop agent never writes one itself, attended or not. The agent removes the literal, reads
the value from the environment, or parks the goal with `record parked "<why>"`, in the order the
refusal prints. Where `.sdlc/config.json` is tracked, the operator's edit leaves the main checkout
dirty, so #954's dirty-root check refuses the next `work.py start` (with `work.base` set and the main
checkout on it) until the edit is committed. Two entry forms, narrowest first:

- `{"rule": "credential-assignment", "line_sha256": "<hash>", "reason": "<why>"}` clears that one
  line, in any file, until the line changes. The refusal prints the command that hashes the goal
  worktree's copy without showing the line, shaped like
  `python3 <sigma>/skills/sigma-loop/scripts/work.py line-hash <worktree>/<path> <line>`. The hash
  is the sha256 of the stripped line; a CRLF line hashes as its LF twin, and bytes that are not
  UTF-8 decode to U+FFFD on both sides.
- `{"rule": "credential-assignment", "path": "<exact repo-relative path>", "reason": "<why>"}` is
  WIDE: it clears every current and future `credential-assignment` hit in that file, a real literal
  added there later included, so it is for a file of fixtures only.

An entry counts only when its `rule` is exactly `credential-assignment` (no wildcard, no provider
rule), its `reason` is a non-empty string, and it carries exactly one of `path`, compared exactly
with git's raw repo-relative path (a leading `./` or another letter case never matches), or
`line_sha256`, 64 lowercase hex. Anything else is ignored, never a crash and never a bypass, and the
refusal says how many entries it ignored. The refusal never prints a line hash.

**What this does not guarantee.** `work.py commit` reads the MAIN checkout's `.sdlc/config.json`, not
the goal branch's, so an entry is visible in review only where that file is committed; a repository
that ignores `.sdlc/` reviews no entry at all. The operator-only rule is prose: nothing in code can
tell an operator's edit of that file from an agent's. The stored hash is an unsalted sha256 of a line
that is being committed anyway.
