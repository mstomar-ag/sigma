# Ledger autowatch — wake-and-work setup (CLI + Desktop)

`ledger.autowatch` is what closes the gap between "the ledger detected a mention/assignment/
blocker addressed to you" and "someone actually acts on it." Without it, a hit is only surfaced
via `.sdlc/state/inbox.md`, read by `loop.py next` — which means nothing happens at all if nobody's
`/sigma-loop` is currently running. That's the common overnight-idle case: a teammate in a different
timezone tags you, and nothing acts on it until you manually start a new session hours later.

Turning `ledger.autowatch.enabled` on is not enough by itself — it makes the *decision logic* live,
but something still has to actually *trigger* a tick. That's the one-time setup below: exactly one
of the two adapters, matched to whichever surface you personally use day to day.

## Prerequisites

- `ledger.enabled: true` in `.sdlc/config.json` — autowatch rides `watch_daemon.py`'s own tick, same as
  every other watcher in this kit (`agent_watch`, `comment_watch`).
- `ledger.autowatch.enabled: true`, plus whatever `scope`/`hop_limit`/`preconditions` you want —
  see the `_autowatch` doc comment in `skills/sigma-init/templates/config.json.tmpl` for the full
  schema.
- Check current state any time: `python3 skills/sigma-doctor/scripts/doctor.py check .sdlc` reports
  whether autowatch is enabled AND whether an adapter is actually wired up yet.
