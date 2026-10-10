# Inbound Slack commands — setup and day-to-day operation (Epic #2335)

`slack_commands_listen.py` holds a Slack **Socket Mode** connection open (an outbound websocket
from this machine to Slack — no public URL, tunnel, or webhook server) and answers structured
commands typed into one curated channel: `--drift`, `--merge <name>`, `--unsafe-merge <name>`,
`--list [page]`, `--rebase <name>`, `--help`. Design: issue #2329's design write-up (and its
in-brief summary).

Slice 1 (#2336) shipped the listener itself, the channel-authorization gate, the command parser,
and a real `--help`. Slice 2 (#2337) wires up the two read-only commands: `--drift` (an on-demand
drift-watch sweep, answered directly, no headless drive) and `--list [page]` (a paginated,
10-per-page read of `.sdlc/features/`). Slice 3 (#2338) built the shared claim/isolated-worktree/
headless-drive machinery every mutating command dispatches through. Slice 4 (#2340) wires up
`--rebase <name>` to that machinery: a real, unattended rebase + push, dispatched to an isolated
headless session — clean on a clean rebase, stopped and reported (never resolved) on a real
conflict. Slice 5 (#2341) wires up `--merge <name>`: it verifies a unit's branch is clean and ready
to land, and reports the result honestly — it never lands anything itself, in any configuration
(`docs/branching-model.md` §13's human-only completion boundary), and never spends a real headless
session to do it. Every command Epic #2335 named is wired up now. Slice 6 (#2339, Component F)
added the "**Supervising it day-to-day**" section below: heartbeat-staleness detection, the
`doctor.py` liveness check, and a worked OS-level supervision example.

## Read this before you set it up

- **One Slack App, reused — no new app to create.** This feature needs a bot token with permission
  to *receive* channel messages, on top of whatever the drift watcher's own app already does
  (`SIGMA_SLACK_BOT_TOKEN`, #2311). Rather than a second, dedicated app, the real deployed setup
  adds Socket Mode and a new App-Level Token (`SIGMA_SLACK_BOT_SOCKET_TOKEN`) to that SAME app,
  and reuses its existing bot token to post replies — there is no second bot token to create or
  copy. (Decision #4/Doubt D-6 in #2329's original design called for a genuinely separate, dedicated
  app; this doc reflects the single-app setup actually running instead.)
- **What restarts this process after a crash or a reboot is an OS-level concern, documented, not
  automatic.** This is a genuinely new kind of component for this kit — every other watcher is
  tick-based, driven by `watch_daemon.py` every `ledger.watch.interval_seconds`; this one holds one
  connection open indefinitely. Slack's own reconnect/keepalive protocol (a periodic `disconnect`
  frame asking the client to move to a fresh URL before the old one closes) is handled internally
  by `slack_sdk`'s own `SocketModeClient` — no setup needed for that. The **outer** question — what
  relaunches the whole Python process if it dies or the machine reboots — is Doubt D-5: there is no
  existing precedent in this kit for "keep a background service alive forever" (only for "relaunch
  a short task repeatedly", `supervise_daemon.py`'s own job). "**Supervising it day-to-day**", below, is
  the worked systemd/launchd example this closes with — a one-time setup step you run yourself, in
  the same disclosed-cost style as this whole doc, not something this kit does for you
  automatically.
- **A genuinely new Python dependency.** Python's stdlib has no websocket client, so this needs
  `slack_sdk` — Slack's own official SDK. There is no `requirements.txt`/`pyproject.toml` mechanism
  for this kit's own scripts to declare it in yet (Doubt D-3), so it's a one-time manual
  `pip install`, same as the CLI/Channels adapter's own one-time `bun install`
  ([`channels/sigma-autowatch/README.md`](channels/sigma-autowatch/README.md)).

## One-time setup

### 1. Open the existing Slack App

1. Go to <https://api.slack.com/apps> and open the app the drift watcher (#2311) already posts
   through — no new app is created for this feature.

### 2. Enable Socket Mode and generate the App-Level Token

1. In the app's settings, open **Socket Mode** and turn it **on**.
2. Slack prompts you to generate an **App-Level Token** as part of that — name it (e.g.
   `slack-commands-socket`) and grant it the **`connections:write`** scope. This is the token
   Socket Mode itself authenticates the websocket with (Doubt D-1).
3. Copy the generated token (starts `xapp-`) — this is `SIGMA_SLACK_BOT_SOCKET_TOKEN` (step 5,
   below). This is the only new credential this feature needs.

### 3. Add bot scopes and subscribe to messages

1. Under **OAuth & Permissions**, check the app already has these **Bot Token Scopes** (drift-watch
   setup should already have `chat:write`) and add whichever is missing:
   - `chat:write` — to post replies.
   - `channels:history` (public channel) or `groups:history` (private channel) — to read message
     text in the automation channel; use whichever matches the channel type you pick in step 4.
2. Under **Event Subscriptions**, turn events **on** and subscribe to the bot event
   **`message.channels`** (or `message.groups` for a private channel) — delivery itself arrives
   over the Socket Mode websocket, not a public URL, but Slack still requires this subscription to
   emit the event at all (Doubt D-1).
3. **Reinstall the app to your workspace** (OAuth & Permissions → Reinstall to Workspace) so the new
   scopes take effect. The existing **Bot User OAuth Token** (starts `xoxb-`,
   `SIGMA_SLACK_BOT_TOKEN`) is reused as-is — there is no second bot token to copy.

### 4. Create (or pick) the automation channel, and invite the bot

1. Use a channel dedicated to this — the channel-authorization gate (below) treats **every**
   message in it as a command attempt from an authorized source. This can be the same channel
   drift-watch already posts to, or a different one.
2. Invite the app's bot user to that channel (`/invite @<your app name>`) — already done if you're
   reusing the channel drift-watch posts to and its bot is already a member.
3. Get the channel's own id (not its name) — e.g. via the channel's **View channel details** panel,
   or `Copy link` (the id is the last path segment, shaped like `C0123456789`).

### 5. Set the one new secret — as an environment variable, never in `config.json`

`config.json` is git-committed; a literal token there ships to every clone. The env-var **name**
below is Sigma's own default (the `pass_env` convention this kit already uses for
`SIGMA_SLACK_BOT_TOKEN`/`SIGMA_SMTP_PASS`) — set the variable itself in your shell profile,
a local `.env` your process manager loads, or your supervisor's own environment block, **never** in
a file this repo tracks:

```bash
export SIGMA_SLACK_BOT_SOCKET_TOKEN="xapp-..."      # step 2
# SIGMA_SLACK_BOT_TOKEN is already set for drift-watch (#2311) -- reused as-is, nothing new here.
```

If you'd rather name either one something else, set `slack_commands.app_token_env` /
`slack_commands.bot_token_env` in `config.json` (step 7) to the names you actually use — the
listener reads whatever those two keys name, never a hardcoded literal.

### 6. Install the Python dependency

```bash
pip install "slack_sdk[socket-mode]"
```

One-time, per machine that runs the listener — matching the CLI/Channels adapter's own `bun
install` precedent (Doubt D-3). Without this, `slack_commands_listen.py` refuses to start with a
clear message naming this exact command.

### 7. Configure `.sdlc/config.json`

```json
"slack_commands": {
  "enabled": true,
  "channel_id": "C0123456789",
  "app_token_env": "SIGMA_SLACK_BOT_SOCKET_TOKEN",
  "bot_token_env": "SIGMA_SLACK_BOT_TOKEN"
}
```

`channel_id` is the id from step 4 — this is the **whole** authorization model (a command is
authorized if and only if it arrived on this exact channel; there is no per-user allowlist). Off by
default: `enabled: false`, or an unset `channel_id`, and the listener refuses to start at all.

**The acting commands (`--rebase`, `--merge`, `--unsafe-merge`) need `ledger.enabled: true` to
report a real outcome — the read-only ones (`--drift`, `--list`) do not.** All three acting
commands dispatch through the shared claim/worktree machinery and report their result by reading
back a completion marker written through `ledger.safe_append`; `ledger.safe_append` fails open and
silently no-ops when `ledger.enabled` is false (`ledger.py`'s own "a ledger problem must never
break a run"). With the ledger off, that marker never lands, so a genuinely successful rebase or
merge is reported back to Slack as `unclear` — indistinguishable from a real failure to diagnose
— with nothing else telling the operator why. The reply itself now names this directly whenever it
happens with the ledger off (#2430), but the fix is turning the ledger on, not reading the note:

```json
"ledger": { "enabled": true, "actor": "your-name" }
```

### 8. Start the listener

```bash
python3 skills/sigma-loop/scripts/slack_commands_listen.py .sdlc
```

Run this in a persistent terminal, `tmux`/`screen` session, or a real OS-level supervisor (see
"**Supervising it day-to-day**", below, for a worked systemd/launchd example). It logs to stderr
and to `.sdlc/state/slack-commands.log`; a pidfile (`.sdlc/state/slack-commands.pid`) and a
heartbeat (`.sdlc/state/slack-commands.heartbeat.json`, refreshed on every confirmed Socket Mode
activity) let you check it's actually alive without attaching to its terminal — see below for the
one command that reads them for you.

### 9. Try it

In the automation channel, type `--help`. You should get a reply listing every command within a
few seconds. `--drift` and `--list` (or `--list 2`, etc.) now give real answers — a drift summary
and a paginated open-unit listing. `--rebase <name>` now genuinely rebases `<name>` onto `work.base`
and pushes it, inside its own isolated worktree — on a real conflict it reports the conflicted files
and stops, never force-pushing or resolving anything itself; run `/sigma-rebase` locally to finish
that one by hand. `--merge <name>` (a currently open unit) now genuinely checks that unit's branch:
`verify.command` (`.sdlc/config.json`) runs against an isolated, ephemeral checkout of its remote
tip, and the reply is either "verified clean, ready to merge — run `/sigma-rebase` locally or `gh pr
merge` yourself to land it" or the real reason it isn't (a stopped rebase, a failed verify, no
`verify.command` configured) — it never lands the merge itself, in any configuration.
`--unsafe-merge <name>` (#2359) runs that SAME check and, if it passes, lands it immediately — the
only thing it skips is the "merge now?" confirmation a human running `verify_merge.py land` would
otherwise answer; a failing verify still blocks it, exactly like `--merge`. With unit upkeep on, it goes through the
landing engine instead and needs the same local single-use unit approval first; the reply names the requester and one of
landed, already landed, landed with a warning, armed, refused (with the reason) or unconfirmed. Expect
`--rebase`/`--merge`/`--unsafe-merge` all to take noticeably longer than `--drift`/`--list` (each
does real work against an isolated worktree, not an in-process read) and to claim the unit for the
duration — a second `--rebase`/`--merge`/`--unsafe-merge` on the SAME unit while one is already
running gets "already in progress", not a second concurrent attempt.

**Mentioning the bot also works** (#2353) — `@ls-bot --help`, `@ls-bot --drift`, etc. give the
identical reply a plain `--help` does; requires the `app_mention` bot event subscribed alongside
`message.channels`/`message.groups` (step 3, above). Slash commands (`/ls-bot --help`) are NOT
supported and never will be through this mechanism — the channel-membership authorization model
above doesn't extend to them (a slash command can be typed from any channel a user is in, not just
this one), a separate decision this feature deliberately doesn't make for you.

### 10. Stop it

```bash
touch .sdlc/state/slack-commands.stop
```

The listener notices the stop-file, disconnects cleanly, and removes its own pidfile/heartbeat/lock
on the way out — mirroring [`supervise_daemon.py`](scripts/supervise_daemon.py)'s own stop-file convention.

## Supervising it day-to-day (#2339, Component F)

### Is it actually running? Don't guess — ask `doctor.py`

A live pid is not the same question as "is it actually doing anything" — the same LIVENESS
distinction this kit already draws for the ledger watcher (`watch_daemon.py`) and autowatch. `enabled:
true` in config only means "configured", never "running": if the process crashed, the machine
rebooted, or nobody ever started it, `slack_commands.enabled` still reads `true` forever. What
actually answers "is it alive" is the heartbeat (`.sdlc/state/slack-commands.heartbeat.json`,
refreshed on every confirmed Socket Mode activity) and its own staleness:

```bash
python3 skills/sigma-doctor/scripts/doctor.py check .sdlc
```

Once `slack_commands.enabled` is `true`, this prints a `slack-commands listener wired up` row —
`OK` only when the heartbeat is genuinely fresh (last touched under 180 seconds ago). Anything
else — the heartbeat has gone STALE, a pidfile exists with no heartbeat at all (DEAD — since #2751
the listener writes its heartbeat before its pidfile, so this shape is pre-upgrade residue or an
external deletion, never something the current code produces), or the listener was configured but
never started — reads as `MISSING`, with the one-line fix (restart the listener) printed alongside
it. A component that has died must read differently from one with
nothing to do (AGENTS.md's own LIVENESS bar): `doctor.py check` tells `NEVER RUN` (nobody has
started it), `DEAD` (it was running, and isn't anymore), and `STALE` (it hasn't touched its
heartbeat in a while, and is probably dead) apart, rather than flattening all three into one
generic "not OK". `doctor.py features .sdlc` prints the same underlying state string
unconditionally (even when `slack_commands` is off) if you just want the current reading without
the pass/fail framing.

### Restarting it by hand

```bash
touch .sdlc/state/slack-commands.stop   # if it's still alive: ask it to stop cleanly first
python3 skills/sigma-loop/scripts/slack_commands_listen.py .sdlc
```

The single-instance guard (an atomic `os.mkdir`, mirroring `watch_daemon.py`'s own mutex) means a genuinely
dead prior instance's stale lock is reclaimed automatically — you do not need to clean up
`.sdlc/state/slack-commands.lock` by hand after a crash.

### The one-command version: `status` / `ensure` (#2396)

**Or skip the python paths entirely: `/sigma-slack` (#2409) wraps both of these as plain slash
commands** — "start the slack bot", "is it running", "stop it" — for anyone who'd rather not touch
a terminal. The raw commands below are what it runs under the hood; read on if you want to call
them directly (a cron entry, a supervisor unit, or just habit).

The two steps above (`doctor.py check` to see if it's alive, then a manual stop-touch-and-relaunch
if it isn't) are now also a single command each, right on `slack_commands_listen.py` itself — no
second script to know about:

```bash
python3 skills/sigma-loop/scripts/slack_commands_listen.py status .sdlc
```

Prints one line reporting the SAME heartbeat-based liveness `doctor.py check` already computes
(`running`, `not running`, `DEAD` — a stale heartbeat, likely a crash, `MISCONFIGURED`, or
`disabled`) and exits `0` only when a live instance genuinely holds it — safe to use in a script or
a cron precondition (`slack_commands_listen.py status .sdlc || slack_commands_listen.py ensure .sdlc`).

```bash
python3 skills/sigma-loop/scripts/slack_commands_listen.py ensure .sdlc
```

Checks the SAME liveness and then acts on it: starts the listener if nothing is running, replaces
it if the prior instance is genuinely dead (a stale heartbeat, not merely an absent one — the same
absent/stale/live distinction the ledger watcher's own `sync.watcher_liveness` already draws), or
reports `already running — nothing to do` and starts nothing if a live instance already holds it.
Every outcome prints one clear line (`was not running — started it (pid N)`, `was dead — restarted
it (pid N)`, `already running (pid N) — nothing to do`, or the real reason it failed to start) and
exits `0` for every outcome except a genuine failure to start. It launches the listener the same
detached way a supervisor would (so it outlives the shell that ran `ensure`), and is safe to run
repeatedly, by hand or from a scheduled task (cron, a launchd/systemd timer) — the same
`acquire_single_instance` mutex `run()` itself always used guarantees a concurrent `ensure` (or a
concurrent manual start) can never leave two live listeners running against the same `.sdlc`.

**This is still a manual command, not automatic supervision.** `ensure` only acts when something —
a human, a cron entry, a scheduled task — actually invokes it; it is not wired into `loop.py`'s
`_ensure_watcher` or `watch_daemon.py`'s own every-tick call sites the way the ledger watcher is. Making
the listener start (and restart) itself automatically on every loop trigger is a larger, separate
change this doc's own Doubt D-5 explicitly leaves open — a held-open Socket Mode connection is a
materially different kind of component from a tick-based daemon, and auto-starting one on every
single loop trigger (including inside a tight multi-goal drain) needs its own design resolution
first, not a silent add-on here. Until that lands, `ensure` on a schedule (a `launchd`/`systemd`
timer, or a cron entry, calling `ensure` every few minutes) is the lowest-effort way to approximate
"keep it alive" without waiting on that larger change — see the worked crash/reboot-supervision
examples below for the alternative that removes the "remember the exact python3 invocation" burden
entirely.

### A worked example: relaunch it automatically after a crash or reboot

Doubt D-5 of #2329's design leaves the **choice** of supervisor open — there's no existing
precedent in this kit for "keep a background service alive forever" (`supervise_daemon.py` relaunches a
short-lived command repeatedly; this is a different problem). Below is one disclosed-cost, worked
example for each of the two platforms this kit already runs on; either genuinely restarts the
process after a crash or a reboot; neither is this kit's own script, and neither is required — a
persistent `tmux`/`screen` session you restart by hand after checking `doctor.py check` is also a
completely valid choice for a single-operator setup.

**macOS — `launchd`.** Save as `~/Library/LaunchAgents/com.sigma.slack-commands.plist`
(replace both `/absolute/path/to/sigma` paths with your real checkout):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.sigma.slack-commands</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/absolute/path/to/sigma/skills/sigma-loop/scripts/slack_commands_listen.py</string>
    <string>/absolute/path/to/sigma/.sdlc</string>
  </array>
  <key>WorkingDirectory</key><string>/absolute/path/to/sigma</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/absolute/path/to/sigma/.sdlc/state/slack-commands.launchd.log</string>
  <key>StandardErrorPath</key><string>/absolute/path/to/sigma/.sdlc/state/slack-commands.launchd.log</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.sigma.slack-commands.plist
```

`KeepAlive` relaunches it on a crash; `RunAtLoad` starts it at login/reboot. `launchctl unload`
that same plist path to stop supervision (do this before `touch .sdlc/state/slack-commands.stop`
if you want it to actually STAY stopped, not be relaunched a moment later). **The same
`caffeinate -is` (or "prevent sleep" power setting) note `supervise_daemon.py` already carries
applies here verbatim**: a sleeping MACHINE stops the Socket Mode connection exactly like it stops
everything else on it, `launchd` included — `KeepAlive` relaunches a crashed *process*, it does not
wake a sleeping *machine*.

**The launchd log is yours to reclaim.** `slack-commands.launchd.log` is written by launchd, not by Sigma, and
Sigma never rotates or prunes it; it repeats every line the listener logs. `slack-commands.log` (rotated at a size cap, three
predecessors kept) already carries them, so if you do not need raw stderr, point both paths at `/dev/null`; otherwise `launchctl
unload` the plist, delete or truncate the file, and load it again when it grows. See
`docs/launch/b6-slack-supervisor-logs.md`.

**Linux — `systemd` (user unit).** Save as
`~/.config/systemd/user/sigma-slack-commands.service`:

```ini
[Unit]
Description=Sigma inbound Slack commands listener

[Service]
Type=simple
WorkingDirectory=/absolute/path/to/sigma
ExecStart=/usr/bin/python3 skills/sigma-loop/scripts/slack_commands_listen.py .sdlc
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now sigma-slack-commands.service
```

`Restart=always` relaunches it on a crash; `enable` starts it at boot/login (add
`loginctl enable-linger <user>` if you need it to survive without an active login session, e.g. on
a headless box). `systemctl --user stop sigma-slack-commands.service` stops it without
relaunching.

Either way, both env vars from step 5 must be visible to whatever launches the process — a
`launchd`/`systemd` unit does **not** inherit your interactive shell's exported variables; add an
`EnvironmentFile=` (systemd) or a `<key>EnvironmentVariables</key>` dict (launchd) naming them, or
export them in a wrapper script the unit calls instead of `python3` directly.

## Command grammar

Structured commands only — no natural-language understanding (Doubt D-2, deliberate):

| Command | What it does |
| --- | --- |
| `--drift` | Show the current feature-branch drift summary, computed on demand — an on-the-spot run of the same commit-delta + landing-PR check the passive drift watcher (#2311) runs on its own TTL tick, answered directly with no headless drive |
| `--merge <name>` | Verifies `<name>` is clean and ready to land (`verify.command` run against an isolated worktree of its remote tip) — never merges it itself, in any configuration; reports "ready, go land it yourself" on a pass, or the real reason it isn't (stopped rebase, failed verify, no `verify.command` configured) |
| `--unsafe-merge <name>` | Runs the SAME verify check as `--merge`, then LANDS it immediately if it passes — the ONE thing this skips versus running `verify_merge.py land` by hand is the "merge now? [y/N]" confirmation; a failing verify still blocks the merge, exactly like `--merge`. Use at your own discretion (#2359). With unit upkeep on: landing engine plus a local unit approval, requester recorded |
| `--list [page]` | List open feature units, 10 per page, straight from `.sdlc/features/index.json` — title, owner, priority per unit; an out-of-range page is refused with the real page count, never a silent empty reply |
| `--rebase <name>` | Rebase `<name>`'s `feature/<name>` branch onto `work.base` and push, unattended, inside an isolated worktree cut just for this dispatch — clean rebases are pushed with `--force-with-lease` and reported done; a real conflict is left stopped exactly where git left it and reported with the conflicted files, never resolved automatically or handed to the interactive walker |
| `--help` | Show usage |

`<name>` must be a real, currently **open** unit in `.sdlc/features/index.json` — an unrecognized or
closed name is refused with the real list of open units, never fuzzy-matched or guessed. A malformed
command (unknown flag, missing/extra arguments, free-form language) is refused loudly, quoting every
valid form — never a silent no-op. A message on any **other** channel is dropped silently (logged,
no reply) — the gate is about which channel it arrived on, not whether the sender is a specific
person.
