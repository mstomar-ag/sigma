# Action log and team ledger

The local trace of what the loop did, and the shared team ledger. Back to the [README](../README.md).

## Local action log (on in a scaffolded config)

The ledger below is shared, git-tracked, and meant for team-visible coordination — the wrong place
for a full local trace of what the loop is doing *right now*: every file touched, every model/effort
choice, every subagent dispatch. That is a separate, local-only mechanism:

```json
"action_log": { "enabled": true }
```

The code default is off when the key is absent; the config that `/sigma-init` scaffolds turns it on. From then on, `.sdlc/state/log/<goal-stem>.jsonl` fills up with one line per event — millisecond
timestamps, since two slice subagents can write to the same goal's log in the same wave. It's
gitignored by default (matches the existing `RUNTIME_IGNORES`, no setup needed) and never imported
by the ledger — the two mechanisms can't leak into each other even if both are on.

Read it with the `sigma-log` skill (or directly):

```bash
python3 <installed-sigma>/skills/sigma-log/scripts/log.py goal   .sdlc 0007-cache.md  # one goal's full trace
python3 <installed-sigma>/skills/sigma-log/scripts/log.py status .sdlc        # every ACTIVE goal, latest event
```

`goal <id>` works for any goal that has ever written an entry, including one an agent has only ever
logged notes for by hand. **`status` shows less than that** — "active" means the loop itself is
driving the goal (a `claimed` entry, which only the loop's own internal call sites can write, never
the CLI), so a goal you populated purely via `loop.py log` from the shell — exactly the example
below — will not appear in `status`'s output even though `goal <id>` reads it back correctly. Not a
bug, just a narrower definition of "active" than the write path above might suggest; check `goal
<id>` directly if `status` looks emptier than expected.

An agent can add its own notes to the same trace — `file` / `model_choice` / `agent_dispatch` /
`agent_done` / `note` / `action` / `permission_use`, a closed vocabulary the CLI enforces, each with
its own whitelisted fields (e.g. `note` takes `text`, `file` takes `path`/`op`; `action` and
`permission_use` take a required `action` text, which the hard-stop record-done net reads when `rulebook.hard_stops.enabled` is true (default false): an
`action` that classifies to a hard-stop class refuses `record done` until a `permission_use` or a
park entry carries the same text; entries exist only with `action_log.enabled` true):

```bash
loop.py log .sdlc 0007-cache.md note --thread slice-2 --text "found the flaky test"
```

## The team ledger (optional, off by default)

The review queue answers *"what stopped?"* for one person on one machine — and it's gitignored, so
nobody else ever sees it. Once more than one person runs the loop against a repo, that isn't enough:
you need a **committed** record of what everyone's loop actually did, with a timestamp and a name on
every line.

`/sigma-init` creates `.sdlc/ledger/` empty apart from a README; the template leaves `ledger.enabled` undecided (null, off), and `/sigma-setup` turns it on the first time it adopts a github repo.

Turn it on:

```json
"ledger": { "enabled": true }
```

From then on the loop records a `claimed` line when it takes a goal and an outcome line
(`done` · `parked` · `failed`) when it finishes one. Every call is **fail-open** — a ledger problem
can never stop a run.

```
.sdlc/ledger/
├── entries/<actor>-<host>.<pid>.jsonl   one file per writing process — you only write your own
└── TEAM.md                              generated view; regenerate, never hand-edit
```

**Why one file per writing process.** Two writers appending to a shared file race in the filesystem
and then conflict again in git. Owning a file nobody else writes removes both by construction, and
the team view is simply their union, computed on read. The owner is the *process*, not the person:
the name carries your handle, a hash of the machine, and the pid, so two parallel loops under one
login — or two machines authenticating as one bot account — never land in the same file. Your handle
comes from `ledger.actor`, else the authenticated account (`gh api user`), else the shell user.

**So expect many files per person, not one.** Every run adds a file and nothing compacts them: an
actor who has run the loop for a few weeks owns hundreds, and an external audit of one adopter
machine found 175 for a single actor after two and a half weeks. That is the designed steady state.
Nothing is ever read back by filename — `read_all()` unions every `*.jsonl` in the directory and
attributes each line by the `actor` field inside it — so the count costs correctness nothing. The
state actually worth asking about is the opposite one: a directory holding exactly ONE file for a
given actor means that actor's writes stopped.