- A POSIX host (macOS or Linux). The driven session runs in a process group of its own, and when
  it overruns `ledger.autowatch.timeout_seconds` (default 2h) the whole group — the model and
  everything it started — gets SIGTERM, up to 10s to exit, then SIGKILL, and is reaped before the
  tick records `failed` (#425). SIGTERM, SIGHUP or Ctrl-C to the tick mid-drive does the same
  first, and a further signal cannot cut that short; afterwards every signal received is
  re-delivered in order, so the tick still dies by it, Ctrl-C still raises KeyboardInterrupt, and a
  handler you installed still runs. If the
  tick is SIGKILLed, alone or with its whole process group, a small lifeline process notices its
  pipe close and runs the same escalation. On Windows the drive refuses (`REFUSED
  [no-process-group]`, recorded in the ledger) instead of leaving model descendants running. Not
  reached: a descendant that calls `setsid` itself, or a model whose lifeline process was itself
  SIGKILLed; the drain after the kill is bounded so neither can hang the tick.

## Pick your surface

`ledger.autowatch.surface` in config picks which adapter THIS machine runs — `"desktop"` (the
default) or `"cli"`. This is a **declared** choice, not auto-detected: there is no documented
signal that lets running code tell "I'm inside Claude Desktop" apart from "I'm inside Claude Code
CLI," so guessing risks a Desktop-only prompt misfiring inside an unattended CLI run. A team shares
one committed default in `.sdlc/config.json`; an individual machine overrides it locally with the
`SIGMA_AUTOWATCH_SURFACE` environment variable, without needing a personal config file.

```json
"ledger": {
  "enabled": true,
  "autowatch": { "enabled": true, "surface": "desktop" }
}
```

---

## Desktop setup

Desktop has a native local scheduled-task feature CLI has no equivalent for (`/loop` needs an
already-open session). Task creation has no headless API — the one manual step is asking Claude,
inside an open Desktop session, to create it.

1. Set `surface: "desktop"` (or leave it unset — it's the default) and `enabled: true` under
   `ledger.autowatch` in `.sdlc/config.json`.
2. Open a Claude Desktop session in this repo and run any `/sigma-*` command. Since no task is wired
   up yet, `loop.py` prints a one-time nudge naming the exact ask.
3. In that same Desktop session, ask (in your own words, or verbatim):
   > Create a scheduled task named `sigma-autowatch-<repo-slug>` that runs every
   > `ledger.autowatch.poll_interval_minutes` minutes and executes
   > `python3 skills/sigma-loop/scripts/autowatch.py tick .sdlc`, with the isolated-worktree toggle
   > on.
4. Click **Run now** once and approve each tool call — future runs auto-approve the same tools
   without prompting.
5. Verify: `doctor.py check .sdlc` should now report `autowatch adapter wired up: OK`. The nudge
   from step 2 won't reappear on future runs.

That's it — from here, every scheduled run ticks `autowatch.py`, which handles everything else
(finding a candidate, checking every precondition, driving `/sigma-loop` only if it's actually safe
to).

---

## CLI/Channels setup

CLI has no native scheduled-task feature, so the CLI adapter is event-driven instead: a small,
custom [Claude Code channel](https://code.claude.com/docs/en/channels) forwards a ledger detection
into an already-open, persistent session, which runs `autowatch.py tick --issue N` in response.

**Read this before setting it up — a real, standing cost, not a one-time step.** Channels are a
research-preview feature. Only Anthropic's own curated channel plugins (Telegram, Discord,
iMessage, fakechat) register without extra steps. A custom channel like this one needs
`--dangerously-load-development-channels` on **every single launch**, forever, with a full-screen
"local development" warning dialog each time. There's no way off this for a personal account
outside a Team/Enterprise org. If that friction isn't worth it, use the Desktop adapter instead.

Full instructions, including troubleshooting, live in
[`skills/sigma-loop/channels/sigma-autowatch/README.md`](channels/sigma-autowatch/README.md).
Short version:

1. Install [Bun](https://bun.sh), then `cd skills/sigma-loop/channels/sigma-autowatch && bun install`.
2. Add a `.mcp.json` at the repo root:
   ```json
   {
     "mcpServers": {
       "sigma-autowatch": {
         "command": "bun",
         "args": ["skills/sigma-loop/channels/sigma-autowatch/webhook.ts"]
       }
     }
   }
   ```
3. From the repo root: `claude --dangerously-load-development-channels server:sigma-autowatch`.
   Accept the local-development warning and the "new MCP server" consent prompt. Keep this session
   open — events only arrive while it is.
4. Set `surface: "cli"` and `channel_webhook_url: "http://127.0.0.1:8790"` (or whatever port
   `SIGMA_AUTOWATCH_CHANNEL_PORT` is set to) under `ledger.autowatch` in `.sdlc/config.json`.
5. Verify: `doctor.py check .sdlc` should report `autowatch adapter wired up: OK`. Inside the open
   session, `/mcp` should show `sigma-autowatch · ✔ connected`.

**Troubleshooting a `/mcp` "✘ failed" status**: the HTTP listener and the MCP stdio connection are
two separate things — the process can bind its port fine while the MCP handshake itself still
fails, and vice versa. The single most common cause, confirmed live: something else is already
holding the configured port (`lsof -i :8790`, or whatever port you set) — kill it and restart the
session. `claude --debug` prints the underlying error if it's something else.

**Live-verified, real end-to-end (2026-08-18)**: a real ledger entry → a real `channel_notify.py`
push → a real running `webhook.ts` server → a real `<channel>` event landed in a real, persistent
`claude --dangerously-load-development-channels` session → the session ran the exact instructed
`autowatch.py tick .sdlc --issue N` command → `autowatch.py` correctly evaluated its safety
preconditions and recorded a real ledger outcome. This is not a theoretical design — it was run for
real, once, in full, before this doc was written.

---

## Board reconciliation heartbeat (reconcile_tick.py)

Running `watch_daemon.py`? Nothing to do here — `reconcile_tick.py` (#2294) is already threaded into its
standing tick sequence, alongside `agent_watch.py`/`comment_watch.py`, so it gets the same
wall-clock heartbeat everything else in this file rides. This section is only for an operator who
does **not** run `watch_daemon.py` at all and relies solely on one of the two adapters above.

`reconcile_tick.py` is a **different kind of periodic piece** from `autowatch.py tick`, and does
not ride it: `autowatch.py tick` decides whether to *drive a whole `/sigma-loop` run* against a
ledger-detected mention, which needs a live Claude session (the Desktop scheduled task, or the
CLI-channel-driven session) to actually execute the loop. `reconcile_tick.py` only applies
`reconcile.py`'s existing AUTOMATIC-tier board/label corrections — a small, dependency-light script
(the same shape as `agent_watch.py`/`comment_watch.py`) that talks to GitHub directly and needs
**no Claude session at all**. So rather than folding it into `autowatch.py`'s own tick — which would
couple two independent concerns and their independent config gates for no reason — give it its own,
simpler trigger:

- **Desktop**: ask Claude (in the same session you used for the setup above) to create a *second*
  scheduled task, e.g.:
  > Create a scheduled task named `sigma-reconcile-<repo-slug>` that runs every
  > `discovery.reconcile.ttl_minutes` minutes and executes
  > `python3 skills/sigma-loop/scripts/reconcile_tick.py .sdlc`.
- **CLI, or any surface**: since it needs no open session, the simplest fix is a plain OS-level
  scheduler — `cron`, `launchd`, or Windows Task Scheduler — running
  `python3 skills/sigma-loop/scripts/reconcile_tick.py .sdlc` directly. No channel, webhook, or MCP
  plumbing required.

Either way, this is **no new config key**: it reuses `discovery.reconcile.mode` (default `off`) and
`discovery.reconcile.ttl_minutes` exactly as `loop.py`'s own loop-driven sweep does, so it is a true
no-op — no `gh` calls — until that gate is turned on, and a repeat call inside the TTL window costs
one cheap watermark read.

**The dependency map's refresh duty (#1612).** The same tick also carries one more duty, in `reconcile_tick.py` itself (so the watcher's tick keeps its eight calls in their documented order): `dag_refresh.refresh_if_due` refreshes the stored dependency map when it is older than `discovery.dag.ttl_minutes` plus a stable per-clone offset of up to 10 percent, within a call cap and a time budget derived from `SIGMA_WATCH_CALL_TIMEOUT`. It runs only when `discovery.dag.mode` is written as `shadow` or `on`, never fails the tick, and adds the summary part `dependency map refreshed`. Without a watcher the same refresh runs on the first pick of a process, or by hand: `python3 skills/sigma-loop/scripts/dag.py refresh [--force] --sdlc .sdlc`.

---

## Passive drift watcher + Slack (drift_tick.py, #2311)

Running `watch_daemon.py`? Nothing to do here — `drift_tick.py` (#2311, epic #2310) is already threaded
into its standing tick sequence, right after `channel_notify.py`, so it gets the same wall-clock
heartbeat everything else in this file rides. This section is only for an operator who does **not**
run `watch_daemon.py` at all and relies solely on one of the two adapters above.

`drift_tick.py` checks every OPEN unit declared in `.sdlc/features/` for two read-only signals — how
far its `feature/<name>` branch has fallen behind the integration branch (a commit delta), and
whether its own landing pull request has gone stale — and posts a plain-text summary to one of two
hardcoded Slack channels once every `drift_watch.ttl_minutes` (default 90). It needs **no Claude
session at all**, the same shape `reconcile_tick.py` above already has, so it gets the identical
kind of trigger:

- **Desktop**: ask Claude (in the same session you used for the setup above) to create a *third*
  scheduled task, e.g.:
  > Create a scheduled task named `sigma-drift-<repo-slug>` that runs every
  > `drift_watch.ttl_minutes` minutes and executes
  > `python3 skills/sigma-loop/scripts/drift_tick.py .sdlc`.
- **CLI, or any surface**: a plain OS-level scheduler — `cron`, `launchd`, or Windows Task
  Scheduler — running `python3 skills/sigma-loop/scripts/drift_tick.py .sdlc` directly. No channel,
  webhook, or MCP plumbing required.

**Unlike `reconcile_tick.py` above, this one DOES need `ledger.enabled: true` — not incidentally,
but structurally.** `reconcile_tick.py`'s own section above never mentions `ledger.enabled` because
that tick has no dependency on it at all; copying that silence here would be exactly the gap a
review round of this design's own confirmation caught and fixed (design #2289, D-2's
addendum). `drift_watch`'s entire "don't post the same summary twice" mechanism is built on the
shared ledger (`ledger.safe_append`/`read_all`) — a summary the WHOLE TEAM sees in one Slack channel
cannot be deduped by a private, per-machine cursor the way `channel_notify.py`'s own personal-nudge
cursor can. So `drift_watch`'s own gate composes `ledger.enabled` first: `drift_watch.enabled: true`
set through this standalone-cron path, without `ledger.enabled: true` also set, makes every tick a
genuine no-op — not a silent double-post risk, just nothing happening at all — until both are on.

Two independently-gated opt-ins layer on top of each other, so a repo can turn the watcher on and
watch its own `watch.log` for a while before anything ever reaches Slack (since #2499 that log is
capped -- the watcher rolls it to one predecessor, `watch.log.1`, at the start of a tick once it
reaches `ledger.watch.log_max_bytes`, so follow it with `tail -F`, not `tail -f`, which stops
following at the roll): `drift_watch.enabled` (plus
`ledger.enabled`) makes the tick run and log what it found; a filled-in
`drift_watch.channels.sigma` **or** `.org` id is what actually lets a post leave the machine —
leaving both `null` means every tick still computes and logs, but posts nothing anywhere.
`SIGMA_SLACK_BOT_TOKEN` (or whatever `drift_watch.slack_bot_token_env` names) unset degrades the
same way — a logged `[stub]` line, never a failure.

---

## Safety notes (read once, applies to both surfaces)

- `hop_limit` bounds how many autowatch-triggered runs may chain from one original trigger — a
  driven run's own new ledger writes can't cascade into an unbounded run of runs.
- `spend_ceiling_tokens_per_week` is checked against a **local heuristic estimate only** — it
  cannot see your interactive usage or any other machine's autowatch history. Never treat it as a
  real balance check.
- `target_issue_only` (always `true`, the only implemented value) means a hit drives `/sigma-loop`
  scoped to that ONE flagged issue — never an open-ended backlog drain while you're away.
- Every outcome — success, a blocked precondition, a hop-limit refusal, or a genuine failure —
  writes one ledger note. Nothing autowatch does happens silently.
