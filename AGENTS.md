# Agent rules — Sigma

These are the rules an agent (or a person) follows when changing Sigma itself. `CLAUDE.md`
imports this file; other hosts read it directly.

## The five properties every design decision is judged against

Sigma is installed across whole organisations' developer machines. That makes five properties
non-negotiable. They are not aspirations to weigh against velocity — a change that breaches one is
wrong, however elegant, and should be reworked or refused.

**RELIABILITY — it does what it claims, and its claims are measured.**
No capability is described as working until it has been run end to end in the real target
environment. A row count is not a timing; a shared counter's delta is not attribution; a green
status file is not freshness. Every performance or resource claim carries the measurement that
produced it. If it was not measured, say so in the same sentence.

**SCALABILITY — it holds as machines, repos and data grow.**
Anything with a per-item cost states how it behaves at 10x and 100x. Serialised chokepoints
(a global lock, a single writer, an unindexed scan) are named as ceilings with a number attached,
not left to be discovered by a user. Unbounded growth — a table nothing prunes, a cursor that
never advances — is a defect at design time, not an operational surprise.

**RESILIENCY — it recovers without a human.**
Every failure mode has a stated recovery: outage, crash, reboot, restore-from-backup, partial write,
lost ack. Recovery must not itself be an outage — a "re-ship everything" lever that stalls the fleet
is not recovery. Retries are idempotent or explicitly bounded; state that cannot self-heal has a
documented lever, and that lever is polite.

**SAFETY — it cannot harm the host, the data, or the user's trust.**
Resource limits are DERIVED from the machine, never constants copied from a developer's laptop.
Nothing spawns background processes, sends data, or consumes quota without the operator opting in.
Secrets never enter config files, logs, or shell history. Where a guarantee cannot be made on a
platform, the code REFUSES loudly rather than proceeding weakly — a silent half-guarantee is worse
than a clear "not supported here".

**LIVENESS — it is running when it should be, and you can tell.**
Every long-running component reports its own health where a human will see it, and a component that
has DIED must be distinguishable from one with nothing to do. Age is the tell, not error state: a
background pipeline can stop for days while reporting zero errors the whole time. Revival is
layered and host-agnostic — no single trigger is trusted, so a missed trigger costs latency, never
data.

### Rules that follow from all five

**Host-agnostic or it does not ship.** `docs/output-contract.md` opens "Host-agnostic. Applies to
Claude Code, Cursor, Codex, or any future agent host", and that governs mechanism, not just prose.
Cursor has NO hooks — its `.mdc` is text the model reads — so a Claude Code hook may be an
accelerator, never load-bearing. Triggers and lifecycles live in Sigma's own Python, or in git,
which every host shares.

**Run the control, or the check is decoration.** Every guard is deliberately broken once and seen to
fail before it is trusted. Defects that inspection cannot see — a guard that passes against the very
bug it targets, a lock test that hangs rather than fails — surface only this way. A test never seen
red proves nothing.

**And run the control on the gesture the DOCS give, not a stronger one.** A guard whose documented
invocation cannot fail is decoration with a passing test: its tests can pass a flag that makes them
go red correctly while the invocation the docs prescribe omits that flag and cannot fail on any
input. Copy the gesture out of the docs, run THAT, and see it red.

**A port across a performance boundary can silently disarm a probabilistic control.** When a change
moves probabilistic-concurrency-under-test across a performance boundary (shell-forking ->
in-process Python, or vice versa), the plan phase must measure the old test's sensitivity against
the new code before trusting its red/green — a test that cannot fail against broken code is
decoration regardless of how it behaved before the port. A race test that reliably catches a
removed mutex in a forking shell script can catch nothing against a straight in-process port,
because the window it races against collapses by orders of magnitude. Where sensitivity has
collapsed, add a deterministic in-process control at the exact seam, and demote the original test
to a labeled smoke test (state so in its own docstring) rather than silently keeping it as the
system of record.

**No unsupervised SIGSTOP.** Pausing a `--watch` daemon with `kill -STOP` to dodge a lock conflict
is not a fix, it is an undocumented, impolite lever — exactly what RESILIENCY already forbids. A
script whose cleanup trap never fires (because the shell running it was killed first) leaves the
daemon stopped for hours. If a pause-based workaround is ever genuinely justified, it does not ship
without a supervisor that cannot leave the daemon orphaned-stopped (a heartbeat, a max-pause
timeout, or process-group-scoped signal handling that survives the pausing script's own abnormal
exit) — no such supervisor exists in this codebase today, so today the answer is: don't.
`tests/test_no_unsupervised_process_pause.py` enforces this structurally.

## Before you run the loop

If the installed Sigma plugin is older than 1.0.0, do not start the loop; run
`/sigma-doctor` and update the installed plugin first. The exact update gesture and
its failure modes are in [agent-rules detail](docs/agent-rules-detail.md).