**Why two sessions can't both grab the same goal.** With the ledger on, picking a goal skips anything
another actor — or a still-live process of your OWN actor, e.g. two of your own concurrent sessions —
already holds an open claim on; a claim whose process has since died is reclaimed rather than waited
out. That check needs a durable claim to compare against, so it has no answer for two picks landing at
the exact same instant with nothing claimed yet — a local, kernel-mediated file lock (`flock`,
POSIX-only, fails open elsewhere) closes that narrower gap, held only for the moment between deciding
on a goal and the ledger claim landing. The lock runs unconditionally, ledger on or off; the
broader "is this claim still live" check needs the ledger on to have anything to check against — with
the ledger off, only the same-instant, same-machine case is covered.

Read it:

```bash
python3 <installed-sigma>/skills/sigma-loop/scripts/ledger.py summary .sdlc  # counts + open hand-offs
python3 <installed-sigma>/skills/sigma-loop/scripts/ledger.py mine    .sdlc  # addressed to me
python3 <installed-sigma>/skills/sigma-loop/scripts/ledger.py render  .sdlc --write
```

Write anything else explicitly — kinds are
`claimed · done · parked · failed · handoff · ack · release · note`, and the code also accepts `merged`, `merge-armed` and `acceptance`:

```bash
ledger.py append .sdlc note 0007-cache.md --why "spike looks viable"
```

A `release` is also written for you, on a GitHub source, when a loop session ends or dies holding goals it
claimed but never started (no work record, no agent marker): the comment reads `claimed but not started: session
ended`. A dead session waits `session_release.grace_minutes` (default `15`, in `config.json`) first; started
goals and live sessions are never released.

An entry with a `to` is **addressed** to that person: it lands in the team view and in their
`ledger.py mine`. `/sigma-status` reports the entry count; `/sigma-doctor` reports whether the ledger
is on.

**A write you type publishes itself.** `append`, and `handoff.py open`/`track`/`ack`, push the entry
to the ops branch as soon as it is written and print on stderr whether the team can see it yet —
because a ledger entry nobody else can read is not an entry, and the watcher — previously the only
thing that ever published — only ever starts from a loop trigger. If the watcher IS running the
write says which tick will carry it, rather than racing it for the worktree's index lock. Nothing
here can change the command's own output: stdout stays the bare entry id and the exit code is
untouched, so a push that fails costs you a warning, never the write. Set
`ledger.publish_on_write: false` to keep the push local and leave it to the watcher.

### What the entry file name says about your machine

Each machine writes its own file, `entries/<actor>-<host token>.<pid>.jsonl`, and the ops branch
publishes it. The host token is a short hash of the host name, never the host name itself, on any
repository. Two optional `ledger` settings change it, both off by default (the default file names
are unchanged):

- `host_alias`: a label of your choosing (letters and digits, at most 12) used in place of the hash.
- `salt_host_token: true`: salts the hash with `discovery.github.repo`, so one machine has a different
  token in each repository.

Files written by older versions are still read and attributed by the `actor` field inside each line.
`/sigma-doctor` reads the file names in the local copy of the ledger branch (at most 500) and warns
when a public repository's published entries carry a host part that is not a hash, the alias or a
pid; fix it with `host_alias` and remove those files from the ledger branch. It does not rewrite
history.

### Blocked on someone else's area? Hand it off, don't park into silence

Parking is right for a decision only a human can make. It is the wrong answer for a **dependency in
code another person owns** — the queue entry is local and gitignored, the issue comment is
unaddressed, and the work stalls until someone happens to notice.

```bash
handoff.py open .sdlc "$goal" --area engine --why "auto-restart needs an engine feature flag" --priority P0
```

That resolves the owner from the repo's own `.github/CODEOWNERS` (the roster your host already
enforces on every PR — no second list to keep true), opens an issue in their area **assigned to them
and carrying the goal label**, records a `handoff` entry addressed to them, and links it from the
blocked issue. Then the goal parks as usual and the loop moves on.

The delivery needs no new machinery: because the new issue is a goal issue with an assignee, **the
owner's own loop picks it up** through the `discovery.github.assignee` filter. The other half is an
answer — taking it, needing time, declining, or closing it out:

```bash
handoff.py ack .sdlc --issue 61 --state accepted --why "after the current slice"
```

To acknowledge a hand-off without an issue number (no `gh`, or a local backlog), use `--goal`
instead of `--issue` — `--area` narrows to one hand-off when that goal carries more than one
outstanding (a goal blocked on two areas at once is two independent hand-offs, settled
independently):

```bash
handoff.py ack .sdlc --goal <goal> --area <area> --state accepted
```

Mixed versions on one ledger: an older Sigma reading an upgraded teammate's area-carrying `ack`
ignores the area and settles the *whole* goal, so upgrade both sides before relying on per-area acks.

`deferred` deliberately does *not* settle a hand-off — a promise to look later is not a resolution,
so `ledger.py summary` keeps showing it. Override the roster per area with `ledger.owners` when your
directory layout doesn't match your area vocabulary. Every step degrades honestly: no owner, no `gh`,
or a local backlog still writes the ledger entry.

**Not every finding is a cross-area block.** `handoff.py open` above always assigns cross-area and
always blocks — right for a genuine hand-off, wrong for the far more common case: a same-area
follow-up finding (a review comment, a mid-goal discovery) that used to have no disciplined path at
all and got filed by hand, unlabeled and unassigned, easy to lose in a long session. `handoff.py
track` is the general tool underneath both:

```bash
handoff.py track .sdlc "$goal" --area engine --why "found a flaky test while implementing" \
  --queue actionable --assignee same-area --blocks no --label sdlc:followup,flaky-test
```

Every axis is a **required** value flag — `--queue actionable|queued`, `--assignee
same-area|cross-area`, `--blocks yes|no` — so a caller can never silently get the wrong routing; a
missing or misspelled value is a hard usage error, nothing written.

**On an autonomous or overnight run, `--queue queued` is the default posture:** triage decides, arming the
issue at a low bucket priority (`sdlc:goal`, always below a human-filed P1) or parking it with a declared
question kind. Use `--queue actionable` only for a genuine blocking dependency (`--blocks yes`) or work a human
asked for by name in this session: it still runs the deny-list, but arms at most as urgent as the bucket.
With `ai_filed.triage.enabled` false the old rule returns and `queued` withholds `sdlc:goal`, so
`next_pending` cannot see it. `--blocks yes` writes the same
machine-readable `**Blocked by:** #N` marker `handoff.py open` does; `--blocks no` files and links
the issue without parking anything — a related finding should never auto-park unrelated work.
`handoff.py open` is a thin wrapper over this same machinery (`--assignee cross-area --blocks yes`,
always), so both commands share one label/assignee/ledger discipline instead of two.

`--title T` sets the new issue's title; `--body-file F` reads a file **verbatim** as its body — for
a body too long or too structured for a CLI arg (the `goal_decompose` `file`-mode meta-issue is the
first caller). The file is read *before* anything is created, so a missing path is a hard usage
error (exit 2, nothing written) rather than a half-filed issue. `--label` takes a **comma-separated
list** — `--label sdlc:followup,flaky-test` — so the class label and whatever labels your own
board uses ride the same filing, and the issue is fully labelled as filed instead of needing a pass
by hand. Those extra labels are yours: Sigma attaches them and reads none of them (see
[the label model](label-model.md), §12). Pass the flag
once carrying the whole list: a repeated `--label` still silently keeps only the last value (plain
last-wins flag-parsing, no accumulation). The split happens in `track`'s own dispatch, deliberately
not in the shared flag parser every other verb uses. `--queue` is what sets the goal label — putting
`sdlc:goal` in `--label` yourself overrides that and makes a queued issue pickable; let `--queue`
own it.

### Sharing it — an ops branch that never touches your working tree

A shared ledger has to be pulled often, and pulling your integration branch mid-task is how people
lose work to a surprise rebase. So the ledger lives on its own branch, and **`.sdlc/ledger/` is a git
worktree checked out to it**:

```bash
sync.py bootstrap .sdlc   # one shot: create the branch (from the EMPTY tree) + worktree, seed your
                          # entries file + TEAM.md, and PUSH — once per clone. Or just run /sigma-ledger.
```