## Labels

Read [the label model](docs/label-model.md) before writing any `sdlc:*` label.
`sdlc:goal` is membership; in-progress/blocked/blocking are overlays; parked stands
alone (the old needs-confirmation label is legacy: nothing writes it unless triage is off).
Prefer the atomic promote/unpark gestures. See
[agent-rules detail](docs/agent-rules-detail.md) for the operational nuances.

## The Dossier pipeline — where work comes from

Start with [the walkthrough](docs/how-the-dossier-pipeline-works.md), then read
[the contract](docs/dossier-pipeline.md) before `/sigma-dossier`,
`/sigma-goal-design`, `/sigma-goal-review`, hand-writing a story/epic body, or
enabling `goal_design`. Dossiers and Spec/Epics are upper tiers and
never `sdlc:goal`; confirmed slices are ordinary goals. The retrofit gate is
OFF by default. `sdlc:designed` has one writer and exactly one reader:
goal-review writes it only on CONFIRM; design-check reads it. A hand-written
`#N` can create a blocker edge, so run the phantom-blocker control. Full
stage and rejection rules are in [agent-rules detail](docs/agent-rules-detail.md).

## Branches and units of work

Read [the walkthrough](docs/how-branching-works.md) and
[branching contract](docs/branching-model.md) before writing a `Feature:`
marker, `feature:` label, or `.sdlc/features/` entry. An issue declares its unit
in both label and body (body wins); labels must already exist. The classifier
attaches a unit and work-start syncs registry membership; later goal triggers
repair narrow membership gaps. Registry index is derived. Full precedence,
fallback, and recovery rules are in [agent-rules detail](docs/agent-rules-detail.md).

## Nobody commits directly to a feature branch

> **Nobody commits directly to a feature branch. All work reaches it through `sdlc/*` goal
> branches.**

`feature_rebase.py` detects some direct commits before a force-push; it never
prevents the commit. Only host branch protection can prevent it, but ordinary
non-fast-forward protection turns this detection off unless you grant upkeep's actor a bypass.
A branch not behind its base is never checked — and the skip is SILENT:
it returns `current`, `clause()` returns the empty string, and the pick line says nothing.
`merge_method: rebase` cannot distinguish landed from direct
commits. See [branching-model §3/§15](docs/branching-model.md) and
[agent-rules detail](docs/agent-rules-detail.md) for further blind spots.

## Watch-loop timeouts and session-start staleness

`skills/sigma-loop/scripts/watch_daemon.py`'s tick-loop subprocess calls (`sync.py pull`, `watch.py`,
`agent_watch.py`, `comment_watch.py`, `reconcile_tick.py`, `channel_notify.py`, `drift_tick.py`,
`sync.py publish`) run through `run_with_timeout.py`, which kills the **whole process group** —
not just the direct child — on overrun, so a hung call (or anything it spawned) is killed and logged
instead of freezing the whole tick and making a genuinely-alive watcher read as dead. Bound by
`SIGMA_WATCH_CALL_TIMEOUT` (default `120`, mirroring `SIGMA_WATCH_INTERVAL`'s own
convention).

Session start (`hooks/session_start.sh`) also runs a proactive ledger-watcher staleness check,
firing automatically once `ledger.enabled: true` (no separate opt-in of its own), that duplicates
(never imports) `doctor.py`'s own heartbeat/staleness math and warns at session start if the
watcher looks stale, dead, or has never run, pointing at `/sigma-doctor`. This is an **accelerator
layered on top of the existing `_ensure_watcher` restart mechanism** — it narrows the detection
gap, not the restart path.

## Direct `gh` call sites are ratcheted

New `gh issue|pr|project|label` call sites do not go in new places: `tests/test_no_direct_gh.py` is a
ratchet (`skills/sigma-loop/scripts/gh_api.py` is the helper; the baseline only goes down). Run
`$HOME/.sigma-venv312/bin/python -m pytest tests/test_no_direct_gh.py`
(generic form: `python -m pytest tests/test_no_direct_gh.py`); the shapes it cannot see are listed in
[cloud sessions](docs/cloud-sessions.md). Cloud sessions: detection and reporting only, see that page.

## Output

Read [the output contract](docs/output-contract.md) before your first status
emission. Use both axes, the correct block, and its emission triggers. Tool
calls alone are not triggers. Dispatch labels include goal ref and phase.
Requested reports or explanations are given in full.

A status block is constructed, never hand-written: pass facts to
`skills/sigma-loop/scripts/render.py status|event|decision` and relay its stdout verbatim —
`python3 skills/sigma-log/scripts/log.py slots .sdlc` is the live Block A, and `phase_report.py end`
prints Block B at every phase boundary. A refusal (stderr, exit 2, empty stdout) means fix the facts.