Add `.sdlc/ledger/` to `.gitignore` on your code branch. From then on:

* fetching and rebasing the ledger touches **only** that worktree — your code checkout never moves;
* the ops branch is never merged into the integration branch, so it needs no review and can stay
  unprotected while your code branch stays locked down;
* the branch starts from the empty tree, so it carries the ledger and nothing else.

`sync.py publish` fast-forwards your own entries file onto it; a rejected push fetches, rebases and
retries rather than forcing — and because nobody shares a file, that replay can't conflict.

**A clone that tracks only one branch still joins the team's ledger.** `git clone --single-branch`,
and `git clone --depth N` (which implies it), write a fetch refspec for `main` and nothing else. In
such a clone a plain `git fetch origin sdlc-ledger` never writes `origin/sdlc-ledger`. So every fetch
`sync.py` makes names its destination (`+refs/heads/<branch>:refs/remotes/<remote>/<branch>`). There
is no need to widen the refspec. `ledger.remote` must therefore be a remote *name*, not a URL or path;
a URL is refused by name.

`init` never starts a second history beside the team's. When it cannot fetch the branch, it asks
`git ls-remote`. If the remote has no such branch, it starts one from the empty tree as before. If the
remote has the branch, or a configured remote cannot be read, it stops with `REFUSED` and creates
nothing. Re-run it once the remote is reachable; entries written in the meantime stay in
`.sdlc/ledger/` and are carried in.

**A ledger that already forked says so, and `bootstrap` repairs it.** A clone bootstrapped by Sigma 1.0.9
or earlier with a narrow refspec may hold an `sdlc-ledger` branch that shares no commit with the remote's.
A remote branch that someone deleted and recreated looks the same. No push can fast-forward either
case, so `publish` and `pull` return `… refused: sdlc-ledger here shares no history with origin/sdlc-ledger …`
 instead of promising a retry. Neither one ever rebases such a branch onto the
remote, since that would overwrite the shared README and TEAM.md with this clone's copies.
`/sigma-doctor`'s team-ledger row says `DIVERGED`. If the remote-tracking ref has never been written,
the row says the clone's fetch refspec does not cover the branch, and that `sync.py pull .sdlc` fetches
it by name. The lever is the command you joined with:

```bash
sync.py bootstrap .sdlc                       # --channel knowledge for the notes branch
```

It moves this clone's branch onto the remote's and keeps the old tip at
`refs/sigma/orphaned/<channel>/<UTC stamp>`.

- **This clone's own entries files are kept and published.** A file is this clone's own if its name
  carries this machine's host token, whatever actor it names, or if it is a legacy `<actor>-<pid>`
  file of this actor. Where the remote has the same file, the lines it lacks are appended; entries
  are append-only and read sorted, so nothing is reordered or lost.
- **Every other path takes the branch's copy.** That includes a teammate's file, README.md, TEAM.md
  (regenerated on publish) and `.gitattributes`.
  - An uncommitted local copy it overwrites is first saved under `.sdlc/state/orphaned/<stamp>/` and
    named in the output.
  - Another machine's file the remote does not have is moved there too, out of `entries/`, so it is
    not republished, rendered into TEAM.md, or counted as unpublished. If the old branch did not hold
    it either, the output names it, because the moved copy is the only one.
- **The notes branch is different.** Notes carry no host, so every local note is republished through
  the secret gate.

It never runs on its own. A recreated remote branch may have been recreated on purpose, and whether
to republish onto it is your call. Stop the watcher first if it is mid-tick. A step that collides with
it on `index.lock` fails loudly. Re-running `bootstrap` starts over if the re-root stopped before
moving the branch, and finishes the cleanup if it stopped after.

`git -C .sdlc/ledger reset --hard refs/sigma/orphaned/ledger/<stamp>` (the knowledge worktree is
`.sdlc/knowledge/analysis`) puts this clone's *branch* back on the old tip. It does not unpublish
what the re-root already pushed. It also removes from the worktree any file committed since that the
old tip lacks; those files are still on the remote.

### The watcher — so a mention actually reaches you

```bash
python3 <installed-sigma>/skills/sigma-loop/scripts/watch_daemon.py .sdlc &   # stop: touch .sdlc/state/watch.stop
```

Each tick pulls the ops branch, works out what is addressed to you and hasn't been surfaced yet,
writes `.sdlc/state/inbox.md`, and publishes anything of your own still sitting local. Interval is
`ledger.watch.interval_seconds` (default 900).

`/sigma-doctor`'s **team ledger** row reads that watcher's heartbeat and compares this clone against
the ops branch, so a ledger that has stopped delivering says so — how many entry files hold writes
no other machine can see, how old the oldest is, and whether a dead watcher is the reason. When it
cannot answer (the worktree does not respond to git) it says *that* rather than reporting health.

**`loop.py next` prints that inbox on stderr before it hands over the next goal.** That boundary is
deliberate and it is the honest one: nothing can inject a message into a running session, and
interrupting a goal mid-flight is how half-finished work gets lost. Worst-case latency is one goal.
A `P0` should be taken *next*, not *now*.

Two independent suppressions keep it quiet: a per-author cursor so history isn't re-read every tick,
and a `kind:issue:state` signature so a colleague's rebase can't replay old mentions at you. A
*state change* on the same issue is news and does fire.

### Watching for a dead agent, not just a mention

An unattended overnight drain has a failure mode the mention-watcher above can't see: a
background/subagent collapses mid-goal, and nothing notices — the goal it was working just sits
there, silently never progressing, burning the rest of the run's budget on an agent that will never
finish. Turn it on:

```json
"agent_watch": {
  "enabled": true,
  "notify": {
    "email": {
      "enabled": true,
      "host": "smtp.example.com",
      "to": "you@example.com",
      "user": "you@example.com",
      "pass_env": "SIGMA_SMTP_PASS"
    }
  }
}
```

Needs `ledger.enabled` too — `watch_daemon.py`'s own tick is what runs the check. The driving skill
registers a marker per `(goal, thread)` at the exact points it starts driving one
(`loop.py agent-start .sdlc <goal> --pid $PID [--thread T]`), cleaned up automatically when the goal
finishes (done, parked, or failed — no gate). Each tick checks every goal with an open ledger claim
for a registered marker whose pid has genuinely died. `notify.email` is stdlib `smtplib` only — zero
new dependency — and **never accepts a literal password**: `pass_env` names an environment variable
(default `SIGMA_SMTP_PASS`), since `config.json` is git-committed. Repository-supplied SMTP settings are refused unless you export `SIGMA_ALLOW_REPO_SMTP=1` in your own shell, and `pass_env` must be `SIGMA_SMTP_PASS` or start with `SIGMA_SMTP_` (#707 family, TM-04). A misconfigured or failing
send is loud on stderr and always falls back to a ledger note addressed to the goal's claimant —
never silently drops a notification. Exactly-once per dead pid: a fresh `agent-start` is what makes
it eligible to notify again.

### Watching for a new comment on a claimed issue

A comment landing on an issue while someone (or something) holds an open claim on it is invisible
unless they happen to re-check GitHub manually — for an unattended overnight drain, a comment
carrying something urgent (a correction, a blocker, a "stop, don't do X") can sit unseen for however
long the goal takes to finish. Turn it on:

```json
"comment_watch": { "enabled": true }
```

Needs `ledger.enabled` too — `watch_daemon.py`'s own tick is what runs the check — and github discovery
(comments aren't a concept for local goal files). Each tick fetches comments for every issue with an
open ledger claim and diffs them against a per-issue, per-machine cursor
(`.sdlc/state/comment-watch-cursor.json`, gitignored, never the shared ledger branch): a genuinely
new comment writes a ledger note addressed to the claimant, reusing the exact same inbox mechanism
the mention-watcher above already delivers through — no new channel. The claimant commenting on
their own claimed issue is suppressed (compares the comment's real GitHub author against the
claimant directly, correct even across machines). Exactly-once per comment: a second tick over the
same comment notifies nothing, and a second, later, genuinely *different* comment on the same issue
still notifies — each comment carries the underlying GitHub comment id as its own ledger `ref`, so
distinct comments never collide on the same suppression signature. Comment text is scrubbed the same
way `why` already is everywhere else in the ledger before it is ever written.
